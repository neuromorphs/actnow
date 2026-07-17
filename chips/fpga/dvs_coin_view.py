#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_coin/main.c
("Heads or Tails, Mid-Air" -- predicts a coin-toss face at the apex of the
trajectory, before the coin lands).

The firmware tracks:
  - Vertical centroid over WIN_SIZE-event windows (y_sum >> WIN_SHIFT).
  - Glint peaks: each WIN_SIZE-event window whose timestamp span is SHORT
    (span < GLINT_DT ticks) is a "compact" window = coin face in view.
    Consecutive compact-window onsets give the half-spin period TS_HALF.
  - APEX: centroid sign flip from rising (delta<0) to falling (delta>0),
    after at least RISE_STEPS rising windows and MIN_GLINTS compact windows.
  - Prediction at apex: remaining_time ≈ elapsed_time (time symmetry);
    remaining half-turns via repeated subtraction (no divide); parity of
    (glints_so_far + remaining_halfturns) gives HEADS (even) or TAILS (odd).

python_coin_words() is a bit-faithful port of the firmware's integer logic
(same windowed centroid, same compact-window glint detector, same repeated-
subtraction predictor, same word packing).

Word layout (27 bits used):
  bits[ 1: 0] = prediction  (0=none, 1=HEADS, 2=TAILS, 3=reserved)
  bits[ 9: 2] = halfturns   (0..255, predicted remaining half-turns at apex)
  bits[17:10] = glint_count (0..255, compact windows seen so far, saturated)
  bits[18]    = apex_reached (0 or 1)
  bits[19]    = valid        (1 once MIN_GLINTS seen + trajectory + ts_half ok)
  bits[23:20] = seq          (4-bit batch sequence counter, wraps mod 16)
  bits[31:24] = 0

------------------------------------------------------------------------------
Usage:
  dvs_coin_view.py --validate                   # synthetic self-test
  dvs_coin_view.py --from-actsim results.mem    # render real chip status words
  dvs_coin_view.py events.csv                   # render (host-computed) from CSV
  dvs_coin_view.py ... --headless --save coin.png
