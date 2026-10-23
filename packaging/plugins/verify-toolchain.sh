#!/usr/bin/env bash
# Verify the pinned Zig toolchain tarball (the build tool of zsmooth and vszip) against its signed release:
# the sha256 pin in plugins.json AND the minisign signature published next to the tarball on ziglang.org.
# Run it whenever the toolchain pin is bumped, before the new hash is trusted. Needs minisign (extra/minisign).
#   packaging/plugins/verify-toolchain.sh [TARBALL]     TARBALL: a copy that is already downloaded
set -euo pipefail

# Zig release key, as printed on https://ziglang.org/download/ (compare it there if this ever fails)
key='RWSGOq2NVecA2UPNdBUZykf1CCb147pkmdtYxgb3Ti+JO/wCYvhbAb/U'
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

command -v minisign >/dev/null || { echo "verify-toolchain: minisign is missing (pacman -S minisign)" >&2; exit 1; }
read -r file _ < <(python3 -I "$here/manifest.py" toolchain zsmooth)
read -r _ _ url sha < <(python3 -I "$here/manifest.py" sources zsmooth | awk -F'\t' -v f="$file" '$2 == f')
[[ -n ${url:-} && -n ${sha:-} ]] || { echo "verify-toolchain: no pin for $file in plugins.json" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tarball=${1:-$work/$file}
[[ -f $tarball ]] || curl -fsSL "$url" -o "$tarball"
curl -fsSL "$url.minisig" -o "$work/signature"

got=$(sha256sum "$tarball" | cut -d' ' -f1)
[[ $got == "$sha" ]] || { echo "verify-toolchain: sha256 $got differs from the pin $sha" >&2; exit 1; }
minisign -Vm "$tarball" -x "$work/signature" -P "$key"
echo "verify-toolchain: $file is the signed official release and matches the pin"
