#!/usr/bin/env python3
"""Live view of software/dvs_timesurface/main.c's real per-cell time surface
-- unlike dvs_motion_blob_live.py's FineTrail (a Python-side mirror built
from raw event (x,y), since the wire protocol there only ever carried one
argmax cell per batch), every pixel rendered here comes directly from the
real simulated chip: isr_handler stamps its own running event counter into
whichever of its 32x28 cells (4x4-pixel, 8x finer per axis than
dvs_motion's 4x4 grid) each event lands in, and dumps the whole grid out
over the output FIFO every DUMP_INTERVAL=64 batches (256 events). See
software/dvs_timesurface/main.c's header for why: no timer/cycle-counter
peripheral exists on this SoC, and the AER word's own ts[16:0] field is
always 0 in every capture this project has, so the firmware's own running
event counter stands in for "now" -- decay is computed from elapsed = now
- last_seen, which only needs relative order, not wall-clock time.

Same live-tailing setup as dvs_motion_live.py: launches
chips/fpga/tests/e2e/e2e_fpga_timesurface_capture.act's actsim run itself
and tails timesurface_capture_results.mem as it grows, one full 897-word
dump (now + GRID_CELLS=896 cells, row-major) at a time -- so this watches
actsim's own progress happen, not a paced replay (see dvs_motion_live.py's
header for why "live" doesn't mean real-time here).

Rendering: black canvas, no raw event dots -- reuses dvs_motion_blob_live's
render_glow pipeline unchanged (cubic upsample, merge blur, INFERNO
colormap, bloom), fed a 32x28 array of per-cell decay (elapsed = now -
last_seen, exponential falloff) computed straight from the real dump, no
client-side event replay or mirroring involved at all.

Usage: dvs_timesurface_live.py [--csv phone.csv] [--fps 30] [--scale 8] [--gain 1.6] [--tau 150] [--cross PREFIX]
live keys: q=quit (kills the actsim run if still in progress)
"""
import argparse
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from dvs_motion_blob_live import render_glow, resolve_csv  # reused unchanged

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CSV = "dvs_capture_20260714_151049.csv"
EVENTS_PATH = os.path.join(os.path.dirname(__file__), "rotate_capture_events.mem")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "timesurface_capture_results.mem")

SX, SY = 126, 112
GRID_COLS = 32
GRID_ROWS = 28
GRID_CELLS = GRID_COLS * GRID_ROWS   # = 896
DUMP_WORDS = 1 + GRID_CELLS          # "now" + one word per cell
BATCH = 4
DUMP_INTERVAL = 64                   # batches between dumps -- must match main.c's DUMP_INTERVAL


def load_les(csv_path):
    les = []
    import csv
    with open(csv_path) as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            les.append(int(row[0]))
    return les


def decay_grid(now, last_seen, tau):
    """elapsed = now - last_seen, per cell; a cell with last_seen==0 has
    never been hit (not just hit long ago) and renders as cold regardless
    of how large `now` has grown -- exponential falloff otherwise."""
    last_seen = np.asarray(last_seen, dtype=np.float64)
    never_hit = last_seen == 0
    elapsed = now - last_seen
    decay = np.exp(-elapsed / tau)
    decay[never_hit] = 0.0
    return decay.reshape(GRID_ROWS, GRID_COLS).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV,
                     help="AER capture to replay -- a bare filename (e.g. phone.csv, stabilize.csv) "
                          "resolves alongside this script; default is the original recording")
    ap.add_argument("--fps", type=float, default=30.0, help="display refresh rate")
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--gain", type=float, default=1.6, help="brightness multiplier before the colormap")
    ap.add_argument("--tau", type=float, default=150.0,
                     help="exponential decay time constant, in events -- a cell hit `tau` events ago "
                          "reads at ~37%% brightness; DUMP_INTERVAL*BATCH=256 events land between "
                          "dumps, so values well under that make each dump look mostly like only its "
                          "own newest events, well over it blends many dumps' worth of history")
    ap.add_argument("--cross", default="riscv64-unknown-elf-", help="RISC-V cross-compiler prefix")
    args = ap.parse_args()

    try:
        import cv2
    except Exception as e:
        print("cv2 (GUI-enabled opencv-python) is required for the live view:", e)
        sys.exit(1)

    csv_path = resolve_csv(args.csv)
    print(f"loading recorded events for actsim's input file ({csv_path})...")
    les = load_les(csv_path)
    n_events = (len(les) // BATCH) * BATCH
    n_batches_total = n_events // BATCH
    n_dumps_total = n_batches_total // DUMP_INTERVAL
    print(f"{n_events} events ({n_batches_total} batches, {n_dumps_total} dumps) in {csv_path}")

    print(f"writing {EVENTS_PATH} (actsim's input) ...")
    with open(EVENTS_PATH, "w") as f:
        for le in les[:n_events]:
            f.write(f"{le}\n")

    for stale in (RESULTS_PATH,):
        if os.path.exists(stale):
            os.remove(stale)

    print("rebuilding the dvs_timesurface ROM image...")
    subprocess.run(
        ["make", f"ROM_TEST=dvs_timesurface", f"CROSS={args.cross}", "file-registry"],
        cwd=REPO_ROOT, check=True,
    )

    print("launching actsim -- this is the live run, not a replay...")
    log_path = "/tmp/dvs_timesurface_live_actsim.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["actsim", "-cnf=gen/file_registry.conf",
         "chips/fpga/tests/e2e/e2e_fpga_timesurface_capture.act", "e2e_fpga_timesurface_capture"],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=log_f, stderr=subprocess.STDOUT,
    )
    proc.stdin.write(b"cycle\nquit\n")
    proc.stdin.close()

    W, H = SY, SX
    grid_img = np.zeros((H * args.scale, W * args.scale, 3), np.uint8)
    now = 0

    cv2.namedWindow("DVS time surface -- LIVE", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS time surface -- LIVE", W * args.scale, H * args.scale)

    results_f = None
    pending = []   # words not yet forming a complete DUMP_WORDS-sized dump
    dumps_seen = 0
    finished = False
    t_start = time.time()

    print("live time-surface view running (q to quit)...")
    try:
        while True:
            if results_f is None and os.path.exists(RESULTS_PATH):
                results_f = open(RESULTS_PATH, "r")

            if results_f is not None:
                for line in results_f:
                    line = line.strip()
                    if line:
                        pending.append(int(line))

            new_dump = False
            while len(pending) >= DUMP_WORDS:
                now = pending[0]
                last_seen = pending[1:DUMP_WORDS]
                pending = pending[DUMP_WORDS:]
                dumps_seen += 1
                new_dump = True

            if new_dump:
                decay = decay_grid(now, last_seen, args.tau)
                grid_img = render_glow(cv2, decay, motion=1, scale=args.scale, gain=args.gain)

            if not finished and proc.poll() is not None:
                finished = True

            img = grid_img.copy()
            elapsed = time.time() - t_start
            status = "DONE" if (finished and dumps_seen >= n_dumps_total) else "running"
            cv2.putText(img, f"dump {dumps_seen}/{n_dumps_total}  now={now}  t={elapsed:0.1f}s  actsim: {status}",
                        (2, H * args.scale - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

            cv2.imshow("DVS time surface -- LIVE", img)
            k = cv2.waitKey(max(1, int(1000 / args.fps))) & 0xff
            if k == ord('q'):
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        log_f.close()
        cv2.destroyAllWindows()

    print(f"processed {dumps_seen}/{n_dumps_total} dumps; actsim log at {log_path}")


if __name__ == "__main__":
    main()
