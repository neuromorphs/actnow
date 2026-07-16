#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_mirror/main.c
("Who Is the Mirror?" -- causality-lag detector for two-player mimicry).
Two players occupy the left (x < 63) and right (x >= 63) halves of the
126x112 frame.  One leads a motion; the other mirrors it.  The algorithm
splits the FOV, binarizes per-half time-bin activity against a per-bin
mean threshold, and finds the lag that maximises AND-popcount between the
two bitmasks -- no multiply, no divide anywhere.

Leader codes: 0=NONE (simultaneous or low confidence), 1=LEFT leads,
2=RIGHT leads.

Word layout: bits[1:0]=leader, bits[9:2]=lag_mag (bins), bits[18:10]=confidence
(AND-popcount 0..32), bits[22:19]=seq (4-bit window counter), bits[31:23]=0.
One time-bin = BIN_TICKS=16 ts ticks (BIN_SHIFT=4).

python_mirror_words() below is a bit-faithful port of the firmware's integer
logic (same ring-buffer aging, same mean binarization, same rotate+AND-popcount
correlation, same word packing) so what we emit is provably what the chip
would emit given the same event stream.

------------------------------------------------------------------------------
Usage:
  dvs_mirror_view.py --validate                  # synthetic self-test
  dvs_mirror_view.py --from-actsim results.mem   # render real chip status words
  dvs_mirror_view.py events.csv --ts-col le      # render (host-computed) from CSV
  dvs_mirror_view.py ... --headless --save mirror.png
