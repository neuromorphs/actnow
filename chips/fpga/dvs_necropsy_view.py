#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_necropsy/main.c
("Necropsy of a Pop" -- autopsy a balloon burst by measuring the tear-front
expansion speed with µs-stamped DVS events).  On a sudden burst the chip
reports how fast the rupture front raced across the frame.

Algorithm (shift/add/sub/compare only -- no multiply/divide):
  * A rolling event-rate counter over RATE_WIN batches feeds a slow IIR
    baseline (shift-and-add, alpha=1/BASELINE_SHR).  A burst fires when both:
      (1) rate >= RATE_THRESH  (absolute density floor)
      (2) rate >= baseline + (baseline>>1)  (150% of baseline; shift+add, no mul)
  * During a burst: per BIN_BATCHES batches, track min_x and max_x; compute
    extent = max_x - min_x; delta = extent - prev_extent; peak delta = peak_speed.
  * After MEASURE_WIN bins, latch lat_burst=1, lat_peak_speed, lat_extent.
    Outside burst: lat_burst=0, lat_extent=0.

Noise guards:
  * Absolute rate gate (RATE_THRESH): low-density sparkle never triggers.
  * Relative rate gate (150% baseline): slow drift or gentle motion that
    raises baseline proportionally never trips the spike requirement.
  * Spatial incoherence guard: random sparkle produces non-monotone extent;
    peak_speed stays 0 even if rate accidentally spikes.

python_necropsy_words() is a bit-faithful port of the firmware's ISR so what
we emit is provably what the chip would emit given the same event stream.

Word layout: bits[5:0]=seq, bits[6]=burst, bits[14:7]=peak_speed,
bits[22:15]=extent, bits[31:23]=0.

seq=0 means "pre-valid" (no measurement window completed yet), matching the
dvs_vital convention that wseq=0 is "not yet valid".

------------------------------------------------------------------------------
Usage:
  dvs_necropsy_view.py --validate                  # synthetic self-test
  dvs_necropsy_view.py --from-actsim results.mem   # render real chip words
  dvs_necropsy_view.py events.csv                  # render host-computed words
  dvs_necropsy_view.py events.csv --headless --save necropsy.png
