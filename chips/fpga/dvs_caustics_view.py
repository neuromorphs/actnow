#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_caustics/main.c
("Event-Caustic Refractor" -- shimmering underwater light-caustics).

The chip "refracts" each event through a fake wavy WATER SURFACE: a multiply-free
quarter-sine LUT (reflected/negated into the full circle) sampled by a TRAVELLING
wave whose phase advances with the event timestamp. The refracted position
xr = clamp(x+ox), yr = clamp(y+oy) (|ox|,|oy| <= AMP) is deposited, per BATCH=4
events, as ONE status word {xr, yr, pol, strength, flag}. This host accumulates the
refracted samples into a DECAYING CAUSTIC FIELD (float buffer, per-frame
multiplicative decay + additive splats) and paints it with a blue/cyan
"underwater light" colormap -- rippling liquid light. ON events tweak the hue
warmer/brighter, OFF cooler.

python_caustics_words() below is a byte-for-byte port of the firmware's integer
logic (same quarter-sine LUT + quadrant reflection, same travelling-wave warp,
same saturating leaky global + per-region gates + threshold flag) so what we render
is provably what the chip would emit given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_caustics_view.py --validate                  # synthetic self-test (numbers)
  dvs_caustics_view.py --from-actsim results.mem   # render real chip status words
  dvs_caustics_view.py events.csv                  # render (host-computed) from a CSV
  dvs_caustics_view.py ... --headless --save caustics.png
