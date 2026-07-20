#!/usr/bin/env python3
"""Live view of software/dvs_denoise/main.c's real, firmware-denoised time
surface -- the same 32x28-cell mechanism as dvs_timesurface_live.py, but
every pixel rendered here comes from signal_seen[], not raw last_seen[]:
isr_handler only stamps a cell if at least one of its 4 grid-adjacent
neighbors (or the cell itself) was touched by ANY event within the last
CORRELATION_WINDOW=25 events -- a real moving edge lights up a
neighborhood of cells close together in time, so it passes; an isolated
background-activity-noise event, with no correlated neighbor, doesn't. See
software/dvs_denoise/main.c's header for the full filter design and why
CORRELATION_WINDOW=25 was picked (empirically, against
chips/fpga/data/dvs_capture_20260714_151049.csv -- smaller values fragment real
motion, larger values let noise back in).

Same live-tailing setup as dvs_timesurface_live.py: launches
chips/fpga/tests/e2e/e2e_fpga_denoise_capture.act's actsim run itself and
tails denoise_capture_results.mem as it grows, one full 897-word dump (now
+ GRID_CELLS=896 cells, row-major) at a time.

Rendering: identical pipeline to dvs_timesurface_live.py/dvs_motion_blob_live's
render_glow (cubic upsample, merge blur, INFERNO colormap, bloom) fed the
same exponential-decay-from-elapsed-events transform -- the only difference
from dvs_timesurface_live.py is which array the firmware dumped.

Usage: dvs_denoise_live.py [--csv phone.csv] [--fps 30] [--scale 8] [--gain 1.6] [--tau 150] [--cross PREFIX]
live keys: q=quit (kills the actsim run if still in progress)

Note: CORRELATION_WINDOW=25 in software/dvs_denoise/main.c was tuned
empirically against the default recording specifically -- it's baked into
the compiled firmware, not adjustable here, so a very differently-paced
capture (denser or sparser events) may not denoise as cleanly until that
constant gets retuned against it too.
"""
import argparse
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from dvs_motion_blob_live import render_glow, resolve_csv  # reused unchanged
from dvs_timesurface_live import DEFAULT_CSV, decay_grid, load_les  # same math, same CSV loader

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(REPO_ROOT, "chips", "fpga")
EVENTS_PATH = os.path.join(DATA_DIR, "rotate_capture_events.mem")
RESULTS_PATH = os.path.join(DATA_DIR, "denoise_capture_results.mem")

SX, SY = 126, 112
GRID_COLS = 32
GRID_ROWS = 28
GRID_CELLS = GRID_COLS * GRID_ROWS
DUMP_WORDS = 1 + GRID_CELLS
BATCH = 4
DUMP_INTERVAL = 64


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV,
                     help="AER capture to replay -- a bare filename (e.g. phone.csv, stabilize.csv) "
                          "resolves alongside this script; default is the original recording")
    ap.add_argument("--fps", type=float, default=30.0, help="display refresh rate")
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--gain", type=float, default=1.6, help="brightness multiplier before the colormap")
    ap.add_argument("--tau", type=float, default=150.0,
                     help="exponential decay time constant, in events -- see dvs_timesurface_live.py's "
                          "--tau help for the full explanation, unchanged here")
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
    print(f"{n_events} events ({n_batches_total} batches, {n_dumps_total} dumps)")

    print(f"writing {EVENTS_PATH} (actsim's input) ...")
    with open(EVENTS_PATH, "w") as f:
        for le in les[:n_events]:
            f.write(f"{le}\n")

    for stale in (RESULTS_PATH,):
        if os.path.exists(stale):
            os.remove(stale)

    print("rebuilding the dvs_denoise ROM image...")
    subprocess.run(
        ["make", f"ROM_TEST=dvs_denoise", f"CROSS={args.cross}", "file-registry"],
        cwd=REPO_ROOT, check=True,
    )

    print("launching actsim -- this is the live run, not a replay...")
    log_path = "/tmp/dvs_denoise_live_actsim.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["actsim", "-cnf=gen/file_registry.conf",
         "chips/fpga/tests/e2e/e2e_fpga_denoise_capture.act", "e2e_fpga_denoise_capture"],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=log_f, stderr=subprocess.STDOUT,
    )
    proc.stdin.write(b"cycle\nquit\n")
    proc.stdin.close()

    W, H = SY, SX
    grid_img = np.zeros((H * args.scale, W * args.scale, 3), np.uint8)
    now = 0

    cv2.namedWindow("DVS denoised time surface -- LIVE", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS denoised time surface -- LIVE", W * args.scale, H * args.scale)

    results_f = None
    pending = []
    dumps_seen = 0
    finished = False
    t_start = time.time()

    print("live denoised view running (q to quit)...")
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
                signal_seen = pending[1:DUMP_WORDS]
                pending = pending[DUMP_WORDS:]
                dumps_seen += 1
                new_dump = True

            if new_dump:
                decay = decay_grid(now, signal_seen, args.tau)
                grid_img = render_glow(cv2, decay, motion=1, scale=args.scale, gain=args.gain)

            if not finished and proc.poll() is not None:
                finished = True

            img = grid_img.copy()
            elapsed = time.time() - t_start
            status = "DONE" if (finished and dumps_seen >= n_dumps_total) else "running"
            cv2.putText(img, f"dump {dumps_seen}/{n_dumps_total}  now={now}  t={elapsed:0.1f}s  actsim: {status}",
                        (2, H * args.scale - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

            cv2.imshow("DVS denoised time surface -- LIVE", img)
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
