# ActNow — Telluride demo ideas (async RV32I event-driven MCU)

Ranked, concrete demo applications for the **actnow** async RISC-V core fed by a
SciDVS event camera, for the Telluride neuromorphic workshop. Every idea is
grounded in the real hardware and existing firmware.

## Ground truth (what the hardware actually gives us)

Calibrated against the repo, not wishful thinking:

- **Core:** async RV32I, `-march=rv32i -mabi=ilp32` (see `software/common/program.mk`).
  **No multiply/divide** — only shift/add/sub/compare/logical are cheap. Any
  `*`, `/`, `%` on non-power-of-2 pulls in a slow software routine, so we design
  around powers of two, LUTs, and reciprocal-by-shift.
- **Memory:** 32 KB SRAM. Code runs XIP from ROM (bootloader copies to SRAM per
  `software/dvs_application/main.c`), so essentially all 32 KB is free for data.
  Every idea below fits in **< 1 KB of state** unless flagged.
- **Event model:** input FIFO raises `event_id_0` every `BATCH` events; the ISR
  drains `BATCH` words, processes, writes status words to the output FIFO (base 6)
  and/or a 4-bit GPIO register (base 7), then returns → `crt0.S` WFI sleeps the
  core. This is the exact pattern in `dvs_motion`, `dvs_rotate`, `dvs_application`.
- **Event word (FPGA ABI, from `harness/README.md`):**
  `[31] pad, [30:24] x(7b), [23:17] y(7b), [16:1] timestep(16b), [0] polarity`.
  Firmware today reads the simpler packed form `{x = word & 0x7F, y = (word>>7)&0x7F}`
  (see `dvs_motion/main.c`); **the 16-bit timestep is available on-chip** — several
  ideas below exploit it and are the ones that best show off event-camera *speed*.
- **Sensor:** 126×112, ON/OFF polarity, up to ~115k ev/s (subsample-able), µs-scale latency.
- **Output visualization (already built):** `chips/fpga/dvs_motion_view.py` renders a
  status word `(motion, val, row, col)` as an overlay; `harness/pynq/actnow_fpga_server.py`
  DMAs result words to **UDP :3334** and raw events to **:3336**; `harness/host/actnow_client.py`
  is the viewer. So "emit a status word, a host renders it" is a *solved* output path —
  new demos just define a new 15-bit word layout and a tiny unpack function.
- **GPIO:** 4 output pins (`gpio_out_0..3`) writable as a 4-bit register; wire an LED /
  buzzer / servo-enable to each. `software/gpio_demo/main.c` shows the write.

**Budget shorthand:** "ops/event" counts the per-event work in the ISR inner loop
(the per-*batch* overhead — decay, argmax — is amortized over `BATCH` events).

---

## Ranked demo ideas

### Tier 1 — best crowd-pleasers (build these first)

#### 1. Looming / time-to-contact "duck!" detector  ⭐ top pick
**Pitch:** Swing your hand toward the camera and a red "AVOID" LED fires *before* you
hit it — event cameras see the approach with millisecond latency.

**Core computation:** A looming object's edges expand outward from a focus of
expansion, so the event cloud's **spatial spread (radius) grows over time** while the
event rate spikes. Track a running centroid with a cheap decayed accumulator, then a
**mean-absolute-deviation radius** `R = mean(|x-cx| + |y-cy|)` over the batch (L1, no
sqrt, no multiply). Keep `R` from the previous few batches in a ring buffer; if
`R` is increasing *and* event rate is rising past a threshold, assert looming.
Centroid via sum/count: divide-by-count avoided by fixing `BATCH` to a power of two
(e.g. 8 or 16) so `cx = sum_x >> log2(BATCH)`.

**Memory / compute:** ~64 B (centroid accumulators, 8-deep R ring buffer, rate EWMA).
~4 adds + 2 abs + 1 compare per event; a handful of shifts per batch. Trivial.

**Shows:** GPIO0 = "AVOID" LED (or buzzer); status word = `(looming_flag, R, cx, cy)`
so the host draws an expanding circle that turns red on trigger. Physically visceral —
the LED beats your reflexes.

**Feasibility:** **moderate.** The math is easy; tuning "expanding vs. just fast" so it
doesn't false-trigger on lateral swipes is the real work. Highest wow-per-effort.

---

