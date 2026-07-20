#!/usr/bin/env python3
"""Live view of the motion detector as a glowing blob heatmap, instead of
dvs_motion_live.py's raw-event-dots + single-cell box.

Same live-tailing setup as dvs_motion_live.py: launches
chips/fpga/tests/e2e/e2e_fpga_motion_capture.act's actsim run itself and
tails motion_capture_results.mem as it grows -- so this watches actsim's own
progress happen, at whatever pace actsim can compute (not paced to any real
clock; see dvs_motion_live.py's header for why "live" doesn't mean real-time
relative to the original recording or real hardware).

The wire protocol only ever carries the single hottest cell per batch
(software/dvs_motion/main.c's isr_handler argmaxes its 4x4 grid down to one
cell before writing the output FIFO) -- the real chip has no way to report
finer-grained shape than "which 32x32 block moved." So MotionGrid mirrors
the firmware's coarse grid math (same approach dvs_motion_view.py's
python_motion_words already uses) purely to cross-check against the real
per-batch status word and derive a motion flag -- it is NOT what gets
rendered. What gets rendered is FineTrail: a full sensor-resolution
(126x112, one cell per pixel) trail built directly from each event's own
raw (x,y), independent of the chip's grid entirely, so the glow traces the
actual shape of whatever is moving (e.g. a waving hand) instead of a single
coarse block.

Rendering: black canvas, no raw event dots. FineTrail's own additive/capped
accumulation (see its docstring) is what turns sparse real events into a
region rather than noise; cubic upsampling plus a small merge blur then
smooth that into a continuous shape, an INFERNO colormap turns activity
into a black-to-white-hot glow, and a Gaussian-blur screen-blend adds bloom
on top -- regions with no repeated recent activity stay black.

Usage: dvs_motion_blob_live.py [--csv phone.csv] [--fps 30] [--scale 8]
                                [--gain 1.6] [--decay 0.98] [--radius 2]
                                [--add 0.18] [--cross PREFIX]
live keys: q=quit (kills the actsim run if still in progress)
"""
import argparse
import csv
import os
import subprocess
import sys
import time

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CSV = "dvs_capture_20260714_151049.csv"
EVENTS_PATH = os.path.join(os.path.dirname(__file__), "rotate_capture_events.mem")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "motion_capture_results.mem")

SX, SY = 126, 112
CELL_SHIFT = 5  # 32x32-pixel cells -- must match software/dvs_motion/main.c's 4x4 grid
GRID_COLS = 4
GRID_ROWS = 4
GRID_CELLS = GRID_COLS * GRID_ROWS
STEP = 32
CAP = 255
THRESHOLD = 96
BATCH = 4


def resolve_csv(name):
    """A bare filename (e.g. "phone.csv") resolves alongside this script,
    same directory as the default capture; an absolute or already-valid
    relative path is used as given."""
    if os.path.isabs(name) or os.path.exists(name):
        return name
    candidate = os.path.join(os.path.dirname(__file__), name)
    return candidate if os.path.exists(candidate) else name


def load_events(csv_path):
    xs, ys, pols, les = [], [], [], []
    with open(csv_path) as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            le, x, y, pol = int(row[0]), int(row[1]), int(row[2]), int(row[3])
            xs.append(x); ys.append(y); pols.append(pol); les.append(le)
    return xs, ys, pols, les


def unpack_status(word):
    motion = (word >> 14) & 0x1
    val = (word >> 6) & 0xFF
    row = (word >> 3) & 0x7
    col = word & 0x7
    return col, row, val, motion


class MotionGrid:
    """Mirrors software/dvs_motion/main.c's isr_handler grid math exactly,
    one step() call per BATCH-sized group of events -- see that file's
    header for the decay/accumulate/argmax algorithm this reproduces."""

    def __init__(self):
        self.grid = [0] * GRID_CELLS

    def step(self, xs, ys):
        self.grid = [g - (g >> 1) for g in self.grid]
        for x, y in zip(xs, ys):
            col = int(x) >> CELL_SHIFT
            row = int(y) >> CELL_SHIFT
            cell = (row << 2) | col
            self.grid[cell] = min(self.grid[cell] + STEP, CAP)
        best_cell = max(range(GRID_CELLS), key=lambda c: self.grid[c])
        best_val = self.grid[best_cell]
        best_col = best_cell & 0x3
        best_row = best_cell >> 2
        motion = 1 if best_val >= THRESHOLD else 0
        return best_col, best_row, best_val, motion


