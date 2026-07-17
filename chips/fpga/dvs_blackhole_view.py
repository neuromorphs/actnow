#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_blackhole/main.c
("Micro-Event Black Holes" -- regions where motion COLLAPSES, the inverse of an
activity heatmap).

The chip keeps, per coarse 8x8-px region, two leaky density measures: a FAST leaky
EMA `f` (short memory, sheds ~1/4 per tick) that events bump directly, and a SLOW
baseline `s` (long memory) that CHASES the fast EMA a fraction each tick. While a
region is active `f` rides high and `s` chases up but stays just below it, so
collapse = clamp(s - f, >=0) is ~0; when activity STOPS, the fast EMA falls quickly
while the slow baseline lags behind, so collapse opens up -- the signature of a
region that *was busy and just went quiet* (an object stopped or left). Per BATCH=4
events the chip emits ONE status word for the strongest collapsing cell {xq, yq,
strength, flag}. A cell fires as a REAL black hole (flag=1) only when collapse >=
COLLAPSE_THRESHOLD AND its slow baseline s >= S_MIN (so hot-pixel noise that never
built a baseline can't fake one). This host accumulates the emitted collapse cells
into decaying DARK gravity WELLS -- each spawns a dark implosion at its region with
a bright gravitational-lensing RING around it, fading per frame.

python_blackhole_words() below is a byte-for-byte port of the firmware's integer
logic (same fast-EMA bump, same fast decay + slow chase, same collapse = s-f clamp,
same argmax + threshold/floor gates) so what we render is provably what the chip would
emit given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_blackhole_view.py --validate                  # synthetic self-test (numbers)
  dvs_blackhole_view.py --from-actsim results.mem   # render real chip status words
  dvs_blackhole_view.py events.csv                  # render (host-computed) from a CSV
  dvs_blackhole_view.py ... --headless --save blackhole.png
"""
import argparse
import numpy as np

# --- must match software/dvs_blackhole/main.c exactly ---
SX, SY = 126, 112
BATCH = 4

# Coarse region grid: 8x8-px cells, 16 cols x 14 rows, stride 16 (power of two).
XQ_SHIFT = 3          # 8-px cols: 125>>3 = 15 -> cols 0..15
YQ_SHIFT = 3          # 8-px rows: 111>>3 = 13 -> rows 0..13
CELL_COLS = 16
CELL_ROWS = 14
STRIDE_SHIFT = 4
CELL_STRIDE = 1 << STRIDE_SHIFT      # 16
CELL_CELLS = CELL_STRIDE * CELL_ROWS  # 224

# Two-EMA parameters.
STEP = 128
EMA_CAP = 255
FAST_DECAY_SHIFT = 3   # f -= f>>3                  (short memory, burst-tolerant)
CHASE_UP_SHIFT = 1     # s += (f-s)>>1 while f>s    (baseline chases UP fast)
CHASE_DOWN_SHIFT = 5   # s -= (s-f)>>5 while f<s    (baseline leaks DOWN slowly)
DECAY_INTERVAL = 8
COLLAPSE_THRESHOLD = 48
S_MIN = 64
STRENGTH_SHIFT = 3


def python_blackhole_words(x, y):
    """Bit-faithful port of software/dvs_blackhole/main.c's ISR: one status word per
    BATCH=4 events. x,y are per-event arrays. Returns a list of packed status words
    (the same integers the chip writes to FIFO_OUT). Only x,y matter to the firmware
    (ts/pol are decoded but unused for the collapse metric)."""
    fast = [0] * CELL_CELLS
    slow = [0] * CELL_CELLS
    batch_count = 0
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        # Bump the FAST EMA for every event's coarse region (saturating add).
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            xq = xi >> XQ_SHIFT
            yq = yi >> YQ_SHIFT
            cell = (yq << STRIDE_SHIFT) | xq
            fast[cell] = min(fast[cell] + STEP, EMA_CAP)

        # Periodic tick: fast sheds a big fraction (short memory), then the slow
        # baseline chases the fast EMA a fraction (long memory: it lags).
        batch_count += 1
        if batch_count >= DECAY_INTERVAL:
            batch_count = 0
            for c in range(CELL_CELLS):
                f = fast[c] - (fast[c] >> FAST_DECAY_SHIFT)
                fast[c] = f
                s = slow[c]
                if f > s:
                    s = s + ((f - s) >> CHASE_UP_SHIFT)     # chase up (fast)
                else:
                    s = s - ((s - f) >> CHASE_DOWN_SHIFT)   # chase down (slow)
                slow[c] = s

        # Argmax over collapse = clamp(s - f, >=0).
        best_cell = 0
        best_collapse = 0
        best_slow = 0
        for c in range(CELL_CELLS):
            s = slow[c]
            f = fast[c]
            collapse = (s - f) if s > f else 0
            if collapse > best_collapse:
                best_collapse = collapse
                best_cell = c
                best_slow = s

        flag = 1 if (best_collapse >= COLLAPSE_THRESHOLD and best_slow >= S_MIN) else 0
        strength = min(best_collapse >> STRENGTH_SHIFT, 31)
        xq = best_cell & (CELL_STRIDE - 1)
        yq = best_cell >> STRIDE_SHIFT

        words.append((flag << 13) | (strength << 8) | (yq << 4) | xq)
    return words


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bits[3:0]=xq, bits[7:4]=yq, bits[12:8]=strength, bit[13]=flag."""
    xq = word & 0xF
    yq = (word >> 4) & 0xF
    strength = (word >> 8) & 0x1F
    flag = (word >> 13) & 1
    return xq, yq, strength, flag


# ---------------------------------------------------------------------------
# Gravity-well renderer: dark imploding wells + a bright lensing ring, into a
# decaying field. (Host may use float/multiply freely -- only firmware is constrained.)
# ---------------------------------------------------------------------------
def accumulate_wells(words, decay=0.90):
    """Accumulate the emitted collapse cells into two decaying float fields over the
    SX x SY sensor frame: a DARK `well` field (subtracts light -- the implosion) and
    a bright `ring` field (the gravitational-lensing halo). Each REAL (flag=1) status
    word deepens a dark gaussian well at its region centre and adds a bright ring
    shell just outside it; both fields decay multiplicatively per word so wells
    implode and fade. Returns (well, ring) 2-D float buffers (rows=y)."""
    W, H = SX, SY
    well = np.zeros((H, W), dtype=np.float64)
    ring = np.zeros((H, W), dtype=np.float64)
    yy, xx = np.mgrid[0:H, 0:W]
    cell_px = 1 << XQ_SHIFT   # 8-px region -> pixel centre
    for w in words:
        xq, yq, strength, flag = unpack_status(w)
        # Multiplicative decay each step so wells implode/fade (shimmer between hits).
        well *= decay
        ring *= decay
        if not flag:
            continue
        cx = xq * cell_px + cell_px / 2.0
        cy = yq * cell_px + cell_px / 2.0
        depth = 0.25 + 0.75 * (strength / 31.0)
        r2 = (xx - cx) ** 2 + (yy - cy) ** 2
        core = 5.0    # dark-core sigma (px)
        halo = 8.0    # lensing-ring radius (px)
        # Dark imploding core: a negative gaussian (the "black hole").
        well += np.exp(-r2 / (2.0 * core * core)) * depth
        # Bright gravitational-lensing ring: a thin gaussian shell at radius `halo`.
        rr = np.sqrt(r2)
        ring += np.exp(-((rr - halo) ** 2) / (2.0 * 2.0 * 2.0)) * depth
    return well, ring


def blackhole_rgb(well, ring):
    """Composite the dark wells + bright lensing rings over a dim starfield-ish
    backdrop into an (H,W,3) float RGB image 0..1. Wells SUBTRACT light (imploding
    darkness); rings ADD a cool blue-white shimmer (gravitational lensing)."""
    wpeak = well.max()
    winv = 1.0 / wpeak if wpeak > 0 else 0.0
    rpeak = ring.max()
    rinv = 1.0 / rpeak if rpeak > 0 else 0.0
    wn = np.clip(well * winv, 0, 1)
    rn = np.clip(ring * rinv, 0, 1)
    # Dim deep-space backdrop.
    r = np.full_like(wn, 0.04)
    g = np.full_like(wn, 0.05)
    b = np.full_like(wn, 0.09)
    # Wells carve darkness out of the backdrop (multiply down toward black).
    dark = 1.0 - 0.95 * wn
    r *= dark; g *= dark; b *= dark
    # Lensing ring: cool blue-white halo added on top.
    r += 0.55 * rn
    g += 0.75 * rn
    b += 1.00 * rn
    rgb = np.stack([r, g, b], axis=-1)
    return np.clip(rgb, 0, 1)


def render_blackhole(words, save=None, headless=False, decay=0.90):
    """Accumulate the emitted collapse cells into decaying dark wells + lensing rings
    and composite the imploding black-hole image. --headless renders without a
    display."""
    well, ring = accumulate_wells(words, decay=decay)
    rgb = blackhole_rgb(well, ring)

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable:", e)
        print("Black-hole field (max channel per row, coarse):")
        np.set_printoptions(precision=2, suppress=True, linewidth=200)
        print(np.round(rgb.max(axis=2), 2))
        return

    fig, ax = plt.subplots(figsize=(SX / 24, SY / 24))
    ax.imshow(rgb, interpolation="bilinear", origin="upper")
    ax.set_title('"Micro-Event Black Holes" -- imploding wells where motion collapsed')
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=110)
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: a blob active in region A that STOPS/leaves (-> a black
# hole must fire at A), plus a region B active the WHOLE time (steady-active, must
# NOT fire) and a region C that is empty throughout (steady-empty, must NOT fire).
# ---------------------------------------------------------------------------
def gen_synthetic(seed=0):
    """Build an event stream with three regions:
      A (~cols 32..48, rows 24..40 px -> cell region xq~4..6, yq~3..5): a dense blob
        active for the FIRST half, then it STOPS (no more events) -> should collapse.
      B (~cols 88..104 px -> xq~11..13): active the ENTIRE time -> steady-active.
      C (empty): never receives events -> steady-empty.
    Returns (x, y, ts, pol) plus the (xq,yq) region for A and B."""
    rng = np.random.default_rng(seed)
    x, y, pol, ts = [], [], [], []
    t = 0
    # Centre each blob squarely inside one 8-px region so a spread=2 blob stays in a
    # single cell (px 44 -> xq=5; px 36 -> yq=4; px 100 -> xq=12).
    A_cx, A_cy = 44, 36      # region A centre (px) -> xq=5, yq=4
    B_cx, B_cy = 100, 36     # region B centre (px) -> xq=12, yq=4
    total_steps = 800
    active_half = total_steps // 2

    def emit(cx, cy, spread=2):
        nonlocal t
        px = int(np.clip(cx + rng.normal(0, spread), 0, SX - 1))
        py = int(np.clip(cy + rng.normal(0, spread), 0, SY - 1))
        t = (t + rng.integers(20, 120)) & 0xFFFF
        x.append(px); y.append(py); ts.append(int(t)); pol.append(int(rng.integers(0, 2)))

    for s in range(total_steps):
        # Region A: dense while active, then completely silent.
        if s < active_half:
            for _ in range(6):
                emit(A_cx, A_cy)
        # Region B: active the whole time (steady-active).
        for _ in range(6):
            emit(B_cx, B_cy)
        # A little scattered background noise (never builds a baseline anywhere).
        if rng.random() < 0.2:
            px = int(rng.integers(0, SX)); py = int(rng.integers(0, SY))
            t = (t + rng.integers(20, 120)) & 0xFFFF
            x.append(px); y.append(py); ts.append(int(t)); pol.append(int(rng.integers(0, 2)))

    A_region = (A_cx >> XQ_SHIFT, A_cy >> YQ_SHIFT)
    B_region = (B_cx >> XQ_SHIFT, B_cy >> YQ_SHIFT)
    return (np.array(x, dtype=np.int64), np.array(y, dtype=np.int64),
            np.array(ts, dtype=np.int64), np.array(pol, dtype=np.int64),
            A_region, B_region)


def validate():
    x, y, ts, pol, A_region, B_region = gen_synthetic()
    words = python_blackhole_words(x, y)
    print(f"synthetic: {len(x)} events, {len(words)} status words; "
          f"A(stop)@region{A_region}, B(steady-active)@region{B_region}")

    ok = True
    unpacked = [unpack_status(w) for w in words]

    # 1. Every reported cell index is in range and fields well-formed.
    idx_ok = all(0 <= u[0] < CELL_COLS and 0 <= u[1] < CELL_ROWS
                 and 0 <= u[2] <= 31 and u[3] in (0, 1) for u in unpacked)
    print(f"  cell indices in [0,{CELL_COLS - 1}]x[0,{CELL_ROWS - 1}], "
          f"strength 0..31, flag 0/1: {'OK' if idx_ok else 'FAIL'}")
    ok = ok and idx_ok

    # 2. A black hole FIRES at region A, and does so AFTER A stops. A's events end
    #    at the stream midpoint of A/B activity; since A fires only once its fast EMA
    #    has drained, require some real fires in the LAST THIRD of the words (well
    #    after A went quiet -- a collapse can't be a live-activity artifact).
    fires = [(i, u) for i, u in enumerate(unpacked) if u[3] == 1]
    fired_A = [(i, u) for i, u in fires if (u[0], u[1]) == A_region]
    late = 2 * len(words) // 3
    fired_A_after_stop = [i for i, _ in fired_A if i >= late]
    a_ok = len(fired_A) > 0 and len(fired_A_after_stop) > 0
    print(f"  black hole fires at A{A_region}: {len(fired_A)} real fires "
          f"({len(fired_A_after_stop)} in the last third, after A stops) "
          f"-> {'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # 3. The STEADY-ACTIVE region B does NOT fire (it stays busy the whole time, so
    #    its re-bumped fast EMA never drains below the slow baseline -> collapse ~0).
    #    Assert no real fire ever lands on B.
    fired_B = [(i, u) for i, u in fires if (u[0], u[1]) == B_region]
    b_ok = len(fired_B) == 0
    print(f"  steady-active B{B_region} does NOT fire: "
          f"{len(fired_B)} fires -> {'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # 4. STEADY-EMPTY regions do NOT fire. Take several regions that never received a
    #    blob (corners + a mid strip away from A and B) and assert none ever fires --
    #    scattered hot-pixel noise never builds a baseline, so it can't collapse.
    empty_regions = {(0, 0), (CELL_COLS - 1, 0), (0, CELL_ROWS - 1),
                     (CELL_COLS - 1, CELL_ROWS - 1), (8, 10), (2, 7)}
    fired_regions = {(u[0], u[1]) for _, u in fires}
    fired_empty = empty_regions & fired_regions
    empty_ok = len(fired_empty) == 0
    print(f"  steady-empty regions {sorted(empty_regions)} do NOT fire "
          f"(offenders={sorted(fired_empty)}): {'OK' if empty_ok else 'FAIL'}")
    ok = ok and empty_ok

    # 5. A is the DOMINANT collapse (it was the deliberate, sustained active->quiet
    #    region), so it must take the plurality of real fires; and the renderer must
    #    carve a well centred on A. Accumulate wells over ONLY A's fires (a decaying
    #    field otherwise reflects just the most-recent word) and confirm the darkest
    #    point sits in A's region.
    from collections import Counter
    fire_counts = Counter((u[0], u[1]) for _, u in fires)
    top_region = fire_counts.most_common(1)[0][0] if fire_counts else None
    dominant_ok = top_region == A_region
    print(f"  A is the dominant collapse (top fire region={top_region}, "
          f"A fires={fire_counts.get(A_region, 0)}): {'OK' if dominant_ok else 'FAIL'}")
    ok = ok and dominant_ok

    A_words = [w for w, u in zip(words, unpacked) if u[3] == 1 and (u[0], u[1]) == A_region]
    well, ring = accumulate_wells(A_words, decay=1.0)   # no decay: pure spatial sum
    cell_px = 1 << XQ_SHIFT
    ax_px = int(A_region[0] * cell_px + cell_px / 2)
    ay_px = int(A_region[1] * cell_px + cell_px / 2)
    well_peak = np.unravel_index(well.argmax(), well.shape)   # (row=y, col=x)
    near = abs(well_peak[0] - ay_px) <= cell_px and abs(well_peak[1] - ax_px) <= cell_px
    well_ok = well.max() > 0 and ring.max() > 0 and near
    print(f"  renderer carves a well + lensing ring at A (well peak {well_peak[::-1]} "
          f"near A px ({ax_px},{ay_px}), ring max={ring.max():.2f}): "
          f"{'OK' if well_ok else 'FAIL'}")
    ok = ok and well_ok

    print()
    print("VALIDATION:", "PASS -- cell/strength/flag in range; a black hole fires at "
          "the collapsing region A (after it stops); the steady-active region B does "
          "NOT fire; steady-empty regions do NOT fire; A is the dominant collapse; and "
          "the renderer carves a well + lensing ring at A"
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
    return x, y


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: cell/strength/flag in range, a black "
                         "hole fires at a collapsing region, steady-active + "
                         "steady-empty regions do NOT fire (prints numbers)")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the black-hole PNG here")
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
        x, y = load_csv(args.csv)
        print(f"loaded {len(x)} events from {args.csv}; computing status words in "
              f"Python (bit-faithful mirror of firmware).")
        words = python_blackhole_words(x, y)
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_blackhole(words, save=args.save, headless=args.headless, decay=args.decay)


if __name__ == "__main__":
    main()
