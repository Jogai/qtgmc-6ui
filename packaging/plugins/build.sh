#!/usr/bin/env bash
# Build the bundled native VapourSynth plugins of qtgmc-6ui from pinned sources.
#
#   build.sh <component|set>[,...] <srcdir> <destdir>
#       Build and install into <destdir> (flat: *.so + data files such as
#       nnedi3_weights.bin).  <srcdir> must contain every source file named in
#       plugins.json (makepkg's $srcdir: the downloaded files or their symlinks;
#       extracted copies are ignored).  No network access.  Sets: required,
#       all (= what the package ships), extra, everything.
#   build.sh fetch <cachedir> [component|set...]
#       Download missing sources into <cachedir> (by URL) and verify sha256.
#   build.sh verify-sources <srcdir> [component|set...]
#   build.sh check <destdir> [component|set...]
#       Load every installed plugin with autoload disabled, print its table as
#       JSON and fail on references to the build tree.
#   build.sh names [component|set...]
#
# Environment:
#   QTGMC6UI_MARCH        x86-64 (default, Arch baseline) | x86-64-v3 | native
#   CFLAGS CXXFLAGS LDFLAGS  honoured as given (makepkg's).  When unset they are
#                         read from /etc/makepkg.conf.
#   QTGMC6UI_BUILDDIR     scratch dir (default <srcdir>/qtgmc6ui-plugins-build; wiped per component)
#   QTGMC6UI_LICENSE_DIR  if set, licence files are copied to <dir>/<component>/
#   QTGMC6UI_STRIP        1 (default) strips the installed .so; 0 leaves it to makepkg
#   QTGMC6UI_JOBS         parallel jobs (default: nproc)
#   QTGMC6UI_KEEP_BUILD   1 keeps the scratch dir after a successful build
set -euo pipefail
shopt -s nullglob

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PYTHON=${PYTHON:-python3}

manifest() { "$PYTHON" -I "$HERE/manifest.py" "$@"; }
msg() { printf '==> plugins: %s\n' "$*" >&2; }
die() { printf '==> plugins: ERROR: %s\n' "$*" >&2; exit 1; }

split_targets() { tr ',' ' ' <<<"$1"; }

# ----------------------------------------------------------------- sources
verify_sources() {  # <dir> <targets...>
    local dir=$1; shift
    local comp file url sum bad=0
    while IFS=$'\t' read -r comp file url sum; do
        if [[ ! -f $dir/$file ]]; then
            printf 'missing  %s (%s)\n' "$file" "$comp" >&2; bad=1; continue
        fi
        if [[ $(sha256sum <"$dir/$file" | cut -d' ' -f1) != "$sum" ]]; then
            printf 'BAD SUM  %s (%s)\n' "$file" "$comp" >&2; bad=1
        fi
    done < <(manifest sources "$@")
    return $bad
}

fetch() {  # <cachedir> <targets...>
    local dir=$1; shift
    mkdir -p "$dir"
    local comp file url sum
    while IFS=$'\t' read -r comp file url sum; do
        if [[ -f $dir/$file && $(sha256sum <"$dir/$file" | cut -d' ' -f1) == "$sum" ]]; then
            continue
        fi
        msg "fetch $file"
        curl -fsSL --retry 3 -o "$dir/$file.part" "$url"
        [[ $(sha256sum <"$dir/$file.part" | cut -d' ' -f1) == "$sum" ]] \
            || die "sha256 mismatch for $file from $url"
        mv "$dir/$file.part" "$dir/$file"
    done < <(manifest sources "$@")
}

# ----------------------------------------------------------------- flags
setup_flags() {
    if [[ -z ${CFLAGS+x} && -z ${CXXFLAGS+x} && -r /etc/makepkg.conf ]]; then
        # shellcheck disable=SC1091
        eval "$(. /etc/makepkg.conf >/dev/null 2>&1
                printf 'CFLAGS=%q\nCXXFLAGS=%q\nLDFLAGS=%q\n' "${CFLAGS-}" "${CXXFLAGS-}" "${LDFLAGS-}")"
    fi
    : "${CFLAGS:=-march=x86-64 -mtune=generic -O2 -pipe -fno-plt -fexceptions -fstack-clash-protection -fcf-protection}"
    : "${CXXFLAGS:=$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS}"
    : "${LDFLAGS:=-Wl,-O1 -Wl,--sort-common -Wl,--as-needed -Wl,-z,relro -Wl,-z,now}"

    MARCH=${QTGMC6UI_MARCH:-x86-64}
    case $MARCH in
        x86-64) ZIG_CPU=x86_64 ;;
        x86-64-v2) ZIG_CPU=x86_64_v2 ;;
        x86-64-v3) ZIG_CPU=x86_64_v3 ;;
        x86-64-v4) ZIG_CPU=x86_64_v4 ;;
        native) ZIG_CPU=native ;;
        *) die "QTGMC6UI_MARCH must be x86-64, x86-64-v2, x86-64-v3, x86-64-v4 or native (got '$MARCH')" ;;
    esac
    if [[ $MARCH != x86-64 ]]; then
        local tune=generic; [[ $MARCH == native ]] && tune=native
        local f
        for f in CFLAGS CXXFLAGS; do
            local v=" ${!f} "
            v=$(sed -E 's/ -march=[^ ]+/ /g; s/ -mtune=[^ ]+/ /g' <<<"$v")
            printf -v "$f" '%s' "-march=$MARCH -mtune=$tune ${v# }"
        done
    fi
    JOBS=${QTGMC6UI_JOBS:-$(nproc)}
    export CFLAGS CXXFLAGS LDFLAGS MARCH ZIG_CPU JOBS
}