"""
import argparse
import numpy as np

# --- must match software/dvs_coin/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
TS_MASK = 0xFFFF

WIN_SHIFT = 4
WIN_SIZE  = 1 << WIN_SHIFT    # 16

GLINT_DT  = 24               # compact if span < GLINT_DT ticks

HS_MIN = 32
HS_MAX = 8000

MIN_GLINTS = 3
RISE_STEPS = 2
COUNT_CAP  = 255
SEQ_MASK   = 0xF

PREDICTION_NAMES = ["none", "HEADS", "TAILS", "reserved"]


# ---------------------------------------------------------------------------
# Bit-faithful integer helpers
# ---------------------------------------------------------------------------

def divfloor_sat(a, b):
    """Repeated-subtraction floor division, saturating at COUNT_CAP.

    Mirrors firmware's divfloor_sat() exactly.  Returns 0 if b==0.
    """
    if b == 0:
        return 0
    q = 0
    while a >= b and q < COUNT_CAP:
        a -= b
        q += 1
    return q


# ---------------------------------------------------------------------------
# Bit-faithful firmware mirror
# ---------------------------------------------------------------------------

def python_coin_words(x_arr, y_arr, ts_arr, pol_arr):
    """Bit-faithful port of software/dvs_coin/main.c's ISR.

    Processes events in order in complete batches of BATCH.  Returns
    (words, events) where:
      - words  : list of packed uint32 status words (one per batch)
      - events : list of dicts, one per apex:
                   {batch, glint_count, halfturns, prediction, ts_half}

    All state mirrors firmware cold-start (all zeros, same as .bss zero).
    """
    # Window state
    y_sum          = 0
    win_cnt        = 0
    win_start_ts   = 0
    have_win_start = 0

    # Centroid trajectory state
    prev_centy      = 0
    have_prev_centy = 0
    rise_streak     = 0

    # Glint state
    in_glint         = 0
    prev_glint_ts    = 0
    first_glint_ts   = 0
    have_prev_glint  = 0
    have_first_glint = 0
    ts_half          = 0
    glint_count      = 0
    last_ts          = 0    # noqa: F841 -- mirrors firmware, kept for clarity

    # Apex / prediction state
    apex_reached = 0
    valid        = 0

    # Latched output fields
    lat_prediction   = 0
    lat_halfturns    = 0
    lat_glint_count  = 0
    lat_apex_reached = 0
    lat_valid        = 0
    seq              = 0

    words  = []
    events = []
    n      = len(x_arr)

    for b_start in range(0, n - n % BATCH, BATCH):
        for i in range(b_start, b_start + BATCH):
            yi  = int(y_arr[i]) & 0x7F
            ts  = int(ts_arr[i]) & TS_MASK
            last_ts = ts  # noqa: F841

            # ----------------------------------------------------------------
            # Centroid + glint window
            # ----------------------------------------------------------------
            if not have_win_start:
                win_start_ts   = ts
                have_win_start = 1

            y_sum   = min(y_sum + yi, 0xFFFFFFFF)
            win_cnt += 1

            if win_cnt >= WIN_SIZE:
                centy = y_sum >> WIN_SHIFT
                span  = (ts - win_start_ts) & TS_MASK

                # ---- GLINT DETECTION ----
                is_compact = 1 if span < GLINT_DT else 0

                if is_compact and not in_glint:
                    in_glint = 1

                    if have_prev_glint:
                        dt = (win_start_ts - prev_glint_ts) & TS_MASK
                        if HS_MIN <= dt <= HS_MAX:
                            ts_half = dt

                    if not have_first_glint:
                        first_glint_ts   = win_start_ts
                        have_first_glint = 1

                    prev_glint_ts   = win_start_ts
                    have_prev_glint = 1

                    if glint_count < COUNT_CAP:
                        glint_count += 1

                    if (not apex_reached
                            and glint_count >= MIN_GLINTS
                            and ts_half >= HS_MIN
                            and ts_half <= HS_MAX):
                        valid = 1

                    lat_glint_count = min(glint_count, COUNT_CAP)
                    lat_valid       = valid

                elif not is_compact:
                    in_glint = 0

                # ---- CENTROID TRACKING ----
                if have_prev_centy and not apex_reached:
                    if centy > prev_centy:
                        # Centroid moved down (coin falling)
                        if rise_streak >= RISE_STEPS:
                            # Apex detected
                            apex_reached = 1

                            if (glint_count >= MIN_GLINTS
                                    and ts_half >= HS_MIN
                                    and ts_half <= HS_MAX
                                    and have_first_glint):
                                valid = 1
                                elapsed   = (ts - first_glint_ts) & TS_MASK
                                extra     = divfloor_sat(elapsed, ts_half)
                                total_ht  = glint_count + extra
                                if total_ht > COUNT_CAP:
                                    total_ht = COUNT_CAP
                                lat_halfturns  = extra
                                lat_prediction = 1 if (total_ht & 1) == 0 else 2
                            else:
                                lat_prediction = 0
                                lat_halfturns  = 0

                            lat_apex_reached = 1
                            lat_valid        = valid
                            lat_glint_count  = min(glint_count, COUNT_CAP)

                            events.append({
                                "batch":       len(words),
                                "glint_count": glint_count,
                                "halfturns":   lat_halfturns,
                                "prediction":  lat_prediction,
                                "ts_half":     ts_half,
                            })

                        rise_streak = 0

                    elif centy < prev_centy:
                        # Centroid moved up (coin rising)
                        if rise_streak < COUNT_CAP:
                            rise_streak += 1

                prev_centy      = centy
                have_prev_centy = 1

                # Reset window
                y_sum          = 0
                win_cnt        = 0
                have_win_start = 0

        # Emit one word per batch
        seq = (seq + 1) & SEQ_MASK
        word = ((seq              << 20)
              | (lat_valid        << 19)
              | (lat_apex_reached << 18)
              | (lat_glint_count  << 10)
              | (lat_halfturns    <<  2)
              |  lat_prediction)
        words.append(word)

    return words, events


def unpack_status(word):
    """Unpack one dvs_coin status word.

    bits[ 1: 0] = prediction   (0=none, 1=HEADS, 2=TAILS)
    bits[ 9: 2] = halfturns    (0..255)
    bits[17:10] = glint_count  (0..255)
    bits[18]    = apex_reached
    bits[19]    = valid
    bits[23:20] = seq          (0..15)
    bits[31:24] = 0
    """
    prediction   =  word        & 0x3
    halfturns    = (word >>  2) & 0xFF
    glint_count  = (word >> 10) & 0xFF
    apex_reached = (word >> 18) & 0x1
    valid        = (word >> 19) & 0x1
    seq          = (word >> 20) & 0xF
    return prediction, halfturns, glint_count, apex_reached, valid, seq


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def load_csv(path, ts_col="le"):
    """Load event CSV (columns: le, x, y, pol).

    ts_col: column name for the timestamp field (default 'le').
    If ts_col absent from header, ts is zeroed.
    NOTE: recorded captures carry a wrapped coarse counter; verdicts on real
    captures are qualitative.  --validate builds synthetic streams with
    known properties and is the deterministic correctness check.
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
    if ts_col in idx:
        ts = np.array([int(row[idx[ts_col]]) & TS_MASK for row in rows],
                      dtype=np.int64)
    else:
        ts = np.zeros(len(x), dtype=np.int64)
    return x, y, ts, pol


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def render_coin(words, save=None, headless=False):
    """Render: schematic arc + big HEADS/TAILS stamp latched at apex."""
    if not words:
        print("no words to render")
        return

    pred_last, ht_last, gc_last, apex_last, valid_last, _ = unpack_status(words[-1])

    # Per-word glint history and apex index
    glint_history = []
    apex_batch    = None
    pred_latch    = 0
    for idx_w, word in enumerate(words):
        pred_, ht_, gc_, apex_, valid_, seq_ = unpack_status(word)
        glint_history.append(gc_)
        if apex_ and apex_batch is None:
            apex_batch = idx_w
            pred_latch = pred_

    BG     = "#0a0a10"
    TEXT   = "#e8dfc8"
    GOLD   = "#e8b84b"
    SILVER = "#aab8c8"
    RED    = "#d45060"
    GREEN  = "#50d480"
    DIM    = "#444455"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patheffects as pe
    except Exception as exc:
        print("matplotlib unavailable:", exc)
        print(f"last prediction={PREDICTION_NAMES[pred_last]} "
              f"halfturns={ht_last} glints={gc_last} "
              f"apex={apex_last} valid={valid_last}")
        return

    fig = plt.figure(figsize=(10, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1.1, 1], wspace=0.35,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_arc   = fig.add_subplot(gs[0])
    ax_right = fig.add_subplot(gs[1])

    for ax in (ax_arc, ax_right):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#33334a")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left: parabolic arc + HEADS/TAILS stamp ---
    ax_arc.set_xlim(0, 1)
    ax_arc.set_ylim(-0.15, 1.05)
    ax_arc.set_aspect("equal")
    ax_arc.set_xticks([])
    ax_arc.set_yticks([])
    ax_arc.set_title("trajectory (schematic)", color=TEXT, fontsize=9, pad=4)

    t_arc = np.linspace(0, 1, 200)
    y_arc = 1.0 - (2 * t_arc - 1) ** 2
    ax_arc.plot(t_arc, y_arc, color=SILVER, linewidth=1.5, alpha=0.5)

    ax_arc.scatter([0.5], [1.0], color=GOLD, s=60, zorder=5)
    ax_arc.text(0.5, 1.03, "APEX", ha="center", va="bottom",
                color=GOLD, fontsize=8)

    if apex_last and valid_last and pred_latch in (1, 2):
        stamp_text  = PREDICTION_NAMES[pred_latch]
        stamp_color = GREEN if pred_latch == 1 else RED
    else:
        stamp_text  = "?"
        stamp_color = DIM

    ax_arc.text(0.5, 0.38, stamp_text,
                ha="center", va="center",
                fontsize=38, fontweight="bold",
                color=stamp_color,
                path_effects=[pe.withStroke(linewidth=4, foreground=BG)])

    ax_arc.text(0.5, 0.04,
                f"glints={gc_last}  extra half-turns={ht_last}",
                ha="center", va="bottom", color=TEXT, fontsize=8)

    # --- Right: glint-count history ---
    ax_right.set_title("glint count history", color=TEXT, fontsize=9, pad=4)
    xs = list(range(len(glint_history)))
    ax_right.step(xs, glint_history, where="post", color=GOLD, linewidth=1.2,
                  label="glint count")
    ax_right.axhline(MIN_GLINTS, color=SILVER, linewidth=0.8, linestyle="--",
                     alpha=0.7, label=f"MIN_GLINTS={MIN_GLINTS}")
    if apex_batch is not None:
        col = GREEN if pred_latch == 1 else RED
        ax_right.axvline(apex_batch, color=col, linewidth=1.0, linestyle=":",
                         alpha=0.9, label="apex")
    ax_right.set_xlabel("batch index", color=TEXT, fontsize=8)
    ax_right.set_ylabel("cumulative compact windows", color=TEXT, fontsize=8)
    ax_right.legend(fontsize=7, framealpha=0.2, labelcolor=TEXT,
                    facecolor=BG, edgecolor="#33334a")

    fig.suptitle('"Heads or Tails, Mid-Air"', color=TEXT, fontsize=12,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic stream builders
# ---------------------------------------------------------------------------

def build_coin_stream(n_glints, half_spin_ticks, glint_span, sparse_span,
                      n_rise_windows, n_fall_windows, t0=1000):
    """Build a synthetic coin-toss event stream for validation.

    Each WIN_SIZE-event window is either a "glint" (compact, span < GLINT_DT)
    or a normal window (sparse, span >= GLINT_DT).  Glints are distributed
    evenly among the rise windows.

    During rise, y values decrease linearly from 80 to 30 (centroid falling
    in pixel-row terms = coin physically rising).  During fall, y values
    increase from 40 to 80 (coin physically falling).  This guarantees a
    rising centroid trajectory followed by a falling one -- triggering apex
    detection -- while interleaving compact glint windows.

    Parameters
    ----------
    n_glints : int
        Number of compact-window glints to inject (must be <= n_rise_windows).
    half_spin_ticks : int
        Timestamp advance after each compact window (approximates HS).
    glint_span : int
        Timestamp span of each compact window (must be < GLINT_DT).
    sparse_span : int
        Timestamp span of each sparse window (must be >= GLINT_DT).
    n_rise_windows : int
        Number of WIN_SIZE-event windows in the rise phase.
    n_fall_windows : int
        Number of WIN_SIZE-event windows in the fall phase.
    t0 : int
        Starting timestamp.

    Returns x, y, ts, pol arrays (dtype int64), padded to BATCH multiple.
    """
    xs, ys, tss, pols = [], [], [], []
    ts = t0

    # y values: rise phase decreases (coin rising = lower pixel row),
    # fall phase increases (coin falling = higher pixel row).
    y_rise_start, y_rise_end = 80, 30
    y_fall_start, y_fall_end = 40, 80

    # Distribute glint windows evenly within the rise phase.
    glint_windows = set()
    for g in range(n_glints):
        w_idx = int(g * n_rise_windows / max(n_glints, 1))
        glint_windows.add(w_idx)

    total_windows = n_rise_windows + n_fall_windows

    for wnum in range(total_windows):
        if wnum < n_rise_windows:
            frac  = wnum / max(n_rise_windows - 1, 1)
            y_val = int(y_rise_start - frac * (y_rise_start - y_rise_end))
            is_compact = wnum in glint_windows
        else:
            frac  = (wnum - n_rise_windows) / max(n_fall_windows - 1, 1)
            y_val = int(y_fall_start + frac * (y_fall_end - y_fall_start))
            is_compact = False

        if is_compact:
            dt = max(glint_span // max(WIN_SIZE - 1, 1), 0)
            for j in range(WIN_SIZE):
                xs.append(63); ys.append(y_val)
                tss.append(ts + j * dt); pols.append(j % 2)
            ts += half_spin_ticks
        else:
            dt = max(sparse_span // max(WIN_SIZE - 1, 1), 1)
            for j in range(WIN_SIZE):
                xs.append(63); ys.append(y_val)
                tss.append(ts + j * dt); pols.append(j % 2)
            ts += sparse_span

    # Pad to BATCH multiple
    pad = (BATCH - len(xs) % BATCH) % BATCH
    if pad:
        ts_pad = int(tss[-1]) + 2 if tss else t0
        for k in range(pad):
            xs.append(63); ys.append(y_fall_end)
            tss.append(ts_pad + k * 2); pols.append(0)

    return (np.array(xs,   dtype=np.int64),
            np.array(ys,   dtype=np.int64),
            np.array(tss,  dtype=np.int64),
            np.array(pols, dtype=np.int64))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks.

    (A) PARABOLIC APEX: parabolic centroid + enough compact-window glints
        -> apex_reached=1, valid=1, prediction in {1,2}.
        Parity check: glint_count + halfturns parity matches prediction.
    (B) MONOTONE (NO APEX): centroid only falls -> apex=0, prediction=0.
    (C) WELL-FORMEDNESS: all field ranges and upper-bit invariants.
    (D) GLINT DETECTION: alternating compact/sparse windows -> correct count.
    (E) REPEATED-SUBTRACTION: divfloor_sat exact for tabulated (a,b) pairs.
    """
    ok = True

    # ------------------------------------------------------------------
    # (E) REPEATED-SUBTRACTION exact
    # ------------------------------------------------------------------
    rs_cases = [
        (0,    10,  0),
        (10,   10,  1),
        (99,   10,  9),
        (100,  10, 10),
        (255,   1, COUNT_CAP),   # saturates
        (0,     0,  0),           # b=0 guard
        (500,   7, 71),
        (1000, 13, 76),
    ]
    rs_ok  = True
    rs_bad = []
    for a, b, expected in rs_cases:
        got = divfloor_sat(a, b)
        if got != expected:
            rs_ok = False
            rs_bad.append(f"divfloor_sat({a},{b})={got} want {expected}")
    print(f"  (E) REPEATED-SUBTRACTION: "
          f"{rs_bad if not rs_ok else 'all exact'} -> "
          f"{'OK' if rs_ok else 'FAIL'}")
    ok = ok and rs_ok

    # ------------------------------------------------------------------
    # (D) GLINT DETECTION
    # Build a stream of alternating compact (span < GLINT_DT) and sparse
    # (span >= GLINT_DT) windows.  Expect glint_count >= n_glints_D.
    # ------------------------------------------------------------------
    n_glints_D    = MIN_GLINTS + 1   # 4 glints
    hs_D          = 200              # half-spin ticks between glint onsets
    glint_span_D  = GLINT_DT - 4    # compact span (must be < GLINT_DT)
    sparse_span_D = GLINT_DT + 20   # sparse span (must be >= GLINT_DT)

    # Build a simple alternating stream: each glint is WIN_SIZE events in
    # glint_span_D ticks; each sparse window is WIN_SIZE events in sparse_span_D.
    xs_D, ys_D, tss_D, pols_D = [], [], [], []
    ts_D = 1000
    for g in range(n_glints_D):
        # Compact window (glint)
        dt_c = max(glint_span_D // max(WIN_SIZE - 1, 1), 0)
        for j in range(WIN_SIZE):
            xs_D.append(63); ys_D.append(56)
            tss_D.append(ts_D + j * dt_c); pols_D.append(j % 2)
        ts_D += hs_D  # jump by half-spin period

        # Sparse window (between glints)
        dt_s = max(sparse_span_D // max(WIN_SIZE - 1, 1), 1)
        for j in range(WIN_SIZE):
            xs_D.append(63); ys_D.append(56)
            tss_D.append(ts_D + j * dt_s); pols_D.append(j % 2)
        ts_D += sparse_span_D

    # Pad to BATCH multiple
    pad_D = (BATCH - len(xs_D) % BATCH) % BATCH
    if pad_D:
        for k in range(pad_D):
            xs_D.append(63); ys_D.append(56)
            tss_D.append(ts_D + k * 2); pols_D.append(0)

    words_D, _ = python_coin_words(
        np.array(xs_D,   dtype=np.int64),
        np.array(ys_D,   dtype=np.int64),
        np.array(tss_D,  dtype=np.int64),
        np.array(pols_D, dtype=np.int64))

    gc_D_final = 0
    if words_D:
        _, _, gc_D_final, _, _, _ = unpack_status(words_D[-1])

    d_ok = gc_D_final >= n_glints_D
    print(f"  (D) GLINT DETECTION: glint_count={gc_D_final} "
          f"(want>={n_glints_D}) -> {'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (A) PARABOLIC APEX
    # Stream: n_rise_windows rising windows interspersed with n_glints
    # compact windows, then n_fall_windows falling windows.
    # Expect apex_reached=1, valid=1, prediction in {1,2}.
    # Also check parity: total_ht = glint_count + extra_halfturns; face
    # = HEADS iff total_ht even.
    # ------------------------------------------------------------------
    n_glints_A      = MIN_GLINTS + 1   # 4, ensures valid
    hs_A            = 150              # half-spin ticks
    glint_span_A    = GLINT_DT - 4    # compact
    sparse_span_A   = GLINT_DT + 30   # sparse (between glints)
    n_rise_A        = n_glints_A * 2 + RISE_STEPS + 2  # enough rise windows
    n_fall_A        = RISE_STEPS + 2  # enough to trigger apex

    x_A, y_A, ts_A, pol_A = build_coin_stream(
        n_glints=n_glints_A,
        half_spin_ticks=hs_A,
        glint_span=glint_span_A,
        sparse_span=sparse_span_A,
        n_rise_windows=n_rise_A,
        n_fall_windows=n_fall_A,
    )

    words_A, events_A = python_coin_words(x_A, y_A, ts_A, pol_A)

    if words_A:
        pred_A, ht_A, gc_A, apex_A, valid_A, _ = unpack_status(words_A[-1])
    else:
        pred_A, ht_A, gc_A, apex_A, valid_A = 0, 0, 0, 0, 0

    a_apex  = apex_A == 1
    a_valid = valid_A == 1
    a_pred  = pred_A in (1, 2)
    a_ok    = a_apex and a_valid and a_pred

    print(f"  (A) PARABOLIC APEX: apex_reached={apex_A} (want 1), "
          f"valid={valid_A} (want 1), "
          f"prediction={PREDICTION_NAMES[pred_A]} (want HEADS or TAILS), "
          f"halfturns={ht_A}, glints={gc_A} -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # Parity verification
    if events_A and valid_A and a_ok:
        ev       = events_A[0]
        total_ht = ev["glint_count"] + ev["halfturns"]
        exp_pred = 1 if (total_ht & 1) == 0 else 2
        par_ok   = pred_A == exp_pred
        print(f"      parity: glints={ev['glint_count']} extra={ev['halfturns']} "
              f"total={total_ht} expected={PREDICTION_NAMES[exp_pred]} "
              f"got={PREDICTION_NAMES[pred_A]} -> "
              f"{'OK' if par_ok else 'FAIL'}")
        ok = ok and par_ok

    # ------------------------------------------------------------------
    # (B) MONOTONE (NO APEX)
    # Centroid only falls (y always high): no sign flip after RISE_STEPS.
    # Expect apex_reached=0, prediction=0.
    # ------------------------------------------------------------------
    n_B  = 12 * WIN_SIZE
    n_B  = (n_B // BATCH) * BATCH
    # y always large (falling): no rise streak ever builds up
    xs_B   = np.full(n_B, 63, dtype=np.int64)
    ys_B   = np.full(n_B, (SY * 3) // 4, dtype=np.int64)  # constant high y
    # Sparse timestamps: span >= GLINT_DT always (no compact windows either)
    tss_B  = np.arange(1000, 1000 + n_B * (GLINT_DT + 5),
                       GLINT_DT + 5, dtype=np.int64)[:n_B]
    pols_B = np.zeros(n_B, dtype=np.int64)

    words_B, events_B = python_coin_words(xs_B, ys_B, tss_B, pols_B)

    if words_B:
        pred_B, ht_B, gc_B, apex_B, valid_B, _ = unpack_status(words_B[-1])
    else:
        pred_B, ht_B, gc_B, apex_B, valid_B = 0, 0, 0, 0, 0

    b_ok = apex_B == 0 and pred_B == 0
    print(f"  (B) MONOTONE NO APEX: apex_reached={apex_B} (want 0), "
          f"prediction={PREDICTION_NAMES[pred_B]} (want none) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (C) WELL-FORMEDNESS
    # ------------------------------------------------------------------
    all_words_C = words_A + words_B
    bad_C = []
    for word in all_words_C:
        pred_, ht_, gc_, apex_, valid_, seq_ = unpack_status(word)
        if pred_ > 3:
            bad_C.append(f"prediction={pred_}")
        if ht_ > 255:
            bad_C.append(f"halfturns={ht_}")
        if gc_ > 255:
            bad_C.append(f"glint_count={gc_}")
        if apex_ > 1:
            bad_C.append(f"apex_reached={apex_}")
        if valid_ > 1:
            bad_C.append(f"valid={valid_}")
        if seq_ > 15:
            bad_C.append(f"seq={seq_}")
        if (word >> 24) != 0:
            bad_C.append(f"upper bits set: 0x{word:08x}")
        if word >= (1 << 32):
            bad_C.append(f"word>=2^32: 0x{word:08x}")
        if bad_C:
            break
    c_ok = len(bad_C) == 0
    print(f"  (C) WELL-FORMEDNESS: {len(all_words_C)} words; "
          f"all ranges + upper-bit zero -> "
          f"{'OK' if c_ok else 'FAIL: ' + '; '.join(bad_C[:5])}")
    ok = ok and c_ok

    print()
    print("VALIDATION:",
          "PASS -- repeated-subtraction exact; glint detection correct; "
          "parabolic apex predicted; parity verified; monotone no-apex guard; "
          "word fields well-formed"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line)")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column for timestamp (default: le)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, ts, pol = load_csv(args.csv, args.ts_col)
        print(f"loaded {len(x)} events from {args.csv}; computing coin words "
              f"(bit-faithful mirror of firmware).")
        words, events = python_coin_words(x, y, ts, pol)
        if words:
            pred, ht, gc, apex, valid, _ = unpack_status(words[-1])
            print(f"final: prediction={PREDICTION_NAMES[pred]}, "
                  f"halfturns={ht}, glints={gc}, "
                  f"apex={apex}, valid={valid} ({len(words)} words)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_coin(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
