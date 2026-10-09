# znedi3 r3.3: upstream Makefile; graphengine and vsxx submodules from their own tarballs.
recipe_build() {
    unpack graphengine-7bb77a5c062c610e7f44df6a59155cc3ab3e452a.tar.gz "$S/graphengine"
    unpack vsxx-11eb026af5517d69e1d3d6258d1b2c2040c508e7.tar.gz "$S/vsxx"
    make -C "$S" -j "$JOBS" X86=1 X86_AVX512=1 \
        CFLAGS="$CFLAGS" CXXFLAGS="$CXXFLAGS" LDFLAGS="$LDFLAGS" vsznedi3.so
}

recipe_install() {
    install_so "$S/vsznedi3.so"
    install_data "$S/nnedi3_weights.bin"
}
