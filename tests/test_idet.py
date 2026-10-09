"""idet parser on recorded ffmpeg n9.0.2 stderr (incl. the leading all-zero block),
window planning, command shape, and live runs in the sandbox."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import REFERENCE_FRAMES
from qtgmc_6ui import idet
from qtgmc_6ui.errors import ErrorCode, InputError
from qtgmc_6ui.models import IdetCounts, RepeatCounts

# ffmpeg -hide_banner -nostats -i test_input.avi -vf idet -an -f null -   (complete stderr)
REF_STDERR = """\
[aist#0:1/pcm_s16le @ 0x55c2d516a880] Guessed Channel Layout: stereo
Input #0, avi, from '/workspace/qtgmc/test_input.avi':
  Duration: 00:00:10.96, start: 0.000000, bitrate: 30440 kb/s
  Stream #0:0: Video: dvvideo (dvsd / 0x64737664), yuv420p, 720x576 [SAR 16:15 DAR 4:3], 28800 kb/s, 25 fps, 25 tbr, 25 tbn
  Stream #0:1: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 48000 Hz, stereo, s16, 1536 kb/s
[Parsed_idet_0 @ 0x55c2d5162440] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x55c2d5162440] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x55c2d5162440] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
Stream mapping:
  Stream #0:0 -> #0:0 (dvvideo (native) -> wrapped_avframe (native))
Press [q] to stop, [?] for help
Output #0, null, to 'pipe:':
  Metadata:
    encoder         : Lavf63.1.102
  Stream #0:0: Video: wrapped_avframe, yuv420p(bottom coded first (swapped)), 720x576 [SAR 16:15 DAR 4:3], q=2-31, 200 kb/s, 25 fps, 25 tbn
    Metadata:
      encoder         : Lavc63.1.102 wrapped_avframe
[Parsed_idet_0 @ 0x7fe284003380] Repeated Fields: Neither:   274 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x7fe284003380] Single frame detection: TFF:     0 BFF:   274 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x7fe284003380] Multi frame detection: TFF:     0 BFF:   274 Progressive:     0 Undetermined:     0
[out#0/null @ 0x55c2d516aa80] video:113KiB audio:0KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
frame=  274 fps=0.0 q=-0.0 Lsize=N/A time=00:00:10.96 bitrate=N/A speed= 277x elapsed=0:00:00.03
"""

# ... -ss 5 -i test_input.avi -vf idet -frames:v 50 ...  (idet counts 51 for 50 requested frames)
REF_WINDOW_STDERR = """\
[Parsed_idet_0 @ 0x560943611480] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x560943611480] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x560943611480] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
Stream mapping:
  Stream #0:0 -> #0:0 (dvvideo (native) -> wrapped_avframe (native))
[Parsed_idet_0 @ 0x7fb75c003380] Repeated Fields: Neither:    51 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x7fb75c003380] Single frame detection: TFF:     0 BFF:    51 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x7fb75c003380] Multi frame detection: TFF:     0 BFF:    51 Progressive:     0 Undetermined:     0
frame=   50 fps=0.0 q=-0.0 Lsize=N/A time=00:00:02.00 bitrate=N/A speed= 174x elapsed=0:00:00.01
"""

# 3:2 telecined 4 s clip (mandelbrot 24000/1001 -> telecine=pattern=23): the Repeated Fields signature.
TELE_STDERR = """\
[Parsed_idet_0 @ 0x5589116ef840] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x5589116ef840] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x5589116ef840] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
Stream mapping:
  Stream #0:0 -> #0:0 (ffv1 (native) -> wrapped_avframe (native))
[Parsed_idet_0 @ 0x7f93b4003380] Repeated Fields: Neither:    72 Top:    24 Bottom:    24
[Parsed_idet_0 @ 0x7f93b4003380] Single frame detection: TFF:    96 BFF:     0 Progressive:     0 Undetermined:    24
[Parsed_idet_0 @ 0x7f93b4003380] Multi frame detection: TFF:   119 BFF:     0 Progressive:     0 Undetermined:     1
frame=  120 fps=0.0 q=-0.0 Lsize=N/A time=00:00:04.00 bitrate=N/A speed=15.2x elapsed=0:00:00.26
"""

# Same clip through fieldmatch=order=auto:combmatch=full,idet: interlacing and repeats vanish.
TELE_FIELDMATCH_STDERR = """\
[Parsed_idet_1 @ 0x55fb514cc880] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_1 @ 0x55fb514cc880] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_1 @ 0x55fb514cc880] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
Stream mapping:
  Stream #0:0 -> #0:0 (ffv1 (native) -> wrapped_avframe (native))
