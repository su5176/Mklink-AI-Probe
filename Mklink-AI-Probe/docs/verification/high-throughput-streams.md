# High-throughput stream qualification

This page separates the automated transport gate from browser, packaged-app,
and physical MKLink evidence. A synthetic result is never reported as HIL.

## Automated release gate

The CI gate runs for a real ten seconds and requires 100,000 aggregate samples,
zero sequence errors, and zero unreported loss:

```powershell
python -m pytest _maintainer/testing/tests/test_stream_performance.py -q
python _maintainer/testing/performance/stream_benchmark.py --stream vofa --duration 10 --rate 10000 --channels 8
```

On 2026-07-14 it produced and consumed 100,000 samples in 10.001 seconds at
403,556.9 encoded bytes/s, with queue high-water 1 and no reported or
unreported loss. The small machine-readable result is
[stream-ci-gate-2026-07-14.json](artifacts/stream-ci-gate-2026-07-14.json).
This is a local Python `StreamHub`/codec benchmark: it does not exercise a
WebSocket, browser Worker, GUI renderer, packaged app, USB, SWD, or target MCU.

`gui/src/lib/stream/renderScheduler.test.ts` is the automated 30 FPS rendering
cadence gate. It verifies coalescing and hidden-document behavior with a fake
clock; it is not a measured packaged-app frame rate.

## MKLink HIL matrix

Target qualification uses the connected MKLink and STM32F103RC test project.
The 30-minute rows report qualified stable zero-drop rates. RTT also has a
measured overload boundary. VOFA and SuperWatch use the fastest supported
request period (10 us), where MKLink throughput plateaus near 8 kSamples/s;
SystemView uses the qualified pairs=2/tick load. These three are not claimed as
drop-bounded maxima because a higher failing point was not retained.

| Stream | Probe firmware | SWD | Channels/events | Qualified stable zero-drop rate | 30 min items | Bytes/s | Backend drops | Frontend drops | Peak memory | Render FPS | Paused/hidden acquisition | Evidence scope |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| SystemView | V4.3.4 | 1 MHz | qualified backend load: pairs=2/tick; frontend: pairs=1/tick | 20,093.43 events/s | 36,171,279 | 966,024.77 | 0 measured increment | 0 | 798,273,536 B | 25.68 visible / 25.73 resumed | pause PASS; hidden not established | backend + Edge visible/pause PASS; hidden N/E |
| VOFA | V4.3.4 | 1 MHz | 2 | 8,044.33 samples/s | 14,479,808 | 73,404.55 | 0 | 0 | 736,600,064 B | 25.98 visible / 25.56 resumed | pause PASS; hidden not established | backend + Edge visible/pause PASS; hidden N/E |
| RTT | V4.3.4 | 1 MHz | 4 numeric + raw log | 12,997.51 samples/s | 23,395,840 | 474,932.82 | 0 | 0 | 709,496,832 B | 5.10 visible / 4.92 resumed | pause PASS; hidden not established | backend + Edge visible/pause PASS; hidden N/E |
| SuperWatch | V4.3.4 | 1 MHz | 2 | 8,023.71 samples/s | 14,442,688 | 73,477.36 | 0 | 0 | 733,954,048 B | 25.98 visible / 25.97 resumed | pause PASS; hidden not established | backend + Edge visible/pause PASS; hidden N/E |

The committed report must contain only the masked probe identity, firmware
version, STM32F103RC, configured SWD frequency, aggregate samples/events per
second, encoded bytes per second, both loss domains, process peak memory, and
measured visible FPS. Full probe IDs, serial ports, usernames, screenshots,
and raw captures must not be committed.

The backend HIL artifacts are
[SystemView](artifacts/systemview-ws-30min-2026-07-14.json),
[VOFA](artifacts/vofa-ws-30min-2026-07-14.json),
[RTT](artifacts/rtt-ws-30min-2026-07-14.json), and
[SuperWatch](artifacts/superwatch-ws-30min-2026-07-14.json). All four used
one authenticated binary WebSocket for the complete timed window and report
zero sequence gaps and zero stream-specific backend loss increments.

