#!/usr/bin/env python3
"""Live view of software/dvs_track/main.c's real, on-chip object tracker:
a single moving object's centroid (a multiply-free exponential moving
average -- see that file's header) and this-window bounding box, reported
once every DUMP_INTERVAL=64 batches (256 events) with a "locked" flag that
says whether there was enough activity in that window to trust the
reading. Unlike dvs_motion_blob_live.py's coarse single-hottest-cell
report or dvs_timesurface_live.py's per-cell recency map, this is a real
continuous (x, y) position estimate of whatever is moving -- the kind of
signal a downstream consumer (a servo loop keeping a camera centered on
the object, a cursor, a robot arm) could act on directly.

Same live-tailing setup as dvs_denoise_live.py: launches
chips/fpga/tests/e2e/e2e_fpga_track_capture.act's actsim run itself and
tails track_capture_results.mem as it grows, two words (status, then bbox
-- see software/dvs_track/main.c's header for the exact bit layout) at a
time.

Rendering: a single panel showing the raw event stream (green=ON,
red=OFF, exponential decay -- same as every other raw panel in this
project) with the real chip's tracker overlaid on top: a cyan rectangle
for the reported bounding box and a magenta crosshair at the reported
centroid, both drawn solid when the chip reports "locked" (enough events
this window to trust the reading) and dimmed/dashed-looking (drawn thin)
otherwise, so a viewer can see exactly when the tracker does and doesn't
trust its own output -- the same distinction the firmware itself makes
before a downstream consumer would act on it.

Usage: dvs_track_live.py [--csv phone.csv] [--fps 30] [--scale 6]
                          [--decay 0.88] [--cross PREFIX]
live keys: q=quit (kills the actsim run if still in progress)
"""
import argparse
import os
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from dvs_motion_blob_live import load_events, resolve_csv  # reused unchanged

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CSV = "phone.csv"
EVENTS_PATH = os.path.join(os.path.dirname(__file__), "rotate_capture_events.mem")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "track_capture_results.mem")

SX, SY = 126, 112
BATCH = 4
DUMP_INTERVAL = 64          # batches -- must match software/dvs_track/main.c
EVENTS_PER_DUMP = BATCH * DUMP_INTERVAL  # 256


def unpack_status(word):
    """Inverse of software/dvs_track/main.c's status-word packing:
    (locked<<24) | (cx<<16) | (cy<<8) | count."""
    locked = (word >> 24) & 0xFF
    cx = (word >> 16) & 0xFF
    cy = (word >> 8) & 0xFF
    count = word & 0xFF
    return bool(locked), cx, cy, count


