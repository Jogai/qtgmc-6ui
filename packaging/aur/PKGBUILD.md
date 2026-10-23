# qtgmc-6ui PKGBUILD notes

## Design

- Official Arch repo packages are used wherever they exist (`depends=`): python, vapoursynth, vapoursynth-plugin-bestsource, ffmpeg, pyside6, qt6-svg, python-cattrs/numpy/platformdirs/psutil/rich/tomli-w/typing_extensions.
- Everything else is bundled from pinned upstream sources (sha256-checked) into the private tree `/usr/lib/qtgmc-6ui`:
  - `plugins/`: native VapourSynth plugins (akarin, resize2, mvutensils, znedi3, zsmooth, dfttest2), built in `build()` by `packaging/plugins/build.sh` (meson/make/zig/cmake, offline).
  - `venv/`: Python venv on the versioned system interpreter (`/usr/bin/python3.X`) with system site-packages, holding vsjetpack, jetpytools, the dfttest2 module and the app itself.
  - `build-info.json`: Python/VapourSynth versions and the plugin table, written at build time and compared with the running system by `qtgmc-6ui doctor`.
- No AUR dependencies and no VCS sources.
- `/usr/bin/qtgmc-6ui` is a small `/bin/sh` wrapper that runs the venv python.

## Generated parts

`packaging/sync-pkgbuild.py` owns everything between `# BEGIN generated:` and `# END generated:`, plus `pkgver` and `.SRCINFO`. Its sources of truth:

| block | source |
|---|---|
| `pkgver` | `src/qtgmc_6ui/__init__.py` |
| `plugin-deps`, `plugin-sources` | `packaging/plugins/plugins.json` (via `plugins/manifest.py`) |
| `python` | `uv.lock` (pure-python wheels and their sha256) |
| `licenses` | `pyproject.toml` (the app licence comes first), `plugins.json`, and the verified tables in `sync-pkgbuild.py` |

```sh
python3 -I packaging/sync-pkgbuild.py           # rewrite PKGBUILD + .SRCINFO
python3 -I packaging/sync-pkgbuild.py --check   # exit 1 when anything is stale
```

## Building and testing locally

- `packaging/local-build.sh [-s]`: tars the working tree (`git ls-files`, files as on disk) into `~/.cache/qtgmc-6ui-build`, copies the PKGBUILD there and runs `makepkg` with `QTGMC6UI_LOCAL_SRC=<tarball>`. No push or tag is needed.
  - It refuses a build dir that is a git checkout (your AUR clone) unless you pass `--force`.
  - `--committed` (also accepted by `test-package.sh`) builds `git archive HEAD` instead of the working tree.
- `packaging/test-package.sh REFERENCE.avi`: the full check on an Arch machine.
  1. Build in a clean chroot (`extra-x86_64-build -c`, or `makepkg -s` without devtools).
  2. namcap, then `pacman -U`.
  3. `qtgmc-6ui doctor`, analyze, and a reference encode verified with ffprobe.
  - `--steps chroot-test` repeats the tests in a throw-away copy of the clean chroot.
- `QTGMC6UI_MARCH=x86-64-v3|native makepkg` opts the plugins into a newer CPU baseline. The default is Arch's generic x86-64.
  - This works with plain makepkg only: the devtools chroot builds do not pass the environment.
  - The package version stays the same, so never copy a `native` build to another machine.
- Build parallelism follows `-jN` in `MAKEFLAGS`. `QTGMC6UI_JOBS` overrides it.

## Release steps

1. Bump `__version__`, run `sync-pkgbuild.py`, then commit, tag `v$pkgver` and push.
2. Run `python3 -I packaging/sync-pkgbuild.py --project-sha256 auto`. It downloads the GitHub archive and records its sha256 in `_project_sha256`.
   - Do not use `updpkgsums`: it merges every sum into one array.
   - If it was run anyway, `sync-pkgbuild.py` folds the result back. `--check` flags the merged state.
