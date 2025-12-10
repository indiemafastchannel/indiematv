# app.py - Broadcast-grade looping HLS channel (H.264/AAC segments)
from flask import Flask, request, Response, render_template_string, redirect, url_for
import time
import requests
import m3u8
import math
import logging
from threading import Lock

# --- Configuration ---
REQUEST_TIMEOUT = 10  # seconds for upstream playlist fetch
WINDOW_SIZE = 6       # number of segments in variant playlists (rolling window)
# ----------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)

state = {
    'started_at': None,        # wallclock epoch when channel started (float)
    'total_duration': 0.0,    # sum of durations for one full loop
    'files': [],              # list of (master_m3u8_object, variants_dict)
    'lock': Lock(),
    'message': None
}

def fetch_text(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text

def parse_m3u8(url):
    """
    Fetch and parse a master playlist URL, return (master_obj, variants)
    variants: {bandwidth: {'segments': [(abs_uri, duration, is_discontinuity), ...], 'target_dur': int}}
    """
    logging.info("Parsing master: %s", url)
    txt = fetch_text(url)
    master = m3u8.loads(txt)

    variants = {}
    for playlist in master.playlists or []:
        variant_uri = requests.compat.urljoin(url, playlist.uri)
        logging.info("Loading variant: %s", variant_uri)
        media = m3u8.load(variant_uri)
        segs = []
        for seg in media.segments or []:
            seg_uri = requests.compat.urljoin(variant_uri, seg.uri)
            segs.append((seg_uri, float(seg.duration), False))
        max_dur = max((s.duration for s in (media.segments or [])), default=10.0)
        bandwidth = int(playlist.stream_info.bandwidth) if playlist.stream_info and playlist.stream_info.bandwidth else 0
        variants[bandwidth] = {
            'segments': segs,
            'target_dur': int(math.ceil(max_dur))
        }
    return master, variants

def safe_headers():
    return {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "Access-Control-Allow-Origin": "*"
    }

@app.route('/', methods=['GET', 'POST'])
def index():
    with state['lock']:
        running = state['started_at'] is not None
        message = state['message']
        state['message'] = None

    if request.method == 'POST':
        urls = request.form.getlist('urls')
        urls = [u.strip() for u in urls if u and u.strip()]

        with state['lock']:
            if 'start' in request.form:
                if state['started_at'] is not None:
                    state['message'] = "Channel already running."
                else:
                    try:
                        logging.info("Starting channel with %d sources", len(urls))
                        state['files'] = []
                        state['total_duration'] = 0.0
                        bws = None

                        for i, url in enumerate(urls):
                            master, variants = parse_m3u8(url)
                            if not variants:
                                raise ValueError(f"No variants found for {url}")

                            if bws is None:
                                bws = set(variants.keys())
                            elif bws != set(variants.keys()):
                                raise ValueError("Inconsistent variant sets across sources")

                            # If concatenating multiple files, mark first segment of subsequent files as discontinuity
                            if i > 0:
                                for v in variants.values():
                                    if v['segments']:
                                        uri, dur, _ = v['segments'][0]
                                        v['segments'][0] = (uri, dur, True)

                            state['files'].append((master, variants))

                            # duration of file = duration of first variant (they all match by bandwidth set)
                            file_dur = sum(seg[1] for seg in next(iter(variants.values()))['segments'])
                            state['total_duration'] += file_dur

                        if state['total_duration'] <= 0:
                            raise ValueError("Total loop duration is zero")

                        # record wallclock start reference
                        state['started_at'] = time.time()
                        state['message'] = "Channel started at /master.m3u8"
                        logging.info("Channel started_at=%s total_duration=%.3f", state['started_at'], state['total_duration'])

                    except Exception as e:
                        logging.exception("Failed to start channel")
                        state['message'] = f"Error starting channel: {e}"

            elif 'stop' in request.form:
                state['started_at'] = None
                state['files'] = []
                state['total_duration'] = 0.0
                state['message'] = "Channel stopped."
                logging.info("Channel stopped by user")

        return redirect(url_for('index'))

    template = '''
    <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>IndieMA TV - Admin</title></head><body style="font-family:Arial;padding:16px">
    {% if message %}<div style="margin-bottom:12px;color:green"><strong>{{ message }}</strong></div>{% endif %}
    <form method="post">
      {% for i in range(6) %}
        <input style="width:98%;margin:4px 0;padding:8px" type="text" name="urls" placeholder="HLS master m3u8 URL {{ i+1 }}">
      {% endfor %}
      <div style="margin-top:8px">
        <button type="submit" name="start">Start Channel</button>
        <button type="submit" name="stop">Stop Channel</button>
      </div>
    </form>

    {% if running %}
      <h3 style="margin-top:18px">Live preview</h3>
      <video id="player" width="640" height="360" controls autoplay muted></video>
      <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
      <script>
        const v = document.getElementById('player');
        if (Hls.isSupported()) {
          const hls = new Hls({liveSyncDuration:10,liveMaxLatencyDuration:30});
          hls.loadSource('/master.m3u8');
          hls.attachMedia(v);
        } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
          v.src = '/master.m3u8';
        }
      </script>
    {% endif %}
    </body></html>
    '''
    return render_template_string(template, running=running, message=message)

@app.route('/master.m3u8')
def master():
    with state['lock']:
        if not state['files']:
            return "No channel running", 404

        master_obj = state['files'][0][0]
        out = '#EXTM3U\n#EXT-X-VERSION:3\n'
        # Produce a clean master manifest listing our variant endpoints
        for pl in master_obj.playlists:
            bw = int(pl.stream_info.bandwidth) if pl.stream_info and pl.stream_info.bandwidth else 0
            out += f'#EXT-X-STREAM-INF:BANDWIDTH={bw}'
            if pl.stream_info and pl.stream_info.resolution:
                out += f',RESOLUTION={pl.stream_info.resolution}'
            out += '\n'
            out += f'variant_{bw}.m3u8\n'

        return Response(out, mimetype='application/x-mpegURL', headers=safe_headers())

@app.route('/variant_<bw>.m3u8')
def variant(bw):
    try:
        bw = int(bw)
    except ValueError:
        return "Bad bandwidth", 400

    with state['lock']:
        if not state['started_at'] or not state['files']:
            return "Not running", 404

        # Gather all segments across concatenated files for the requested bandwidth
        all_segments = []
        for _, variants in state['files']:
            if bw not in variants:
                return "Variant not available", 404
            all_segments.extend(variants[bw]['segments'])

        if not all_segments:
            return "No segments", 404

        # total duration of one loop
        total_duration = sum(s[1] for s in all_segments)
        if total_duration <= 0:
            return "Invalid total duration", 500

        now = time.time()
        elapsed = now - state['started_at']   # seconds since channel start
        current_time_in_loop = elapsed % total_duration

        # Find the segment index where current_time_in_loop lands
        offset = 0.0
        start_idx = 0
        for i, (_, dur, _) in enumerate(all_segments):
            if offset <= current_time_in_loop < offset + dur:
                start_idx = i
                break
            offset += dur

        # Build a rolling window of segments (wrap around)
        window = []
        for i in range(WINDOW_SIZE):
            idx = (start_idx + i) % len(all_segments)
            window.append(all_segments[idx])

        # Compute media sequence: number of segments played so far (full loops * segments_per_loop + start_idx)
        loops = int(elapsed / total_duration)
        media_sequence = loops * len(all_segments) + start_idx

        # target duration must be integer >= max segment duration
        target_dur = int(math.ceil(max(s[1] for s in all_segments)))

        # Compute wallclock time for the start segment occurrence:
        # base of the current loop (wallclock) = started_at + (elapsed - current_time_in_loop)
        loop_base_wallclock = state['started_at'] + (elapsed - current_time_in_loop)
        # offset is the sum durations before start_idx
        seg_offset = sum(all_segments[i][1] for i in range(start_idx)) if start_idx > 0 else 0.0
        start_segment_wallclock = loop_base_wallclock + seg_offset

        # Build playlist
        out = '#EXTM3U\n'
        out += '#EXT-X-VERSION:3\n'
        out += '#EXT-X-INDEPENDENT-SEGMENTS\n'
        out += '#EXT-X-ALLOW-CACHE:NO\n'
        # Server control (helps live linear behavior)
        out += '#EXT-X-SERVER-CONTROL:CAN-SKIP-UNTIL=0.0\n'
        out += f'#EXT-X-TARGETDURATION:{target_dur}\n'
        out += f'#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n'
        out += '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'

        # Emit PROGRAM-DATE-TIME for each segment in the window using start_segment_wallclock + cumulative offsets
        cumulative = 0.0
        for uri, dur, disc in window:
            seg_wall_time = start_segment_wallclock + cumulative
            # Format as UTC ISO8601 (Z)
            ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seg_wall_time))
            out += f'#EXT-X-PROGRAM-DATE-TIME:{ts}\n'
            out += f'#EXTINF:{dur:.6f},\n{uri}\n'
            cumulative += dur

        return Response(out, mimetype='application/x-mpegURL', headers=safe_headers())

if __name__ == '__main__':
    # Listen on all interfaces so Bunny Magic Container / Docker can route.
    app.run(host='0.0.0.0', port=5000, threaded=True)
