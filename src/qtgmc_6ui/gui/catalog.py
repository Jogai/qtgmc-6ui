"""Profiles and encoder/container data, fetched from the CLI (``profiles … --json``, ``encoders --json``)."""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..models import EncoderInfo, MuxerInfo, Profile, ToolInfo
from .backend import CliCall, CliResult

COMMON_CONTAINERS = ("matroska", "mp4", "mov", "webm", "avi", "mpegts", "ogg", "nut")
COMMON_VIDEO = ("libx264", "libx265", "libsvtav1", "libaom-av1", "libvpx-vp9", "ffv1", "prores_ks", "dnxhd",
                "mpeg2video", "mpeg4", "utvideo", "huffyuv")
COMMON_AUDIO = ("aac", "libopus", "flac", "libmp3lame", "libvorbis", "ac3", "eac3", "alac", "pcm_s16le", "pcm_s24le")
CLI_BACKENDS = {"x264": ("libx264", "H.264"), "x265": ("libx265", "HEVC"), "kvazaar": ("libx265", "HEVC")}


class Catalog(QObject):
    """Async cache of everything the viewing-copy UI needs from the backend."""

    profilesChanged = Signal()
    encodersLoaded = Signal()
    probesLoaded = Signal(str)  # container
    encoderDetailsLoaded = Signal(str)  # encoder name
    failed = Signal(str)  # human-readable message

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.profiles: list[Profile] = []
        self.profiles_error = ""
        self.profiles_dir: str = ""
        self.muxers: dict[str, MuxerInfo] = {}
        self.encoders: dict[str, EncoderInfo] = {}
        self.tools: dict[str, ToolInfo] = {}
        self.probes: dict[str, dict[str, dict[str, Any]]] = {}  # container -> encoder -> probe dict
        self.details: dict[str, EncoderInfo] = {}
        self.encoders_error = ""
        self._calls: set[CliCall] = set()
        self._pending_probes: set[str] = set()
        self._pending_details: set[str] = set()

    # ------------------------------------------------------------------ plumbing
    def _run(self, args: list[str], handler) -> None:
        call = CliCall(args, self)
        self._calls.add(call)

        def done(result: CliResult) -> None:
            self._calls.discard(call)
            call.deleteLater()
            handler(result)

        call.finished.connect(done)
        call.start()

    def shutdown(self) -> None:
        for call in list(self._calls):
            call.kill()
        self._calls.clear()

    @property
    def busy(self) -> bool:
        return bool(self._calls)

    # ------------------------------------------------------------------ profiles
    def load_profiles(self) -> None:
        self._run(["profiles", "list", "--json"], self._on_profiles)
        if not self.profiles_dir:
            self._run(["profiles", "path", "--json"], self._on_path)

    def _on_profiles(self, r: CliResult) -> None:
        data = r.data.get("profiles") if isinstance(r.data, dict) else r.data
        if not r.ok or not isinstance(data, list):
            self.profiles_error = (r.error or {}).get("message", "could not list profiles")
            self.failed.emit(f"Viewing-copy profiles unavailable: {self.profiles_error}")
        else:
            self.profiles_error = ""
            out = []
            for d in data:
                try:
                    out.append(Profile.from_dict(d))
                except (TypeError, ValueError):
                    continue
            self.profiles = out
        self.profilesChanged.emit()

    def _on_path(self, r: CliResult) -> None:
        if r.ok and isinstance(r.data, dict) and r.data.get("path"):
            self.profiles_dir = str(r.data["path"])

    def profile(self, pid: str) -> Profile | None:
        return next((p for p in self.profiles if p.id == pid), None)

    # ------------------------------------------------------------------ encoders
    def load_encoders(self) -> None:
        self._run(["encoders", "--json"], self._on_encoders)

    def _on_encoders(self, r: CliResult) -> None:
        d = r.data if isinstance(r.data, dict) else {}
        if not r.ok or "encoders" not in d:
            self.encoders_error = (r.error or {}).get("message", "could not list encoders")
            self.failed.emit(f"Encoder list unavailable: {self.encoders_error}")
            self.encodersLoaded.emit()
            return
        self.encoders_error = ""
        self.muxers = {m["name"]: MuxerInfo.from_dict(m) for m in d.get("muxers") or []}
        self.encoders = {e["name"]: EncoderInfo.from_dict(e) for e in d.get("encoders") or []}
        self.tools = {t["name"]: ToolInfo.from_dict(t) for t in d.get("tools") or []}
        self._store_probes(d.get("probes") or d.get("matrix") or [])
        self.encodersLoaded.emit()

    def _store_probes(self, probes: list[dict[str, Any]]) -> set[str]:
        touched = set()
        for p in probes:
            c, e = p.get("container"), p.get("encoder")
            if c and e:
                self.probes.setdefault(c, {})[e] = p
                touched.add(c)
        return touched

    def load_probes(self, container: str) -> None:
        """Cached verdicts first (instant); pairs never tested are then probe-muxed in the background."""
        if container in self.probes or container in self._pending_probes:
            return
        self._pending_probes.add(container)

        def done(r: CliResult, probing: bool) -> None:
            d = r.data if isinstance(r.data, dict) else {}
            self._store_probes(d.get("probes") or [])
            self.probes.setdefault(container, {})
            if isinstance(d.get("muxer"), dict):
                self.muxers[container] = MuxerInfo.from_dict(d["muxer"])
            if not probing and d.get("unprobed"):
                self.probesLoaded.emit(container)  # show what is known while the rest is tested
                self._run(["encoders", "--json", "--container", container, "--common", "--probe"], lambda r2: done(r2, True))
                return
            if not r.ok:
                self.failed.emit(f"Codec compatibility for {container} unavailable: {(r.error or {}).get('message', '')}")
            self._pending_probes.discard(container)
            self.probesLoaded.emit(container)

        self._run(["encoders", "--json", "--container", container, "--common"], lambda r: done(r, False))

    def probes_pending(self, container: str) -> bool:
        return container in self._pending_probes

    def load_details(self, encoder: str) -> None:
        if encoder in self.details or encoder in self._pending_details or encoder not in self.encoders:
            return
        self._pending_details.add(encoder)

        def done(r: CliResult) -> None:
            self._pending_details.discard(encoder)
            d = r.data if isinstance(r.data, dict) else {}
            info = d.get("encoder") if isinstance(d.get("encoder"), dict) else None
            if info:
                self.details[encoder] = EncoderInfo.from_dict(info)
            self.encoderDetailsLoaded.emit(encoder)

        self._run(["encoders", "--json", "--encoder", encoder], done)

    # ------------------------------------------------------------------ queries
    def tool_available(self, name: str) -> bool:
        t = self.tools.get(name)
        return bool(t and t.available)

    def verdict(self, container: str, encoder: str) -> tuple[str, str]:
        """(verdict, reason) with verdict in ok | ok_with_warning | fail | unknown; the reason is the
        backend's text (``muxmatrix.reason``), the raw probe note only as a fallback."""
        p = self.probes.get(container, {}).get(encoder)
        if not p:
            return "unknown", ""
        v = str(p.get("verdict", "unknown"))
        if v == "unknown":
            return v, ""
        return v, str(p.get("reason") or p.get("note") or "")

    def containers(self, show_all: bool) -> list[str]:
        names = list(self.muxers)
        common = [c for c in COMMON_CONTAINERS if c in self.muxers]
        if not show_all:
            return common
        return common + sorted(n for n in names if n not in common)

    def encoder_names(self, kind: str, show_all: bool) -> list[str]:
        names = [n for n, e in self.encoders.items() if e.kind == kind]
        common = [n for n in (COMMON_VIDEO if kind == "video" else COMMON_AUDIO) if n in self.encoders]
        if not show_all:
            return common
        return common + sorted(n for n in names if n not in common)

    def save_profile(self, profile: Profile, handler, *, overwrite: bool = False, replaces: str | None = None) -> None:
        """``profiles save - --json`` with the profile as JSON on stdin; handler(ok, problems, saved_path).
        ``replaces``: id of the profile being renamed, deleted after a successful save."""
        data = {k: v for k, v in profile.to_dict().items() if k not in ("builtin", "path")}
        args = ["profiles", "save", "-", "--json"] + (["--overwrite"] if overwrite else [])

        def done(r: CliResult) -> None:
            d = r.data if isinstance(r.data, dict) else {}
            if d.get("valid") and d.get("path"):
                if replaces and replaces != profile.id:
                    self.delete_profile(replaces, lambda ok, msg: None)
                handler(True, [], str(d["path"]))
                return
            problems = [str(x) for x in d.get("problems") or []]
            if not problems:
                problems = [str((d.get("error") or r.error or {}).get("message", "saving failed"))]
            handler(False, problems, "")

        call = CliCall(args, self, stdin=json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self._calls.add(call)

        def finished(r: CliResult) -> None:
            self._calls.discard(call)
            call.deleteLater()
            done(r)

        call.finished.connect(finished)
        call.start()

    def delete_profile(self, pid: str, handler) -> None:
        """``profiles delete ID --json``; handler(ok, message)."""

        def done(r: CliResult) -> None:
            if r.ok:
                handler(True, "")
            else:
                handler(False, str((r.error or {}).get("message", "deleting failed")))

        self._run(["profiles", "delete", pid, "--json"], done)
