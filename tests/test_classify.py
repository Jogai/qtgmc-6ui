"""Window and file classification thresholds on recorded / synthetic idet counts."""

from __future__ import annotations

import pytest

from qtgmc_6ui import classify
from qtgmc_6ui.models import (
    ContentClass,
    FieldOrder,
    IdetCounts,
    IdetWindow,
    ProbeInfo,
    RecommendedAction,
    RepeatCounts,
)

PROBE = ProbeInfo()


def win(multi: IdetCounts, rep: RepeatCounts | None = None, start_frame: int = 0, start_s: float = 0.0) -> IdetWindow:
    rep = rep if rep is not None else RepeatCounts(neither=multi.total)
    return IdetWindow(start_frame=start_frame, start_s=start_s, frames=multi.total, single=multi, multi=multi, repeated=rep)


def tele_window(start_frame: int = 0, start_s: float = 0.0, confirmed: bool = True) -> IdetWindow:
    w = win(IdetCounts(tff=119, undetermined=1), RepeatCounts(neither=72, top=24, bottom=24), start_frame, start_s)
    if confirmed:
        w.after_fieldmatch = IdetCounts(undetermined=120)
        w.repeated_after_fieldmatch = RepeatCounts(neither=120)
    return w


REF = win(IdetCounts(bff=274))
TFF = win(IdetCounts(tff=300), start_frame=100, start_s=4.0)
PROG = win(IdetCounts(progressive=75))
UND = win(IdetCounts(undetermined=96))


# --------------------------------------------------------------------------- windows
def test_window_reference_bff() -> None:
    assert classify.classify_window(REF) is ContentClass.INTERLACED_BFF
    assert classify.classify_window(TFF) is ContentClass.INTERLACED_TFF


def test_window_telecined_and_progressive_and_undetermined() -> None:
    assert classify.classify_window(tele_window()) is ContentClass.TELECINED
    assert classify.classify_window(tele_window(confirmed=False)) is ContentClass.TELECINED
    assert classify.classify_window(PROG) is ContentClass.PROGRESSIVE
    assert classify.classify_window(UND) is ContentClass.UNDETERMINED
    assert classify.classify_window(win(IdetCounts())) is ContentClass.UNDETERMINED


def test_window_thresholds() -> None:
    # determined share below 20 % -> undetermined even if what is determined is all BFF
    assert classify.classify_window(win(IdetCounts(bff=19, undetermined=81))) is ContentClass.UNDETERMINED
    assert classify.classify_window(win(IdetCounts(bff=20, undetermined=80))) is ContentClass.INTERLACED_BFF
    # interlaced share 80 % of determined
    assert classify.classify_window(win(IdetCounts(tff=80, progressive=20))) is ContentClass.INTERLACED_TFF
    assert classify.classify_window(win(IdetCounts(tff=79, progressive=21))) is ContentClass.UNDETERMINED
    # progressive share
    assert classify.classify_window(win(IdetCounts(progressive=80, tff=20))) is ContentClass.PROGRESSIVE
    assert classify.classify_window(win(IdetCounts(progressive=79, tff=21))) is ContentClass.UNDETERMINED
    # testsrc2-style noise (TFF 41 / prog 9 of 50) must NOT become progressive, and is interlaced TFF
    assert classify.classify_window(win(IdetCounts(tff=41, progressive=9))) is ContentClass.INTERLACED_TFF


def test_window_field_order_share() -> None:
    assert classify.field_order_of(IdetCounts(tff=80, bff=20)) is FieldOrder.TFF
    assert classify.field_order_of(IdetCounts(tff=79, bff=21)) is None
    assert classify.field_order_of(IdetCounts(tff=20, bff=80)) is FieldOrder.BFF
    assert classify.field_order_of(IdetCounts(progressive=5)) is None
    assert classify.classify_window(win(IdetCounts(tff=150, bff=150))) is ContentClass.UNDETERMINED


def test_window_suspect_but_fieldmatch_says_interlaced() -> None:
    w = win(IdetCounts(bff=60), RepeatCounts(neither=36, top=12, bottom=12))
    w.after_fieldmatch = IdetCounts(bff=60)
    w.repeated_after_fieldmatch = RepeatCounts(neither=60)
    assert classify.classify_window(w) is ContentClass.INTERLACED_BFF
    w.after_fieldmatch = IdetCounts(bff=30, undetermined=30, progressive=30)  # 50 % interlaced: neither
    assert classify.classify_window(w) is ContentClass.UNDETERMINED


# --------------------------------------------------------------------------- files
def test_file_reference_bff() -> None:
    c, r = classify.classify_file([REF], PROBE)
    assert c.content is ContentClass.INTERLACED_BFF and c.field_order is FieldOrder.BFF
    assert c.confidence >= 0.95
    assert r.action is RecommendedAction.DEINTERLACE and "BFF" in r.message
    assert REF.window_class is ContentClass.INTERLACED_BFF
    assert any("274 frames" in s for s in c.reasons) and any("container field_order" in s for s in c.reasons)
    assert not c.warnings


def test_file_multi_window_tff_with_noise() -> None:
    ws = [win(IdetCounts(tff=290, bff=3, undetermined=8), start_frame=i * 1000, start_s=i * 40.0) for i in range(5)]
    c, r = classify.classify_file(ws, PROBE)
    assert c.content is ContentClass.INTERLACED_TFF and c.field_order is FieldOrder.TFF
    assert c.confidence == pytest.approx(290 * 5 / (301 * 5), abs=1e-3)
    assert r.action is RecommendedAction.DEINTERLACE


