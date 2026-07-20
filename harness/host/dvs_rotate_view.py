#!/usr/bin/env python3
"""Live side-by-side view of a recorded AER capture (le,x,y,pol CSV, from
kr260_capture.py / kr260_record.py) next to the same events rotated 45
degrees.

By default the rotated panel is a host-side mirror of software/dvs_rotate/
main.c's multiply-free shift rotation (Python port, not actsim/hardware) --
fine for smooth continuous playback, but not literally chip output.

--from-actsim rotate_capture_results.mem switches the rotated panel to the
*real* result of chips/fpga/tests/e2e/e2e_fpga_rotate_capture.act's actsim
run: that file is the real simulated core (running software/dvs_rotate/
main.c's actual ISR) processing chips/fpga/rotate_capture_events.mem one
event at a time, one rotated result per line, in order -- the full 61752-event
recording, not a truncated sample (see e2e_fpga_rotate_capture.act's header
for the stack-leak bug that used to cap this well below the full recording)
-- so what's on screen is provably the chip's own output, not a
reimplementation.

Rendering matches dvs_replay.py (ON=green, OFF=red, exponential decay);
left panel is the raw capture, right panel is the rotated copy, decayed and
looped in lockstep.

Usage: dvs_rotate_view.py capture.csv [--from-actsim rotate_capture_results.mem]
                           [--rate 200] [--fps 60] [--scale 6]
                           [--decay 0.88] [--loop]
                           [--headless] [--save out.png]
live keys: space=pause/resume, r=restart, q=quit
"""
import argparse
import numpy as np

SX, SY = 126, 112
CX, CY = SX // 2, SY // 2


def load(path):
    d = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.int64)
    if d.ndim == 1:
        d = d[None, :]
    return d[:, 1], d[:, 2], d[:, 3]  # x, y, pol


def unpack(words):
    """Inverse of evt_pack.v's low 15 bits: {pol, y[6:0], x[6:0]}."""
    words = np.asarray(words, dtype=np.int64)
    x = words & 0x7F
    y = (words >> 7) & 0x7F
    pol = (words >> 14) & 0x1
    return x, y, pol


def rotate45(x, y):
    """Mirrors software/dvs_rotate/main.c's rotate45(): multiply-free
    45-degree rotation (no RV32M on the real core), so this is a Python
    port of the exact same integer shifts, not a "true" cos45/sin45
    rotation -- see that file's header for why."""
    tx, ty = x - CX, y - CY
    rx = (tx - ty) >> 1
    ry = (tx + ty) >> 1
    nx = np.clip(rx + CX, 0, SX - 1)
    ny = np.clip(ry + CY, 0, SY - 1)
    return nx, ny


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                     help="use real actsim-captured chip output (one packed word per "
                          "line) for the rotated panel instead of recomputing it in Python")
    ap.add_argument("--rate", type=int, default=200, help="events injected per frame")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--decay", type=float, default=0.88)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--headless", action="store_true", help="accumulate all events, no window")
    ap.add_argument("--save", help="write the final side-by-side frame to this PNG")
    args = ap.parse_args()

    x, y, pol = load(args.csv)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            results = [int(line) for line in f if line.strip()]
        n = len(results)
        x, y, pol = x[:n], y[:n], pol[:n]
        rx, ry, _ = unpack(results)
        print(f"loaded {n} events from {args.csv}, rotated panel from real actsim "
              f"capture {args.from_actsim} (NOT a Python reimplementation)")
    else:
        n = len(x)
        rx, ry = rotate45(x, y)
        print(f"loaded {n} events from {args.csv} (rotated panel is a Python mirror "
              f"of the firmware math, not real chip/actsim output)")

    # swap=True, flipx=True, flipy=False -- same default orientation as
    # dvs_replay.py's rig.
    W, H = SY, SX
    gap = max(2, args.scale // 3)

    acc_raw = np.zeros((H, W, 3), np.float32)
    acc_rot = np.zeros((H, W, 3), np.float32)

    def stamp(acc, xi, yi, pi):
        if not (0 <= xi < SX and 0 <= yi < SY):
            return
        col, row = yi, xi
        col = W - 1 - col  # flipx
        if 0 <= col < W and 0 <= row < H:
            acc[row, col] = (0, 1, 0) if pi else (0, 0, 1)  # ON green, OFF red

    def stamp_i(i):
        xi, yi, pi = int(x[i]), int(y[i]), int(pol[i])
        stamp(acc_raw, xi, yi, pi)
        stamp(acc_rot, int(rx[i]), int(ry[i]), pi)

    def side_by_side():
        sep = np.full((H, gap, 3), 0.15, np.float32)
        return np.concatenate([acc_raw, sep, acc_rot], axis=1)

    def to_u8(img):
        return (np.clip(img, 0, 1) * 255).astype(np.uint8)

    if args.headless or (args.save and args.rate <= 0):
        for i in range(n):
            stamp_i(i)
        if args.save:
            import cv2
            cv2.imwrite(args.save, to_u8(side_by_side()))
            print(f"wrote {args.save}")
        return

    try:
        import cv2
    except Exception as e:
        print("cv2 unavailable, falling back to --headless:", e)
        for i in range(n):
            stamp_i(i)
        if args.save:
            print("cv2 required to save PNG; skipping --save")
        return

    total_w = W * 2 + gap
    cv2.namedWindow("DVS rotate-45 view: raw | rotated", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS rotate-45 view: raw | rotated", total_w * args.scale, H * args.scale)
    i = 0
    paused = False
    print(f"replaying {n} events  display {W}x{H} x2  rate={args.rate} ev/frame  "
          f"(space=pause r=restart q=quit)")
    try:
        while True:
            if not paused:
                acc_raw *= args.decay
                acc_rot *= args.decay
                for _ in range(args.rate):
                    if i >= n:
                        if args.loop:
                            i = 0
                            acc_raw[:] = 0
                            acc_rot[:] = 0
                        else:
                            break
                    stamp_i(i)
                    i += 1
            img = to_u8(side_by_side())
            cv2.putText(img, f"raw {i}/{n}", (2, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(img, "rotated 45", (W + gap + 2, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.imshow("DVS rotate-45 view: raw | rotated", img)
            k = cv2.waitKey(max(1, int(1000 / args.fps))) & 0xff
            if k == ord('q'):
                break
            elif k == ord(' '):
                paused = not paused
            elif k == ord('r'):
                i = 0
                acc_raw[:] = 0
                acc_rot[:] = 0
    finally:
        cv2.destroyAllWindows()

    if args.save:
        cv2.imwrite(args.save, to_u8(side_by_side()))
        print(f"wrote {args.save}")


if __name__ == "__main__":
    main()