3. Run `makepkg --verifysource` (or `packaging/test-package.sh`).
4. Copy `PKGBUILD` and `.SRCINFO` into the AUR clone, commit and push.

`.github/workflows/aur.yml` automates steps 2 to 4 for a pushed tag (see the next section).

## Publishing from GitHub Actions

A pushed tag `v$pkgver` (it must equal `__version__`) runs `.github/workflows/aur.yml`. The `test-build` job runs in an `archlinux:base-devel` container: it pins the release checksum with `sync-pkgbuild.py --project-sha256 auto`, builds with `makepkg -s`, runs namcap (errors fail, warnings are the known ones below), installs the package and runs `packaging/ci-smoke.sh` (`doctor`, a master plus a viewing copy, and a `--no-master` run). Only if that passes does the `publish` job push `PKGBUILD` and `.SRCINFO` to the AUR. Nothing is committed back to GitHub, so the tracked `PKGBUILD` keeps `_project_sha256='SKIP'`.

One-time setup:

1. Create the AUR account `jogai` and add an SSH public key to it. Use a key that is only for this: `ssh-keygen -t ed25519 -f aur_ci -C qtgmc-6ui-ci`.
2. In the GitHub repo add the private key as the secret `AUR_SSH_PRIVATE_KEY`. Optionally create the environment `aur` (Settings, Environments) and require a reviewer, so every publish needs an approval.
3. Create the package by hand once: clone `ssh://aur@aur.archlinux.org/qtgmc-6ui.git`, add `PKGBUILD` and `.SRCINFO` and push to `master`.

