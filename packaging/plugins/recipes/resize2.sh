# resize2: meson; the zimg.wrap (wrap-git + patch_directory + diff_files) is
# reproduced by hand from the pinned tarballs, then meson runs with nodownload.
recipe_build() {
    local z=$S/subprojects/zimg
    unpack zimg-1ad1895d5ff0bbe69c61243f9996aede713d1b5f.tar.gz "$z"
    unpack graphengine-cb5b2ce13384ec2491f0c37256ea210034799f69.tar.gz "$z/graphengine"
    cp -r "$S/subprojects/packagefiles/zimg/." "$z/"
    patch -d "$z" -p1 --forward --no-backup-if-mismatch <"$S/subprojects/packagefiles/zimg/0001.patch"
    meson setup "$WORK/build" "$S" --prefix=/usr --buildtype=release --wrap-mode=nodownload
    meson compile -C "$WORK/build" -j "$JOBS"
}

recipe_install() {
    install_so "$WORK/build/libresize2.so"
}
