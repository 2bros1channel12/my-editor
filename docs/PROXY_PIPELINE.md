# Local proxy and preview generation

In Projects, choose **Generate preview assets** beside an imported source. Jobs shows progress and supports cancellation/retry. The `proxy_generate` service method accepts `{ "path": "absolute project path", "assetId": "stable asset ID" }`. It selects settings from the current capability profile and queues the built-in proxy handler. It returns a job ID, or a validated cached artifact. No timeline edits are made.

## Outputs and provenance

A successful job registers one immutable directory artifact at `cache/proxies/<job-id>/`:

- `proxy.mp4`: H.264/yuv420p, optional selected AAC audio, VFR presentation cadence retained.
- `analysis.wav`: when audio exists, 16 kHz mono PCM16 for later speech analysis.
- `waveform.json`: 10 ms min/max PCM bins, sample rate, sample count and original audio start/stream/timebase.
- `thumbnail-*.png`: up to five uniformly distributed frame positions, upright square-pixel images preserving display aspect ratio. These are timeline samples, not semantic scene selection.
- `mapping.json`: presentation-order frame pairs, original/proxy integer PTS, exact rational timebases, frame durations when reported, separate original/proxy keyframe flags, and the first source video PTS in seconds.
- `manifest.json`: asset ID, original checksum, pipeline/tool versions, stream metadata, selected settings, thumbnails' source PTS, component checksums and generator provenance. Waveforms identify Local Cut's PCM peak algorithm; media identifies FFmpeg; timing identifies FFprobe.

Paths within a bundle are relative to the bundle root. SQLite stores the bundle artifact path, recursive checksum and provenance. Consumers must validate the registered checksum before using a bundle. Missing or changed caches become stale and can be regenerated; originals are never overwritten or removed. An explicit full source checksum check precedes cache lookup and runs again before generation and publication. Large linked sources therefore add hashing latency to submission.

The timeline preview reads this waveform only from a validated proxy bundle. The service summarizes it to at most 1,200 min/max bins and returns source start and audio duration in microsecond ticks; the UI does not receive the full analysis PCM or the unsummarized waveform file.

## Timestamp contract

Time is derived from decoded presentation timestamps, never average frame rates. `sourcePts * sourceTimebase` is the timestamp on the original video stream; `proxyPts * proxyTimebase` is the proxy stream timestamp. Frame ordinal pairs preserve VFR cadence. `sourceMinusProxySeconds` is an exact rational offset measured from the first frame, and `sourceStartSeconds` records that first source frame's absolute PTS. Analysis and timeline ticks are media-relative: convert proxy time to source time with `proxyTime + sourceMinusProxySeconds - sourceStartSeconds`. Audio sample zero maps to `audio.sourceStartSeconds - mapping.sourceStartSeconds`; thumbnail timestamps use `sourcePts * sourceTimebase - mapping.sourceStartSeconds`. For a seek, locate the displayed proxy frame in the ordered mapping and use its paired source PTS normalized by `sourceStartSeconds`; do not compute a frame number from nominal FPS. Original absolute stream start times remain in the manifest. Timeline microseconds and absolute stream PTS are different units and must be converted explicitly.

The encoder uses `-copyts -start_at_zero`, `-fps_mode passthrough`, the source encoder timebase and a compatible MP4 track timescale. Source and proxy must have equal frame counts, strictly increasing timestamps and matching relative cadence within two timebase ticks. Muxers can change timestamps, so actual output timestamps are probed and checked. Audio and video share one origin; the pipeline does not independently reset them with STARTPTS. Audio stream offset is checked within 50 ms and duration within 100 ms, accounting for AAC priming/rounding. Analysis sample zero maps to the original selected audio stream's start time. Missing duration or unusable timestamps fail generation rather than inventing a mapping.

FFmpeg applies the original rotation once. Output rotation must be zero and display aspect ratio must match within 1%. Original display matrices, sample aspect ratio and timebases remain in the manifest. Arbitrary non-right-angle rotations are currently rejected. Source and proxy keyframes are both recorded because re-encoding creates different GOPs.

