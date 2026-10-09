# akarin: meson, LLVM JIT backend built against Arch's llvm22 (shared libLLVM).
LLVM_CONFIG=${QTGMC6UI_LLVM_CONFIG:-/usr/lib/llvm22/bin/llvm-config}

recipe_build() {
    [[ -x $LLVM_CONFIG ]] || die "akarin needs llvm22 ($LLVM_CONFIG missing)"
    printf "[binaries]\nllvm-config = '%s'\n" "$LLVM_CONFIG" >"$WORK/native.ini"
    meson setup "$WORK/build" "$S" --native-file "$WORK/native.ini" \
        --prefix=/usr --buildtype=release --wrap-mode=nodownload \
        -Dstatic-llvm=false -Dasmjit=false
    meson compile -C "$WORK/build" -j "$JOBS"
}

recipe_install() {
    install_so "$WORK/build/libakarin.so"
}