#### 2. Hand/ball tracker with 4-LED direction compass  ⭐
**Pitch:** Wave a ball; four LEDs (N/E/S/W) light to point at it, and it tracks at the
camera's full speed with no frames.

**Core computation:** This is `dvs_motion/main.c` *already working* — a decaying
activity grid + argmax hottest cell — repurposed to drive GPIO instead of only a
status word. Map the argmax cell's `(row,col)` relative to frame center to a 4-bit
GPIO: bit set if the hot cell is in that half (up/down/left/right), or a
one-hot "which quadrant" code. The status word path is unchanged, so
`dvs_motion_view.py` keeps rendering the tracking box for the projector.

**Memory / compute:** 16-cell grid = 16 B (as shipped). Per event: 2 shifts + 1 add.
Per batch: 16 halvings + 16-way argmax. Already benchmarked in `e2e_fpga_motion_test`.

**Shows:** 4 discrete LEDs as a physical compass **plus** the existing on-screen
tracking box. Two output modalities from one demo.

**Feasibility:** **easy.** Lowest-risk demo — it's a 10-line GPIO addition to shipping,
tested firmware. Great "always works" fallback for the booth.

---

#### 3. Blinking-light / fan-RPM frequency meter (exploits the µs timestamp)  ⭐
**Pitch:** Point the camera at a spinning fan, a blinking LED, or a strobe; the chip
reads out the frequency in real time — impossible to do this fast with a normal camera.

**Core computation:** Pick the hottest region (or just gate on a bright ROI), and
measure the **time between successive ON-event bursts** using the on-chip 16-bit
`timestep`. Maintain "time of last burst"; on a new burst, `period = t - t_last`.
Frequency needs a divide (`f = 1/period`) — **avoid it**: either (a) report the
*period* directly (a host divides once for display), or (b) use a small
**reciprocal LUT** indexed by the top bits of `period` (a few hundred bytes) to emit
Hz directly. Debounce with a minimum inter-burst gap.

**Memory / compute:** ~32 B state + optional ~256–512 B reciprocal LUT. A subtract and
a compare per burst; cheap. Handles fan RPM, PWM-dimmed LEDs, strobes, even
mains-flicker (50/60 Hz) as a party trick.

**Shows:** Status word = measured period/Hz, host renders a big number / bar; or blink
GPIO0 at a divided-down copy of the detected frequency so an LED "echoes" the fan.
Directly demonstrates the temporal resolution frames can't touch.

**Feasibility:** **moderate.** Burst detection + debounce needs a little tuning, but the
timestamp is right there in the event word. Uniquely event-camera — very demo-worthy.

---

### Tier 2 — strong, distinctive

#### 4. OMS "independent object" flagger (Meister object-motion sensitivity)
**Pitch:** Pan the camera across a cluttered scene and nothing fires; move one object
independently and a box lights up *only* on that object — background motion is ignored.

**Core computation:** A drastically simplified, integer, multiply-free port of
`~/git/oms-meister`'s center–surround OMS. Full pipeline (dual leaky integrators,
tanh, divisive AGC) is too heavy, but the **load-bearing invariant survives cheaply**:
rectify-before-pool. On the shipped 4×4 activity grid, for each cell compute a
**center vs. annulus** contrast: `residual = center_activity - (surround_activity >> k)`
(surround = mean of neighbors, approximated by a shift). Global/coherent motion lights
center and surround together → residual ≈ 0 → silent. An independently moving object
decorrelates them → positive residual → fire on that cell. Divisive inhibition becomes
a **subtractive** surround (no divide); "unit-DC-gain" pooling becomes shift-normalized
neighbor sums.

**Memory / compute:** two 16 B grids (ON/OFF or center/surround) + a few accumulators.
Per event: grid update as in demo 2. Per batch: per-cell neighbor sum (8 adds) + subtract
+ threshold. ~200–400 ops/batch — comfortably real-time.

**Shows:** status word flags the firing cell → host box, exactly like `dvs_motion_view.py`.
The killer demo is the *negative*: whole-field motion produces nothing.

**Feasibility:** **moderate→stretch.** Getting the surround normalization to actually
cancel background pan (vs. subtractive being too crude) is the risk; a coarse version is
easy, a *convincing* one takes tuning. Conceptually the most "neuromorphic" story to tell.

#### 5. Wave / swipe-gesture recognizer (left/right/up/down)
**Pitch:** Swipe your hand and the chip classifies the direction; drives an LED arrow or
sends a keystroke-like status word.

