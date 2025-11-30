from flask import Flask, request, Response, render_template_string, redirect, url_for
import time
import requests
import m3u8
import math
from threading import Lock

app = Flask(__name__)

# Global state
state = {
    'start_time': None,
    'total_duration': 0.0,
    'files': [],  # List of {'master': m3u8.M3U8, 'variants': {bw: {'segments': [(uri, duration, is_discont), ...], 'target_dur': int}}}
    'lock': Lock(),
    'message': None
}

def parse_m3u8(url):
    resp = requests.get(url)
    master = m3u8.loads(resp.text)
    variants = {}
    for playlist in master.playlists:
        variant_url = requests.compat.urljoin(url, playlist.uri)
        media = m3u8.load(variant_url)
        segments = [(requests.compat.urljoin(variant_url, seg.uri), seg.duration, False) for seg in media.segments]
        max_dur = max(seg.duration for seg in media.segments) if media.segments else 10.0
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
        state['message'] = None  # Clear after display

    if request.method == 'POST':
        urls = request.form.getlist('urls')
        urls = [url for url in urls if url]  # Filter empty
        with state['lock']:
            if 'start' in request.form:
                if state['start_time'] is not None:
                    state['message'] = "Channel already running."
                else:
                    try:
                        state['files'] = []
                        state['total_duration'] = 0.0
                        bws = None
                        for i, url in enumerate(urls):
                            master, variants = parse_m3u8(url)
                            if bws is None:
                                bws = set(variants.keys())
                            elif bws != set(variants.keys()):
                                raise ValueError("Inconsistent variants across files")
                            if i > 0:
                                for data in variants.values():
                                    if data['segments']:
                                        uri, dur, _ = data['segments'][0]
                                        data['segments'][0] = (uri, dur, True)
                            state['files'].append((master, variants))
                            file_dur = sum(seg[1] for seg in next(iter(variants.values()))['segments']) if variants else 0.0
                            state['total_duration'] += file_dur
                        if state['total_duration'] == 0.0:
                            raise ValueError("No valid segments found")
                        state['start_time'] = time.time()
                        state['message'] = "Channel started. Master URL: /master.m3u8"
                    except Exception as e:
                        state['message'] = f"Error: {str(e)}"
            elif 'stop' in request.form:
                state['start_time'] = None
                state['files'] = []
                state['total_duration'] = 0.0
                state['message'] = "Channel stopped."
        return redirect(url_for('index'))

    template = '''
        {% if message %}<p>{{ message }}</p>{% endif %}
        <form method="post">
            {% for i in range(10) %}
                <input type="text" name="urls" placeholder="HLS m3u8 URL {{ i+1 }}"><br>
            {% endfor %}
            <button type="submit" name="start">Start Channel</button>
            <button type="submit" name="stop">Stop Channel</button>
        </form>
        {% if running %}
            <video id="player" width="640" height="360" controls autoplay></video>
            <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
            <script>
                if (Hls.isSupported()) {
                    var hls = new Hls();
                    hls.loadSource('/master.m3u8');
                    hls.attachMedia(document.getElementById('player'));
                } else if (document.getElementById('player').canPlayType('application/vnd.apple.mpegurl')) {
                    document.getElementById('player').src = '/master.m3u8';
                }
            </script>
        {% endif %}
    '''
    return render_template_string(template, running=running, message=message)

@app.route('/master.m3u8')
def master():
    with state['lock']:
        if not state['files']:
            return "No channel running.", 404
        master = state['files'][0][0]
        output = '#EXTM3U\n#EXT-X-VERSION:3\n'
        for pl in master.playlists:
            output += f'#EXT-X-STREAM-INF:BANDWIDTH={pl.stream_info.bandwidth}'
            if pl.stream_info.resolution:
                output += f',RESOLUTION={pl.stream_info.resolution}'
            output += '\n'
            output += f'variant_{pl.stream_info.bandwidth}.m3u8\n'
        return Response(output, mimetype='application/x-mpegURL')

@app.route('/variant_<bw>.m3u8')
def variant(bw):
    bw = int(bw)
    window_size = 6
    with state['lock']:
        if state['start_time'] is None:
            return "No channel running.", 404
        all_segments = []
        for _, variants in state['files']:
            if bw not in variants:
                return "Variant not found.", 404
            all_segments.extend(variants[bw]['segments'])
        if not all_segments:
            return "No segments.", 404
        target_dur = max(math.ceil(s[1]) for s in all_segments)
        seg_per_cycle = len(all_segments)
        elapsed = time.time() - state['start_time']
        loops = int(elapsed / state['total_duration'])
        current_time = elapsed % state['total_duration']
        offset = 0.0
        start_idx = 0
        for i, (_, dur, _) in enumerate(all_segments):
            if offset <= current_time < offset + dur:
                start_idx = i
                break
            offset += dur
        media_sequence = loops * seg_per_cycle + start_idx
        window = all_segments[start_idx:start_idx + window_size]
        wrapped = False
        if len(window) < window_size:
            wrapped = True
            additional = all_segments[0:window_size - len(window)]
            window += additional
        wrap_index = len(all_segments) - start_idx if wrapped else -1
        output = f'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:{target_dur}\n#EXT-X-MEDIA-SEQUENCE:{media_sequence}\n#EXT-X-PLAYLIST-TYPE:LIVE\n'
        for i, (uri, dur, disc) in enumerate(window):
            if i == wrap_index:
                output += '#EXT-X-DISCONTINUITY\n'
            if disc:
                output += '#EXT-X-DISCONTINUITY\n'
            output += f'#EXTINF:{dur:.6f},\n{uri}\n'
        return Response(output, mimetype='application/x-mpegURL')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
