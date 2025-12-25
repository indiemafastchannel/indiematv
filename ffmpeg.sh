#!/bin/sh

set -e

INPUTS="/app/inputs.txt"
OUT="/app/output"

mkdir -p "$OUT"
rm -f "$OUT"/*.m3u8 "$OUT"/*.ts

if [ ! -f "$INPUTS" ]; then
  echo "inputs.txt not found"
  exit 1
fi

INPUT_OPTIONS=""
FILTER_INPUTS=""
INDEX=0

while read -r URL; do
  if [ -n "$URL" ]; then
    INPUT_OPTIONS="$INPUT_OPTIONS -i $URL"
    FILTER_INPUTS="$FILTER_INPUTS[$INDEX:v][$INDEX:a]"
    INDEX=$((INDEX + 1))
  fi
done < "$INPUTS"

if [ "$INDEX" -eq 0 ]; then
  echo "No input URLs found"
  exit 1
fi

FILTER_COMPLEX="$FILTER_INPUTS concat=n=$INDEX:v=1:a=1[concatv][concata]; \
[concatv]split=3[v1080][v720][v640]; \
[v1080]scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2[v1080out]; \
[v720]scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,pad=1280:720:(ow-iw)/2:(oh-ih)/2[v720out]; \
[v640]scale=640:360:force_original_aspect_ratio=decrease:flags=lanczos,pad=640:360:(ow-iw)/2:(oh-ih)/2[v640out]"

ffmpeg \
$INPUT_OPTIONS \
-filter_complex "$FILTER_COMPLEX" \
-map "[v1080out]" -map "[concata]" \
-map "[v720out]" -map "[concata]" \
-map "[v640out]" -map "[concata]" \
-stream_loop -1 \
-c:v libx264 -preset veryfast -profile:v main -level 4.0 -pix_fmt yuv420p \
-g 40 -keyint_min 40 -sc_threshold 0 \
-force_key_frames "expr:gte(t,n_forced*2)" \
-b:v:0 5000k -maxrate:v:0 5000k -bufsize:v:0 10000k \
-b:v:1 2800k -maxrate:v:1 2800k -bufsize:v:1 5600k \
-b:v:2 800k -maxrate:v:2 800k -bufsize:v:2 1600k \
-c:a aac -ar 48000 -b:a 128k \
-f hls \
-hls_time 4 \
-hls_playlist_type event \
-hls_flags independent_segments+delete_segments+program_date_time+omit_endlist \
-hls_segment_type mpegts \
-hls_segment_filename "$OUT/stream_%v_segment_%05d.ts" \
-var_stream_map "v:0,a:0,name:1080p v:1,a:0,name:720p v:2,a:0,name:360p" \
-master_pl_name master.m3u8 \
"$OUT/stream_%v.m3u8"