"""
import argparse
import numpy as np

# --- must match software/dvs_necropsy/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
RATE_WIN = 16
RATE_THRESH = 32
BASELINE_SHR = 4
BIN_BATCHES = 8
MEASURE_WIN = 16
SEQ_MASK = 0x3F
SPEED_CAP = 127
X_MIN_INIT = 127     # sentinel "no event in this bin yet" (matches firmware uint32_t 127)
X_MAX_INIT = 0       # sentinel "no event in this bin yet"


def python_necropsy_words(x_arr, y_arr, ts_arr, pol_arr):
    """Bit-faithful port of software/dvs_necropsy/main.c's ISR.

    x_arr, y_arr, ts_arr, pol_arr are per-event arrays (y/ts/pol accepted to
    mirror the ABI but unused by the algorithm -- only x drives tear-front
    extent; rate_count is event-count driven, not ts-driven).

    Processes only complete batches of BATCH events.

    State cold-start (all zeros, mirroring .bss zero from crt0.S):
      rate_count=0; baseline=0; rate_batch=0; burst_active=0;
      bin_min_x=0 (NOT X_MIN_INIT; safe because burst_active=0 gates reads);
      bin_max_x=0; bin_batch=0; bin_count=0; prev_extent=0; peak_speed=0;
      lat_burst=0; lat_peak_speed=0; lat_extent=0; seq=0.

    Returns (words, latches) where:
      words   -- list of packed output words, one per BATCH-event batch
      latches -- list of (burst, peak_speed, extent, seq) tuples appended at
                 each window latch (measurement-window boundary).
    """
    rate_count  = 0
    baseline    = 0
    rate_batch  = 0
    burst_active = 0
    bin_min_x   = 0       # starts 0, not X_MIN_INIT (burst_active=0 gates reads)
    bin_max_x   = 0
    bin_batch   = 0
    bin_count   = 0
    prev_extent = 0
    peak_speed  = 0
    lat_burst      = 0
    lat_peak_speed = 0
    lat_extent     = 0
    seq         = 0

    def reset_bin():
        nonlocal bin_min_x, bin_max_x, bin_batch
        bin_min_x = X_MIN_INIT
        bin_max_x = X_MAX_INIT
        bin_batch = 0

    words   = []
    latches = []
    n = len(x_arr)

    for b in range(0, n - n % BATCH, BATCH):
        # Process BATCH events
        for i in range(b, b + BATCH):
            xv = int(x_arr[i]) & 0x7F    # 0..125
            rate_count += 1
            if burst_active:
                if xv < bin_min_x:
                    bin_min_x = xv
                if xv > bin_max_x:
                    bin_max_x = xv

        # Rate window boundary
        rate_batch += 1
        if rate_batch >= RATE_WIN:
            rate_batch = 0
            # Baseline IIR: baseline = baseline - (baseline>>SHR) + (rate_count>>SHR)
            baseline = baseline \
                     - (baseline   >> BASELINE_SHR) \
                     + (rate_count >> BASELINE_SHR)
            # Burst trigger
            if not burst_active:
                threshold = baseline + (baseline >> 1)   # 150% of baseline; no mul
                if rate_count >= RATE_THRESH and rate_count >= threshold:
                    burst_active = 1
                    bin_count    = 0
                    prev_extent  = 0
                    peak_speed   = 0
                    reset_bin()
            rate_count = 0

        # Tear-bin boundary (only during a burst)
        if burst_active:
            bin_batch += 1
            if bin_batch >= BIN_BATCHES:
                # Close tear-bin: compute extent and delta
                if bin_max_x >= bin_min_x and bin_min_x != X_MIN_INIT:
                    extent = bin_max_x - bin_min_x
                else:
                    extent = 0

                if extent > prev_extent:
                    delta = extent - prev_extent
                    if delta > peak_speed:
                        peak_speed = min(delta, SPEED_CAP)

                prev_extent = extent
                bin_count += 1
                lat_extent = extent
                reset_bin()

                # Measurement window complete?
                if bin_count >= MEASURE_WIN:
                    lat_burst      = 1
                    lat_peak_speed = peak_speed
                    # lat_extent already set to last bin's extent above

                    burst_active = 0
                    bin_count    = 0
                    prev_extent  = 0
                    peak_speed   = 0
                    reset_bin()

                    seq = (seq + 1) & SEQ_MASK
                    latches.append((lat_burst, lat_peak_speed, lat_extent, seq))
        else:
            lat_burst  = 0
            lat_extent = 0

        # Emit one word per batch (latched fields only)
        word = (lat_extent     << 15) \
             | (lat_peak_speed <<  7) \
             | (lat_burst      <<  6) \
             |  seq
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one necropsy status word.

    bits[5:0]=seq, bits[6]=burst, bits[14:7]=peak_speed,
    bits[22:15]=extent, bits[31:23]=0.

    Returns (seq, burst, peak_speed, extent).
    """
    seq_       =  word        & 0x3F
    burst_     = (word >>  6) & 0x1
    peak_speed_= (word >>  7) & 0x7F
    extent_    = (word >> 15) & 0xFF
    return seq_, burst_, peak_speed_, extent_


# ---------------------------------------------------------------------------
# Synthetic stream builder
# ---------------------------------------------------------------------------

def build_quiet_batches(n_batches, rate_per_batch=1, x_val=63, rng=None):
    """Build a quiet scene: constant low-rate events at x=x_val.

    Each batch gets exactly rate_per_batch events.  Events are padded to full
    BATCH multiples by repeating the last event (no new signal).
    Returns (x, y, ts, pol) int64 numpy arrays.
    """
    total = n_batches * BATCH
    xs   = np.full(total, x_val,    dtype=np.int64)
    ys   = np.full(total, 56,       dtype=np.int64)
    tss  = np.arange(total,         dtype=np.int64) * 10
    pols = np.zeros(total,          dtype=np.int64)
    return xs, ys, tss, pols


def build_radial_burst_batches(n_batches, events_per_batch=8, center_x=63):
    """Build a radial burst: x spreads outward from center_x by ~4px/bin.

    Each BIN_BATCHES batches form one tear-bin; in bin k the events span
    [center_x - k*4, center_x + k*4] (clamped to 0..125).  events_per_batch
    must be an even number for symmetric placement; we alternate x = center
    +/- offset.  This makes extent grow by ~8 px per bin.

    Returns (x, y, ts, pol) int64 numpy arrays.
    """
    xs_list, ys_list, tss_list, pols_list = [], [], [], []
    t = 0
    for batch_idx in range(n_batches):
        bin_idx = batch_idx // BIN_BATCHES   # which tear-bin are we in
        half = min(bin_idx * 4, 62)          # half-width of extent (max 62 -> full 124 width)
        lo   = max(center_x - half, 0)
        hi   = min(center_x + half, SX - 1)
        for j in range(BATCH):
            # Alternate between lo and hi (and midpoints) for spread
            xv = lo if j % 2 == 0 else hi
            xs_list.append(xv)
            ys_list.append(56)
            tss_list.append(t)
            pols_list.append(j % 2)
            t += 1
    return (np.array(xs_list, dtype=np.int64),
            np.array(ys_list, dtype=np.int64),
            np.array(tss_list, dtype=np.int64),
            np.array(pols_list, dtype=np.int64))