"""
import argparse
import numpy as np

# --- must match software/dvs_mirror/main.c exactly ---
SX, SY        = 126, 112
BATCH         = 4
TS_MASK       = 0xFFFF
SPLIT_X       = 63       # x < SPLIT_X -> left; x >= SPLIT_X -> right
BIN_SHIFT     = 4        # 1 bin = 16 ts ticks
BIN_TICKS     = 1 << BIN_SHIFT
NBINS         = 32       # must be <= 32 (fits in uint32_t bitmask)
LOG2_NBINS    = 5        # log2(NBINS); threshold = total >> LOG2_NBINS
BIN_MASK      = NBINS - 1
LAG_MAX       = 8        # search k in [-LAG_MAX .. +LAG_MAX]
WINDOW_BATCHES = 128
MIN_ACTIVITY  = 32       # min total events per half before correlating
MIN_CONFIDENCE = 4       # min AND-popcount to trust a leader call
HALF_CAP      = 255
WSEQ_MASK     = 0xF

LEADER_NONE   = 0
LEADER_LEFT   = 1
LEADER_RIGHT  = 2
LEADER_NAMES  = ["NONE", "LEFT leads", "RIGHT leads", "reserved"]


def popcount32(x):
    """Count set bits in a 32-bit integer.  5-stage shift-and-add, no multiply."""
    x = x & 0xFFFFFFFF
    x = x - ((x >> 1) & 0x55555555)
    x = (x & 0x33333333) + ((x >> 2) & 0x33333333)
    x = (x + (x >> 4)) & 0x0F0F0F0F
    x = x + (x >> 8)
    x = x + (x >> 16)
    return x & 0x3F


def rotate_bits32(x, n):
    """Rotate the low NBINS bits of x LEFT by n positions (0..NBINS-1).

    Two shifts and one OR, no multiply.  n=0 returns x & mask unchanged.
    Only the low NBINS bits of x and the result are meaningful.
    """
    mask = (1 << NBINS) - 1
    x = x & mask
    if n == 0:
        return x
    return ((x << n) | (x >> (NBINS - n))) & mask


def build_bitmask(cnt, total):
    """Binarize a ring-buffer against per-bin mean threshold.

    threshold = total >> LOG2_NBINS (= total / NBINS, one shift, no divide).
    bit i is set iff cnt[i] > threshold.  Returns a NBINS-bit integer.
    """
    threshold = total >> LOG2_NBINS
    mask = 0
    for i in range(NBINS):
        if cnt[i] > threshold:
            mask |= (1 << i)
    return mask


def python_mirror_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_mirror/main.c's ISR.

    x, y, ts, pol are per-event arrays (y and pol are consumed per ABI but
    ignored by the algorithm).  Processes only complete batches (n - n%BATCH).

    State cold-start all zeros:
      left_cnt=[0]*NBINS; right_cnt=[0]*NBINS
      lat_leader=lat_lag_mag=lat_confidence=0; batch_in_window=0; wseq=0

    Per event in order:
      bin = (ts[i] & TS_MASK) >> BIN_SHIFT & BIN_MASK
      x[i] < SPLIT_X -> left_cnt[bin]++ (capped at HALF_CAP)
      else           -> right_cnt[bin]++ (capped at HALF_CAP)

    After each batch of BATCH events (latch BEFORE emit):
      batch_in_window += 1
      if batch_in_window >= WINDOW_BATCHES: run correlation, latch, advance wseq

    Emit one word per batch:
      word = (wseq<<19) | (confidence<<10) | (lag_mag<<2) | leader

    Returns (words, latches) where latches is a list of
    (leader, lag_mag, confidence) tuples appended at every window update.
    NOTE: ring buffers age naturally (old bins overwritten by new ts) --
    no explicit clear at window boundaries, exactly as in the firmware.
    """
    left_cnt  = [0] * NBINS
    right_cnt = [0] * NBINS
    lat_leader     = 0
    lat_lag_mag    = 0
    lat_confidence = 0
    batch_in_window = 0
    wseq = 0
    words   = []
    latches = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Process BATCH events: accumulate into ring buffers
        for i in range(b, b + BATCH):
            t   = int(ts[i]) & TS_MASK
            xi  = int(x[i])
            bin_idx = (t >> BIN_SHIFT) & BIN_MASK
            if xi < SPLIT_X:
                if left_cnt[bin_idx] < HALF_CAP:
                    left_cnt[bin_idx] += 1
            else:
                if right_cnt[bin_idx] < HALF_CAP:
                    right_cnt[bin_idx] += 1

        # After each batch: check window boundary (latch BEFORE emit)
        batch_in_window += 1
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0

            total_L = sum(left_cnt)
            total_R = sum(right_cnt)

            if total_L < MIN_ACTIVITY or total_R < MIN_ACTIVITY:
                lat_leader     = LEADER_NONE
                lat_lag_mag    = 0
                lat_confidence = 0
            else:
                left_bits  = build_bitmask(left_cnt,  total_L)
                right_bits = build_bitmask(right_cnt, total_R)

                best_corr    = 0
                best_lag_mag = 0
                best_leader  = LEADER_NONE

                # k == 0: simultaneous motion
                corr = popcount32(left_bits & right_bits)
                if corr > best_corr:
                    best_corr    = corr
                    best_lag_mag = 0
                    best_leader  = LEADER_NONE

                # k in 1..LAG_MAX: rotate +k -> RIGHT leads.
                # rotate_bits32(right_bits, k): bit r -> (r+k)%NBINS.
                # AND hits at left bit l = r+k, meaning right fired at r = l-k
                # (k bins before left) -> RIGHT is the leader.
                for k in range(1, LAG_MAX + 1):
                    rk   = rotate_bits32(right_bits, k)
                    corr = popcount32(left_bits & rk)
                    if corr > best_corr:
                        best_corr    = corr
                        best_lag_mag = k
                        best_leader  = LEADER_RIGHT

                # k in 1..LAG_MAX: rotate (NBINS-k) -> LEFT leads.
                # rotate_bits32(right_bits, NBINS-k): bit r -> (r-k)%NBINS.
                # AND hits at left bit l = r-k, meaning right fired at r = l+k
                # (k bins after left) -> LEFT is the leader.
                for k in range(1, LAG_MAX + 1):
                    rk   = rotate_bits32(right_bits, NBINS - k)
                    corr = popcount32(left_bits & rk)
                    if corr > best_corr:
                        best_corr    = corr
                        best_lag_mag = k
                        best_leader  = LEADER_LEFT

                # Confidence gate
                if best_corr < MIN_CONFIDENCE:
                    best_leader  = LEADER_NONE
                    best_lag_mag = 0

                if best_lag_mag > LAG_MAX:
                    best_lag_mag = LAG_MAX

                lat_leader     = best_leader
                lat_lag_mag    = best_lag_mag
                lat_confidence = best_corr

            latches.append((lat_leader, lat_lag_mag, lat_confidence))
            wseq = (wseq + 1) & WSEQ_MASK

        # Emit one word per batch using latched values
        word = (wseq           << 19) \
             | (lat_confidence << 10) \
             | (lat_lag_mag    <<  2) \
             |  lat_leader
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one mirror status word.

    bits[1:0]=leader, bits[9:2]=lag_mag, bits[18:10]=confidence,
    bits[22:19]=seq, bits[31:23]=0.
    """
    leader     =  word        & 0x3
    lag_mag    = (word >>  2) & 0xFF
    confidence = (word >> 10) & 0x1FF
    seq        = (word >> 19) & 0xF
    return leader, lag_mag, confidence, seq


# ---------------------------------------------------------------------------
# Synthetic stream builder
# ---------------------------------------------------------------------------

def build_leader_stream(lag_inject, n_events_per_half, left_x=30, right_x=90,
                        n_active_bins=4, events_per_bin=None, t0=200):
    """Build a synthetic two-player stream with a known causal lag.

    Left events are concentrated in bins BASE_BIN .. BASE_BIN+n_active_bins-1.
    Right events are concentrated in bins (BASE_BIN+|lag_inject|) ..
    (BASE_BIN+|lag_inject|+n_active_bins-1).

    If lag_inject > 0: left fires first, right fires lag_inject bins later
                       (LEFT leads).
    If lag_inject < 0: right fires first, left fires |lag_inject| bins later
                       (RIGHT leads).
    If lag_inject == 0: both fire in the SAME bins (simultaneous, NONE).

    n_events_per_half events are spread evenly across the active bins.
    With enough events per bin (> total/NBINS mean threshold) the bitmask
    has exactly n_active_bins set bits; at the injected lag the bitmasks
    align perfectly (popcount = n_active_bins).

    Bins are chosen so left and right active ranges do not overlap
    (lag_inject must be >= n_active_bins for a clean non-overlapping test;
    for lag_inject=0 the ranges are identical by design).

    Returns int64 numpy arrays (x, y, ts, pol).
    """
    BASE_BIN = 0          # left active bins start at bin index BASE_BIN
    if events_per_bin is None:
        events_per_bin = max(n_events_per_half // n_active_bins, 1)

    abs_lag  = abs(lag_inject) if lag_inject != 0 else 0
    # Left active bin indices
    left_bins  = [BASE_BIN + k for k in range(n_active_bins)]
    # Right active bin indices (shifted by lag_inject)
    right_bins = [(BASE_BIN + abs_lag + k) % NBINS for k in range(n_active_bins)]

    # For lag_inject < 0 (right leads): right fires first, swap roles.
    # We achieve "right leads" by placing right events at BASE_BIN and
    # left events at BASE_BIN + |lag_inject|.
    if lag_inject < 0:
        left_bins, right_bins = right_bins, left_bins

    xs, ys, tss, pols = [], [], [], []

    def add_events_to_bins(x_val, bins, count):
        for b in bins:
            base_ts = t0 + b * BIN_TICKS    # timestamp that lands in bin b
            for k in range(count):
                # Multiple events within the same bin: ts+k (k < BIN_TICKS)
                xs.append(x_val)
                ys.append(56)
                tss.append(base_ts + k)
                pols.append(0)

    add_events_to_bins(left_x,  left_bins,  events_per_bin)
    add_events_to_bins(right_x, right_bins, events_per_bin)

    # Sort by timestamp so the ISR receives events in causal order
    pairs = sorted(zip(tss, xs, ys, pols))
    if not pairs:
        return (np.array([], dtype=np.int64),) * 4
    tss_, xs_, ys_, pols_ = zip(*pairs)
    return (np.array(xs_,   dtype=np.int64),
            np.array(ys_,   dtype=np.int64),
            np.array(tss_,  dtype=np.int64),
            np.array(pols_, dtype=np.int64))


# ---------------------------------------------------------------------------
# Renderer: tribunal / needle aesthetic on dark backdrop.
# ---------------------------------------------------------------------------

def render_mirror(words, save=None, headless=False):
    """Compose one figure: leader needle + lag history + confidence history."""
    if not words:
        print("no words to render")
        return

    leader_last, lag_last, conf_last, _ = unpack_status(words[-1])

    # Collect one sample per seq change
    history_leader = []
    history_lag    = []
    history_conf   = []
    prev_seq = None
    for word in words:
        leader_, lag_, conf_, seq_ = unpack_status(word)
        if seq_ != prev_seq:
            history_leader.append(leader_)
            history_lag.append(lag_ if leader_ == LEADER_LEFT else
                                -lag_ if leader_ == LEADER_RIGHT else 0)
            history_conf.append(conf_)
            prev_seq = seq_

    BG     = "#0b0f12"
    TEXT   = "#ddd8c8"
    GOLD   = "#e8c84b"
    INDIGO = "#5a7fd4"
    RED    = "#d45a5a"
    GREEN  = "#5fd490"
    STEEL  = "#8a94a6"
    DIM    = "#3a3a4a"

    LEADER_COLORS = [DIM, GREEN, RED]   # NONE, LEFT, RIGHT

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.patheffects as pe
        import math
    except Exception as exc:
        print("matplotlib unavailable:", exc)
        print(f"last leader={LEADER_NAMES[leader_last & 3]} "
              f"lag_mag={lag_last} bins confidence={conf_last}")
        if history_lag:
            print("per-window signed-lag history:", history_lag)
            print("per-window confidence history:", history_conf)
        return

    fig = plt.figure(figsize=(11, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.6], wspace=0.40,
                          top=0.88, bottom=0.10, left=0.06, right=0.96)
    ax_needle = fig.add_subplot(gs[0])

    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_lag   = fig.add_subplot(gs_right[0])
    ax_conf  = fig.add_subplot(gs_right[1])
    # Remove placeholder right axis (replaced by subgridspec)
    # (gs[1] itself has no ax; we work with ax_lag and ax_conf directly)

    for ax in (ax_needle, ax_lag, ax_conf):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#263040")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left panel: tribunal needle ---
    # Needle angle: 0 = vertical (NONE); negative angle = LEFT leads; positive = RIGHT leads.
    # Signed lag from -LAG_MAX to +LAG_MAX (bins).
    signed_lag = (lag_last if leader_last == LEADER_LEFT else
                  -lag_last if leader_last == LEADER_RIGHT else 0)
    angle_deg  = (signed_lag / LAG_MAX) * 75.0   # full swing = 75 degrees each side

    ax_needle.set_xlim(-1.6, 1.6)
    ax_needle.set_ylim(-1.6, 1.6)
    ax_needle.set_aspect("equal")
    ax_needle.set_xticks([])
    ax_needle.set_yticks([])
    ax_needle.set_title("leader needle", color=TEXT, fontsize=9, pad=4)

    # Draw scale arc
    theta_arc = np.linspace(math.radians(90 - 75), math.radians(90 + 75), 120)
    ax_needle.plot(1.2 * np.cos(theta_arc), 1.2 * np.sin(theta_arc),
                   color="#263040", linewidth=2.5)

    # Draw LEFT / NONE / RIGHT labels
    ax_needle.text(-1.25, 0.2, "LEFT\nleads", ha="center", va="center",
                   color=GREEN, fontsize=8, fontweight="bold")
    ax_needle.text(+1.25, 0.2, "RIGHT\nleads", ha="center", va="center",
                   color=RED, fontsize=8, fontweight="bold")
    ax_needle.text(0, 1.35, "NONE", ha="center", va="center",
                   color=STEEL, fontsize=8)

    # Draw needle
    needle_angle_rad = math.radians(90 - angle_deg)
    nx = 1.1 * math.cos(needle_angle_rad)
    ny = 1.1 * math.sin(needle_angle_rad)
    needle_color = LEADER_COLORS[leader_last & 3]
    ax_needle.annotate("", xy=(nx, ny), xytext=(0, 0),
                        arrowprops=dict(arrowstyle="->", color=needle_color,
                                        lw=2.5, mutation_scale=18))
    # Pivot dot
    ax_needle.plot(0, 0, "o", color=TEXT, ms=7, zorder=5)

    # Label below needle
    ax_needle.text(0, -1.25,
                   f"{LEADER_NAMES[leader_last & 3]}",
                   ha="center", va="center", color=needle_color,
                   fontsize=11, fontweight="bold")
    ax_needle.text(0, -1.52,
                   f"lag={lag_last} bins ({lag_last * BIN_TICKS} ticks)  "
                   f"conf={conf_last}",
                   ha="center", va="center", color=TEXT, fontsize=8)

    # --- Right top: per-window signed-lag history ---
    if history_lag:
        ax_lag.axhline(0, color=STEEL, linewidth=0.8, linestyle="--", alpha=0.6)
        ax_lag.axhline(+LAG_MAX, color=RED,   linewidth=0.6, linestyle=":", alpha=0.5)
        ax_lag.axhline(-LAG_MAX, color=GREEN, linewidth=0.6, linestyle=":", alpha=0.5)
        xs_ = list(range(len(history_lag)))
        colors_ = [LEADER_COLORS[ldr & 3] for ldr in history_leader]
        ax_lag.scatter(xs_, history_lag, c=colors_, s=18, zorder=3)
        ax_lag.step(xs_, history_lag, where="post", color=STEEL, linewidth=0.7, alpha=0.5)
    ax_lag.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_lag.set_ylabel("signed lag (bins; − = right leads)", color=TEXT, fontsize=8)
    ax_lag.set_title("per-window lag (negative=right leads, positive=left leads)",
                     color=TEXT, fontsize=9, pad=4)
    ax_lag.set_ylim(-LAG_MAX - 0.5, LAG_MAX + 0.5)
    ax_lag.text(0.02, 0.93, f"1 bin = {BIN_TICKS} ts ticks",
                transform=ax_lag.transAxes, color=STEEL, fontsize=7, va="top")

    # --- Right bottom: per-window confidence history ---
    if history_conf:
        xs_ = list(range(len(history_conf)))
        ax_conf.axhline(MIN_CONFIDENCE, color=GOLD, linewidth=0.8, linestyle="--",
                        alpha=0.7, label=f"MIN_CONFIDENCE={MIN_CONFIDENCE}")
        ax_conf.step(xs_, history_conf, where="post", color=TEXT, linewidth=1.0)
        legend = ax_conf.legend(fontsize=7, framealpha=0.2,
                                labelcolor=TEXT, facecolor=BG, edgecolor="#263040")
    ax_conf.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_conf.set_ylabel("AND-popcount confidence", color=TEXT, fontsize=8)
    ax_conf.set_title("per-window correlation confidence", color=TEXT, fontsize=9, pad=4)
    ax_conf.set_ylim(-0.5, NBINS + 0.5)

    fig.suptitle('"Who Is the Mirror?" — causality-lag detector',
                 color=TEXT, fontsize=12, fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# Synthetic validation
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks against analytically-derived expectations.

    The bit-faithful mirror (python_mirror_words) must agree with pre-computed
    expectations.  Never adjust the expectations -- if they disagree, the
    mirror is wrong.
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) LEFT-LEADS EXACT
    # Left fires at bins 0..3 (x=30); right fires at bins 4..7 (x=90).
    # EPB_A=64 events/bin -> total_L=total_R=256; threshold=256>>5=8.
    # All active bins have count 64 > 8: left_bits=0x0F, right_bits=0xF0.
    # ROTATION SEMANTICS: rotate_bits32(right_bits, NBINS-k) shifts bit r to
    # (r-k)%NBINS; AND hits where left has bit l=r-k -> right fired at l+k
    # (k bins AFTER left) -> LEFT leads.  At k=4:
    #   rotate_bits32(0xF0, 28): bits 4..7 -> (4-4)..(7-4) = 0..3 -> 0x0F.
    #   popcount(0x0F & 0x0F)=4 >= MIN_CONFIDENCE.
    # k=0: popcount(0x0F & 0xF0)=0.  RIGHT-leads scan k=4: rotate(0xF0,4)=0xF00
    #   (bits 4-7 shift to 8-11); popcount(0x0F & 0xF00)=0.
    # So left-leads scan k=4 wins -> leader=LEFT(1), lag_mag=4, confidence=4.
    # ------------------------------------------------------------------
    LAG_INJECT_A = 4          # bins; left leads right by 4 bins
    N_ACTIVE_A   = 4          # 4 active bins per half
    EPB_A        = 64         # events per active bin (64*4*2=512 total = 1 full window)
    x_a, y_a, ts_a, pol_a = build_leader_stream(LAG_INJECT_A, None,
                                                  n_active_bins=N_ACTIVE_A,
                                                  events_per_bin=EPB_A, t0=200)
    words_a, latches_a = python_mirror_words(x_a, y_a, ts_a, pol_a)

    n_lat_a = len(latches_a)
    lat1_a  = latches_a[0] if n_lat_a >= 1 else None

    a_leader_ok   = lat1_a is not None and lat1_a[0] == LEADER_LEFT
    a_lag_ok      = lat1_a is not None and lat1_a[1] >= 1
    a_conf_ok     = lat1_a is not None and lat1_a[2] >= MIN_CONFIDENCE
    a_ok = a_leader_ok and a_lag_ok and a_conf_ok
    print(f"  (a) LEFT-LEADS EXACT: latches={n_lat_a} (want >=1), "
          f"latch1={lat1_a} (want leader=1/LEFT, lag>=1, conf>={MIN_CONFIDENCE}) -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) RIGHT-LEADS EXACT
    # lag_inject=-4: build_leader_stream swaps bins -> left fires at bins 4..7
    # (left_bits=0xF0), right fires at bins 0..3 (right_bits=0x0F).
    # Right fires first -> RIGHT leads.
    # ROTATION SEMANTICS: rotate_bits32(right_bits, k) shifts bit r to (r+k)%NBINS.
    # AND hits where left has bit l=r+k -> right fired at l-k (k bins before left)
    # -> RIGHT leads.  At k=4: rotate_bits32(0x0F, 4): bits 0..3 -> 4..7 = 0xF0.
    # popcount(0xF0 & 0xF0)=4 >= MIN_CONFIDENCE -> leader=RIGHT(2), lag_mag=4.
    # k=0: popcount(0xF0 & 0x0F)=0.  Left-leads scan k=4: rotate(0x0F, 28)=0xF0000000;
    #   popcount(0xF0 & 0xF0000000)=0.  RIGHT-leads scan k=4 wins -> leader=RIGHT.
    # ------------------------------------------------------------------
    LAG_INJECT_B = -4
    x_b, y_b, ts_b, pol_b = build_leader_stream(LAG_INJECT_B, None,
                                                  n_active_bins=N_ACTIVE_A,
                                                  events_per_bin=EPB_A, t0=200)
    words_b, latches_b = python_mirror_words(x_b, y_b, ts_b, pol_b)

    n_lat_b = len(latches_b)
    lat1_b  = latches_b[0] if n_lat_b >= 1 else None

    b_leader_ok = lat1_b is not None and lat1_b[0] == LEADER_RIGHT
    b_lag_ok    = lat1_b is not None and lat1_b[1] >= 1
    b_conf_ok   = lat1_b is not None and lat1_b[2] >= MIN_CONFIDENCE
    b_ok = b_leader_ok and b_lag_ok and b_conf_ok
    print(f"  (b) RIGHT-LEADS EXACT: latches={n_lat_b} (want >=1), "
          f"latch1={lat1_b} (want leader=2/RIGHT, lag>=1, conf>={MIN_CONFIDENCE}) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) SIMULTANEOUS SAME-PATTERN -> NONE
    # lag_inject=0: left and right both fire in bins 0..3.
    # left_bits=right_bits=0x0F.
    # k=0: popcount(0x0F & 0x0F) = 4.
    # k=1 left-leads: rotate(0x0F, 1)=0x1E; popcount(0x0F & 0x1E)=popcount(0x0E)=3 < 4.
    # So k=0 wins -> leader=NONE.
    # ------------------------------------------------------------------
    x_c, y_c, ts_c, pol_c = build_leader_stream(0, None,
                                                  n_active_bins=N_ACTIVE_A,
                                                  events_per_bin=EPB_A, t0=200)
    words_c, latches_c = python_mirror_words(x_c, y_c, ts_c, pol_c)

    n_lat_c = len(latches_c)
    lat1_c  = latches_c[0] if n_lat_c >= 1 else None

    c_leader_ok = lat1_c is not None and lat1_c[0] == LEADER_NONE
    c_ok = c_leader_ok
    print(f"  (c) SIMULTANEOUS SAME-PATTERN -> NONE: latches={n_lat_c} (want >=1), "
          f"latch1={lat1_c} (want leader=0/NONE) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) ACTIVITY GUARD -- one half dark -> NONE
    # 512 events all in left half (x=30); right half is dark (total_R=0).
    # total_R < MIN_ACTIVITY -> lat_leader=NONE, lat_confidence=0.
    # ------------------------------------------------------------------
    n_d  = 512
    x_d  = np.full(n_d, 30,  dtype=np.int64)
    y_d  = np.full(n_d, 56,  dtype=np.int64)
    # Spread across bins: ts = 200 + i*BIN_TICKS/2 (sub-bin spacing) so many bins fill.
    ts_d = np.array([200 + i * (BIN_TICKS // 2) for i in range(n_d)], dtype=np.int64)
    pol_d = np.zeros(n_d, dtype=np.int64)
    words_d, latches_d = python_mirror_words(x_d, y_d, ts_d, pol_d)

    n_lat_d = len(latches_d)
    lat1_d  = latches_d[0] if n_lat_d >= 1 else None

    d_ok = lat1_d is not None and lat1_d[0] == LEADER_NONE and lat1_d[2] == 0
    print(f"  (d) ACTIVITY GUARD (right dark): latches={n_lat_d} (want >=1), "
          f"latch1={lat1_d} (want leader=0/NONE, confidence=0) -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) WELL-FORMEDNESS over all words from (a)-(d)
    # leader<=2, lag_mag<=LAG_MAX, confidence<=NBINS, seq<=15,
    # bits[31:23]==0, word < 2**32.
    # ------------------------------------------------------------------
    all_words_e = words_a + words_b + words_c + list(words_d)
    bad_e = []
    for word in all_words_e:
        leader_, lag_, conf_, seq_ = unpack_status(word)
        if leader_ > 2:
            bad_e.append(f"leader={leader_}")
        if lag_ > LAG_MAX:
            bad_e.append(f"lag_mag={lag_}")
        if conf_ > NBINS:
            bad_e.append(f"confidence={conf_}")
        if seq_ > 15:
            bad_e.append(f"seq={seq_}")
        if (word >> 23) != 0:
            bad_e.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_e.append(f"word>=2^32: 0x{word:08x}")
        if bad_e:
            break
    e_ok = len(bad_e) == 0
    print(f"  (e) WELL-FORMEDNESS: {len(all_words_e)} total words; "
          f"leader<=2, lag_mag<={LAG_MAX}, confidence<={NBINS}, "
          f"seq<=15, upper bits=0, word<2^32 -> "
          f"{'OK' if e_ok else 'FAIL: ' + '; '.join(bad_e[:5])}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) WSEQ ARITHMETIC
    # Concatenate (a) and (d): word index i carries
    # wseq == ((i+1)//WINDOW_BATCHES) & WSEQ_MASK.
    # ------------------------------------------------------------------
    x_f   = np.concatenate([x_a,   x_d])
    y_f   = np.concatenate([y_a,   y_d])
    ts_f  = np.concatenate([ts_a,  ts_d])
    pol_f = np.concatenate([pol_a, pol_d])
    words_f, _ = python_mirror_words(x_f, y_f, ts_f, pol_f)

    bad_f = []
    for i, word in enumerate(words_f):
        expected_seq = ((i + 1) // WINDOW_BATCHES) & WSEQ_MASK
        actual_seq   = (word >> 19) & 0xF
        if actual_seq != expected_seq:
            bad_f.append(f"i={i} got={actual_seq} want={expected_seq}")
            if len(bad_f) >= 3:
                break
    f_ok = len(bad_f) == 0
    print(f"  (f) WSEQ ARITHMETIC: {len(words_f)} words; "
          f"every word[i] has seq==((i+1)//{WINDOW_BATCHES})&0xF -> "
          f"{'OK' if f_ok else 'FAIL: ' + '; '.join(bad_f[:3])}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) POPCOUNT32 EXHAUSTIVE
    # For v in 0..255: popcount32(v) == bin(v).count('1').
    # ------------------------------------------------------------------
    g_ok = all(popcount32(v) == bin(v).count('1') for v in range(256))
    print(f"  (g) POPCOUNT32 EXHAUSTIVE (0..255): {'OK' if g_ok else 'FAIL'}")
    ok = ok and g_ok

    # ------------------------------------------------------------------
    # (h) ROTATE_BITS32 SPOT CHECKS
    # rotate_bits32(1, 1) == 2; rotate_bits32(1, NBINS-1) == 1<<(NBINS-1);
    # rotate_bits32(mask, 0) == mask; rotate_bits32(1<<(NBINS-1), 1) == 1.
    # ------------------------------------------------------------------
    mask_full = (1 << NBINS) - 1
    h_ok = (
        rotate_bits32(1, 1) == 2
        and rotate_bits32(1, NBINS - 1) == (1 << (NBINS - 1))
        and rotate_bits32(0xABCDEF12 & mask_full, 0) == (0xABCDEF12 & mask_full)
        and rotate_bits32(1 << (NBINS - 1), 1) == 1
    )
    print(f"  (h) ROTATE_BITS32 SPOT CHECKS: {'OK' if h_ok else 'FAIL'}")
    ok = ok and h_ok

    print()
    print("VALIDATION:",
          "PASS -- left-leads exact; right-leads exact; simultaneous=NONE; "
          "activity guard; well-formedness; wseq arithmetic; "
          "popcount32 exhaustive; rotate_bits32 spot checks"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def load_csv(path, ts_col):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for the timestamp field (default 'le').
    NOTE: recorded captures carry a wrapped coarse counter (not real
    microseconds) so leader calls on captures are qualitative -- --validate
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
                    help="synthetic self-test: left-leads exact, right-leads exact, "
                         "simultaneous=NONE, activity guard, well-formedness, "
                         "wseq arithmetic, popcount exhaustive, rotate spot checks")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le -- NOTE: le is a "
                         "wrapped coarse counter, not real microseconds; leader calls on "
                         "recorded captures are qualitative; --validate builds its own "
                         "synthetic timestamps and is the deterministic check)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the mirror PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing mirror words in Python "
              f"(bit-faithful mirror of firmware).")
        words, _ = python_mirror_words(x, y, ts, pol)
        if words:
            leader_last, lag_last, conf_last, _ = unpack_status(words[-1])
            print(f"final leader={LEADER_NAMES[leader_last & 3]}, "
                  f"lag={lag_last} bins ({lag_last * BIN_TICKS} ticks), "
                  f"confidence={conf_last} ({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_mirror(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