def test_file_conflicting_orders() -> None:
    bff = win(IdetCounts(bff=300), start_frame=5000, start_s=200.0)
    c, r = classify.classify_file([TFF, bff], PROBE)
    assert c.content is ContentClass.UNDETERMINED and c.field_order is None
    assert r.action is RecommendedAction.CONFIRM_FIELD_ORDER
    assert classify.CONFLICT_WARNING in c.warnings
    assert c.confidence == pytest.approx(0.5)
    # a single window with both orders is a conflict too
    c, r = classify.classify_file([win(IdetCounts(tff=150, bff=150))], PROBE)
    assert c.content is ContentClass.UNDETERMINED and r.action is RecommendedAction.CONFIRM_FIELD_ORDER


def test_file_telecined() -> None:
    c, r = classify.classify_file([tele_window()], PROBE)
    assert c.content is ContentClass.TELECINED and c.field_order is None and c.confidence == 1.0
    assert r.action is RecommendedAction.BLOCK_TELECINED and r.force_allowed
    assert r.message == classify.IVTC_MESSAGE and "IVTC" in r.message
    # telecined + undetermined/progressive sections is still telecined
    c, r = classify.classify_file([tele_window(), UND, PROG], PROBE)
    assert c.content is ContentClass.TELECINED and r.action is RecommendedAction.BLOCK_TELECINED
    assert c.confidence == pytest.approx(120 / (120 + 96 + 75), abs=1e-4)


def test_file_mixed() -> None:
    c, r = classify.classify_file([tele_window(), TFF], PROBE)
    assert c.content is ContentClass.MIXED and c.field_order is None
    assert r.action is RecommendedAction.BLOCK_MIXED and r.force_allowed
    assert "IVTC" in r.message and "interlaced" in r.message
    assert c.confidence == 1.0


def test_file_progressive() -> None:
    c, r = classify.classify_file([PROG], PROBE)
    assert c.content is ContentClass.PROGRESSIVE and c.confidence == 1.0
    assert r.action is RecommendedAction.SKIP_PROGRESSIVE and r.force_allowed
    c, r = classify.classify_file([PROG, UND], PROBE)
    assert c.content is ContentClass.PROGRESSIVE and c.confidence == pytest.approx(75 / 171, abs=1e-4)
    assert classify.UNDETERMINED_SECTIONS_REASON in c.reasons


def test_file_interlaced_plus_progressive_sections_warns() -> None:
    c, r = classify.classify_file([REF, PROG], PROBE)
    assert c.content is ContentClass.INTERLACED_BFF and r.action is RecommendedAction.DEINTERLACE
    assert classify.PROGRESSIVE_SECTIONS_WARNING in c.warnings
    assert c.confidence == pytest.approx(274 / 349, abs=1e-4)


def test_file_all_undetermined() -> None:
    c, r = classify.classify_file([UND], PROBE)
    assert c.content is ContentClass.UNDETERMINED and c.confidence == 0.0
    assert r.action is RecommendedAction.CONFIRM_FIELD_ORDER
    assert classify.NO_INTERLACING_WARNING in c.warnings
    c, r = classify.classify_file([], PROBE)
    assert c.content is ContentClass.UNDETERMINED and r.action is RecommendedAction.CONFIRM_FIELD_ORDER


def test_file_tiny_clip_is_no_evidence() -> None:
    c, r = classify.classify_file([win(IdetCounts(tff=2))], PROBE)
    assert c.content is ContentClass.UNDETERMINED and c.field_order is None
    assert c.confidence == pytest.approx(0.2) and classify.TOO_FEW_FRAMES_WARNING in c.warnings
    assert r.action is RecommendedAction.CONFIRM_FIELD_ORDER and "TFF" in r.message
    assert any("only 2 determined" in s for s in c.reasons)
    # 9 determined frames: still no verdict; 10: verdict
    c, _ = classify.classify_file([win(IdetCounts(bff=9, undetermined=3))], PROBE)
    assert c.content is ContentClass.UNDETERMINED and c.confidence < 0.9
    c, r = classify.classify_file([win(IdetCounts(bff=10, undetermined=3))], PROBE)
    assert c.content is ContentClass.INTERLACED_BFF and r.action is RecommendedAction.DEINTERLACE
    # progressive and telecined verdicts need the same minimum
    c, r = classify.classify_file([win(IdetCounts(progressive=3))], PROBE)
    assert c.content is ContentClass.UNDETERMINED and r.action is RecommendedAction.CONFIRM_FIELD_ORDER
    assert "lean" not in r.message
    # all undetermined keeps its own explanation
    c, _ = classify.classify_file([win(IdetCounts(undetermined=3))], PROBE)
    assert classify.NO_INTERLACING_WARNING in c.warnings and classify.TOO_FEW_FRAMES_WARNING not in c.warnings


def test_file_sets_window_classes_and_roundtrips() -> None:
    ws = [tele_window(), TFF, PROG, UND]
    classify.classify_file(ws, PROBE)
    assert [w.window_class for w in ws] == [
        ContentClass.TELECINED, ContentClass.INTERLACED_TFF, ContentClass.PROGRESSIVE, ContentClass.UNDETERMINED,
    ]
    for w in ws:
        assert IdetWindow.from_dict(w.to_dict()) == w
