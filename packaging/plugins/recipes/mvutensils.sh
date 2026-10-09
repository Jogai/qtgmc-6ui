# mvutensils v9: meson; only the baseline library is built (the .avx2/.zn4
# variants share the plugin id and are never loaded by qtgmc-6ui).
recipe_build() {
    meson setup "$WORK/build" "$S" --prefix=/usr --buildtype=release --wrap-mode=nofallback
    ninja -C "$WORK/build" -j "$JOBS" mvutensils.so
}

recipe_install() {
    install_so "$WORK/build/mvutensils.so"
}
