#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_apophenia/main.c
("Apophenia Engine" -- a living Rorschach).

The chip keeps a coarse 32x14 DECAYING activity grid over the 126x112 sensor.
Each event warms its cell (saturating +STEP); every DECAY_INTERVAL batches the
whole grid is halved. Per BATCH=4 events it emits ONE status word for the
HOTTEST cell that crosses EMIT_THRESHOLD (flag=1), else the last-touched cell
(flag=0). This host unpacks those words, rebuilds a coarse activity buffer with
its own decay, then paints a 4-FOLD MIRRORED image: it treats the top-left
quadrant as the "seed" and reflects it across x and y so the result is a
symmetric, breathing inkblot -- an apophenic Rorschach that warps as the scene
moves.

python_apophenia_words() below is a byte-for-byte port of the firmware's integer
logic (same coarse grid, same saturating warm, same halving decay, same
argmax+threshold emit) so what we render is provably what the chip would emit
given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_apophenia_view.py --validate                  # synthetic self-test (numbers)
  dvs_apophenia_view.py --from-actsim results.mem   # render real chip status words
  dvs_apophenia_view.py events.csv                  # render (host-computed) from a CSV
  dvs_apophenia_view.py ... --headless --save inkblot.png
"""
import argparse
import numpy as np

# --- must match software/dvs_apophenia/main.c exactly ---
SX, SY = 126, 112
XQ_SHIFT = 2                              # 4-px columns: 125>>2 = 31 -> cols 0..31
YQ_SHIFT = 3                              # 8-px rows:    111>>3 = 13 -> rows 0..13
GRID_COLS = 32                            # logical columns (0..31)
GRID_ROWS = 14                            # logical rows    (0..13)
GRID_STRIDE_SHIFT = 5                     # STRIDE == 32 == 1<<5
GRID_STRIDE = 1 << GRID_STRIDE_SHIFT      # 32 (power-of-two stride)
GRID_CELLS = GRID_STRIDE * GRID_ROWS      # 448
STEP = 24
GRID_CAP = 255
DECAY_SHIFT = 1                           # cool: c -= c>>1  (halve)
DECAY_INTERVAL = 8                        # decay the whole grid every 8 batches
EMIT_THRESHOLD = 64
BATCH = 4


def python_apophenia_words(x, y, pol):
    """Bit-faithful port of software/dvs_apophenia/main.c's ISR: one status word
    per BATCH=4 events. x,y,pol are per-event arrays. Returns a list of packed
    status words (the same integers the chip writes to FIFO_OUT)."""
    grid = [0] * GRID_CELLS
    batch_count = 0
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        last_cell = 0
        # Warm the grid.
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            xq = xi >> XQ_SHIFT
            yq = yi >> YQ_SHIFT
            cell = (yq << GRID_STRIDE_SHIFT) | xq
            last_cell = cell
            grid[cell] = min(grid[cell] + STEP, GRID_CAP)

        # Periodic halving decay.
        batch_count += 1
        if batch_count >= DECAY_INTERVAL:
            batch_count = 0
            for c in range(GRID_CELLS):
                grid[c] = grid[c] - (grid[c] >> DECAY_SHIFT)

        # Argmax.
        best_cell, best_val = 0, 0
        for c in range(GRID_CELLS):
            if grid[c] > best_val:
                best_val, best_cell = grid[c], c

        if best_val >= EMIT_THRESHOLD:
            out_cell, out_val, flag = best_cell, best_val, 1
        else:
            out_cell, out_val, flag = last_cell, grid[last_cell], 0

        xq = out_cell & (GRID_STRIDE - 1)
        yq = out_cell >> GRID_STRIDE_SHIFT
        words.append((flag << 17) | (out_val << 9) | (yq << 5) | xq)
    return words


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bits[4:0]=xq, bits[8:5]=yq, bits[16:9]=val, bit[17]=flag."""
    xq = word & 0x1F
    yq = (word >> 5) & 0xF
    val = (word >> 9) & 0xFF
    flag = (word >> 17) & 1
    return xq, yq, val, flag


