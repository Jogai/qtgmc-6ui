# Shared by the zig recipes: offline 'zig build' with a private global cache that is
# pre-seeded from the pinned tarballs.  Every 'zig fetch' result is compared with the
# zig_hash in plugins.json (= the hash in upstream build.zig.zon).
zig_seed_cache() {  # <component>
    export ZIG_GLOBAL_CACHE_DIR=$WORK/zig-global ZIG_LOCAL_CACHE_DIR=$WORK/zig-local
    local file want got
    while IFS=$'\t' read -r file want; do
        got=$(cd "$S" && zig fetch --global-cache-dir "$ZIG_GLOBAL_CACHE_DIR" "$(src_file "$file")")
        [[ $got == "$want" ]] || die "zig hash of $file is $got, expected $want"
    done < <(manifest zighashes "$1")
}

zig_build() {  # extra args...
    # zig compiles C (FFTW) with its bundled clang and ignores CFLAGS; clang's driver
    # still honours CCC_OVERRIDE_OPTIONS, used here to keep $WORK out of __FILE__.
    (cd "$S" && CCC_OVERRIDE_OPTIONS="+-ffile-prefix-map=$WORK=/usr/src/qtgmc-6ui/$NAME" zig build \
        --global-cache-dir "$ZIG_GLOBAL_CACHE_DIR" --cache-dir "$ZIG_LOCAL_CACHE_DIR" \
        --prefix "$WORK/out" -j"$JOBS" --summary none \
        -Doptimize=ReleaseFast -Dtarget=x86_64-linux-gnu -Dcpu="$ZIG_CPU" "$@")
}
