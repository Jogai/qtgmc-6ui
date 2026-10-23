# qtgmc-6UI development

## Setup

```
uv venv --system-site-packages --python /usr/bin/python3.14 .venv
uv sync --frozen
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

This needs the Arch `vapoursynth`, `bestsource`, `ffmpeg` and `pyside6` packages in the system Python. Native plugins can be built with `packaging/plugins/build.sh` into any directory and used with `QTGMC6UI_PLUGIN_DIR` (and `QTGMC6UI_BUILD_INFO`) for development. Pytest markers: `needs_sandbox` (ffmpeg, vapoursynth, bestsource), `needs_qtgmc` (full stack), `needs_tool(name)` (x264/x265/kvazaar/sox), `slow`, `gui`. Development and the full suite (750 tests) were run in an Arch environment; the test fixtures (synthetic clips, reference file) come from `tests/fixtures_gen.py`. After editing pins or `uv.lock` run `python3 -I packaging/sync-pkgbuild.py` (`--check` to verify).

## Package builds and tests

Local test build from a working tree of this repository (needs `base-devel`; `-s` lets makepkg install the makedepends with sudo):

```
packaging/local-build.sh -s            # tars the tree, runs makepkg in ~/.cache/qtgmc-6ui-build
sudo pacman -U ~/.cache/qtgmc-6ui-build/qtgmc-6ui-0.4.0-1-x86_64.pkg.tar.zst
qtgmc-6ui doctor
```

Full package test (clean-chroot build if `devtools` is installed, namcap, `pacman -U`, doctor, reference encode with checks on the result):

```
packaging/test-package.sh /path/to/interlaced-clip.avi
packaging/test-package.sh --no-chroot --steps build,namcap     # subset
```

The packaging notes (generated PKGBUILD blocks, `sync-pkgbuild.py`, the release steps, namcap warnings and the known risks) are in [packaging/aur/PKGBUILD.md](packaging/aur/PKGBUILD.md).

## Continuous integration

Four workflows run on GitHub. `ci.yml` runs on every push to master and every pull request, in the `ghcr.io/astral-sh/uv` image (Debian): ruff, actionlint and shellcheck (their versions are pinned in the workflow), `uv lock --check`, `sync-pkgbuild.py --check`, a mypy run over `src/` (Qt, vapoursynth and psutil count as untyped there), the roughly 400 tests that need neither ffmpeg nor the native plugins with a coverage floor on the non-GUI code (`fail_under` in `pyproject.toml`; the GUI is covered by the local suite only), and a wheel build with a content check. It takes about two minutes. The full suite (750 tests, about 5 minutes) needs the Arch stack and runs only locally; see Setup. `aur.yml` publishes a release tag to the AUR without building the package (the release is live in about a minute), `arch-canary.yml` builds and smoke-tests master against current Arch every Wednesday, and `upstream-checks.yml` runs weekly network checks: the pinned plugin sources still match their sha256, `uv.lock` has no known vulnerabilities and the links in the documentation resolve. Before a release that changes `packaging/` or the plugin pins, start the canary by hand (Actions, Arch canary, Run workflow) or run `packaging/test-package.sh` locally.

## Changelog draft

`cliff.toml` configures [git-cliff](https://github.com/orhun/git-cliff), which drafts the changelog of the next release from the commits since the last tag. Commits are grouped by area (GUI, Analysis, Pipeline and CLI, Internals, Packaging, CI and repository, Documentation, Tests) from the module prefix of each subject, with the prefix removed from the text; Conventional Commits types (`feat:`, `fix:`) get their own Added and Fixed groups if they are ever used. Release commits, fixups and merges are left out. Print the draft with `git cliff --unreleased --tag v0.4.0` (Arch: `pacman -S git-cliff`; elsewhere: `uvx git-cliff`) and edit it into the release commit description. The grouping is by area and not by Fixed or Changed, so the wording still needs a human.

## Releasing

Tag `vX.Y.Z` (it must equal `__version__`). `.github/workflows/aur.yml` then pins the tarball checksum, regenerates `.SRCINFO` and pushes both to the AUR, without building (install it yourself to check it; the weekly canary builds master); the one-time setup is described in `packaging/aur/PKGBUILD.md`.
