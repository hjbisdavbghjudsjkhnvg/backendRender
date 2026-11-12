"""
Streamed-audio backend for Roblox (async, memory-lean).

GET /tostream?url=<media url>
  - If the analyzed stream is cached -> 200 + binary stream.
  - If not cached -> kicks off analysis in the background and returns 202
    immediately with a short JSON body. Roblox retries until it gets 200.
  This keeps every request well under Roblox's ~30s HTTP limit, even when the
  first analysis of a track takes 20-30s on the slow free tier.

GET /status?url=...  -> JSON {state: ready|processing|error, ...}
GET /                -> health check
"""
import os
import re
import json
import hashlib
import subprocess
import tempfile
import threading
import wave
import numpy as np
from flask import Flask, request, Response, send_file

import analyze

app = Flask(__name__)

SR = 44100
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/audio_cache")
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "420"))
os.makedirs(CACHE_DIR, exist_ok=True)

# in-process job registry: key -> "processing" | "error:<msg>"
_jobs = {}
_jobs_lock = threading.Lock()


def _key(url):
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def _stream_path(url):
    return os.path.join(CACHE_DIR, _key(url) + ".bin")


def _wav_path(url):
    return os.path.join(CACHE_DIR, _key(url) + ".wav")


def fetch_mono_wav(url, out_path):
    tmp = tempfile.mkdtemp()
    raw = os.path.join(tmp, "in")
    got = None
    try:
        dl = subprocess.run(
            ["yt-dlp", "-f", "bestaudio/best", "--no-playlist",
             "-o", raw + ".%(ext)s", "--max-filesize", "80M", url],
            capture_output=True, text=True, timeout=120)
        if dl.returncode == 0:
            for f in os.listdir(tmp):
                if f.startswith("in."):
                    got = os.path.join(tmp, f)
                    break
    except Exception:
        got = None

    src = got if got else url
    ff = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", str(SR),
         "-t", str(MAX_SECONDS), "-f", "wav", "-acodec", "pcm_s16le", out_path],
        capture_output=True, text=True, timeout=180)
    if ff.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + ff.stderr[-300:])


def wav_to_float(path):
    with wave.open(path, "rb") as wf:
        n = wf.getnframes()
        raw = wf.readframes(n)
        sw = wf.getsampwidth()
    if sw == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sw == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    return np.frombuffer(raw, dtype="<i4").astype(np.float32) / (2 ** 31)


def _run_job(url):
    key = _key(url)
    try:
        wav = _wav_path(url)
        if not os.path.exists(wav):
            fetch_mono_wav(url, wav)
        x = wav_to_float(wav)
        blob = analyze.analyze(x)
        tmp = _stream_path(url) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, _stream_path(url))   # atomic: only appears when complete
        try:
            os.remove(wav)                   # free disk; wav no longer needed
        except OSError:
            pass
        with _jobs_lock:
            _jobs.pop(key, None)
    except Exception as e:
        with _jobs_lock:
            _jobs[key] = "error:" + str(e)[:200]


@app.route("/")
def health():
    return "audio backend ok"


@app.route("/status")
def status():
    url = request.args.get("url", "")
    if not url:
        return Response(json.dumps({"state": "error", "msg": "no url"}),
                        mimetype="application/json", status=400)
    key = _key(url)
    if os.path.exists(_stream_path(url)):
        return Response(json.dumps({"state": "ready"}), mimetype="application/json")
    with _jobs_lock:
        j = _jobs.get(key)
    if j and j.startswith("error:"):
        return Response(json.dumps({"state": "error", "msg": j[6:]}),
                        mimetype="application/json")
    if j == "processing":
        return Response(json.dumps({"state": "processing"}), mimetype="application/json")
    return Response(json.dumps({"state": "unknown"}), mimetype="application/json")


@app.route("/tostream")
def tostream():
    url = request.args.get("url", "")
    if not url or not re.match(r"^https?://", url):
        return Response("bad url", status=400)

    # ready -> serve it
    if os.path.exists(_stream_path(url)):
        return send_file(_stream_path(url), mimetype="application/octet-stream")

    key = _key(url)
    with _jobs_lock:
        state = _jobs.get(key)
        if state is None:
            _jobs[key] = "processing"
            threading.Thread(target=_run_job, args=(url,), daemon=True).start()
            state = "processing"

    if state and state.startswith("error:"):
        with _jobs_lock:
            _jobs.pop(key, None)   # let a retry start fresh
        return Response(json.dumps({"state": "error", "msg": state[6:]}),
                        mimetype="application/json", status=502)

    # processing: tell Roblox to retry shortly
    return Response(json.dumps({"state": "processing", "retry": 3}),
                    mimetype="application/json", status=202)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
