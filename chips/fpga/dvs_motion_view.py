#!/usr/bin/env python3
"""Live view of a recorded AER capture (le,x,y,pol CSV, from kr260_capture.py
/ kr260_record.py) with the real chip's detected motion location overlaid.

By default the overlay is a host-side mirror of software/dvs_motion/main.c's
grid math (Python port, not actsim/hardware) -- fine for smooth continuous
playback, but not literally chip output.

--from-actsim motion_capture_results.mem switches the overlay to the *real*
result of chips/fpga/tests/e2e/e2e_fpga_motion_capture.act's actsim run: one
status word per BATCH=4 events, produced by the real simulated core running
software/dvs_motion/main.c's actual ISR (a decaying 8x8-cell activity grid,
16x16-pixel cells, shift-only indexing -- see that file's header). Word i
covers raw events [4*i, 4*i+4) -- so the box drawn for a given group of 4
events is provably the chip's own argmax-cell decision, not a
reimplementation.

Status word layout: bit14=motion flag, bits[13:6]=hottest cell's activity
(0-255), bits[5:3]=cell row, bits[2:0]=cell col (8x8 grid, 16px cells).

Rendering: raw events accumulate and decay like dvs_replay.py (ON=green,
OFF=red); the hottest cell is drawn as a bordered box, brighter/thicker and a
different color when the motion flag is set.

Usage: dvs_motion_view.py capture.csv [--from-actsim motion_capture_results.mem]
                           [--rate 200] [--fps 60] [--scale 6]
                           [--decay 0.88] [--loop]
                           [--headless] [--save out.png]
live keys: space=pause/resume, r=restart, q=quit
"""
import argparse
import numpy as np

SX, SY = 126, 112
CELL_SHIFT = 4
GRID_COLS = 8
GRID_ROWS = 8
GRID_CELLS = GRID_COLS * GRID_ROWS
STEP = 32
CAP = 255
THRESHOLD = 96
BATCH = 4


def load(path):
    d = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.int64)
    if d.ndim == 1:
        d = d[None, :]
    return d[:, 1], d[:, 2], d[:, 3]  # x, y, pol


def python_motion_words(x, y):
    """Mirrors software/dvs_motion/main.c's grid math exactly (Python port,
    not real chip/actsim output) -- one status word per BATCH=4 events."""
    grid = [0] * GRID_CELLS
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        grid = [g - (g >> 1) for g in grid]
        for i in range(b, b + BATCH):
            col = int(x[i]) >> CELL_SHIFT
            row = int(y[i]) >> CELL_SHIFT
            cell = (row << 3) | col
            grid[cell] = min(grid[cell] + STEP, CAP)
        best_cell = max(range(GRID_CELLS), key=lambda c: grid[c])
        best_val = grid[best_cell]
        best_col = best_cell & 0x7
        best_row = best_cell >> 3
        motion = 1 if best_val >= THRESHOLD else 0
        words.append((motion << 14) | (best_val << 6) | (best_row << 3) | best_col)
    return words


