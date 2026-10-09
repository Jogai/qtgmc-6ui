#!/usr/bin/env bash
# Packaging test for qtgmc-6ui, meant for the developer's Arch/EndeavourOS machine.
#
#   packaging/test-package.sh [options] [REFERENCE_VIDEO]
#
# Steps (default: build,namcap,install,test):
#   build        tarball of the working tree + PKGBUILD in BUILDDIR (packaging/local-build.sh),
#                then a CLEAN CHROOT build with `extra-x86_64-build -c` when devtools is
#                installed (catches missing depends/makedepends), else plain `makepkg -sf`
#   namcap       namcap on PKGBUILD and package (pydepends excluded: venv modules are bundled)
#   install      sudo pacman -U the package (pacman asks for confirmation)
#   test         as the current user: qtgmc-6ui version, doctor (must pass), analyze and
#                deint REFERENCE_VIDEO into OUTDIR, then ffprobe checks of the master:
#                2x frames, 2x frame rate, same size/SAR, FLAC audio of the same duration
#   chroot-test  (devtools only) the same test inside a throw-away copy of the clean build
#                chroot with only the package and its depends installed (runtime-deps check)
#
# Options:
#   --no-chroot       build with makepkg even if devtools is present
#   --steps LIST      comma-separated subset of the steps above
#   --builddir DIR    default ${XDG_CACHE_HOME:-~/.cache}/qtgmc-6ui-build
#   --outdir DIR      default BUILDDIR/test-output
#   --pkg FILE        package to install/test (default: what `makepkg --packagelist` names, else
#                     the newest qtgmc-6ui-*-x86_64.pkg.tar* in BUILDDIR; any PKGEXT/PKGDEST)
#   --committed       build `git archive HEAD` instead of the working tree
#   --force           allow a BUILDDIR that is a git checkout or holds a different PKGBUILD
#                     (e.g. your AUR clone; it would be overwritten)
#
# Examples:
#   packaging/test-package.sh ~/video/test_input.avi
#   packaging/test-package.sh --steps chroot-test ~/video/test_input.avi
#   packaging/test-package.sh --no-chroot --steps build,namcap
# Exit status 0 only if every requested step passed.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
builddir=${XDG_CACHE_HOME:-$HOME/.cache}/qtgmc-6ui-build
outdir=
steps=build,namcap,install,test
use_chroot=1
force=
committed=
pkg=
ref=

say() { printf '\n==> %s\n' "$*" >&2; }
die() { printf '==> FAIL: %s\n' "$*" >&2; exit 1; }
want() { [[ ",$steps," == *",$1,"* ]]; }
as_root() { if ((EUID == 0)); then "$@"; else sudo "$@"; fi; }

while (($#)); do
  case $1 in
    --no-chroot) use_chroot=0 ;;
    --force) force=--force ;;
    --committed) committed=--committed ;;
    --steps) steps=$2; shift ;;
    --builddir) builddir=$2; shift ;;
    --outdir) outdir=$2; shift ;;
    --pkg) pkg=$(realpath "$2"); shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) ref=$(realpath "$1") ;;
  esac
  shift
done
mkdir -p "$builddir"
builddir=$(cd "$builddir" && pwd)
outdir=${outdir:-$builddir/test-output}
command -v extra-x86_64-build >/dev/null || use_chroot=0
if (want test || want chroot-test) && [[ -z $ref ]]; then
  die "REFERENCE_VIDEO is required for the test steps"
fi
[[ -z $ref || -f $ref ]] || die "no such file: $ref"

