#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_widdershins/main.c
("The Widdershins Engine" -- a communal winding-number counter that follows the
scene's activity locus with a median tracker and accumulates signed winding
around frame centre.  Every SAMPLE_BATCHES batches it samples the tracker's
compass octant; circular octant differences accumulate into a signed winding
register `wind` (eighth-turns).  Clockwise-on-screen (deosil, octant index
increasing) ramps wind positive; counter-clockwise (widdershins) ramps it
negative; wind>>3 = whole turns.  Stillness/noise freezes it via a Chebyshev
radius dead-zone RMIN.

python_widdershins_words() below is a bit-faithful port of the firmware's
integer logic (same median tracker, same octant classifier, same WLUT, same
word packing) so what we emit is provably what the chip would emit given the
same event stream.

Word layout: bits[2:0]=oct, bit[3]=valid, bits[15:4]=wind (12-bit two's
complement, eighth-turns, +/-1023), bits[23:16]=turns (8-bit two's complement,
wind>>3 floor), bits[27:24]=wseq, bits[31:28]=radq (Chebyshev radius >>3).

------------------------------------------------------------------------------
Usage:
  dvs_widdershins_view.py --validate                  # synthetic self-test (numbers)
  dvs_widdershins_view.py --from-actsim results.mem   # render real chip status words
  dvs_widdershins_view.py events.csv                  # render (host-computed) from a CSV
  dvs_widdershins_view.py ... --headless --save widdershins.png
"""
import argparse
import numpy as np

# --- must match software/dvs_widdershins/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
CX0, CY0 = 63, 56
SAMPLE_BATCHES = 8        # sample every 8 batches = 32 events
RMIN = 10                 # Chebyshev radius dead-zone
WIND_CAP = 1023           # wind clamps to [-1023, +1023]
WSEQ_MASK = 0xF
WLUT = [0, 1, 2, 3, 0, -3, -2, -1]   # circular octant diff -> signed step; d=4 ambiguous -> 0


def python_widdershins_words(x, y, pol):
    """Bit-faithful port of software/dvs_widdershins/main.c's ISR.

    x, y, pol are per-event arrays.  Processes only complete batches
    (n - n%BATCH events).

    State: cx=CX0; cy=CY0; wind=0; prev_oct=0; have_prev=0;
           lat_oct=0; lat_valid=0; lat_rad=0; batch_in_sample=0; wseq=0.

    Per batch b, for each event i in order:
      xi = int(x[i]) & 0x7F; yi = int(y[i]) & 0x7F   # pol decoded but unused
      if xi > cx: cx += 1
      elif xi < cx: cx -= 1
      if yi > cy: cy += 1
      elif yi < cy: cy -= 1

    After the 4 events:
      batch_in_sample += 1
      if batch_in_sample >= SAMPLE_BATCHES:
          (sample octant, update wind, update latch fields)

    Then emit ONE word per batch (latch happens BEFORE emit):
      word index i (0-based) carries wseq == ((i+1)//SAMPLE_BATCHES) & 0xF

    Returns (words, samples) where samples is a list of (valid, oct, wind)
    tuples appended at every sample point.
    """
    cx = CX0
    cy = CY0
    wind = 0
    prev_oct = 0
    have_prev = 0
    lat_oct = 0
    lat_valid = 0
    lat_rad = 0
    batch_in_sample = 0
    wseq = 0
    words = []
    samples = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Process 4 events: update median tracker
        for i in range(b, b + BATCH):
            xi = int(x[i]) & 0x7F
            yi = int(y[i]) & 0x7F
            # pol decoded but unused in tracker logic
            if xi > cx:
                cx += 1
            elif xi < cx:
                cx -= 1
            if yi > cy:
                cy += 1
            elif yi < cy:
                cy -= 1

        # After batch: check if we hit a sample boundary
        batch_in_sample += 1
        if batch_in_sample >= SAMPLE_BATCHES:
            batch_in_sample = 0
            dx = cx - CX0
            dy = cy - CY0
            adx = -dx if dx < 0 else dx
            ady = -dy if dy < 0 else dy
            rad = adx if adx > ady else ady
            if rad >= RMIN:
                if dy >= 0:
                    if dx > 0:
                        oct_ = 0 if ady <= adx else 1
                    else:
                        oct_ = 2 if ady > adx else 3
                else:
                    if dx < 0:
                        oct_ = 4 if ady <= adx else 5
                    else:
                        oct_ = 6 if ady > adx else 7
                if have_prev:
                    d = (oct_ - prev_oct) & 7
                    wind += WLUT[d]
                    if wind > WIND_CAP:
                        wind = WIND_CAP
                    if wind < -WIND_CAP:
                        wind = -WIND_CAP
                prev_oct = oct_
                have_prev = 1
                lat_oct = oct_
                lat_valid = 1
                lat_rad = rad
            else:
                lat_valid = 0
                have_prev = 0          # stillness breaks the winding chain
                # lat_oct / lat_rad keep their last valid values
            wseq = (wseq + 1) & WSEQ_MASK   # advances on EVERY sample, valid or not
            samples.append((lat_valid, lat_oct, wind))

        # Emit one word per batch; latch already updated above
        turns = wind >> 3            # Python arithmetic shift == C srai == floor(wind/8)
        word = (((lat_rad >> 3) & 0xF) << 28) | (wseq << 24) | ((turns & 0xFF) << 16) \
             | ((wind & 0xFFF) << 4) | (lat_valid << 3) | lat_oct
        words.append(word)

    return words, samples


def unpack_status(word):
    """Mirror of the firmware's FIFO_OUT packing.
    bits[2:0]=oct, bit[3]=valid, bits[15:4]=wind (12-bit two's complement),
    bits[23:16]=turns (8-bit two's complement), bits[27:24]=wseq,
    bits[31:28]=radq."""
    oct_ = word & 7
    valid = (word >> 3) & 1
    wind = (word >> 4) & 0xFFF
    if wind >= 2048:
        wind -= 4096
    turns = (word >> 16) & 0xFF
    if turns >= 128:
        turns -= 256
    wseq = (word >> 24) & 0xF
    radq = (word >> 28) & 0xF
    return oct_, valid, wind, turns, wseq, radq


# ---------------------------------------------------------------------------
# Renderer: brass-compass aesthetic on dark backdrop.
# ---------------------------------------------------------------------------

def render_widdershins(words, save=None, headless=False):
    """Compose one figure: compass dial, winding readout, per-sample wind history."""
    if not words:
        print("no words to render")
        return

    last_oct, last_valid, last_wind, last_turns, _, _ = unpack_status(words[-1])

    # Collect one wind sample per wseq change (like dvs_entropy's history collection)
    history = []
    prev_wseq = None
    for word in words:
        oct_, valid, wind, turns, wseq, radq = unpack_status(word)
        if wseq != prev_wseq:
            history.append(wind)
            prev_wseq = wseq

    GOLD   = "#e8b84b"
    INDIGO = "#5a5fd4"
    DIM    = "#555566"
    BG     = "#0d0b10"
    TEXT   = "#e8dfc8"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        if last_wind >= 8:
            verdict_str = "DEOSIL (clockwise)"
        elif last_wind <= -8:
            verdict_str = "WIDDERSHINS (counter-clockwise)"
        else:
            verdict_str = "UNWOUND"
        print(f"last wind={last_wind} turns={last_turns} verdict={verdict_str}")
        if history:
            print("per-sample wind history:", history)
        return

    import math

    fig = plt.figure(figsize=(10, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.6], wspace=0.38,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_left  = fig.add_subplot(gs[0])
    ax_right = fig.add_subplot(gs[1])

    # Split right column into readout + history
    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_readout = fig.add_subplot(gs_right[0])
    ax_hist    = fig.add_subplot(gs_right[1])
    ax_right.remove()

    for ax in (ax_left, ax_readout, ax_hist):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Compass dial ---
    ax_left.set_xlim(-1.4, 1.4)
    ax_left.set_ylim(-1.4, 1.4)
    ax_left.set_aspect("equal")
    ax_left.set_xticks([])
    ax_left.set_yticks([])
    ax_left.set_title("compass (octant)", color=TEXT, fontsize=9, pad=4)

    # Outer circle
    circle = mpatches.Circle((0, 0), 1.0, fill=False,
                              edgecolor="#332d40", linewidth=1.5)
    ax_left.add_patch(circle)

    # 8 octant ticks and labels
    # Screen y grows DOWN so oct0=(+dx,+dy near zero) is East,
    # oct1=(+dx,+dy) is SE, etc. going clockwise on screen.
    # Centre angle for octant k: 45*k + 22.5 degrees from East, clockwise
    # (clockwise on screen = increasing angle in standard math with y flipped).
    # We plot in standard axes (y up) so "clockwise on screen" = decreasing math angle.
    # oct_k centre angle (math): -( 45*k + 22.5 ) degrees from East, plus we offset
    # so that E=0deg, SE=-45deg, S=-90deg, SW=-135deg, W=-180deg, NW=-225deg,
    # N=-270deg(=90deg), NE=-315deg(=45deg).
    OCT_LABELS = ["E", "SE", "S", "SW", "W", "NW", "N", "NE"]
    for k in range(8):
        # math angle in radians: East=0, going clockwise on screen = decreasing
        angle_rad = math.radians(-45.0 * k)
        tx = math.cos(angle_rad)
        ty = math.sin(angle_rad)
        # Tick mark
        ax_left.plot([0.88 * tx, 1.0 * tx], [0.88 * ty, 1.0 * ty],
                     color="#332d40", linewidth=1.0)
        # Label
        ax_left.text(1.22 * tx, 1.22 * ty, OCT_LABELS[k],
                     ha="center", va="center", color=DIM, fontsize=7)

    # Needle at last valid octant
    if last_valid:
        needle_angle = math.radians(-(45.0 * last_oct))
        nx = math.cos(needle_angle)
        ny = math.sin(needle_angle)
        ax_left.annotate(
            "", xy=(0.80 * nx, 0.80 * ny), xytext=(0.0, 0.0),
            arrowprops=dict(arrowstyle="-|>", color=GOLD,
                            lw=2.5, mutation_scale=18),
        )
    else:
        ax_left.text(0, 0, "—", ha="center", va="center",
                     color=DIM, fontsize=16)

    # --- Winding readout ---
    ax_readout.set_xlim(0, 1)
    ax_readout.set_ylim(0, 1)
    ax_readout.set_xticks([])
    ax_readout.set_yticks([])
    ax_readout.set_title("winding register", color=TEXT, fontsize=9, pad=4)

    ax_readout.text(0.5, 0.82, f"wind = {last_wind:+d}",
                    ha="center", va="center", color=TEXT,
                    fontsize=15, fontweight="bold",
                    transform=ax_readout.transAxes)
    ax_readout.text(0.5, 0.58, f"turns = {last_turns:+d}",
                    ha="center", va="center", color=TEXT,
                    fontsize=11, transform=ax_readout.transAxes)

    if last_wind >= 8:
        verdict_str = "DEOSIL (clockwise)"
        verdict_color = GOLD
    elif last_wind <= -8:
        verdict_str = "WIDDERSHINS (counter-clockwise)"
        verdict_color = INDIGO
    else:
        verdict_str = "UNWOUND"
        verdict_color = DIM

    ax_readout.text(0.5, 0.30, verdict_str,
                    ha="center", va="center", color=verdict_color,
                    fontsize=10, fontweight="bold",
                    transform=ax_readout.transAxes)

    # --- Wind history: stepped filled area ---
    ax_hist.axhline(0, color=DIM, linewidth=0.7)
    ax_hist.axhline(8,   color=GOLD,   linewidth=0.5, linestyle="--", alpha=0.5)
    ax_hist.axhline(-8,  color=INDIGO, linewidth=0.5, linestyle="--", alpha=0.5)
    if history:
        ws = np.array(history, dtype=float)
        xs = list(range(len(ws)))
        ax_hist.step(xs, ws, where="post", color=TEXT, linewidth=1.0)
        ax_hist.fill_between(xs, ws, 0,
                             where=(ws >= 0), step="post",
                             color=GOLD, alpha=0.35)
        ax_hist.fill_between(xs, ws, 0,
                             where=(ws < 0), step="post",
                             color=INDIGO, alpha=0.35)
    ax_hist.set_xlabel("sample index", color=TEXT, fontsize=8)
    ax_hist.set_ylabel("wind (eighth-turns)", color=TEXT, fontsize=8)
    ax_hist.set_title("per-sample wind history", color=TEXT, fontsize=9, pad=4)

    fig.suptitle('"The Widdershins Engine"', color=TEXT, fontsize=12,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation helpers
# ---------------------------------------------------------------------------

# Clockwise anchors as (dx, dy) offsets from (CX0, CY0) at Chebyshev radius 28.
# Absolute positions: (91,68) (75,84) (51,84) (35,68) (35,44) (51,28) (75,28) (91,44)
# Octants 0..7 exactly.  Per-axis gap between consecutive anchors <= 32 so
# the median tracker converges EXACTLY within one 32-event sample period.
ANCHORS = [
    (+28, +12),   # oct 0  E-ish  (91, 68)
    (+12, +28),   # oct 1  SE     (75, 84)
    (-12, +28),   # oct 2  S-ish  (51, 84)
    (-28, +12),   # oct 3  SW-ish (35, 68)
    (-28, -12),   # oct 4  W-ish  (35, 44)
    (-12, -28),   # oct 5  NW     (51, 28)
    (+12, -28),   # oct 6  N-ish  (75, 28)
    (+28, -12),   # oct 7  NE     (91, 44)
]


def build_anchor_stream(anchor_indices):
    """Build (x, y, pol) arrays from a sequence of anchor indices.

    For each anchor index a in anchor_indices, append 32 events
    (8 batches * 4) all at that absolute position so the tracker
    arrives exactly there by the end of the sample period.
    """
    xs_list = []
    ys_list = []
    ps_list = []
    for a in anchor_indices:
        dx, dy = ANCHORS[a]
        ax_pos = CX0 + dx
        ay_pos = CY0 + dy
        for ev in range(32):
            xs_list.append(ax_pos)
            ys_list.append(ay_pos)
            ps_list.append(ev % 2)  # alternate 1/0, unused by tracker
    x = np.array(xs_list, dtype=np.int64)
    y = np.array(ys_list, dtype=np.int64)
    p = np.array(ps_list, dtype=np.int64)
    return x, y, p


# ---------------------------------------------------------------------------
# Synthetic validation: lettered exact-integer checks, zero tolerance.
# ---------------------------------------------------------------------------

def validate():
    ok = True

    # ------------------------------------------------------------------
    # (a) DEOSIL LAPS EXACT: K=4 CW laps around all 8 octants.
    # anchor_indices = [0,1,2,3,4,5,6,7] * 4  -> 32 sample periods.
    # Every sample valid; sampled octants exactly 0,1,...,7 repeated 4x;
    # final wind == 8*4-1 == 31 exactly (first valid sample only sets prev);
    # final turns == 31>>3 == 3; final radq == 28>>3 == 3.
    # ------------------------------------------------------------------
    K = 4
    ai_a = list(range(8)) * K
    x_a, y_a, p_a = build_anchor_stream(ai_a)
    words_a, samples_a = python_widdershins_words(x_a, y_a, p_a)

    # Check sampled octants
    sampled_octs_a = [s[1] for s in samples_a if s[0]]
    expected_octs_a = list(range(8)) * K
    last_oct_a, last_valid_a, last_wind_a, last_turns_a, _, last_radq_a = \
        unpack_status(words_a[-1])

    a_ok = (
        len(samples_a) == 32
        and all(s[0] == 1 for s in samples_a)
        and sampled_octs_a == expected_octs_a
        and last_wind_a == 31
        and last_turns_a == 3
        and last_radq_a == 3
    )
    print(f"  (a) DEOSIL LAPS EXACT: samples={len(samples_a)} (want 32), "
          f"all valid={all(s[0]==1 for s in samples_a)}, "
          f"octs correct={sampled_octs_a==expected_octs_a}, "
          f"final wind={last_wind_a} (want 31), "
          f"turns={last_turns_a} (want 3), "
          f"radq={last_radq_a} (want 3) -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) WIDDERSHINS ANTISYMMETRY: CCW laps -> final wind == -31.
    # Also check WLUT oddness: all(WLUT[(8-d)&7] == -WLUT[d] for d in range(8)).
    # ------------------------------------------------------------------
    ai_b = list(range(7, -1, -1)) * K
    x_b, y_b, p_b = build_anchor_stream(ai_b)
    words_b, samples_b = python_widdershins_words(x_b, y_b, p_b)

    last_oct_b, last_valid_b, last_wind_b, last_turns_b, _, _ = \
        unpack_status(words_b[-1])

    wlut_odd = all(WLUT[(8 - d) & 7] == -WLUT[d] for d in range(8))
    b_ok = (
        last_wind_b == -31
        and last_wind_b == -last_wind_a
        and last_turns_b == -4        # floor(-31/8) == -4
        and wlut_odd
    )
    print(f"  (b) WIDDERSHINS ANTISYMMETRY: final wind={last_wind_b} (want -31), "
          f"wind_b==-wind_a={last_wind_b==-last_wind_a}, "
          f"turns={last_turns_b} (want -4), "
          f"WLUT odd={wlut_odd} -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) STILLNESS GUARD: 1024 events with small offsets around centre;
    # tracker never leaves +/-4 box, rad < RMIN always, every word valid==0,
    # wind field==0, turns==0; final wind==0.
    # ------------------------------------------------------------------
    offsets = [(4, 0), (-4, 1), (0, -4), (-2, -2), (3, 3), (-3, 2), (1, -3), (-1, 4)]
    xs_c_list = []
    ys_c_list = []
    ps_c_list = []
    n_still = 1024
    for i in range(n_still):
        ox, oy = offsets[i % len(offsets)]
        xs_c_list.append(CX0 + ox)
        ys_c_list.append(CY0 + oy)
        ps_c_list.append(i % 2)
    x_c = np.array(xs_c_list, dtype=np.int64)
    y_c = np.array(ys_c_list, dtype=np.int64)
    p_c = np.array(ps_c_list, dtype=np.int64)
    words_c, samples_c = python_widdershins_words(x_c, y_c, p_c)

    all_invalid_c = all(((w >> 3) & 1) == 0 for w in words_c)
    all_wind_zero_c = all(((w >> 4) & 0xFFF) == 0 for w in words_c)
    all_turns_zero_c = all(((w >> 16) & 0xFF) == 0 for w in words_c)
    _, _, final_wind_c, final_turns_c, _, _ = unpack_status(words_c[-1])
    c_ok = (
        all_invalid_c
        and all_wind_zero_c
        and all_turns_zero_c
        and final_wind_c == 0
    )
    print(f"  (c) STILLNESS GUARD: all valid==0={all_invalid_c}, "
          f"all wind==0={all_wind_zero_c}, "
          f"all turns==0={all_turns_zero_c}, "
          f"final wind={final_wind_c} (want 0) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) HALF-TURN AMBIGUITY DROP: alternate inline anchors
    # A=(+12,+5) (oct0) absolute (75,61) and B=(-12,-5) (oct4) absolute (51,51).
    # Per-axis gaps: 24 in x, 10 in y, both <= 32 -> tracker converges exactly.
    # d==4 always -> WLUT[4]==0 -> wind stays exactly 0 on every word.
    # ------------------------------------------------------------------
    A_pos_d = (CX0 + 12, CY0 + 5)    # (75, 61)  oct 0
    B_pos_d = (CX0 - 12, CY0 - 5)    # (51, 51)  oct 4
    n_periods_d = 16
    xs_d_list = []
    ys_d_list = []
    ps_d_list = []
    for i in range(n_periods_d):
        ax, ay = A_pos_d if i % 2 == 0 else B_pos_d
        for ev in range(32):
            xs_d_list.append(ax)
            ys_d_list.append(ay)
            ps_d_list.append(ev % 2)
    x_d = np.array(xs_d_list, dtype=np.int64)
    y_d = np.array(ys_d_list, dtype=np.int64)
    p_d = np.array(ps_d_list, dtype=np.int64)
    words_d, samples_d = python_widdershins_words(x_d, y_d, p_d)

    sampled_octs_d = [s[1] for s in samples_d]
    expected_octs_d = [0 if i % 2 == 0 else 4 for i in range(n_periods_d)]
    all_wind_zero_d = all(((w >> 4) & 0xFFF) == 0 for w in words_d)
    d_ok = (
        len(samples_d) == n_periods_d
        and all(s[0] == 1 for s in samples_d)
        and sampled_octs_d == expected_octs_d
        and all_wind_zero_d
    )
    print(f"  (d) HALF-TURN AMBIGUITY DROP: samples={len(samples_d)} (want {n_periods_d}), "
          f"all valid={all(s[0]==1 for s in samples_d)}, "
          f"octs alternate 0/4={sampled_octs_d==expected_octs_d}, "
          f"all wind==0={all_wind_zero_d} -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) TURNS FLOOR: over all words from (a)+(b)+(d):
    # sign-extended turns == sign-extended wind >> 3 (Python floor shift).
    # ------------------------------------------------------------------
    all_words_e = words_a + words_b + words_d
    turns_ok = True
    bad_e = []
    for w in all_words_e:
        _, _, wind_e, turns_e, _, _ = unpack_status(w)
        expected_turns = wind_e >> 3  # Python floor shift
        if turns_e != expected_turns:
            turns_ok = False
            bad_e.append(f"wind={wind_e} turns={turns_e} expected={expected_turns}")
            if len(bad_e) >= 3:
                break
    e_ok = turns_ok
    print(f"  (e) TURNS FLOOR: {len(all_words_e)} words checked; "
          f"turns==wind>>3 (floor) for all -> "
          f"{'OK' if e_ok else 'FAIL: ' + '; '.join(bad_e[:3])}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) WSEQ ARITHMETIC: concatenation of streams (a) and (c) as ONE
    # contiguous stream; word i carries wseq == ((i+1)//SAMPLE_BATCHES) & 0xF.
    # ------------------------------------------------------------------
    x_f = np.concatenate([x_a, x_c])
    y_f = np.concatenate([y_a, y_c])
    p_f = np.concatenate([p_a, p_c])
    words_f, _ = python_widdershins_words(x_f, y_f, p_f)
    wseq_ok = True
    bad_f = []
    for i, word in enumerate(words_f):
        expected_wseq = ((i + 1) // SAMPLE_BATCHES) & WSEQ_MASK
        _, _, _, _, actual_wseq, _ = unpack_status(word)
        if actual_wseq != expected_wseq:
            wseq_ok = False
            bad_f.append(f"i={i} got={actual_wseq} want={expected_wseq}")
            if len(bad_f) >= 3:
                break
    f_ok = wseq_ok
    print(f"  (f) WSEQ ARITHMETIC: every word[i] has "
          f"wseq==((i+1)//{SAMPLE_BATCHES})&0xF -> "
          f"{'OK' if f_ok else 'FAIL: ' + '; '.join(bad_f[:3])}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) WELL-FORMEDNESS: over all words from every stream above.
    # oct in 0..7, valid in {0,1}, sign-extended wind in [-1023,1023],
    # sign-extended turns in [-128,127], wseq <= 15, radq <= 15, word < 2**32.
    # ------------------------------------------------------------------
    all_words_g = words_a + words_b + words_c + words_d
    well_ok = True
    bad_g = []
    for word in all_words_g:
        oct_g, valid_g, wind_g, turns_g, wseq_g, radq_g = unpack_status(word)
        if oct_g not in range(8):
            bad_g.append(f"oct={oct_g}"); well_ok = False
        if valid_g not in (0, 1):
            bad_g.append(f"valid={valid_g}"); well_ok = False
        if not (-1023 <= wind_g <= 1023):
            bad_g.append(f"wind={wind_g}"); well_ok = False
        if not (-128 <= turns_g <= 127):
            bad_g.append(f"turns={turns_g}"); well_ok = False
        if wseq_g > 15:
            bad_g.append(f"wseq={wseq_g}"); well_ok = False
        if radq_g > 15:
            bad_g.append(f"radq={radq_g}"); well_ok = False
        if word >= (1 << 32):
            bad_g.append(f"word=0x{word:08x}"); well_ok = False
    g_ok = well_ok
    print(f"  (g) WELL-FORMEDNESS: {len(all_words_g)} total words; "
          f"oct in 0..7, valid in {{0,1}}, wind in [-1023,1023], "
          f"turns in [-128,127], wseq<=15, radq<=15, word<2^32 -> "
          f"{'OK' if g_ok else 'FAIL: ' + '; '.join(bad_g[:5])}")
    ok = ok and g_ok

    print()
    print("VALIDATION:", "PASS -- deosil laps exact; widdershins antisymmetry exact; "
          "stillness guard proven; half-turn ambiguity drop exact; "
          "turns floor exact; wseq arithmetic exact; word fields well-formed"
          if ok else "FAIL")
    return ok


def load_csv(path):
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name.strip(): i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x   = np.array([int(row[idx["x"]])   for row in rows], dtype=np.int64)
    y   = np.array([int(row[idx["y"]])   for row in rows], dtype=np.int64)
    pol = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int64)
    return x, y, pol


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: deosil laps, widdershins antisymmetry, "
                         "stillness guard, half-turn ambiguity drop, turns floor, "
                         "wseq arithmetic, well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the widdershins PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing widdershins words in "
              f"Python (bit-faithful mirror of firmware).")
        words, _ = python_widdershins_words(x, y, pol)
        if words:
            last_oct, last_valid, last_wind, last_turns, last_wseq, last_radq = \
                unpack_status(words[-1])
            if last_wind >= 8:
                verdict_str = "DEOSIL (clockwise)"
            elif last_wind <= -8:
                verdict_str = "WIDDERSHINS (counter-clockwise)"
            else:
                verdict_str = "UNWOUND"
            print(f"final wind={last_wind:+d}, turns={last_turns:+d}, "
                  f"verdict={verdict_str} ({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_widdershins(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
