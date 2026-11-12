"""
Streamed-audio backend for Roblox.

Endpoints:
  GET /              -> health check
  GET /tostream?url= -> download+decode to 44.1k mono, analyze, return binary
                        stream (cached per url). This is what the Roblox server
                        calls instead of exoliner's /towav.
  GET /towav?url=    -> legacy: returns raw 44.1k mono WAV (if you ever want the
                        old client to work against your own backend).

Analysis is cached on disk per URL hash so the expensive step runs once.
"""
import os
import re
import hashlib
import subprocess
import tempfile
import wave
import numpy as np
from flask import Flask, request, Response, send_file

import analyze

app = Flask(__name__)

SR = 44100
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/audio_cache")
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "420"))   # cap track length
os.makedirs(CACHE_DIR, exist_ok=True)


def _key(url, kind):
    return hashlib.sha256((kind + "|" + url).encode()).hexdigest()[:24]


def _cache_path(url, kind, ext):
    return os.path.join(CACHE_DIR, f"{_key(url, kind)}.{ext}")


def fetch_mono_wav(url, out_path):
    """Download `url` (direct media OR a yt-dlp-supported site) and transcode to
    44.1k mono 16-bit WAV at out_path. Returns (ok, message)."""
    tmp = tempfile.mkdtemp()
    raw = os.path.join(tmp, "in")

    # yt-dlp handles YouTube + hundreds of sites AND direct links.
    dl = subprocess.run(
        ["yt-dlp", "-f", "bestaudio/best", "--no-playlist",
         "-o", raw + ".%(ext)s", "--max-filesize", "80M", url],
        capture_output=True, text=True, timeout=120)
    if dl.returncode != 0:
        # fall back to treating it as a direct media URL via ffmpeg
        got = None
    else:
        got = None
        for f in os.listdir(tmp):
            if f.startswith("in."):
                got = os.path.join(tmp, f)
                break

    src = got if got else url
    ff = subprocess.run(
        ["ffmpeg", "-y", "-i", src,
         "-ac", "1", "-ar", str(SR),
         "-t", str(MAX_SECONDS),
         "-f", "wav", "-acodec", "pcm_s16le", out_path],
        capture_output=True, text=True, timeout=180)
    if ff.returncode != 0:
        return False, "ffmpeg failed: " + ff.stderr[-400:]
    return True, "ok"


def wav_to_float(path):
    with wave.open(path, "rb") as wf:
        n = wf.getnframes()
        raw = wf.readframes(n)
        sampwidth = wf.getsampwidth()
    if sampwidth == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sampwidth == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128.0
    else:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / (2 ** 31)
    return x


@app.route("/")
def health():
    return "audio backend ok"


@app.route("/tostream")
def tostream():
    url = request.args.get("url", "")
    if not url or not re.match(r"^https?://", url):
        return Response("bad url", status=400)

    cache = _cache_path(url, "stream", "bin")
    if os.path.exists(cache):
        return send_file(cache, mimetype="application/octet-stream")

    wav_path = _cache_path(url, "wav", "wav")
    if not os.path.exists(wav_path):
        ok, msg = fetch_mono_wav(url, wav_path)
        if not ok:
            return Response("fetch failed: " + msg, status=502)

    try:
        x = wav_to_float(wav_path)
        blob = analyze.analyze(x)
    except Exception as e:
        return Response("analyze failed: " + str(e), status=500)

    with open(cache, "wb") as f:
        f.write(blob)
    return send_file(cache, mimetype="application/octet-stream")


@app.route("/towav")
def towav():
    """Legacy raw-WAV path, so the pre-backend client still works if needed."""
    url = request.args.get("url", "")
    if not url or not re.match(r"^https?://", url):
        return Response("bad url", status=400)
    wav_path = _cache_path(url, "wav", "wav")
    if not os.path.exists(wav_path):
        ok, msg = fetch_mono_wav(url, wav_path)
        if not ok:
            return Response("fetch failed: " + msg, status=502)
    return send_file(wav_path, mimetype="audio/wav")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
