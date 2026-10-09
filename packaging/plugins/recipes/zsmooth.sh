# zsmooth: zig build (FFTW compiled statically from the pinned fftw.org tarball).
. "$HERE/recipes/zig-common.sh"

recipe_build() {
    zig_seed_cache zsmooth
    zig_build
}

recipe_install() {
    install_so "$WORK/out/lib/libzsmooth.so"
}
