# app.py - HLS looping channel with Option A (EXT-X-CUE-OUT / CUE-IN) ad slots between contents
from flask import Flask, request, Response, render_template_string, redirect, url_for
import time
import requests
import m3u8
import math
import logging
from threading import Lock

# ------ CONFIG ------
REQUEST_TIMEOUT = 10        # seconds when fetching upstream manifests
WINDOW_SEGMENTS = 6        # number of media segments to expose in playlist window
AD_DURATION = 30.0         # seconds per ad slot between contents (Option A)
# --------------------

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)

state = {
    'started_at': None,
    'total_duration': 0.0,
    'files': [],   # list of tuples: (master_m3u8_obj, variants_dict_per_bandwidth)
    'lock': Lock(),
    'message': None
}

def safe_headers():
    return {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "Access-Control-Allow-Origin": "*"
    }

def fetch_text(url):
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

def parse_m3u8(url):
    """
    Parse a master playlist URL and return (master_obj, variants)
    variants: { bandwidth: { 'segments': [(uri, duration, is_discontinuity), ...], 'target_dur': int } }
    """
    logging.info("Fetching master: %s", url)
    txt = fetch_text(url)
    master = m3u8.loads(txt)

    variants = {}
    for pl in master.playlists or []:
        variant_url = requests.compat.urljoin(url, pl.uri)
        logging.info("  loading variant: %s", variant_url)
        media = m3u8.load(variant_url)  # can raise
        segs = []
        for s in media.segments or []:
            seg_uri = requests.compat.urljoin(variant_url, s.uri)
            segs.append((seg_uri, float(s.duration), False))
        max_dur = max((seg.duration for seg in (media.segments or [])), default=10.0)
        bw = int(pl.stream_info.bandwidth) if pl.stream_info and pl.stream_info.bandwidth else 0
        variants[bw] = {
            'segments': segs,
            'target_dur': int(math.ceil(max_dur))
        }
    return master, variants