def unpack_bbox(word):
    """Inverse of software/dvs_track/main.c's bbox-word packing:
    (min_x<<24) | (min_y<<16) | (max_x<<8) | max_y."""
    min_x = (word >> 24) & 0xFF
    min_y = (word >> 16) & 0xFF
    max_x = (word >> 8) & 0xFF
    max_y = word & 0xFF
    return min_x, min_y, max_x, max_y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV,
                     help="AER capture to replay -- a bare filename (e.g. phone.csv, stabilize.csv) "
                          "resolves alongside this script; default is a handheld recording with a "
                          "clear moving subject, good for showing the tracker lock on and follow it")
    ap.add_argument("--fps", type=float, default=30.0, help="display refresh rate")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--decay", type=float, default=0.88, help="raw-trail display accumulator decay")
    ap.add_argument("--cross", default="riscv64-unknown-elf-", help="RISC-V cross-compiler prefix")
    args = ap.parse_args()

    try:
        import cv2
    except Exception as e:
        print("cv2 (GUI-enabled opencv-python) is required for the live view:", e)
        sys.exit(1)

    csv_path = resolve_csv(args.csv)
    print(f"loading recorded events for actsim's input ({csv_path})...")
    xs, ys, pols, les = load_events(csv_path)
    n_events = (len(xs) // BATCH) * BATCH
    n_dumps_total = n_events // EVENTS_PER_DUMP
    print(f"{n_events} events ({n_dumps_total} dumps of {EVENTS_PER_DUMP} events each)")

    print(f"writing {EVENTS_PATH} (actsim's input) ...")
    with open(EVENTS_PATH, "w") as f:
        for le in les[:n_events]:
            f.write(f"{le}\n")

    for stale in (RESULTS_PATH,):
        if os.path.exists(stale):
            os.remove(stale)

    print("rebuilding the dvs_track ROM image...")
    subprocess.run(
        ["make", f"ROM_TEST=dvs_track", f"CROSS={args.cross}", "file-registry"],
        cwd=REPO_ROOT, check=True,
    )

    print("launching actsim -- this is the live run, not a replay...")
    log_path = "/tmp/dvs_track_live_actsim.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["actsim", "-cnf=gen/file_registry.conf",
         "chips/fpga/tests/e2e/e2e_fpga_track_capture.act", "e2e_fpga_track_capture"],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=log_f, stderr=subprocess.STDOUT,
    )
    proc.stdin.write(b"cycle\nquit\n")
    proc.stdin.close()

    # swap=True, flipx=True, flipy=False -- same default orientation as
    # dvs_replay.py / dvs_rotate_view.py's rig.
    W, H = SY, SX
    acc = np.zeros((H, W, 3), np.float32)

    def to_disp(xi, yi):
        """Sensor (x, y) -> display (col, row), matching stamp()'s transform below."""
        col = W - 1 - yi  # flipx
        row = xi
        return col, row

    def stamp(xi, yi, pi):
        if not (0 <= xi < SX and 0 <= yi < SY):
            return
        col, row = to_disp(xi, yi)
        if 0 <= col < W and 0 <= row < H:
            acc[row, col] = (0, 1, 0) if pi else (0, 0, 1)  # ON green, OFF red

    win_title = "DVS object tracker -- raw events + on-chip centroid/bbox (LIVE)"
    cv2.namedWindow(win_title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win_title, W * args.scale, H * args.scale)

    results_f = None
    pending = []
    words_seen = 0
    dumps_seen = 0
    finished = False
    last_status = (False, SX // 2, SY // 2, 0)
    last_bbox = (0, 0, 0, 0)
    t_start = time.time()

    print("live track view running (q to quit)...")
    try:
        while True:
            if results_f is None and os.path.exists(RESULTS_PATH):
                results_f = open(RESULTS_PATH, "r")

            if results_f is not None:
                for line in results_f:
                    line = line.strip()
                    if line:
                        pending.append(int(line))

            while len(pending) >= 2:
                status_word, bbox_word = pending[0], pending[1]
                pending = pending[2:]

                lo, hi = words_seen, min(words_seen + EVENTS_PER_DUMP, n_events)
                acc *= args.decay
                for i in range(lo, hi):
                    stamp(int(xs[i]), int(ys[i]), int(pols[i]))
                words_seen = hi

                last_status = unpack_status(status_word)
                last_bbox = unpack_bbox(bbox_word)
                dumps_seen += 1

            if not finished and proc.poll() is not None:
                finished = True

            img = (np.clip(acc, 0, 1) * 255).astype(np.uint8)
            img = cv2.resize(img, (W * args.scale, H * args.scale), interpolation=cv2.INTER_NEAREST)

            locked, cx, cy, count = last_status
            min_x, min_y, max_x, max_y = last_bbox
            thickness = 2 if locked else 1
            box_color = (255, 255, 0) if locked else (90, 90, 40)      # cyan when locked, dim otherwise
            cross_color = (255, 0, 255) if locked else (90, 30, 90)    # magenta when locked, dim otherwise

            (bx0, by0), (bx1, by1) = to_disp(min_x, min_y), to_disp(max_x, max_y)
            p0 = (min(bx0, bx1) * args.scale, min(by0, by1) * args.scale)
            p1 = ((max(bx0, bx1) + 1) * args.scale, (max(by0, by1) + 1) * args.scale)
            cv2.rectangle(img, p0, p1, box_color, thickness)

            ccol, crow = to_disp(cx, cy)
            ccx, ccy = ccol * args.scale + args.scale // 2, crow * args.scale + args.scale // 2
            r = max(4, args.scale)
            cv2.line(img, (ccx - r, ccy), (ccx + r, ccy), cross_color, thickness)
            cv2.line(img, (ccx, ccy - r), (ccx, ccy + r), cross_color, thickness)
            cv2.circle(img, (ccx, ccy), r, cross_color, thickness)

            elapsed = time.time() - t_start
            status = "DONE" if (finished and words_seen >= n_events) else "running"
            lock_note = f"LOCKED cx={cx} cy={cy} n={count}" if locked else f"searching... n={count}"
            cv2.putText(img, lock_note, (2, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
            cv2.putText(img, f"dump {dumps_seen}/{n_dumps_total}  events {words_seen}/{n_events}  "
                              f"t={elapsed:0.1f}s  actsim: {status}",
                        (2, H * args.scale - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

            cv2.imshow(win_title, img)
            k = cv2.waitKey(max(1, int(1000 / args.fps))) & 0xff
            if k == ord('q'):
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        log_f.close()
        cv2.destroyAllWindows()

    print(f"processed {dumps_seen}/{n_dumps_total} dumps ({words_seen}/{n_events} events); "
          f"actsim log at {log_path}")


if __name__ == "__main__":
    main()
