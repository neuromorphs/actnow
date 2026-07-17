#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_vital/main.c
("The Vitalometer" -- burst-timing statistics decide whether the thing in
front of the camera is ALIVE (irregular/jittery rhythm) or a MECHANISM
(metronomic periodicity).  Every WINDOW_BATCHES batches it analyses IBI
(inter-burst-interval) histogram statistics: peak bin, spread (number of
bins above floor), and total IBI count.  Verdicts: 0=DORMANT (too few
IBIs), 1=MECHANISM (narrow IBI spread, periodic), 2=ALIVE (wide IBI
spread, irregular), 3=LIMINAL (intermediate spread).  Position-invariant:
x/y are consumed by the ISR ABI but ignored by the algorithm.

python_vital_words() below is a bit-faithful port of the firmware's integer
logic (same burst tracker, same IBI histogram, same log-bin mapper, same
word packing) so what we emit is provably what the chip would emit given the
same event stream.

Word layout: bits[4:0]=pbin, bits[10:5]=spread, bits[18:11]=total,
bits[20:19]=verdict, bits[24:21]=wseq, bits[31:25]=0.

------------------------------------------------------------------------------
Usage:
  dvs_vital_view.py --validate                  # synthetic self-test (numbers)
  dvs_vital_view.py --from-actsim results.mem   # render real chip status words
  dvs_vital_view.py events.csv --ts-col le      # render (host-computed) from a CSV
  dvs_vital_view.py ... --headless --save vital.png
"""
import argparse
import numpy as np

# --- must match software/dvs_vital/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
TS_MASK = 0xFFFF
GAP_MIN = 48
BURST_MIN_LEN = 4
WINDOW_BATCHES = 256
MIN_IBIS = 6
SPREAD_MECH = 2
SPREAD_ALIVE = 5
HIST_CAP = 255
TOTAL_CAP = 255
NBINS = 32
WSEQ_MASK = 0xF
VERDICT_NAMES = ["DORMANT", "MECHANISM", "ALIVE", "LIMINAL"]


def log2bin32(v):
    """Map IBI value v (1..65535) to half-octave bin 0..31.

    m = floor(log2(v)); sub = bit below MSB.  bin = (m<<1) | sub.
    """
    m = 0
    t = v
    while t >= 2:
        t >>= 1
        m += 1
    sub = ((v >> (m - 1)) & 1) if m >= 1 else 0
    return (m << 1) | sub


def python_vital_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_vital/main.c's ISR.

    x, y, pol are per-event arrays (x/y/pol accepted to mirror the ABI but
    ignored by the algorithm -- position-invariant).  ts is the per-event
    timestamp array.  Processes only complete batches (n - n%BATCH events).

    State cold-start all zeros:
      hist=[0]*NBINS; last_ts=0; cur_onset_ts=0; cur_len=0;
      prev_onset_ts=0; have_prev=0; ibi_total=0;
      lat_pbin=lat_spread=lat_total=lat_verdict=0; batch_in_window=0; wseq=0

    Per event in order:
      t = int(ts[i]) & TS_MASK
      dt = (t - last_ts) & TS_MASK
      last_ts = t
      if dt >= GAP_MIN: start new burst (onset, len=1)
      else: grow burst; at BURST_MIN_LEN confirmation -> IBI bookkeeping

    After each batch of BATCH events (latch BEFORE emit):
      batch_in_window += 1
      if batch_in_window >= WINDOW_BATCHES: analyse window, latch, clear hist

    Emit one word per batch:
      word = (wseq<<21) | (verdict<<19) | (total<<11) | (spread<<5) | pbin

    Returns (words, latches) where latches is a list of
    (verdict, spread, pbin, total) tuples appended at every window latch.
    NOTE: the burst tracker (cur_onset_ts, cur_len, prev_onset_ts, have_prev)
    persists across window boundaries; only hist and ibi_total are cleared.
    """
    hist = [0] * NBINS
    last_ts = 0
    cur_onset_ts = 0
    cur_len = 0
    prev_onset_ts = 0
    have_prev = 0
    ibi_total = 0
    lat_pbin = 0
    lat_spread = 0
    lat_total = 0
    lat_verdict = 0
    batch_in_window = 0
    wseq = 0
    words = []
    latches = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Process BATCH events: update burst tracker
        for i in range(b, b + BATCH):
            t = int(ts[i]) & TS_MASK
            dt = (t - last_ts) & TS_MASK
            last_ts = t
            if dt >= GAP_MIN:
                cur_onset_ts = t
                cur_len = 1
            else:
                if cur_len < 255:
                    cur_len += 1
                if cur_len == BURST_MIN_LEN:
                    if have_prev:
                        ibi = (cur_onset_ts - prev_onset_ts) & TS_MASK
                        if ibi != 0:
                            bk = log2bin32(ibi)
                            if hist[bk] < HIST_CAP:
                                hist[bk] += 1
                            if ibi_total < TOTAL_CAP:
                                ibi_total += 1
                    prev_onset_ts = cur_onset_ts
                    have_prev = 1

        # After each batch: check window boundary (latch BEFORE emit)
        batch_in_window += 1
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0
            peak = 0
            pbin = 0
            for bk in range(NBINS):
                if hist[bk] > peak:
                    peak = hist[bk]
                    pbin = bk
            floor_ = peak >> 3
            spread = sum(1 for bk in range(NBINS) if hist[bk] > floor_)
            if ibi_total < MIN_IBIS:
                verdict = 0
            elif spread <= SPREAD_MECH:
                verdict = 1
            elif spread >= SPREAD_ALIVE:
                verdict = 2
            else:
                verdict = 3
            lat_pbin = pbin
            lat_spread = spread
            lat_total = ibi_total
            lat_verdict = verdict
            latches.append((verdict, spread, pbin, ibi_total))
            hist = [0] * NBINS
            ibi_total = 0
            wseq = (wseq + 1) & WSEQ_MASK

        # Emit one word per batch; latch fields already updated above
        word = (wseq << 21) | (lat_verdict << 19) | (lat_total << 11) \
             | (lat_spread << 5) | lat_pbin
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one vital status word.

    bits[4:0]=pbin, bits[10:5]=spread, bits[18:11]=total,
    bits[20:19]=verdict, bits[24:21]=wseq, bits[31:25]=0.
    """
    pbin    =  word        & 0x1F
    spread  = (word >>  5) & 0x3F
    total   = (word >> 11) & 0xFF
    verdict = (word >> 19) & 0x3
    wseq    = (word >> 21) & 0xF
    return pbin, spread, total, verdict, wseq


# ---------------------------------------------------------------------------
# Synthetic stream builder
# ---------------------------------------------------------------------------

def build_burst_stream(periods, n_bursts, burst_len=8, intra_dt=2, t0=1000):
    """Build a synthetic burst stream with known inter-burst intervals.

    Onset times: t_onset[0]=t0; t_onset[k]=t_onset[k-1]+periods[(k-1)%len(periods)].
    Each burst k contributes burst_len events at ts t_onset[k] + j*intra_dt
    for j in 0..burst_len-1 (full-precision ints; the mirror applies TS_MASK).
    x=63, y=56, pol=j%2 throughout (irrelevant to the algorithm).

    Returns int64 numpy arrays (x, y, ts, pol).
    """
    t_onset = [t0]
    for k in range(1, n_bursts):
        t_onset.append(t_onset[k - 1] + periods[(k - 1) % len(periods)])
    xs = []
    ys = []
    tss = []
    pols = []
    for k in range(n_bursts):
        for j in range(burst_len):
            xs.append(63)
            ys.append(56)
            tss.append(t_onset[k] + j * intra_dt)
            pols.append(j % 2)
    return (np.array(xs,   dtype=np.int64),
            np.array(ys,   dtype=np.int64),
            np.array(tss,  dtype=np.int64),
            np.array(pols, dtype=np.int64))


# ---------------------------------------------------------------------------
# Renderer: séance-gauge aesthetic on dark backdrop.
# ---------------------------------------------------------------------------

def render_vital(words, save=None, headless=False):
    """Compose one figure: verdict lamp, spread history, dominant-bin history."""
    if not words:
        print("no words to render")
        return

    pbin_last, spread_last, total_last, verdict_last, _ = unpack_status(words[-1])

    # Collect one sample per wseq change
    history_spread = []
    history_pbin = []
    prev_wseq = None
    for word in words:
        pbin_, spread_, total_, verdict_, wseq_ = unpack_status(word)
        if wseq_ != prev_wseq:
            history_spread.append(spread_)
            history_pbin.append(pbin_)
            prev_wseq = wseq_

    BG     = "#0d0b10"
    TEXT   = "#e8dfc8"
    GOLD   = "#e8b84b"
    INDIGO = "#5a5fd4"
    GREEN  = "#5fd48a"
    STEEL  = "#8a94a6"
    DIM    = "#555566"

    VERDICT_COLORS = [DIM, STEEL, GREEN, GOLD]

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last verdict={VERDICT_NAMES[verdict_last]} spread={spread_last} "
              f"total={total_last} pbin={pbin_last}")
        if history_spread:
            print("per-window spread history:", history_spread)
            print("per-window pbin history:", history_pbin)
        return

    fig = plt.figure(figsize=(10, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.6], wspace=0.38,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_left  = fig.add_subplot(gs[0])
    ax_right = fig.add_subplot(gs[1])

    # Split right column: spread history top, pbin history bottom
    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_spread = fig.add_subplot(gs_right[0])
    ax_pbin   = fig.add_subplot(gs_right[1])
    ax_right.remove()

    for ax in (ax_left, ax_spread, ax_pbin):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left panel: verdict lamp ---
    ax_left.set_xlim(-1.4, 1.4)
    ax_left.set_ylim(-1.8, 1.4)
    ax_left.set_aspect("equal")
    ax_left.set_xticks([])
    ax_left.set_yticks([])
    ax_left.set_title("verdict lamp", color=TEXT, fontsize=9, pad=4)

    lamp_color = VERDICT_COLORS[verdict_last]
    lamp = mpatches.Circle((0, 0.2), 0.85, color=lamp_color, zorder=2)
    ax_left.add_patch(lamp)
    ring = mpatches.Circle((0, 0.2), 0.85, fill=False,
                            edgecolor=TEXT, linewidth=1.5, zorder=3)
    ax_left.add_patch(ring)

    ax_left.text(0, -0.90, VERDICT_NAMES[verdict_last],
                 ha="center", va="center", color=TEXT,
                 fontsize=12, fontweight="bold")
    ax_left.text(0, -1.25, f"spread={spread_last} bins,  total={total_last} IBIs",
                 ha="center", va="center", color=TEXT, fontsize=8)

    # --- Right top: per-window spread history ---
    ax_spread.axhline(SPREAD_MECH, color=STEEL, linewidth=0.8, linestyle="--", alpha=0.7,
                      label=f"SPREAD_MECH={SPREAD_MECH}")
    ax_spread.axhline(SPREAD_ALIVE, color=GREEN, linewidth=0.8, linestyle="--", alpha=0.7,
                      label=f"SPREAD_ALIVE={SPREAD_ALIVE}")
    if history_spread:
        xs = list(range(len(history_spread)))
        ax_spread.step(xs, history_spread, where="post", color=TEXT, linewidth=1.0)
    ax_spread.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_spread.set_ylabel("spread (bins above floor)", color=TEXT, fontsize=8)
    ax_spread.set_title("per-window IBI spread", color=TEXT, fontsize=9, pad=4)
    ax_spread.set_ylim(-0.5, NBINS + 0.5)
    legend = ax_spread.legend(fontsize=7, framealpha=0.2,
                              labelcolor=TEXT, facecolor=BG, edgecolor="#332d40")

    # --- Right bottom: per-window dominant-bin (pbin) history ---
    if history_pbin:
        xs = list(range(len(history_pbin)))
        ax_pbin.scatter(xs, history_pbin, color=GOLD, s=15, zorder=2)
        ax_pbin.step(xs, history_pbin, where="post", color=GOLD, linewidth=0.7, alpha=0.5)
    ax_pbin.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_pbin.set_ylabel("dominant IBI log-bin (half-octaves)", color=TEXT, fontsize=8)
    ax_pbin.set_title("per-window dominant bin", color=TEXT, fontsize=9, pad=4)
    ax_pbin.set_ylim(-0.5, 31.5)

    fig.suptitle('"The Vitalometer"', color=TEXT, fontsize=12,
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
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) METRONOME EXACT
    # build_burst_stream([1000], 260) -> 2080 events, 520 words, 2 latches.
    # Latch 1: (verdict=1, spread=1, pbin=19, total=127)
    # Latch 2: (1, 1, 19, 128)
    # Words 0..254: wseq=0, all fields zero (pre-first-latch zeros).
    # Words 255..510: wseq=1, latch-1 fields.
    # Words 511..519: wseq=2, latch-2 fields.
    # ------------------------------------------------------------------
    x_a, y_a, ts_a, pol_a = build_burst_stream([1000], 260)
    words_a, latches_a = python_vital_words(x_a, y_a, ts_a, pol_a)

    n_ev_a  = len(x_a)
    n_wd_a  = len(words_a)
    n_lat_a = len(latches_a)
    lat1_a  = latches_a[0] if len(latches_a) > 0 else None
    lat2_a  = latches_a[1] if len(latches_a) > 1 else None

    pre_zero_a = all(
        (words_a[i] >> 19) & 3 == 0 and
        (words_a[i] >> 21) & 0xF == 0 and
        (words_a[i] >> 11) & 0xFF == 0 and
        (words_a[i] >>  5) & 0x3F == 0 and
        words_a[i] & 0x1F == 0
        for i in range(255)
    )
    mid_wseq_a = all((words_a[i] >> 21) & 0xF == 1 for i in range(255, 511))
    end_wseq_a = all((words_a[i] >> 21) & 0xF == 2 for i in range(511, 520))

    a_ok = (
        n_ev_a == 2080
        and n_wd_a == 520
        and n_lat_a == 2
        and lat1_a == (1, 1, 19, 127)
        and lat2_a == (1, 1, 19, 128)
        and pre_zero_a
        and mid_wseq_a
        and end_wseq_a
    )
    print(f"  (a) METRONOME EXACT: events={n_ev_a} (want 2080), "
          f"words={n_wd_a} (want 520), latches={n_lat_a} (want 2), "
          f"latch1={lat1_a} (want (1,1,19,127)), "
          f"latch2={lat2_a} (want (1,1,19,128)), "
          f"pre-zero={pre_zero_a}, mid-wseq1={mid_wseq_a}, end-wseq2={end_wseq_a} -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) ALIVE JITTER-CYCLE EXACT
    # build_burst_stream([600,900,1400,2100,3200,4800], 260) -> 2 latches.
    # Latch 1: (2, 6, 18, 127); latch 2: (2, 6, 19, 128).
    # (Tie in window 2 between bins 19 and 20 -> lowest bin wins -> pbin=19.)
    # ------------------------------------------------------------------
    periods_b = [600, 900, 1400, 2100, 3200, 4800]
    x_b, y_b, ts_b, pol_b = build_burst_stream(periods_b, 260)
    words_b, latches_b = python_vital_words(x_b, y_b, ts_b, pol_b)

    n_lat_b = len(latches_b)
    lat1_b  = latches_b[0] if len(latches_b) > 0 else None
    lat2_b  = latches_b[1] if len(latches_b) > 1 else None

    b_ok = (
        n_lat_b == 2
        and lat1_b == (2, 6, 18, 127)
        and lat2_b == (2, 6, 19, 128)
    )
    print(f"  (b) ALIVE JITTER-CYCLE EXACT: latches={n_lat_b} (want 2), "
          f"latch1={lat1_b} (want (2,6,18,127)), "
          f"latch2={lat2_b} (want (2,6,19,128)) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) DENSE SPARKLE GUARD
    # 2080 events, ts = 1000 + 8*i (dt=8 < GAP_MIN forever after first gap).
    # No burst of length >= BURST_MIN_LEN forms that also has a previous onset
    # with ibi!=0.  Both latches == (0,0,0,0); every word verdict==0, total==0.
    # ------------------------------------------------------------------
    x_c  = np.full(2080, 63, dtype=np.int64)
    y_c  = np.full(2080, 56, dtype=np.int64)
    ts_c = np.array([1000 + 8 * i for i in range(2080)], dtype=np.int64)
    pol_c = np.zeros(2080, dtype=np.int64)
    words_c, latches_c = python_vital_words(x_c, y_c, ts_c, pol_c)

    latches_c_ok   = latches_c == [(0, 0, 0, 0), (0, 0, 0, 0)]
    all_v0_c       = all((w >> 19) & 3 == 0 for w in words_c)
    all_total0_c   = all((w >> 11) & 0xFF == 0 for w in words_c)

    c_ok = latches_c_ok and all_v0_c and all_total0_c
    print(f"  (c) DENSE SPARKLE GUARD: latches={latches_c} (want [(0,0,0,0),(0,0,0,0)]), "
          f"all verdict==0={all_v0_c}, all total==0={all_total0_c} -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) SINGLETON GUARD
    # 2080 events, ts = 1000 + 5000*i (every event opens a fresh 1-event burst,
    # never reaches BURST_MIN_LEN).  Both latches == (0,0,0,0).
    # ------------------------------------------------------------------
    ts_d  = np.array([1000 + 5000 * i for i in range(2080)], dtype=np.int64)
    words_d, latches_d = python_vital_words(x_c, y_c, ts_d, pol_c)

    latches_d_ok  = latches_d == [(0, 0, 0, 0), (0, 0, 0, 0)]
    all_v0_d      = all((w >> 19) & 3 == 0 for w in words_d)
    all_total0_d  = all((w >> 11) & 0xFF == 0 for w in words_d)

    d_ok = latches_d_ok and all_v0_d and all_total0_d
    print(f"  (d) SINGLETON GUARD: latches={latches_d} (want [(0,0,0,0),(0,0,0,0)]), "
          f"all verdict==0={all_v0_d}, all total==0={all_total0_d} -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) POSITION INVARIANCE
    # Rerun (b)'s ts/pol with scrambled x2=(x*37+11)%126, y2=(y*23+5)%112,
    # pol2=1-pol -> words must be identical element-for-element to (b)'s.
    # ------------------------------------------------------------------
    x_e   = (x_b * 37 + 11) % 126
    y_e   = (y_b * 23 + 5)  % 112
    pol_e = 1 - pol_b
    words_e, _ = python_vital_words(x_e, y_e, ts_b, pol_e)

    e_ok = (words_e == words_b)
    print(f"  (e) POSITION INVARIANCE: words identical element-for-element to (b) -> "
          f"{'OK' if e_ok else 'FAIL'}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) LOG-BIN EXHAUSTIVE
    # For every v in 1..65535: 0 <= log2bin32(v) <= 31; monotone non-decreasing;
    # spot-assert six hand values and log2bin32(1000)==19.
    # ------------------------------------------------------------------
    bins_f = [log2bin32(v) for v in range(1, 65536)]

    range_ok_f  = all(0 <= b <= 31 for b in bins_f)
    mono_ok_f   = all(bins_f[i] <= bins_f[i + 1] for i in range(len(bins_f) - 1))

    # Verify against reference formula
    def ref_log2bin(v):
        bl = v.bit_length()
        m = bl - 1
        sub = ((v >> (bl - 2)) & 1) if bl >= 2 else 0
        return (m << 1) | sub

    formula_ok_f = all(log2bin32(v) == ref_log2bin(v) for v in range(1, 65536))

    spot_vals_f = {1000: 19, 600: 18, 900: 19, 1400: 20, 2100: 22, 3200: 23, 4800: 24}
    spot_ok_f = all(log2bin32(v) == exp for v, exp in spot_vals_f.items())

    f_ok = range_ok_f and mono_ok_f and formula_ok_f and spot_ok_f
    print(f"  (f) LOG-BIN EXHAUSTIVE: range 0..31={range_ok_f}, "
          f"monotone={mono_ok_f}, formula match={formula_ok_f}, "
          f"spot values correct={spot_ok_f} -> "
          f"{'OK' if f_ok else 'FAIL'}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) WSEQ ARITHMETIC
    # Concatenate (a) and (c) into ONE stream; every word i has
    # wseq == ((i+1)//WINDOW_BATCHES) & WSEQ_MASK.
    # ------------------------------------------------------------------
    x_g   = np.concatenate([x_a,   x_c])
    y_g   = np.concatenate([y_a,   y_c])
    ts_g  = np.concatenate([ts_a,  ts_c])
    pol_g = np.concatenate([pol_a, pol_c])
    words_g, _ = python_vital_words(x_g, y_g, ts_g, pol_g)

    bad_g = []
    for i, word in enumerate(words_g):
        expected_wseq = ((i + 1) // WINDOW_BATCHES) & WSEQ_MASK
        actual_wseq   = (word >> 21) & 0xF
        if actual_wseq != expected_wseq:
            bad_g.append(f"i={i} got={actual_wseq} want={expected_wseq}")
            if len(bad_g) >= 3:
                break
    g_ok = len(bad_g) == 0
    print(f"  (g) WSEQ ARITHMETIC: {len(words_g)} words; "
          f"every word[i] has wseq==((i+1)//{WINDOW_BATCHES})&0xF -> "
          f"{'OK' if g_ok else 'FAIL: ' + '; '.join(bad_g[:3])}")
    ok = ok and g_ok

    # ------------------------------------------------------------------
    # (h) WELL-FORMEDNESS over ALL words from (a)-(d)
    # pbin<=31, spread<=32, total<=255, verdict<=3, wseq<=15,
    # (word>>25)==0, word < 2**32.
    # ------------------------------------------------------------------
    all_words_h = words_a + words_b + list(words_c) + list(words_d)
    bad_h = []
    for word in all_words_h:
        pbin_, spread_, total_, verdict_, wseq_ = unpack_status(word)
        if pbin_ > 31:
            bad_h.append(f"pbin={pbin_}")
        if spread_ > 32:
            bad_h.append(f"spread={spread_}")
        if total_ > 255:
            bad_h.append(f"total={total_}")
        if verdict_ > 3:
            bad_h.append(f"verdict={verdict_}")
        if wseq_ > 15:
            bad_h.append(f"wseq={wseq_}")
        if (word >> 25) != 0:
            bad_h.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_h.append(f"word>=2^32: 0x{word:08x}")
        if bad_h:
            break
    h_ok = len(bad_h) == 0
    print(f"  (h) WELL-FORMEDNESS: {len(all_words_h)} total words; "
          f"pbin<=31, spread<=32, total<=255, verdict<=3, "
          f"wseq<=15, upper bits=0, word<2^32 -> "
          f"{'OK' if h_ok else 'FAIL: ' + '; '.join(bad_h[:5])}")
    ok = ok and h_ok

    print()
    print("VALIDATION:", "PASS -- metronome exact; alive jitter-cycle exact; "
          "dense sparkle guard proven; singleton guard proven; "
          "position invariance proven; log-bin exhaustive; "
          "wseq arithmetic exact; word fields well-formed"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader (from dvs_heartbeats_view.py pattern)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for the timestamp field (default 'le').
    NOTE: recorded captures carry a wrapped coarse counter (not real
    microseconds) so verdicts on captures are qualitative -- --validate
    builds its own synthetic timestamps and is the deterministic check.
    If ts_col is absent from the header, ts is zeroed.
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
        ts = np.array([int(row[idx[ts_col]]) & TS_MASK for row in rows], dtype=np.int64)
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
                    help="synthetic self-test: metronome exact, alive jitter-cycle exact, "
                         "dense sparkle guard, singleton guard, position invariance, "
                         "log-bin exhaustive, wseq arithmetic, well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le -- NOTE: le is a "
                         "wrapped coarse counter, not real microseconds; verdicts on "
                         "recorded captures are qualitative; --validate builds its own "
                         "synthetic timestamps and is the deterministic check)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the vital PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing vital words in Python "
              f"(bit-faithful mirror of firmware).")
        words, _ = python_vital_words(x, y, ts, pol)
        if words:
            pbin_last, spread_last, total_last, verdict_last, _ = unpack_status(words[-1])
            print(f"final verdict={VERDICT_NAMES[verdict_last]}, "
                  f"spread={spread_last} bins, total={total_last} IBIs, "
                  f"pbin={pbin_last} ({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_vital(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
