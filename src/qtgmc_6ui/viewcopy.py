"""Viewing-copy encoders (WP-C): from the MASTER, never from the original.

Backends:
  ffmpeg        ffmpeg -i MASTER [-vf scale/select] -c:v ENC <rate control> [-pix_fmt] -c:a ENC ... OUT
  x264/x265/kvazaar (CLI)
                ffmpeg -i MASTER -map 0:v -f yuv4mpegpipe [-pix_fmt] - | TOOL --y4m [opts] -o video.es -
                then ffmpeg -i video.es -i MASTER -map 0:v -map 1:a -c:v copy -c:a ENC ... OUT
                (kvazaar: ``kvazaar -i - --input-res WxH --input-fps N/D -o out.hevc``; verify flags)
  sox audio chain (optional, before the audio encoder)
                ffmpeg -i MASTER -map 0:a:N -f sox - | sox -t sox - -t sox - EFFECTS | ffmpeg -f sox -i - ...
                (or via a temp .wav/.w64 when piping proves fragile; WP-C decides and documents)
Scaling: square pixels = width*SAR rounded to even, after deinterlacing (master is already
deinterlaced).  Single rate from a double-rate master: ``select=not(mod(n\\,2)),setpts=N/FRAME_RATE/TB``
with ``-r`` = master fps / 2 - keeps the first field's frame of each pair, exact for 60000/1001.
Colour tags: copied from the master via setparams in the filter chain.
"""

from __future__ import annotations

from .events import Emitter
from .models import OutputFile, Profile, ProbeInfo
from .runtime import CancelToken


def build_commands(master: str, master_probe: ProbeInfo, profile: Profile, output: str, *, overwrite: bool = False) -> list[list[str]]:
    """The ordered command list (one for ffmpeg backend; pipeline stages for CLI backends)."""
    raise NotImplementedError


def run_view(
    master: str,
    master_probe: ProbeInfo,
    profile: Profile,
    output: str,
    *,
    emitter: Emitter,
    cancel: CancelToken,
    overwrite: bool = False,
) -> OutputFile:
    """Encode one viewing copy with progress (frames from ffmpeg stats or the tool's own output)."""
    raise NotImplementedError


def view_output_path(master: str, profile: Profile, name_template: str, output_dir: str) -> str:
    """``<output_dir>/<template>.<profile.id>.<profile.extension>``."""
    raise NotImplementedError
