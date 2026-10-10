#!/usr/bin/env bash
# Smoke test of an INSTALLED qtgmc-6ui package (the CI gate before publishing to the AUR).
# Needs ffmpeg/ffprobe; run as a normal user.  Exit status 0 only if everything passed.
set -euo pipefail

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fail() { printf 'smoke: FAIL: %s\n' "$*" >&2; exit 1; }
frames() { ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 "$1"; }

qtgmc-6ui version
qtgmc-6ui doctor

# 2 s of 25i PAL (top field first) with sound: 50 progressive frames in, 50 interlaced frames out.
ffmpeg -v error -f lavfi -i "testsrc2=size=720x576:rate=50" -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -t 2 -vf "interlace=scan=tff,setfield=tff,format=yuv420p" -c:v ffv1 -c:a pcm_s16le "$work/in.mkv"
[[ $(frames "$work/in.mkv") == 50 ]] || fail "test clip should have 50 frames"

# Synthetic content does not classify reliably, so the field order is given and --force used.
run=(qtgmc-6ui deint "$work/in.mkv" --preset draft --field-order tff --force)

"${run[@]}" -o "$work/master.mkv" --view mp4-h264-aac
[[ $(frames "$work/master.mkv") == 100 ]] || fail "master should have 100 frames (bob)"
view=$(find "$work" -maxdepth 1 -name '*.mp4')
[[ -n $view && $(frames "$view") == 100 ]] || fail "viewing copy should have 100 frames"

mkdir "$work/views"
"${run[@]}" --no-master --view mp4-h264-aac --out-dir "$work/views"
[[ $(find "$work/views" -mindepth 1 | wc -l) == 1 ]] || fail "--no-master must leave only the viewing copy (and no temp files)"

echo "smoke: OK"
