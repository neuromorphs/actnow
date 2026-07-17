#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_gravity/main.c
("The Gravity Notary" -- a free-fall certificator that measures the
discrete 2nd difference of a projectile arc's vertical centroid and notarises
which planet the arc was recorded on).

Algorithm (bit-faithful mirror of firmware):
  1. CENTROID: step-toward-median tracker on y; cy +-= 1 per event.
  2. SAMPLE: capture cy every SAMPLE_INTERVAL events -> y[] arc buffer.
  3. D2: for k>=2: d2 = y[k] - 2*y[k-1] + y[k-2]  (shift for 2*).
  4. MEDIAN: insertion-sort d2_buf[0..ARC_D2-1], pick element ARC_D2>>1.
  5. PLANET LUT: |median_d2| -> Moon/Mars/Earth/Jupiter.
  6. FRAUD: count |d2 - median| > FRAUD_TOL; if > FRAUD_THRESH -> fraud=1.
  7. NOISE GUARD: drift guard (cy range < DRIFT_MIN -> valid=0).

Output word layout (bits[31:14]=0):
  bits[ 2: 0] = seq     (3-bit arc counter, wraps mod 8)
  bits[ 3: 3] = valid   (1 if arc was confident)
  bits[ 4: 4] = fraud   (1 if arc is non-ballistic)
  bits[ 6: 5] = planet  (0=Moon, 1=Mars, 2=Earth, 3=Jupiter)
  bits[13: 7] = g_est   (7-bit signed median D2, 2's complement)

------------------------------------------------------------------------------
Usage:
  dvs_gravity_view.py --validate                  # synthetic self-test
  dvs_gravity_view.py --from-actsim results.mem   # render real chip status words
  dvs_gravity_view.py events.csv                  # render host-computed from CSV
  dvs_gravity_view.py ... --headless --save gravity.png
"""
import argparse
import sys
import numpy as np

# --- must match software/dvs_gravity/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
Y_SHIFT = 17

SAMPLE_INTERVAL = 64
ARC_LEN = 6
ARC_D2 = ARC_LEN - 2          # 4
DRIFT_MIN = 3
FRAUD_TOL = 2
FRAUD_THRESH = 1

# Planet LUT thresholds (|median_d2| in pixels/step^2).
# Calibrated for SAMPLE_INTERVAL=64, SciDVS 126x112 sensor.
MOON_MAX    = 1    # |D2| in [0,1]  -> Moon
MARS_MAX    = 3    # |D2| in [2,3]  -> Mars
EARTH_MAX   = 5    # |D2| in [4,5]  -> Earth
JUPITER_MIN = 6    # |D2| >= 6      -> Jupiter

SEQ_MASK  = 0x7
GEST_MASK = 0x7F

PLANET_NAMES = ["Moon", "Mars", "Earth", "Jupiter"]
PLANET_G     = [1.6, 3.7, 9.8, 24.8]   # m/s^2, for display only


# ---------------------------------------------------------------------------
# Bit-faithful mirror of firmware logic
# ---------------------------------------------------------------------------

def isort_median(arr):
    """Insertion sort arr (list of ints), return element at index ARC_D2>>1.

    Matches firmware's isort_median: sort ascending, pick s[ARC_D2>>1].
    ARC_D2=4, so index=2 (upper-middle of the 4-element even array).
    """
    s = list(arr)
    for i in range(1, len(s)):
        key = s[i]
        j = i
        while j > 0 and s[j - 1] > key:
            s[j] = s[j - 1]
            j -= 1
        s[j] = key
    return s[ARC_D2 >> 1]


def python_gravity_words(x, y, pol):
    """Bit-faithful port of software/dvs_gravity/main.c's ISR.

    x, y, pol are per-event arrays (pol unused, x unused -- algorithm uses y).
    Processes only complete batches (n - n%BATCH events).

    State cold-start:
      cy = SY>>1 = 56; cy_min = cy_max = cy; ev_in_interval = 0;
      arc_slot = d2_slot = 0; arc_y = [0]*ARC_LEN; d2_buf = [0]*ARC_D2;
      lat_planet=0; lat_fraud=0; lat_valid=0; lat_g_est=0; seq=0.

    Per event (in batches of BATCH):
      - step-toward-median centroid
      - update cy_min, cy_max
      - ev_in_interval += 1; if >= SAMPLE_INTERVAL: sample cy, compute D2
      - if arc full: median, planet, fraud, latch, reset arc

    Emit one word per batch from latched values.

    Returns (words, arcs) where arcs is a list of
    (planet, fraud, valid, g_est, seq) tuples appended at every arc latch.
    """
    cy = SY >> 1                  # = 56
    cy_min = cy
    cy_max = cy
    ev_in_interval = 0
    arc_slot = 0
    d2_slot = 0
    arc_y = [0] * ARC_LEN
    d2_buf = [0] * ARC_D2
    lat_planet = 0
    lat_fraud = 0
    lat_valid = 0
    lat_g_est = 0
    seq = 0
    words = []
    arcs = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Process BATCH events
        for i in range(b, b + BATCH):
            y_ev = int(y[i]) & 0x7F

            # Step-toward-median centroid tracker
            if y_ev > cy:
                if cy < SY - 1:
                    cy += 1
            elif y_ev < cy:
                if cy > 0:
                    cy -= 1

            # Drift bookkeeping
            if cy < cy_min:
                cy_min = cy
            if cy > cy_max:
                cy_max = cy

            # Advance event-in-interval counter
            ev_in_interval += 1
            if ev_in_interval >= SAMPLE_INTERVAL:
                ev_in_interval = 0

                # Capture sample
                if arc_slot < ARC_LEN:
                    arc_y[arc_slot] = cy
                    arc_slot += 1

                    # Compute D2 once we have >= 3 samples
                    if arc_slot >= 3 and d2_slot < ARC_D2:
                        k = arc_slot - 1
                        d2 = arc_y[k] - (arc_y[k - 1] << 1) + arc_y[k - 2]
                        d2_buf[d2_slot] = d2
                        d2_slot += 1

                    # Full arc
                    if arc_slot >= ARC_LEN:
                        # Drift guard
                        drift = cy_max - cy_min
                        valid = 1 if drift >= DRIFT_MIN else 0

                        # Median D2
                        med = isort_median(d2_buf[:d2_slot])

                        # Absolute value (no multiply)
                        abs_med = abs(med)

                        # Planet LUT
                        if abs_med <= MOON_MAX:
                            planet = 0
                        elif abs_med <= MARS_MAX:
                            planet = 1
                        elif abs_med <= EARTH_MAX:
                            planet = 2
                        else:
                            planet = 3

                        # Fraud detection
                        fraud_count = sum(1 for d in d2_buf[:d2_slot]
                                          if abs(d - med) > FRAUD_TOL)
                        fraud = 1 if fraud_count > FRAUD_THRESH else 0

                        if fraud:
                            valid = 0

                        # Clamp g_est to 7-bit signed [-64, 63]
                        g_est = med
                        if g_est > 63:
                            g_est = 63
                        if g_est < -64:
                            g_est = -64

                        # Latch
                        lat_planet = planet
                        lat_fraud = fraud
                        lat_valid = valid
                        lat_g_est = g_est

                        # Advance seq BEFORE emit
                        seq = (seq + 1) & SEQ_MASK

                        arcs.append((planet, fraud, valid, g_est, seq))

                        # Reset arc
                        arc_slot = 0
                        d2_slot = 0
                        ev_in_interval = 0
                        cy_min = cy
                        cy_max = cy

        # Emit one word per batch
        g_est_bits = lat_g_est & GEST_MASK
        word = (g_est_bits  <<  7) \
             | (lat_planet  <<  5) \
             | (lat_fraud   <<  4) \
             | (lat_valid   <<  3) \
             |  seq
        words.append(word)

    return words, arcs


def unpack_status(word):
    """Unpack one gravity status word.

    bits[2:0]=seq, bits[3]=valid, bits[4]=fraud, bits[6:5]=planet,
    bits[13:7]=g_est (7-bit signed), bits[31:14]=0.
    Returns (seq, valid, fraud, planet, g_est).
    """
    seq     =  word        & 0x7
    valid   = (word >>  3) & 0x1
    fraud   = (word >>  4) & 0x1
    planet  = (word >>  5) & 0x3
    g_raw   = (word >>  7) & GEST_MASK
    # Sign-extend 7-bit field
    g_est   = g_raw if g_raw < 64 else g_raw - 128
    return seq, valid, fraud, planet, g_est


# ---------------------------------------------------------------------------
# Synthetic stream builders for --validate
# ---------------------------------------------------------------------------

def build_freefall_stream(d2_val, n_arcs, y0=10, b=0):
    """Build a synthetic free-fall stream with integer D2 = d2_val px/step^2.

    Each arc has ARC_LEN=6 sample points k=0..5, producing ARC_D2=4 D2 values.
    Sample point k is driven by exactly SAMPLE_INTERVAL events all at y_tgt[k].

    The trajectory uses the discrete parabola y[k] = y0 + b*k + c*k^2 where
    c = d2_val // 2 (so D2 = 2c = d2_val).  For even d2_val this is exact.

    The centroid tracker converges to y_tgt[k] within a single interval
    provided |y_tgt[k] - y_tgt[k-1]| <= SAMPLE_INTERVAL (guaranteed by design)
    and the trajectory stays within [0, SY-1] (enforced by caller's parameters).

    Constraints (caller must satisfy, validated here with assertions):
      - All y_tgt[k] must be in [0, SY-1].
      - |y_tgt[k+1] - y_tgt[k]| <= SAMPLE_INTERVAL for all k in [0, ARC_LEN-2].
      - The centroid-to-y0 gap at cold start: |SY//2 - y0| <= SAMPLE_INTERVAL.

    Returns (x, y, pol) integer numpy arrays.
    """
    import math
    # D2 = 2c in integer arithmetic.  We use d2_val directly as 2c.
    # y[k] = y0 + b*k + (d2_val//2)*k^2  for even d2_val.
    # For odd d2_val, we use half-integer c -- not supported here (always even).
    c_num = d2_val       # numerator; y[k] = y0 + b*k + c_num*k*k/2
    c_den = 2

    def ytgt(k):
        # Integer-exact: y[k] = y0 + b*k + d2_val*k*(k)/2
        # Use integer arithmetic to avoid float rounding.
        # d2_val * k^2 must be even for integer result; for even d2_val always true.
        return y0 + b * k + (d2_val * k * k) // 2

    # Verify trajectory fits
    y_tgts = [ytgt(k) for k in range(ARC_LEN)]
    for k, yt in enumerate(y_tgts):
        assert 0 <= yt < SY, f"y_tgt[{k}]={yt} out of range [0,{SY-1}]"
    for k in range(1, ARC_LEN):
        delta = abs(y_tgts[k] - y_tgts[k-1])
        assert delta <= SAMPLE_INTERVAL, \
            f"|y_tgt[{k}]-y_tgt[{k-1}]|={delta} > SAMPLE_INTERVAL={SAMPLE_INTERVAL}"

    xs, ys, pols = [], [], []
    for _ in range(n_arcs):
        for k in range(ARC_LEN):
            yt = y_tgts[k]
            for j in range(SAMPLE_INTERVAL):
                xs.append(63)
                ys.append(yt)
                pols.append(j % 2)
    return (np.array(xs, dtype=np.int64),
            np.array(ys, dtype=np.int64),
            np.array(pols, dtype=np.int64))


def build_const_velocity_stream(n_arcs):
    """Build a synthetic non-ballistic stream that triggers fraud=1.

    Uses a trajectory with alternating large D2 fluctuations: the centroid
    follows a zig-zag pattern rather than a parabola, producing D2 values
    that deviate far from the median.  With ARC_D2=4 and FRAUD_THRESH=1,
    any arc where more than 1 of the 4 D2 values deviates from the median
    by more than FRAUD_TOL=2 is flagged as fraud.

    Strategy: send events that push cy to alternating positions, creating
    D2 values like [+8, -8, +8, -8], which have median ~0 but all deviate
    by 8 >> FRAUD_TOL=2, so fraud_count=4 > FRAUD_THRESH=1 -> fraud=1.
    """
    import math
    # Zig-zag y targets: e.g. [20, 50, 20, 50, 20, 50]
    # D2 at k=2: y[2] - 2*y[1] + y[0] = 20 - 100 + 20 = -60
    # D2 at k=3: y[3] - 2*y[2] + y[1] = 50 - 40 + 50 = 60
    # All D2 values are +-60; median is in between, deviations huge -> fraud=1.
    y_zigzag = [20, 50, 20, 50, 20, 50]  # ARC_LEN=6
    xs, ys, pols = [], [], []
    for _ in range(n_arcs):
        for k in range(ARC_LEN):
            yt = y_zigzag[k]
            for j in range(SAMPLE_INTERVAL):
                xs.append(63)
                ys.append(yt)
                pols.append(j % 2)
    return (np.array(xs, dtype=np.int64),
            np.array(ys, dtype=np.int64),
            np.array(pols, dtype=np.int64))


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def render_gravity(words, arcs, save=None, headless=False):
    """Compose one figure: arc centroid history + certificate panel."""
    if not words:
        print("no words to render")
        return

    # Find last valid verdict
    last_seq, last_valid, last_fraud, last_planet, last_gest = unpack_status(words[-1])
    for word in reversed(words):
        s, v, f, p, g = unpack_status(word)
        if v:
            last_seq, last_valid, last_fraud, last_planet, last_gest = s, v, f, p, g
            break

    # Collect per-arc history (one point per seq change)
    history_seq = []
    history_planet = []
    history_fraud = []
    history_valid = []
    history_gest = []
    prev_seq = None
    for word in words:
        s, v, f, p, g = unpack_status(word)
        if s != prev_seq:
            history_seq.append(s)
            history_planet.append(p)
            history_fraud.append(f)
            history_valid.append(v)
            history_gest.append(g)
            prev_seq = s

    BG      = "#080c12"
    TEXT    = "#d8e4f0"
    GOLD    = "#f0c040"
    STEEL   = "#6a8099"
    GREEN   = "#44cc88"
    RED     = "#cc4444"
    MOON_C  = "#b0b8c8"
    MARS_C  = "#c85030"
    EARTH_C = "#4488cc"
    JUP_C   = "#d08830"
    DIM     = "#333d48"

    PLANET_COLORS = [MOON_C, MARS_C, EARTH_C, JUP_C]

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last: planet={PLANET_NAMES[last_planet]} g_est={last_gest} "
              f"valid={last_valid} fraud={last_fraud}")
        return

    fig = plt.figure(figsize=(11, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.7], wspace=0.38,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_cert  = fig.add_subplot(gs[0])

    # Right column: g_est history top, planet history bottom
    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_gest   = fig.add_subplot(gs_right[0])
    ax_planet = fig.add_subplot(gs_right[1])

    for ax in (ax_cert, ax_gest, ax_planet):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#223344")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left: notary certificate ---
    ax_cert.set_xlim(-1.5, 1.5)
    ax_cert.set_ylim(-2.2, 1.8)
    ax_cert.set_aspect("equal")
    ax_cert.set_xticks([])
    ax_cert.set_yticks([])
    ax_cert.set_title("notary certificate", color=TEXT, fontsize=9, pad=4)

    # Certificate border
    import matplotlib.patches as mpatches
    border = mpatches.FancyBboxPatch((-1.35, -2.05), 2.7, 3.7,
                                     boxstyle="round,pad=0.1",
                                     edgecolor=GOLD, facecolor=DIM,
                                     linewidth=1.8, zorder=1)
    ax_cert.add_patch(border)

    # Planet seal
    planet_color = PLANET_COLORS[last_planet]
    seal = mpatches.Circle((0, 0.7), 0.6, color=planet_color, zorder=2)
    ax_cert.add_patch(seal)
    seal_ring = mpatches.Circle((0, 0.7), 0.6, fill=False,
                                edgecolor=GOLD, linewidth=2.0, zorder=3)
    ax_cert.add_patch(seal_ring)
    ax_cert.text(0, 0.7, PLANET_NAMES[last_planet][0],
                 ha="center", va="center", color=TEXT,
                 fontsize=16, fontweight="bold", zorder=4)

    # Planet name
    ax_cert.text(0, -0.15, PLANET_NAMES[last_planet],
                 ha="center", va="center", color=TEXT,
                 fontsize=13, fontweight="bold")

    # g estimate
    g_sign = "+" if last_gest >= 0 else ""
    ax_cert.text(0, -0.58,
                 f"gₑₛₜ = {g_sign}{last_gest} px/step²",
                 ha="center", va="center", color=GOLD, fontsize=9)
    ax_cert.text(0, -0.88,
                 f"(ref g ≈ {PLANET_G[last_planet]:.1f} m/s²)",
                 ha="center", va="center", color=STEEL, fontsize=8)

    # Valid / fraud status
    status_col = GREEN if last_valid else RED
    status_txt = "CERTIFIED" if last_valid else ("FRAUD" if last_fraud else "INSUFFICIENT DATA")
    ax_cert.text(0, -1.25, status_txt,
                 ha="center", va="center", color=status_col,
                 fontsize=10, fontweight="bold")

    ax_cert.text(0, -1.65,
                 f"arc seq={last_seq}  valid={last_valid}  fraud={last_fraud}",
                 ha="center", va="center", color=STEEL, fontsize=7)

    # Stamp
    stamp_col = GREEN if last_valid else RED
    stamp = mpatches.Circle((0.9, -1.75), 0.28, fill=False,
                             edgecolor=stamp_col, linewidth=1.5, zorder=3)
    ax_cert.add_patch(stamp)
    ax_cert.text(0.9, -1.75, "✓" if last_valid else "✗",
                 ha="center", va="center", color=stamp_col,
                 fontsize=12, fontweight="bold")

    # --- Right top: g_est history ---
    if history_gest:
        xi = list(range(len(history_gest)))
        colors = [PLANET_COLORS[p] for p in history_planet]
        ax_gest.scatter(xi, history_gest, c=colors, s=18, zorder=3)
        ax_gest.step(xi, history_gest, where="post", color=STEEL, linewidth=0.7, alpha=0.5)
        # Draw planet threshold lines
        ax_gest.axhline(MOON_MAX,    color=MOON_C,  linewidth=0.7, linestyle="--", alpha=0.6,
                        label=f"Moon |D2|≤{MOON_MAX}")
        ax_gest.axhline(MARS_MAX,    color=MARS_C,  linewidth=0.7, linestyle="--", alpha=0.6,
                        label=f"Mars |D2|≤{MARS_MAX}")
        ax_gest.axhline(EARTH_MAX,   color=EARTH_C, linewidth=0.7, linestyle="--", alpha=0.6,
                        label=f"Earth |D2|≤{EARTH_MAX}")
        ax_gest.axhline(-MOON_MAX,   color=MOON_C,  linewidth=0.4, linestyle=":", alpha=0.4)
        ax_gest.axhline(-MARS_MAX,   color=MARS_C,  linewidth=0.4, linestyle=":", alpha=0.4)
        ax_gest.axhline(-EARTH_MAX,  color=EARTH_C, linewidth=0.4, linestyle=":", alpha=0.4)
        ax_gest.legend(fontsize=6, framealpha=0.2, labelcolor=TEXT,
                       facecolor=BG, edgecolor="#223344")
    ax_gest.set_xlabel("arc index", color=TEXT, fontsize=8)
    ax_gest.set_ylabel("g_est (median D2, px/step²)", color=TEXT, fontsize=8)
    ax_gest.set_title("per-arc g_est", color=TEXT, fontsize=9, pad=4)

    # --- Right bottom: planet history ---
    if history_planet:
        xi = list(range(len(history_planet)))
        colors_p = [PLANET_COLORS[p] for p in history_planet]
        ax_planet.scatter(xi, history_planet, c=colors_p, s=18, zorder=3)
        ax_planet.step(xi, history_planet, where="post", color=STEEL, linewidth=0.7, alpha=0.5)
        # Fraud markers
        for i, f in enumerate(history_fraud):
            if f:
                ax_planet.axvline(i, color=RED, linewidth=0.6, alpha=0.5)
    ax_planet.set_yticks([0, 1, 2, 3])
    ax_planet.set_yticklabels(PLANET_NAMES, color=TEXT, fontsize=7)
    ax_planet.set_xlabel("arc index", color=TEXT, fontsize=8)
    ax_planet.set_title("per-arc planet verdict", color=TEXT, fontsize=9, pad=4)

    fig.suptitle('"The Gravity Notary"', color=TEXT, fontsize=12,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks against expected values.

    All expected values are hand-derived from the discrete-parabola model and
    the step-toward-median tracker dynamics.  If the mirror disagrees the
    mirror is wrong -- never adjust the expectations.

    (a) EARTH FREE-FALL: D2=4 px/step^2, clean parabola y0=10, b=0.
        ARC_LEN=6 sample pts: y=[10,12,18,28,42,60].
        D2 values (4 of them): all 4. Median=4 -> planet=2 (Earth), fraud=0, valid=1.
    (b) MARS FREE-FALL: D2=2 px/step^2, clean parabola y0=10, b=0.
        ARC_LEN=6 sample pts: y=[10,11,14,19,26,35].
        D2 values: all 2. Median=2 -> planet=1 (Mars), fraud=0, valid=1.
    (c) NON-BALLISTIC (zig-zag y=[20,50,20,50,20,50]) -> fraud=1.
        D2 at k=2: 20-100+50=-30; k=3: 50-40+50=60; k=4: 20-100+50=-30; k=5: 50-40+50=60.
        D2 values: [-30,60,-30,60]; median = isort([-30,-30,60,60])[2] = 60.
        Deviations: |-30-60|=90>2, |60-60|=0, |-30-60|=90>2, |60-60|=0.
        fraud_count=2 > FRAUD_THRESH=1 -> fraud=1.
    (d) Well-formedness over all words from (a)-(c).
    (e) Planet LUT boundary checks (direct, all 8 bucket boundaries).
    (f) isort_median correctness (1000 random arrays).
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) EARTH FREE-FALL (D2=4 -> planet=2)
    # y[k] = 10 + 2*k^2.  k=0..5: [10,12,18,28,42,60].
    # D2: 18-24+10=4; 28-36+12=4; 42-56+18=4; 60-84+28=4. All 4. Median=4.
    # Drift: cy_max=60, cy_min=10, drift=50 >= DRIFT_MIN=3 -> valid=1.
    # Fraud: deviations all 0 -> fraud=0.
    # Expected per-arc: (planet=2, fraud=0, valid=1, g_est=4).
    # ------------------------------------------------------------------
    x_a, y_a, pol_a = build_freefall_stream(d2_val=4, n_arcs=4, y0=10, b=0)
    words_a, arcs_a = python_gravity_words(x_a, y_a, pol_a)

    earth_arcs_a = [(p, f, v, g) for (p, f, v, g, s) in arcs_a
                    if p == 2 and f == 0 and v == 1 and g == 4]
    a_ok = len(earth_arcs_a) >= 1
    print(f"  (a) EARTH FREE-FALL (D2=4): {len(arcs_a)} arcs, "
          f"{len(earth_arcs_a)} with planet=Earth fraud=0 valid=1 g_est=4 -> "
          f"{'OK' if a_ok else 'FAIL'}")
    if arcs_a:
        print(f"      first arc: planet={PLANET_NAMES[arcs_a[0][0]]} g_est={arcs_a[0][3]} "
              f"fraud={arcs_a[0][1]} valid={arcs_a[0][2]}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) MARS FREE-FALL (D2=2 -> planet=1)
    # y[k] = 10 + k^2.  k=0..5: [10,11,14,19,26,35].
    # D2: 14-22+10=2; 19-28+11=2; 26-38+14=2; 35-52+19=2. All 2. Median=2.
    # Drift: cy_max=35, cy_min=10, drift=25 >= DRIFT_MIN=3 -> valid=1.
    # Fraud: deviations all 0 -> fraud=0.
    # Expected per-arc: (planet=1, fraud=0, valid=1, g_est=2).
    # ------------------------------------------------------------------
    x_b, y_b, pol_b = build_freefall_stream(d2_val=2, n_arcs=4, y0=10, b=0)
    words_b, arcs_b = python_gravity_words(x_b, y_b, pol_b)

    mars_arcs_b = [(p, f, v, g) for (p, f, v, g, s) in arcs_b
                   if p == 1 and f == 0 and v == 1 and g == 2]
    b_ok = len(mars_arcs_b) >= 1
    print(f"  (b) MARS FREE-FALL (D2=2): {len(arcs_b)} arcs, "
          f"{len(mars_arcs_b)} with planet=Mars fraud=0 valid=1 g_est=2 -> "
          f"{'OK' if b_ok else 'FAIL'}")
    if arcs_b:
        print(f"      first arc: planet={PLANET_NAMES[arcs_b[0][0]]} g_est={arcs_b[0][3]} "
              f"fraud={arcs_b[0][1]} valid={arcs_b[0][2]}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) NON-BALLISTIC ZIG-ZAG -> fraud=1
    # y_zigzag=[20,50,20,50,20,50].
    # Once centroid converges (2nd+ arcs): D2=[-30,60,-30,60].
    # Sorted: [-30,-30,60,60]; median=sorted[2]=60.
    # Deviations: 90, 0, 90, 0 -> count=2 > FRAUD_THRESH=1 -> fraud=1.
    # At least the 2nd arc (after centroid has converged) must have fraud=1.
    # ------------------------------------------------------------------
    x_c, y_c, pol_c = build_const_velocity_stream(n_arcs=4)
    words_c, arcs_c = python_gravity_words(x_c, y_c, pol_c)

    fraud_arcs_c = [(p, f, v, g) for (p, f, v, g, s) in arcs_c if f == 1]
    c_ok = len(fraud_arcs_c) >= 1
    print(f"  (c) NON-BALLISTIC ZIG-ZAG: {len(arcs_c)} arcs, "
          f"{len(fraud_arcs_c)} with fraud=1 -> "
          f"{'OK' if c_ok else 'FAIL'}")
    if arcs_c:
        print(f"      arcs: " +
              "; ".join(f"planet={PLANET_NAMES[p]} g={g} f={f} v={v}"
                        for p, f, v, g, s in arcs_c))
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) WELL-FORMEDNESS over all words from (a)-(c)
    # seq in [0,7], valid in {0,1}, fraud in {0,1}, planet in [0,3],
    # g_est in [-64,63], bits[31:14] == 0.
    # ------------------------------------------------------------------
    all_words_d = words_a + words_b + words_c
    bad_d = []
    for word in all_words_d:
        s, v, f, p, g = unpack_status(word)
        if not (0 <= s <= 7):
            bad_d.append(f"seq={s}")
        if v not in (0, 1):
            bad_d.append(f"valid={v}")
        if f not in (0, 1):
            bad_d.append(f"fraud={f}")
        if not (0 <= p <= 3):
            bad_d.append(f"planet={p}")
        if not (-64 <= g <= 63):
            bad_d.append(f"g_est={g}")
        if (word >> 14) != 0:
            bad_d.append(f"upper bits set in 0x{word:08x}")
        if bad_d:
            break
    d_ok = len(bad_d) == 0
    print(f"  (d) WELL-FORMEDNESS: {len(all_words_d)} total words; "
          f"seq<=7, valid/fraud in {{0,1}}, planet<=3, g_est in [-64,63], "
          f"bits[31:14]=0 -> "
          f"{'OK' if d_ok else 'FAIL: ' + '; '.join(bad_d[:5])}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) PLANET LUT BOUNDARY CHECKS
    # Direct test of planet assignment for each boundary value.
    # LUT: |D2| in [0,1]=Moon, [2,3]=Mars, [4,5]=Earth, >=6=Jupiter.
    # ------------------------------------------------------------------
    def check_planet(d2_val, expected_planet):
        d2_list = [d2_val] * ARC_D2
        med = isort_median(d2_list)
        abs_med = abs(med)
        if abs_med <= MOON_MAX:
            p = 0
        elif abs_med <= MARS_MAX:
            p = 1
        elif abs_med <= EARTH_MAX:
            p = 2
        else:
            p = 3
        return p == expected_planet, p

    cases_e = [
        (0,  0, "D2=0  -> Moon"),
        (1,  0, "D2=1  -> Moon"),
        (-1, 0, "D2=-1 -> Moon (abs)"),
        (2,  1, "D2=2  -> Mars"),
        (3,  1, "D2=3  -> Mars"),
        (-2, 1, "D2=-2 -> Mars (abs)"),
        (4,  2, "D2=4  -> Earth"),
        (5,  2, "D2=5  -> Earth"),
        (-5, 2, "D2=-5 -> Earth (abs)"),
        (6,  3, "D2=6  -> Jupiter"),
        (30, 3, "D2=30 -> Jupiter"),
    ]
    e_ok = True
    e_fails = []
    for d2_val, exp_p, desc in cases_e:
        passed, got_p = check_planet(d2_val, exp_p)
        if not passed:
            e_ok = False
            e_fails.append(f"{desc}: got {PLANET_NAMES[got_p]}")
    print(f"  (e) PLANET LUT BOUNDARIES ({len(cases_e)} cases) -> "
          f"{'OK' if e_ok else 'FAIL: ' + '; '.join(e_fails)}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) ISORT_MEDIAN CORRECTNESS
    # Compare against Python's sorted()[ARC_D2>>1] for 1000 random arrays.
    # ------------------------------------------------------------------
    import random
    rng = random.Random(99)
    f_ok = True
    f_fails = []
    for trial in range(1000):
        arr = [rng.randint(-30, 30) for _ in range(ARC_D2)]
        got  = isort_median(arr)
        want = sorted(arr)[ARC_D2 >> 1]
        if got != want:
            f_ok = False
            f_fails.append(f"trial {trial}: got={got} want={want}")
            if len(f_fails) >= 3:
                break
    print(f"  (f) ISORT_MEDIAN (1000 random ARC_D2={ARC_D2} arrays) -> "
          f"{'OK' if f_ok else 'FAIL: ' + '; '.join(f_fails)}")
    ok = ok and f_ok

    print()
    print("VALIDATION:", "PASS -- earth/mars free-fall certified; fraud zig-zag detected; "
          "fields well-formed; planet LUT boundaries exact; median sort verified"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def load_csv(path, ts_col="le"):
    """Load event CSV with columns x, y, pol (and optional ts column).

    The gravity algorithm is event-ORDER driven (no timestamps used), so ts
    is loaded but not passed to python_gravity_words.  Included for
    compatibility with the --ts-col CLI argument.
    """
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x   = np.array([int(row[idx["x"]])   for row in rows], dtype=np.int64)
    y   = np.array([int(row[idx["y"]])   for row in rows], dtype=np.int64)
    pol = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int64)
    return x, y, pol


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: free-fall earth/mars, fraud, well-formedness, "
                         "planet LUT boundaries, median sort")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV timestamp column (unused by gravity algorithm, kept for CLI compat)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the gravity PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            raw_words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(raw_words)} real chip status words from {args.from_actsim}")
        words = raw_words
        arcs = []
        for word in words:
            s, v, f, p, g = unpack_status(word)
            if v:
                arcs.append((p, f, v, g, s))
    elif args.csv:
        x, y, pol = load_csv(args.csv, args.ts_col)
        print(f"loaded {len(x)} events from {args.csv}; computing gravity words in Python "
              f"(bit-faithful mirror of firmware).")
        words, arcs = python_gravity_words(x, y, pol)
        if words:
            s, v, f, p, g = unpack_status(words[-1])
            print(f"final: planet={PLANET_NAMES[p]} g_est={g} valid={v} fraud={f} "
                  f"seq={s} ({len(words)} words, {len(arcs)} arcs)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_gravity(words, arcs, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
