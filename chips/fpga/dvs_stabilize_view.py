#!/usr/bin/env python3
"""Estimate + visualise the GLOBAL BACKGROUND-MOTION vector of a recorded AER
capture (le,x,y,pol CSV, from kr260_capture.py / kr260_record.py), for scene
stabilization -- a host-side mirror of software/dvs_stabilize/main.c's
time-surface flow estimator (Python port, not actsim/hardware, unless
--from-actsim is given).

The algorithm is the complement of the object-motion work: instead of
suppressing the coherent background flow to find an object, it REPORTS that
flow as a 2-DOF vector (acc_dx, acc_dy) a stabilizer would subtract.

python_stabilize_words() below is a BIT-EXACT port of the firmware's ISR:
  * an OPTIONAL spatio-temporal correlation noise gate (CORR_MIN default 2, as
    in main.c): an event feeds the surface/votes only if >= CORR_MIN of its 8
    super-pixel neighbours fired within the last CORR_WINDOW events (hot-pixel/
    background rejection). Set CORR_MIN=0 to mirror a -DCORR_MIN=0 firmware build;
  * a downsampled (>>TS_SHIFT) time surface T holding a per-super-pixel byte
    "recency" (a monotonic per-event counter >> TICK_SHIFT -- the recording has
    no per-event timestamp, so arrival order IS the time order);
  * per event, the more-recently-updated of {left,right}/{up,down} neighbours
    says which side the moving edge came from -> a unit vote per axis (integer
    compares only, no gradient/atan2/multiply -- RV32I has no mul/div);
  * per BATCH=4 events, halve-decay the running vector then add the batch votes,
    and pack sign+magnitude+octant+coarse-magnitude into one word.
See software/dvs_stabilize/main.c's header for the full derivation, the
aperture/translation-only/foreground limitations, and every no-mul workaround.

--from-actsim stabilize_capture_results.mem switches the overlay to the *real*
actsim run of chips/fpga/tests/e2e/e2e_fpga_stabilize_capture.act (one packed
word per BATCH, real simulated core running the actual ISR) instead of this
Python reimplementation.

Output word layout (unpack_status):
  bit15   = sign of acc_dx (1=neg)     bits14..11 = |acc_dx| clamped 0..15
  bit10   = sign of acc_dy (1=neg)     bits9..6   = |acc_dy| clamped 0..15
  bits5..3= 8-octant dir (0=E,1=NE,2=N,3=NW,4=W,5=SW,6=S,7=SE; 7+mag0=still)
  bits2..0= coarse Chebyshev magnitude 0..7

Usage: dvs_stabilize_view.py capture.csv
           [--from-actsim stabilize_capture_results.mem]
           [--rate 200] [--fps 60] [--scale 6] [--decay 0.88] [--loop]
           [--headless] [--save out.png] [--report N] [--plot out.png]
live keys: space=pause/resume, r=restart, q=quit
"""
import argparse
import numpy as np

SX, SY = 126, 112

# --- must match software/dvs_stabilize/main.c exactly ---
TS_SHIFT = 1
TW = (SX + (1 << TS_SHIFT) - 1) >> TS_SHIFT       # 63
TH = (SY + (1 << TS_SHIFT) - 1) >> TS_SHIFT       # 56
TW_LOG2 = 6
TW_P2 = 1 << TW_LOG2                              # 64
TS_CELLS = TW_P2 * TH                             # 3584
TICK_SHIFT = 4
BATCH = 4
DXY_MAX = 15
MAG_MAX = 7
# Optional spatio-temporal correlation noise gate -- matches main.c's default
# (CORR_MIN=2, CORR_WINDOW=30). Set CORR_MIN=0 to mirror a firmware built with
# -DCORR_MIN=0. Kept in lock-step so the e2e baked words stay bit-exact.
CORR_WINDOW = 30
CORR_MIN = 2
OCT_NAMES = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]


def load(path):
    d = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.int64)
    if d.ndim == 1:
        d = d[None, :]
    return d[:, 1], d[:, 2], d[:, 3]  # x, y, pol


