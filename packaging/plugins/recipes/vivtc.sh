# vivtc R2: plain meson.
recipe_build() {
    meson setup "$WORK/build" "$S" --prefix=/usr --buildtype=release --wrap-mode=nodownload
    meson compile -C "$WORK/build" -j "$JOBS"
}

recipe_install() {
    install_so "$WORK/build/vivtc.so"
}