class FineTrail:
    """Full sensor-resolution motion trail, independent of the real chip's
    coarse 4x4 grid entirely, at the sensor's own 126x112 resolution.

    Real consecutive AER events in a recording land ~38px apart on average
    (checked against dvs_capture_20260714_151049.csv directly) -- nothing
    like a locally-continuous edge trace -- so a single-pixel stamp that
    resets straight to full brightness either fades before a nearby event
    ever reinforces it (decay too fast: isolated dying dots) or, once
    persistence is long enough to let events accumulate, saturates a large
    area solid white the moment enough of those low-radius stamps overlap
    (a "fireball", not a shape). Both were tried and rejected empirically.

    What actually reveals a legible region: additive, capped accumulation
    over a small splat radius. A pixel only gets bright if it's repeatedly
    re-hit -- which is what a real moving edge sweeping back and forth does
    -- while a one-off stray/noise event stays dim and decays away. This
    can't recover fine detail (e.g. individual fingers) that the recording's
    own event order doesn't locally trace -- that's a property of the
    capture, not something reachable by further tuning this class -- but it
    does turn "which pixels have been hit repeatedly, recently" into a
    genuine blob-shaped region instead of noise."""

    def __init__(self, decay, radius, add):
        self.decay = decay
        self.radius = radius
        self.add = add
        self.acc = np.zeros((SY, SX), dtype=np.float32)

    def step(self, cv2, xs, ys):
        self.acc *= self.decay
        for x, y in zip(xs, ys):
            xi, yi = int(x), int(y)
            if 0 <= xi < SX and 0 <= yi < SY:
                mask = np.zeros_like(self.acc, dtype=np.uint8)
                cv2.circle(mask, (xi, yi), self.radius, 1, -1)
                self.acc = np.where(mask > 0, np.minimum(self.acc + self.add, 1.0), self.acc)


