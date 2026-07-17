#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_actuary/main.c
("The Actuary of Spinning Tops" -- watch a spinning top; as its precession
wobble grows, extrapolate the moment of death and show a countdown in
precession cycles.  Every EXTENT_WINDOW batches the firmware measures the
bounding extent of the activity centroid (Manhattan amplitude A = Δx + Δy),
tracks growth ΔA per window, and extrapolates cycles-to-topple by repeated
subtraction when ΔA > 0 and the centroid oscillation is validated).

python_actuary_words() is a bit-faithful port of the firmware ISR (same
centroid, same leaky-integrator smoothing, same zero-crossing detector,
same extent tracker, same repeated-subtraction countdown loop).

Output word layout (27 bits used):
  bits[ 5: 0] = countdown  (0..63, capped at COUNTDOWN_CAP; 0 = no prediction)
  bits[13: 6] = amplitude  (0..255, current wobble extent, saturated)
  bits[20:14] = period     (0..127, precession period in batches, capped)
  bits[21]    = valid      (1 = growing oscillation detected, countdown meaningful)
  bits[25:22] = seq        (4-bit batch sequence, wraps mod 16)
  bits[31:26] = 0

------------------------------------------------------------------------------
Usage:
  dvs_actuary_view.py --validate                    # synthetic self-test
  dvs_actuary_view.py --from-actsim results.mem     # render real chip status words
  dvs_actuary_view.py events.csv --ts-col le        # render from a CSV
  dvs_actuary_view.py ... --headless --save actuary.png
