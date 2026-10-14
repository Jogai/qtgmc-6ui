# qtgmc-6UI technical notes

Background for people who want to know what is installed and why. Usage is in [README.md](README.md), development in [DEV.md](DEV.md).

## Dependencies: repo versus bundled

Rule: use the official Arch repositories (`core`, `extra`) when a package exists there; bundle the rest from pinned upstream sources. Never depend on AUR packages. Checked 2026-10-09 with `pacman -Si`/`-Ss`; re-check at every version bump and switch to the repo if something moved in. Sources are in `packaging/aur/PKGBUILD` (generated blocks from `packaging/plugins/plugins.json` and `uv.lock` via `packaging/sync-pkgbuild.py`; all `sha256sums` pinned except the project tarball until the first tag).

Runtime `depends` (all from official repos; versions are those seen on 2026-10-09):

| dependency | why |
|---|---|
| python 3.14.7, vapoursynth 80 (R80, API 4.3) | runtime, including the `vapoursynth` Python module. PyPI wheels are not used. |
| vapoursynth-plugin-bestsource R22 | source filter, loaded explicitly |
| ffmpeg 9.0.2 | probe, idet/fieldmatch analysis, encoding (libx264, libx265, ffv1, flac, ...) |
| pyside6 6.12.0, qt6-svg | GUI; qt6-svg for the SVG icon |
| python-numpy, python-rich, python-typing_extensions | imported by the app and vsjetpack |
| python-psutil 7.2.2 | process liveness (`osdep.process_alive`) for the stale temporary-master cleanup; also imported by vsjetpack |
| python-platformdirs 4.13.0 | user config and cache base directories (`osdep.config_base`/`cache_base`) |
| python-cattrs 26.2.1 (pulls python-attrs 26.1.0) | (de)serialisation of every JSON/TOML contract in `models.py` and the strict profile key/type check |
| python-tomli-w 1.2.0 | writes profile TOML files |
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

Bundled Python packages (pure-Python wheels from PyPI, hashes from `uv.lock`, installed offline into `/usr/lib/qtgmc-6ui/venv`): `vsjetpack` 2.2.6 and `jetpytools` 3.1.2 (both MIT), plus the dfttest2 module above. Neither is in the official repos. The venv is created with the versioned system interpreter and system site-packages, so `vapoursynth`, `PySide6`, `numpy`, `psutil`, `platformdirs`, `cattrs`, `attrs`, `tomli_w`, `rich` and `typing_extensions` come from pacman. `[tool.uv] exclude-dependencies` in `pyproject.toml` keeps uv from resolving them from PyPI (and from shadowing the system VapourSynth with PyPI wheels).

Why bundle Python packages: Arch's guidelines do not forbid a private venv, and nothing in the repos provides vsjetpack/jetpytools. The AUR route would break the no-AUR rule and brings the rebuild-on-every-Python-bump problem described above. The guidelines discourage prebuilt `.whl` as sources; here they are pure-Python, hash-pinned and fetched through `source=()` so the build is offline and reproducible, and the app's own wheel is built from source (`python -m build`).

Optional (`optdepends`, official repos): `kvazaar`, `sox`. `x264` and `x265` are not listed because Arch's `ffmpeg` depends on both packages.

## Upstream projects and what they are used for