The real Edge frontend artifacts are
[SystemView](artifacts/edge-systemview-browser-2026-07-14.json),
[VOFA](artifacts/edge-vofa-browser-2026-07-14.json),
[RTT](artifacts/edge-rtt-browser-2026-07-14.json), and
[SuperWatch](artifacts/edge-superwatch-browser-2026-07-14.json). Each gate
loaded the hashed Worker and binary WebSocket, observed real target data,
kept acquisition and Worker counters advancing during a five-second render
pause, rendered zero target Canvas frames while paused, resumed without
sequence or transport loss. The SystemView, VOFA, and SuperWatch gates also
captured zero-client/resource cleanup. The original RTT Edge run did not
capture cleanup; its stale session was stopped and dearmed before later gates,
and the committed probe now verifies stop, dearm readback, zero clients, and
resource release in `finally`. SystemView startup/SYNC loss is reported as a frozen warm-up baseline;
the visible, pause, and resume intervals all have zero target/parser/backend
loss increments.

The RTT Edge run predates the final strict integrity predicate. Its artifact
separates the captured run evidence from the later probe hardening: the current
probe explicitly counts frontend sequence errors and checks Worker transport,
Worker-reported backend, and backend batch/item/byte loss before PASS.

Edge 130 did not support `Emulation.setPageVisibilityOverride`, and bringing a
second tab to the foreground did not change the tested tab to
`document.hidden=true`. Therefore hidden-tab HIL is uniformly recorded as
`not-established`, not PASS. Hidden-document scheduling remains covered by
the deterministic `RenderScheduler` test.

RTT qualification used a test-firmware-only 16 KiB channel-0 up buffer. The
original 1 KiB buffer exposed target scheduling jitter: burst 11 sustained
10,999.61 samples/s and had no WebSocket or Hub loss, but accumulated 32
target-side write drops over 30 minutes. That diagnostic is retained as
[the burst-11 failed-limit artifact](artifacts/rtt-ws-burst11-fail-30min-2026-07-14.json).
After changing `PKG_SEGGER_RTT_BUFFER_SIZE_UP` from 1,024 to 16,384 in the
external STM32F103RC test project's `.config` and `rtconfig.h`, Keil rebuilt
the firmware with zero errors (four pre-existing warnings; ZI 45,816 bytes).
Five-minute host-armed screens were lossless at bursts 10, 12, and 13; burst
14 was the overload boundary. The selected burst 13 then completed 1,800.025
seconds with 23,395,840 numeric samples, target markers
`130/130/0 -> 23395710/23395710/0`, zero Hub loss, and queue high-water 33/64.
The target fixture remains outside this repository and must keep the 16 KiB
test setting for this exact RTT result to be reproducible.

## Browser and packaged-app gates

The browser/Worker gate must connect to `/ws/streams/{name}`, observe binary
frames and Worker telemetry, pause rendering, hide the tab, and prove that
backend and Worker collection counters continue while visible rendering stays
at or below 30 FPS. These measurements are recorded separately from backend-
only HIL.

The packaged Tauri gate is:

```powershell
python skills/tauri-gui-builder/scripts/build.py --check
Set-Location gui
npx tauri build
```

Install or launch the produced bundle, then run one physical stream for five
wall-clock minutes. Confirm that the hashed Worker asset and binary WebSocket
load from the packaged application. Build products and raw captures remain
untracked.

The release build produced the Windows executable, MSI, and NSIS bundles. A
  real packaged WebView2 RTT session then ran for 300.027 seconds through the
  Tauri asset origin and hashed Worker: 31,010 WebSocket data frames matched
  31,010 Worker-accepted frames, both ended at sequence 34,334, and carried
  7,832,827 items. The run retained 200,000 Worker samples, rendered at 5.27 FPS,
  reported zero backend/frontend/sequence loss and zero console errors, then
  finished with the backend stopped, zero active clients, no RTT resource owner,
  and successful target-dearm readback. See
[tauri-rtt-5min-2026-07-14.json](artifacts/tauri-rtt-5min-2026-07-14.json).
The committed probe performs stop, dearm, readback, client cleanup, and resource-
owner verification in `finally`. Exact WebSocket/Worker frame parity, final
sequence agreement, transport/backend loss, render FPS, console state, and every
cleanup result are required by one PASS predicate.

## Regression commands

```powershell
python -m pytest -q
Set-Location gui
npm test
npm run build
Set-Location ..
git diff --check
```

Baseline before HIL: Python 571 passed; GUI 18 files / 217 tests passed; Vite
production build transformed 140 modules. Vitest reports pre-existing invalid
`<tr>` nesting warnings, and `npm audit` reports two pre-existing high-severity
dependency findings; neither was introduced by this qualification task.

## Existing bounded synthetic evidence

The earlier RTT/SuperWatch synthetic soaks remain available in Git history.
They must not be used to fill the HIL matrix above.