A manual run (Actions, AUR release, Run workflow) with an existing tag is a dry run: it runs the gate and never publishes. Publishing only happens for a pushed tag. The AUR host key is pinned in the workflow; if the AUR ever rotates it, the publish step fails until the key line is updated (the fingerprints are on https://aur.archlinux.org).

The archive URL of a tag is generated by GitHub. If GitHub ever changes the compression, the sha256 of an old tag changes with it; the gate would then fail at the checksum step, not publish a broken package.

## Weekly canary build

`.github/workflows/arch-canary.yml` builds the current master against current Arch every Wednesday at 00:00 UTC (and on demand from the Actions tab), runs namcap and `packaging/ci-smoke.sh`, and opens an issue titled "Weekly Arch canary build failed" when something breaks. Further failures add a comment to that issue, and a later green run closes it. It exists for breaks that appear without any change in this repository, like the Zig 0.17 one. Scheduled workflows only run on the default branch, can start late, and GitHub disables them after 60 days without repository activity (a push or a manual run re-enables them).

## When Arch updates python or vapoursynth

- The venv is tied to `/usr/bin/python3.X`, and the plugins are built against the repo VapourSynth.
- After a Python minor bump, `/usr/bin/qtgmc-6ui` prints "Arch updated python …; reinstall/rebuild qtgmc-6ui" (with a desktop notification when started from the menu) and exits with 4.
- After a VapourSynth bump, `doctor` and every encode compare `build-info.json` with the running core: a different API major or a lower API minor blocks ("Arch updated vapoursynth …; reinstall/rebuild qtgmc-6ui", exit 4); a new release with the same API (R80 to R81, both API 4.3) only warns (`vapoursynth_abi` WARN in doctor, one `vapoursynth_updated` warning per CLI run) because the core loads every plugin of its API major with a minor not above its own, so no rebuild is needed. The weekly canary build and the pre-release test verify new VapourSynth releases.
- Fix: bump `pkgrel` and rebuild (users: their AUR helper's rebuild option).

## Known risks

- akarin needs llvm 20–22 (`extra/llvm22` for the build, `llvm22-libs` at runtime); akarin v1.5.0 and its main (2026-10-22) both say `llvm >= 20, < 23` in `meson.build`, and no release or issue adds LLVM 23. Arch's `llvm` is 23.1.x since 2026-09-15, but versioned packages are kept: llvm18 to llvm22 are all in `extra` (llvm22 22.1.8-2 rebuilt 2026-09-17, llvm19 as late as 2026-09-19), and `llvm22-libs` is required by official packages (zig, ldc, python-llvmlite, sparse), so it should stay while they use it. Arch has no published retention policy that we found; this rests on those observations.
  - If llvm22 disappears the package can neither be built nor installed (`makedepends` and `depends` fail). Watch for a release of Jaded-Encoding-Thaumaturgy/akarin-vapoursynth-plugin newer than v1.5.0 whose `meson.build` allows `< 24`, or a commit on main that does (as of 2026-10-22 main only has dependency-bot commits); then bump it in `plugins.json` together with `llvm22`/`llvm22-libs` to the then-current `llvmNN`/`llvm`/`llvm-libs`.
  - Fallbacks, in this order: (1) pin the `meson.build` range with a `patch` entry and test against llvm 23 (the Reactor JIT code is the part that may break; verify the lossless fidelity tests); (2) `-Dasmjit=true` drops LLVM but needs `-mavx` and has no expr2 path, so it is not acceptable for the generic baseline; (3) build llvm22 ourselves (heavy, not an option for the AUR).
- jetpytools stays at 3.1.2 although 3.2.0 (2026-10-10) exists. vsjetpack 2.2.6 accepts it (`jetpytools>=3.1.0,<4`) and a lossless bob with 3.2.0 was bit-exact, but the whole 3.1.2 to 3.2.0 change is one new `inject_self.cached.clear_cache()` static method that nothing here uses, so there is nothing to gain. Bump it with the next vsjetpack release.
- mvutensils is pinned to v9 together with vsjetpack 2.2.6 (v10 changed the API). Bump them together.
- Zig churn: every Zig minor release breaks the language, std and CLI (0.17, in Arch since 2026-10-20, removed `@cImport` and the `--global-cache-dir` option of `zig fetch`; zsmooth 0.20.0 and its Zig dependencies no longer compile). zsmooth is therefore built with the pinned Zig 0.16.0 release tarball from ziglang.org (`plugins.json` `toolchain` source), never with Arch's `zig`, and cache paths go through `ZIG_GLOBAL_CACHE_DIR`/`ZIG_LOCAL_CACHE_DIR`. Detection: the weekly/gate clean-root build fails in the zsmooth step. To move on, bump zsmooth to a release that names a newer `minimum_zig_version`, update its zig dependency tarballs and `zig_hash`es, and the toolchain URL/sha256 (from ziglang.org/download/index.json) together.

## namcap

- **PKGBUILD:** one known warning, "Reference to x86_64 should be changed to $CARCH" (twice). It comes from the file name of the pinned Zig tarball (`zig-x86_64-linux-0.16.0.tar.xz`), which is ziglang.org's name for it. The package is x86_64 only, so it stays.
- **"Dependency included, but may not be needed":** namcap cannot see the venv's python symlink, Python imports from the private venv (vapoursynth, PySide6, numpy, cattrs, attrs, platformdirs, psutil, rich, tomli_w, typing_extensions), Qt's SVG image plugin (qt6-svg), ffmpeg run as a subprocess, or bestsource loaded with `LoadPlugin`. Run namcap with `-e pydepends`: the venv modules are bundled.
- **`libzsmooth.so` "lacks GNU_PROPERTY_X86_FEATURE_1_SHSTK" and "unused libpthread.so.0":** zig emits no CET notes. These are properties of the toolchain and cannot be fixed in the PKGBUILD.
- **Options:** `!lto` because the plugin build script is tested without LTO (per-ISA SIMD kernels), and `!debug`.

## Licences

- `license=()` is generated. The first entry is the app (from `pyproject.toml`); the rest come from the bundled components.
- Component licence texts are installed under `/usr/share/licenses/qtgmc-6ui/<component>/`:
  - main sources: `plugins.json` `license_files`;
  - sub-sources: `_extra_licenses`;
  - Python packages: links to their dist-info.
- Common SPDX texts come from Arch's `licenses` package.
