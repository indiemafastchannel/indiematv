from flask import Flask, request, jsonify
import os
import signal
import subprocess

app = Flask(__name__, static_folder='.')

FFMPEG_PID_FILE = "/app/ffmpeg.pid"
INPUTS_FILE = "/app/inputs.txt"
OUTPUT_DIR = "/app/output"

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/api/start', methods=['POST'])
def start():
    data = request.get_json()
    links = data.get('links', [])
    if not links:
        return jsonify({"message": "No links provided"}), 400

    with open(INPUTS_FILE, 'w') as f:
        for link in links:
            f.write(link.strip() + '\n')

    stop_ffmpeg()

    cmd = ["/bin/sh", "/app/ffmpeg.sh"]
    proc = subprocess.Popen(cmd, cwd="/app")
    with open(FFMPEG_PID_FILE, 'w') as f:
        f.write(str(proc.pid))

    if os.path.exists(OUTPUT_DIR):
        for file in os.listdir(OUTPUT_DIR):
            os.remove(os.path.join(OUTPUT_DIR, file))

    return jsonify({"message": f"Channel started with {len(links)} clips. Use /master.m3u8"})

@app.route('/api/stop', methods=['POST'])
def stop():
    stopped = stop_ffmpeg()
    msg = "Channel stopped." if stopped else "No channel was running."
    return jsonify({"message": msg})

def stop_ffmpeg():
    if os.path.exists(FFMPEG_PID_FILE):
        with open(FFMPEG_PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            os.remove(FFMPEG_PID_FILE)
            return True
        except:
            pass
    return False

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