"""
import argparse
import numpy as np

# --- must match software/dvs_actuary/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
EXTENT_WINDOW  = 32
GROW_MIN       = 2
A_FLOOR        = 4
A_CRIT         = 80
COUNTDOWN_CAP  = 63
SMOOTH_SH      = 3
PERIOD_WINDOW  = 64
CROSS_MIN      = 3
PERIOD_MAX     = 127
SEQ_MASK       = 0xF
AMP_SAT        = 255


def python_actuary_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_actuary/main.c's ISR.

    x, y, ts, pol are per-event arrays (ts/pol decoded per ABI but unused by
    the algorithm).  Processes only complete batches (n - n%BATCH events).

    State cold-start all zeros (mirrors crt0.S .bss zero-fill):
      cx_smooth=0; prev_sign=0; have_prev_sign=0; cross_count=0;
      batch_in_period=0; zc_ts=[0,0]; zc_count=0; have_period=0;
      period_batches=0; batch_in_extent=0; cmin/cmax all 0; extent_init=0;
      amp_prev=0; have_amp_prev=0; grow_streak=0; delta_a=0; seq=0;
      lat_countdown=lat_amplitude=lat_period=lat_valid=0.

    Per batch of BATCH events:
      1. Compute cx = (sum of ex) >> 2;  cy = (sum of ey) >> 2
      2. Update cx_smooth (leaky integrator)
      3. Detect zero-crossing of (cx - cx_smooth)
      4. Update extent min/max
      5. Advance period window; if complete -> check crossing count
      6. Advance extent window; if complete -> compute A, ΔA, countdown
      7. Emit one word from latched values

    Returns (words, latches) where latches is a list of
    (valid, countdown, amplitude, period, delta_a) tuples at every window latch.
    """
    cx_smooth      = 0
    prev_sign      = 0
    have_prev_sign = 0
    cross_count    = 0
    batch_in_period= 0
    zc_ts          = [0, 0]
    zc_count       = 0
    have_period    = 0
    period_batches = 0
    batch_in_extent= 0
    cmin_x = cmax_x = cmin_y = cmax_y = 0
    extent_init    = 0
    amp_prev       = 0
    have_amp_prev  = 0
    grow_streak    = 0
    delta_a        = 0
    seq            = 0
    lat_countdown  = 0
    lat_amplitude  = 0
    lat_period     = 0
    lat_valid      = 0
    words          = []
    latches        = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # 1. Batch centroid
        sx = sum(int(x[b + i]) for i in range(BATCH))
        sy = sum(int(y[b + i]) for i in range(BATCH))
        cx = sx >> 2
        cy = sy >> 2

        # 2. Leaky-integrator smoothing of cx (same shifts as firmware)
        cx_smooth = cx_smooth - (cx_smooth >> SMOOTH_SH) + (cx >> SMOOTH_SH)

        # 3. Zero-crossing: sign of (cx - cx_smooth) changes
        dev = cx - cx_smooth
        sign = 1 if dev < 0 else 0

        if have_prev_sign:
            if sign != prev_sign:
                cross_count += 1
                half_period = (seq - zc_ts[1]) & 0xFFFF  # unsigned wrap
                zc_ts[0] = zc_ts[1]
                zc_ts[1] = seq
                zc_count += 1
                if zc_count >= 2:
                    p = half_period << 1
                    if p > PERIOD_MAX:
                        p = PERIOD_MAX
                    period_batches = p
                    have_period = 1
        else:
            zc_ts[1] = seq
        prev_sign      = sign
        have_prev_sign = 1

        # 4. Update extent window min/max
        if not extent_init:
            cmin_x = cx; cmax_x = cx
            cmin_y = cy; cmax_y = cy
            extent_init = 1
        else:
            if cx < cmin_x: cmin_x = cx
            if cx > cmax_x: cmax_x = cx
            if cy < cmin_y: cmin_y = cy
            if cy > cmax_y: cmax_y = cy

        # 5. Advance period window
        batch_in_period += 1
        if batch_in_period >= PERIOD_WINDOW:
            batch_in_period = 0
            if cross_count < CROSS_MIN:
                have_period  = 0
                grow_streak  = 0
            cross_count = 0

        # 6. Advance extent window
        batch_in_extent += 1
        if batch_in_extent >= EXTENT_WINDOW:
            batch_in_extent = 0
            amp = (cmax_x - cmin_x) + (cmax_y - cmin_y)
            if amp > AMP_SAT:
                amp = AMP_SAT
            # Reset for next window
            extent_init = 0

            # Compute ΔA
            da = 0
            if have_amp_prev:
                if amp > amp_prev:
                    da = amp - amp_prev
                    grow_streak += 1
                else:
                    da = 0
                    grow_streak = 0
            amp_prev      = amp
            have_amp_prev = 1

            # Countdown by repeated subtraction (bit-faithful, NO divide)
            valid     = 0
            countdown = 0
            if (have_period
                    and grow_streak >= GROW_MIN
                    and amp >= A_FLOOR
                    and amp < A_CRIT
                    and da > 0):
                valid = 1
                tmp = amp
                countdown = 0
                while tmp < A_CRIT and countdown < COUNTDOWN_CAP:
                    tmp += da
                    countdown += 1
                if tmp < A_CRIT:
                    countdown = COUNTDOWN_CAP

            delta_a       = da
            lat_amplitude = amp
            lat_period    = period_batches
            lat_valid     = valid
            lat_countdown = countdown
            latches.append((valid, countdown, amp, period_batches, da))

        # Advance sequence counter
        seq = (seq + 1) & SEQ_MASK

        # Emit one word from latched values
        word = (seq           << 22) \
             | (lat_valid     << 21) \
             | (lat_period    << 14) \
             | (lat_amplitude <<  6) \
             |  lat_countdown
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one actuary status word.

    bits[ 5: 0] = countdown  (0..63)
    bits[13: 6] = amplitude  (0..255)
    bits[20:14] = period     (0..127)
    bits[21]    = valid      (0 or 1)
    bits[25:22] = seq        (0..15)
    bits[31:26] = 0
    """
    countdown =  word        & 0x3F
    amplitude = (word >>  6) & 0xFF
    period    = (word >> 14) & 0x7F
    valid     = (word >> 21) & 0x1
    seq       = (word >> 22) & 0xF
    return countdown, amplitude, period, valid, seq


# ---------------------------------------------------------------------------
# Synthetic validation
# ---------------------------------------------------------------------------

def build_centroid_stream(n_batches, cx_fn, cy_fn=None, burst_len=2, intra_dt=2):
    """Build a synthetic event stream whose per-batch centroid follows cx_fn.

    cx_fn(batch_idx) -> float centroid x (0..125).
    cy_fn(batch_idx) -> float centroid y (0..111); defaults to SY//2.
    Each batch of BATCH events is built around the target centroid:
      events at (cx ± spread) so the batch mean = cx.
    Returns int32 numpy arrays (x, y, ts, pol).
    """
    if cy_fn is None:
        cy_fn = lambda _: float(SY // 2)
    xs, ys, tss, pols = [], [], [], []
    for bi in range(n_batches):
        cx_target = float(cx_fn(bi))
        cy_target = float(cy_fn(bi))
        # Spread events around the centroid so mean = target
        # BATCH=4: place two events at floor, two at ceil
        cx_lo = max(0, int(cx_target))
        cx_hi = min(SX - 1, cx_lo + 1)
        cy_lo = max(0, int(cy_target))
        cy_hi = min(SY - 1, cy_lo + 1)
        # 4 events: (lo,lo),(hi,lo),(lo,hi),(hi,hi) -- mean is near target
        for (ex, ey) in [(cx_lo, cy_lo), (cx_hi, cy_lo),
                         (cx_lo, cy_hi), (cx_hi, cy_hi)]:
            xs.append(ex)
            ys.append(ey)
            tss.append(bi * BATCH * intra_dt)
            pols.append(0)
    return (np.array(xs,   dtype=np.int32),
            np.array(ys,   dtype=np.int32),
            np.array(tss,  dtype=np.int32),
            np.array(pols, dtype=np.int32))


def validate():
    """Run lettered validation checks (zero-tolerance exact integer assertions).

    (a) Growing oscillation: centroid-x = centre + A0 + ΔA_per_window × window_idx
        × sin(2π batch / period_batches).  After GROW_MIN windows of growth the
        countdown must match (A_CRIT - A) / ΔA computed by repeated subtraction.
    (b) Constant-amplitude oscillation (ΔA=0): valid never goes 1; no countdown.
    (c) Static scene (centroid fixed at centre): zero-crossings never reach
        CROSS_MIN, no period detected, no countdown.
    (d) Well-formedness over all words from (a)-(c).
    """
    import math
    ok = True

    # -----------------------------------------------------------------------
    # (a) GROWING OSCILLATION -- countdown matches repeated-subtraction formula
    # Build n_batches batches with centroid x oscillating at a known frequency
    # and with amplitude growing linearly by DA_STEP each EXTENT_WINDOW batches.
    # -----------------------------------------------------------------------
    DA_STEP    = 4          # ΔA per extent window (pixels)
    A0_START   = 8          # starting amplitude (pixels)
    PREC_HALF  = 6          # half-period in batches (period=12 batches; >CROSS_MIN crossings)
    # We need at least GROW_MIN+1 extent windows after first amp is established,
    # plus enough period windows to validate crossings.
    # Run for 3×EXTENT_WINDOW + enough to flush period check.
    N_WINDOWS  = GROW_MIN + 4
    N_BATCHES  = N_WINDOWS * EXTENT_WINDOW + PERIOD_WINDOW + 4

    centre_x = float(SX // 2)
    # amplitude at window w (0-indexed after first window):
    #   w=0: A0_START; w=1: A0_START+DA_STEP; ...
    # cx oscillates: cx = centre_x + (A0_START + DA_STEP*w) * sin(2π*batch/PREC_HALF*π)
    # simplified: sign alternates every PREC_HALF batches
    def cx_growing(bi):
        w_idx = bi // EXTENT_WINDOW   # which extent window
        amp_target = A0_START + DA_STEP * w_idx
        # triangle wave with half-period PREC_HALF batches
        phase = bi % (2 * PREC_HALF)
        if phase < PREC_HALF:
            frac = phase / float(PREC_HALF)
        else:
            frac = 1.0 - (phase - PREC_HALF) / float(PREC_HALF)
        offset = amp_target * (2.0 * frac - 1.0)  # range -amp..+amp
        return min(SX - 1, max(0, centre_x + offset))

    x_a, y_a, ts_a, pol_a = build_centroid_stream(N_BATCHES, cx_growing)
    words_a, latches_a = python_actuary_words(x_a, y_a, ts_a, pol_a)

    # Find the first latch where valid=1
    first_valid_latch = None
    for latch in latches_a:
        v, cd, amp, per, da = latch
        if v == 1:
            first_valid_latch = latch
            break

    a_ok = False
    if first_valid_latch is not None:
        v, cd, amp, per, da = first_valid_latch
        # Independently compute expected countdown by the SAME repeated subtraction
        if da > 0 and amp < A_CRIT:
            tmp = amp; expected_cd = 0
            while tmp < A_CRIT and expected_cd < COUNTDOWN_CAP:
                tmp += da; expected_cd += 1
            if tmp < A_CRIT:
                expected_cd = COUNTDOWN_CAP
            a_ok = (cd == expected_cd)
            print(f"  (a) GROWING OSCILLATION: first valid latch cd={cd} "
                  f"expected={expected_cd} amp={amp} da={da} per={per} -> "
                  f"{'OK' if a_ok else 'FAIL'}")
        else:
            print(f"  (a) GROWING OSCILLATION: da={da} amp={amp} invalid for test -> FAIL")
    else:
        print(f"  (a) GROWING OSCILLATION: no valid latch found -> FAIL")
        print(f"      latches={latches_a[:8]}")
    ok = ok and a_ok

    # -----------------------------------------------------------------------
    # (b) CONSTANT AMPLITUDE -- valid must never go 1
    # Oscillating at fixed amplitude A0_START (da=0 after first grow window).
    # -----------------------------------------------------------------------
    def cx_constant(bi):
        phase = bi % (2 * PREC_HALF)
        if phase < PREC_HALF:
            frac = phase / float(PREC_HALF)
        else:
            frac = 1.0 - (phase - PREC_HALF) / float(PREC_HALF)
        offset = A0_START * (2.0 * frac - 1.0)
        return min(SX - 1, max(0, centre_x + offset))

    x_b, y_b, ts_b, pol_b = build_centroid_stream(N_BATCHES, cx_constant)
    words_b, latches_b = python_actuary_words(x_b, y_b, ts_b, pol_b)

    any_valid_b = any(latch[0] == 1 for latch in latches_b)
    b_ok = not any_valid_b
    print(f"  (b) CONSTANT AMPLITUDE: any valid=1 latch={any_valid_b} (want False) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # -----------------------------------------------------------------------
    # (c) STATIC SCENE -- centroid fixed at centre; no crossings -> no period
    # -----------------------------------------------------------------------
    def cx_static(bi):
        return float(centre_x)

    x_c, y_c, ts_c, pol_c = build_centroid_stream(N_BATCHES, cx_static)
    words_c, latches_c = python_actuary_words(x_c, y_c, ts_c, pol_c)

    any_valid_c = any(latch[0] == 1 for latch in latches_c)
    c_ok = not any_valid_c
    print(f"  (c) STATIC SCENE: any valid=1 latch={any_valid_c} (want False) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # -----------------------------------------------------------------------
    # (d) WELL-FORMEDNESS over all words from (a)-(c)
    # countdown<=63, amplitude<=255, period<=127, valid<=1, seq<=15,
    # (word>>26)==0, word < 2**32.
    # -----------------------------------------------------------------------
    all_words_d = words_a + words_b + words_c
    bad_d = []
    for word in all_words_d:
        cd_, amp_, per_, v_, seq_ = unpack_status(word)
        if cd_ > COUNTDOWN_CAP:
            bad_d.append(f"countdown={cd_}")
        if amp_ > AMP_SAT:
            bad_d.append(f"amplitude={amp_}")
        if per_ > PERIOD_MAX:
            bad_d.append(f"period={per_}")
        if v_ > 1:
            bad_d.append(f"valid={v_}")
        if seq_ > 15:
            bad_d.append(f"seq={seq_}")
        if (word >> 26) != 0:
            bad_d.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_d.append(f"word>=2^32: 0x{word:08x}")
        if bad_d:
            break
    d_ok = len(bad_d) == 0
    print(f"  (d) WELL-FORMEDNESS: {len(all_words_d)} total words; "
          f"all fields in bounds -> "
          f"{'OK' if d_ok else 'FAIL: ' + '; '.join(bad_d[:5])}")
    ok = ok and d_ok

    print()
    print("VALIDATION:", "PASS -- growing oscillation countdown exact; "
          "constant amplitude stays dormant; static scene no prediction; "
          "word fields well-formed"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# Renderer: coroner-report aesthetic on dark backdrop
# ---------------------------------------------------------------------------

def render_actuary(words, save=None, headless=False):
    """Compose one figure: countdown panel + amplitude/period history."""
    if not words:
        print("no words to render")
        return

    cd_last, amp_last, per_last, valid_last, _ = unpack_status(words[-1])

    # Collect one sample per seq change (one per batch)
    history_amp  = []
    history_cd   = []
    history_valid= []
    prev_seq = None
    for word in words:
        cd_, amp_, per_, v_, seq_ = unpack_status(word)
        if seq_ != prev_seq:
            history_amp.append(amp_)
            history_cd.append(cd_ if v_ else None)
            history_valid.append(v_)
            prev_seq = seq_

    BG     = "#0b0d12"
    TEXT   = "#d8d0c0"
    RED    = "#e03030"
    AMBER  = "#e0a030"
    GREEN  = "#40b860"
    STEEL  = "#8090a8"
    DIM    = "#445060"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last valid={valid_last} countdown={cd_last} "
              f"amplitude={amp_last} period={per_last}")
        return

    fig = plt.figure(figsize=(11, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.8], wspace=0.35,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_left  = fig.add_subplot(gs[0])
    ax_right_placeholder = fig.add_subplot(gs[1])

    gs_right = gs[1].subgridspec(2, 1, hspace=0.60)
    ax_amp  = fig.add_subplot(gs_right[0])
    ax_cd   = fig.add_subplot(gs_right[1])
    ax_right_placeholder.remove()

    for ax in (ax_left, ax_amp, ax_cd):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#2a3040")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left panel: countdown clock ---
    ax_left.set_xlim(-1.5, 1.5)
    ax_left.set_ylim(-1.9, 1.5)
    ax_left.set_aspect("equal")
    ax_left.set_xticks([])
    ax_left.set_yticks([])
    ax_left.set_title("topple countdown", color=TEXT, fontsize=9, pad=4)

    if valid_last:
        frac = cd_last / float(COUNTDOWN_CAP)
        lamp_color = RED if frac < 0.3 else (AMBER if frac < 0.6 else GREEN)
        lamp = mpatches.Circle((0, 0.3), 0.9, color=lamp_color, zorder=2)
        ax_left.add_patch(lamp)
        ring = mpatches.Circle((0, 0.3), 0.9, fill=False,
                               edgecolor=TEXT, linewidth=1.5, zorder=3)
        ax_left.add_patch(ring)
        ax_left.text(0, 0.3, str(cd_last),
                     ha="center", va="center", color="white",
                     fontsize=28, fontweight="bold")
        ax_left.text(0, -0.85, "cycles to topple",
                     ha="center", va="center", color=TEXT, fontsize=9)
    else:
        lamp = mpatches.Circle((0, 0.3), 0.9, color=DIM, zorder=2)
        ax_left.add_patch(lamp)
        ring = mpatches.Circle((0, 0.3), 0.9, fill=False,
                               edgecolor=TEXT, linewidth=1.5, zorder=3)
        ax_left.add_patch(ring)
        ax_left.text(0, 0.3, "?",
                     ha="center", va="center", color=STEEL,
                     fontsize=32, fontweight="bold")
        ax_left.text(0, -0.85, "no prediction",
                     ha="center", va="center", color=STEEL, fontsize=9)

    ax_left.text(0, -1.28,
                 f"amp={amp_last}px  period={per_last} batches",
                 ha="center", va="center", color=TEXT, fontsize=8)

    # --- Right top: amplitude history ---
    ax_amp.axhline(A_CRIT, color=RED, linewidth=0.9, linestyle="--", alpha=0.7,
                   label=f"A_CRIT={A_CRIT}")
    ax_amp.axhline(A_FLOOR, color=STEEL, linewidth=0.7, linestyle=":", alpha=0.6,
                   label=f"A_FLOOR={A_FLOOR}")
    if history_amp:
        xs = list(range(len(history_amp)))
        ax_amp.step(xs, history_amp, where="post", color=AMBER, linewidth=1.0)
    ax_amp.set_xlabel("batch index", color=TEXT, fontsize=8)
    ax_amp.set_ylabel("wobble amplitude A (px)", color=TEXT, fontsize=8)
    ax_amp.set_title("precession amplitude", color=TEXT, fontsize=9, pad=4)
    ax_amp.set_ylim(-2, AMP_SAT + 2)
    legend = ax_amp.legend(fontsize=7, framealpha=0.2,
                           labelcolor=TEXT, facecolor=BG, edgecolor="#2a3040")

    # --- Right bottom: countdown history (only when valid) ---
    if any(v is not None for v in history_cd):
        valid_xs  = [i for i, v in enumerate(history_cd) if v is not None]
        valid_cds = [v for v in history_cd if v is not None]
        ax_cd.scatter(valid_xs, valid_cds, color=RED, s=12, zorder=2)
        ax_cd.step([valid_xs[0]] + valid_xs, [valid_cds[0]] + valid_cds,
                   where="post", color=RED, linewidth=0.7, alpha=0.5)
    ax_cd.set_xlabel("batch index", color=TEXT, fontsize=8)
    ax_cd.set_ylabel("cycles to topple", color=TEXT, fontsize=8)
    ax_cd.set_title("coroner's countdown (valid only)", color=TEXT, fontsize=9, pad=4)
    ax_cd.set_ylim(-1, COUNTDOWN_CAP + 2)

    fig.suptitle('"The Actuary of Spinning Tops"', color=TEXT, fontsize=12,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# CSV loader (same pattern as dvs_vital_view.py)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for the timestamp field (default 'le').
    NOTE: recorded captures carry a wrapped coarse counter; --validate builds
    its own synthetic streams and is the deterministic check.
    If ts_col is absent from the header, ts is zeroed.
    """
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x   = np.array([int(row[idx["x"]])   for row in rows], dtype=np.int32)
    y   = np.array([int(row[idx["y"]])   for row in rows], dtype=np.int32)
    pol = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int32)
    if ts_col in idx:
        ts = np.array([int(row[idx[ts_col]]) & 0xFFFF for row in rows], dtype=np.int32)
    else:
        ts = np.zeros(len(x), dtype=np.int32)
    return x, y, ts, pol


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: growing oscillation countdown exact; "
                         "constant amplitude stays dormant; static scene no prediction; "
                         "well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le -- NOTE: le is a "
                         "wrapped coarse counter, not real microseconds; verdicts on "
                         "recorded captures are qualitative; --validate builds its own "
                         "synthetic streams and is the deterministic check)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the actuary PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, ts, pol = load_csv(args.csv, args.ts_col)
        print(f"loaded {len(x)} events from {args.csv}; computing actuary words in Python "
              f"(bit-faithful mirror of firmware).")
        words, _ = python_actuary_words(x, y, ts, pol)
        if words:
            cd_last, amp_last, per_last, valid_last, _ = unpack_status(words[-1])
            print(f"final valid={valid_last} countdown={cd_last} "
                  f"amplitude={amp_last}px period={per_last} batches "
                  f"({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_actuary(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