def screen_blend(a, b):
    """Lightens a by b without ever exceeding white -- standard "screen"
    compositing, used here so a blurred bright copy adds bloom around a
    glowing cell instead of just washing it out."""
    a32, b32 = a.astype(np.int32), b.astype(np.int32)
    return (255 - (255 - a32) * (255 - b32) // 255).astype(np.uint8)


def render_glow(cv2, sensor_arr, motion, scale, gain):
    """sensor_arr: any 2D array in sensor space (y rows, x cols), coarse
    (MotionGrid's 4x4) or fine (FineTrail's full 126x112) -- resized to full
    sensor resolution either way, so the same pipeline renders both."""
    W, H = SY, SX  # matches dvs_motion_view.py / dvs_motion_live.py's display transform

    arr = np.clip(np.asarray(sensor_arr, dtype=np.float32) * gain, 0.0, 1.0)

    # Cubic upsample to full sensor resolution -- for a coarse grid this is
    # what turns hard cell edges into a soft blob shape; for an
    # already-full-resolution fine trail it's a no-op resize. Either way, a
    # small merge blur afterward blends neighboring pixels into a
    # continuous shape instead of isolated speckles (most visible on the
    # fine trail, where only a handful of exact pixels light up per batch).
    sensor = cv2.resize(arr, (SX, SY), interpolation=cv2.INTER_CUBIC)
    sensor = cv2.GaussianBlur(sensor, (0, 0), sigmaX=1.0)
    sensor = np.clip(sensor, 0.0, 1.0)

    # sensor space is (y,x); display space is (row=x, col=W-1-y), same swap
    # +flipx transform every other viewer here uses.
    disp = np.flip(sensor.T, axis=1)

    heat = (disp * 255).astype(np.uint8)
    color = cv2.applyColorMap(heat, cv2.COLORMAP_INFERNO)
    if not motion:
        color = (color.astype(np.float32) * 0.55).astype(np.uint8)

    bloom = cv2.GaussianBlur(color, (0, 0), sigmaX=max(1.0, scale * 1.2))
    glow = screen_blend(color, bloom)

    return cv2.resize(glow, (W * scale, H * scale), interpolation=cv2.INTER_LINEAR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV,
                     help="AER capture to replay -- a bare filename (e.g. phone.csv, stabilize.csv) "
                          "resolves alongside this script; default is the original recording")
    ap.add_argument("--fps", type=float, default=30.0, help="display refresh rate")
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--gain", type=float, default=1.6,
                     help="brightness multiplier on trail activity before the colormap -- a region "
                          "needs several repeated hits to reach 1.0 on its own (see --add), so this "
                          "makes moderately-active regions visible without needing that many hits")
    ap.add_argument("--decay", type=float, default=0.98,
                     help="per-batch fade multiplier on the fine per-pixel trail -- real events land "
                          "~38px apart on average, so this needs to be close to 1 (a long half-life) "
                          "for nearby events to ever overlap before fading; too low just flickers dots")
    ap.add_argument("--radius", type=int, default=2, help="per-event splat radius, in sensor pixels")
    ap.add_argument("--add", type=float, default=0.18,
                     help="brightness added (capped at 1.0) to a pixel each time an event re-hits it -- "
                          "additive+capped rather than reset-to-max, so only repeatedly-hit regions "
                          "(a real moving edge) go bright; one-off stray events stay dim and fade")
    ap.add_argument("--cross", default="riscv64-unknown-elf-", help="RISC-V cross-compiler prefix")
    args = ap.parse_args()

    try:
        import cv2
    except Exception as e:
        print("cv2 (GUI-enabled opencv-python) is required for the live view:", e)
        sys.exit(1)

    csv_path = resolve_csv(args.csv)
    print(f"loading recorded events for the grid mirror ({csv_path})...")
    xs, ys, pols, les = load_events(csv_path)
    n_events = (len(xs) // BATCH) * BATCH
    n_batches_total = n_events // BATCH
    print(f"{n_events} events ({n_batches_total} batches) in {csv_path}")

    print(f"writing {EVENTS_PATH} (actsim's input) ...")
    with open(EVENTS_PATH, "w") as f:
        for le in les[:n_events]:
            f.write(f"{le}\n")

    for stale in (RESULTS_PATH,):
        if os.path.exists(stale):
            os.remove(stale)

    print("rebuilding the dvs_motion ROM image...")
    subprocess.run(
        ["make", f"ROM_TEST=dvs_motion", f"CROSS={args.cross}", "file-registry"],
        cwd=REPO_ROOT, check=True,
    )

    print("launching actsim -- this is the live run, not a replay...")
    log_path = "/tmp/dvs_motion_blob_live_actsim.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["actsim", "-cnf=gen/file_registry.conf",
         "chips/fpga/tests/e2e/e2e_fpga_motion_capture.act", "e2e_fpga_motion_capture"],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=log_f, stderr=subprocess.STDOUT,
    )
    proc.stdin.write(b"cycle\nquit\n")
    proc.stdin.close()

    W, H = SY, SX
    mirror = MotionGrid()
    fine = FineTrail(decay=args.decay, radius=args.radius, add=args.add)
    grid_img = np.zeros((H * args.scale, W * args.scale, 3), np.uint8)
    last_motion = 0
    mismatches = 0

    cv2.namedWindow("DVS motion detector -- LIVE BLOB", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS motion detector -- LIVE BLOB", W * args.scale, H * args.scale)

    results_f = None
    words_seen = 0
    finished = False
    t_start = time.time()

    print("live blob view running (q to quit)...")
    try:
        while True:
            if results_f is None and os.path.exists(RESULTS_PATH):
                results_f = open(RESULTS_PATH, "r")

            new_words = []
            if results_f is not None:
                for line in results_f:
                    line = line.strip()
                    if line:
                        new_words.append(int(line))

            for w in new_words:
                b = words_seen
                ev = range(b * BATCH, b * BATCH + BATCH)
                batch_xs, batch_ys = [xs[i] for i in ev], [ys[i] for i in ev]

                mcol, mrow, mval, mmotion = mirror.step(batch_xs, batch_ys)
                rcol, rrow, rval, rmotion = unpack_status(w)
                if (mcol, mrow, mval, mmotion) != (rcol, rrow, rval, rmotion):
                    mismatches += 1
                    print(f"batch {b}: grid mirror {(mcol, mrow, mval, mmotion)} != "
                          f"real chip {(rcol, rrow, rval, rmotion)}")
                last_motion = rmotion

                fine.step(cv2, batch_xs, batch_ys)
                words_seen += 1

            if new_words:
                grid_img = render_glow(cv2, fine.acc, last_motion, args.scale, args.gain)

            if not finished and proc.poll() is not None:
                finished = True

            img = grid_img.copy()
            elapsed = time.time() - t_start
            status = "DONE" if (finished and words_seen >= n_batches_total) else "running"
            note = f"  mismatches={mismatches}" if mismatches else ""
            cv2.putText(img, f"batch {words_seen}/{n_batches_total}  t={elapsed:0.1f}s  actsim: {status}{note}",
                        (2, H * args.scale - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

            cv2.imshow("DVS motion detector -- LIVE BLOB", img)
            k = cv2.waitKey(max(1, int(1000 / args.fps))) & 0xff
            if k == ord('q'):
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        log_f.close()
        cv2.destroyAllWindows()

    print(f"processed {words_seen}/{n_batches_total} batches "
          f"({mismatches} grid-mirror mismatches); actsim log at {log_path}")


if __name__ == "__main__":
    main()
