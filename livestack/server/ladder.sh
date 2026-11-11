#!/usr/bin/env bash
# FFmpeg ABR ladder: one live input -> 720p / 480p / 360p HLS + master.m3u8.
#
# Launched by MediaMTX (runOnReady) with MTX_PATH set to the stream path,
# e.g. "live/test". Output: $HLS_ROOT/<path>/master.m3u8 and <path>/<720p|480p|360p>/.
# nginx serves $HLS_ROOT at https://<ORIGIN_HOST>/hls/.
#
# Fault tolerance
#  * If FFmpeg exits while the stream is live, MediaMTX restarts it (runOnReadyRestart).
#  * If FFmpeg hangs, server/monitor.sh sees the playlist go stale and kills it.
#  * Segment numbers start from the epoch clock, so a restarted encoder never
#    reuses a segment name and players keep moving forward.
#  * temp_file: segments are written to *.tmp and renamed, so nginx/CDN never
#    serve a half-written segment.
#
# All settings can be overridden by environment variables (used by the local test).
set -Eeuo pipefail

FFMPEG_BIN=${FFMPEG_BIN:-ffmpeg}
HLS_ROOT=${HLS_ROOT:-/var/www/hls}
RUN_DIR=${RUN_DIR:-/run/livestack}
RTSP_BASE=${RTSP_BASE:-rtsp://127.0.0.1:8554}
FPS=${LADDER_FPS:-30}          # output frame rate (OBS should send 30 fps)
SEG=${HLS_TIME:-4}             # segment length in seconds
LIST=${HLS_LIST_SIZE:-6}       # segments kept in each live playlist
GOP=$((FPS * SEG))             # 120 at 30 fps / 4 s -> one keyframe per segment

stream=${1:-${MTX_PATH:-}}

# The path comes from the publisher; never let it escape $HLS_ROOT.
if [[ ! "$stream" =~ ^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$ ]]; then
  echo "ladder: refusing invalid stream path '$stream'" >&2
  exit 2
fi

out="$HLS_ROOT/$stream"
mkdir -p "$out/720p" "$out/480p" "$out/360p" "$RUN_DIR"
# Clear leftovers from an earlier run of this path (keeps files from a quick restart).
find "$out" -type f -mmin +10 -delete 2>/dev/null || true

echo "ladder: starting $stream -> $out (fps=$FPS seg=${SEG}s)" >&2

exec "$FFMPEG_BIN" -hide_banner -loglevel warning -nostats -nostdin \
  -progress "$RUN_DIR/progress-${stream//\//_}" \
  -rtsp_transport tcp -i "$RTSP_BASE/$stream" \
  -filter_complex "[0:v]fps=$FPS,format=yuv420p,split=3[s0][s1][s2];[s0]scale=-2:720[v0];[s1]scale=-2:480[v1];[s2]scale=-2:360[v2]" \
  -map "[v0]" -map 0:a:0 -map "[v1]" -map 0:a:0 -map "[v2]" -map 0:a:0 \
  -c:v libx264 -preset veryfast -tune zerolatency -profile:v main \
  -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
  -force_key_frames "expr:gte(t,n_forced*$SEG)" \
  -b:v:0 2800k -maxrate:v:0 3000k -bufsize:v:0 6000k \
  -b:v:1 1400k -maxrate:v:1 1500k -bufsize:v:1 3000k \
  -b:v:2 800k  -maxrate:v:2 856k  -bufsize:v:2 1600k \
  -c:a aac -b:a 96k -ac 2 -ar 48000 \
  -f hls -hls_time "$SEG" -hls_list_size "$LIST" -hls_delete_threshold 4 \
  -hls_flags independent_segments+delete_segments+omit_endlist+program_date_time+temp_file \
  -hls_start_number_source epoch -hls_segment_type mpegts \
  -master_pl_name master.m3u8 \
  -var_stream_map "v:0,a:0,name:720p v:1,a:1,name:480p v:2,a:2,name:360p" \
  -hls_segment_filename "$out/%v/seg_%d.ts" \
  "$out/%v/index.m3u8"
