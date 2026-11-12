"""
Offline sinusoidal + noise-band analysis (memory-lean, single-pass).

Produces the binary stream in STREAM_FORMAT.md. Designed to stay well under a
512 MB free-tier limit even for multi-minute tracks: the audio is float32, and
frames are quantized and written out as we go rather than all held in memory.
"""
import numpy as np
import struct

SR          = 44100
FRAME_RATE  = 100
WINDOW      = 4096
OSC_COUNT   = 224
TOL_FRAC    = 0.012
PEAK_FLOOR  = -90
NOISE_EDGES = [0, 250, 500, 1000, 2000, 4000, 8000, 16000, SR // 2]

MAGIC   = 0x52535631
VERSION = 1

# Fixed amplitude reference. Input is peak-normalized to 1.0, so a single
# full-scale sinusoid has amp ~1.0 after /winScale. We headroom x4 so quantizing
# to u16 never clips, and carry the real loudness in ampScale. One pass, no need
# to scan all frames for a global max.
AMP_REF = 4.0


def _seg_into(dst, x, start, n):
    dst[:] = 0.0
    a, b = max(start, 0), min(start + n, len(x))
    if b > a:
        dst[a - start:b - start] = x[a:b]


def analyze(x):
    x = np.asarray(x, dtype=np.float32)
    peak = float(np.max(np.abs(x))) if len(x) else 1.0
    if peak > 0:
        x = x / peak

    hop = SR // FRAME_RATE
    N = WINDOW
    w = np.hanning(N).astype(np.float32)
    ws = float(w.sum()) / 2
    nfr = max(len(x) // hop, 1)
    nb = len(NOISE_EDGES) - 1

    fb = np.fft.rfftfreq(N, 1 / SR)
    band_bins = [np.where((fb >= NOISE_EDGES[b]) & (fb < NOISE_EDGES[b + 1]))[0]
                 for b in range(nb)]

    K = OSC_COUNT
    slot_freq = np.zeros(K, dtype=np.float64)
    slot_live = np.zeros(K, dtype=bool)

    # header first
    final_amp_scale = AMP_REF * peak
    out = bytearray()
    out += struct.pack("<IHHHHIffII",
                       MAGIC, VERSION, FRAME_RATE, int(K), int(nb),
                       int(nfr), float(len(x) / SR), float(final_amp_scale), 0, 0)
    out += np.array(NOISE_EDGES, dtype="<f4").tobytes()

    seg = np.zeros(N, dtype=np.float32)
    inter = np.zeros(K * 2, dtype="<u2")

    for k in range(nfr):
        c = k * hop + hop // 2
        _seg_into(seg, x, c - N // 2, N)
        X = np.fft.rfft(seg * w)
        mag = np.abs(X) + 1e-12
        db = 20 * np.log10(mag)
        i = np.arange(1, len(db) - 1)
        sel = (db[i] > db[i - 1]) & (db[i] >= db[i + 1]) & (db[i] > db.max() + PEAK_FLOOR)
        pk = i[sel]
        if len(pk):
            a_, b_, cc = db[pk - 1], db[pk], db[pk + 1]
            denom = (a_ - 2 * b_ + cc)
            p = np.where(denom != 0, 0.5 * (a_ - cc) / np.where(denom != 0, denom, 1), 0.0)
            f = (pk + p) * SR / N
            amp = 10 ** ((b_ - 0.25 * (a_ - cc) * p) / 20) / ws
            order = np.argsort(-amp)[:K]
            f, amp = f[order], amp[order]
        else:
            f, amp = np.zeros(0), np.zeros(0)

        nf = np.zeros(K); na = np.zeros(K); used = np.zeros(K, bool)
        for ff, aa in zip(f, amp):
            tol = max(6.0, TOL_FRAC * ff)
            cand = np.where(slot_live & ~used & (np.abs(slot_freq - ff) < tol))[0]
            if len(cand):
                j = cand[np.argmin(np.abs(slot_freq[cand] - ff))]
            else:
                free = np.where(~used & ~slot_live)[0]
                if len(free):
                    j = free[0]
                else:
                    rem = np.where(~used)[0]
                    if len(rem) == 0:
                        break
                    j = rem[0]
            used[j] = True; nf[j] = ff; na[j] = aa

        nf[~used] = slot_freq[~used]
        slot_freq = nf.copy()
        slot_live = used.copy()

        # noise residual per band
        m = mag.copy()
        for ff in nf[used]:
            ci = int(round(ff / SR * N))
            m[max(ci - 3, 0):ci + 4] = 0
        noise = np.empty(nb, dtype=np.float64)
        for b in range(nb):
            noise[b] = np.sqrt(np.sum(m[band_bins[b]] ** 2)) / ws

        # quantize this frame and append
        fq = np.clip(np.round(nf / 0.5), 0, 65535).astype("<u2")
        fq[na <= 0] = 0
        aq = np.clip(np.round(na / AMP_REF * 65535), 0, 65535).astype("<u2")
        inter[0::2] = fq
        inter[1::2] = aq
        out += inter.tobytes()
        nq = np.clip(np.round(noise / AMP_REF * 65535), 0, 65535).astype("<u2")
        out += nq.tobytes()

    return bytes(out)


if __name__ == "__main__":
    import resource, time
    t = np.arange(SR * 3) / SR
    x = 0.5 * np.sin(2 * np.pi * (220 + 220 * t) * t) + 0.1 * np.random.randn(len(t))
    t0 = time.time()
    blob = analyze(x)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"stream {len(blob)} bytes, {time.time()-t0:.1f}s, peak RSS {rss:.0f}MB")
    magic, ver, fr, osc, nb, nfr, dur, amps, _, _ = struct.unpack_from("<IHHHHIffII", blob, 0)
    print(f"magic=0x{magic:X} osc={osc} bands={nb} frames={nfr} dur={dur:.2f} ampScale={amps:.4f}")
    assert magic == MAGIC
    print("OK")