# ----------------------------------------------------------------- helpers for recipes
# src_file FILENAME          -> absolute path of a pinned source file
# unpack FILENAME DIR        -> extract a tarball into DIR (top-level dir stripped)
# install_so FILE [NAME]     -> install a shared object into $PLUGIN_DEST (mode 755)
# install_data FILE [NAME]   -> install a data file into $PLUGIN_DEST (mode 644)
src_file() {
    local f=$SRCDIR/$1
    [[ -f $f ]] || die "source file $1 not found in $SRCDIR"
    readlink -f "$f"
}
unpack() {
    mkdir -p "$2"
    tar -xf "$(src_file "$1")" -C "$2" --strip-components=1 --no-same-owner
}
install_so() { install -Dm755 "$1" "$PLUGIN_DEST/${2:-$(basename "$1")}"; }
install_data() { install -Dm644 "$1" "$PLUGIN_DEST/${2:-$(basename "$1")}"; }

# Runs in a subshell per component: recipe sees NAME, WORK (fresh scratch dir), S (main
# source tree = $WORK/src), SRCDIR, PLUGIN_DEST, flags, MARCH/ZIG_CPU, JOBS.
build_component() {
    local name=$1
    local recipe=$HERE/recipes/$name.sh
    [[ -f $recipe ]] || die "no recipe for $name"
    local work=$BUILDROOT/$name
    rm -rf "$work"; mkdir -p "$work"
    msg "build $name ($(manifest get "$name" tag), march $MARCH)"
    (
        set -euo pipefail
        WORK=$work S=$work/src NAME=$name
        # Map the scratch dir out of debug info / __FILE__ strings.
        CFLAGS="$CFLAGS -ffile-prefix-map=$work=/usr/src/qtgmc-6ui/$name"
        CXXFLAGS="$CXXFLAGS -ffile-prefix-map=$work=/usr/src/qtgmc-6ui/$name"
        export CFLAGS CXXFLAGS
        # Keep 'git describe' (meson vcs_tag) from describing an enclosing repository.
        export GIT_CEILING_DIRECTORIES=$work
        unset DESTDIR   # zig/meson/make would prepend it to install paths
        # shellcheck disable=SC1090
        . "$recipe"
        cd "$work"
        unpack "$(manifest sources "$name" | head -n1 | cut -f2)" "$S"
        recipe_build
        recipe_install
        if [[ -n ${QTGMC6UI_LICENSE_DIR:-} ]]; then
            local lf
            for lf in $(manifest get "$name" license_files); do
                install -Dm644 "$S/$lf" "$QTGMC6UI_LICENSE_DIR/$name/$(basename "$lf")"
            done
        fi
    )
    local f
    for f in $(manifest get "$name" files); do
        [[ -f $PLUGIN_DEST/$f ]] || die "$name did not install $f"
        if [[ $f == *.so && ${QTGMC6UI_STRIP:-1} == 1 ]]; then
            strip --strip-unneeded "$PLUGIN_DEST/$f"
        fi
    done
    [[ ${QTGMC6UI_KEEP_BUILD:-0} == 1 ]] || rm -rf "$work"
}

build() {  # <targets> <srcdir> <destdir>
    local targets=$1
    SRCDIR=$(cd "$2" && pwd)
    mkdir -p "$3"
    PLUGIN_DEST=$(cd "$3" && pwd)
    BUILDROOT=${QTGMC6UI_BUILDDIR:-$SRCDIR/qtgmc6ui-plugins-build}
    mkdir -p "$BUILDROOT"; BUILDROOT=$(cd "$BUILDROOT" && pwd)
    export SRCDIR PLUGIN_DEST BUILDROOT
    local -a names
    mapfile -t names < <(manifest names $(split_targets "$targets"))
    verify_sources "$SRCDIR" "${names[@]}" || die "sources incomplete or corrupt in $SRCDIR"
    setup_flags
    msg "CFLAGS=$CFLAGS"
    msg "LDFLAGS=$LDFLAGS"
    local n
    for n in "${names[@]}"; do build_component "$n"; done
    [[ ${QTGMC6UI_KEEP_BUILD:-0} == 1 ]] || rmdir "$BUILDROOT" 2>/dev/null || true
    msg "installed into $PLUGIN_DEST: $(cd "$PLUGIN_DEST" && echo *)"
}

check() {  # <destdir> <targets...>
    local dest=$1; shift
    "$PYTHON" -I "$HERE/check.py" "$dest" $(manifest names "$@")
}

[[ $# -ge 1 ]] || { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }
case $1 in
    fetch) [[ $# -ge 2 ]] || die "usage: build.sh fetch <cachedir> [targets...]"
           shift; d=$1; shift; fetch "$d" "${@:-all}" ;;
    verify-sources) [[ $# -ge 2 ]] || die "usage: build.sh verify-sources <srcdir> [targets...]"
           shift; d=$1; shift; verify_sources "$d" "${@:-all}" && msg "sources ok" ;;
    check) [[ $# -ge 2 ]] || die "usage: build.sh check <destdir> [targets...]"
           shift; d=$1; shift; check "$d" "${@:-all}" ;;
    names) shift; manifest names "${@:-all}" ;;
    -h|--help|help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) [[ $# -eq 3 ]] || die "usage: build.sh <component|set>[,...] <srcdir> <destdir>"
       build "$@" ;;
esac
