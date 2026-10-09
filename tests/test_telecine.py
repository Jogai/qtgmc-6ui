"""Telecine signature + confirmation logic on recorded idet counts."""

from __future__ import annotations

import pytest

from qtgmc_6ui import telecine
from qtgmc_6ui.models import IdetCounts, IdetWindow, RepeatCounts


def win(multi: IdetCounts, rep: RepeatCounts, start_frame: int = 0, start_s: float = 0.0, frames: int | None = None) -> IdetWindow:
    return IdetWindow(start_frame=start_frame, start_s=start_s, frames=frames if frames is not None else multi.total, multi=multi, repeated=rep)


# Recorded (ffmpeg n9.0.2, see test_idet samples)
TELE = win(IdetCounts(tff=119, undetermined=1), RepeatCounts(neither=72, top=24, bottom=24))
TELE_AFTER = (IdetCounts(undetermined=120), RepeatCounts(neither=120))
REF = win(IdetCounts(bff=274), RepeatCounts(neither=274))
BFF_AFTER = (IdetCounts(bff=60), RepeatCounts(neither=60))
PROG = win(IdetCounts(progressive=75), RepeatCounts(neither=75))


def test_repeat_ratio() -> None:
    assert telecine.repeat_ratio_raw(TELE) == pytest.approx(0.40)
    assert telecine.repeat_ratio(TELE) == pytest.approx(48 / 119)
    assert telecine.repeat_ratio(REF) == 0.0
    assert telecine.repeat_ratio(win(IdetCounts(), RepeatCounts())) == 0.0
    # static stretches (undetermined, no repeat) do not dilute the signature
    diluted = win(IdetCounts(tff=30, undetermined=70), RepeatCounts(neither=88, top=6, bottom=6))
    assert telecine.repeat_ratio_raw(diluted) == pytest.approx(0.12)
    assert telecine.repeat_ratio(diluted) == pytest.approx(0.40) and telecine.is_suspect(diluted)


def test_suspect_thresholds() -> None:
    assert telecine.is_suspect(TELE) and telecine.is_strong(TELE)
    assert not telecine.is_suspect(REF) and not telecine.is_suspect(PROG)
    border = win(IdetCounts(tff=100), RepeatCounts(neither=85, top=8, bottom=7))
    assert telecine.is_suspect(border) and not telecine.is_strong(border)
    below = win(IdetCounts(tff=100), RepeatCounts(neither=86, top=7, bottom=7))
    assert not telecine.is_suspect(below)
    # absolute minimum of repeated fields
    tiny = win(IdetCounts(tff=5), RepeatCounts(neither=2, top=2, bottom=1))
    assert telecine.repeat_ratio(tiny) >= 0.15 and not telecine.is_suspect(tiny)


def test_interlaced_share() -> None:
    assert telecine.interlaced_share(IdetCounts(tff=119, undetermined=1)) == 1.0
    assert telecine.interlaced_share(IdetCounts(undetermined=120)) == 0.0
    assert telecine.interlaced_share(IdetCounts(tff=20, progressive=80)) == pytest.approx(0.2)
    assert telecine.interlaced_share(None) == 0.0


def test_window_is_telecined_signature_only() -> None:
    w = win(TELE.multi, TELE.repeated)
    assert w.after_fieldmatch is None and telecine.window_is_telecined(w)
    assert not telecine.window_is_telecined(REF)


def test_window_is_telecined_with_confirmation() -> None:
    w = win(TELE.multi, TELE.repeated)
    w.after_fieldmatch, w.repeated_after_fieldmatch = TELE_AFTER
    w.combed_after_fieldmatch = 0
    assert telecine.confirmation_clean(w) is True and telecine.window_is_telecined(w)
    # chroma smeared across fields: idet still sees combing after fieldmatch (and calls it BFF),
    # fieldmatch's own comb detector is clean -> telecined, never "interlaced bff"
    w.after_fieldmatch = IdetCounts(bff=71, progressive=49)
    assert telecine.combed_share(w) == 0.0 and telecine.window_is_telecined(w)
    # strong signature wins even when fieldmatch cannot clean it (damaged fields)
    w.combed_after_fieldmatch = 120
    assert telecine.confirmation_clean(w) is False and telecine.is_strong(w) and telecine.window_is_telecined(w)
    # weak signature (0.20, e.g. half telecined) that fieldmatch leaves combed -> not telecine
    w2 = win(IdetCounts(bff=60), RepeatCounts(neither=48, top=6, bottom=6))
    w2.after_fieldmatch, w2.repeated_after_fieldmatch = BFF_AFTER
    w2.combed_after_fieldmatch = 60
    assert telecine.is_suspect(w2) and not telecine.is_strong(w2) and not telecine.window_is_telecined(w2)
    w2.combed_after_fieldmatch = 18  # 0.30: at the limit still counts as clean
    assert telecine.window_is_telecined(w2)
    # fallback without a combed count: idet share after fieldmatch, exactly at the limit still confirms
    w3 = win(IdetCounts(tff=100), RepeatCounts(neither=80, top=10, bottom=10))
    w3.after_fieldmatch = IdetCounts(tff=30, progressive=70)
    assert telecine.combed_share(w3) is None and telecine.window_is_telecined(w3)
    w3.after_fieldmatch = IdetCounts(tff=31, progressive=69)
    assert not telecine.window_is_telecined(w3)


def test_confirm_windows_runs_only_suspects(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, int, bool]] = []

    def fake(path, start_s, frames, *, fieldmatch=False, ffmpeg="ffmpeg", extra_filters=(), stats=None):
        calls.append((start_s, frames, fieldmatch))
        if stats is not None:
            stats.update({"frames": float(frames), "time_s": frames / 30.0, "combed": 1.0})
        return IdetCounts(), TELE_AFTER[0], TELE_AFTER[1]

    monkeypatch.setattr(telecine, "run_idet_window", fake)
    full = win(TELE.multi, TELE.repeated)  # full-scan window: start 0
    sampled = win(TELE.multi, TELE.repeated, start_frame=750, start_s=30.0, frames=301)
    clean = win(REF.multi, REF.repeated, start_frame=1500, start_s=60.0, frames=275)
    seen: list[tuple[int, int]] = []
    out = telecine.confirm_windows("x.mkv", [full, sampled, clean], progress=lambda d, t: seen.append((d, t)))
    assert out == [full, sampled]
    assert calls == [(0.0, 120, True), (30.0, 301, True)]  # exactly the window's frames
    assert seen == [(1, 2), (2, 2)]
    assert full.after_fieldmatch == TELE_AFTER[0] and full.repeated_after_fieldmatch == TELE_AFTER[1]
    assert full.combed_after_fieldmatch == 1 and sampled.combed_after_fieldmatch == 1
    assert clean.after_fieldmatch is None
    assert telecine.window_is_telecined(full) and telecine.window_is_telecined(sampled)
