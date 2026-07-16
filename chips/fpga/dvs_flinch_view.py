#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_flinch/main.c
("The Flinch" -- a biological looming detector: a giant eye that flinches when
something LUNGES at the camera, ignoring waving / walking / panning).

The chip runs the locust LGMD looming principle. Approaching objects grow LARGER in
the image; the chip measures the ACTIVE AREA -- the number of coarse 8x8-px cells the
object covers (a cell counts only once it clears a MIN_EVENTS noise floor) -- and
watches its trend. Per WINDOW the looming score is S = area - prev_area, CLAMPED to
+/-S_CLAMP so a single whole-field jump (an object simply appearing) can't fire on
its own. A leaky accumulator A += S - (A>>SHIFT) integrates S, and a FLINCH latches
(with a short refractory) when A crosses FLINCH_THRESHOLD. A GROWING object (approach)
keeps the area rising -> S sustained positive -> fires; a CONSTANT-size pan keeps the
area flat -> S ~= 0 -> silent (area is translation-invariant -- this is the crux); a
SHRINKING/receding object lowers the area -> S negative -> silent. A focus of
expansion (cx,cy) is tracked divide-free as a running spatial median (nudge one step
toward each event) and emitted so the host eye glares at the action. Per BATCH=4
events the chip emits ONE status word {flinch, level, cx, cy}. This host draws a
GIANT EYE whose pupil dilates with `level` and SNAPS SHUT / recoils (screen-shake) on
`flinch`, glaring its focus at (cx,cy).

python_flinch_words() below is a byte-for-byte port of the firmware's integer logic
(same running median, same per-cell MIN_EVENTS area count, same clamped area-trend
score, same leaky accumulator + threshold/refractory) so what we render is provably
what the chip would emit given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_flinch_view.py --validate                  # synthetic self-test (numbers)
  dvs_flinch_view.py --from-actsim results.mem   # render real chip status words
  dvs_flinch_view.py events.csv                  # render (host-computed) from a CSV
  dvs_flinch_view.py ... --headless --save flinch.png
"""
import argparse
import numpy as np

# --- must match software/dvs_flinch/main.c exactly ---
SX, SY = 126, 112
BATCH = 4

# Coarse cell grid: 8x8-px cells, 16 cols x 14 rows, stride 16 (power of two).
XQ_SHIFT = 3          # 8-px cols: 125>>3 = 15 -> cols 0..15
YQ_SHIFT = 3          # 8-px rows: 111>>3 = 13 -> rows 0..13
CELL_COLS = 16
CELL_ROWS = 14
STRIDE_SHIFT = 4
CELL_STRIDE = 1 << STRIDE_SHIFT       # 16
CELL_CELLS = CELL_STRIDE * CELL_ROWS  # 224

# Timebase + noise + accumulator parameters.
WINDOW_BATCHES = 120
MIN_EVENTS = 3
S_CLAMP = 3
ACC_LEAK_SHIFT = 4
ACC_CAP = 2047
FLINCH_THRESHOLD = 20
REFRACTORY_WINDOWS = 6
LEVEL_SHIFT = 3


def python_flinch_words(x, y):
    """Bit-faithful port of software/dvs_flinch/main.c's ISR: one status word per
    BATCH=4 events. x,y are per-event arrays. Returns a list of packed status words
    (the same integers the chip writes to FIFO_OUT). Only x,y matter to the firmware
    (ts/pol are decoded but unused for the looming mechanism).

    The looming statistic is the per-window CHANGE in ACTIVE AREA (count of cells
    that cleared the MIN_EVENTS noise floor), clamped to +/-S_CLAMP so a single
    whole-field jump can't fire alone; a leaky accumulator integrates it and a FLINCH
    latches (with a refractory) when it crosses FLINCH_THRESHOLD. A growing object
    (approach) raises the area steadily -> fires; a constant-size pan keeps the area
    flat -> silent; a shrinking/receding object lowers it -> silent."""
    count = [0] * CELL_CELLS        # per-cell event count this window
    prev_area = 0                   # active cell count from the previous window
    cx = SX // 2                    # focus X (running median), matches firmware init
    cy = SY // 2                    # focus Y (running median)
    acc = 0
    window_batches = 0
    refractory = 0
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        # Per event: nudge the running-median focus + bump this window's cell count.
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            if xi > cx:
                cx += 1
            elif xi < cx:
                cx -= 1
            if yi > cy:
                cy += 1
            elif yi < cy:
                cy -= 1
            xq = xi >> XQ_SHIFT
            yq = yi >> YQ_SHIFT
            cell = (yq << STRIDE_SHIFT) | xq
            if count[cell] < 255:
                count[cell] += 1

        window_batches += 1
        flinch_pulse = 0

        if window_batches >= WINDOW_BATCHES:
            window_batches = 0

            # Active AREA: how many cells cleared the MIN_EVENTS noise floor (the
            # object's covered size in cells). Clear counts for the next window.
            area = 0
            for c in range(CELL_CELLS):
                if count[c] >= MIN_EVENTS:
                    area += 1
                count[c] = 0

            # Looming score S = per-window change in area, clamped so a lone
            # whole-field jump (an appearance) can't fire -- only a sustained rise.
            S = area - prev_area
            prev_area = area
            if S > S_CLAMP:
                S = S_CLAMP
            if S < -S_CLAMP:
                S = -S_CLAMP

            acc = acc + S - (acc >> ACC_LEAK_SHIFT)
            if acc < 0:
                acc = 0
            if acc > ACC_CAP:
                acc = ACC_CAP

            if refractory > 0:
                refractory -= 1
            elif acc > FLINCH_THRESHOLD:
                flinch_pulse = 1
                refractory = REFRACTORY_WINDOWS

        level = min(acc >> LEVEL_SHIFT, 63)
        ecx = cx & 0x7F
        ecy = cy & 0x7F
        words.append((ecy << 14) | (ecx << 7) | (level << 1) | (flinch_pulse & 1))
    return words


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bit0=flinch, bits[6:1]=level, bits[13:7]=cx, bits[20:14]=cy."""
    flinch = word & 1
    level = (word >> 1) & 0x3F
    cx = (word >> 7) & 0x7F
    cy = (word >> 14) & 0x7F
    return flinch, level, cx, cy