[Parsed_fieldmatch_0 @ 0x7f4a740034c0] Frame #89 at 2.97 is still interlaced
[Parsed_idet_1 @ 0x7fe5a0003780] Repeated Fields: Neither:   120 Top:     0 Bottom:     0
[Parsed_idet_1 @ 0x7fe5a0003780] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:   120
[Parsed_idet_1 @ 0x7fe5a0003780] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:   120
frame=  120 fps=0.0 q=-0.0 Lsize=N/A time=00:00:04.00 bitrate=N/A speed=12.1x elapsed=0:00:00.33
"""

# True BFF interlace through the same confirmation pass: stays interlaced.
BFF_FIELDMATCH_STDERR = """\
[Parsed_idet_1 @ 0x55aa00000001] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_1 @ 0x55aa00000001] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_1 @ 0x55aa00000001] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_fieldmatch_0 @ 0x7f380c0034c0] Frame #59 at 3.937 is still interlaced
[Parsed_idet_1 @ 0x7f380c003780] Repeated Fields: Neither:    60 Top:     0 Bottom:     0
[Parsed_idet_1 @ 0x7f380c003780] Single frame detection: TFF:     0 BFF:    60 Progressive:     0 Undetermined:     0
[Parsed_idet_1 @ 0x7f380c003780] Multi frame detection: TFF:     0 BFF:    60 Progressive:     0 Undetermined:     0
"""

# Progressive (mandelbrot + rotate, 25 fps).
PROG_STDERR = """\
[Parsed_idet_0 @ 0x55aa00000002] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x55aa00000002] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x55aa00000002] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x7f0000000001] Repeated Fields: Neither:    75 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x7f0000000001] Single frame detection: TFF:     0 BFF:     0 Progressive:    75 Undetermined:     0
[Parsed_idet_0 @ 0x7f0000000001] Multi frame detection: TFF:     0 BFF:     0 Progressive:    75 Undetermined:     0
"""

ONLY_ZERO_BLOCK = """\
[Parsed_idet_0 @ 0x55c2d5162440] Repeated Fields: Neither:     0 Top:     0 Bottom:     0
[Parsed_idet_0 @ 0x55c2d5162440] Single frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
[Parsed_idet_0 @ 0x55c2d5162440] Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0
/nonexistent.avi: No such file or directory
"""


def test_parse_reference_takes_last_block() -> None:
    single, multi, rep = idet.parse_idet_summary(REF_STDERR)
    assert multi == IdetCounts(tff=0, bff=REFERENCE_FRAMES, progressive=0, undetermined=0)
    assert single == IdetCounts(bff=REFERENCE_FRAMES)
    assert rep == RepeatCounts(neither=REFERENCE_FRAMES, top=0, bottom=0)
    assert multi.total == multi.determined == REFERENCE_FRAMES


def test_parse_window_counts_one_more_than_requested() -> None:
    _, multi, rep = idet.parse_idet_summary(REF_WINDOW_STDERR)
    assert multi.bff == 51 and rep.neither == 51


def test_parse_telecine_signature() -> None:
    single, multi, rep = idet.parse_idet_summary(TELE_STDERR)
    assert rep == RepeatCounts(neither=72, top=24, bottom=24)
    assert multi == IdetCounts(tff=119, bff=0, progressive=0, undetermined=1)
    assert single == IdetCounts(tff=96, undetermined=24)
    assert (rep.top + rep.bottom) / rep.total == pytest.approx(0.40)


def test_parse_fieldmatch_passes() -> None:
    _, multi, rep = idet.parse_idet_summary(TELE_FIELDMATCH_STDERR)
    assert multi == IdetCounts(undetermined=120) and rep == RepeatCounts(neither=120)
    _, multi, rep = idet.parse_idet_summary(BFF_FIELDMATCH_STDERR)
    assert multi == IdetCounts(bff=60) and rep == RepeatCounts(neither=60)
    _, multi, _ = idet.parse_idet_summary(PROG_STDERR)
    assert multi == IdetCounts(progressive=75)


def test_parse_all_zero_only_is_zero_not_error() -> None:
    # A decode failure leaves only the probing block; the caller checks the exit code.
    _, multi, rep = idet.parse_idet_summary(ONLY_ZERO_BLOCK)
    assert multi.total == 0 and rep.total == 0


def test_parse_ffmpeg_stats() -> None:
    assert idet.parse_ffmpeg_stats(REF_STDERR) == {"frames": 274.0, "time_s": pytest.approx(10.96)}
    assert idet.parse_ffmpeg_stats(REF_WINDOW_STDERR) == {"frames": 50.0, "time_s": pytest.approx(2.0)}
    assert idet.parse_ffmpeg_stats(PROG_STDERR) is None
    assert idet.parse_ffmpeg_stats("frame=   10 fps=0.0 q=-0.0 Lsize=N/A time=01:02:03.50 bitrate=N/A")["time_s"] == pytest.approx(3723.5)


def test_parse_no_summary_raises() -> None:
    with pytest.raises(InputError) as ei:
        idet.parse_idet_summary("ffmpeg: nothing useful here\n")
    assert ei.value.code == ErrorCode.ANALYSIS_FAILED


def test_parse_tolerates_crlf_and_prefix_noise() -> None:
    text = REF_STDERR.replace("\n", "\r\n")
    _, multi, _ = idet.parse_idet_summary(text)
    assert multi.bff == REFERENCE_FRAMES


# --------------------------------------------------------------------------- planning
def test_plan_short_file_is_one_full_window() -> None:
    assert idet.plan_windows(274, 10.96, 25.0) == [(0, 0.0, 0)]
    assert idet.plan_windows(1500, 60.0, 25.0) == [(0, 0.0, 0)]
    assert idet.plan_windows(None, 30.0, 25.0) == [(0, 0.0, 0)]  # unknown length < 60 s
    assert idet.plan_windows(None, 0.0, 25.0) == [(0, 0.0, 0)]  # nothing known
    assert idet.plan_windows(100000, 4000.0, 25.0, full_scan=True) == [(0, 0.0, 0)]


def test_plan_long_file_windows_spread_2_to_98_percent() -> None:
    total = 25 * 3600  # one hour of PAL
    plan = idet.plan_windows(total, 3600.0, 25.0)
    assert len(plan) == idet.IDET_WINDOWS
    starts = [p[0] for p in plan]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert starts[0] == round(total * 0.02)
    assert starts[-1] + idet.IDET_WINDOW_FRAMES == round(total * 0.98)
    assert all(p[2] == idet.IDET_WINDOW_FRAMES for p in plan)
    assert all(abs(p[1] - p[0] / 25.0) < 0.01 for p in plan)
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert max(gaps) - min(gaps) <= 1


def test_plan_long_file_unknown_frames_uses_duration() -> None:
    plan = idet.plan_windows(None, 600.0, 30000 / 1001)
    assert len(plan) == idet.IDET_WINDOWS and plan[0][1] > 0


def test_plan_overrides_and_no_overlap_near_threshold() -> None:
    plan = idet.plan_windows(1600, 64.0, 25.0)
    starts = [p[0] for p in plan]
    assert len(plan) == 5 and all(b - a >= idet.IDET_WINDOW_FRAMES for a, b in zip(starts, starts[1:], strict=False))
    plan = idet.plan_windows(100000, 4000.0, 25.0, windows=3, window_frames=100)
    assert len(plan) == 3 and all(p[2] == 100 for p in plan)
    # too many/large windows for the file -> full scan instead
    assert idet.plan_windows(2000, 80.0, 25.0, windows=10, window_frames=300) == [(0, 0.0, 0)]
    # explicit overrides sample even a short file when they fit
    plan = idet.plan_windows(240, 8.0, 30.0, windows=2, window_frames=100)
    assert len(plan) == 2 and plan[0][0] == 5 and plan[1][0] == 135 and plan[1][2] == 100
    assert idet.plan_windows(240, 8.0, 30.0, windows=3, window_frames=100) == [(0, 0.0, 0)]


def test_plan_sections() -> None:
    assert idet.plan_sections(240, 30.0) == [(0, 0.0, 60), (60, 2.0, 60), (120, 4.0, 60), (180, 6.0, 60)]
    assert idet.plan_sections(1000, 25.0) == [(i * 200, i * 8.0, 200) for i in range(5)]
    assert idet.plan_sections(130, 25.0) == [(0, 0.0, 65), (65, 2.6, 65)]
    assert idet.plan_sections(99, 25.0) == [(0, 0.0, 0)]
    secs = idet.plan_sections(1003, 25.0)
    assert sum(s[2] for s in secs) == 1003


# --------------------------------------------------------------------------- command shape
def test_idet_command_shape() -> None:
    cmd = idet.idet_command("/v/clip.avi", 0.0, 0)
    assert cmd[0] == "ffmpeg" and "-nostats" in cmd and "-nostdin" in cmd
    assert "-ss" not in cmd and "-frames:v" not in cmd
    assert cmd[cmd.index("-vf") + 1] == "idet"
    assert cmd[-4:] == ["-dn", "-f", "null", "-"]
    assert cmd[cmd.index("-i") + 1] == "/v/clip.avi"
    cmd = idet.idet_command("rel/clip.avi", 12.5, 300, fieldmatch=True, ffmpeg="/opt/ffmpeg")
    assert cmd[0] == "/opt/ffmpeg"
    assert cmd[cmd.index("-ss") + 1] == "12.500"
    assert cmd[cmd.index("-frames:v") + 1] == "300"
    assert cmd[cmd.index("-vf") + 1] == "fieldmatch=order=auto:combmatch=full,idet"
    assert cmd[cmd.index("-i") + 1].startswith("/")  # absolute: never an option or protocol
    assert cmd.index("-ss") < cmd.index("-i")  # input seeking


def test_analyze_windows_uses_counts_as_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, int, bool]] = []

    def fake(path, start_s, frames, *, fieldmatch=False, ffmpeg="ffmpeg", extra_filters=(), stats=None):
        calls.append((start_s, frames, fieldmatch))
        if stats is not None:
            stats.update(idet.parse_ffmpeg_stats(REF_WINDOW_STDERR))
        return idet.parse_idet_summary(REF_WINDOW_STDERR)

    monkeypatch.setattr(idet, "run_idet_window", fake)
    seen: list[tuple[int, int]] = []
    ws = idet.analyze_windows("x.avi", [(100, 4.0, 50), (200, 8.0, 50)], progress=lambda d, t: seen.append((d, t)))
    assert [(w.start_frame, w.start_s, w.frames) for w in ws] == [(100, 4.0, 51), (200, 8.0, 51)]
    assert ws[0].span_frames == 50 and ws[0].span_s == pytest.approx(2.0)
    assert calls == [(4.0, 50, False), (8.0, 50, False)]
    assert seen == [(1, 2), (2, 2)]


# --------------------------------------------------------------------------- live
@pytest.mark.needs_sandbox
def test_live_reference_full_and_window(reference_clip: Path) -> None:
    st: dict[str, float] = {}
    single, multi, rep = idet.run_idet_window(str(reference_clip), 0.0, 0, stats=st)
    assert multi == IdetCounts(bff=REFERENCE_FRAMES) and rep.neither == REFERENCE_FRAMES
    assert st["frames"] == REFERENCE_FRAMES and st["time_s"] == pytest.approx(10.96, abs=0.05)
    _, multi, _ = idet.run_idet_window(str(reference_clip), 5.0, 50)
    assert multi.bff >= 50 and multi.tff == 0


@pytest.mark.needs_sandbox
def test_live_non_ascii_path(reference_clip: Path, tmp_path: Path) -> None:
    d = tmp_path / "idet ünïcode ビデオ"
    d.mkdir()
    target = d / "clip ö.avi"
    try:
        target.symlink_to(reference_clip)
    except OSError:
        shutil.copyfile(reference_clip, target)
    ws = idet.analyze_windows(str(target), idet.plan_windows(REFERENCE_FRAMES, 10.96, 25.0))
    assert len(ws) == 1 and ws[0].multi.bff == REFERENCE_FRAMES and ws[0].frames == REFERENCE_FRAMES


@pytest.mark.needs_sandbox
def test_live_decode_failure_raises(tmp_path: Path) -> None:
    f = tmp_path / "garbage.avi"
    f.write_bytes(b"\0" * 4096)
    with pytest.raises(InputError) as ei:
        idet.run_idet_window(str(f), 0.0, 0)
    assert ei.value.code == ErrorCode.ANALYSIS_FAILED
