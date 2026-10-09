# dfttest2 (CPU backend): CMake project of the vs-wheels PyPI sdist.
recipe_build() {
    local inc
    inc=$("$PYTHON" -I -c 'import vapoursynth as vs; print(vs.get_include())')
    cmake -S "$S" -B "$WORK/build" -G Ninja \
        -DCMAKE_BUILD_TYPE=None \
        -DCMAKE_CXX_FLAGS="$CXXFLAGS -ffast-math" \
        -DCMAKE_MODULE_LINKER_FLAGS="$LDFLAGS" \
        -DSKBUILD_PROJECT_VERSION="$(manifest get dfttest2 version)" \
        -DVAPOURSYNTH_INCLUDE_DIR="$inc"
    cmake --build "$WORK/build" -j "$JOBS"
}

recipe_install() {
    install_so "$WORK/build/libdfttest2_cpu.so"
}
