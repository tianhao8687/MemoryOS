from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from memoryos.db.models import RepositoryRow
from memoryos.db.session import Database
from memoryos.errors import NotFoundError


@dataclass(frozen=True)
class GitContext:
    root: Path
    branch: str
    head: str
    remote_url: str | None
    stable_key: str

    @property
    def branch_scope_key(self) -> str:
        """Return the scope key that binds memory to this repository and branch."""
        return f"{self.stable_key}:{self.branch}"


def _git(path: Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise NotFoundError("Git executable is not available")
    completed = subprocess.run(  # noqa: S603 - fixed executable, structured arguments
        [executable, "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise NotFoundError("path is not inside a readable Git repository")
    return completed.stdout.strip()


def normalize_remote(url: str) -> str:
    value = url.strip()
    if re.match(r"^[\w.-]+@[\w.-]+:", value):
        user_host, path = value.split(":", 1)
        host = user_host.split("@", 1)[1].lower()
        return f"ssh://{host}/{path.removesuffix('.git').strip('/').lower()}"
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname.lower() if parsed.hostname else parsed.netloc.lower()
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path.removesuffix(".git").rstrip("/").lower()
        return urlunsplit((parsed.scheme.lower(), f"{host}{port}", path, "", ""))
    return value.replace("\\", "/").removesuffix(".git").rstrip("/").lower()


def stable_repository_key(root: Path, remote_url: str | None) -> str:
    if remote_url:
        identity = f"remote:{normalize_remote(remote_url)}"
    else:
        git_dir = root / ".git"
        marker = git_dir / "memoryos-repository-id"
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
        seed = f"local:{root.resolve()}:{_git(root, 'rev-list', '--max-parents=0', 'HEAD')}"
        identity = f"local:{hashlib.sha256(seed.encode()).hexdigest()}"
        with suppress(OSError):
            marker.write_text(identity, encoding="utf-8")
        return identity
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def discover_git_context(path: Path | str = ".") -> GitContext:
    start = Path(path).expanduser().resolve()
    root = Path(_git(start, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(root, "branch", "--show-current") or "DETACHED"
    head = _git(root, "rev-parse", "HEAD")
    try:
        remote = _git(root, "remote", "get-url", "origin") or None
    except NotFoundError:
        remote = None
    return GitContext(
        root=root,
        branch=branch,
        head=head,
        remote_url=remote,
        stable_key=stable_repository_key(root, remote),
    )


def upsert_repository(database: Database, context: GitContext) -> dict[str, str | None]:
    with database.session() as session:
        row = session.scalar(
            select(RepositoryRow).where(RepositoryRow.stable_key == context.stable_key)
        )
        if row is None:
            row = RepositoryRow(
                stable_key=context.stable_key,
                name=context.root.name,
                path=str(context.root),
                remote_url=context.remote_url,
                default_branch=context.branch,
            )
            session.add(row)
        else:
            row.path = str(context.root)
            row.name = context.root.name
            row.remote_url = context.remote_url
        session.flush()
        return {
            "id": row.id,
            "stable_key": row.stable_key,
            "name": row.name,
            "path": row.path,
            "remote_url": row.remote_url,
            "branch": context.branch,
            "branch_scope_key": context.branch_scope_key,
            "head": context.head,
        }
