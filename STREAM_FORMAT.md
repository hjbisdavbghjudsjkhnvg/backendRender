# Audio stream binary format (v1)

Little-endian. Produced by the backend's offline analysis, consumed by the
Roblox client. One blob per track, cached per source URL.

## Header (32 bytes)
| offset | type   | field          | notes                               |
|--------|--------|----------------|-------------------------------------|
| 0      | u32    | magic          | 0x52535631  ("RSV1")               |
| 4      | u16    | version        | 1                                   |
| 6      | u16    | frameRate      | analysis frames per second (e.g 100)|
| 8      | u16    | oscCount       | max oscillators per frame (e.g 224) |
| 10     | u16    | noiseBands     | number of noise bands (e.g 8)       |
| 12     | u32    | frameCount     | total frames                        |
| 16     | f32    | duration       | seconds                             |
| 20     | f32    | ampScale       | multiply decoded amps by this       |
| 24     | u32    | reserved       |                                     |
| 28     | u32    | reserved       |                                     |

Pack string: `<IHHHHIffII`

## Noise band edges (noiseBands+1 × f32)
The frequency edges, in Hz. Lets the client build matching band loops / know
which uploaded noise asset maps to which band.

## Frames (frameCount × frame)
Each frame is fixed size so the client can seek by index = time*frameRate.

Per frame:
- oscCount × 2 × u16:
  - freq : u16, frequency in 0.5 Hz units (0 = oscillator silent).
          decoded Hz = raw * 0.5  (range 0..32767.5 Hz, covers full 44.1k)
  - amp  : u16, amplitude 0..65535 mapped to 0..1 via /65535, then ×ampScale
- noiseBands × u16:
  - energy : u16, per-band noise level 0..65535 -> 0..1 via /65535, ×ampScale

Oscillator slot index is stable across frames where possible (the backend keeps
a partial on the same slot while it lives), so the client can glide rather than
reassign. A slot with freq=0 is silent that frame but keeps its identity.

## Size
Per frame = oscCount*4 + noiseBands*2 bytes.
At 100 fps, 224 osc, 8 bands: 912 B/frame = ~91 KB/s = ~5.5 MB for a 60s clip
(gzip'd in transit; HttpService handles decompression).