def _absi(v):
    return -v if v < 0 else v


def _clamp_mag(v, cap):
    a = _absi(v)
    return cap if a > cap else a


def octant(dx, dy):
    """8-octant code from (dx,dy), compares only (mirrors main.c's octant())."""
    if dx == 0 and dy == 0:
        return 7
    ax, ay = _absi(dx), _absi(dy)
    diagonal = (ax <= (ay << 1)) and (ay <= (ax << 1))
    if not diagonal:
        if ax >= ay:
            return 0 if dx >= 0 else 4      # E : W
        return 2 if dy < 0 else 6           # N : S  (dy<0 = up = North)
    if dx >= 0:
        return 1 if dy < 0 else 7           # NE : SE
    return 3 if dy < 0 else 5               # NW : SW


def pack_result(acc_dx, acc_dy):
    sx = 1 if acc_dx < 0 else 0
    sy = 1 if acc_dy < 0 else 0
    mx = _clamp_mag(acc_dx, DXY_MAX)
    my = _clamp_mag(acc_dy, DXY_MAX)
    oct = octant(acc_dx, acc_dy)
    ax, ay = _absi(acc_dx), _absi(acc_dy)
    cheb = ax if ax > ay else ay
    mag = _clamp_mag(cheb, MAG_MAX)
    return (sx << 15) | (mx << 11) | (sy << 10) | (my << 6) | (oct << 3) | mag


def python_stabilize_words(x, y, ret_state=False):
    """Bit-exact mirror of software/dvs_stabilize/main.c's ISR. One word per
    BATCH=4 events. If ret_state, also returns the per-batch (acc_dx, acc_dy)
    integer accumulator trace (for numeric reporting/plots)."""
    T = [0] * TS_CELLS
    acc_dx = 0
    acc_dy = 0
    now = 0
    last_touched = [0] * TS_CELLS
    event_count = 0

    def is_recent(last, nowc):
        return last != 0 and (nowc - last) <= CORR_WINDOW

    words = []
    trace = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        # decay (halve) -- arithmetic shift of the signed accumulator
        acc_dx = acc_dx - (acc_dx >> 1)
        acc_dy = acc_dy - (acc_dy >> 1)
        sum_dx = 0
        sum_dy = 0
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            tx = xi >> TS_SHIFT
            ty = yi >> TS_SHIFT
            if tx >= TW:
                tx = TW - 1
            if ty >= TH:
                ty = TH - 1
            idx = (ty << TW_LOG2) | tx

            if CORR_MIN > 0:
                # Spatio-temporal correlation gate (bit-exact to firmware).
                event_count += 1
                nc = 0
                has_l, has_r = tx > 0, tx < TW - 1
                has_u, has_d = ty > 0, ty < TH - 1
                if has_l:            nc += is_recent(last_touched[idx - 1], event_count)
                if has_r:            nc += is_recent(last_touched[idx + 1], event_count)
                if has_u:            nc += is_recent(last_touched[idx - TW_P2], event_count)
                if has_d:            nc += is_recent(last_touched[idx + TW_P2], event_count)
                if has_l and has_u:  nc += is_recent(last_touched[idx - TW_P2 - 1], event_count)
                if has_r and has_u:  nc += is_recent(last_touched[idx - TW_P2 + 1], event_count)
                if has_l and has_d:  nc += is_recent(last_touched[idx + TW_P2 - 1], event_count)
                if has_r and has_d:  nc += is_recent(last_touched[idx + TW_P2 + 1], event_count)
                last_touched[idx] = event_count
                if nc < CORR_MIN:
                    continue   # uncorrelated -- background noise or a hot pixel

            rleft = T[idx - 1] if tx > 0 else T[idx]
            rright = T[idx + 1] if tx < TW - 1 else T[idx]
            rup = T[idx - TW_P2] if ty > 0 else T[idx]
            rdown = T[idx + TW_P2] if ty < TH - 1 else T[idx]
            if rleft > rright:
                sum_dx += 1
            elif rright > rleft:
                sum_dx -= 1
            if rup > rdown:
                sum_dy += 1
            elif rdown > rup:
                sum_dy -= 1
            now += 1
            T[idx] = (now >> TICK_SHIFT) & 0xFF
        acc_dx += sum_dx
        acc_dy += sum_dy
        words.append(pack_result(acc_dx, acc_dy))
        trace.append((acc_dx, acc_dy))
    if ret_state:
        return words, trace
    return words


