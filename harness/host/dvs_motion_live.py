#!/usr/bin/env python3
"""Live view of the motion detector while actsim is still computing it.

dvs_motion_view.py's --from-actsim mode replays a *finished* capture file --
run the whole thing, then watch it back. This script instead launches
chips/fpga/tests/e2e/e2e_fpga_motion_capture.act's actsim run itself and
tails its output file (motion_capture_results.mem) as it grows, updating the
display as each new status word is written -- so you watch the simulator's
own progress happen, rather than a replay.

This is NOT real-time relative to the original recording or real hardware:
actsim is a functional/behavioral simulator, not a real-time system -- for
this capture, ~5.1ms of simulated circuit time takes actsim tens of seconds
of actual wall-clock compute (roughly a 1000-8000x slowdown), and this
particular recording has no per-event timestamps to pace against even if it
were faster. "Live" here means watching actsim's own progress as it
happens, at whatever pace actsim can compute -- not paced to any real clock.
Genuine real-time would mean running the synthesized design on the actual
KR260 FPGA (harness/), a separate hardware bring-up effort.

Launches its own actsim process (rebuilding the dvs_motion ROM image and
clearing any previous results file first) rather than tailing one you start
yourself, specifically to avoid two actsim processes writing the same
output file at once (confirmed to silently corrupt/truncate the result).

Usage: dvs_motion_live.py [--fps 30] [--scale 8] [--decay 0.90] [--cross PREFIX]
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
DATA_DIR = os.path.join(REPO_ROOT, "chips", "fpga")
CSV_PATH = os.path.join(DATA_DIR, "dvs_capture_20260714_151049.csv")
EVENTS_PATH = os.path.join(DATA_DIR, "rotate_capture_events.mem")
RESULTS_PATH = os.path.join(DATA_DIR, "motion_capture_results.mem")

SX, SY = 126, 112
CELL_SHIFT = 5  # 32x32-pixel cells -- must match software/dvs_motion/main.c's 4x4 grid
                # (126>>5=3, 112>>5=3): only used here for cell_box()'s pixel geometry, since
                # the status word's row/col fields (unpack_status below) are decoded directly
                # regardless of grid size
BATCH = 4


def load_events():
    xs, ys, pols, les = [], [], [], []
    with open(CSV_PATH) as f:
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


def cell_box(col, row, W):
    x0, x1 = col << CELL_SHIFT, min((col << CELL_SHIFT) + (1 << CELL_SHIFT), SX)
    y0, y1 = row << CELL_SHIFT, min((row << CELL_SHIFT) + (1 << CELL_SHIFT), SY)
    return W - y1, x0, W - y0, x1


def draw_box(cv2, img, word, scale):
    col, row, val, motion = unpack_status(word)
    c0, r0, c1, r1 = cell_box(col, row, SY)
    x0, y0 = c0 * scale, r0 * scale
    x1, y1 = c1 * scale - 1, r1 * scale - 1
    color = (0, 255, 255) if motion else (255, 255, 0)  # BGR: yellow / cyan
    thickness = max(2, scale // 3)
    arm = max(6, (x1 - x0) // 3)
    if motion:
        overlay = img.copy()
        cv2.rectangle(overlay, (x0, y0), (x1, y1), color, -1)
        cv2.addWeighted(overlay, 0.25, img, 0.75, 0, dst=img)
    for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        cv2.line(img, (cx, cy), (cx + dx * arm, cy), color, thickness)
        cv2.line(img, (cx, cy), (cx, cy + dy * arm), color, thickness)
    label = f"MOTION val={val}" if motion else f"val={val}"
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
    ly = max(th + 4, y0 - 4)
    cv2.rectangle(img, (x0, ly - th - 4), (x0 + tw + 4, ly + 2), (0, 0, 0), -1)
    cv2.putText(img, label, (x0 + 2, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=float, default=30.0, help="display refresh rate")
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--half-life", type=float, default=3.0,
                     help="seconds for an event's brightness to fade to half -- actsim only "
                          "reveals ~240 events/sec here (vs. dvs_motion_view.py's ~12,000 "
                          "events/sec replay of a finished capture), so persistence is a real "
                          "wall-clock half-life rather than a fixed per-tick multiplier: at this "
                          "much slower arrival rate, a per-tick decay tuned for the fast replay "
                          "would fade events out almost as soon as they appear")
    ap.add_argument("--cross", default="riscv64-unknown-elf-", help="RISC-V cross-compiler prefix")
    args = ap.parse_args()

    try:
        import cv2
    except Exception as e:
        print("cv2 (GUI-enabled opencv-python) is required for the live view:", e)
        sys.exit(1)

    print("loading recorded events for the raw panel...")
    xs, ys, pols, les = load_events()
    n_events = (len(xs) // BATCH) * BATCH
    n_batches_total = n_events // BATCH
    print(f"{n_events} events ({n_batches_total} batches) in {CSV_PATH}")

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
    log_path = "/tmp/dvs_motion_live_actsim.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["actsim", "-cnf=gen/file_registry.conf",
         "chips/fpga/tests/e2e/e2e_fpga_motion_capture.act", "e2e_fpga_motion_capture"],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=log_f, stderr=subprocess.STDOUT,
    )
    proc.stdin.write(b"cycle\nquit\n")
    proc.stdin.close()

    W, H = SY, SX
    acc = np.zeros((H, W, 3), np.float32)

    def stamp(i):
        xi, yi, pi = xs[i], ys[i], pols[i]
        if not (0 <= xi < SX and 0 <= yi < SY):
            return
        col, row = yi, xi
        col = W - 1 - col
        if 0 <= col < W and 0 <= row < H:
            acc[row, col] = (0, 1, 0) if pi else (0, 0, 1)

    cv2.namedWindow("DVS motion detector -- LIVE", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS motion detector -- LIVE", W * args.scale, H * args.scale)

    results_f = None
    words_seen = 0
    events_stamped = 0
    last_word = None
    finished = False
    t_start = time.time()
    last_tick = t_start

    print("live view running (q to quit)...")
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

            # Decay once per displayed frame (matching dvs_replay.py /
            # dvs_rotate_view.py / dvs_motion_view.py's convention), not once
            # per incoming word -- a word only covers BATCH=4 events. Decay is
            # a real wall-clock half-life, not a fixed per-tick multiplier --
            # see --half-life's help for why (this viewer's true event
            # arrival rate is ~50x slower than the finished-capture replay).
            now = time.time()
            dt = now - last_tick
            last_tick = now
            acc *= 0.5 ** (dt / args.half_life)
            for w in new_words:
                target = min(n_events, (words_seen + 1) * BATCH)
                while events_stamped < target:
                    stamp(events_stamped)
                    events_stamped += 1
                last_word = w
                words_seen += 1

            if not finished and proc.poll() is not None:
                finished = True

            img = (np.clip(acc, 0, 1) * 255).astype(np.uint8)
            img_up = cv2.resize(img, (W * args.scale, H * args.scale), interpolation=cv2.INTER_NEAREST)
            if last_word is not None:
                draw_box(cv2, img_up, last_word, args.scale)

            elapsed = time.time() - t_start
            status = "DONE" if (finished and words_seen >= n_batches_total) else "running"
            cv2.putText(img_up, f"batch {words_seen}/{n_batches_total}  t={elapsed:0.1f}s  actsim: {status}",
                        (2, H * args.scale - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

            cv2.imshow("DVS motion detector -- LIVE", img_up)
            k = cv2.waitKey(max(1, int(1000 / args.fps))) & 0xff
            if k == ord('q'):
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        log_f.close()
        cv2.destroyAllWindows()

    print(f"processed {words_seen}/{n_batches_total} batches; actsim log at {log_path}")


if __name__ == "__main__":
    main()