# ---------------------------------------------------------------------------
# Giant-eye renderer: a big glaring eye whose pupil dilates with `level` and snaps
# shut / recoils (screen-shake) on `flinch`. (Host may use float/multiply freely --
# only firmware is constrained.)
# ---------------------------------------------------------------------------
def eye_rgb(level, cx, cy, flinch, shake=0.0):
    """Render one (H,W,3) float RGB frame of the giant eye over the SX x SY sensor
    frame. `level` (0..63) sets iris tension + pupil dilation; the eye's gaze points
    toward the focus (cx,cy); `flinch` snaps the lid shut and `shake` offsets the
    whole eye (screen-shake recoil)."""
    W, H = SX, SY
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    ecx, ecy = W / 2.0 + shake, H / 2.0 + shake * 0.6
    # Eyeball: a bright sclera disc.
    eye_r = min(W, H) * 0.46
    r = np.sqrt((xx - ecx) ** 2 + (yy - ecy) ** 2)
    sclera = np.clip(1.0 - (r - eye_r) / 3.0, 0.0, 1.0)   # soft-edged disc mask

    # Gaze: iris/pupil shifted toward the looming focus (cx,cy).
    gx = ecx + (cx - W / 2.0) * 0.5
    gy = ecy + (cy - H / 2.0) * 0.5
    rp = np.sqrt((xx - gx) ** 2 + (yy - gy) ** 2)

    tension = level / 63.0
    iris_r = eye_r * 0.5
    pupil_r = eye_r * (0.12 + 0.30 * tension)             # pupil dilates with level

    iris = np.clip(1.0 - (rp - iris_r) / 4.0, 0.0, 1.0)
    pupil = np.clip(1.0 - (rp - pupil_r) / 2.0, 0.0, 1.0)

    # Compose: dark backdrop, white sclera, coloured iris (reddens with tension),
    # black pupil.
    rr = np.full((H, W), 0.03)
    gg = np.full((H, W), 0.03)
    bb = np.full((H, W), 0.05)
    # sclera (off-white)
    rr = rr * (1 - sclera) + 0.92 * sclera
    gg = gg * (1 - sclera) + 0.90 * sclera
    bb = bb * (1 - sclera) + 0.85 * sclera
    # iris: amber -> angry red as tension rises
    ir_r = 0.60 + 0.40 * tension
    ir_g = 0.45 * (1 - tension) + 0.10
    ir_b = 0.10
    m = iris * sclera
    rr = rr * (1 - m) + ir_r * m
    gg = gg * (1 - m) + ir_g * m
    bb = bb * (1 - m) + ir_b * m
    # pupil (black)
    p = pupil * sclera
    rr = rr * (1 - p)
    gg = gg * (1 - p)
    bb = bb * (1 - p)

    rgb = np.stack([rr, gg, bb], axis=-1)

    if flinch:
        # Lid snaps shut: horizontal band collapses the eye to a slit + red flash.
        lid = np.clip(1.0 - np.abs(yy - ecy) / (H * 0.06), 0.0, 1.0)[:, :, None]
        flash = np.array([0.25, 0.02, 0.02])
        rgb = rgb * lid + flash * (1 - lid)
    return np.clip(rgb, 0, 1)


