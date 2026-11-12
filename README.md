# Roblox streamed-audio backend

Replaces exoliner. Downloads/decodes audio to **44.1 kHz mono**, runs offline
sinusoidal + noise-band analysis, and returns a compact binary stream the Roblox
client plays back. Analysis is cached per URL so the heavy work runs once.

## Endpoints
- `GET /` — health check
- `GET /tostream?url=<media or youtube url>` — the one the Roblox server calls.
  Returns the binary analysis stream (see `STREAM_FORMAT.md`).
- `GET /towav?url=...` — legacy raw 44.1k mono WAV (for the old client).

## Deploy to Render (free)

1. Put this `backend/` folder in a GitHub repo (its own repo, or a subfolder).
2. On https://render.com → **New → Web Service** → connect the repo.
3. Render auto-detects the **Dockerfile**. Settings:
   - Runtime: **Docker**
   - Instance type: **Free**
   - Health check path: `/`
   (If you committed `render.yaml`, use **New → Blueprint** instead and it
   fills these in.)
4. Deploy. First build takes a few minutes (installs ffmpeg).
5. You get a URL like `https://roblox-audio-backend.onrender.com`.
   That URL is what you pass as `api` in the Roblox module.

### Keep it awake (important)
Render's free tier **sleeps after 15 min idle**, so the first play after idle
eats a ~30–50s cold start. Fix: create a free cron at https://cron-job.org that
GETs `https://your-app.onrender.com/` every 10 minutes.

### Free-tier limits to expect
- ~0.5 CPU: analyzing a long track the first time can take 15–40s. It's cached
  after, so only the first play of each song is slow.
- Disk cache lives in `/tmp` and is wiped on redeploy/restart — songs just
  re-analyze on next request. Fine for this use.
- `MAX_SECONDS` (default 420) caps track length so one request can't run forever.

### YouTube caveat
`yt-dlp` works locally but **datacenter IPs (incl. Render) are often blocked or
rate-limited by YouTube.** Direct media URLs (`.mp3`, `.wav`, `.ogg`...) are far
more reliable. If YouTube fails with 403/429, that's Google, not this code.

## Local test
```
pip install -r requirements.txt   # + ffmpeg installed on your machine
python app.py
curl "http://localhost:8000/tostream?url=https://example.com/song.mp3" -o out.bin
```

## Files
- `app.py` — web server + download/transcode
- `analyze.py` — the offline analysis (edit tunables at the top)
- `STREAM_FORMAT.md` — the binary format the client decodes
- `Dockerfile`, `render.yaml`, `requirements.txt` — deploy
