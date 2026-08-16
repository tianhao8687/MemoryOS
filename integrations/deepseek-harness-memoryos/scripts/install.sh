#!/usr/bin/env sh
set -eu

profile="${1:-memoryos}"
version="$(dsh --version 2>&1)"
case "$version" in
  *0.1.0-rc.5*) ;;
  *) echo "DeepSeek Harness 0.1.0-rc.5 is required; found: $version" >&2; exit 1 ;;
esac
package_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
pack_root="$(mktemp -d "${TMPDIR:-/tmp}/dsh-memoryos.XXXXXX")"
trap 'rm -rf -- "$pack_root"' EXIT HUP INT TERM
(cd "$package_root" && pnpm pack --pack-destination "$pack_root")
set -- "$pack_root"/*.tgz
[ "$#" -eq 1 ] && [ -f "$1" ] || {
  echo "Expected exactly one DeepSeek Harness plugin tarball" >&2
  exit 1
}
dsh plugin --profile "$profile" add "$1"