def render_flinch(words, save=None, headless=False):
    """Render the giant eye from the emitted status words. We drive it from the LAST
    word (the freshest state) but boost the shake if any recent word flinched, so a
    single frame conveys the flinch. --headless renders without a display."""
    if words:
        flinch_any = any(unpack_status(w)[0] for w in words[-WINDOW_BATCHES:])
        f, level, cx, cy = unpack_status(words[-1])
        flinch = 1 if (f or flinch_any) else 0
        shake = 4.0 if flinch else 0.0
    else:
        level, cx, cy, flinch, shake = 0, SX // 2, SY // 2, 0, 0.0
    rgb = eye_rgb(level, cx, cy, flinch, shake=shake)

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable:", e)
        print("Eye frame (max channel per row, coarse):")
        np.set_printoptions(precision=2, suppress=True, linewidth=200)
        print(np.round(rgb.max(axis=2), 2))
        return

    fig, ax = plt.subplots(figsize=(SX / 24, SY / 24))
    ax.imshow(rgb, interpolation="bilinear", origin="upper")
    title = '"The Flinch" -- ' + ("FLINCH!" if flinch else f"tension {level}/63")
    ax.set_title(title)
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=110)
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: three textured discs over ~40 windows each -- (a) EXPANDING
# (looming -> flinch), (b) TRANSLATING at constant size (pan -> no flinch), (c)
# SHRINKING (recede -> no flinch). The translation-silent case is the crux. Each disc
# is filled with texture events (a real approaching object fires events across its
# whole area) and interpolated CONTINUOUSLY per event so its size/position changes
# smoothly, and dense enough (>= WINDOW_BATCHES*BATCH events per window) that covered
# cells reliably clear the MIN_EVENTS area floor.
# ---------------------------------------------------------------------------
# Aim for ~40 windows so the accumulator has room to build a sustained trend.
_GEN_WINDOWS = 40
_GEN_TOTAL = _GEN_WINDOWS * WINDOW_BATCHES * BATCH   # events per synthetic stream


def _filled_disc_stream(centre_fn, radius_fn, total, seed):
    """Generate `total` texture events; for event i (fraction f=i/total) the object
    centre is centre_fn(f) and radius radius_fn(f). Area-uniform sampling
    (r = R*sqrt(u)) keeps the disc SOLID so its covered-cell AREA tracks its size."""
    rng = np.random.default_rng(seed)
    x = np.empty(total, dtype=np.int64)
    y = np.empty(total, dtype=np.int64)
    for i in range(total):
        f = i / total
        cx, cy = centre_fn(f)
        r = radius_fn(f)
        ang = rng.uniform(0, 2 * np.pi)
        rad = r * np.sqrt(rng.uniform(0, 1))
        x[i] = int(np.clip(cx + rad * np.cos(ang), 0, SX - 1))
        y[i] = int(np.clip(cy + rad * np.sin(ang), 0, SY - 1))
    return x, y


def gen_expanding(seed=1):
    """A textured disc CENTRED in frame whose radius GROWS 6 -> 50 px -- a looming
    approach. Its covered AREA rises monotonically, so the area-trend score stays
    positive over many windows -> the accumulator crosses threshold -> a flinch."""
    return _filled_disc_stream(lambda f: (SX / 2, SY / 2),
                               lambda f: 6 + 44 * f, _GEN_TOTAL, seed)


