# Shared by the zig recipes.  Zig is taken from the pinned official release tarball listed in
# plugins.json ("toolchain"), not from Arch: zig's language, std and CLI break on every minor
# release (0.17 cannot build zsmooth 0.20.0), and Arch ships only the newest zig.
# The build is offline: a private global cache is pre-seeded from the pinned dependency
# tarballs and every 'zig fetch' result is compared with the zig_hash in plugins.json
# (= the hash in upstream build.zig.zon).  Cache locations go through the ZIG_*_CACHE_DIR
# environment variables (stable across zig versions; the CLI flags are not).
zig_setup() {  # <component>
    local file want
    IFS=$'\t' read -r file want < <(manifest toolchain "$1")
    [[ -n $file ]] || die "$1: no zig toolchain in plugins.json"
    unpack "$file" "$WORK/zig-toolchain"
    ZIG=$WORK/zig-toolchain/zig
    [[ $("$ZIG" version) == "$want" ]] || die "zig toolchain $file is not version $want"
    export ZIG_GLOBAL_CACHE_DIR=$WORK/zig-global ZIG_LOCAL_CACHE_DIR=$WORK/zig-local
    local got
    while IFS=$'\t' read -r file want; do
        got=$(cd "$S" && "$ZIG" fetch "$(src_file "$file")")
        [[ $got == "$want" ]] || die "zig hash of $file is $got, expected $want"
    done < <(manifest zighashes "$1")
}

zig_build() {  # extra args...
    # zig compiles C (FFTW) with its bundled clang and ignores CFLAGS; clang's driver
    # still honours CCC_OVERRIDE_OPTIONS, used here to keep $WORK out of __FILE__.
    (cd "$S" && CCC_OVERRIDE_OPTIONS="+-ffile-prefix-map=$WORK=/usr/src/qtgmc-6ui/$NAME" "$ZIG" build \
        --prefix "$WORK/out" -j"$JOBS" --summary none \
        -Doptimize=ReleaseFast -Dtarget=x86_64-linux-gnu -Dcpu="$ZIG_CPU" "$@")
}
