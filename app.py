from flask import Flask, request, Response, render_template_string, redirect, url_for
import time
import requests
import m3u8
import math
from threading import Lock

app = Flask(__name__)

state = {
    'start_time': None,
    'total_duration': 0.0,
    'files': [],
    'lock': Lock(),
    'message': None
}

def parse_m3u8(url):
    resp = requests.get(url, timeout=10)
    master = m3u8.loads(resp.text)

    variants = {}
    for playlist in master.playlists:
        variant_url = requests.compat.urljoin(url, playlist.uri)
        media = m3u8.load(variant_url)

        segments = [
            (requests.compat.urljoin(variant_url, seg.uri), seg.duration, False)
            for seg in media.segments
        ]

        max_dur = max([s.duration for s in media.segments]) if media.segments else 10.0

        variants[playlist.stream_info.bandwidth] = {
            'segments': segments,
            'target_dur': math.ceil(max_dur)
        }

    return master, variants

@app.route('/', methods=['GET', 'POST'])
def index():
    with state['lock']:
        running = state['start_time'] is not None
        message = state['message']
        state['message'] = None

    if request.method == 'POST':
        urls = request.form.getlist('urls')
        urls = [u for u in urls if u]

        with state['lock']:
            if 'start' in request.form:
                try:
                    state['files'] = []
                    state['total_duration'] = 0.0
                    bws = None

                    for i, url in enumerate(urls):
                        master, variants = parse_m3u8(url)
                        if bws is None:
                            bws = set(variants.keys())
                        elif bws != set(variants.keys()):
                            raise ValueError("Variant mismatch between sources")

                        if i > 0:
                            for v in variants.values():
                                if v['segments']:
                                    uri, dur, _ = v['segments'][0]
                                    v['segments'][0] = (uri, dur, True)

                        state['files'].append((master, variants))

                        file_dur = sum(
                            s[1] for s in next(iter(variants.values()))['segments']
                        )
                        state['total_duration'] += file_dur

                    if state['total_duration'] == 0:
                        raise ValueError("No valid segments found")

                    state['start_time'] = time.time()
                    state['message'] = "Channel running at /master.m3u8"

                except Exception as e:
                    state['message'] = f"Error: {str(e)}"

            elif 'stop' in request.form:
                state['start_time'] = None
                state['files'] = []
                state['total_duration'] = 0
                state['message'] = "Channel stopped"

        return redirect(url_for('index'))

    return render_template_string('''
        {% if message %}<p>{{ message }}</p>{% endif %}
        <form method="post">
            {% for i in range(10) %}
                <input type="text" name="urls" placeholder="HLS URL {{ i+1 }}"><br>
            {% endfor %}
            <button type="submit" name="start">Start Channel</button>
            <button type="submit" name="stop">Stop Channel</button>
        </form>

        {% if running %}
        <video id="v" width="640" height="360" controls autoplay></video>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <script>
        var v=document.getElementById('v');
        if(Hls.isSupported()){
            var hls=new Hls({liveSyncDuration:10,liveMaxLatencyDuration:30});
            hls.loadSource('/master.m3u8');
            hls.attachMedia(v);
        } else if(v.canPlayType('application/vnd.apple.mpegurl')){
            v.src='/master.m3u8';
        }
        </script>
        {% endif %}
    ''', running=running, message=message)

@app.route('/master.m3u8')
def master():
    with state['lock']:
        if not state['files']:
            return "Not running", 404

        master = state['files'][0][0]
        out = '#EXTM3U\n#EXT-X-VERSION:3\n'

        for pl in master.playlists:
            out += f'#EXT-X-STREAM-INF:BANDWIDTH={pl.stream_info.bandwidth}'
            if pl.stream_info.resolution:
                out += f',RESOLUTION={pl.stream_info.resolution}'
            out += '\n'
            out += f'variant_{pl.stream_info.bandwidth}.m3u8\n'

        return Response(out, mimetype='application/x-mpegURL', headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Access-Control-Allow-Origin': '*'
        })

@app.route('/variant_<bw>.m3u8')
def variant(bw):
    bw = int(bw)
    window_size = 6

    with state['lock']:
        if not state['start_time']:
            return "Not running", 404

        all_segments = []
        for _, variants in state['files']:
            if bw not in variants:
                return "Variant not found", 404
            all_segments.extend(variants[bw]['segments'])

        if not all_segments:
            return "Empty stream", 404

        total_duration = sum(s[1] for s in all_segments)
        elapsed = time.time() - state['start_time']
        current_time = elapsed % total_duration

        offset = 0.0
        start_idx = 0
        for i, (_, dur, _) in enumerate(all_segments):
            if offset <= current_time < offset + dur:
                start_idx = i
                break
            offset += dur

        window = []
        for i in range(window_size):
            window.append(all_segments[(start_idx + i) % len(all_segments)])

        loops = int(elapsed / total_duration)
        media_sequence = loops * len(all_segments) + start_idx

        target_dur = max(math.ceil(s[1]) for s in all_segments)

        out = '#EXTM3U\n'
        out += '#EXT-X-VERSION:3\n'
        out += '#EXT-X-INDEPENDENT-SEGMENTS\n'
        out += '#EXT-X-ALLOW-CACHE:NO\n'
        out += '#EXT-X-SERVER-CONTROL:CAN-SKIP-UNTIL=0.0\n'
        out += f'#EXT-X-TARGETDURATION:{target_dur}\n'
        out += f'#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n'
        out += '#EXT-X-DISCONTINUITY-SEQUENCE:0\n'

        now = time.time()
        running_time = 0.0

        for uri, dur, _ in window:
            ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + running_time))
            out += f'#EXT-X-PROGRAM-DATE-TIME:{ts}\n'
            out += f'#EXTINF:{dur:.6f},\n{uri}\n'
            running_time += dur

        return Response(out, mimetype='application/x-mpegURL', headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Access-Control-Allow-Origin': '*'
        })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