This section says what each upstream project does inside qtgmc-6UI, at which processing stage, and in which scenario. Versions, pins and the repo-versus-bundled decision are in [Dependencies: repo versus bundled](#dependencies-repo-versus-bundled); licence texts are listed under [Licensing](README.md#licensing).

The plugin statements below are measured, not inferred: the project's own graph builder was run on 8-frame trims (the PAL DV reference clip, plus synthetic 4:1:1 and 4:2:2 10-bit clips) for every configuration listed, once with all plugins and once with each bundled plugin removed (2026-10-10, Arch, vsjetpack 2.2.6). Every failure happened while the graph was being built, before any frame was rendered.

### How the pieces fit

(a) End to end, from input file to outputs:

```text
input file (DV, DVD MPEG-2, HDV, ...)       opened read-only, never modified
  |
  v
analyze (analysis.py, no VapourSynth involved)
  ffprobe -show_format -show_streams ........ container, codec, size, SAR, colour tags, audio
  ffmpeg -vf idet -f null (sampled windows) . TFF / BFF / progressive / repeated-field counts
  ffmpeg -vf fieldmatch,idet ................ only windows with a repeated-field signature
  classify.py ............................... interlaced TFF|BFF, telecined, mixed, progressive,
                                              undetermined
  |
  v
job (jobs.py): telecined / mixed / progressive -> blocked (exit 3) unless --force
               field order undetermined        -> blocked (exit 3) unless --field-order
  |
  v
master (pipeline.py): VapourSynth + QTGMC in-process -> y4m pipe -> ffmpeg, see (b)
  -> .<name>.mkv.partial -> ffprobe -count_packets check -> renamed to <name>.mkv
  |
  v
viewing copies (viewcopy.py): always from the master, never from the original, see (d)
  -> .<name>.<profile>.<ext>.partial -> ffprobe check -> renamed
```

(b) The master pipeline. Audio never passes through VapourSynth; ffmpeg takes every audio track from the original file.

```text
original ---+--> bs.VideoSource (bestsource) -> std.SetFieldBased(tff|bff from the analysis)
            |      |
            |      |   4:1:1 only: core resize.Point to 4:2:2 (mvutensils rejects 4:1:1)
            |      v
            |    QTempGaussMC.bob(clip, tff=...)       vsjetpack + bundled plugins, see (c)
            |      |   --rate single: [::2]   4:1:1 only: resize.Point back to 4:1:1
            |      v
            |    clip.output(y4m)  in the CLI process (no vspipe)
            |      |
            |      |  stdin pipe: y4m carries frames, but no SAR and no colour tags
            |      v
            +--> ffmpeg -i pipe:0 -i ORIGINAL -map 0:v:0 -map 1:a?
                   -vf setsar=SAR,setparams=colorspace:color_primaries:color_trc:range
                   -color_range tv|pc
                   -c:v ffv1 -level 3 -g 1 -slices 16 -slicecrc 1
                   -c:a flac -compression_level 8 -f matroska .<name>.mkv.partial
```

(c) Inside QTempGaussMC (`vsdeinterlace/qtgmc.py`, stages in execution order) with the plugin that does the work. "std" and "resize" are built into VapourSynth.

```text
stage       operation                        plugin(s)                 skipped / changed by
----------  -------------------------------  ------------------------  -----------------------------
prefilter   Catrom bob -> draft clip         resize2                   always runs
            scene-change props, temporal     akarin, std               TR0 = 0 (draft): skipped
            binomial smooth, shimmer mask                              Rep0 = 0 (draft, faster): off
analyze     motion vectors: Super, Analyse,  mvutensils                block size 32 for draft,
            Recalculate on the draft clip                              faster and width > 1100
denoise     DFTTest, motion-compensated      dfttest2, mvutensils      only with --denoise
basic       bob the source fields            znedi3 (NNEDI3)           draft: Catrom via resize2
            binomial degrain over TR1        mvutensils                TR1 = 2 from slower up
            source match (twice refined):    znedi3, mvutensils,       off with --no-source-match
            re-bob the error, degrain        akarin
            sharpen + temporal limit         akarin, mvutensils        only with --no-source-match
                                                                       (draft: never)
final       degrain over TR2                 mvutensils                TR2 >= 1 with source match
            shimmer mask                     akarin                    Rep2 = 0 (draft): off
            lossless (POSTSMOOTH): weave     std, akarin, zsmooth      off with --no-lossless
            the original fields back, then   (Median, Repair,
            vertical anti-comb on the        RemoveGrain)
            synthesised lines only
output      one frame per field (bob); qtgmc.py takes every other frame for --rate single
```

(d) The three viewing-copy backends, plus the optional sox audio step:

```text
master <name>.mkv (FFV1 + FLAC)
  |
  +-- ffmpeg backend (one process)
  |     ffmpeg -i master -vf [framestep=2,]setpts,[scale,]setsar,setparams
  |            -c:v libx264|libx265|... -c:a aac|libopus|flac|...  -f mp4|matroska|...
  |
  +-- x264 CLI (x264 writes mp4 itself: exact constant-rate timestamps)
  |     ffmpeg -i master -an -f yuv4mpegpipe - | x264 --demuxer y4m --muxer mp4 -> tmp.mp4
  |     ffmpeg -i tmp.mp4 -i master -map 0:v:0 -map 1:a? -c:v copy -c:a ENC     -> copy
  |
  +-- x265 CLI / kvazaar (both write a raw HEVC elementary stream)
  |     ffmpeg -i master -an -f yuv4mpegpipe - | x265|kvazaar                   -> tmp.hevc
  |     esmux.py: ffprobe -show_packets and -show_frames give decode and display
  |               order; the app writes tmp.ts (MPEG-TS with PTS/DTS per access unit)
  |     ffmpeg -f mpegts -i tmp.ts -i master -map 0:v:0 -map 1:a? -c:v copy -c:a ENC -> copy
  |
  +-- sox audio step (any backend, when the profile enables [audio.sox]), per audio track:
        ffmpeg -i master -map 0:a:N -f sox - | sox -t sox - -b BITS -t w64 tmp.aN.w64 EFFECTS
        the final mux then takes tmp.aN.w64 instead of the master's audio
```

(e) Processes: the GUI never processes video itself.

```text
qtgmc-6ui GUI (PySide6 / Qt 6)
  |  QProcess: <venv python> -m qtgmc_6ui <command>
  +-- analyze FILE --events ............. JSON lines on stdout -> Analysis tab
  +-- run-job job-<id>.json --events .... JSON lines: stage, progress, log, result, done
  |     cancel: GUI writes "cancel" to stdin; the CLI stops, removes partial output, exits 130
  |             (GUI escalates to terminate after 20 s, kill after 45 s)
  +-- preview-serve FILE ................ long-lived: one VapourSynth core, source index and
  |     last QTGMC window kept open; one JSON request per stdin line -> PNG + JSON reply;
  |     "quit" ends it (preview-frame ... --json, one process per frame, is the fallback)
  +-- doctor / profiles / encoders --json  one result document each

CLI process (deint, run-job)
  main thread:  cancel token (SIGINT, SIGTERM, stdin "cancel")
  worker:       VapourSynth renders frames into ffmpeg's stdin
  children:     ffmpeg, ffprobe, x264, x265, kvazaar, sox in their own process groups;
                stopped gracefully on cancel, forced after 5 s
```

### Bundled plugins: what needs them (measured)

| plugin | needed by | not needed by |
|---|---|---|
| akarin | every QTGMC graph (first use: scene-change detection in the prefilter; Expr everywhere) | analyze; preview source and fields |
| resize2 | every QTGMC graph: the prefilter always builds a Catrom-bobbed draft clip | analyze; preview source and fields |
| mvutensils | every QTGMC graph (motion analysis, degrain, compensation) | analyze; preview source and fields |
| znedi3 + `nnedi3_weights.bin` | every preset except `draft` (the basic bob and the source-match re-bobs) | `--preset draft` (Catrom bobber) |
| zsmooth | lossless on (the default): anti-comb filtering in the lossless stage | `--no-lossless`, with or without source match |
| dfttest2 (plugin + Python module) | `--denoise`; without either part the job stops with `denoise_unavailable` | everything else (outputs were bit-identical without it) |

Single rate, the `faster`, `slower` and `placebo` presets, 4:1:1 and 4:2:2 10-bit sources need no plugin beyond the default configuration. Even so, the app refuses to start a job if any of akarin, resize2, mvutensils, znedi3 or zsmooth is missing, because `build-info.json` marks them required. `qtgmc-6ui analyze` uses only ffprobe and ffmpeg. The preview's source and fields modes use only bestsource and VapourSynth's built-in `std`/`resize`; output mode builds the same graph as the master (with source match on and no denoise).

### Runtime, official repositories

| project | upstream, licence | used for | scenario |
|---|---|---|---|
| Python | python.org, PSF-2.0 | interpreter of the app and of the private venv in `/usr/lib/qtgmc-6ui/venv` | always; a minor-version bump needs a rebuild |
| VapourSynth | vapoursynth.com, LGPL-2.1-or-later | frame server, used in-process through its Python module: plugin loading with autoloading off, `std.SetFieldBased`/`SeparateFields`, `resize.Point` for the 4:1:1 detour and RGB conversion for previews, y4m output into ffmpeg | master, preview, doctor; not analyze |
| bestsource (`vapoursynth-plugin-bestsource`) | github.com/vapoursynth/bestsource, MIT | the only source filter: frame-accurate decoding with an index cached in `~/.cache/qtgmc-6ui/bsindex` | every master and every preview; without it nothing can be opened. ffms2 and other source filters are not used |
| FFmpeg (`ffmpeg`, incl. ffprobe) | ffmpeg.org, GPL-3.0-only (Arch build) | ffprobe: probe, verification of master and copies, packet/frame order for esmux. ffmpeg: `idet` and `fieldmatch` analysis; master `setsar`, `setparams`, `ffv1`, `flac`, matroska; viewing copies `framestep`, `setpts`, `scale`, `setsar`, `setparams`, `libx264`, `libx265`, `aac`, `libopus`, `flac`, y4m output for the CLI encoders, `-f sox` output for the sox step, MPEG-TS input for the HEVC remux; short test muxes (`lavfi` `testsrc2`/`sine`) that decide which container/codec pairs are offered | analysis of every file; every master; every viewing copy; encoder lists and the profile editor |
| PySide6, Qt 6 (`pyside6`, `qt6-svg`) | qt.io, LGPL-3.0-only / GPL-3.0-only | GUI widgets and QProcess; `qt6-svg` loads the SVG icon | GUI only; the CLI never imports it |
| numpy, rich, typing_extensions | numpy.org (custom), MIT, Python-2.0.1 | imported by vsjetpack and jetpytools; numpy also speeds up PNG writing in the preview | whenever vsjetpack is imported (master, preview output, doctor) |
| psutil (`python-psutil`) | github.com/giampaolo/psutil, BSD-3-Clause | `osdep.process_alive`: whether the process that owns a `.<name>.nomaster.<pid>.tmp.mkv` temporary master still runs (a zombie counts as dead), so leftovers of killed `--no-master` runs are deleted and live runs are left alone; vsjetpack imports it too | every `deint`/`run-job` (stale temp-master scan of the output folder) |
| cattrs (`python-cattrs`, with `python-attrs`) | github.com/python-attrs/cattrs, MIT (attrs: MIT) | `models.to_dict`/`from_dict` for analysis results and their cache, JobSpec/JobResult, DoctorReport, build-info and profiles (Fraction and enum hooks; the JSON stays byte-identical); a second, strict converter rejects unknown keys, wrong types and invalid enum values in profile TOML with path-qualified problems | every command that reads or writes a profile, a job, an analysis (or its cache), the doctor report or build-info |
| tomli-w (`python-tomli-w`) | github.com/hukkin/tomli-w, MIT | writes profile TOML (`profiles.to_toml`); reading uses the stdlib `tomllib` | `profiles save`/`export`, the GUI profile editor, `profiles show` |
| platformdirs (`python-platformdirs`) | github.com/tox-dev/platformdirs, MIT | `osdep.config_base`/`cache_base`: the user config and cache directories (`$XDG_CONFIG_HOME` or `~/.config`, `$XDG_CACHE_HOME` or `~/.cache`; relative or blank XDG values are ignored per the XDG spec), unless `QTGMC6UI_CONFIG_DIR`/`QTGMC6UI_CACHE_DIR` override them | every command that reads profiles, the GUI state or a cache (analysis, bestsource index, mux matrix, previews) |
| zimg, fftw (`zimg`, `fftw`) | sekrit-twc/zimg (WTFPL), fftw.org (GPL-2.0-or-later) | shared libraries of the bundled mvutensils (and zimg of VapourSynth's built-in resize) | with mvutensils, i.e. every QTGMC run |
| LLVM 22 (`llvm22-libs`) | llvm.org, Apache-2.0 WITH LLVM-exception | JIT compiler behind akarin's Expr | with akarin, i.e. every QTGMC run |

### Bundled native plugins and their sub-sources

| component | upstream, licence | what it is and what it does here |
|---|---|---|
| akarin | Jaded-Encoding-Thaumaturgy/akarin-vapoursynth-plugin, LGPL-3.0-only | JIT expression evaluator (`akarin.Expr`/`PropExpr`). vsjetpack's `norm_expr` and its scene-change detection run through it. Its source tree embeds SwiftShader Reactor (Apache-2.0, the code-generation layer over LLVM) and {fmt} (MIT, used by akarin's text filters) |
| resize2 | Jaded-Encoding-Thaumaturgy/vapoursynth-resize2, LGPL-2.1-only | resizer with field-aware shifts, used by vskernels for the Catrom bob (the prefilter's draft clip, and the main bobber of `draft`) |
| resize2 sub-sources: zimg @1ad1895 (patched), graphengine @cb5b2ce | sekrit-twc, WTFPL | the scaling library and its filter-graph engine, compiled statically into libresize2.so; build-time sources only |
| mvutensils | myrsloik/mvutensils v9, GPL-2.0-or-later | motion estimation and compensation (`mvu.Super`, `Analyse`, `Recalculate`, `Degrain`, `Compensate`), the core of QTGMC's temporal processing |
| znedi3 | sekrit-twc/znedi3, GPL-2.0-only | NNEDI3 neural-network interpolation of the missing field lines; reads `nnedi3_weights.bin` from its own directory |
| znedi3 sub-sources: graphengine @7bb77a5, vsxx @11eb026 | sekrit-twc, WTFPL / LGPL-2.1 | git submodules of znedi3 (graph engine, C++ wrapper of the VapourSynth API), compiled in; build-time sources only |
| zsmooth | adworacz/zsmooth, MIT AND GPL-2.0-or-later | `Median`, `Repair`, `RemoveGrain` for the lossless anti-comb step |
| zsmooth sub-sources: vapoursynth-zig, fftw zig wrapper, FFTW 3.3.11 | dnjulek (LGPL-2.1-only), adworacz (MIT), fftw.org (GPL-2.0-or-later) | Zig bindings to the VapourSynth API and the Zig build of FFTW, linked statically. FFTW only backs zsmooth's `DCTFilter`, which this app does not call |
| dfttest2 | Ichunjo/vs-dfttest2 (PyPI sdist), GPL-2.0-or-later; Python module GPL-3.0-or-later | DFT-based denoiser (`dfttest2_cpu`) plus the Python module that vsdenoise's `DFTTest` imports; vectorclass (Apache-2.0, SIMD C++ headers in the sdist) is compile-time only |
| vivtc, vszip | vapoursynth/vivtc, dnjulek/vapoursynth-zip | recipes in `plugins.json` only. The PKGBUILD builds the `all` set, which excludes them, so they are neither built nor shipped. Telecine detection uses FFmpeg; QTGMC with these settings never calls vszip |

### Bundled Python packages

| package | upstream, licence | used for |
|---|---|---|
| vsjetpack 2.2.6 | Jaded-Encoding-Thaumaturgy/vs-jetpack, MIT | `vsdeinterlace.QTempGaussMC` (the QTGMC implementation), with `vsaa` (NNEDI3 wrapper), `vskernels` (Catrom), `vsdenoise` (MVTools wrapper, DFTTest), `vsrgtools`, `vsmasktools` and `vsexprtools` underneath. Every master, preview output and doctor smoke test |
| jetpytools 3.1.2 | Jaded-Encoding-Thaumaturgy/jetpytools, MIT | helper library that vsjetpack is built on |
| dfttest2 11.0.0 | see dfttest2 above | `--denoise` only |

### Standalone encoders and sox

`kvazaar` and `sox` are `optdepends`. `x264` and `x265` come with `ffmpeg` and are detected at runtime like the others.

| tool | upstream, licence | used by | without it |
|---|---|---|---|
| x264 | videolan.org, GPL-2.0-only | profile `mp4-x264cli-aac` (and user profiles with the x264 backend) | profile hidden if the CLI was removed. Arch's ffmpeg depends on the `x264` package (libx264), so the CLI is normally installed |
| x265 | x265.org, GPL-2.0-or-later | profile `mkv-x265cli10-opus` | as x264: normally installed as an ffmpeg dependency |
| kvazaar | ultravideo/kvazaar, BSD-3-Clause | profile `mkv-kvazaar-opus`; Arch's ffmpeg has no libkvazaar | profile hidden |
| sox | `sox` package (the sox_ng fork, codeberg.org/sox_ng), GPL-2.0-only | profile `mp4-h264-aac-sox`: resampling to 48 kHz and shaped dither before the audio encoder | profile hidden |

### Build only (`makedepends`)

| tool | used for |
|---|---|
| uv | creates the venv on the system interpreter and installs the hash-pinned wheels offline (`uv export` from `uv.lock`, then `uv pip install --require-hashes`); also the development setup |
| python-build, python-hatchling | build the app's own wheel from source |
| meson, ninja | akarin, resize2, mvutensils (ninja also for dfttest2) |
| make | znedi3 (upstream Makefile) |
| cmake | dfttest2 (the CMake project of the PyPI sdist) |
| zig | zsmooth, offline from a private Zig cache |
| patch | the zimg patch that resize2 requires |
| llvm22 | compiling akarin (its meson file accepts LLVM 20 to 22 only) |
| vapoursynth, python (runtime `depends`, also used by the build) | VapourSynth headers and include path for every plugin build |