@app.route('/', methods=['GET', 'POST'])
def index():
    with state['lock']:
        running = state['started_at'] is not None
        msg = state['message']
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
                                raise ValueError("Inconsistent variants across sources (bandwidth sets differ)")

                            # If concatenating multiple files, mark discontinuity for safety
                            if i > 0:
                                for v in variants.values():
                                    if v['segments']:
                                        uri, dur, _ = v['segments'][0]
                                        v['segments'][0] = (uri, dur, True)

                            state['files'].append((master, variants))

                            # add onto total_duration using first variant to determine durations
                            file_dur = sum(seg[1] for seg in next(iter(variants.values()))['segments'])
                            state['total_duration'] += file_dur

                        if state['total_duration'] <= 0:
                            raise ValueError("Total loop duration computed as zero")

                        state['started_at'] = time.time()
                        state['message'] = "Channel started. Master available at /master.m3u8"
                        logging.info("Channel started_at=%s total_duration=%.3f", state['started_at'], state['total_duration'])

                    except Exception as e:
                        logging.exception("Error starting channel")
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
    {% if msg %}<div style="margin-bottom:12px;color:green"><strong>{{ msg }}</strong></div>{% endif %}
    <form method="post">
      {% for i in range(8) %}
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
          const hls = new Hls({liveSyncDuration:10, liveMaxLatencyDuration:30});
          hls.loadSource('/master.m3u8');
          hls.attachMedia(v);
        } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
          v.src = '/master.m3u8';
        }
      </script>
    {% endif %}
    </body></html>
    '''
    return render_template_string(template, running=running, msg=msg)

@app.route('/master.m3u8')
def master():
    with state['lock']:
        if not state['files']:
            return "No channel running", 404

        master_obj = state['files'][0][0]
        out = '#EXTM3U\n#EXT-X-VERSION:3\n'

        for pl in master_obj.playlists or []:
            bw = int(pl.stream_info.bandwidth) if pl.stream_info and pl.stream_info.bandwidth else 0
            out += f'#EXT-X-STREAM-INF:BANDWIDTH={bw}'
            if pl.stream_info and pl.stream_info.resolution:
                out += f',RESOLUTION={pl.stream_info.resolution}'
            out += '\n'
            out += f'variant_{bw}.m3u8\n'

        return Response(out, mimetype='application/x-mpegURL', headers=safe_headers())

@app.route('/variant_<bw>.m3u8')
def variant(bw):
    """
    Build a variant manifest with Option A cue markers inserted between each source.
    The manifest exposes a rolling window of segments (WINDOW_SEGMENTS). Media sequence counts only media segments.
    """
    try:
        bw = int(bw)
    except ValueError:
        return "Bad bandwidth", 400

    with state['lock']:
        if not state['started_at'] or not state['files']:
            return "Not running", 404

        # Build a full_list (interleaving cue markers and media segments).
        # full_list entries: {'type':'seg','uri':..., 'dur':..., 'disc': bool} or {'type':'cue','duration': AD_DURATION}
        full_list = []
        # Also maintain per-loop seg-only list to compute durations and media-sequence logic
        seg_only_list = []

        # iterate over files and build full_list inserting cue slots between files
        files = state['files']
        for idx, (_, variants) in enumerate(files):
            if bw not in variants:
                return "Variant not found", 404
            segs = list(variants[bw]['segments'])  # list of (uri,dur,disc)
            if idx == 0:
                # first file: append all segments as normal
                for (uri, dur, disc) in segs:
                    full_list.append({'type': 'seg', 'uri': uri, 'dur': dur, 'disc': disc})
                    seg_only_list.append({'uri': uri, 'dur': dur, 'disc': disc})
            else:
                # boundary before this file — insert an ad cue slot
                full_list.append({'type': 'cue', 'duration': AD_DURATION})

                # consume enough initial segments from this file to act as ad placeholder (>= AD_DURATION)
                needed = AD_DURATION
                consume_n = 0
                while needed > 0 and consume_n < len(segs):
                    needed -= segs[consume_n][1]
                    consume_n += 1

                # if consume_n == 0 (rare, empty file), just continue without consuming
                # Insert the consumed segments as placeholder (they will play if no SSAI replaces them)
                for j in range(consume_n):
                    uri, dur, disc = segs[j]
                    full_list.append({'type': 'seg', 'uri': uri, 'dur': dur, 'disc': disc})
                    seg_only_list.append({'uri': uri, 'dur': dur, 'disc': disc})

                # mark end of cue with EXT-X-CUE-IN tag after the placeholder segments
                full_list.append({'type': 'cue-end', 'duration': AD_DURATION})

                # append the rest of the segments from this file (starting after consume_n)
                for j in range(consume_n, len(segs)):
                    uri, dur, disc = segs[j]
                    full_list.append({'type': 'seg', 'uri': uri, 'dur': dur, 'disc': disc})
                    seg_only_list.append({'uri': uri, 'dur': dur, 'disc': disc})

        if not seg_only_list:
            return "No segments available", 404

        # Compute durations and locate current position
        total_loop_duration = sum(s['dur'] for s in seg_only_list)
        if total_loop_duration <= 0:
            return "Invalid durations", 500

        now = time.time()
        elapsed = now - state['started_at']
        current_time_in_loop = elapsed % total_loop_duration

        # find start media-segment index in seg_only_list
        offset = 0.0
        start_seg_index = 0
        for i, s in enumerate(seg_only_list):
            if offset <= current_time_in_loop < offset + s['dur']:
                start_seg_index = i
                break
            offset += s['dur']

        # produce mapping from seg-only indices to full_list indices
        seg_to_full_indices = []
        for i, item in enumerate(full_list):
            if item['type'] == 'seg':
                seg_to_full_indices.append(i)

        if len(seg_to_full_indices) != len(seg_only_list):
            # Sanity check: counts must match
            logging.error("Segment mapping mismatch: full_list seg count != seg_only_list count")
            return "Internal manifest error", 500

        # Starting full_list index corresponding to start_seg_index
        start_full_index = seg_to_full_indices[start_seg_index]

        # Build window: collect full_list entries starting at start_full_index,
        # continue until we've included WINDOW_SEGMENTS media segments (counting seg items)
        window = []
        segs_collected = 0
        idx_full = start_full_index
        full_len = len(full_list)
        while segs_collected < WINDOW_SEGMENTS:
            item = full_list[idx_full]
            window.append(item)
            if item['type'] == 'seg':
                segs_collected += 1
            idx_full = (idx_full + 1) % full_len

        # compute media sequence as number of media segments before the start segment
        loops_completed = int(elapsed / total_loop_duration)
        media_sequence = loops_completed * len(seg_only_list) + start_seg_index

        target_dur = int(math.ceil(max(s['dur'] for s in seg_only_list)))

        # Build playlist text
        out = '#EXTM3U\n'
        out += '#EXT-X-VERSION:3\n'
        out += '#EXT-X-INDEPENDENT-SEGMENTS\n'
        out += '#EXT-X-ALLOW-CACHE:NO\n'
        out += f'#EXT-X-TARGETDURATION:{target_dur}\n'
        out += f'#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n'
        out += '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'

        # Emit lines for window: cue tags and media segments
        for item in window:
            if item['type'] == 'cue':
                # start ad break
                out += f'#EXT-X-CUE-OUT:DURATION={int(item["duration"])}\n'
            elif item['type'] == 'cue-end':
                # end ad break
                out += '#EXT-X-CUE-IN\n'
            elif item['type'] == 'seg':
                # if the segment was flagged as discontinuity during parse, include EXT-X-DISCONTINUITY
                if item.get('disc'):
                    out += '#EXT-X-DISCONTINUITY\n'
                out += f'#EXTINF:{item["dur"]:.6f},\n{item["uri"]}\n'

        return Response(out, mimetype='application/x-mpegURL', headers=safe_headers())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
