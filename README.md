# qtgmc-6UI

QTGMC deinterlacing for archival video on Arch Linux, with a Qt6 GUI and a scriptable CLI. The display name is qtgmc-6UI. The Arch package, the command and the directories are `qtgmc-6ui` (pkgname must be lowercase). The "6" stands for Qt6.

Status: 0.1.0, not yet on the AUR (https://github.com/jogai/qtgmc-6ui).

## What it is, and why

The goal is a QTGMC package that installs and keeps working. The existing AUR `qtgmc` meta-package does not:

- It depends on `vapoursynth-miscfilters-obsolete-git`, a name no AUR package provides. The plugin ships as `vapoursynth-plugin-misc-git`, so dependency resolution fails.
- Nearly all its dependencies are `-git` packages, so upstream changes break it without warning.
- `vapoursynth-plugin-havsfunc-git` pulls in `vapoursynth-plugin-vsjetpack-git`, reported upstream as unstable and a frequent cause of havsfunc breakage.
- Every Arch Python bump forces a rebuild of all the Python-side plugin packages.

qtgmc-6UI therefore has no AUR dependencies and no `-git` sources. It uses official Arch packages where they exist and builds everything else from pinned upstream sources with sha256 checksums. It keeps working until a pin is deliberately bumped, and when an Arch update does require a rebuild it says so (see "doctor and the ABI story").

The QTGMC implementation is `vsdeinterlace.QTempGaussMC` from vsjetpack 2.2.6. havsfunc no longer contains QTGMC: r34 removed it, and the last working tag, r33, targets very old plugins.

## Install

AUR (placeholder, not published yet):

```
yay -S qtgmc-6ui        # or any AUR helper / manual makepkg
```

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

The first build compiles the native plugins and takes a while. They target Arch's generic x86-64 baseline (SIMD kernels dispatch at runtime). Opt-in: plain `makepkg` with `QTGMC6UI_MARCH=x86-64-v3` or `native` (never copy such a build to another machine; chroot builds ignore the variable).

## Requirements

- Arch Linux (or derivative), x86_64. The CPU path is first-class; no GPU is needed or used.
- Everything else is pulled in by pacman; see the dependency table. The venv is tied to the Python minor it was built with.

## Usage

### GUI

`qtgmc-6ui` (no arguments), `qtgmc-6ui gui [FILE ...]`, or the "qtgmc-6UI" menu entry. The GUI contains no processing logic. It runs the CLI and reads its JSON-lines event stream.

- Queue: add files, reorder, remove.
- Analysis tab: ffprobe summary, idet counts, detected type (TFF, BFF, telecined, mixed, progressive, undetermined). A manual override is visibly marked as an override. Telecined or mixed files get an explanation that they need IVTC with another tool.
- Settings tab: preset, bob versus single rate, source match and lossless, denoise (off, labelled as altering the image), output folder and naming.
- Viewing copies tab: pick profiles, open the profile editor.
- Preview tab: frame scrubbing with source, separated fields and QTGMC output. Frames are rendered to PNG by the backend, not by a video player, so no player-side deinterlacing hides combing.
- Run panel: progress (frames, fps, ETA), cancel (partial output is removed), full log with a copy button.

### CLI

```
qtgmc-6ui analyze clip.avi                       # probe + idet + telecine check + verdict
qtgmc-6ui analyze clip.avi --json                # machine-readable
qtgmc-6ui deint clip.avi --dry-run               # show plan and ffmpeg commands, write nothing
qtgmc-6ui deint clip.avi                         # master: <name>.mkv next to the input (see --out-dir, --name)
qtgmc-6ui deint clip.avi --view mp4-h264-aac --view mkv-hevc10-opus
qtgmc-6ui deint clip.avi --field-order bff --rate single --preset slow
qtgmc-6ui deint clip.avi --force --field-order tff   # override a telecine/progressive block
qtgmc-6ui view clip.deint.mkv --profile mp4-h264-aac # viewing copy from an existing master
qtgmc-6ui profiles list | show ID | validate FILE.toml | export ID | save FILE.toml | delete ID | path
qtgmc-6ui encoders [--probe] [--container mp4] [--kind video]
qtgmc-6ui preview-frame clip.avi 100 --mode fields -o fields.png
qtgmc-6ui script clip.avi                        # equivalent VapourSynth script, for bug reports
qtgmc-6ui deint clip.avi --save-job job.json     # write the JobSpec and exit
qtgmc-6ui run-job job.json                       # run a saved JobSpec (or - for stdin)
qtgmc-6ui doctor [--json]
```

Most commands accept `--json` (one result document) or `--events` (JSON lines). Exit codes: 0 ok, 1 error, 2 usage, 3 blocked (telecined, mixed, progressive, unknown field order), 4 component (plugin, ABI, tool), 5 input, 6 encode, 7 output, 130 cancelled. Cancel with SIGTERM/SIGINT.

The pipeline runs VapourSynth in-process and streams y4m into ffmpeg's stdin. There is no `vspipe` step (vspipe evaluates scripts in the system Python, not the bundled venv).

## Archival master and the fidelity guarantee

The master is fixed apart from the options above: Matroska, FFV1 level 3 (`-g 1 -slices 16 -slicecrc 1`), FLAC level 8. Audio is copied from the original file, never through VapourSynth, and all audio tracks are mapped. The source pixel format and bit depth are kept (no chroma conversion). SAR and colour tags are re-applied with `setsar` and `setparams`, because y4m carries neither. Tags come from the source, else defaults by resolution and rate: PAL SD `bt470bg`, NTSC SD `smpte170m`, HD `bt709`, range tv. The input file is never modified. Output goes to a hidden `.<name>.partial` file that is renamed only when the encode and verification succeed.

Defaults: detected field order, bob (25i gives 50p, 29.97i gives 59.94p), source match + lossless, no denoise. The preset follows the height: up to 576 lines `slower`, up to 720 `medium`, above that (1080i, 1440x1080) `fast`. `--preset` goes higher or lower, up to `placebo`.

Guarantee: with source match and lossless on (the default), every output field line that corresponds to an original field is bit-identical to the source, and only the missing lines are synthesised. It was verified with zero mismatching field planes for 4:2:0, 4:2:2, 4:4:4, gray and 4:1:1, 8 and 10 bit, both field orders, both rates. `tests/test_fidelity.py` checks it automatically. Caveats:

- Denoise (`--denoise SIGMA`, needs the bundled dfttest2) breaks it. Lossless then weaves back the denoised fields, so original pixels are no longer preserved. The tool warns about this, as it does for `--no-lossless`.
- Single rate is bob followed by dropping every other frame, which keeps the first field of each frame. Presets only change the synthesised lines while lossless is on.
- 4:1:1 (NTSC DV) is widened to 4:2:2 for QTGMC and narrowed back; verified bit-exact, and the master stays 4:1:1.

Field order is never guessed. It comes from idet content analysis. The container `field_order` is reported but not used (DV reports unknown/bb regardless). If the content is ambiguous, the tool stops and asks for `--field-order tff|bff`.

Telecine and IVTC: inverse telecine is out of scope. Analysis samples several windows across the file and combines two signals: the idet Repeated Fields ratio (about 0.40 for pure 3:2 pulldown, 0.00 otherwise) and a `fieldmatch` confirmation pass. Telecined and mixed files are blocked by default with an explanation that IVTC with another tool is needed. Progressive files get a "do not deinterlace" recommendation. `--force` overrides either, with a warning.

## Supported sources

Truly interlaced PAL-family (25i) and NTSC-family (29.97i, also 30i) video from about 320x240 up to 1920x1080i, including 1440x1080 anamorphic HDV: DV/DVCAM, DVD MPEG-2, VHS/Hi8/Video8 captures, HDV, AVCHD. The source SAR passes through unchanged. Not supported (clear error, no silent conversion): RGB, float, alpha, 4:1:0 and 4:4:0 sources; odd frame dimensions; heights that cannot be split into fields for the chroma format (the error suggests cropping, for example 486 to 480 lines). Variable frame rate gets a warning and an averaged constant rate. HD at slow presets is very slow and memory-hungry; the frame cache is limited (2 GB SD, 6 GB HD by default, `--cache-mb` to change).

## Viewing copies

Viewing copies are always encoded from the master, never from the original. A job can produce the master plus any number of copies. Built-in profiles (`qtgmc-6ui profiles list`):

| id | container | video | audio |
|---|---|---|---|
| `mp4-h264-aac` | mp4 | libx264 | aac |
| `mp4-h264-aac-sox` | mp4 | libx264 | aac (sox audio steps) |
| `mkv-h264-flac` | mkv | libx264 | flac |
| `mkv-hevc10-opus` | mkv | libx265 10-bit | opus |
| `mp4-x264cli-aac` | mp4 | x264 CLI | aac |
| `mkv-x265cli10-opus` | mkv | x265 CLI 10-bit | opus |
| `mkv-kvazaar-opus` | mkv | kvazaar CLI | opus |

Profiles that need an optional tool are hidden when the tool is missing (`profiles list --all` shows them). User profiles are TOML files in `~/.config/qtgmc-6ui/profiles/` and can be shared and versioned. Which container/codec pairs are selectable is decided by a probe-mux on the installed ffmpeg (cached per ffmpeg version), not by a static table; the reason for a greyed-out option is shown. Options per profile: rate control, preset, tune, profile/level, pixel format, audio bitrate/rate, scaling (keep anamorphic SAR, or square pixels, or custom), frame rate (keep double or single), extra arguments. x265 and kvazaar output is raw HEVC; a small built-in step gives it real timestamps in a temporary MPEG-TS before the final mux.

Optional tools (pacman `optdepends`, detected at runtime): `x264`, `x265`, `kvazaar` (HEVC; ffmpeg on Arch has no libkvazaar), `sox` (audio resampling/dithering steps, not a codec).

## doctor and the ABI story

Bundled plugins are built against the repo VapourSynth, and the bundled venv against the repo Python. When Arch moves either, the package needs a rebuild. At build time `/usr/lib/qtgmc-6ui/build-info.json` records the Python minor, VapourSynth core/API, pinned component versions and plugin table. `qtgmc-6ui doctor` and the startup check compare it with the running system and report plainly, for example "Arch updated python; reinstall/rebuild qtgmc-6ui". After a Python minor bump the venv interpreter is gone, so `/usr/bin/qtgmc-6ui` itself prints that message (and a desktop notification when started from the menu). Plugins are loaded explicitly from `/usr/lib/qtgmc-6ui/plugins` with VapourSynth autoloading disabled for the process, so plugins the user installed from the AUR cannot conflict.

`qtgmc-6ui doctor` checks the build-info, Python and VapourSynth ABI, python packages, core creation, every plugin load, a small QTGMC smoke run, ffmpeg/ffprobe and the optional tools, and the locale. `doctor --json` is the paste-into-a-bug-report format.

## Dependencies: repo versus bundled

Rule: use the official Arch repositories (`core`, `extra`) when a package exists there; bundle the rest from pinned upstream sources. Never depend on AUR packages. Checked 2026-10-09 with `pacman -Si`/`-Ss`; re-check at every version bump and switch to the repo if something moved in. Sources are in `packaging/aur/PKGBUILD` (generated blocks from `packaging/plugins/plugins.json` and `uv.lock` via `packaging/sync-pkgbuild.py`; all `sha256sums` pinned except the project tarball until the first tag).

Runtime `depends` (all from official repos; versions are those seen on 2026-10-09):

| dependency | why |
|---|---|
| python 3.14.7, vapoursynth 80 (R80, API 4.3) | runtime, including the `vapoursynth` Python module. PyPI wheels are not used. |
| vapoursynth-plugin-bestsource R22 | source filter, loaded explicitly |
| ffmpeg 9.0.2 | probe, idet/fieldmatch analysis, encoding (libx264, libx265, ffv1, flac, ...) |
| pyside6 6.12.0, qt6-svg | GUI; qt6-svg for the SVG icon |
| python-numpy, python-psutil, python-rich, python-typing_extensions | imported by the app and vsjetpack |
| fftw, zimg, llvm22-libs, libgcc, libstdc++, glibc | runtime libraries of the bundled plugins |
| hicolor-icon-theme, bash | icon directory; bash as listed in the PKGBUILD |

Build-only (`makedepends`, official repos): `uv`, `python-build`, `python-hatchling`, `meson`, `ninja`, `cmake`, `make`, `patch`, `zig`, `llvm22`.

Bundled native VapourSynth plugins (built in `build()`, installed to `/usr/lib/qtgmc-6ui/plugins`). In the official repos only mvtools, bestsource and deblock exist; vsjetpack 2.x needs none of mvtools (it uses mvutensils), so none of the others are usable here.

| plugin | version / source | licence | why bundled |
|---|---|---|---|
| akarin | 1.5.0, GitHub tag (Jaded-Encoding-Thaumaturgy) | LGPL-3.0-only (SwiftShader Reactor: Apache-2.0) | Expr/PropExpr for vsjetpack; not in repos. Built against `llvm22` (its meson refuses llvm 23) |
| resize2 | 0.5.1, GitHub tag + patched zimg and graphengine (pinned commits) | LGPL-2.1-only (zimg, graphengine: WTFPL) | vskernels bob resizer; needs a patched zimg linked statically, so the repo `zimg` cannot replace it |
| mvutensils | v9, GitHub tag (myrsloik) | GPL-2.0-or-later | motion analysis for QTempGaussMC. Pinned to v9: v10 breaks vsjetpack 2.2.6 |
| znedi3 | r3.3, GitHub tag (sekrit-twc) + graphengine, vsxx (pinned commits) + `nnedi3_weights.bin` | GPL-2.0-only (vsxx: LGPL-2.1) | default NNEDI3 bobber; not in repos |
| zsmooth | 0.20.0, GitHub tag (adworacz), zig deps pinned, FFTW 3.3.11 static | MIT AND GPL-2.0-or-later | repair/removegrain for the lossless stage |
| dfttest2 | plugin 11.1.0 (PyPI sdist), python module 11.0.0 (PyPI wheel) | GPL-2.0-or-later (module: GPL-3.0-or-later; vectorclass: Apache-2.0) | optional denoise only |

`vivtc` and `vszip` recipes exist in `plugins.json` but are not shipped (not needed at runtime).

Bundled Python packages (pure-Python wheels from PyPI, hashes from `uv.lock`, installed offline into `/usr/lib/qtgmc-6ui/venv`): `vsjetpack` 2.2.6 and `jetpytools` 3.1.2 (both MIT), plus the dfttest2 module above. Neither is in the official repos. The venv is created with the versioned system interpreter and system site-packages, so `vapoursynth`, `PySide6`, `numpy`, `psutil`, `rich` and `typing_extensions` come from pacman. `[tool.uv] exclude-dependencies` in `pyproject.toml` keeps uv from resolving them from PyPI (and from shadowing the system VapourSynth with PyPI wheels).

Why bundle Python packages: Arch's guidelines do not forbid a private venv, and nothing in the repos provides vsjetpack/jetpytools. The AUR route would break the no-AUR rule and brings the rebuild-on-every-Python-bump problem described above. The guidelines discourage prebuilt `.whl` as sources; here they are pure-Python, hash-pinned and fetched through `source=()` so the build is offline and reproducible, and the app's own wheel is built from source (`python -m build`).

Optional (`optdepends`, official repos): `x264`, `x265`, `kvazaar`, `sox`.

## Development

```
uv venv --system-site-packages --python /usr/bin/python3.14 .venv
uv sync --frozen
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

This needs the Arch `vapoursynth`, `bestsource`, `ffmpeg` and `pyside6` packages in the system Python. Native plugins can be built with `packaging/plugins/build.sh` into any directory and used with `QTGMC6UI_PLUGIN_DIR` (and `QTGMC6UI_BUILD_INFO`) for development. Pytest markers: `needs_sandbox` (ffmpeg, vapoursynth, bestsource), `needs_qtgmc` (full stack), `needs_tool(name)` (x264/x265/kvazaar/sox), `slow`, `gui`. Development and the full suite (516 tests) were run in an Arch environment; the test fixtures (synthetic clips, reference file) come from `tests/fixtures_gen.py`. After editing pins or `uv.lock` run `python3 -I packaging/sync-pkgbuild.py` (`--check` to verify).

## Licensing

The project is licensed under GPL-2.0-only.

Bundled components (full texts installed in `/usr/share/licenses/qtgmc-6ui/<component>/`): GPL-2.0-only znedi3; GPL-2.0-or-later mvutensils, dfttest2, FFTW (static in zsmooth); LGPL-2.1-only resize2, vsxx, vapoursynth-zig; LGPL-3.0-only akarin; MIT zsmooth, fftw zig wrapper, {fmt}, vsjetpack, jetpytools; Apache-2.0 SwiftShader Reactor, vectorclass; WTFPL zimg, graphengine; GPL-3.0-or-later dfttest2 Python module.

## Known limitations

- Depending on your opinion of AI generated works.
- akarin needs LLVM 20 to 22. If Arch drops `llvm22`, the package can be neither built nor installed until akarin is bumped. `llvm22-libs` is a large runtime dependency (about 150 MB).
- Python minor or VapourSynth release bumps need a rebuild (`pkgrel` bump). mvutensils is pinned at v9 until vsjetpack supports v10; both must be bumped together.
- x86_64 only.
- No IVTC; telecined and mixed material is blocked by default.
- The preset table is the project's own (vsjetpack has no presets), mapped from havsfunc's table. Output is not expected to be identical to classic QTGMC.
- The sampled analysis can miss telecine or interlacing confined to unsampled parts of a long file (`analyze --full` scans every frame). Smooth content classifies best; `testsrc2`-like progressive detail can be misread by idet.
- Variable frame rate sources get an averaged constant rate (warning).
- Viewing-copy validity comes from probe-muxes. Some selectable combinations are flagged as possibly unplayable in players (for example FFV1 or PCM in MP4), even though ffmpeg accepts them.
- 1080i at slow presets is slow and memory-hungry.
