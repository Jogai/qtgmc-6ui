#!/usr/bin/env bash
# Build the qtgmc-6ui package from the WORKING TREE (no push/tag needed).
#
#   packaging/local-build.sh [--prepare-only] [--committed] [--force] [--builddir DIR] [makepkg args...]
#
# 1. tars the working tree (git ls-files: tracked + untracked-but-not-ignored files, as they
#    are on disk) into DIR/qtgmc-6ui-<pkgver>.tar.gz with the qtgmc-6ui-<pkgver>/ prefix the
#    GitHub release archive has,
#    (--committed: `git archive HEAD` instead, i.e. without uncommitted changes),
# 2. copies packaging/aur/PKGBUILD to DIR,
# 3. runs `QTGMC6UI_LOCAL_SRC=<tarball> makepkg -f [makepkg args]` in DIR (skipped with
#    --prepare-only; the tarball also sits next to the PKGBUILD under the name makepkg
#    expects, so extra-x86_64-build/makechrootpkg in DIR use it without the variable).
# DIR defaults to ${XDG_CACHE_HOME:-~/.cache}/qtgmc-6ui-build.  Prints DIR on stdout.
# Refuses a DIR that is a git checkout (e.g. your AUR clone) or holds a different PKGBUILD,
# unless --force.
# Typical: packaging/local-build.sh -s     (makepkg -s installs missing deps with sudo)
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
top=$(git -C "$here" rev-parse --show-toplevel)
builddir=${XDG_CACHE_HOME:-$HOME/.cache}/qtgmc-6ui-build
prepare_only=0
force=0
committed=0
while (($#)); do
  case $1 in
    --prepare-only) prepare_only=1; shift ;;
    --force) force=1; shift ;;
    --committed) committed=1; shift ;;
    --builddir) builddir=$2; shift 2 ;;
    *) break ;;
  esac
done

pkgver=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$top/src/qtgmc_6ui/__init__.py")
[[ -n $pkgver ]] || { echo "local-build: cannot read __version__" >&2; exit 1; }
grep -qx "pkgver=$pkgver" "$here/aur/PKGBUILD" \
  || { echo "local-build: PKGBUILD pkgver != $pkgver; run python3 -I packaging/sync-pkgbuild.py" >&2; exit 1; }

mkdir -p "$builddir"
builddir=$(cd "$builddir" && pwd)
if ((!force)); then
  if [[ -e $builddir/.git ]]; then
    echo "local-build: $builddir is a git checkout (AUR clone?); use another --builddir or --force" >&2; exit 1
  fi
  if [[ -f $builddir/PKGBUILD ]] && ! grep -q '^pkgname=qtgmc-6ui$' "$builddir/PKGBUILD"; then
    echo "local-build: $builddir/PKGBUILD is not a qtgmc-6ui PKGBUILD; use another --builddir or --force" >&2; exit 1
  fi
fi
tarball=$builddir/qtgmc-6ui-$pkgver.tar.gz
if ((committed)); then
  git -C "$top" archive --format=tar --prefix="qtgmc-6ui-$pkgver/" HEAD | gzip -n >"$tarball.part"
else
# Reproducible tarball of what is on disk; files deleted but still tracked are skipped.
(cd "$top" && git ls-files -z --cached --others --exclude-standard \
   | while IFS= read -r -d '' f; do [[ -e $f || -L $f ]] && printf '%s\0' "$f"; done \
   | tar --null -T - --sort=name --owner=0 --group=0 --numeric-owner \
         --mtime="@${SOURCE_DATE_EPOCH:-0}" --transform "s,^,qtgmc-6ui-$pkgver/," -cf - \
   | gzip -n >"$tarball.part")
fi
mv "$tarball.part" "$tarball"
install -m644 "$here/aur/PKGBUILD" "$builddir/PKGBUILD"
echo "local-build: $tarball ($(du -h "$tarball" | cut -f1))" >&2

if ((!prepare_only)); then
  (cd "$builddir" && QTGMC6UI_LOCAL_SRC=$tarball makepkg -f "$@" >&2)
fi
echo "$builddir"