newest_pkg() {  # never fails; prints nothing when there is no package
  local f
  if [[ -f $builddir/PKGBUILD ]] && command -v makepkg >/dev/null; then
    while IFS= read -r f; do
      [[ -f $f ]] && { printf '%s\n' "$f"; return 0; }
    done < <(cd "$builddir" && makepkg --packagelist 2>/dev/null | grep -- '-x86_64\.pkg\.tar' || true)
  fi
  # shellcheck disable=SC2012,SC2010  # names are ours, no odd characters
  ls -t "$builddir"/qtgmc-6ui-[0-9]*-x86_64.pkg.tar* 2>/dev/null | grep -v '\.sig$' | head -n1 || true
}
need_pkg() {
  [[ -n $pkg && -f $pkg ]] || die "no qtgmc-6ui package found in $builddir (or PKGDEST); build first (--steps build) or pass --pkg FILE"
}

# ------------------------------------------------------------------ build
if want build; then
  say "sync check (PKGBUILD generated blocks vs plugins.json / uv.lock)"
  python3 -I "$here/sync-pkgbuild.py" --check || die "run: python3 -I packaging/sync-pkgbuild.py"
  "$here/local-build.sh" --prepare-only $force $committed --builddir "$builddir" >/dev/null
  rm -f "$builddir"/qtgmc-6ui-[0-9]*-x86_64.pkg.tar*
  if ((use_chroot)); then
    say "clean chroot build: extra-x86_64-build -c (sudo)"
    (cd "$builddir" && extra-x86_64-build -c)
  else
    say "plain build: makepkg -sf (NOT a clean chroot: missing depends may go unnoticed)"
    # The working-tree tarball sits next to the PKGBUILD under the expected name.
    (cd "$builddir" && makepkg -sf --noconfirm)
  fi
  [[ -n $pkg ]] || pkg=$(newest_pkg)
  need_pkg
  say "built $pkg"
fi
[[ -n $pkg ]] || pkg=$(newest_pkg)

# ------------------------------------------------------------------ namcap
if want namcap; then
  need_pkg
  command -v namcap >/dev/null || die "namcap not installed (pacman -S namcap)"
  say "namcap PKGBUILD"
  namcap "$builddir/PKGBUILD" || true
  say "namcap $(basename "$pkg")  (-e pydepends: bundled venv modules are not pacman deps)"
  namcap -e pydepends "$pkg" || true
fi

# ------------------------------------------------------------------ install
if want install; then
  need_pkg
  say "install $(basename "$pkg")"
  as_root pacman -U "$pkg"
fi

# ------------------------------------------------------------------ test
# ffprobe checks of the master; runs where the package runs (host or test chroot).
write_verify() {
  cat >"$1/verify-master.py" <<'EOF'
import json, subprocess, sys
from fractions import Fraction
def probe(p):
    cmd = ["ffprobe", "-v", "error", "-count_packets", "-show_streams", "-show_format", "-of", "json", p]
    return json.loads(subprocess.run(cmd, capture_output=True, check=True, text=True).stdout)
src, dst = (probe(p) for p in sys.argv[1:3])
v = lambda d: next(s for s in d["streams"] if s["codec_type"] == "video")
a = lambda d: [s for s in d["streams"] if s["codec_type"] == "audio"]
sv, dv = v(src), v(dst)
n_src, n_dst = int(sv["nb_read_packets"]), int(dv["nb_read_packets"])
checks = [
    ("frames doubled", n_dst == 2 * n_src, f"{n_src} -> {n_dst}"),
    ("rate doubled", Fraction(dv["r_frame_rate"]) == 2 * Fraction(sv["r_frame_rate"]),
     f'{sv["r_frame_rate"]} -> {dv["r_frame_rate"]}'),
    ("size kept", (sv["width"], sv["height"]) == (dv["width"], dv["height"]), f'{dv["width"]}x{dv["height"]}'),
    ("SAR kept", sv.get("sample_aspect_ratio") == dv.get("sample_aspect_ratio"),
     f'{sv.get("sample_aspect_ratio")} -> {dv.get("sample_aspect_ratio")}'),
    ("pix_fmt kept", sv["pix_fmt"] == dv["pix_fmt"], f'{sv["pix_fmt"]} -> {dv["pix_fmt"]}'),
    ("video ffv1", dv["codec_name"] == "ffv1", dv["codec_name"]),
    ("audio tracks kept", len(a(src)) == len(a(dst)), f"{len(a(src))} -> {len(a(dst))}"),
    ("audio flac", all(s["codec_name"] == "flac" for s in a(dst)), ",".join(s["codec_name"] for s in a(dst))),
    ("duration kept", abs(float(src["format"]["duration"]) - float(dst["format"]["duration"])) < 0.1,
     f'{float(src["format"]["duration"]):.2f}s -> {float(dst["format"]["duration"]):.2f}s'),
]
for name, ok, detail in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
sys.exit(0 if all(ok for _, ok, _ in checks) else 1)
EOF
}

