#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_shibboleth/main.c
("Shibboleth" -- identify a light by its PWM accent).  Most LED torches and
phone flashlights are PWM-dimmed at some kHz rate, invisible to eyes and frame
cameras but obvious to a DVS: it fires events at the switching frequency.  The
chip finds the most-active pixel region, builds an inter-event-interval (IEI)
log-bin histogram over that region, detects whether a dominant peak exists
(noise guard), and reports the peak bin as the "accent" (PWM period estimate).

python_shibboleth_words() is a bit-faithful port of the firmware's integer
logic (same coarse cell grid, same IEI histogram, same log-bin mapper, same
noise guard, same word packing).

Word layout:
  bits[ 4: 0] = pbin      (0..31, dominant IEI log-bin; 0 when valid=0)
  bits[ 5]    = valid      (1=PWM accent detected; 0=no accent / noisy)
  bits[13: 6] = iei_total  (0..255, IEI count this window, saturated)
  bits[20:14] = hot_cidx7  (lat_hot_cidx >> 1, 0..111; host * 2 to recover)
  bits[23:21] = wseq       (3-bit window sequence counter, wraps mod 8)
  bits[31:24] = 0

HISTOGRAM CONSISTENCY: hist[] is only incremented when iei_total < TOTAL_CAP,
so hist[] and iei_total count exactly the same set of IEIs.  This makes the
peak-dominance noise guard (valid when peak >= total >> CONF_SHIFT) have a
consistent denominator: for broadband noise spread across N > 8 bins, each bin
gets ~total/N counts and peak ~ total/N < total/8 -> valid=0.  A sharp PWM
source concentrates all IEIs in one bin -> peak = total -> valid=1.

Frequency label LUT: bin b covers IEI tick range [2^(b/2), 2^((b+2)/2)).
BIN_FREQ_HZ[b] (1 MHz tick rate) = 1e6 / (2^((b+1)/2)) Hz.  Adjust for the
actual chip tick rate.

------------------------------------------------------------------------------
Usage:
  dvs_shibboleth_view.py --validate                  # synthetic self-test
  dvs_shibboleth_view.py --from-actsim results.mem   # render real chip words
  dvs_shibboleth_view.py events.csv --ts-col le      # host-computed from CSV
  dvs_shibboleth_view.py ... --headless --save shibboleth.png
