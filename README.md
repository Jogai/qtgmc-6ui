# qtgmc-6UI

[![AUR](https://img.shields.io/aur/version/qtgmc-6ui?style=for-the-badge&logo=archlinux&logoColor=white&label=AUR)](https://aur.archlinux.org/packages/qtgmc-6ui)
[![Latest tag](https://img.shields.io/github/v/tag/jogai/qtgmc-6ui?style=for-the-badge&logo=github&label=latest%20tag)](https://github.com/jogai/qtgmc-6ui/tags)
[![License](https://img.shields.io/badge/license-GPL--2.0--only-blue?style=for-the-badge&logo=gnu&logoColor=white)](#licensing)
[![Last commit](https://img.shields.io/github/last-commit/jogai/qtgmc-6ui?style=for-the-badge&logo=github)](https://github.com/jogai/qtgmc-6ui/commits)

QTGMC deinterlacing for archival video on Arch Linux, with a Qt6 GUI and a scriptable CLI. The display name is qtgmc-6UI. The Arch package, the command and the directories are `qtgmc-6ui` (pkgname must be lowercase). The "6" stands for Qt6.

## What it is, and why

qtgmc-6UI is an application for restoring interlaced archival video: VHS, Hi8 and Video8 captures, DV, DVD, HDV and AVCHD. It has a Qt6 GUI and a scriptable CLI on top of the same core, so you do not write VapourSynth scripts yourself. A file is analysed first (field order, and whether it is truly interlaced, telecined, mixed or progressive), then deinterlaced with QTGMC, by default to double rate. The result is an FFV1 master, with the original audio tracks copied unchanged, in which the original field lines are copied back unchanged and only the missing lines are synthesised. Viewing copies (H.264, HEVC and others, from editable profiles) are encoded from that master, and the original file is never modified.

Deinterlacing is done by `vsdeinterlace.QTempGaussMC` from vsjetpack 2.2.6, because havsfunc no longer contains QTGMC (r34 removed it, and the last working tag, r33, targets very old plugins). Telecined and mixed material is blocked unless you force it, because it needs IVTC with another tool, and the field order is never guessed.

The package has no AUR dependencies and no `-git` sources. Official Arch packages are used where they exist, and everything else is built from pinned upstream sources with sha256 checksums. It keeps working until a pin is deliberately bumped, and when an Arch update does require a rebuild it says so (see "doctor and the ABI story").

## Install

From the AUR ([qtgmc-6ui](https://aur.archlinux.org/packages/qtgmc-6ui)):

```
yay -S qtgmc-6ui        # or any AUR helper / manual makepkg
```

The first build compiles the native plugins and takes a while. They target Arch's generic x86-64 baseline (SIMD kernels dispatch at runtime). Opt-in: plain `makepkg` with `QTGMC6UI_MARCH=x86-64-v3` or `native` (never copy such a build to another machine; chroot builds ignore the variable).

To build from a checkout of this repository, and for the package tests, see [DEV.md](DEV.md).

## Requirements

- Arch Linux (or derivative), x86_64. The CPU path is first-class; no GPU is needed or used.
- Everything else is pulled in by pacman; see the [dependency table](TECHNICAL.md#dependencies-repo-versus-bundled). The venv is tied to the Python minor it was built with.

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
qtgmc-6ui deint clip.avi --audio flac            # master audio: copy (initial) | pcm | flac | alac | aac | opus
qtgmc-6ui audio-modes [--json]                   # the audio modes with their labels (what the GUI shows)
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

The master is fixed apart from the options above: Matroska, FFV1 level 3 (`-g 1 -slices 16 -slicecrc 1`). Audio is taken from the original file, never through VapourSynth, and all audio tracks are mapped. `--audio` chooses what happens to every track: `copy` (the initial value: the source stream unchanged, bit-exact; a codec Matroska cannot carry is stored as FLAC with a warning), `pcm` (uncompressed, at the source sample format and bit depth), `flac` (level 8), `alac` (16- or 24/32-bit integer; not for floating-point sources), `aac` (256 kbps stereo, 128 kbps per channel) or `opus` (192 kbps stereo, 96 kbps per channel, 48 kHz). The two lossy modes warn that the master's audio is no longer the original. After the encode the master is verified: for the lossless modes every track is decoded and compared with the decoded source (MD5), the lossy ones must keep their duration; the A/V start offset of the source is kept in every mode. The source pixel format and bit depth are kept (no chroma conversion). SAR and colour tags are re-applied with `setsar` and `setparams`, because y4m carries neither. Tags come from the source, else defaults by resolution and rate: PAL SD `bt470bg`, NTSC SD `smpte170m`, HD `bt709`, range tv. The input file is never modified. Output goes to a hidden `.<name>.partial` file that is renamed only when the encode and verification succeed.

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
| `mkv-hevc10-opus` | mkv | libx265 10-bit | libopus |
| `mp4-x264cli-aac` | mp4 | x264 CLI | aac |
| `mkv-x265cli10-opus` | mkv | x265 CLI 10-bit | libopus |
| `mkv-kvazaar-opus` | mkv | kvazaar CLI | libopus |

Profiles that need an optional tool are hidden when the tool is missing (`profiles list --all` shows them). User profiles are TOML files in `~/.config/qtgmc-6ui/profiles/` and can be shared and versioned. Which container/codec pairs are selectable is decided by a probe-mux on the installed ffmpeg (cached per ffmpeg version), not by a static table; the reason for a greyed-out option is shown. Options per profile: rate control, preset, tune, profile/level, pixel format, audio bitrate/rate, scaling (keep anamorphic SAR, or square pixels, or custom), frame rate (keep double or single), extra arguments. x265 and kvazaar output is raw HEVC; a small built-in step gives it real timestamps in a temporary MPEG-TS before the final mux.

The standalone `x264` and `x265` encoders are installed together with Arch's `ffmpeg`, which depends on them. Optional tools (pacman `optdepends`, detected at runtime): `kvazaar` (HEVC; ffmpeg on Arch has no libkvazaar) and `sox` (audio resampling/dithering steps, not a codec).

## doctor and the ABI story

Bundled plugins are built against the repo VapourSynth, and the bundled venv against the repo Python. When Arch moves either, the package needs a rebuild. At build time `/usr/lib/qtgmc-6ui/build-info.json` records the Python minor, VapourSynth core/API, pinned component versions and plugin table. `qtgmc-6ui doctor` and the startup check compare it with the running system and report plainly, for example "Arch updated python; reinstall/rebuild qtgmc-6ui". After a Python minor bump the venv interpreter is gone, so `/usr/bin/qtgmc-6ui` itself prints that message (and a desktop notification when started from the menu). Plugins are loaded explicitly from `/usr/lib/qtgmc-6ui/plugins` with VapourSynth autoloading disabled for the process, so plugins the user installed from the AUR cannot conflict.

`qtgmc-6ui doctor` checks the build-info, Python and VapourSynth ABI, python packages, core creation, every plugin load, a small QTGMC smoke run, ffmpeg/ffprobe and the optional tools, and the locale. `doctor --json` is the paste-into-a-bug-report format.

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

## More

- [TECHNICAL.md](TECHNICAL.md): the dependency decisions (repo versus bundled), what every upstream project is used for, and diagrams of the processing pipelines.
- [DEV.md](DEV.md): development setup, tests, local package builds and releasing.