def unpack_status(word):
    sx = (word >> 15) & 1
    mx = (word >> 11) & 0xF
    sy = (word >> 10) & 1
    my = (word >> 6) & 0xF
    oct = (word >> 3) & 0x7
    mag = word & 0x7
    dx = -mx if sx else mx
    dy = -my if sy else my
    return dx, dy, oct, mag


def report(words, trace, every):
    """Print the global-motion vector over time (from the real int accumulator
    trace, not the clamped word fields) so a coherent pan shows a consistent
    direction."""
    print(f"{'batch':>7} {'ev':>8}  {'acc_dx':>7} {'acc_dy':>7}  {'octant':>6}  mag")
    for bi in range(0, len(words), every):
        adx, ady = trace[bi]
        _, _, oct, mag = unpack_status(words[bi])
        ev = (bi + 1) * BATCH
        print(f"{bi:>7} {ev:>8}  {adx:>7} {ady:>7}  {OCT_NAMES[oct]:>6}  {mag}")
    # summary: dominant octant + mean vector
    from collections import Counter
    octs = Counter(unpack_status(w)[2] for w in words)
    mdx = sum(t[0] for t in trace) / len(trace)
    mdy = sum(t[1] for t in trace) / len(trace)
    print("\n--- summary ---")
    print(f"batches: {len(words)}   events: {len(words)*BATCH}")
    print(f"mean accumulator vector: (dx={mdx:+.2f}, dy={mdy:+.2f})  "
          f"[+dx=right, +dy=down]")
    top = octs.most_common(3)
    print("octant histogram (top 3): " +
          ", ".join(f"{OCT_NAMES[o]}={c} ({100*c/len(words):.0f}%)" for o, c in top))
    dom = octs.most_common(1)[0][0]
    print(f"dominant background-motion direction: {OCT_NAMES[dom]}")


def save_plot(trace, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable, skipping --plot:", e)
        return
    adx = [t[0] for t in trace]
    ady = [t[1] for t in trace]
    ev = [(i + 1) * BATCH for i in range(len(trace))]
    fig, ax = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    ax[0].plot(ev, adx, lw=0.8)
    ax[0].axhline(0, color="k", lw=0.5)
    ax[0].set_ylabel("acc_dx  (+ = right)")
    ax[0].set_title("dvs_stabilize: global background-motion vector over time")
    ax[1].plot(ev, ady, lw=0.8, color="tab:orange")
    ax[1].axhline(0, color="k", lw=0.5)
    ax[1].set_ylabel("acc_dy  (+ = down)")
    ax[1].set_xlabel("event index")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real actsim-captured chip output (one packed word "
                         "per line) instead of the Python mirror")
    ap.add_argument("--rate", type=int, default=200, help="events injected per frame")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--decay", type=float, default=0.88)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--headless", action="store_true", help="no window")
    ap.add_argument("--save", help="write the final frame to this PNG")
    ap.add_argument("--report", type=int, metavar="EVERY", default=0,
                    help="print the motion vector every EVERY batches and a summary")
    ap.add_argument("--plot", metavar="PNG", help="save an acc_dx/acc_dy vs time plot")
    args = ap.parse_args()

    x, y, pol = load(args.csv)
    n_batches = len(x) // BATCH
    x, y, pol = x[:n_batches * BATCH], y[:n_batches * BATCH], pol[:n_batches * BATCH]

    trace = None
    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        n_batches = min(n_batches, len(words))
        x, y, pol = x[:n_batches * BATCH], y[:n_batches * BATCH], pol[:n_batches * BATCH]
        words = words[:n_batches]
        # reconstruct a (clamped) trace from the words for reporting/plot
        trace = [(dx, dy) for dx, dy, _, _ in (unpack_status(w) for w in words)]
        print(f"loaded {len(x)} events from {args.csv}; vector overlay from real "
              f"actsim capture {args.from_actsim} (NOT a Python reimplementation)")
    else:
        words, trace = python_stabilize_words(x, y, ret_state=True)
        print(f"loaded {len(x)} events from {args.csv} (vector overlay is a Python "
              f"mirror of the firmware's time-surface math, not real chip output)")

    if args.report:
        report(words, trace, args.report)
    if args.plot:
        save_plot(trace, args.plot)

    # Only open the live OpenCV window when the user asked for interactive
    # playback (no report/plot/save/headless requested) -- report/plot are
    # batch/numeric outputs and must not spawn a GUI.
    if not (args.headless or args.save or args.report or args.plot):
        _render_live(args, x, y, pol, words)


