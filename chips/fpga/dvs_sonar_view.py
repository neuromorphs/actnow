#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_sonar/main.c
("Radial Motion Oracle" -- a living sonar/radar oracle).

The chip reduces every event's position, relative to the frame CENTRE (CX,CY), to
a coarse POLAR coordinate: an OCTANT (0..7, which 45-degree compass wedge) and a
RADIUS (quantized Chebyshev distance r>>1, 0..31). Events vote into an 8-entry
LEAKY histogram; per BATCH=4 events it emits ONE status word for the DOMINANT
octant -- {octant, radius, pol, strength, flag}. This host unpacks those words and
animates each emission as an expanding SONAR RIPPLE: a ring spawned at that polar
position that grows outward and fades over frames, coloured by octant, hued by
polarity (ON warm / OFF cool). A circular sweep around the centre therefore paints
rings marching outward in every direction -- a radar/oracle display.

python_sonar_words() below is a byte-for-byte port of the firmware's integer logic
(same signed offset, same compass octant from pure compares, same Chebyshev
radius, same saturating warm + leaky decay + argmax + threshold emit) so what we
render is provably what the chip would emit given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_sonar_view.py --validate                  # synthetic self-test (numbers)
  dvs_sonar_view.py --from-actsim results.mem   # render real chip status words
  dvs_sonar_view.py events.csv                  # render (host-computed) from a CSV
  dvs_sonar_view.py ... --headless --save sonar.png
"""
import argparse
import numpy as np

# --- must match software/dvs_sonar/main.c exactly ---
SX, SY = 126, 112
CX, CY = 63, 56
RADIUS_SHIFT = 1
RADIUS_MAX = 31
NUM_OCTANTS = 8
STEP = 24
HIST_CAP = 255
DECAY_SHIFT = 2
EMIT_THRESHOLD = 64
BATCH = 4

# Compass octant unit vectors (x right, y DOWN the sensor; N = dy<0 = up). Index
# is the octant field; matches octant_of() in the firmware and OCTANT_VEC in the
# dashboard: 0=E 1=NE 2=N 3=NW 4=W 5=SW 6=S 7=SE.
OCTANT_VEC = [(1, 0), (1, -1), (0, -1), (-1, -1),
              (-1, 0), (-1, 1), (0, 1), (1, 1)]
OCTANT_NAMES = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]


def octant_of(dx, dy):
    """Bit-faithful port of the firmware's octant_of(): compass wedge (0..7) from
    the signed offset, using pure sign+magnitude compares (no atan2, no multiply).
    +x right, +y down; N is dy<0."""
    adx = -dx if dx < 0 else dx
    ady = -dy if dy < 0 else dy
    if adx >= ady:
        return 0 if dx >= 0 else 4          # E : W
    else:
        if dy < 0:
            return 1 if dx >= 0 else 3      # NE : NW
        else:
            return 7 if dx >= 0 else 5      # SE : SW


def python_sonar_words(x, y, pol):
    """Bit-faithful port of software/dvs_sonar/main.c's ISR: one status word per
    BATCH=4 events. x,y,pol are per-event arrays. Returns a list of packed status
    words (the same integers the chip writes to FIFO_OUT)."""
    hist = [0] * NUM_OCTANTS
    words = []
    n = len(x)
    for b in range(0, n - n % BATCH, BATCH):
        radius_sum = [0] * NUM_OCTANTS
        radius_cnt = [0] * NUM_OCTANTS
        last_pol = [0] * NUM_OCTANTS

        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            pi = int(pol[i]) & 1
            dx = xi - CX
            dy = yi - CY
            oct_ = octant_of(dx, dy)

            adx = -dx if dx < 0 else dx
            ady = -dy if dy < 0 else dy
            cheb = adx if adx >= ady else ady
            rq = cheb >> RADIUS_SHIFT
            if rq > RADIUS_MAX:
                rq = RADIUS_MAX

            hist[oct_] = min(hist[oct_] + STEP, HIST_CAP)
            radius_sum[oct_] += rq
            radius_cnt[oct_] += 1
            last_pol[oct_] = pi

        # Leaky decay of the whole histogram, once per batch.
        for o in range(NUM_OCTANTS):
            hist[o] = hist[o] - (hist[o] >> DECAY_SHIFT)

        # Argmax over the leaky counters.
        best_oct, best_val = 0, 0
        for o in range(NUM_OCTANTS):
            if hist[o] > best_val:
                best_val, best_oct = hist[o], o

        # Leaky-averaged radius via count-based right shift (cnt 1..BATCH).
        out_radius = 0
        cnt = radius_cnt[best_oct]
        if cnt > 0:
            shift = 2 if cnt >= 3 else (1 if cnt == 2 else 0)
            out_radius = radius_sum[best_oct] >> shift
            if out_radius > RADIUS_MAX:
                out_radius = RADIUS_MAX

        out_pol = last_pol[best_oct] & 1
        strength = best_val >> 3
        if strength > 31:
            strength = 31
        flag = 1 if best_val >= EMIT_THRESHOLD else 0

        words.append((flag << 14) | (strength << 9) | (out_pol << 8)
                     | (out_radius << 3) | best_oct)
    return words


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bits[2:0]=octant, bits[7:3]=radius, bit[8]=pol, bits[13:9]=strength,
    bit[14]=flag."""
    octant = word & 0x7
    radius = (word >> 3) & 0x1F
    pol = (word >> 8) & 1
    strength = (word >> 9) & 0x1F
    flag = (word >> 14) & 1
    return octant, radius, pol, strength, flag


