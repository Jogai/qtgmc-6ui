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
