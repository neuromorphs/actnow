# Handoff — DVS object-tracking on the KR260 dashboard

Session summary for the `core-fpga` branch. Focus: a live **object-tracking**
view in the ActNow dashboard, driven by the `dvs_track` firmware, plus fixes and
tuning discovered along the way. All work is committed (latest:
`Updated tracker to use spatio-temporal correlation`).

---

## 1. What this session delivered

1. **Tracking mode in the dashboard** (a second view next to the existing
   "Live Lab"): renders the **raw DVS event tap** with the tracker's **bounding
   box + centroid** overlaid.
2. **Fixed a firmware event-ABI bug**: `dvs_track` decoded x/y from the wrong
   bits on real hardware (see §4) — the single reason on-hardware tracking was
   garbage while the actsim reference looked fine.
3. **Replaced the noise filter with a spatio-temporal correlation filter (STCF)**
   modelled on jAER's `SpatioTemporalCorrelationFilter` (see §3).
4. **UI controls** for the tracker: gate radius, correlation strength, and a
   display-side "min events" reject threshold; **workbench hidden** in tracking
   mode; **colour-blind-safe orange/blue** event palette.

---

## 2. How the tracking algorithm works

### 2a. Firmware — `software/dvs_track/main.c` (runs on the RISC-V core)

Interrupt-driven: `fifo_in` fires once `BATCH=4` event words land; `isr_handler`
reads them. For **each event**:

1. **Decode** `x = (word>>24)&0x7F`, `y = (word>>17)&0x7F` (the hardware
   `evt_pack.v` ABI — see §4). Cell = 4×4-pixel block on a 32×28 grid.
2. **Spatio-temporal correlation filter (noise rejection).** Keep the event only
   if at least `CORR_MIN` (=2) of the **8 neighbouring cells** (3×3, *excluding
   the cell itself*) were touched within the last `CORR_WINDOW` (=30) events.
   - `last_touched[cell]` = event index a cell last fired; updated for **every**
     event (even dropped ones) so a genuine new region can bootstrap.
   - Excluding self is what rejects **hot pixels** — a stuck pixel firing into a
     quiet neighbourhood finds no support. Scattered background noise likewise
     has no correlated neighbours and is dropped.
   - `"now"` is the **event counter**, not a wall-clock timestamp (this rig has
     none; same convention as `dvs_denoise`/`dvs_timesurface`).
3. **Tracking gate.** Once locked, reject events whose Chebyshev distance from the
   current centroid exceeds `GATE_RADIUS` (=32). Active only when the *previous*
   window locked (`gate_active`), so a cold start / lost track searches the whole
   frame to reacquire.
4. **Centroid** = multiply-free exponential moving average in Q4 fixed point:
   `ema += (x - ema) >> EMA_SHIFT` (EMA_SHIFT=3 → closes 1/8 of the gap per
   event). Continuous, **not** reset per window.
5. **Bounding box** = plain min/max x/y of the surviving events **this window**.

Every `DUMP_INTERVAL=64` batches (=256 events) it writes **two status words** and
resets the box/count (not the EMA):

```
word0 (status): (locked<<24) | (cx<<16) | (cy<<8) | count   // count capped at 255
word1 (bbox):   (min_x<<24)  | (min_y<<16) | (max_x<<8) | max_y
locked = (window_count >= LOCK_THRESHOLD(=32))
gate_active = locked   // carried to the next window
```

`locked` tells a consumer whether the window had enough activity to trust the
reading. All coordinates fit in one byte (sensor is 126×112).

**Tunable constants** (all `#ifndef`-guarded → overridable with `-DNAME=…`):
`CORR_WINDOW=30`, `CORR_MIN=2`, `GATE_RADIUS=32`. Defaults `EMA_SHIFT=3`,
`LOCK_THRESHOLD=32`, `DUMP_INTERVAL=64`.

### 2b. Dashboard — `harness/dashboard/frontend/src/main.ts`

- **Raw tap** (UDP 3336 → websocket, `STREAM_RAW`) is drawn as a decaying event
  image (orange = ON, light blue = OFF; colour-blind-safe pair).
- **Result stream** (UDP 3334, `STREAM_RESULT`) in tracking mode is the
  `dvs_track` status/bbox pairs. `decodeTrack()` **self-synchronises** the pairing
  (a status word's top 7 bits are always zero because `locked∈{0,1}`; if the head
  isn't a plausible word0, drop one word until it realigns — otherwise a dropped
  UDP packet or leftover pre-load data keeps status/bbox swapped forever).
- `drawTrackOverlay()` maps bbox corners + centroid through the same
  `mapEvent()` used for the raw events, so box and events stay aligned in every
  orientation. Box colour: white = locked, grey = searching, dim = stale.
- **"Min events"** slider is a *display-side* reject: hide the box when
  `count < threshold` (instant; no reload). Distinct from the firmware's
  `LOCK_THRESHOLD`/gate.

---

## 3. Data path (raw + result streams)