"""
import argparse
import math
import numpy as np

# --- must match software/dvs_shibboleth/main.c exactly ---
SX, SY       = 126, 112
BATCH        = 4
TS_MASK      = 0xFFFF
N_CX         = 16
N_CY         = 14
N_CELLS      = N_CX * N_CY    # 224
X_CELL_SHIFT = 3               # col = x >> 3
Y_CELL_SHIFT = 3               # row = y >> 3
WINDOW_BATCHES = 256
MIN_IEIS     = 8
CONF_SHIFT   = 3               # valid=1 only if peak >= (total >> CONF_SHIFT)
IEI_MAX      = 32768
HIST_CAP     = 255
TOTAL_CAP    = 255
NBINS        = 32
WSEQ_MASK    = 0x7

# Frequency LUT (1 MHz tick rate): bin b -> Hz (approximate centre frequency)
# Centre of bin b: 2^((b+1)/2) ticks.  freq = 1e6 / centre.
BIN_FREQ_HZ = [1e6 / (2 ** ((b + 1) / 2)) for b in range(NBINS)]


def log2bin32(v):
    """Map IEI value v (1..32767) to half-octave bin 0..31.

    Bit-faithful Python mirror of the firmware's log2bin32().
    m = floor(log2(v)); sub = bit below MSB.  bin = (m<<1) | sub.
    """
    m = 0
    t = v
    while t >= 2:
        t >>= 1
        m += 1
    sub = ((v >> (m - 1)) & 1) if m >= 1 else 0
    return (m << 1) | sub


def cell_of(x, y):
    """Compute coarse cell index from (x, y).

    col = x >> X_CELL_SHIFT (0..15); row = y >> Y_CELL_SHIFT (0..13).
    cidx = col + row * N_CX = col + (row << 4), range 0..223.
    """
    col = int(x) >> X_CELL_SHIFT
    row = int(y) >> Y_CELL_SHIFT
    return col + (row << 4)   # row<<4 = row*16, no multiply


def python_shibboleth_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_shibboleth/main.c's ISR.

    x, y, pol, ts are per-event arrays.  pol is accepted (mirrors ABI) but
    unused by the algorithm.  Processes only complete batches of BATCH events.

    State cold-start all zeros:
      cell_cnt=[0]*N_CELLS; cell_last_ts=[0]*N_CELLS; hist=[0]*NBINS;
      hot_cidx=0; iei_total=0;
      lat_pbin=lat_valid=lat_total=lat_hot_cidx=0;
      batch_in_window=0; wseq=0.

    Per event (in arrival order within each batch):
      c = cell_of(x[i] & 0x7F, y[i] & 0x7F)
      cell_cnt[c] += 1 (saturating at 255)
      if c == hot_cidx:
          dt = (ts[i] - cell_last_ts[c]) & TS_MASK
          if 1 <= dt < IEI_MAX and iei_total < TOTAL_CAP:
              hist[log2bin32(dt)] += 1 (if < HIST_CAP)
              iei_total += 1
      cell_last_ts[c] = ts[i] & TS_MASK

    KEY: hist[] and iei_total count EXACTLY the same set of IEIs (both gated
    by iei_total < TOTAL_CAP).  This consistency is required for the noise guard.

    After each batch (latch BEFORE emit):
      batch_in_window += 1
      if batch_in_window >= WINDOW_BATCHES:
          find best_c = argmax(cell_cnt) (lowest index wins ties)
          find peak, pbin = max/argmax(hist) (lowest index wins ties)
          valid = (iei_total >= MIN_IEIS and peak >= iei_total >> CONF_SHIFT)
          if not valid: pbin = 0
          latch lat_*, advance hot_cidx = best_c
          clear cell_cnt, hist, iei_total; wseq = (wseq+1) & WSEQ_MASK

    Emit one word per batch:
      word = (wseq<<21) | ((lat_hot_cidx>>1)<<14) | (lat_total<<6)
           | (lat_valid<<5) | lat_pbin

    Returns (words, latches) where latches is a list of
    (pbin, valid, iei_total, hot_cidx) tuples appended at every window latch.
    cell_last_ts[] persists across window boundaries; only cell_cnt[], hist[],
    and iei_total are cleared.
    """
    cell_cnt     = [0] * N_CELLS
    cell_last_ts = [0] * N_CELLS
    hist         = [0] * NBINS
    hot_cidx     = 0
    iei_total    = 0
    lat_pbin     = 0
    lat_valid    = 0
    lat_total    = 0
    lat_hot_cidx = 0
    batch_in_window = 0
    wseq         = 0
    words        = []
    latches      = []
    n            = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Process BATCH events
        for i in range(b, b + BATCH):
            xi  = int(x[i])  & 0x7F
            yi  = int(y[i])  & 0x7F
            tsi = int(ts[i]) & TS_MASK

            c = cell_of(xi, yi)

            # Accumulate cell activity count (saturating at 255)
            if cell_cnt[c] < 255:
                cell_cnt[c] += 1

            # Accumulate IEI only for the hot cell; gate both hist and iei_total
            # on iei_total < TOTAL_CAP for consistency (mirrors firmware exactly)
            if c == hot_cidx:
                prev = cell_last_ts[c]
                dt   = (tsi - prev) & TS_MASK
                if 1 <= dt < IEI_MAX and iei_total < TOTAL_CAP:
                    bk = log2bin32(dt)
                    if hist[bk] < HIST_CAP:
                        hist[bk] += 1
                    iei_total += 1

            # Update per-cell last timestamp always
            cell_last_ts[c] = tsi

        # After each batch: check window boundary (latch BEFORE emit)
        batch_in_window += 1
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0

            # Find hottest cell (lowest index wins ties)
            best_cnt = 0
            best_c   = 0
            for c in range(N_CELLS):
                if cell_cnt[c] > best_cnt:
                    best_cnt = cell_cnt[c]
                    best_c   = c

            # Find dominant IEI bin (lowest index wins ties)
            peak = 0
            pbin = 0
            for bk in range(NBINS):
                if hist[bk] > peak:
                    peak = hist[bk]
                    pbin = bk

            # Noise guard: valid=1 only when peak bin dominates.
            # peak >= (iei_total >> CONF_SHIFT) means peak holds >= 1/8 of IEIs.
            # Since hist[] and iei_total count the same IEIs, for N-bin noise
            # peak ~ total/N < total/8 when N > 8 -> valid=0.
            if iei_total >= MIN_IEIS and peak >= (iei_total >> CONF_SHIFT):
                valid = 1
            else:
                valid = 0
                pbin  = 0

            lat_pbin     = pbin
            lat_valid    = valid
            lat_total    = iei_total
            lat_hot_cidx = hot_cidx   # latch the cell that WAS hot this window

            latches.append((pbin, valid, iei_total, hot_cidx))

            # Advance hot_cidx to newly found best cell
            hot_cidx = best_c

            # Clear per-window accumulators (cell_last_ts[] persists)
            cell_cnt  = [0] * N_CELLS
            hist      = [0] * NBINS
            iei_total = 0

            wseq = (wseq + 1) & WSEQ_MASK

        # Emit one word per batch; latch fields already updated above
        word = (wseq                << 21) \
             | ((lat_hot_cidx >> 1) << 14) \
             | (lat_total           <<  6) \
             | (lat_valid           <<  5) \
             |  lat_pbin
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one shibboleth status word.

    bits[4:0]=pbin, bits[5]=valid, bits[13:6]=iei_total,
    bits[20:14]=hot_cidx7 (hot_cidx>>1), bits[23:21]=wseq, bits[31:24]=0.
    """
    pbin      =  word        & 0x1F
    valid     = (word >>  5) & 0x1
    iei_total = (word >>  6) & 0xFF
    hot_cidx7 = (word >> 14) & 0x7F
    wseq      = (word >> 21) & 0x7
    return pbin, valid, iei_total, hot_cidx7, wseq


def pbin_to_freq_str(pbin, tick_hz=1e6):
    """Convert a pbin value to a human-readable frequency string.

    Bin b covers IEI ticks in [2^(b/2), 2^((b+2)/2)).
    Centre at 2^((b+1)/2) ticks -> freq = tick_hz / centre.
    Returns e.g. "~12.5 kHz (bin 19)" for pbin=19 at 1 MHz tick rate.
    """
    centre_ticks = 2 ** ((pbin + 1) / 2)
    freq_hz      = tick_hz / centre_ticks
    if freq_hz >= 1e3:
        return f"~{freq_hz / 1e3:.1f} kHz (bin {pbin})"
    return f"~{freq_hz:.0f} Hz (bin {pbin})"


# ---------------------------------------------------------------------------
# Synthetic stream builder (PWM model)
# ---------------------------------------------------------------------------

def build_pwm_stream(period, n_events, cell_col, cell_row, t0=1000):
    """Build a synthetic periodic (PWM) event stream in a single cell.

    Events arrive in cell (cell_col, cell_row) with IEI exactly = period ticks.
    x = cell_col * 8 + 3 (centre pixel of the cell);
    y = cell_row * 8 + 3.
    Returns int64 numpy arrays (x, y, ts, pol).
    """
    xv  = cell_col * 8 + 3
    yv  = cell_row * 8 + 3
    xs  = [xv]  * n_events
    ys  = [yv]  * n_events
    tss = [t0 + period * i for i in range(n_events)]
    pol = [i % 2 for i in range(n_events)]
    return (np.array(xs,  dtype=np.int64),
            np.array(ys,  dtype=np.int64),
            np.array(tss, dtype=np.int64),
            np.array(pol, dtype=np.int64))


# ---------------------------------------------------------------------------
# Renderer: spectral bars + detected accent on a dark backdrop.
# ---------------------------------------------------------------------------

def render_shibboleth(words, tick_hz=1e6, save=None, headless=False):
    """Render a three-panel figure: accent lamp, IEI histogram, accent history."""
    if not words:
        print("no words to render")
        return

    pbin_last, valid_last, total_last, hot7_last, _ = unpack_status(words[-1])

    # Collect per-window samples (one per wseq change)
    history_pbin  = []
    history_valid = []
    prev_wseq = None
    for word in words:
        pb, vl, tt, hc, wq = unpack_status(word)
        if wq != prev_wseq:
            history_pbin.append(pb)
            history_valid.append(vl)
            prev_wseq = wq

    # Schematic IEI spectral bar chart: Gaussian-like peak at pbin_last
    hist_disp = [0.0] * NBINS
    if valid_last and total_last > 0:
        for b in range(NBINS):
            dist = abs(b - pbin_last)
            hist_disp[b] = math.exp(-dist * dist / 2.0) * total_last

    BG    = "#0d0b10"
    TEXT  = "#e8dfc8"
    GOLD  = "#e8b84b"
    CYAN  = "#4bd4e8"
    STEEL = "#8a94a6"
    DIM   = "#555566"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last: pbin={pbin_last} valid={valid_last} total={total_last} "
              f"hot_cidx={hot7_last * 2}")
        if valid_last:
            print(f"detected accent: {pbin_to_freq_str(pbin_last, tick_hz)}")
        else:
            print("no accent detected")
        if history_pbin:
            print("per-window pbin history:", history_pbin)
            print("per-window valid history:", history_valid)
        return

    fig = plt.figure(figsize=(12, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(2, 2, width_ratios=[1, 1.8], height_ratios=[1.2, 1],
                          wspace=0.38, hspace=0.52,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_lamp = fig.add_subplot(gs[:, 0])    # left column, full height
    ax_hist = fig.add_subplot(gs[0, 1])    # right top: IEI spectral bars
    ax_pbin = fig.add_subplot(gs[1, 1])    # right bottom: pbin history

    for ax in (ax_lamp, ax_hist, ax_pbin):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left panel: accent lamp ---
    ax_lamp.set_xlim(-1.4, 1.4)
    ax_lamp.set_ylim(-2.0, 1.4)
    ax_lamp.set_aspect("equal")
    ax_lamp.set_xticks([])
    ax_lamp.set_yticks([])
    ax_lamp.set_title("accent lamp", color=TEXT, fontsize=9, pad=4)

    lamp_color = GOLD if valid_last else DIM
    lamp = mpatches.Circle((0, 0.2), 0.85, color=lamp_color, zorder=2)
    ax_lamp.add_patch(lamp)
    ring = mpatches.Circle((0, 0.2), 0.85, fill=False,
                            edgecolor=TEXT, linewidth=1.5, zorder=3)
    ax_lamp.add_patch(ring)

    if valid_last:
        label_main = pbin_to_freq_str(pbin_last, tick_hz)
        label_sub  = f"pbin={pbin_last}  total={total_last}"
    else:
        label_main = "no accent"
        label_sub  = f"total={total_last}  (noise / off)"

    ax_lamp.text(0, -0.75, label_main,
                 ha="center", va="center", color=TEXT, fontsize=11, fontweight="bold")
    ax_lamp.text(0, -1.15, label_sub,
                 ha="center", va="center", color=STEEL, fontsize=8)
    hot_col = (hot7_last * 2) % N_CX
    hot_row = (hot7_last * 2) // N_CX
    ax_lamp.text(0, -1.50, f"hot cell ~col={hot_col} row={hot_row}",
                 ha="center", va="center", color=STEEL, fontsize=7)

    # --- Right top: IEI spectral bar chart ---
    bar_colors = [GOLD if b == pbin_last and valid_last else CYAN
                  for b in range(NBINS)]
    ax_hist.bar(range(NBINS), hist_disp, color=bar_colors, width=0.8, zorder=2)
    if valid_last and pbin_last < NBINS:
        ax_hist.axvline(pbin_last, color=GOLD, linewidth=1.2,
                        linestyle="--", alpha=0.7, zorder=3)
        freq_label = pbin_to_freq_str(pbin_last, tick_hz)
        if max(hist_disp) > 0:
            ax_hist.text(pbin_last + 0.5, max(hist_disp) * 0.85,
                         freq_label, color=GOLD, fontsize=7, va="top")
    ax_hist.set_xlabel("IEI log-bin (half-octaves, 0..31)", color=TEXT, fontsize=8)
    ax_hist.set_ylabel("relative count (schematic)", color=TEXT, fontsize=8)
    ax_hist.set_title("IEI spectral profile (last window)", color=TEXT, fontsize=9, pad=4)
    ax_hist.set_xlim(-0.5, 31.5)

    # --- Right bottom: per-window pbin history ---
    if history_pbin:
        xs_h     = list(range(len(history_pbin)))
        colors_h = [GOLD if history_valid[i] else DIM
                    for i in range(len(history_pbin))]
        ax_pbin.scatter(xs_h, history_pbin, color=colors_h, s=18, zorder=2)
        ax_pbin.step(xs_h, history_pbin, where="post",
                     color=STEEL, linewidth=0.7, alpha=0.5)
    ax_pbin.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_pbin.set_ylabel("dominant IEI bin", color=TEXT, fontsize=8)
    ax_pbin.set_title("per-window pbin  (gold=valid, grey=no accent)",
                      color=TEXT, fontsize=9, pad=4)
    ax_pbin.set_ylim(-0.5, 31.5)

    fig.suptitle('"Shibboleth" -- PWM accent detector', color=TEXT,
                 fontsize=12, fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks against pre-computed expected values.

    Expected numbers were hand-derived from the algorithm and independently
    verified.  If the mirror disagrees with any of them the mirror is wrong.
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) PWM_SINGLE: clean periodic pulse train at period P=1000 ticks
    #     in cell (col=0, row=0) -> cidx=0 = hot_cidx from cold start.
    #     Each event has IEI=P with respect to the previous event in that cell.
    #     log2bin32(1000): m=floor(log2(1000))=9, sub=(1000>>8)&1=1,
    #     bin=(9<<1)|1=19.  All IEIs land in bin 19.
    #     N_A = WINDOW_BATCHES*BATCH + 64 = 1088 events = 272 batches > 1 window.
    #     In window 1, hot_cidx=0 (cold start) and all events go to cell 0,
    #     so IEIs accumulate from the 2nd event onward (first dt from ts=0
    #     is 1000 < IEI_MAX, so that first IEI also counts).
    #     After window 1: iei_total=min(1023, 255)=255 (saturated), all in bin 19.
    #     peak=255 >= 255>>3=31 AND total>=8 -> valid=1, pbin=19.
    #     wseq check: word i has wseq==((i+1)//WINDOW_BATCHES)&WSEQ_MASK.
    # ------------------------------------------------------------------
    P_A  = 1000
    N_A  = WINDOW_BATCHES * BATCH + 64    # 1088 events = 272 batches > 1 window
    x_a, y_a, ts_a, pol_a = build_pwm_stream(P_A, N_A, cell_col=0, cell_row=0)

    words_a, latches_a = python_shibboleth_words(x_a, y_a, ts_a, pol_a)

    lb_1000    = log2bin32(1000)
    lat1_a     = latches_a[0] if latches_a else None
    a_pbin_ok  = lat1_a is not None and lat1_a[0] == 19
    a_valid_ok = lat1_a is not None and lat1_a[1] == 1
    wf_a       = all((w >> 24) == 0 for w in words_a)

    a_ok = a_pbin_ok and a_valid_ok and (lb_1000 == 19) and wf_a
    print(f"  (a) PWM_SINGLE (P={P_A}, expect pbin=19, valid=1): "
          f"latch1={lat1_a}, log2bin32({P_A})={lb_1000} (want 19), "
          f"upper-bits-clear={wf_a} -> {'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) TWO_LIGHTS: two periodic streams in separate cells; cell A fires
    #     more frequently so it becomes the hot cell.
    #     Cell A: col=0,row=0 (x=3,y=3)     P_A=200 -> log2bin32(200)=15
    #     Cell B: col=8,row=4 (x=67,y=35)   P_B=1000 -> log2bin32(1000)=19
    #     Cell A fires 5x per 1000 ticks; cell B fires 1x per 1000 ticks.
    #     After window 1: hot_cidx updated to cell A.
    #     After window 2 (using cell A): pbin should be 15 (cell A's period).
    #
    #     Build: generate two independent sorted streams merged by timestamp.
    #     Cell A fires at t0, t0+P_A, t0+2*P_A, ...
    #     Cell B fires at t0+5, t0+5+P_B, ...  (offset 5 to avoid same ts)
    # ------------------------------------------------------------------
    P_A2  = 200
    P_B2  = 1000
    lb_pa = log2bin32(P_A2)   # 15: floor(log2(200))=7, sub=(200>>6)&1=1, bin=15
    lb_pb = log2bin32(P_B2)   # 19
    N_WIN2 = WINDOW_BATCHES * BATCH * 2 + 64   # > 2 windows

    # Cell A: x=3, y=3 -> cell 0
    # Cell B: x=67, y=35 -> col=8, row=4 -> cell=8+4*16=72
    evts_2 = []
    ta, tb = 1000, 1005
    while len(evts_2) < N_WIN2 * 2:
        evts_2.append((ta, 3, 3, 0))
        ta += P_A2
    while len(evts_2) < N_WIN2 * 2 + N_WIN2 // 5:
        evts_2.append((tb, 67, 35, 0))
        tb += P_B2
    evts_2.sort(key=lambda e: e[0])
    n_2 = (len(evts_2) // BATCH) * BATCH
    evts_2 = evts_2[:n_2]

    x_2   = np.array([e[1] for e in evts_2], dtype=np.int64)
    y_2   = np.array([e[2] for e in evts_2], dtype=np.int64)
    ts_2  = np.array([e[0] for e in evts_2], dtype=np.int64)
    pol_2 = np.array([e[3] for e in evts_2], dtype=np.int64)

    words_2, latches_2 = python_shibboleth_words(x_2, y_2, ts_2, pol_2)

    # After window 2, pbin should be lb_pa=15 (cell A dominates)
    lat2_2     = latches_2[1] if len(latches_2) > 1 else None
    b_pbin_ok  = lat2_2 is not None and lat2_2[0] == lb_pa
    b_valid_ok = lat2_2 is not None and lat2_2[1] == 1

    b_ok = b_pbin_ok and b_valid_ok
    print(f"  (b) TWO_LIGHTS (A: P={P_A2}->pbin={lb_pa}; B: P={P_B2}->pbin={lb_pb}; "
          f"A dominates by event count): latch2={lat2_2} "
          f"(want pbin={lb_pa}, valid=1) -> {'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) BROADBAND_NOISE: events at intervals that each map to a DIFFERENT
    #     bin (one dt per bin).  With hist[]/iei_total counting the same IEIs,
    #     each of the ~29 occupied bins gets total/29 ~ 8 counts while total=255
    #     is saturated.  peak ~ 9 < 255>>3=31 -> valid=0.
    #     All events go to cell (col=0, row=0) = hot_cidx at cold start.
    #
    #     NOISE DT LIST (one dt representative per bin, 29 unique bins 0..28):
    #     These are the smallest v in 1..32767 for each half-octave bin.
    # ------------------------------------------------------------------
    # First dt per half-octave bin 0..28
    noise_dts_one_per_bin = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96,
                              128, 192, 256, 384, 512, 768, 1024, 1536, 2048,
                              3072, 4096, 6144, 8192, 12288, 16384, 24576]
    # Verify all distinct bins
    bins_covered = set(log2bin32(d) for d in noise_dts_one_per_bin)
    assert len(bins_covered) == len(noise_dts_one_per_bin), \
        f"collision in noise_dts_one_per_bin: {len(bins_covered)} bins for {len(noise_dts_one_per_bin)} dts"

    N_C = WINDOW_BATCHES * BATCH + 64
    xs_c = []; ys_c = []; tss_c = []; pol_c = []
    t_c = 1000
    for i in range(N_C):
        xs_c.append(3); ys_c.append(3)
        tss_c.append(t_c); pol_c.append(i % 2)
        t_c += noise_dts_one_per_bin[i % len(noise_dts_one_per_bin)]

    x_c   = np.array(xs_c,  dtype=np.int64)
    y_c   = np.array(ys_c,  dtype=np.int64)
    ts_c  = np.array(tss_c, dtype=np.int64)
    pol_c = np.array(pol_c, dtype=np.int64)

    words_c, latches_c = python_shibboleth_words(x_c, y_c, ts_c, pol_c)

    c_valid0       = len(latches_c) > 0 and all(lat[1] == 0 for lat in latches_c)
    c_words_valid0 = all(((w >> 5) & 1) == 0 for w in words_c)

    c_ok = c_valid0 and c_words_valid0 and len(latches_c) > 0
    print(f"  (c) BROADBAND_NOISE ({len(noise_dts_one_per_bin)} bins, "
          f"noise guard rejects): latches={len(latches_c)}, "
          f"all latch valid=0={c_valid0}, all word valid=0={c_words_valid0} -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) TOO_FEW_IEIS (MIN_IEIS guard): events arrive in the hot cell at
    #     a clean period P=1000, but only BATCH events total in the window
    #     (one batch), so at most BATCH-1=3 IEIs per window < MIN_IEIS=8.
    #     valid=0 because iei_total < MIN_IEIS.
    #     Run exactly one full window (WINDOW_BATCHES*BATCH events) in the
    #     hot cell; but space them far apart so that only a few per-window
    #     IEIs actually accumulate before ts wraps.
    #     Simplest: send exactly MIN_IEIS-1 events in the hot cell, padding
    #     the rest to a different cell so cell counts don't interfere.
    #     Use only WINDOW_BATCHES*BATCH total events (exactly 1 window).
    # ------------------------------------------------------------------
    N_D_HOT  = MIN_IEIS - 1    # 7 events in hot cell
    N_D_PAD  = WINDOW_BATCHES * BATCH - N_D_HOT   # rest in a different cell
    # hot cell: col=0, row=0 -> x=3, y=3
    # pad cell: col=1, row=0 -> x=11, y=3
    xs_d  = [3]  * N_D_HOT + [11] * N_D_PAD
    ys_d  = [3]  * N_D_HOT + [3]  * N_D_PAD
    tss_d = [1000 + 1000 * i for i in range(N_D_HOT)] + \
            [1000 + 1000 * N_D_HOT + 5 * j for j in range(N_D_PAD)]
    pol_d = [i % 2 for i in range(N_D_HOT + N_D_PAD)]

    x_d   = np.array(xs_d,  dtype=np.int64)
    y_d   = np.array(ys_d,  dtype=np.int64)
    ts_d  = np.array(tss_d, dtype=np.int64)
    pol_d = np.array(pol_d, dtype=np.int64)

    words_d, latches_d = python_shibboleth_words(x_d, y_d, ts_d, pol_d)

    lat1_d    = latches_d[0] if latches_d else None
    d_valid0  = lat1_d is not None and lat1_d[1] == 0
    d_total_lt_min = lat1_d is not None and lat1_d[2] < MIN_IEIS

    d_ok = d_valid0 and d_total_lt_min
    print(f"  (d) TOO_FEW_IEIS (MIN_IEIS={MIN_IEIS} guard, hot cell has {N_D_HOT} events): "
          f"latch1={lat1_d} (want valid=0, total<{MIN_IEIS}) -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) WSEQ_ARITHMETIC: wseq == ((i+1) // WINDOW_BATCHES) & WSEQ_MASK
    #     for every word index i (0-based).  Use stream (a) (large enough).
    # ------------------------------------------------------------------
    bad_e = []
    for i, word in enumerate(words_a):
        expected_wseq = ((i + 1) // WINDOW_BATCHES) & WSEQ_MASK
        actual_wseq   = (word >> 21) & WSEQ_MASK
        if actual_wseq != expected_wseq:
            bad_e.append(f"i={i} got={actual_wseq} want={expected_wseq}")
            if len(bad_e) >= 3:
                break
    e_ok = len(bad_e) == 0
    print(f"  (e) WSEQ_ARITHMETIC: {len(words_a)} words; "
          f"every word[i] has wseq==((i+1)//{WINDOW_BATCHES})&0x{WSEQ_MASK:x} -> "
          f"{'OK' if e_ok else 'FAIL: ' + '; '.join(bad_e[:3])}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) WELL-FORMEDNESS over all words from (a)-(d):
    #     pbin<=31, valid<=1, iei_total<=255, hot_cidx7<=127,
    #     wseq<=7, upper byte=0, word < 2^32.
    # ------------------------------------------------------------------
    all_words_f = words_a + words_2 + list(words_c) + list(words_d)
    bad_f = []
    for word in all_words_f:
        pb, vl, tt, hc7, wq = unpack_status(word)
        if pb  > 31:   bad_f.append(f"pbin={pb}")
        if vl  > 1:    bad_f.append(f"valid={vl}")
        if tt  > 255:  bad_f.append(f"total={tt}")
        if hc7 > 127:  bad_f.append(f"hot_cidx7={hc7}")
        if wq  > 7:    bad_f.append(f"wseq={wq}")
        if (word >> 24) != 0:
            bad_f.append(f"upper bits in 0x{word:08x}")
        if word >= (1 << 32):
            bad_f.append(f"word>=2^32: 0x{word:08x}")
        if bad_f:
            break
    f_ok = len(bad_f) == 0
    print(f"  (f) WELL-FORMEDNESS: {len(all_words_f)} total words; "
          f"pbin<=31, valid<=1, total<=255, hot_cidx7<=127, "
          f"wseq<=7, upper byte=0, word<2^32 -> "
          f"{'OK' if f_ok else 'FAIL: ' + '; '.join(bad_f[:5])}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) LOG-BIN EXHAUSTIVE: for every v in 1..32767:
    #     0 <= log2bin32(v) <= 31; monotone non-decreasing;
    #     spot-assert log2bin32(1000)=19, log2bin32(200)=15, log2bin32(1)=0.
    # ------------------------------------------------------------------
    bins_g      = [log2bin32(v) for v in range(1, 32768)]
    range_ok_g  = all(0 <= b <= 31 for b in bins_g)
    mono_ok_g   = all(bins_g[i] <= bins_g[i + 1] for i in range(len(bins_g) - 1))

    def ref_log2bin(v):
        bl  = v.bit_length()
        m   = bl - 1
        sub = ((v >> (bl - 2)) & 1) if bl >= 2 else 0
        return (m << 1) | sub

    formula_ok_g = all(log2bin32(v) == ref_log2bin(v) for v in range(1, 32768))
    spot_ok_g    = (log2bin32(1000) == 19 and log2bin32(200)  == 15
                    and log2bin32(1) == 0  and log2bin32(2)    == 2
                    and log2bin32(3) == 3  and log2bin32(4)    == 4)

    g_ok = range_ok_g and mono_ok_g and formula_ok_g and spot_ok_g
    print(f"  (g) LOG-BIN EXHAUSTIVE (1..32767): range 0..31={range_ok_g}, "
          f"monotone={mono_ok_g}, formula match={formula_ok_g}, "
          f"spot values correct={spot_ok_g} -> {'OK' if g_ok else 'FAIL'}")
    ok = ok and g_ok

    print()
    print("VALIDATION:", "PASS -- PWM single-period exact; two-lights dominance; "
          "broadband noise guard; too-few-IEIs guard; wseq arithmetic; "
          "well-formedness; log-bin exhaustive"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader (matches dvs_vital_view.py pattern)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for the timestamp field (default 'le').
    NOTE: recorded captures carry a wrapped coarse counter (not real
    microseconds) so frequency labels are qualitative -- --validate builds
    its own synthetic timestamps and is the deterministic check.
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
        ts = np.array([int(row[idx[ts_col]]) & TS_MASK for row in rows],
                      dtype=np.int64)
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
                    help="synthetic self-test: PWM single-period exact, two-lights "
                         "dominance, broadband noise guard, too-few-IEIs guard, "
                         "wseq arithmetic, well-formedness, log-bin exhaustive")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le -- NOTE: le is a "
                         "wrapped coarse counter, not real microseconds; frequency labels "
                         "on recorded captures are qualitative; --validate builds its own "
                         "synthetic timestamps and is the deterministic check)")
    ap.add_argument("--tick-hz", type=float, default=1e6,
                    help="chip tick rate in Hz for frequency labels (default: 1e6)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the shibboleth PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing shibboleth words "
              f"in Python (bit-faithful mirror of firmware).")
        words, _ = python_shibboleth_words(x, y, ts, pol)
        if words:
            pbin_l, valid_l, total_l, hot7_l, _ = unpack_status(words[-1])
            if valid_l:
                print(f"final accent: {pbin_to_freq_str(pbin_l, args.tick_hz)}, "
                      f"total={total_l} IEIs, hot_cidx~{hot7_l * 2} "
                      f"({len(words)} words emitted)")
            else:
                print(f"final: no accent (valid=0), total={total_l} IEIs "
                      f"({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_shibboleth(words, tick_hz=args.tick_hz, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