## Presets and resource limits

| Profile recommendation | Maximum coded side | Peak video rate | Encoder |
| --- | ---: | ---: | --- |
| low / unknown | 720 | 2 Mbps | libx264 veryfast, CRF 23 |
| balanced | 1280 | 4 Mbps | libx264 veryfast, CRF 23 |
| high | 1920 | 8 Mbps | libx264 veryfast, CRF 23 |

Images are not enlarged; dimensions are even and SAR retains display aspect. Encoder threads use half reported logical CPUs, bounded to 1–8 per job. The global job concurrency limit still applies. GPU encoders are not selected from advertised capability listings because the device/driver has not been exercised. A usable FFmpeg build with libx264, AAC, PNG and FFprobe is required; no binary is bundled or downloaded by the application.

Before encoding, the pipeline estimates peak-rate proxy bytes, PCM audio, indexes, thumbnails and 50% headroom plus 64 MiB fixed overhead. The project volume must also retain a 256 MiB reserve. This is an estimate, not a reservation across concurrent jobs; a later ENOSPC fails without registering partial output. Tool stdout is bounded to 128 MiB and stderr to 16 MiB; individual processes time out after one hour. Very long media that exceeds these bounds fails clearly. Full frame probing and decode validation prioritize correctness over generation speed.

## Process safety, cancellation and publication

All invocations are argument arrays with `shell=False`, fixed codecs/filters/options, absolute source paths, local `file,pipe` protocol allowlists, `-nostdin`, `-n`, and machine-readable progress. No creative-brief or model output becomes a command or filter. Process output uses independently opened file handles to avoid pipe deadlocks. Cancellation is polled even while a child emits no output.

On Windows, each child belongs to a Job Object with kill-on-close: cancellation terminates descendants, and service exit releases the object and terminates them. On POSIX, a new process group is terminated on cancellation; automatic group termination after an uncatchable service crash is not yet guaranteed. Windows is the release target. Handles are reaped before cleanup, with a short bounded retry for delayed Windows file-handle release.

The queue persists the complete input envelope. A bundle is built in `project/.localcut-staging/<job-id>/`, on the final artifact volume. Output validation includes complete decode of proxy streams, audio sample checks, thumbnail decode and component checksums. A durable publication journal records path/checksum/file identity before rename. Only after validation and publication does a transaction register the artifact and mark the project job complete; the queue acknowledgement follows. Recovery reconciles committed outputs or removes a matching, owned, unregistered publication before retry. Existing unrelated targets are preserved. Cancellation cleans owned partial output before reporting cancelled.

Only explicit transient launch failures and interrupted retry-safe jobs retry automatically, with the queue's bounded exponential policy. Unsupported media, failed encodes, invalid output and insufficient space fail with a useful error and allow explicit retry after correction. Current generation restarts from the beginning, rather than resuming a partially encoded MP4.

## Development verification

From the repository root:

```powershell
service/.venv/Scripts/python.exe -m pytest service/tests -q
npm test
npm run build
```

Real FFmpeg tests are opt-in and create only synthetic media under pytest's temporary directory:

```powershell
$env:LOCAL_CUT_TEST_FFMPEG = 'C:/tools/ffmpeg/bin/ffmpeg.exe'
$env:LOCAL_CUT_TEST_FFPROBE = 'C:/tools/ffmpeg/bin/ffprobe.exe'
service/.venv/Scripts/python.exe -m pytest service/tests/test_proxy_ffmpeg.py -q
```

The suite covers ordinary, VFR, rotated, delayed-audio, nonzero-start, silent and non-square-pixel media, corrupt input, unusual path characters, cancellation (including a Windows launcher descendant), process failures, low disk space, cache tampering, provenance, and interruption between publication and registration. Tested locally against FFmpeg N-111059 (June 2023, libavcodec 60/libavformat 60); other builds must pass these checks. Native desktop packaging and GPU encoding are not validated by these tests.

Design references: [FFmpeg process/timestamp options](https://ffmpeg.org/ffmpeg.html), [FFprobe stream and frame output](https://ffmpeg.org/ffprobe.html).