```
AER camera → aer_rx → evt_pack (packs {0,x,y,ts,pol}) ─┬─► evt_stream → core.fifo_in  → dvs_track → result DMA → UDP 3334
                                                       └─► evt_stream → raw DMA        → UDP 3336
KR260 server (harness/pynq/actnow_fpga_server.py) forwards both DMAs on separate UDP ports.
Dashboard backend (harness/dashboard/backend/dashboard.py):
  - binds 3334 (result, always) and 3336 (raw, only while a client is in Tracking mode)
  - tags each websocket binary frame with a 4-byte stream id (STREAM_RESULT=0 / STREAM_RAW=1)
Frontend routes by tag: raw → image, result → track overlay (tracking mode) or event echo (live mode).
```

The tracker firmware is **not** the boot firmware. The board boots
`application`; clicking **Load tracker firmware** (Tracking mode) builds+applies
`dvs_track`. That action force-cleans `software/dvs_track/build` and rebuilds, so
the current source and any changed slider values always take effect. The
dashboard passes tuning via `make … EXTRA_CFLAGS="-DGATE_RADIUS=N -DCORR_MIN=N"`
(hook added in `software/common/program.mk`).

---

## 4. Critical gotcha — two event ABIs (sim vs hardware)

There are **two incompatible event packings** in this repo:

- **Hardware / `evt_pack.v`**: `x` in bits [30:24], `y` in [23:17], ts in [16:1],
  pol in [0]. `software/application/main.c` uses this (`X_SHIFT=24`).
- **chips/fpga sim**: e2e tests push raw low-bit literals directly to the FIFO
  (`x = v&0x7F`, `y = (v>>7)&0x7F`).

`dvs_track` was originally written for the **low-bit** ABI, so it worked in
actsim but decoded timestamp/polarity bits as coordinates on the FPGA. **Fixed**
by switching `dvs_track` to the hardware ABI (`X_SHIFT=24/Y_SHIFT=17`). To keep
sim green, the sim event sources were **repacked to the hardware ABI** (this
preserves the decoded x,y exactly, so tracker output is unchanged):
`chips/fpga/tests/e2e/e2e_fpga_track_test.act`,
`…/e2e_fpga_track_hotpixel_test.act`, and `chips/fpga/dvs_track_live.py`.

> Note: `dvs_denoise`/other `dvs_*` firmwares still use the low-bit ABI and would
> be **broken on hardware** the same way — out of scope this session, but relevant
> if any are ported to the dashboard.

---

## 5. Verification status

- Firmware compiles (RISC-V toolchain at `/opt/riscv/bin`), defaults and `-D`
  overrides both build; the constants bake in (confirmed in disassembly).
- Frontend `tsc --noEmit` clean, `npm run build` succeeds. Backend
  `unittest` suite (10 tests) passes via `harness/dashboard/.venv`.
- e2e assertions were **regenerated** with a Python reimplementation of the
  tracker (STCF + gate + EMA + min/max), which reproduces them exactly. New
  values: track `22425896 / 957249113`, hotpixel `21704484 / 957249113`.
- **NOT run on hardware or actsim** (neither available in this environment). The
  e2e `.act` tests are correct-by-construction and are **not wired into the
  Makefile `test` target**. STCF defaults were tuned only against the recorded
  capture, not a live wave test — this is the main thing to validate next.

---

## 6. How to run

```sh
make dashboard HOST_IP=<this machine's IP reachable from the KR260>
# opens http://127.0.0.1:8088 ; boots the `application` firmware
```
Then in the UI: switch to **Tracking** → set **Gate radius** / **Correlation** →
**Load tracker firmware**. Ports: result 3334, raw 3336, control 3335, http 8088.

Standalone reference (sim, needs actsim + cv2):
`python3 chips/fpga/dvs_track_live.py --csv <capture>.csv`.

**Restart `make dashboard`** to pick up frontend/backend changes (the running
aiohttp server does not hot-reload). Firmware is rebuilt on each "Load tracker
firmware" click.

---

## 7. Open items / next steps

- **Validate on hardware**: run a live wave test; tune `CORR_WINDOW`, `CORR_MIN`,
  `GATE_RADIUS` against real noise. The box may still be looser than ideal — the
  gate helps only after lock.
- **Wire the track e2e tests into CI** (`Makefile` test target) and run them under
  actsim to turn "correct-by-construction" into "verified".
- Consider fixing the low-bit ABI in the other `dvs_*` firmwares if any reach the
  dashboard.
- Optional: expose `CORR_WINDOW` as a UI control too (currently only `CORR_MIN`
  and `GATE_RADIUS` have sliders).

## 8. File map

| Area | File |
|---|---|
| Tracker firmware | `software/dvs_track/main.c` |
| Build flag hook | `software/common/program.mk` (`EXTRA_CFLAGS`) |
| Dashboard backend | `harness/dashboard/backend/dashboard.py` (`track()`, raw tap, stream tags) |
| Dashboard frontend | `harness/dashboard/frontend/{index.html,src/main.ts,src/style.css}` |
| e2e correctness | `chips/fpga/tests/e2e/e2e_fpga_track_test.act`, `…_hotpixel_test.act` |
| Sim reference viewer | `chips/fpga/dvs_track_live.py` |
| HW event packing | `harness/static/evt_pack.v`, `actnow_pl.v` |
