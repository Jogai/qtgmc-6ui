"""Recorded analysis results for the WP-B tests (what WP-A produces for the reference clip)."""

from __future__ import annotations

import os
from fractions import Fraction

from qtgmc_6ui.models import (
    AnalysisResult,
    AudioProbe,
    Classification,
    Colorimetry,
    ContentClass,
    FieldOrder,
    IdetCounts,
    IdetWindow,
    Preset,
    ProbeInfo,
    Recommendation,
    RecommendedAction,
    RepeatCounts,
    VideoProbe,
)

PLUGIN_DIR_CANDIDATES = ("/workspace/qtgmc/sandbox/plugins", "/workspace/qtgmc/fixtures/plugins")


def reference_analysis(path: str = "/workspace/qtgmc/test_input.avi") -> AnalysisResult:
    size = os.path.getsize(path) if os.path.exists(path) else 41703544
    video = VideoProbe(
        codec="dvvideo", codec_long="DV (Digital Video)", width=720, height=576, pix_fmt="yuv420p", bit_depth=8,
        fps=Fraction(25, 1), sar=Fraction(16, 15), dar=Fraction(4, 3), nb_frames=274, nb_frames_exact=True,
        duration_s=10.96, container_field_order="unknown",
        color_tags=Colorimetry("unknown", "unknown", "unknown", "tv", "tags"),
    )
    audio = [AudioProbe(index=1, codec="pcm_s16le", sample_rate=48000, channels=2, channel_layout="stereo",
                        bit_depth=16, duration_s=10.96)]
    probe = ProbeInfo(path=path, container="avi", size_bytes=size, duration_s=10.96, video=video, audio=audio)
    win = IdetWindow(start_frame=0, start_s=0.0, frames=274, single=IdetCounts(bff=274), multi=IdetCounts(bff=274),
                     repeated=RepeatCounts(neither=274), window_class=ContentClass.INTERLACED_BFF)
    return AnalysisResult(
        input=path, probe=probe, windows=[win], totals_single=win.single, totals_multi=win.multi,
        totals_repeated=win.repeated, fieldmatch_run=False,
        classification=Classification(ContentClass.INTERLACED_BFF, FieldOrder.BFF, 1.0, ["idet: BFF 274/274"]),
        recommendation=Recommendation(RecommendedAction.DEINTERLACE, "interlaced, bottom field first", True),
        colorimetry=Colorimetry("bt470bg", "bt470bg", "bt709", "tv", "default"),
        preset_default=Preset.SLOWER, frames_out_double=548, fps_out_double=Fraction(50, 1), sampled_all_frames=True,
    )


def synth_analysis(path: str, field_order: FieldOrder, *, ffprobe: str = "ffprobe") -> AnalysisResult:
    """AnalysisResult for a synthetic clip from a plain ffprobe (independent of WP-A's analysis):
    classification = interlaced with the given order, colorimetry = PAL/NTSC SD defaults, range
    from the stream tag."""
    import json
    import subprocess

    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    vs_ = next(s for s in info["streams"] if s["codec_type"] == "video")
    fps = Fraction(vs_.get("r_frame_rate", "25/1"))
    sar = Fraction(vs_.get("sample_aspect_ratio", "1:1").replace(":", "/") or "1")
    rng = vs_.get("color_range") or "tv"
    video = VideoProbe(codec=vs_["codec_name"], width=int(vs_["width"]), height=int(vs_["height"]), pix_fmt=vs_["pix_fmt"],
                       bit_depth=int(vs_.get("bits_per_raw_sample") or 8), fps=fps, sar=sar, nb_frames=None,
                       nb_frames_exact=False, duration_s=float(info["format"].get("duration") or 0),
                       color_tags=Colorimetry(vs_.get("color_space", "unknown"), vs_.get("color_primaries", "unknown"),
                                              vs_.get("color_transfer", "unknown"), rng, "tags"))
    audio = []
    for s in info["streams"]:
        if s["codec_type"] == "audio":
            audio.append(AudioProbe(index=int(s["index"]), codec=s["codec_name"], sample_rate=int(s.get("sample_rate") or 0),
                                    channels=int(s.get("channels") or 0), bit_depth=int(s.get("bits_per_raw_sample") or 0) or None))
    probe = ProbeInfo(path=path, container=info["format"]["format_name"], size_bytes=int(info["format"].get("size") or 0),
                      duration_s=video.duration_s, video=video, audio=audio)
    ntsc = abs(float(fps) - 30000 / 1001) < 0.1 or abs(float(fps) - 30) < 0.1
    colorimetry = (Colorimetry("smpte170m", "smpte170m", "smpte170m", rng, "default") if ntsc
                   else Colorimetry("bt470bg", "bt470bg", "bt709", rng, "default"))
    content = ContentClass.INTERLACED_TFF if field_order == FieldOrder.TFF else ContentClass.INTERLACED_BFF
    return AnalysisResult(
        input=path, probe=probe, classification=Classification(content, field_order, 1.0, ["synthetic"]),
        recommendation=Recommendation(RecommendedAction.DEINTERLACE, "synthetic interlaced clip", True),
        colorimetry=colorimetry, preset_default=Preset.DRAFT, fps_out_double=fps * 2,
    )
