#!/usr/bin/env python3
"""Display a recorded AER event capture (le,x,y,pol CSV, from
kr260_capture.py / kr260_record.py) as a DVS-style accumulated image —
same rendering as aer_udp_viewer.py (ON=green, OFF=red, exponential decay).

kr260_capture.py records events in arrival order but without per-event
timestamps, so playback speed here is events-per-frame, not wall-clock time.

Usage: dvs_replay.py capture.csv [--rate 200] [--fps 60] [--scale 6]
                                  [--decay 0.88] [--loop]
                                  [--headless] [--save out.png]
live keys: space=pause/resume, r=restart, q=quit
Orientation flags (--no-swap/--flipx/--no-flipy) match aer_udp_viewer.py;
default matches the rig in the README (swap=True, flipx=True, flipy=False).
"""
import argparse
import numpy as np

SX, SY = 126, 112

def load(path):
    d = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.int64)
    if d.ndim == 1:
        d = d[None, :]
    return d[:, 1], d[:, 2], d[:, 3]   # x, y, pol

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--rate", type=int, default=200, help="events injected per frame")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--decay", type=float, default=0.88)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--headless", action="store_true", help="accumulate all events, no window")
    ap.add_argument("--save", help="write the final accumulated frame to this PNG")
    ap.add_argument("--no-swap", action="store_true")
    ap.add_argument("--flipx", action="store_true")
    ap.add_argument("--no-flipy", action="store_true")
    args = ap.parse_args()

    x, y, pol = load(args.csv)
    n = len(x)
    print(f"loaded {n} events from {args.csv}")

    swap  = not args.no_swap
    flipx = args.flipx
    flipy = not args.no_flipy
    W, H = (SY, SX) if swap else (SX, SY)

    acc = np.zeros((H, W, 3), np.float32)

    def stamp(i):
        xi, yi, pi = int(x[i]), int(y[i]), int(pol[i])
        if not (0 <= xi < SX and 0 <= yi < SY):
            return
        col, row = (yi, xi) if swap else (xi, yi)
        if flipx: col = W - 1 - col
        if flipy: row = H - 1 - row
        if 0 <= col < W and 0 <= row < H:
            acc[row, col] = (0, 1, 0) if pi else (0, 0, 1)   # ON green, OFF red

    if args.headless or (args.save and args.rate <= 0):
        for i in range(n):
            stamp(i)
        if args.save:
            import cv2
            cv2.imwrite(args.save, (np.clip(acc, 0, 1) * 255).astype(np.uint8))
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

    cv2.namedWindow("DVS replay", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS replay", W * args.scale, H * args.scale)
    i = 0
    paused = False
    print(f"replaying {n} events  display {W}x{H}  rate={args.rate} ev/frame  "
          f"swap={swap} flipx={flipx} flipy={flipy}  (space=pause r=restart q=quit)")
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
            img = (np.clip(acc, 0, 1) * 255).astype(np.uint8)
            cv2.putText(img, f"{i}/{n}", (2, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.imshow("DVS replay", img)
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
        cv2.imwrite(args.save, (np.clip(acc, 0, 1) * 255).astype(np.uint8))
        print(f"wrote {args.save}")

if __name__ == "__main__":
    main()