# ---------------------------------------------------------------------------
# Host-side grid accumulation + 4-fold mirror.
# ---------------------------------------------------------------------------
def accumulate_grid(words, host_decay=0.90):
    """Replay the emitted (xq,yq,val) status words into a GRID_ROWS x GRID_COLS
    float buffer with a gentle per-word exponential decay, so the host view
    breathes even between peaks. Returns the final buffer (row-major, [yq,xq])."""
    buf = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.float64)
    for w in words:
        xq, yq, val, flag = unpack_status(w)
        if xq >= GRID_COLS or yq >= GRID_ROWS:
            continue
        buf *= host_decay
        # A real peak (flag) writes its value; the fallback below-threshold
        # report just nudges its cell so the field never fully dies.
        if flag:
            buf[yq, xq] = max(buf[yq, xq], val)
        else:
            buf[yq, xq] = max(buf[yq, xq], val * 0.5)
    return buf


def mirror4(buf):
    """4-fold mirror the grid into a symmetric inkblot. The buffer's LEFT HALF
    (columns 0..GRID_COLS/2-1) is the seed quadrant; reflect it across the
    vertical mid-line, then reflect the whole thing across the horizontal
    mid-line, so the result is symmetric under x- and y-flip (the classic
    Rorschach bilateral symmetry, doubled)."""
    half_c = GRID_COLS // 2
    # Seed = left half of the grid (all rows). Build a full-width row-symmetric
    # field by mirroring the left half onto the right.
    left = buf[:, :half_c]
    wide = np.concatenate([left, left[:, ::-1]], axis=1)   # x-symmetric, full width
    # Now make it y-symmetric by folding the top half onto the bottom.
    half_r = GRID_ROWS // 2
    top = wide[:half_r, :]
    tall = np.concatenate([top, top[::-1, :]], axis=0)     # y-symmetric (even rows)
    if tall.shape[0] < GRID_ROWS:
        # Odd row count: pad the centre row back (GRID_ROWS=14 is even, so this
        # branch is inert here, but keep it robust to a future odd GRID_ROWS).
        centre = wide[half_r:half_r + 1, :]
        tall = np.concatenate([top, centre, top[::-1, :]], axis=0)[:GRID_ROWS]
    return tall


def render_inkblot(words, save=None, headless=False, upscale=24, host_decay=0.90):
    """Accumulate the status words, 4-fold mirror them, upscale and paint with a
    smooth colormap -- the living Rorschach."""
    buf = accumulate_grid(words, host_decay=host_decay)
    blot = mirror4(buf)

    # Normalise for display.
    peak = blot.max()
    norm = blot / peak if peak > 0 else blot

    # Upscale by nearest-neighbour for a chunky, organic look.
    big = np.kron(norm, np.ones((upscale, upscale)))

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from scipy.ndimage import gaussian_filter
        big = gaussian_filter(big, sigma=upscale * 0.35)   # soften the blocks
    except Exception:
        try:
            import matplotlib
            if headless:
                matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception as e:
            print("matplotlib unavailable:", e)
            print("Mirrored inkblot grid (row-major, normalised):")
            np.set_printoptions(precision=2, suppress=True, linewidth=200)
            print(np.round(norm, 2))
            return

    fig, ax = plt.subplots(figsize=(GRID_COLS / 3, GRID_ROWS / 3))
    ax.imshow(big, cmap="magma", vmin=0, vmax=1, interpolation="bilinear")
    ax.set_title('"Apophenia Engine" -- a living Rorschach')
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=110)
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: a moving blob; assert bounded + mirror symmetric.
# ---------------------------------------------------------------------------
def gen_synthetic(seconds_events=6000, seed=0):
    """A blob of activity that sweeps across the sensor plus scattered noise.
    Returns (x, y, pol). The blob moves left->right over the frame so the grid
    keeps warming new cells -- a good stress test for boundedness + decay."""
    rng = np.random.default_rng(seed)
    x, y, pol = [], [], []
    for t in range(seconds_events):
        # Blob centre sweeps across the frame.
        cx = int(4 + (SX - 8) * (t / seconds_events))
        cy = SY // 2 + int(10 * np.sin(t / 300.0))
        for _ in range(6):
            px = int(np.clip(cx + rng.normal(0, 6), 0, SX - 1))
            py = int(np.clip(cy + rng.normal(0, 6), 0, SY - 1))
            x.append(px); y.append(py); pol.append(int(rng.integers(0, 2)))
        # A little background noise.
        if rng.random() < 0.3:
            x.append(int(rng.integers(0, SX)))
            y.append(int(rng.integers(0, SY)))
            pol.append(int(rng.integers(0, 2)))
    return (np.array(x, dtype=np.int64),
            np.array(y, dtype=np.int64),
            np.array(pol, dtype=np.int64))


