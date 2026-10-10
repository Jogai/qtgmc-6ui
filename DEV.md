# qtgmc-6UI development

## Setup

```
uv venv --system-site-packages --python /usr/bin/python3.14 .venv
uv sync --frozen
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

This needs the Arch `vapoursynth`, `bestsource`, `ffmpeg` and `pyside6` packages in the system Python. Native plugins can be built with `packaging/plugins/build.sh` into any directory and used with `QTGMC6UI_PLUGIN_DIR` (and `QTGMC6UI_BUILD_INFO`) for development. Pytest markers: `needs_sandbox` (ffmpeg, vapoursynth, bestsource), `needs_qtgmc` (full stack), `needs_tool(name)` (x264/x265/kvazaar/sox), `slow`, `gui`. Development and the full suite (516 tests) were run in an Arch environment; the test fixtures (synthetic clips, reference file) come from `tests/fixtures_gen.py`. After editing pins or `uv.lock` run `python3 -I packaging/sync-pkgbuild.py` (`--check` to verify).

## Package builds and tests

Local test build from a working tree of this repository (needs `base-devel`; `-s` lets makepkg install the makedepends with sudo):

```
packaging/local-build.sh -s            # tars the tree, runs makepkg in ~/.cache/qtgmc-6ui-build
sudo pacman -U ~/.cache/qtgmc-6ui-build/qtgmc-6ui-0.1.0-1-x86_64.pkg.tar.zst
qtgmc-6ui doctor
```

Full package test (clean-chroot build if `devtools` is installed, namcap, `pacman -U`, doctor, reference encode with checks on the result):

```
packaging/test-package.sh /path/to/interlaced-clip.avi
packaging/test-package.sh --no-chroot --steps build,namcap     # subset
```

The packaging notes (generated PKGBUILD blocks, `sync-pkgbuild.py`, the release steps, namcap warnings and the known risks) are in [packaging/aur/PKGBUILD.md](packaging/aur/PKGBUILD.md).

## Releasing

Tag `vX.Y.Z` (it must equal `__version__`). `.github/workflows/aur.yml` then builds and smoke-tests the package on Arch (`packaging/ci-smoke.sh`) and pushes `PKGBUILD` and `.SRCINFO` to the AUR; the one-time setup is described in `packaging/aur/PKGBUILD.md`.