def gen_translating(seed=2):
    """A CONSTANT-size (radius 24) textured disc panning across the frame -- pure
    translation. Its covered AREA stays ~CONSTANT (area is translation-invariant), so
    the area-trend score is ~0 every window -> NO flinch. THIS IS THE CRUX CASE."""
    return _filled_disc_stream(lambda f: (28 + 70 * f, SY / 2),
                               lambda f: 24, _GEN_TOTAL, seed)


def gen_shrinking(seed=3):
    """A textured disc CENTRED in frame whose radius SHRINKS 50 -> 6 px -- a receding
    object. Its covered AREA falls monotonically, so the area-trend score is negative
    -> the accumulator sinks -> NO flinch."""
    return _filled_disc_stream(lambda f: (SX / 2, SY / 2),
                               lambda f: 50 - 44 * f, _GEN_TOTAL, seed)


def _fired(words):
    return sum(unpack_status(w)[0] for w in words)


def _peak_level(words):
    return max((unpack_status(w)[1] for w in words), default=0)


def validate():
    ok = True

    # (a) EXPANDING disc (looming) -> flinch DOES fire.
    xe, ye = gen_expanding()
    we = python_flinch_words(xe, ye)
    loom_fires = _fired(we)
    a_ok = loom_fires > 0
    print(f"  (a) EXPANDING disc (looming): {len(xe)} events, {len(we)} words, "
          f"flinches={loom_fires}, peak level={_peak_level(we)} -> "
          f"{'OK (fires)' if a_ok else 'FAIL (should fire)'}")
    ok = ok and a_ok

    # (b) TRANSLATING disc (pan) -> flinch does NOT fire (the crux).
    xt, yt = gen_translating()
    wt = python_flinch_words(xt, yt)
    trans_fires = _fired(wt)
    b_ok = trans_fires == 0
    print(f"  (b) TRANSLATING disc (pan):   {len(xt)} events, {len(wt)} words, "
          f"flinches={trans_fires}, peak level={_peak_level(wt)} -> "
          f"{'OK (silent)' if b_ok else 'FAIL (should be silent)'}")
    ok = ok and b_ok

    # (c) SHRINKING disc (recede) -> flinch does NOT fire.
    xs, ys = gen_shrinking()
    ws = python_flinch_words(xs, ys)
    recede_fires = _fired(ws)
    c_ok = recede_fires == 0
    print(f"  (c) SHRINKING disc (recede):  {len(xs)} events, {len(ws)} words, "
          f"flinches={recede_fires}, peak level={_peak_level(ws)} -> "
          f"{'OK (silent)' if c_ok else 'FAIL (should be silent)'}")
    ok = ok and c_ok

    # Field/word well-formedness on the looming stream.
    fields_ok = all(0 <= unpack_status(w)[1] <= 63
                    and 0 <= unpack_status(w)[2] <= 125
                    and 0 <= unpack_status(w)[3] <= 111
                    and unpack_status(w)[0] in (0, 1) for w in we)
    print(f"  status fields well-formed (flinch 0/1, level 0..63, cx/cy in frame): "
          f"{'OK' if fields_ok else 'FAIL'}")
    ok = ok and fields_ok

    # The looming accumulator visibly out-tensions translation (a sanity margin, not
    # just the binary flinch): the looming peak level must exceed the translation
    # peak level.
    margin_ok = _peak_level(we) > _peak_level(wt)
    print(f"  looming peak level ({_peak_level(we)}) > translation peak "
          f"({_peak_level(wt)}): {'OK' if margin_ok else 'FAIL'}")
    ok = ok and margin_ok

    print()
    print("VALIDATION:", "PASS -- looming (expanding disc) FIRES the flinch; "
          "translation (constant-size pan) does NOT; recede (shrinking disc) does "
          "NOT; status fields well-formed; looming out-tensions translation"
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
                    help="synthetic self-test: looming fires, translation + recede "
                         "stay silent (prints numbers)")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the eye PNG here")
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
        words = python_flinch_words(x, y)
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_flinch(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