def _draw_arrow(img, cx, cy, dx, dy, oct, mag, scale):
    import cv2
    # scale the unit direction by a fixed display length * (mag+1)
    L = 8 * (mag + 1)
    # octant -> unit display vector (x right, y down); N (dy<0) points up
    dirs = {0: (1, 0), 1: (1, -1), 2: (0, -1), 3: (-1, -1),
            4: (-1, 0), 5: (-1, 1), 6: (0, 1), 7: (1, 1)}
    ux, uy = dirs[oct]
    ex, ey = int(cx + ux * L), int(cy + uy * L)
    still = (oct == 7 and mag == 0)
    color = (128, 128, 128) if still else (0, 255, 255)
    if not still:
        cv2.arrowedLine(img, (cx, cy), (ex, ey), color, 2, tipLength=0.3)
    label = "still" if still else f"{OCT_NAMES[oct]} m={mag}"
    cv2.putText(img, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)


def _render_live(args, x, y, pol, words):
    W, H = SY, SX  # swap+flipx like dvs_replay.py
    acc = np.zeros((H, W, 3), np.float32)
    n = len(x)

    def stamp(i):
        xi, yi, pi = int(x[i]), int(y[i]), int(pol[i])
        if not (0 <= xi < SX and 0 <= yi < SY):
            return
        col, row = yi, xi
        col = W - 1 - col
        if 0 <= col < W and 0 <= row < H:
            acc[row, col] = (0, 1, 0) if pi else (0, 0, 1)

    def to_u8(img):
        return (np.clip(img, 0, 1) * 255).astype(np.uint8)

    if args.headless or (args.save and args.rate <= 0):
        for i in range(n):
            stamp(i)
        if args.save:
            import cv2
            img = to_u8(acc)
            img_up = cv2.resize(img, (W * args.scale, H * args.scale),
                                interpolation=cv2.INTER_NEAREST)
            if words:
                _, _, oct, mag = unpack_status(words[-1])
                _draw_arrow(img_up, W * args.scale // 2, H * args.scale // 2,
                            0, 0, oct, mag, args.scale)
            cv2.imwrite(args.save, img_up)
            print(f"wrote {args.save}")
        return

    try:
        import cv2
    except Exception as e:
        print("cv2 unavailable, falling back to headless:", e)
        for i in range(n):
            stamp(i)
        return

    cv2.namedWindow("DVS stabilize", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("DVS stabilize", W * args.scale, H * args.scale)
    i = 0
    paused = False
    print(f"replaying {n} events ({len(words)} vectors) (space=pause r=restart q=quit)")
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
            img_up = cv2.resize(img, (W * args.scale, H * args.scale),
                                interpolation=cv2.INTER_NEAREST)
            bi = max(0, min(len(words) - 1, i // BATCH - 1))
            if words:
                _, _, oct, mag = unpack_status(words[bi])
                _draw_arrow(img_up, W * args.scale // 2, H * args.scale // 2,
                            0, 0, oct, mag, args.scale)
            cv2.putText(img_up, f"{i}/{n}", (2, H * args.scale - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.imshow("DVS stabilize", img_up)
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


if __name__ == "__main__":
    main()