# ---------------------------------------------------------------------------
# Sonar ripple animation.
# ---------------------------------------------------------------------------
def ripple_position(octant, radius):
    """Polar (octant, radius) -> a canvas (col,row) point where the ripple is
    born, relative to the centre. radius is the quantized 0..31 value; scale it
    back to sensor pixels (~ * 2, undoing RADIUS_SHIFT). Returns (px, py) in the
    112(w) x 126(h) canvas convention (col = x-ish, row = y-ish)."""
    ux, uy = OCTANT_VEC[octant]
    # Normalise the (possibly diagonal) octant vector so the ring sits at the
    # requested radius regardless of wedge, then place it out from centre.
    mag = (ux * ux + uy * uy) ** 0.5 or 1.0
    r_px = (radius << RADIUS_SHIFT)          # back to sensor-pixel scale
    # Canvas is 112 wide (x axis) x 126 tall (y axis) in the dashboard layout;
    # centre it on (CX-ish, CY-ish). We render in a 112x126 buffer where col<->x,
    # row<->y so it lines up with the dashboard's appImage (row*112+col).
    cx_canvas = 112 * (CX / SX)
    cy_canvas = 126 * (CY / SY)
    px = cx_canvas + (ux / mag) * r_px
    py = cy_canvas + (uy / mag) * r_px
    return px, py


def octant_color(octant, pol):
    """Colour a ripple: hue by octant (spun around the wheel), brightness/tint by
    polarity (ON = warm, OFF = cool). Returns an (r,g,b) float triple 0..1."""
    import colorsys
    hue = octant / NUM_OCTANTS
    sat = 0.85
    val = 1.0 if pol else 0.7
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    # Nudge ON warmer, OFF cooler so polarity is legible on top of octant hue.
    if pol:
        r = min(1.0, r + 0.15)
    else:
        b = min(1.0, b + 0.15)
    return r, g, b


def render_sonar(words, save=None, headless=False, frames=None,
                 ring_life=18, ring_speed=3.0):
    """Animate the emitted status words as expanding, fading sonar rings and
    composite the final frame (all live rings at their current radius). Each word
    spawns a ring at its polar birth point that grows by ring_speed px/frame and
    fades over ring_life frames. For a static PNG we composite the accumulated
    ring field; --headless renders without a display."""
    W, H = 112, 126
    canvas = np.zeros((H, W, 3), dtype=np.float64)

    # Spawn a ring per status word (skip fallback/below-threshold pings faintly).
    # We advance a virtual clock one tick per word so later pings are "younger"
    # (smaller radius, brighter) -- the sweep marches outward over time.
    n = len(words)
    if frames is None:
        frames = n
    for k, w in enumerate(words):
        octant, radius, pol, strength, flag = unpack_status(w)
        age = frames - k                      # ticks since this ring spawned
        if age < 0 or age > ring_life:
            continue
        birth_px, birth_py = ripple_position(octant, radius)
        # Ring grows outward from its birth point over its life.
        ring_r = ring_speed * age
        fade = 1.0 - age / ring_life          # 1 at spawn -> 0 at death
        amp = fade * (0.3 + 0.7 * (strength / 31.0)) * (1.0 if flag else 0.4)
        r, g, b = octant_color(octant, pol)

        # Draw the ring as a thin annulus around (birth_px, birth_py).
        yy, xx = np.mgrid[0:H, 0:W]
        dist = np.hypot(xx - birth_px, yy - birth_py)
        band = np.exp(-((dist - ring_r) ** 2) / (2.0 * 2.5 ** 2))  # gaussian ring
        contrib = (amp * band)[:, :, None] * np.array([r, g, b])
        canvas += contrib

    canvas = np.clip(canvas, 0, 1)

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib unavailable:", e)
        print("Sonar ring field (max channel per row, coarse):")
        np.set_printoptions(precision=2, suppress=True, linewidth=200)
        print(np.round(canvas.max(axis=2), 2))
        return

    fig, ax = plt.subplots(figsize=(W / 24, H / 24))
    ax.imshow(canvas, interpolation="bilinear", origin="upper")
    ax.set_title('"Radial Motion Oracle" -- expanding sonar rings')
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=110)
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: an object sweeping in a circle around the centre.
# ---------------------------------------------------------------------------
def gen_synthetic(revolutions=3.0, events_per_step=6, steps=1200, seed=0):
    """A bright object orbiting the frame CENTRE plus scattered noise. Sweeping in
    a circle makes the dominant octant march 0->1->...->7->0, a good test that a
    circular sweep visits multiple octants. Returns (x, y, pol)."""
    rng = np.random.default_rng(seed)
    x, y, pol = [], [], []
    orbit_r = 40.0
    for t in range(steps):
        theta = 2.0 * np.pi * revolutions * (t / steps)
        # +x right, +y DOWN. Screen angle: object at (CX + r cos, CY + r sin).
        cx = CX + orbit_r * np.cos(theta)
        cy = CY + orbit_r * np.sin(theta)
        for _ in range(events_per_step):
            px = int(np.clip(cx + rng.normal(0, 3), 0, SX - 1))
            py = int(np.clip(cy + rng.normal(0, 3), 0, SY - 1))
            x.append(px); y.append(py); pol.append(int(rng.integers(0, 2)))
        if rng.random() < 0.25:               # a little background noise
            x.append(int(rng.integers(0, SX)))
            y.append(int(rng.integers(0, SY)))
            pol.append(int(rng.integers(0, 2)))
    return (np.array(x, dtype=np.int64),
            np.array(y, dtype=np.int64),
            np.array(pol, dtype=np.int64))