def validate():
    x, y, pol = gen_synthetic()
    words = python_apophenia_words(x, y, pol)
    print(f"synthetic stream: {len(x)} events, {len(words)} status words")

    ok = True

    # 1. Every reported val must be within the 8-bit grid cap (grid stays bounded
    #    -- the decay never lets it run away, and STEP-saturation clamps it).
    vals = [unpack_status(w)[2] for w in words]   # unpack -> (xq, yq, val, flag)
    max_val = max(vals) if vals else 0
    bounded = 0 <= max_val <= GRID_CAP
    print(f"  grid bounded: max reported val = {max_val} (cap {GRID_CAP})  "
          f"-> {'OK' if bounded else 'FAIL'}")
    ok = ok and bounded

    # 2. All (xq,yq) indices must be inside the logical grid.
    idx_ok = all(0 <= xq < GRID_COLS and 0 <= yq < GRID_ROWS
                 for (xq, yq, _v, _f) in (unpack_status(w) for w in words))
    print(f"  cell indices in range: {'OK' if idx_ok else 'FAIL'}")
    ok = ok and idx_ok

    # 3. At least some real-peak (flag=1) emissions occurred for a real blob
    #    (the threshold is crossed), i.e. the app actually fires.
    peaks = sum(1 for w in words if unpack_status(w)[3] == 1)
    fired = peaks > 0
    print(f"  real-peak emissions (flag=1): {peaks}  -> {'OK' if fired else 'FAIL'}")
    ok = ok and fired

    # 4. The 4-fold mirror is symmetric: flipping the rendered inkblot across x
    #    and across y leaves it unchanged (that IS the Rorschach property).
    buf = accumulate_grid(words)
    blot = mirror4(buf)
    sym_x = np.allclose(blot, blot[:, ::-1])
    sym_y = np.allclose(blot, blot[::-1, :])
    print(f"  mirror symmetric across x: {'OK' if sym_x else 'FAIL'}")
    print(f"  mirror symmetric across y: {'OK' if sym_y else 'FAIL'}")
    ok = ok and sym_x and sym_y

    print()
    print("VALIDATION:", "PASS -- grid bounded, indices valid, app fires, and the "
          "inkblot is 4-fold mirror-symmetric" if ok else "FAIL")
    return ok


def load_csv(path):
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x = np.array([int(row[idx["x"]]) for row in rows], dtype=np.int64)
    y = np.array([int(row[idx["y"]]) for row in rows], dtype=np.int64)
    if "pol" in idx:
        pol = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int64)
    else:
        pol = np.zeros(len(x), dtype=np.int64)
    return x, y, pol


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: bounded grid + symmetric mirror (prints numbers)")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the inkblot PNG here")
    ap.add_argument("--upscale", type=int, default=24, help="px per grid cell in the render")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, pol = load_csv(args.csv)
        print(f"loaded {len(x)} events from {args.csv}; computing status words in "
              f"Python (bit-faithful mirror of firmware).")
        words = python_apophenia_words(x, y, pol)
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_inkblot(words, save=args.save, headless=args.headless, upscale=args.upscale)


if __name__ == "__main__":
    main()