def unpack_status(word):
    motion = (word >> 14) & 0x1
    val = (word >> 6) & 0xFF
    row = (word >> 3) & 0x7
    col = word & 0x7
    return col, row, val, motion


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                     help="use real actsim-captured chip output (one packed status word per "
                          "line) for the overlay instead of recomputing it in Python")
    ap.add_argument("--rate", type=int, default=200, help="events injected per frame")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--decay", type=float, default=0.88)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--headless", action="store_true", help="accumulate all events, no window")
    ap.add_argument("--save", help="write the final frame to this PNG")
    args = ap.parse_args()

    x, y, pol = load(args.csv)
    n_batches = len(x) // BATCH
    x, y, pol = x[:n_batches * BATCH], y[:n_batches * BATCH], pol[:n_batches * BATCH]

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        n_batches = min(n_batches, len(words))
        x, y, pol = x[:n_batches * BATCH], y[:n_batches * BATCH], pol[:n_batches * BATCH]
        print(f"loaded {len(x)} events from {args.csv}, motion overlay from real actsim "
              f"capture {args.from_actsim} (NOT a Python reimplementation)")
    else:
        words = python_motion_words(x, y)
        print(f"loaded {len(x)} events from {args.csv} (motion overlay is a Python mirror "
              f"of the firmware's grid math, not real chip/actsim output)")

    n = len(x)
    W, H = SY, SX  # swap=True, flipx=True, flipy=False -- matches dvs_replay.py's rig

    acc = np.zeros((H, W, 3), np.float32)

    def stamp(i):
        xi, yi, pi = int(x[i]), int(y[i]), int(pol[i])
        if not (0 <= xi < SX and 0 <= yi < SY):
            return
        col, row = yi, xi
        col = W - 1 - col  # flipx
        if 0 <= col < W and 0 <= row < H:
            acc[row, col] = (0, 1, 0) if pi else (0, 0, 1)

    # Display-space geometry for a grid cell (col,row in sensor space -> a box
    # in display space, matching stamp()'s swap+flipx transform). A sensor
    # cell spans x in [col*16, col*16+16) and y in [row*16, row*16+16); in
    # display space that's rows [col*16, col*16+16) and columns (flipped)
    # [W-1-(row*16+16-1), W-1-row*16].
    def cell_box(col, row):
        x0, x1 = col << CELL_SHIFT, min((col << CELL_SHIFT) + (1 << CELL_SHIFT), SX)
        y0, y1 = row << CELL_SHIFT, min((row << CELL_SHIFT) + (1 << CELL_SHIFT), SY)
        disp_row0, disp_row1 = x0, x1
        disp_col0, disp_col1 = W - y1, W - y0
        return disp_col0, disp_row0, disp_col1, disp_row1

    def draw_box(img, word):
        # Camera-AF-style corner brackets instead of a plain thin rectangle --
        # a full-perimeter line in red/orange disappears against this
        # rendering's own red OFF-events. Bright yellow (motion) / cyan
        # (tracking, below threshold) don't occur anywhere else in the frame,
        # so they stay visible regardless of what's under them, and a
        # semi-transparent fill plus a labeled background keep the box
        # readable even over a dense event cluster.
        col, row, val, motion = unpack_status(word)
        c0, r0, c1, r1 = cell_box(col, row)
        x0, y0 = c0 * args.scale, r0 * args.scale
        x1, y1 = c1 * args.scale - 1, r1 * args.scale - 1

        color = (0, 255, 255) if motion else (255, 255, 0)  # BGR: yellow / cyan
        thickness = max(2, args.scale // 3)
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

    def to_u8(img):
        return (np.clip(img, 0, 1) * 255).astype(np.uint8)

    if args.headless or (args.save and args.rate <= 0):
        for i in range(n):
            stamp(i)
        img = to_u8(acc)
        if args.save:
            import cv2
            img_up = cv2.resize(img, (W * args.scale, H * args.scale), interpolation=cv2.INTER_NEAREST)
            if words:
                draw_box(img_up, words[-1])
            cv2.imwrite(args.save, img_up)
            print(f"wrote {args.save}")
        return

    try:
        import cv2
    except Exception as e:
        print("cv2 unavailable, falling back to --headless:", e)
        for i in range(n):
            stamp(i)
        if args.save:
            print("cv2 required to save PNG; skipping --save")
        return

    cv2.namedWindow("DVS motion detector", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS motion detector", W * args.scale, H * args.scale)
    i = 0
    paused = False
    print(f"replaying {n} events ({len(words)} status words) display {W}x{H}  "
          f"rate={args.rate} ev/frame  (space=pause r=restart q=quit)")
    try:
        while True:
            if not paused:
                acc *= args.decay
                for _ in range(args.rate):
                    if i >= n:
                        if args.loop:
                            i = 0
                            acc[:] = 0
                        else:
                            break
                    stamp(i)
                    i += 1
            img = to_u8(acc)
            img_up = cv2.resize(img, (W * args.scale, H * args.scale), interpolation=cv2.INTER_NEAREST)
            batch_idx = max(0, min(len(words) - 1, i // BATCH - 1))
            if words:
                draw_box(img_up, words[batch_idx])
            cv2.putText(img_up, f"{i}/{n}", (2, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.imshow("DVS motion detector", img_up)
            k = cv2.waitKey(max(1, int(1000 / args.fps))) & 0xff
            if k == ord('q'):
                break
            elif k == ord(' '):
                paused = not paused
            elif k == ord('r'):
                i = 0
                acc[:] = 0
    finally:
        cv2.destroyAllWindows()

    if args.save:
        img = to_u8(acc)
        img_up = cv2.resize(img, (W * args.scale, H * args.scale), interpolation=cv2.INTER_NEAREST)
        if words:
            draw_box(img_up, words[-1])
        cv2.imwrite(args.save, img_up)
        print(f"wrote {args.save}")


if __name__ == "__main__":
    main()