def validate():
    x, y, pol = gen_synthetic()
    words = python_sonar_words(x, y, pol)
    print(f"synthetic circular sweep: {len(x)} events, {len(words)} status words")

    ok = True
    unpacked = [unpack_status(w) for w in words]

    # 1. Every octant field is a valid 0..7 compass wedge.
    octs = [u[0] for u in unpacked]
    oct_ok = all(0 <= o < NUM_OCTANTS for o in octs)
    print(f"  octant in range [0,7]: {'OK' if oct_ok else 'FAIL'}")
    ok = ok and oct_ok

    # 2. Every radius field is inside the 5-bit quantized range.
    radii = [u[1] for u in unpacked]
    max_r = max(radii) if radii else 0
    rad_ok = all(0 <= r <= RADIUS_MAX for r in radii)
    print(f"  radius in range [0,{RADIUS_MAX}]: max={max_r} -> "
          f"{'OK' if rad_ok else 'FAIL'}")
    ok = ok and rad_ok

    # 3. strength within 5 bits, pol/flag are single bits.
    fields_ok = all(0 <= u[3] <= 31 and u[2] in (0, 1) and u[4] in (0, 1)
                    for u in unpacked)
    print(f"  strength/pol/flag well-formed: {'OK' if fields_ok else 'FAIL'}")
    ok = ok and fields_ok

    # 4. A circular sweep must visit MULTIPLE octants (the whole point: motion all
    #    the way around the centre lights up every wedge). Require >= 6 of 8.
    visited = set(octs)
    sweep_ok = len(visited) >= 6
    print(f"  circular sweep visited {len(visited)}/8 octants "
          f"({sorted(OCTANT_NAMES[o] for o in visited)}) -> "
          f"{'OK' if sweep_ok else 'FAIL'}")
    ok = ok and sweep_ok

    # 5. The app actually fires real pings (flag=1) for a real orbiting object.
    peaks = sum(1 for u in unpacked if u[4] == 1)
    fired = peaks > 0
    print(f"  real-peak pings (flag=1): {peaks} -> {'OK' if fired else 'FAIL'}")
    ok = ok and fired

    print()
    print("VALIDATION:", "PASS -- octant/radius in range, fields well-formed, a "
          "circular sweep visits multiple octants, and the oracle fires"
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
    return x, y, pol


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: octant/radius range + circular "
                         "sweep visits multiple octants (prints numbers)")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the sonar PNG here")
    ap.add_argument("--ring-life", type=int, default=18,
                    help="frames a ring lives before fading out")
    ap.add_argument("--ring-speed", type=float, default=3.0,
                    help="px a ring expands per frame")
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
        words = python_sonar_words(x, y, pol)
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_sonar(words, save=args.save, headless=args.headless,
                 ring_life=args.ring_life, ring_speed=args.ring_speed)


if __name__ == "__main__":
    main()
