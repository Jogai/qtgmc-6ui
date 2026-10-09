# vszip: zig build, like zsmooth.
. "$HERE/recipes/zig-common.sh"

recipe_build() {
    zig_seed_cache vszip
    zig_build
}

recipe_install() {
    install_so "$WORK/out/lib/libvszip.so"
}