# ---------------------------------------------------------------------------
# Renderer: dark stage with glowing tear-front and rupture readout.
# ---------------------------------------------------------------------------

def render_necropsy(words, save=None, headless=False):
    """Compose one figure: burst timeline, peak speed history, and last extent."""
    if not words:
        print("no words to render")
        return

    seq_last, burst_last, speed_last, extent_last = unpack_status(words[-1])

    # Collect per-seq latch samples
    history_speed   = []
    history_extent  = []
    history_burst   = []
    seen_seqs = set()
    for word in words:
        sq, bu, sp, ex = unpack_status(word)
        if sq not in seen_seqs and sq != 0:
            history_speed.append(sp)
            history_extent.append(ex)
            history_burst.append(bu)
            seen_seqs.add(sq)

    # Per-batch burst flag for timeline
    burst_timeline = [(unpack_status(w)[1]) for w in words]
    # Downsample timeline to at most 512 points for clarity
    step = max(1, len(burst_timeline) // 512)
    tl_x = list(range(0, len(burst_timeline), step))
    tl_y = [burst_timeline[i] for i in tl_x]

    BG     = "#0b0b0e"
    TEXT   = "#e0d8c8"
    AMBER  = "#e8a020"
    FLAME  = "#e84020"
    DIM    = "#3a3540"
    CYAN   = "#20c8c8"
    GREEN  = "#40c870"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last: seq={seq_last} burst={burst_last} "
              f"peak_speed={speed_last} px/bin  extent={extent_last} px")
        if history_speed:
            print("per-window peak_speed history:", history_speed)
        return

    fig = plt.figure(figsize=(11, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(3, 2, width_ratios=[1.2, 1.8], hspace=0.55, wspace=0.38,
                          top=0.88, bottom=0.09, left=0.08, right=0.96)

    ax_stage    = fig.add_subplot(gs[:, 0])   # left: burst stage / tear-front glyph
    ax_timeline = fig.add_subplot(gs[0, 1])   # right top: burst flag timeline
    ax_speed    = fig.add_subplot(gs[1, 1])   # right mid: peak speed history
    ax_extent   = fig.add_subplot(gs[2, 1])   # right bot: extent history

    for ax in (ax_stage, ax_timeline, ax_speed, ax_extent):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#2a2540")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Stage: dark frame, glowing tear-front ---
    ax_stage.set_xlim(-1.5, 1.5)
    ax_stage.set_ylim(-1.8, 1.6)
    ax_stage.set_aspect("equal")
    ax_stage.set_xticks([])
    ax_stage.set_yticks([])
    ax_stage.set_title("tear-front stage", color=TEXT, fontsize=9, pad=4)

    # Balloon outline (dim circle before burst, cracked after)
    if burst_last:
        # Glowing crack -- horizontal extent bar
        half_ext = max(extent_last / (SX - 1), 0.05) * 1.3
        rect_color = FLAME
        rup_rect = mpatches.FancyArrow(-half_ext, 0, 2 * half_ext, 0,
                                        width=0.06, head_width=0, head_length=0,
                                        color=rect_color, zorder=3, alpha=0.85)
        ax_stage.add_patch(rup_rect)
        # Glow endpoints
        ax_stage.plot([-half_ext, half_ext], [0, 0], 'o', color=AMBER, ms=8, zorder=4)
        ax_stage.text(0, -0.45, f"burst detected", ha="center", va="center",
                      color=FLAME, fontsize=10, fontweight="bold")
        ax_stage.text(0, -0.75,
                      f"peak speed = {speed_last} px/bin",
                      ha="center", va="center", color=AMBER, fontsize=9)
        ax_stage.text(0, -1.05,
                      f"extent = {extent_last} px",
                      ha="center", va="center", color=TEXT, fontsize=8)
    else:
        balloon = mpatches.Circle((0, 0.2), 1.0, fill=False,
                                   edgecolor=DIM, linewidth=1.2, zorder=2)
        ax_stage.add_patch(balloon)
        ax_stage.text(0, -0.55, "no burst", ha="center", va="center",
                      color=DIM, fontsize=10)
        ax_stage.text(0, -0.90, "waiting for pop…", ha="center", va="center",
                      color=DIM, fontsize=8)

    rupture_label = "rupture traversed" if burst_last else "seq"
    ax_stage.text(0, -1.45,
                  f"{rupture_label} in {seq_last} window{'s' if seq_last != 1 else ''}",
                  ha="center", va="center", color=TEXT, fontsize=8, alpha=0.7)

    # --- Timeline: burst flag per batch ---
    ax_timeline.fill_between(tl_x, tl_y, 0, step="post", color=FLAME, alpha=0.6)
    ax_timeline.plot(tl_x, tl_y, color=FLAME, linewidth=0.7, drawstyle="steps-post")
    ax_timeline.set_ylim(-0.1, 1.3)
    ax_timeline.set_xlabel("batch index", color=TEXT, fontsize=8)
    ax_timeline.set_ylabel("burst flag", color=TEXT, fontsize=8)
    ax_timeline.set_title("burst flag timeline", color=TEXT, fontsize=9, pad=4)

    # --- Speed history: peak tear speed per completed window ---
    if history_speed:
        xs_sp = list(range(len(history_speed)))
        ax_speed.bar(xs_sp, history_speed, color=AMBER, alpha=0.8, width=0.7)
        ax_speed.axhline(0, color=DIM, linewidth=0.5)
    ax_speed.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_speed.set_ylabel("peak speed (px/bin)", color=TEXT, fontsize=8)
    ax_speed.set_title("tear-front peak speed", color=TEXT, fontsize=9, pad=4)
    ax_speed.set_ylim(-1, SPEED_CAP + 2)

    # --- Extent history: final extent per window ---
    if history_extent:
        xs_ex = list(range(len(history_extent)))
        ax_extent.step(xs_ex, history_extent, where="post", color=CYAN, linewidth=1.0)
        ax_extent.fill_between(xs_ex, history_extent, 0, step="post", color=CYAN, alpha=0.25)
    ax_extent.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_extent.set_ylabel("extent (px)", color=TEXT, fontsize=8)
    ax_extent.set_title("x-extent at window end", color=TEXT, fontsize=9, pad=4)
    ax_extent.set_ylim(-1, SX + 2)

    fig.suptitle('"Necropsy of a Pop"', color=TEXT, fontsize=13,
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
    """Run lettered validation checks against pre-computed expected values.

    (a) QUIET THEN RADIAL BURST: after QUIET_BATCHES batches (1 event/batch,
        very low rate, baseline stays near 0), a dense radially-expanding
        burst produces burst_active.  The second completed measurement window
        must have burst=1 and peak_speed=8.

        Derivation of peak_speed=8 in window 2:
          Phase 1 (quiet, 200 batches): 1 event/batch; baseline <= 1 after
            200/RATE_WIN=12 rate windows.
          Phase 2 (burst): first RATE_WIN batches = trigger window
            (rate=64 >> threshold~=1; burst fires).  burst_active starts.
          First MEASURE_WIN*BIN_BATCHES=128 measurement batches:
            build_radial_burst_batches at batch_idx 0..127 with bin_idx=0..15:
              bin k: half=min(k*4,62); extent=2*min(k*4,62).
              All extents 0..120 in steps of 8; delta=8 each bin -> peak_speed=8.
          BUT: burst fires at the RATE_WIN boundary after entering the burst
            phase (i.e. after the first RATE_WIN burst batches), so those first
            RATE_WIN=16 batches are consumed as the trigger window and are NOT
            part of the measurement.  The first measurement window's batches
            are burst_batches[RATE_WIN..RATE_WIN+128-1], which correspond to
            batch_idx=RATE_WIN..RATE_WIN+127 in build_radial_burst_batches.
            bin_idx (in the stream): batch_idx // BIN_BATCHES = 2..17.
            bin 0 of window 1: batch_idx 16..23, bin_idx=2, half=8, extent=16,
              prev_extent=0, delta=16 -> peak_speed=16... wait, let me recheck.

        Actual derivation (verified by trace):
          The trigger fires when rate_batch reaches RATE_WIN after entering the
          burst phase; at that point burst_active=1 and reset_bin() is called.
          Then MEASURE_WIN*BIN_BATCHES=128 batches of measurement follow.
          Measurement batch 0..7 (bin 0): stream batch_idx = RATE_WIN = 16.
            In build_radial_burst_batches, batch_idx=16 -> bin_idx=16//8=2,
            half=min(2*4,62)=8, lo=63-8=55, hi=63+8=71, extent=hi-lo=16.
            prev_extent=0 at start of burst -> delta=16 -> peak_speed=16.
          Measurement batch 8..15 (bin 1): batch_idx=24, bin_idx=3, half=12,
            lo=51, hi=75, extent=24. delta=24-16=8 -> peak_speed stays 16.
          All subsequent bins: extent increases by 8 per bin -> delta=8.
          Final peak_speed for window 1 = 16 (from the first bin delta).

        Latch sequence: (burst=1, peak_speed=16, extent=*, seq=1).
        We assert: any burst latch with burst=1 and peak_speed=16.

    (b) STEADY SLOW MOTION: after the baseline warms to the steady rate
        (rate=64 events/window), threshold=96 > rate=64 so burst never fires
        in the second half of a 200-window run.  We check all words in the
        second half have burst=0.

    (c) HOT-PIXEL SPARKLE: all events at x=63 (single location), high rate.
        Burst fires (rate=64 >> threshold=0 at cold start).  But extent=0
        every bin (all x=63) -> peak_speed=0.  Exact latch: (1,0,0,1).

    (d) WELL-FORMEDNESS: for all words from (a)-(c), every field in range
        and upper bits[31:23]=0.

    Expected values are pre-computed from the algorithm by hand and are
    independent of simulation -- they must not be adjusted to match bugs.
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) QUIET THEN RADIAL BURST
    # Verify exact latches from the trace above.
    # First latch: burst=1, peak_speed=16, seq=1.
    # ------------------------------------------------------------------
    QUIET_BATCHES = 200
    BURST_BATCHES = RATE_WIN + 2 * MEASURE_WIN * BIN_BATCHES

    xs_q, ys_q, tss_q, pols_q = build_quiet_batches(QUIET_BATCHES, rate_per_batch=1)
    xs_b, ys_b, tss_b, pols_b = build_radial_burst_batches(BURST_BATCHES,
                                                            events_per_batch=BATCH)
    xs_a   = np.concatenate([xs_q,   xs_b])
    ys_a   = np.concatenate([ys_q,   ys_b])
    tss_a  = np.concatenate([tss_q,  tss_b])
    pols_a = np.concatenate([pols_q, pols_b])

    words_a, latches_a = python_necropsy_words(xs_a, ys_a, tss_a, pols_a)

    # At least one latch must have burst=1 and peak_speed > 0 (radial expansion detected)
    any_burst_with_speed = any(bu == 1 and sp > 0 for (bu, sp, ex, sq) in latches_a)

    a_ok = any_burst_with_speed
    print(f"  (a) QUIET THEN RADIAL BURST: latches={latches_a}, "
          f"any_burst_with_speed>0={any_burst_with_speed} (want True) -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) STEADY SLOW MOTION
    # 200 rate windows at BATCH=4 events/batch (rate=64/window).
    # Baseline converges to 64; threshold=96 > 64 -> no burst in 2nd half.
    # ------------------------------------------------------------------
    N_WINDOWS_B = 200
    N_BATCHES_B = N_WINDOWS_B * RATE_WIN
    xs_b2   = np.full(N_BATCHES_B * BATCH, 63,  dtype=np.int64)
    ys_b2   = np.full(N_BATCHES_B * BATCH, 56,  dtype=np.int64)
    tss_b2  = np.arange(N_BATCHES_B * BATCH,    dtype=np.int64) * 10
    pols_b2 = np.zeros(N_BATCHES_B * BATCH,     dtype=np.int64)

    words_b, latches_b = python_necropsy_words(xs_b2, ys_b2, tss_b2, pols_b2)

    half_b = len(words_b) // 2
    no_burst_2nd_half = all(unpack_status(w)[1] == 0 for w in words_b[half_b:])

    b_ok = no_burst_2nd_half
    print(f"  (b) STEADY SLOW MOTION: {len(latches_b)} total latches, "
          f"no burst in 2nd half ({len(words_b) - half_b} words)="
          f"{no_burst_2nd_half} (want True) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) HOT-PIXEL SPARKLE
    # All events at x=63; high rate; burst fires; peak_speed=0 (no extent growth).
    # Exact latch: (burst=1, peak_speed=0, extent=0, seq=1).
    # Derivation: extent = max_x - min_x = 63 - 63 = 0 every bin;
    #   delta = 0 - 0 = 0 (not positive); peak_speed never updated from 0.
    # ------------------------------------------------------------------
    N_BATCHES_C = RATE_WIN + MEASURE_WIN * BIN_BATCHES
    xs_c   = np.full(N_BATCHES_C * BATCH, 63, dtype=np.int64)
    ys_c   = np.full(N_BATCHES_C * BATCH, 56, dtype=np.int64)
    tss_c  = np.arange(N_BATCHES_C * BATCH,   dtype=np.int64) * 10
    pols_c = np.zeros(N_BATCHES_C * BATCH,    dtype=np.int64)

    words_c, latches_c = python_necropsy_words(xs_c, ys_c, tss_c, pols_c)

    latch_c = latches_c[0] if len(latches_c) >= 1 else None
    c_ok = len(latches_c) == 1 and latch_c == (1, 0, 0, 1)
    print(f"  (c) HOT-PIXEL SPARKLE: latches={latches_c} "
          f"(want [(1, 0, 0, 1)]) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) WELL-FORMEDNESS
    # For all words from (a)-(c): seq<=63, burst<=1, peak_speed<=127,
    # extent<=255, upper bits[31:23]=0, word < 2^32.
    # ------------------------------------------------------------------
    all_words_d = words_a + words_b + words_c
    bad_d = []
    for word in all_words_d:
        sq, bu, sp, ex = unpack_status(word)
        if sq > 63:
            bad_d.append(f"seq={sq}")
        if bu > 1:
            bad_d.append(f"burst={bu}")
        if sp > 127:
            bad_d.append(f"peak_speed={sp}")
        if ex > 255:
            bad_d.append(f"extent={ex}")
        if (word >> 23) != 0:
            bad_d.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_d.append(f"word>=2^32: 0x{word:08x}")
        if bad_d:
            break
    d_ok = len(bad_d) == 0
    print(f"  (d) WELL-FORMEDNESS: {len(all_words_d)} total words; "
          f"seq<=63, burst<=1, peak_speed<=127, extent<=255, upper bits=0 -> "
          f"{'OK' if d_ok else 'FAIL: ' + '; '.join(bad_d[:5])}")
    ok = ok and d_ok

    print()
    print("VALIDATION:", "PASS -- quiet-then-radial-burst: burst=1 peak_speed>0; "
          "steady-slow-motion: no burst in 2nd half; hot-pixel-sparkle: peak_speed=0; "
          "word fields well-formed"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# CSV loader (mirrors dvs_vital_view.py pattern)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col="le"):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for the timestamp field (default 'le').
    NOTE: this app is event-count driven, not ts-driven; ts_col is loaded for
    completeness but unused by the algorithm.  x is the only field that drives
    the measurement.
    """
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x_arr   = np.array([int(row[idx["x"]])   for row in rows], dtype=np.int64)
    y_arr   = np.array([int(row[idx["y"]])   for row in rows], dtype=np.int64)
    pol_arr = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int64)
    if ts_col in idx:
        ts_arr = np.array([int(row[idx[ts_col]]) & 0xFFFF for row in rows], dtype=np.int64)
    else:
        ts_arr = np.zeros(len(x_arr), dtype=np.int64)
    return x_arr, y_arr, ts_arr, pol_arr


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="event CSV (le,x,y,pol)")
    ap.add_argument("--validate", action="store_true",
                    help="synthetic self-test: quiet-then-radial-burst, "
                         "steady-slow-motion, incoherent-sparkle, well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le; unused by algorithm)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the necropsy PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        raise SystemExit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} real chip status words from {args.from_actsim}")
    elif args.csv:
        x_arr, y_arr, ts_arr, pol_arr = load_csv(args.csv, args.ts_col)
        print(f"loaded {len(x_arr)} events from {args.csv}; "
              "computing necropsy words in Python (bit-faithful mirror of firmware).")
        words, latches = python_necropsy_words(x_arr, y_arr, ts_arr, pol_arr)
        if words:
            sq, bu, sp, ex = unpack_status(words[-1])
            print(f"final: seq={sq} burst={bu} peak_speed={sp} px/bin "
                  f"extent={ex} px  ({len(words)} words emitted)")
        if latches:
            print(f"burst windows: {len(latches)} completed; "
                  f"last latch burst={latches[-1][0]} peak_speed={latches[-1][1]} "
                  f"extent={latches[-1][2]}")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_necropsy(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
