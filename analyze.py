"""
Offline sinusoidal + noise-band analysis.

Turns a mono 44.1kHz float array into the binary stream described in
STREAM_FORMAT.md. This is the expensive step; the web layer caches its output
per source URL so it only runs once per track.
"""
import numpy as np
import struct

SR          = 44100
FRAME_RATE  = 100            # frames per second in the stream
WINDOW      = 4096           # analysis FFT size (sweet spot; bigger smears time)
OSC_COUNT   = 224            # partials per frame
TOL_FRAC    = 0.012          # peak-to-oscillator tracking tolerance
PEAK_FLOOR  = -90            # dB below frame max to still count a peak
NOISE_EDGES = [0, 250, 500, 1000, 2000, 4000, 8000, 16000, SR // 2]

MAGIC   = 0x52535631
VERSION = 1


def _seg(x, start, n):
    out = np.zeros(n, dtype=np.float64)
    a, b = max(start, 0), min(start + n, len(x))
    if b > a:
        out[a - start:b - start] = x[a:b]
    return out


def analyze(x):
    """x: mono float array at SR. Returns bytes in stream format v1."""
    x = np.asarray(x, dtype=np.float64)
    peak = np.max(np.abs(x)) if len(x) else 1.0
    if peak > 0:
        x = x / peak  # normalize; ampScale carries loudness back

    hop = SR // FRAME_RATE
    N = WINDOW
    w = np.hanning(N)
    ws = w.sum() / 2
    nfr = max(len(x) // hop, 1)
    nb = len(NOISE_EDGES) - 1

    fb = np.fft.rfftfreq(N, 1 / SR)
    band_bins = [np.where((fb >= NOISE_EDGES[b]) & (fb < NOISE_EDGES[b + 1]))[0]
                 for b in range(nb)]

    K = OSC_COUNT
    slot_freq = np.zeros(K)     # freq each slot held last frame
    slot_live = np.zeros(K, bool)

    # output arrays
    out_freq = np.zeros((nfr, K), dtype=np.uint16)
    out_amp  = np.zeros((nfr, K), dtype=np.uint16)
    out_noise = np.zeros((nfr, nb), dtype=np.uint16)

    global_max_amp = 1e-9

    frames_peaks = []
    # ---- pass 1: peak pick + track into stable slots ----
    for k in range(nfr):
        c = k * hop + hop // 2
        X = np.fft.rfft(w * _seg(x, c - N // 2, N))
        mag = np.abs(X) + 1e-12
        db = 20 * np.log10(mag)
        i = np.arange(1, len(db) - 1)
        pk = i[(db[i] > db[i - 1]) & (db[i] >= db[i + 1]) & (db[i] > db.max() + PEAK_FLOOR)]
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

        # track each peak to nearest live slot, else a free slot
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

        # slots that kept no peak this frame: keep freq, go silent
        nf[~used] = slot_freq[~used]
        slot_freq = nf.copy()
        slot_live = used.copy()

        if na.max(initial=0) > global_max_amp:
            global_max_amp = na.max()

        # noise residual: remove sinusoid lobes, measure per-band energy
        m = mag.copy()
        for ff in nf[used]:
            ci = int(round(ff / SR * N))
            m[max(ci - 3, 0):ci + 4] = 0
        noise = np.array([np.sqrt(np.sum(m[band_bins[b]] ** 2)) / ws for b in range(nb)])
        if noise.max(initial=0) > global_max_amp:
            global_max_amp = noise.max()

        frames_peaks.append((nf, na, noise))

    # ---- pass 2: quantize with a single amp scale ----
    amp_scale = global_max_amp if global_max_amp > 0 else 1.0
    for k, (nf, na, noise) in enumerate(frames_peaks):
        fq = np.clip(np.round(nf / 0.5), 0, 65535).astype(np.uint16)
        fq[na <= 0] = 0
        aq = np.clip(np.round(na / amp_scale * 65535), 0, 65535).astype(np.uint16)
        nq = np.clip(np.round(noise / amp_scale * 65535), 0, 65535).astype(np.uint16)
        out_freq[k] = fq
        out_amp[k] = aq
        out_noise[k] = nq

    # amp decoded as raw/65535*ampScale; carry original loudness (peak) too
    final_amp_scale = amp_scale * peak

    # ---- serialize ----
    buf = bytearray()
    # header: magic(I) ver(H) frameRate(H) osc(H) bands(H) frameCount(I)
    #         duration(f) ampScale(f) reserved(I) reserved(I) = 32 bytes
    buf += struct.pack("<IHHHHIffII",
                       MAGIC, VERSION, FRAME_RATE, int(K), int(nb),
                       int(nfr), float(len(x) / SR), float(final_amp_scale), 0, 0)
    buf += np.array(NOISE_EDGES, dtype="<f4").tobytes()
    for k in range(nfr):
        inter = np.empty(K * 2, dtype="<u2")
        inter[0::2] = out_freq[k]
        inter[1::2] = out_amp[k]
        buf += inter.tobytes()
        buf += out_noise[k].astype("<u2").tobytes()
    return bytes(buf)


if __name__ == "__main__":
    # self-test with a synthetic tone sweep
    t = np.arange(SR * 3) / SR
    x = 0.5 * np.sin(2 * np.pi * (220 + 220 * t) * t) + 0.1 * np.random.randn(len(t))
    blob = analyze(x)
    print(f"stream: {len(blob)} bytes for {len(x)/SR:.1f}s")
    magic, ver, fr, osc, nb, nfr, dur, amps, _, _ = struct.unpack_from("<IHHHHIffII", blob, 0)
    print(f"magic=0x{magic:X} ver={ver} frameRate={fr} osc={osc} bands={nb} frames={nfr} dur={dur:.2f}s ampScale={amps:.4f}")
    assert magic == MAGIC and ver == VERSION
    print("OK")