# run_tests OUTDIR [prefix command...]: doctor + reference encode + ffprobe checks.  OUTDIR and
# REFERENCE_VIDEO have the same paths for the prefix command (bind mounts in the chroot).
run_tests() {
  local out=$1; shift
  local -a run=("$@")
  mkdir -p "$out"
  write_verify "$out"
  say "qtgmc-6ui version"
  "${run[@]}" qtgmc-6ui version
  say "qtgmc-6ui doctor"
  "${run[@]}" qtgmc-6ui doctor || die "doctor reported problems (see above)"
  say "qtgmc-6ui analyze $(basename "$ref")"
  "${run[@]}" qtgmc-6ui analyze "$ref" || die "analyze failed"
  say "qtgmc-6ui deint $(basename "$ref") -> $out"
  "${run[@]}" qtgmc-6ui deint "$ref" --out-dir "$out" --overwrite --json >"$out/result.json" \
    || { cat "$out/result.json" >&2; die "deint failed"; }
  local master
  master=$(python3 -I -c 'import json,sys
r=json.load(open(sys.argv[1])); print(next(o["path"] for o in r["outputs"] if o["kind"]=="master"))' \
    "$out/result.json") || die "no master in $out/result.json"
  say "verify $(basename "$master")"
  "${run[@]}" python3 -I "$out/verify-master.py" "$ref" "$master" || die "master does not match the expectations"
  say "OK: $master"
}

if want test; then
  command -v qtgmc-6ui >/dev/null || die "qtgmc-6ui is not installed (step install)"
  command -v ffprobe >/dev/null || die "ffprobe missing although ffmpeg is a dependency: broken install?"
  run_tests "$outdir" env
fi

if want chroot-test; then
  command -v arch-nspawn >/dev/null || die "chroot-test needs devtools (arch-nspawn)"
  need_pkg
  base=/var/lib/archbuild/extra-x86_64/root
  troot=/var/lib/archbuild/extra-x86_64/qtgmc-6ui-test
  [[ -d $base/usr/bin ]] || die "$base missing: run the build step with devtools first"
  ! mountpoint -q "$troot" 2>/dev/null || die "$troot is a mount point; refusing to touch it"
  say "throw-away chroot $troot (copy of the clean build root: base-devel + nothing else)"
  trap 'as_root rm -rf --one-file-system "$troot"' EXIT
  as_root rm -rf --one-file-system "$troot"
  as_root cp -a --reflink=auto "$base" "$troot"
  mkdir -p "$outdir/chroot"
  # pacman resolves the package's depends from the repos: this is the runtime-deps check.
  as_root arch-nspawn "$troot" --bind-ro="$(dirname "$pkg")" pacman -U --noconfirm "$pkg"
  # The tests run as an unprivileged user with the caller's uid, so HOME/XDG paths are a
  # normal user's and the outputs in OUTDIR stay owned by the caller.
  as_root arch-nspawn "$troot" useradd -m -o -u "$(id -u)" qtgmctest
  run_tests "$outdir/chroot" as_root arch-nspawn "$troot" --user=qtgmctest \
    --bind-ro="$ref" --bind="$outdir/chroot"
fi
say "all requested steps passed ($steps)"
