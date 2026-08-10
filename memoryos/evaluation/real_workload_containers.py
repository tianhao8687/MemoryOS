from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


class ContainerRuntimeError(RuntimeError):
    """Raised when the local container runtime cannot uphold the benchmark boundary."""


def default_container_user() -> str:
    """Return a non-root numeric user that can write host bind mounts when possible."""

    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if callable(getuid) and callable(getgid):
        uid = int(getuid())
        gid = int(getgid())
        if uid > 0 and gid >= 0:
            return f"{uid}:{gid}"
    return "65532:65532"


def prepare_writable_bind_mount(path: Path, user: str, *, recursive: bool) -> None:
    """Make a disposable bind mount writable by its declared numeric container user."""

    getuid = getattr(os, "getuid", None)
    chown = getattr(os, "chown", None)
    if not callable(getuid) or not callable(chown):
        return
    host_uid = int(getuid())
    uid_text, gid_text = user.split(":", maxsplit=1)
    uid = int(uid_text)
    gid = int(gid_text)
    if host_uid not in {0, uid}:
        raise ContainerRuntimeError(
            f"container uid {uid} must match non-root host uid {host_uid} for writable binds"
        )
    if host_uid != 0:
        return
    target = path.resolve(strict=True)
    candidates = [target]
    if recursive and target.is_dir():
        candidates.extend(_tree_without_following_links(target))
    for candidate in candidates:
        chown(candidate, uid, gid, follow_symlinks=False)


def make_bind_mount_world_readable(path: Path) -> None:
    """Expose a non-secret benchmark input to numeric container users without ownership coupling."""

    if not callable(getattr(os, "getuid", None)):
        return
    target = path.resolve(strict=True)
    if not target.is_file() or target.is_symlink():
        raise ContainerRuntimeError("read-only benchmark bind must be a regular file")
    target.chmod(0o444)


@dataclass(frozen=True)
class ContainerCommandResult:
    exit_code: int
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


class DockerEngine:
    def __init__(self, *, executable: Path | None = None) -> None:
        resolved = executable or _find_docker()
        self.executable = resolved.resolve()

    def create_network(self, name: str, *, internal: bool) -> None:
        arguments = ["network", "create"]
        if internal:
            arguments.append("--internal")
        arguments.append(name)
        self.control(arguments)

    def remove_network(self, name: str) -> None:
        self.control(["network", "rm", name], check=False)

    def remove_container(self, name: str) -> None:
        self.control(["container", "rm", "--force", name], check=False)

    def start_detached(self, arguments: list[str]) -> str:
        return self.control(["run", "--detach", *arguments]).strip()

    def probe_python_socket(
        self,
        container: str,
        *,
        python_command: str,
        host: str,
        port: int,
        timeout_seconds: float = 30,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        program = (
            "import socket; "
            f"connection=socket.create_connection(({host!r},{port}),timeout=1); "
            "connection.close()"
        )
        while time.monotonic() < deadline:
            result = self.control(
                ["exec", container, python_command, "-c", program],
                check=False,
            )
            if not result:
                return
            time.sleep(0.25)
        raise ContainerRuntimeError(f"container {container} did not open {host}:{port}")

    def run_attached(
        self,
        arguments: list[str],
        *,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        max_log_bytes: int,
    ) -> ContainerCommandResult:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed docker executable, argv only
                [str(self.executable), "run", *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ContainerRuntimeError(f"failed to start Docker: {exc}") from exc
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_state = [False]
        stderr_state = [False]
        stdout_thread = threading.Thread(
            target=_bounded_copy,
            args=(process.stdout, stdout_path, max_log_bytes, stdout_state),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_bounded_copy,
            args=(process.stderr, stderr_path, max_log_bytes, stderr_state),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            exit_code = 124
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        return ContainerCommandResult(
            exit_code=exit_code,
            duration_seconds=round(time.perf_counter() - started, 6),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_truncated=stdout_state[0],
            stderr_truncated=stderr_state[0],
            timed_out=timed_out,
        )

    def control(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout_seconds: int = 60,
    ) -> str:
        try:
            result = subprocess.run(  # noqa: S603 - fixed docker executable, argv only
                [str(self.executable), *arguments],
                check=check,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise ContainerRuntimeError(f"Docker command failed: {detail}") from exc
        if len(result.stdout) > 1_000_000 or len(result.stderr) > 1_000_000:
            raise ContainerRuntimeError("Docker control command exceeded its output limit")
        if not check and result.returncode != 0:
            return result.stderr.strip() or f"exit {result.returncode}"
        return result.stdout


def bind_mount(source: Path, destination: str, *, read_only: bool) -> str:
    resolved = source.resolve(strict=True)
    if "," in str(resolved) or "," in destination:
        raise ContainerRuntimeError("Docker bind paths must not contain commas")
    value = f"type=bind,src={resolved},dst={destination}"
    return f"{value},readonly" if read_only else value


def _bounded_copy(
    source: object,
    destination: Path,
    limit: int,
    truncated: list[bool],
) -> None:
    written = 0
    with destination.open("wb") as stream:
        while True:
            chunk = source.read(65_536)  # type: ignore[attr-defined]
            if not chunk:
                break
            remaining = max(0, limit - written)
            if remaining:
                stream.write(chunk[:remaining])
                written += min(len(chunk), remaining)
            if len(chunk) > remaining:
                truncated[0] = True


def _tree_without_following_links(root: Path) -> list[Path]:
    entries: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            entries.append(child)
            if child.is_dir() and not child.is_symlink():
                pending.append(child)
    return entries


def _find_docker() -> Path:
    executable = shutil.which("docker")
    if executable is None:
        raise ContainerRuntimeError("Docker is required for real-workload execution")
    return Path(executable)


__all__ = [
    "ContainerCommandResult",
    "ContainerRuntimeError",
    "DockerEngine",
    "bind_mount",
    "default_container_user",
    "make_bind_mount_world_readable",
    "prepare_writable_bind_mount",
]