**Core computation:** Track the activity-grid centroid over the last N batches (reuse
demo 2's grid). A swipe = centroid crossing the frame with consistent sign of `Δcx`/`Δcy`
above a speed threshold, then activity collapsing. Classify by which axis dominates
(`|Δcx|` vs `|Δcy|`, L1, no multiply) and the sign. A tiny state machine
(idle → moving → committed) debounces.

**Memory / compute:** ~64 B (centroid history ring + FSM state). ~4 ops/event, a dozen
per batch. Easy.

**Shows:** one-hot 4-bit GPIO (←→↑↓ LEDs) + status word the host turns into an arrow.
Interactive and reliable — good for hands-on booth traffic.

**Feasibility:** **easy→moderate.** The FSM is the only fiddly part.

#### 6. Laser-pointer / hotspot tracker
**Pitch:** Shine a laser pointer or bright flashlight; the chip locks onto the brightest
moving spot and a pan/tilt or LED cursor follows it.

**Core computation:** A laser dot is a tight, high-rate ON-event cluster. Compute the
event centroid but **weight by local density** (winner-take-all: keep the single grid
cell with the most events this batch, then a fine centroid within it using the raw
`x,y`). Sub-cell position via sum/`BATCH` shift. Optionally reject non-point clutter by a
compactness check (batch spatial spread below a threshold — reuse demo 1's `R`).

**Memory / compute:** ~48 B. ~4 ops/event, argmax per batch. Easy.

**Shows:** status word = `(x,y)` → host cursor, or GPIO PWM two pins to nudge a hobby
servo pan/tilt so a physical turret tracks the dot. High "it's alive" factor.

**Feasibility:** **moderate.** Firmware trivial; a servo turret is extra hardware but a
showstopper if you have one. LED-cursor version is easy.

#### 7. "How fast are you moving" event-rate speedometer
**Pitch:** A live bar/needle that climbs with scene motion — hold still, it drops to zero;
wave fast, it pegs. Instant, intuitive "activity = events" story.

**Core computation:** Count events per unit time using the on-chip `timestep`:
maintain an EWMA of `BATCH / (t_now - t_last_batch)`. Divide avoided by reporting
**events-in-fixed-time** instead (accumulate a count, emit and reset every time the
timestamp advances by a power-of-two window → the window edge is a bitmask test, no
divide). Emit the count as the status word.

**Memory / compute:** ~16 B. 1 add/event, 1 compare/batch. Trivially easy.

**Shows:** status word → host VU-meter/bar; or thermometer-code the 4 GPIO LEDs
(1/2/3/4 lit by rate bands) for a frame-free "how much is happening" gauge. Also a great
always-on **background/idle animation** for the booth.

**Feasibility:** **easy.** Near-zero risk; pairs well with any other demo as a HUD.

#### 8. Motion-activated wake / intrusion alarm (showcases the power story)
**Pitch:** The core sleeps in WFI drawing ~nothing; the instant something moves, it wakes,
flashes an alarm LED, and goes back to sleep. The whole point of event-driven silicon.

**Core computation:** Almost none — the wake *is* the computation. FIFO fires only when
events arrive, so an empty scene = permanent WFI. On wake, require rate/spread above a
threshold (reuse demo 7's count) to reject single-event noise, then latch GPIO0 (alarm)
for a fixed number of batches before re-arming.

**Memory / compute:** ~16 B. Effectively free.

**Shows:** GPIO alarm LED + status word ("armed/triggered"). The narration is the demo:
"it's asleep… *[wave]* …and it woke in microseconds and slept again." Best **low-power**
story for the async-core pitch.

**Feasibility:** **easy.** The most *conceptually* on-message demo for an async
event-driven MCU, and nearly free to build.

### Tier 3 — nice, more involved

#### 9. Edge-orientation histogram ("what angle are the moving edges?")
**Pitch:** Move a striped card or an edge; a rose/compass shows the dominant edge
orientation, updating live.

**Core computation:** For each event, estimate local gradient direction from the
**offset to the recent centroid** or from a small per-cell ON/OFF imbalance, and bin into
4 or 8 orientation buckets using **sign/compare tests** (octant classification is pure
comparisons of `|dx|` vs `|dy|` and their signs — no atan, no multiply). Accumulate a
decayed histogram; emit the argmax bucket.

**Memory / compute:** 8-bin histogram = 8 B + a small neighbor state. ~6 compares/event.
Moderate.

**Shows:** status word = dominant orientation → host rose plot, or map 4 buckets to the
4 GPIO LEDs. Visually rich but the underlying estimate is coarse on 126×112.

**Feasibility:** **moderate.** Orientation from sparse events without gradients is
approximate; convincing on structured stimuli (gratings), noisier on hands.

#### 10. Blink / eye-open-closed detector
**Pitch:** Look at the camera and it counts your blinks / shows a "closed" indicator —
event cameras catch the fast eyelid motion cleanly.

**Core computation:** A blink is a brief, spatially-localized burst of OFF-then-ON events
in a horizontal band. Gate on an ROI (upper-center grid cells), detect a **rate spike with
OFF-before-ON ordering** using the timestamp, debounce to a plausible blink duration
(~100–300 ms) with the on-chip clock. Count via increment.

**Memory / compute:** ~32 B. Cheap per event; a small FSM.

**Shows:** GPIO "blink" LED pulse + status-word blink counter on the host.

**Feasibility:** **moderate→stretch.** Needs the subject framed and reasonably still;
booth lighting and head motion make it finicky. Great when it works, fragile on a table.

#### 11. Braitenberg light/motion follower (vehicle behavior)
**Pitch:** A little two-wheeled bot (or two LEDs standing in for motors) steers *toward*
(or away from) motion — a classic neuromorphic Braitenberg vehicle driven by a real
event core.

**Core computation:** Split the frame into left/right halves; per batch compare event
counts `L` vs `R` (from the grid). Steer = drive the two motor GPIOs proportional to the
imbalance (`L>R` → turn right, etc.); "coward vs. aggressor" wiring = swap which side
speeds up. Pure counts and compares.

**Memory / compute:** ~16 B. ~2 ops/event. Trivial firmware.

**Shows:** two GPIO-driven motors/LEDs = physical steering. The most *behavioral*,
Telluride-flavored demo if you have (or 3D-print) a chassis.

**Feasibility:** **moderate.** Firmware is easy; the demo's cost is the robot chassis and
motor driver, not the code. High payoff as a moving physical artifact.

#### 12. Falling-object / gravity-drop reaction timer
**Pitch:** Drop a ball through the field of view; the chip catches the exact moment it
crosses a virtual tripwire and flashes an LED / logs the crossing time — a frame-free
photo-finish.

**Core computation:** Define a horizontal tripwire row band. Watch the activity
centroid's `y`; when it crosses the band with downward `Δy`, latch the on-chip timestamp
and fire GPIO. Two tripwires → a **velocity** between them (`Δy` fixed, so speed ∝ `1/Δt`
— again report `Δt` or LUT the reciprocal to dodge the divide).

**Memory / compute:** ~32 B. Cheap.

**Shows:** GPIO flash at crossing + status word timestamps → host "gate times". Crisp
demonstration of low-latency timing.

**Feasibility:** **moderate.** Reliable tripwire logic; the two-gate speed version is the
stretch. Good pairing with demo 3 (both sell the timestamp).

---

## Recommended booth lineup

- **Always-on HUD:** #7 event-rate speedometer (idle animation + activity gauge).
- **Grab-attention:** #1 looming "duck!" detector and #3 fan/blink frequency meter —
  both are visceral and uniquely event-camera.
- **Reliable hands-on:** #2 hand/ball tracker (the shipping firmware) and #5 swipe
  gestures — never fail, invite interaction.
- **Deep story:** #4 OMS flagger and #8 motion-wake alarm — the "why async + event-driven"
  narrative for the technically curious.

## Honest constraints to respect in every build

- **No multiply/divide:** fix `BATCH` to a power of two so centroids divide by shift; use
  L1 (`|dx|+|dy|`) not Euclidean distance; report periods/counts and let the host divide,
  or use a small reciprocal LUT (a few hundred bytes). Never call `%` on a runtime value.
- **32 KB is plenty** for every idea here (< 1 KB state each) — the constraint is compute
  and *tuning*, not memory. LUTs up to a few KB are fine.
- **Per-batch vs per-event:** heavy steps (argmax, decay, neighbor sums) go once per batch
  and amortize over `BATCH` events; keep the per-event inner loop to a few ops.
- **The timestamp is your differentiator.** Demos that use the 16-bit `timestep`
  (#1, #3, #7, #8, #10, #12) are the ones that show what frames *can't* do — prioritize them.