"""
import argparse
import numpy as np

# --- must match software/dvs_caustics/main.c exactly ---
SX, SY = 126, 112
XR_MAX, YR_MAX = 125, 111
BATCH = 4

# Wavy water surface.
AMP = 8
MASK = 0xFF            # full-cycle 8-bit phase mask (256 phases)
PHASE_SHIFT = 8        # ts >> PHASE_SHIFT (temporal phase)
WAVE_SHIFT = 4         # spatial wavelength: coord >> WAVE_SHIFT

# Quarter-sine LUT: SIN_Q[i] = round(AMP*sin(i/64 * pi/2)), i in 0..63 (0..AMP).
# Must match SIN_Q[] in the firmware byte-for-byte.
SIN_Q = [
    0, 0, 0, 1, 1, 1, 1, 1,
    2, 2, 2, 2, 2, 3, 3, 3,
    3, 3, 3, 4, 4, 4, 4, 4,
    4, 5, 5, 5, 5, 5, 5, 6,
    6, 6, 6, 6, 6, 6, 6, 7,
    7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 8, 8, 8, 8, 8, 8,
    8, 8, 8, 8, 8, 8, 8, 8,
]

# Leaky noise guards.
STEP = 32
ACT_CAP = 255
DECAY_SHIFT = 2
REGX_SHIFT = 5         # 32-px cols: 125>>5 = 3 -> cols 0..3
REGY_SHIFT = 4         # 16-px rows: 111>>4 = 6 -> rows 0..6
REG_COLS = 4
REG_ROWS = 7
REG_STRIDE_SHIFT = 2
REG_STRIDE = 1 << REG_STRIDE_SHIFT   # 4
REG_CELLS = REG_STRIDE * REG_ROWS    # 28
REGION_MIN = 48
EMIT_THRESHOLD = 64


def sinLUT(idx):
    """Bit-faithful port of the firmware's sinLUT(): full sine from the quarter LUT
    via quadrant reflection/negation (shifts, masks, compares -- no multiply).
    Returns a signed offset in [-AMP, AMP] for the 8-bit phase idx."""
    q = (idx >> 6) & 3
    i = idx & 63
    if q == 0:
        return SIN_Q[i]
    elif q == 1:
        return SIN_Q[i ^ 63]
    elif q == 2:
        return -SIN_Q[i]
    else:
        return -SIN_Q[i ^ 63]


def warp(x, y, ts):
    """Bit-faithful port of the firmware's refraction warp: travelling-wave offsets
    (ox uses y, oy uses x) driven by the temporal phase ph = ts>>PHASE_SHIFT, then
    clamped into the sensor frame. Returns (xr, yr, ox, oy)."""
    ph = ts >> PHASE_SHIFT
    ox = sinLUT((ph + (y >> WAVE_SHIFT)) & MASK)
    oy = sinLUT((ph + (x >> WAVE_SHIFT)) & MASK)
    xr = x + ox
    xr = 0 if xr < 0 else (XR_MAX if xr > XR_MAX else xr)
    yr = y + oy
    yr = 0 if yr < 0 else (YR_MAX if yr > YR_MAX else yr)
    return xr, yr, ox, oy


def python_caustics_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_caustics/main.c's ISR: one status word per
    BATCH=4 events. x,y,ts,pol are per-event arrays. Returns a list of packed status
    words (the same integers the chip writes to FIFO_OUT)."""
    activity = 0
    region = [0] * REG_CELLS
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        rx_last = ry_last = 0
        x_last = y_last = ts_last = pol_last = 0
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            ti = int(ts[i]) & 0xFFFF
            pi = int(pol[i]) & 1

            activity = min(activity + STEP, ACT_CAP)
            rx = xi >> REGX_SHIFT
            ry = yi >> REGY_SHIFT
            rcell = (ry << REG_STRIDE_SHIFT) | rx
            region[rcell] = min(region[rcell] + STEP, ACT_CAP)

            rx_last, ry_last = rx, ry
            x_last, y_last, ts_last, pol_last = xi, yi, ti, pi

        # Leaky decay (global + whole region grid), once per batch.
        activity = activity - (activity >> DECAY_SHIFT)
        for c in range(REG_CELLS):
            region[c] = region[c] - (region[c] >> DECAY_SHIFT)

        xr, yr, _, _ = warp(x_last, y_last, ts_last)

        strength = activity >> 3
        if strength > 31:
            strength = 31

        rcell_last = (ry_last << REG_STRIDE_SHIFT) | rx_last
        flag = 1 if (activity >= EMIT_THRESHOLD and region[rcell_last] >= REGION_MIN) else 0

        words.append((flag << 20) | (strength << 15) | (pol_last << 14)
                     | (yr << 7) | xr)
    return words


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bits[6:0]=xr, bits[13:7]=yr, bit[14]=pol, bits[19:15]=strength, bit[20]=flag."""
    xr = word & 0x7F
    yr = (word >> 7) & 0x7F
    pol = (word >> 14) & 1
    strength = (word >> 15) & 0x1F
    flag = (word >> 20) & 1
    return xr, yr, pol, strength, flag


# ---------------------------------------------------------------------------
# Caustic-field renderer: additive splats into a decaying float field.
# ---------------------------------------------------------------------------
def accumulate_field(words, decay=0.90, splat_sigma=2.2):
    """Accumulate the refracted samples into a decaying caustic field. Each status
    word splats a small gaussian at (xr,yr) into a float field that decays
    multiplicatively per word; ON events splat warmer/brighter than OFF. Returns
    (fieldON, fieldOFF) 2-D float buffers in the SX x SY sensor frame (rows=y)."""
    W, H = SX, SY
    fieldON = np.zeros((H, W), dtype=np.float64)
    fieldOFF = np.zeros((H, W), dtype=np.float64)
    yy, xx = np.mgrid[0:H, 0:W]
    two_s2 = 2.0 * splat_sigma * splat_sigma
    for w in words:
        xr, yr, pol, strength, flag = unpack_status(w)
        # Multiplicative decay each step so the field ripples/shimmers.
        fieldON *= decay
        fieldOFF *= decay
        amp = (0.25 + 0.75 * (strength / 31.0)) * (1.0 if flag else 0.35)
        blob = np.exp(-((xx - xr) ** 2 + (yy - yr) ** 2) / two_s2) * amp
        if pol:
            fieldON += blob
        else:
            fieldOFF += blob
    return fieldON, fieldOFF


def underwater_rgb(fieldON, fieldOFF):
    """Blue/cyan 'underwater light' colormap: OFF -> deep blue, ON -> bright cyan.
    Combines the two polarity fields into an (H,W,3) float RGB image 0..1."""
    total = fieldON + fieldOFF
    peak = total.max()
    inv = 1.0 / peak if peak > 0 else 0.0
    on = np.clip(fieldON * inv, 0, 1)
    off = np.clip(fieldOFF * inv, 0, 1)
    t = np.clip(total * inv, 0, 1)
    # Base underwater gradient: dark navy -> cyan-white as intensity rises.
    r = 0.02 + 0.20 * on + 0.05 * t
    g = 0.06 + 0.55 * t + 0.30 * on
    b = 0.12 + 0.85 * t + 0.10 * off
    rgb = np.stack([r, g, b], axis=-1)
    # Fade to the dark backdrop where the field is empty.
    veil = np.clip(t * 3.0, 0, 1)[:, :, None]
    backdrop = np.array([0.01, 0.03, 0.07])
    rgb = backdrop * (1 - veil) + rgb * veil
    return np.clip(rgb, 0, 1)


def render_caustics(words, save=None, headless=False, decay=0.90):
    """Accumulate the refracted samples into a decaying caustic field and composite
    the shimmering underwater-light image. --headless renders without a display."""
    fieldON, fieldOFF = accumulate_field(words, decay=decay)
    rgb = underwater_rgb(fieldON, fieldOFF)

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable:", e)
        print("Caustic field (max channel per row, coarse):")
        np.set_printoptions(precision=2, suppress=True, linewidth=200)
        print(np.round(rgb.max(axis=2), 2))
        return

    fig, ax = plt.subplots(figsize=(SX / 24, SY / 24))
    ax.imshow(rgb, interpolation="bilinear", origin="upper")
    ax.set_title('"Event-Caustic Refractor" -- shimmering underwater light')
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=110)
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: a moving blob so the warp has real structure to bend.
# ---------------------------------------------------------------------------
def gen_synthetic(steps=1600, events_per_step=6, seed=0):
    """A blob drifting across the frame with a rising timestamp (so the water
    surface travels) plus scattered noise. Returns (x, y, ts, pol)."""
    rng = np.random.default_rng(seed)
    x, y, ts, pol = [], [], [], []
    t = 0
    for s in range(steps):
        cx = 20 + (s * 86) // steps            # drift across x (integer, no float need)
        cy = 30 + (s * 50) // steps            # drift down y
        for _ in range(events_per_step):
            px = int(np.clip(cx + rng.normal(0, 4), 0, SX - 1))
            py = int(np.clip(cy + rng.normal(0, 4), 0, SY - 1))
            t = (t + rng.integers(20, 120)) & 0xFFFF
            x.append(px); y.append(py); ts.append(int(t))
            pol.append(int(rng.integers(0, 2)))
        if rng.random() < 0.25:                # a little background noise
            t = (t + rng.integers(20, 120)) & 0xFFFF
            x.append(int(rng.integers(0, SX)))
            y.append(int(rng.integers(0, SY)))
            ts.append(int(t))
            pol.append(int(rng.integers(0, 2)))
    return (np.array(x, dtype=np.int64), np.array(y, dtype=np.int64),
            np.array(ts, dtype=np.int64), np.array(pol, dtype=np.int64))


def validate():
    x, y, ts, pol = gen_synthetic()
    words = python_caustics_words(x, y, ts, pol)
    print(f"synthetic drifting blob: {len(x)} events, {len(words)} status words")

    ok = True
    unpacked = [unpack_status(w) for w in words]

    # 1. Every refracted sample is inside the sensor frame.
    coords_ok = all(0 <= u[0] <= XR_MAX and 0 <= u[1] <= YR_MAX for u in unpacked)
    print(f"  refracted coords in [0,{XR_MAX}]x[0,{YR_MAX}]: "
          f"{'OK' if coords_ok else 'FAIL'}")
    ok = ok and coords_ok

    # 2. The offsets the warp applies are bounded by AMP (|ox|,|oy| <= AMP). Recompute
    #    directly from sinLUT over the full phase circle -- the LUT can never exceed AMP.
    lut_vals = [sinLUT(p) for p in range(256)]
    amp_ok = all(-AMP <= v <= AMP for v in lut_vals) and max(lut_vals) == AMP and min(lut_vals) == -AMP
    print(f"  |ox|,|oy| <= AMP={AMP} over all 256 phases "
          f"(max={max(lut_vals)}, min={min(lut_vals)}): {'OK' if amp_ok else 'FAIL'}")
    ok = ok and amp_ok

    # 3. strength within 5 bits, pol/flag single bits.
    fields_ok = all(0 <= u[3] <= 31 and u[2] in (0, 1) and u[4] in (0, 1)
                    for u in unpacked)
    print(f"  strength/pol/flag well-formed: {'OK' if fields_ok else 'FAIL'}")
    ok = ok and fields_ok

    # 4. The app fires REAL splats (flag=1) for sustained activity.
    peaks = sum(1 for u in unpacked if u[4] == 1)
    fired = peaks > 0
    print(f"  real splats (flag=1): {peaks} -> {'OK' if fired else 'FAIL'}")
    ok = ok and fired

    # 5. The warp ACTUALLY DISPLACES samples: accumulate the warped field vs a
    #    no-warp field (same events, offsets forced to 0) and require they differ.
    fON, fOFF = accumulate_field(words)
    field_warp = fON + fOFF
    # No-warp reference: re-pack with ox=oy=0 (deposit raw x,y instead of xr,yr).
    nowarp_words = []
    activity = 0
    region = [0] * REG_CELLS
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        x_last = y_last = pol_last = 0
        rx_last = ry_last = 0
        for i in range(b, b + BATCH):
            xi, yi, pi = int(x[i]) & 0x7F, int(y[i]) & 0x7F, int(pol[i]) & 1
            activity = min(activity + STEP, ACT_CAP)
            rx, ry = xi >> REGX_SHIFT, yi >> REGY_SHIFT
            region[(ry << REG_STRIDE_SHIFT) | rx] = min(
                region[(ry << REG_STRIDE_SHIFT) | rx] + STEP, ACT_CAP)
            rx_last, ry_last = rx, ry
            x_last, y_last, pol_last = xi, yi, pi
        activity = activity - (activity >> DECAY_SHIFT)
        for c in range(REG_CELLS):
            region[c] = region[c] - (region[c] >> DECAY_SHIFT)
        strength = min(activity >> 3, 31)
        flag = 1 if (activity >= EMIT_THRESHOLD
                     and region[(ry_last << REG_STRIDE_SHIFT) | rx_last] >= REGION_MIN) else 0
        # deposit the UN-warped position
        nowarp_words.append((flag << 20) | (strength << 15) | (pol_last << 14)
                            | (y_last << 7) | x_last)
    nON, nOFF = accumulate_field(nowarp_words)
    field_nowarp = nON + nOFF
    diff = float(np.abs(field_warp - field_nowarp).sum())
    displaced = diff > 1e-6
    # Also confirm at least one word's xr/yr differs from its raw x/y.
    any_moved = any(unpack_status(w)[0] != (int(x[b + BATCH - 1]) & 0x7F)
                    or unpack_status(w)[1] != (int(y[b + BATCH - 1]) & 0x7F)
                    for w, b in zip(words, range(0, len(x) - len(x) % BATCH, BATCH)))
    warp_ok = displaced and any_moved
    print(f"  warp displaces samples (field L1 diff vs no-warp={diff:.1f}, "
          f"some xr/yr != x/y={any_moved}): {'OK' if warp_ok else 'FAIL'}")
    ok = ok and warp_ok

    print()
    print("VALIDATION:", "PASS -- refracted coords in frame, offsets bounded by AMP, "
          "fields well-formed, real splats fire, and the warp actually displaces "
          "samples (field differs from a no-warp accumulation)"
          if ok else "FAIL")
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
    # Timestamp column is "le" in the chips/fpga captures (rising ~microseconds);
    # fall back to a synthetic ramp if absent so the wave still travels.
    if "le" in idx:
        ts = np.array([int(row[idx["le"]]) & 0xFFFF for row in rows], dtype=np.int64)
    elif "ts" in idx:
        ts = np.array([int(row[idx["ts"]]) & 0xFFFF for row in rows], dtype=np.int64)
    else:
        ts = (np.arange(len(x), dtype=np.int64) * 37) & 0xFFFF
    return x, y, ts, pol


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: coords in frame, |ox|,|oy|<=AMP, "
                         "warp displaces samples (prints numbers)")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the caustics PNG here")
    ap.add_argument("--decay", type=float, default=0.90,
                    help="per-word multiplicative field decay (0..1)")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, ts, pol = load_csv(args.csv)
        print(f"loaded {len(x)} events from {args.csv}; computing status words in "
              f"Python (bit-faithful mirror of firmware).")
        words = python_caustics_words(x, y, ts, pol)
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_caustics(words, save=args.save, headless=args.headless, decay=args.decay)


if __name__ == "__main__":
    main()
