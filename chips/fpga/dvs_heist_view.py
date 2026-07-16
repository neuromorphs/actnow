#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_heist/main.c
("The Museum Heist" -- cross the camera's field of view without tripping a
motion alarm.  A global leaky rate integrator R charges by +1 per event and
decays by R>>LEAK_K per batch; ALARM fires when R > ALARM_THRESH.  An 8-bin
column histogram (bin[x>>4], decaying >>1 every HIST_DECAY_BATCHES batches)
tracks the burglar's horizontal position as argmax.  Progress ratchets the
maximum argmax column reached; a clean crossing completes when progress==7.

Column mapping: x is 7 bits (0..125); col = (x>>4)&7 partitions 126 pixels
into 8 bins of 16 pixels each (x=0..15->col 0, x=16..31->col 1, ...,
x=112..125->col 7).

python_heist_words() below is a bit-faithful port of the firmware's integer
logic (same leaky integrator, same column histogram, same argmax, same seq
counter and word packing) so what we emit is provably what the chip would
emit given the same event stream.

Word layout: bits[3:0]=seq, bits[6:4]=progress, bits[9:7]=pos,
bits[16:10]=rate, bits[17]=alarm, bits[31:18]=0.

Note on the batch model: python_heist_words() is batch-count-based, not
wall-clock-based; R decays once per BATCH events regardless of real time.  In
real hardware a slow burglar accumulates events slowly so more real time elapses
between batches and R decays further.  The Python mirror faithfully reproduces
the chip's integer operations batch-for-batch.  The "slow vs fast" distinction
is modelled analytically: with LEAK_K=3 and BATCH=4 events/batch R_ss = 32 >
ALARM_THRESH; with 2 events/batch R_ss = 16 < ALARM_THRESH.  The validation
confirms both the alarm and the column-crossing logic independently.

------------------------------------------------------------------------------
Usage:
  dvs_heist_view.py --validate                  # synthetic self-test (numbers)
  dvs_heist_view.py --from-actsim results.mem   # render real chip status words
  dvs_heist_view.py events.csv                  # render (host-computed) from a CSV
  dvs_heist_view.py ... --headless --save heist.png
"""
import argparse
import numpy as np

# --- must match software/dvs_heist/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
LEAK_K = 3
ALARM_THRESH = 24
R_CAP = 127
BIN_CAP = 255
HIST_DECAY_BATCHES = 32
NCOLS = 8
COL_SHIFT = 4
SEQ_MASK = 0xF


def python_heist_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_heist/main.c's ISR.

    x, y, pol, ts are per-event arrays; y, ts, pol are consumed to mirror the
    ABI but ignored by the algorithm (y/ts/pol-invariant).  x drives the
    column histogram.  Processes only complete batches (n - n%BATCH events).

    State cold-start all zeros:
      R=0; bins=[0]*NCOLS; progress=0; hist_decay_ctr=0; seq=0

    Per event (inside each batch of BATCH events):
      col = (int(x[i]) >> COL_SHIFT) & (NCOLS-1)
      if R < R_CAP: R += 1
      if bins[col] < BIN_CAP: bins[col] += 1

    After each batch of BATCH events:
      R -= R >> LEAK_K                          (integer floor shift, always >= 0)
      hist_decay_ctr += 1
      if hist_decay_ctr >= HIST_DECAY_BATCHES:
        hist_decay_ctr = 0; bins[c] >>= 1 for each c

      peak=0, pos=0
      for c in 0..NCOLS-1: if bins[c] > peak: peak=bins[c]; pos=c
      if pos > progress: progress = pos
      alarm = 1 if R > ALARM_THRESH else 0
      rate = R    (already <= R_CAP=127, fits 7 bits)
      seq = (seq + 1) & SEQ_MASK

    Emit one word per batch:
      word = (alarm<<17) | (rate<<10) | (pos<<7) | (progress<<4) | seq

    Returns (words, statuses) where statuses is a list of
    (alarm, pos, progress, rate) tuples appended each batch.
    """
    R = 0
    bins = [0] * NCOLS
    progress = 0
    hist_decay_ctr = 0
    seq = 0
    words = []
    statuses = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Process BATCH events
        for i in range(b, b + BATCH):
            col = (int(x[i]) >> COL_SHIFT) & (NCOLS - 1)
            if R < R_CAP:
                R += 1
            if bins[col] < BIN_CAP:
                bins[col] += 1

        # Per-batch alarm integrator decay: R -= R >> LEAK_K
        R -= R >> LEAK_K

        # Per-batch histogram decay (every HIST_DECAY_BATCHES batches)
        hist_decay_ctr += 1
        if hist_decay_ctr >= HIST_DECAY_BATCHES:
            hist_decay_ctr = 0
            for c in range(NCOLS):
                bins[c] >>= 1

        # Argmax over 8 bins (lowest column wins ties; strict > keeps first max)
        peak = 0
        pos = 0
        for c in range(NCOLS):
            if bins[c] > peak:
                peak = bins[c]
                pos = c

        # Progress ratchet
        if pos > progress:
            progress = pos

        # Alarm flag
        alarm = 1 if R > ALARM_THRESH else 0

        # Rate: R already saturated at R_CAP=127 (fits 7 bits exactly)
        rate = R

        # Sequence counter incremented BEFORE emit:
        # word index i (0-based) carries seq == (i+1) & SEQ_MASK
        seq = (seq + 1) & SEQ_MASK

        # Emit word
        word = (alarm << 17) | (rate << 10) | (pos << 7) | (progress << 4) | seq
        words.append(word)
        statuses.append((alarm, pos, progress, rate))

    return words, statuses


def unpack_status(word):
    """Unpack one heist status word.

    bits[3:0]=seq, bits[6:4]=progress, bits[9:7]=pos,
    bits[16:10]=rate, bits[17]=alarm, bits[31:18]=0.
    """
    seq      =  word        & 0xF
    progress = (word >>  4) & 0x7
    pos      = (word >>  7) & 0x7
    rate     = (word >> 10) & 0x7F
    alarm    = (word >> 17) & 0x1
    return seq, progress, pos, rate, alarm


# ---------------------------------------------------------------------------
# Renderer: museum heist aesthetic on dark backdrop.
# ---------------------------------------------------------------------------

def render_heist(words, save=None, headless=False):
    """Compose one figure: alarm meter + burglar gallery."""
    if not words:
        print("no words to render")
        return

    seq_last, progress_last, pos_last, rate_last, alarm_last = unpack_status(words[-1])

    history_rate = []
    history_alarm = []
    history_pos = []
    for word in words:
        s, prog, pos, rate, alarm = unpack_status(word)
        history_rate.append(rate)
        history_alarm.append(alarm)
        history_pos.append(pos)

    BG      = "#0d0b10"
    TEXT    = "#e8dfc8"
    GOLD    = "#e8b84b"
    RED     = "#d44a4a"
    GREEN   = "#5fd48a"
    STEEL   = "#8a94a6"
    DIM     = "#555566"
    BURGLAR = "#a0c8f0"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last alarm={alarm_last} rate={rate_last} "
              f"pos={pos_last} progress={progress_last}")
        return

    fig = plt.figure(figsize=(12, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(2, 2, width_ratios=[1, 2.2], height_ratios=[1, 1],
                          hspace=0.55, wspace=0.38,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_alarm   = fig.add_subplot(gs[0, 0])
    ax_gallery = fig.add_subplot(gs[1, 0])
    ax_rate    = fig.add_subplot(gs[0, 1])
    ax_pos     = fig.add_subplot(gs[1, 1])

    for ax in (ax_alarm, ax_gallery, ax_rate, ax_pos):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Alarm meter (top-left): filled bar proportional to R ---
    ax_alarm.set_xlim(0, R_CAP)
    ax_alarm.set_ylim(0, 1)
    ax_alarm.set_xticks([0, ALARM_THRESH, R_CAP])
    ax_alarm.set_xticklabels(["0", f"thresh={ALARM_THRESH}", f"{R_CAP}"])
    ax_alarm.set_yticks([])
    ax_alarm.set_title("alarm integrator R", color=TEXT, fontsize=9, pad=4)
    bar_color = RED if alarm_last else GREEN
    ax_alarm.barh(0.5, rate_last, 0.5, color=bar_color, left=0)
    ax_alarm.axvline(ALARM_THRESH, color=RED, linewidth=1.2, linestyle="--", alpha=0.8)
    label_x = min(rate_last + 2, R_CAP - 22)
    ax_alarm.text(label_x, 0.5,
                  f"R={rate_last}  {'ALARM' if alarm_last else 'safe'}",
                  va="center", color=bar_color, fontsize=8, fontweight="bold")

    # --- Gallery (bottom-left): burglar dot in 8-column hall ---
    ax_gallery.set_xlim(-0.5, 7.5)
    ax_gallery.set_ylim(-0.6, 1.5)
    ax_gallery.set_xticks(range(8))
    ax_gallery.set_xticklabels([f"c{c}" for c in range(8)], fontsize=7)
    ax_gallery.set_yticks([])
    ax_gallery.set_title("gallery (columns 0→7)", color=TEXT, fontsize=9, pad=4)

    for c in range(8):
        facecolor = DIM if c > progress_last else "#1a2a1a"
        edge_color = GREEN if c <= progress_last else STEEL
        ax_gallery.add_patch(mpatches.FancyBboxPatch(
            (c - 0.45, 0.1), 0.9, 0.8,
            boxstyle="round,pad=0.02", facecolor=facecolor,
            edgecolor=edge_color, linewidth=0.9, zorder=1))
    ax_gallery.add_patch(mpatches.Circle((pos_last, 0.5), 0.32,
                                         color=BURGLAR, zorder=4))
    ax_gallery.text(pos_last, 0.5, "B", ha="center", va="center",
                    color=BG, fontsize=9, fontweight="bold", zorder=5)
    status_str = "CROSSED!" if progress_last == 7 else f"progress {progress_last}/7"
    ax_gallery.text(3.5, -0.42, status_str,
                    ha="center", va="top", color=GOLD, fontsize=8, fontweight="bold")

    # --- Rate history (top-right) ---
    if history_rate:
        xs = list(range(len(history_rate)))
        ax_rate.fill_between(xs, history_rate, color=RED, alpha=0.25)
        ax_rate.plot(xs, history_rate, color=RED, linewidth=0.8)
        alarm_xs = [xi for xi, a in zip(xs, history_alarm) if a]
        if alarm_xs:
            ax_rate.scatter(alarm_xs, [history_rate[xi] for xi in alarm_xs],
                            color=RED, s=8, zorder=3)
    ax_rate.axhline(ALARM_THRESH, color=RED, linewidth=0.9, linestyle="--", alpha=0.8,
                    label=f"ALARM_THRESH={ALARM_THRESH}")
    ax_rate.set_xlabel("batch index", color=TEXT, fontsize=8)
    ax_rate.set_ylabel("integrator R", color=TEXT, fontsize=8)
    ax_rate.set_title("alarm integrator R over time", color=TEXT, fontsize=9, pad=4)
    ax_rate.set_ylim(-1, R_CAP + 4)
    ax_rate.legend(fontsize=7, framealpha=0.2, labelcolor=TEXT,
                   facecolor=BG, edgecolor="#332d40")

    # --- Burglar position history (bottom-right) ---
    if history_pos:
        xs = list(range(len(history_pos)))
        ax_pos.step(xs, history_pos, where="post", color=BURGLAR, linewidth=1.0)
    ax_pos.axhline(7, color=GOLD, linewidth=0.8, linestyle="--", alpha=0.7,
                   label="col 7 (exit)")
    ax_pos.set_xlabel("batch index", color=TEXT, fontsize=8)
    ax_pos.set_ylabel("argmax column (0..7)", color=TEXT, fontsize=8)
    ax_pos.set_title("burglar position (argmax col)", color=TEXT, fontsize=9, pad=4)
    ax_pos.set_ylim(-0.5, 8.0)
    ax_pos.set_yticks(range(8))
    ax_pos.legend(fontsize=7, framealpha=0.2, labelcolor=TEXT,
                  facecolor=BG, edgecolor="#332d40")

    fig.suptitle('"The Museum Heist"', color=TEXT, fontsize=12,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation: lettered exact-integer checks, zero tolerance.
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks against pre-computed expected values.

    Expected numbers were hand-derived and independently review-verified.
    If the mirror disagrees with any of them the mirror is wrong -- never
    adjust the expectations.

    Leaky-integrator arithmetic (LEAK_K=3, decay = R>>3 per batch):
      At steady state with n events/batch: R_ss + n - floor((R_ss+n)/8) = R_ss
      => floor((R_ss+n)/8) = n => R_ss+n in [8n, 8n+7] => R_ss in [7n, 7n+7].
      Integer simulation converges to the lower end: R_ss = 7*n.
      n=4 (BATCH=4 events/tick): R_ss = 28 > ALARM_THRESH=24 -> alarm fires.
      n=2:                        R_ss = 14 < ALARM_THRESH=24 -> no alarm.
    Test (a) confirms progress==7 and alarm fires (BATCH=4 events/tick, R_ss=28).
    Test (b) confirms analytic R_ss values for n=4 and n=2 (R_ss=7n, integer
    simulation; n=2 R_ss=14 < ALARM_THRESH confirms slow-burglar concept).
    Tests (c)-(g) cover alarm firing, hot-pixel guard, well-formedness,
    y/ts/pol invariance, seq arithmetic, and column mapping.
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) COLUMN CROSSING: 8 columns * DWELL_BATCHES batches each, all BATCH
    # events at target column per tick.  Progress must reach 7.
    # Alarm expected to fire (R_ss=32>ALARM_THRESH=24 with BATCH=4 events/tick).
    # DWELL_BATCHES=80 > 2*HIST_DECAY_BATCHES=64 guarantees the target column's
    # bin dominates before we move on: after DWELL batches at col c, bin[c] has
    # accumulated 80*4=320 events (capped at BIN_CAP=255); the previous column's
    # bin decays twice (halved at 32 and 64 batches into the new column's phase).
    # ------------------------------------------------------------------
    DWELL = 80

    xs_a, ys_a, ts_a, pol_a = [], [], [], []
    for col in range(NCOLS):
        x_val = col * 16 + 4    # x >> 4 == col (centre pixel of bin)
        for _ in range(DWELL):
            for _ in range(BATCH):
                xs_a.append(x_val)
                ys_a.append(56)
                ts_a.append(0)
                pol_a.append(0)
    xs_a  = np.array(xs_a,  dtype=np.int64)
    ys_a  = np.array(ys_a,  dtype=np.int64)
    ts_a  = np.array(ts_a,  dtype=np.int64)
    pol_a = np.array(pol_a, dtype=np.int64)

    words_a, statuses_a = python_heist_words(xs_a, ys_a, ts_a, pol_a)

    final_progress_a = statuses_a[-1][2] if statuses_a else -1
    alarm_fires_a = any(s[0] for s in statuses_a)
    a_ok = (final_progress_a == 7) and alarm_fires_a
    print(f"  (a) COLUMN CROSSING: {len(words_a)} words; "
          f"final progress={final_progress_a} (want 7); "
          f"alarm fires={alarm_fires_a} "
          f"(expected True: R_ss=28>ALARM_THRESH={ALARM_THRESH} with BATCH={BATCH} "
          f"events/tick, analytic R_ss=7*BATCH=28) -> {'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) LEAKY-INTEGRATOR ANALYTIC CHECK
    # Simulate 200 batches to convergence for n=4 and n=2 events/batch.
    # Expected: R_ss(n=4) near 32 (analytic 8*4=32, integer floor +-1 ok);
    #           R_ss(n=2) near 16 (analytic 8*2=16) and < ALARM_THRESH=24.
    # Confirms that the "slow burglar" concept is sound: n=2 keeps R below alarm.
    # ------------------------------------------------------------------
    R4 = 0
    for _ in range(500):
        R4 = min(R4 + BATCH, R_CAP)
        R4 -= R4 >> LEAK_K
    R2 = 0
    for _ in range(500):
        R2 = min(R2 + 2, R_CAP)
        R2 -= R2 >> LEAK_K
    # Analytic: R_ss = 7*n (from floor((R_ss+n)/8)=n => R_ss in [7n, 7n+7]).
    # Simulation converges to the lower bound 7*n.
    # n=4: R_ss = 28 > ALARM_THRESH -> alarm fires.
    # n=2: R_ss = 14 < ALARM_THRESH -> no alarm (slow-burglar concept sound).
    b_ok = (R4 == 7 * BATCH) and (R2 == 7 * 2) and (R2 < ALARM_THRESH)
    print(f"  (b) LEAKY-INTEGRATOR ANALYTIC: R_ss(n=4)={R4} "
          f"(want {7*BATCH}=7*BATCH, analytic R_ss=7n); R_ss(n=2)={R2} "
          f"(want {7*2}=7*2 and < ALARM_THRESH={ALARM_THRESH}) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) FAST BURST ALARM: 40 batches of BATCH events all at col 0.
    # R_ss=32 > ALARM_THRESH; alarm must fire within the first ~10 batches.
    # ------------------------------------------------------------------
    N_BURST = 40
    xs_c  = np.zeros(N_BURST * BATCH, dtype=np.int64)
    ys_c  = np.zeros(N_BURST * BATCH, dtype=np.int64)
    ts_c  = np.zeros(N_BURST * BATCH, dtype=np.int64)
    pol_c = np.zeros(N_BURST * BATCH, dtype=np.int64)

    words_c, statuses_c = python_heist_words(xs_c, ys_c, ts_c, pol_c)

    any_alarm_c  = any(s[0] == 1 for s in statuses_c)
    max_rate_c   = max(s[3] for s in statuses_c) if statuses_c else 0
    c_ok = any_alarm_c and (max_rate_c > ALARM_THRESH)
    print(f"  (c) FAST BURST ALARM: {len(words_c)} words; "
          f"max_rate={max_rate_c} (want > {ALARM_THRESH}); "
          f"any alarm={any_alarm_c} (want True) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) HOT PIXEL COLUMN GUARD: all events at x=5 (col 0).
    # Argmax must be 0 throughout; progress must stay 0.
    # ------------------------------------------------------------------
    N_HOT = len(xs_a)
    xs_d  = np.full(N_HOT, 5, dtype=np.int64)   # x=5 -> (5>>4)&7 = 0
    ys_d  = np.full(N_HOT, 56, dtype=np.int64)
    ts_d  = np.zeros(N_HOT, dtype=np.int64)
    pol_d = np.zeros(N_HOT, dtype=np.int64)

    words_d, statuses_d = python_heist_words(xs_d, ys_d, ts_d, pol_d)

    all_progress0_d = all(s[2] == 0 for s in statuses_d)
    all_pos0_d      = all(s[1] == 0 for s in statuses_d)
    d_ok = all_progress0_d and all_pos0_d
    print(f"  (d) HOT PIXEL COLUMN GUARD: {len(words_d)} words; "
          f"all progress==0={all_progress0_d}, all pos==0={all_pos0_d} "
          f"(hot pixel at x=5 col=0 never advances) -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) WELL-FORMEDNESS over ALL words from (a)-(d)
    # alarm in {0,1}; pos in 0..7; progress in 0..7; rate in 0..127;
    # seq in 0..15; bits[31:18]==0; word < 2^32.
    # ------------------------------------------------------------------
    all_words_e = words_a + words_c + list(words_d)
    bad_e = []
    for word in all_words_e:
        seq_, progress_, pos_, rate_, alarm_ = unpack_status(word)
        if alarm_ not in (0, 1):
            bad_e.append(f"alarm={alarm_}")
        if pos_ > 7:
            bad_e.append(f"pos={pos_}")
        if progress_ > 7:
            bad_e.append(f"progress={progress_}")
        if rate_ > 127:
            bad_e.append(f"rate={rate_}")
        if seq_ > 15:
            bad_e.append(f"seq={seq_}")
        if (word >> 18) != 0:
            bad_e.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_e.append(f"word>=2^32: 0x{word:08x}")
        if bad_e:
            break
    e_ok = len(bad_e) == 0
    print(f"  (e) WELL-FORMEDNESS: {len(all_words_e)} total words; "
          f"alarm in {{0,1}}, pos/progress in 0..7, rate in 0..127, "
          f"seq in 0..15, upper bits=0, word<2^32 -> "
          f"{'OK' if e_ok else 'FAIL: ' + '; '.join(bad_e[:5])}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) Y/TS/POL INVARIANCE
    # Take (a)'s stream; scramble y, ts, pol while keeping x;
    # words must be element-for-element identical to (a)'s.
    # ------------------------------------------------------------------
    ya_scr   = (ys_a * 23 + 5) % 112
    tsa_scr  = (ts_a * 37 + 11) % 65536
    pola_scr = 1 - pol_a
    words_f, _ = python_heist_words(xs_a, ya_scr, tsa_scr, pola_scr)

    f_ok = (words_f == words_a)
    print(f"  (f) Y/TS/POL INVARIANCE: words identical element-for-element to (a) -> "
          f"{'OK' if f_ok else 'FAIL'}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) SEQ ARITHMETIC
    # For all words from (a): word i (0-based) carries seq == (i+1) & SEQ_MASK.
    # ------------------------------------------------------------------
    bad_g = []
    for i, word in enumerate(words_a):
        expected_seq = (i + 1) & SEQ_MASK
        actual_seq   = word & SEQ_MASK
        if actual_seq != expected_seq:
            bad_g.append(f"i={i} got={actual_seq} want={expected_seq}")
            if len(bad_g) >= 3:
                break
    g_ok = len(bad_g) == 0
    print(f"  (g) SEQ ARITHMETIC: {len(words_a)} words; "
          f"every word[i] has seq==(i+1)&0xF -> "
          f"{'OK' if g_ok else 'FAIL: ' + '; '.join(bad_g[:3])}")
    ok = ok and g_ok

    # ------------------------------------------------------------------
    # (h) COLUMN MAPPING EXHAUSTIVE
    # For every x in 0..125: col = (x>>4)&7 in 0..7; non-decreasing; spot checks.
    # ------------------------------------------------------------------
    col_map = [(x >> COL_SHIFT) & (NCOLS - 1) for x in range(SX)]
    range_ok_h = all(0 <= c <= 7 for c in col_map)
    mono_ok_h  = all(col_map[i] <= col_map[i + 1] for i in range(SX - 1))
    spot_ok_h  = (col_map[0] == 0 and col_map[15] == 0 and
                  col_map[16] == 1 and col_map[111] == 6 and
                  col_map[112] == 7 and col_map[125] == 7)
    h_ok = range_ok_h and mono_ok_h and spot_ok_h
    print(f"  (h) COLUMN MAPPING EXHAUSTIVE: range 0..7={range_ok_h}, "
          f"non-decreasing={mono_ok_h}, spot checks={spot_ok_h} -> "
          f"{'OK' if h_ok else 'FAIL'}")
    ok = ok and h_ok

    print()
    print("VALIDATION:", "PASS -- column crossing proven; leaky-integrator analytic "
          "confirmed; fast burst alarm fires; hot-pixel column guard proven; "
          "well-formedness checked; y/ts/pol invariance proven; "
          "seq arithmetic exact; column mapping exhaustive"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader (from dvs_vital_view.py pattern)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col="le"):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for the timestamp field (default 'le').
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
        ts = np.array([int(row[idx[ts_col]]) & 0xFFFF for row in rows], dtype=np.int64)
    else:
        ts = np.zeros(len(x), dtype=np.int64)
    return x, y, ts, pol


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: column crossing, leaky-integrator analytic, "
                         "fast burst alarm, hot-pixel column guard, well-formedness, "
                         "y/ts/pol invariance, seq arithmetic, column mapping")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the heist PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing heist words in Python "
              f"(bit-faithful mirror of firmware).")
        words, statuses = python_heist_words(x, y, ts, pol)
        if words:
            seq_l, prog_l, pos_l, rate_l, alarm_l = unpack_status(words[-1])
            print(f"final alarm={alarm_l} rate={rate_l} pos={pos_l} "
                  f"progress={prog_l}/7 ({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_heist(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
