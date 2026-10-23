"""vsenv + buildinfo: no-autoload core, explicit plugin loading, ABI checks, dev build-info."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from qtgmc_6ui import buildinfo, paths, vsenv
from qtgmc_6ui.errors import ComponentError, ErrorCode
from qtgmc_6ui.models import BuildInfo, PluginInfo


def _dev_bi() -> BuildInfo:
    return BuildInfo(python_minor=list(sys.version_info[:2]))


# ----------------------------------------------------------------------------- buildinfo
def test_buildinfo_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QTGMC6UI_PLUGIN_DIR", raising=False)
    with pytest.raises(ComponentError) as ei:
        buildinfo.load(tmp_path / "nope.json")
    assert ei.value.code == ErrorCode.BUILD_INFO_MISSING
    assert "reinstall" in ei.value.hint
    with pytest.raises(ComponentError):
        buildinfo.load_or_dev(tmp_path / "nope.json")


def test_buildinfo_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bi.json"
    p.write_text("[1, 2]")
    with pytest.raises(ComponentError) as ei:
        buildinfo.load(p)
    assert ei.value.code == ErrorCode.BUILD_INFO_MISSING and "malformed" in ei.value.message


def test_buildinfo_roundtrip(tmp_path: Path) -> None:
    bi = BuildInfo(pkgver="0.1.0", python_minor=[3, 14], vapoursynth_core=80, vapoursynth_api=[4, 3],
                   plugins=[PluginInfo("akarin", "akarin", "x.y", "v1.5.0", "libakarin.so")],
                   python_packages={"vsjetpack": "2.2.6"})
    p = tmp_path / "bi.json"
    p.write_text(json.dumps(bi.to_dict()))
    assert buildinfo.load(p) == bi


def test_dev_build_info_scans_plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("libakarin.so", "libresize2.so", "mvutensils.so", "mvutensils.avx2.so", "mvutensils.zn4.so",
                 "vsznedi3.so", "libzsmooth.so", "libvivtc.so", "nnedi3_weights.bin", "manifest.vs"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path))
    bi = buildinfo.load_or_dev(tmp_path / "absent.json")
    names = {p.name: p for p in bi.plugins}
    assert set(names) == {"akarin", "resize2", "mvutensils", "znedi3", "zsmooth", "vivtc"}
    assert names["mvutensils"].file == "mvutensils.so" and names["mvutensils"].namespace == "mvu"
    assert names["vivtc"].required is False and names["akarin"].required is True
    assert bi.python_minor == list(sys.version_info[:2])
    assert bi.pkgrel == "dev"


# ----------------------------------------------------------------------------- check_abi (pure)
@pytest.mark.needs_sandbox
def test_check_abi_messages() -> None:
    import vapoursynth as vs

    ok = BuildInfo(python_minor=list(sys.version_info[:2]), vapoursynth_core=vs.__version__.release_major,
                   vapoursynth_api=[vs.__api_version__.api_major, vs.__api_version__.api_minor])
    assert vsenv.check_abi(ok) == []
    bad = BuildInfo(python_minor=[3, 9], vapoursynth_core=70, vapoursynth_api=[3, 0])
    problems = vsenv.check_abi(bad)
    assert len(problems) == 2
    assert "Arch updated python" in problems[0] and "3.9" in problems[0] and "reinstall/rebuild" in problems[0]
    assert "Arch updated vapoursynth" in problems[1] and "R70" in problems[1] and "API" in problems[1]
    assert BuildInfo() and vsenv.check_abi(BuildInfo()) == []  # unknown fields are not compared
    # a release change with the same API is not a problem (reviews/vs-r81-compat.md), only a notice
    older = BuildInfo(python_minor=list(sys.version_info[:2]), vapoursynth_core=vs.__version__.release_major - 1,
                      vapoursynth_api=[vs.__api_version__.api_major, vs.__api_version__.api_minor])
    assert vsenv.check_abi(older) == [] and len(vsenv.abi_warnings(older)) == 1


@pytest.mark.needs_sandbox
def test_runtime_versions() -> None:
    v = vsenv.runtime_versions()
    assert v["vapoursynth"].startswith("R") and v["api"].startswith("R4")
    assert v["vsjetpack"] == "2.2.6" and v["jetpytools"] == "3.1.2"


# ----------------------------------------------------------------------------- core (sandbox)
@pytest.fixture
def no_plugins_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "plugins"
    d.mkdir()
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(d))
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(tmp_path / "absent.json"))
    return d


@pytest.mark.needs_sandbox
def test_open_core_disables_autoload_and_loads_bestsource(no_plugins_dir: Path) -> None:
    import vapoursynth as vs

    vc = vsenv.open_core()
    try:
        assert vc.core.flags & vs.DISABLE_AUTO_LOADING
        ns = sorted(r["namespace"] for r in vsenv.plugin_table(vc.core))
        assert ns == ["bs", "resize", "std", "text"]  # repo autoload dir (mvtools, deblock) NOT loaded
        assert vc.has("bs") and not vc.has("mvu")
        assert vc.build_info is not None and vc.build_info.pkgrel == "dev"
        with pytest.raises(ComponentError):
            vsenv.open_core()  # only one per process
    finally:
        vc.close()
    # reopen after close works (fresh environment, fresh core)
    vc2 = vsenv.open_core(threads=2, cache_mb=256)
    try:
        assert vc2.core.num_threads == 2 and vc2.core.max_cache_size == 256
        assert vc2.core is not vc.core
    finally:
        vc2.close()


@pytest.mark.needs_sandbox
def test_abi_mismatch_is_named_error(no_plugins_dir: Path) -> None:
    bad = BuildInfo(python_minor=[3, 9], vapoursynth_core=80, vapoursynth_api=[4, 3])
    with pytest.raises(ComponentError) as ei:
        vsenv.open_core(build_info=bad)
    assert ei.value.code == ErrorCode.PYTHON_VERSION_MISMATCH
    assert "Arch updated python" in ei.value.message
    assert vsenv.current() is None
    bad = BuildInfo(python_minor=list(sys.version_info[:2]), vapoursynth_core=12, vapoursynth_api=[3, 0])
    with pytest.raises(ComponentError) as ei:
        vsenv.open_core(build_info=bad)
    assert ei.value.code == ErrorCode.VS_VERSION_MISMATCH
    assert vsenv.current() is None


@pytest.mark.needs_sandbox
def test_load_plugin_errors(no_plugins_dir: Path, tmp_path: Path) -> None:
    sysdir = paths.system_vs_plugin_dir()
    assert sysdir is not None
    bs = sysdir / "libbestsource.so"
    vc = vsenv.open_core()
    try:
        # same file again: no-op
        assert vsenv.load_plugin(vc.core, bs) == "bs"
        # copy under another name: same identifier -> conflict
        copy = tmp_path / "libbs_copy.so"
        shutil.copy(bs, copy)
        with pytest.raises(ComponentError) as ei:
            vsenv.load_plugin(vc.core, copy)
        assert ei.value.code == ErrorCode.PLUGIN_CONFLICT and "com.vapoursynth.bestsource" in ei.value.message
        # missing / garbage files
        with pytest.raises(ComponentError) as ei:
            vsenv.load_plugin(vc.core, tmp_path / "nope.so")
        assert ei.value.code == ErrorCode.PLUGIN_LOAD_FAILED
        garbage = tmp_path / "garbage.so"
        garbage.write_bytes(b"not a shared object")
        with pytest.raises(ComponentError) as ei:
            vsenv.load_plugin(vc.core, garbage)
        assert ei.value.code == ErrorCode.PLUGIN_LOAD_FAILED and "garbage.so" in ei.value.message
        # expectation mismatch names the plugin
        with pytest.raises(ComponentError) as ei:
            vsenv.load_plugin(vc.core, bs, PluginInfo("bestsource", "wrongns", "", "", "libbestsource.so"))
        assert "expected 'wrongns'" in ei.value.message
    finally:
        vc.close()


@pytest.mark.needs_sandbox
def test_required_bundled_plugin_missing(no_plugins_dir: Path) -> None:
    bi = _dev_bi()
    bi.plugins = [PluginInfo("akarin", "akarin", "", "v1.5.0", "libakarin.so", True, "bundled")]
    with pytest.raises(ComponentError) as ei:
        vsenv.open_core(build_info=bi)
    assert ei.value.code == ErrorCode.PLUGIN_LOAD_FAILED and "akarin" in ei.value.message
    assert vsenv.current() is None
    # optional plugin missing is recorded, not fatal
    bi.plugins[0].required = False
    vc = vsenv.open_core(build_info=bi)
    try:
        assert "akarin" in vc.skipped
    finally:
        vc.close()


@pytest.mark.needs_sandbox
def test_plugin_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path / "missing"))
    bi = _dev_bi()
    bi.plugins = [PluginInfo("akarin", "akarin", "", "v1.5.0", "libakarin.so", True, "bundled")]
    with pytest.raises(ComponentError) as ei:
        vsenv.open_core(build_info=bi)
    assert ei.value.code == ErrorCode.PLUGIN_DIR_MISSING
