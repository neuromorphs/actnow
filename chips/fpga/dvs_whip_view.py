#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_whip/main.c
("The Whipcracker" -- measures traveling-wave speed along a flicked rope and
certifies whether the tip broke the sound barrier.  Per-column leaky activity
counters reject hot-pixel noise; epoch-based activation gates prevent a static
pixel from masquerading as a moving front.  The wavefront hop classifier uses
a precomputed LUT of max-Δt thresholds per (Δcol, speed-bin), implemented
entirely with shifts and compares -- no multiply or divide ever used).

python_whip_words() is a bit-faithful port of the firmware ISR so that what
we emit is provably what the chip would emit given the same event stream.

Output word layout:
  bit     0       = valid   (1 = at least MIN_HOPS classified hops in window)
  bits[ 4: 1]     = seq     (4-bit window sequence counter, wraps mod 16)
  bits[11: 5]     = front_col (current activation column, 0..125)
  bit    12       = sonic   (1 = any hop this window was fast enough)
  bits[16:13]     = maxspeedbin (0..15, highest speed bin reached this window)
  bits[31:17]     = 0

Speed bins: bin 0 = slowest (dt < dc<<15), bin 15 = SONIC (dt < dc, i.e.
one tick per column or faster).  Sonic = bin 15 reached.

------------------------------------------------------------------------------
Usage:
  dvs_whip_view.py --validate                  # synthetic self-test
  dvs_whip_view.py --from-actsim results.mem   # render real chip status words
  dvs_whip_view.py events.csv --ts-col le      # render host-computed from CSV
  dvs_whip_view.py ... --headless --save whip.png
"""
import argparse
import numpy as np

# --- must match software/dvs_whip/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
TS_MASK = 0xFFFF

NSPEEDBINS    = 16
MAX_DC        = 16
ACT_THRESH    = 8
ACT_CAP       = 200
ACT_LEAK      = 3
LEAK_EPOCH    = 64
EPOCH_BATCHES = 32
MIN_HOPS      = 2
WINDOW_BATCHES = 256
WSEQ_MASK     = 0xF
EPOCH_MASK    = 0xFF


# ---------------------------------------------------------------------------
# LUT: mirror of lut_max_dt[dc][s] = dc << (NSPEEDBINS-1-s), clamped to 0xFFFF
# ---------------------------------------------------------------------------

def _make_lut():
    lut = {}
    for dc in range(MAX_DC + 1):
        row = []
        for s in range(NSPEEDBINS):
            shift = NSPEEDBINS - 1 - s
            val = dc << shift
            if val > 0xFFFF:
                val = 0xFFFF
            row.append(val)
        lut[dc] = row
    return lut

LUT_MAX_DT = _make_lut()


def classify_hop(dc, dt):
    """Return highest bin s s.t. dt < LUT_MAX_DT[dc][s], or -1 if none.

    Mirrors firmware's classify_hop(): walk s from NSPEEDBINS-1 down to 0.
    Bin 15 (sonic) requires dt < dc.
    """
    if dc < 1 or dc > MAX_DC:
        return -1
    row = LUT_MAX_DT[dc]
    for s in range(NSPEEDBINS - 1, -1, -1):
        if dt < row[s]:
            return s
    return -1


# ---------------------------------------------------------------------------
# Bit-faithful firmware mirror
# ---------------------------------------------------------------------------

def python_whip_words(x_arr, y_arr, ts_arr, pol_arr):
    """Bit-faithful port of software/dvs_whip/main.c's ISR.

    Parameters: per-event arrays x, y, ts, pol (numpy int64 or plain lists).
    Processes only complete batches (n - n%BATCH events).

    State cold-start all zeros:
      col_act=[0]*SX; col_epoch=[0]*SX; prev_col=0; prev_col_ts=0;
      prev_col_valid=0; window_maxbin=0; window_sonic=0; window_hops=0;
      window_front_col=0; lat_valid=0; lat_seq=0; lat_front_col=0;
      lat_sonic=0; lat_maxspeedbin=0; batch_in_window=0; wseq=0;
      epoch=0; leak_counter=0.

    Returns (words, latches) where latches is a list of
    (valid, sonic, maxspeedbin, front_col, seq) tuples appended at each
    window latch.
    """
    col_act        = [0] * SX
    col_epoch      = [0] * SX
    prev_col       = 0
    prev_col_ts    = 0
    prev_col_valid = 0
    window_maxbin  = 0
    window_sonic   = 0
    window_hops    = 0
    window_front_col = 0
    lat_valid      = 0
    lat_seq        = 0
    lat_front_col  = 0
    lat_sonic      = 0
    lat_maxspeedbin = 0
    batch_in_window = 0
    wseq           = 0
    epoch          = 0
    leak_counter   = 0

    words   = []
    latches = []
    n = len(x_arr)

    for b in range(0, n - n % BATCH, BATCH):
        # Advance leak counter (mirrors firmware: done BEFORE event processing)
        leak_counter += 1
        if leak_counter >= LEAK_EPOCH:
            leak_counter = 0
            for c in range(SX):
                col_act[c] >>= ACT_LEAK

        # Process BATCH events
        for i in range(b, b + BATCH):
            xi  = int(x_arr[i])
            ts  = int(ts_arr[i]) & TS_MASK

            if xi < 0 or xi >= SX:
                continue

            # Accumulate activity (saturating)
            if col_act[xi] < ACT_CAP:
                col_act[xi] += 1

            # Activity threshold guard
            if col_act[xi] < ACT_THRESH:
                continue

            # Static-pixel guard: column may only activate once per epoch
            if col_epoch[xi] == (epoch & EPOCH_MASK):
                continue

            # Mark column activated in this epoch
            col_epoch[xi] = epoch & EPOCH_MASK

            # Track current front column
            window_front_col = xi

            # Attempt to classify the hop from prev_col to xi
            if prev_col_valid:
                if xi > prev_col:
                    dc = xi - prev_col
                elif xi < prev_col:
                    dc = prev_col - xi
                else:
                    dc = 0

                if 1 <= dc <= MAX_DC:
                    dt = (ts - prev_col_ts) & TS_MASK
                    if dt == 0:
                        dt = 1
                    bin_s = classify_hop(dc, dt)
                    if bin_s >= 0:
                        if bin_s > window_maxbin:
                            window_maxbin = bin_s
                        if bin_s == NSPEEDBINS - 1:
                            window_sonic = 1
                        window_hops += 1

            prev_col       = xi
            prev_col_ts    = ts
            prev_col_valid = 1

        # Advance batch-within-window counter
        batch_in_window += 1

        # Epoch advance every EPOCH_BATCHES batches
        if batch_in_window % EPOCH_BATCHES == 0:
            epoch = (epoch + 1) & EPOCH_MASK

        # Window boundary: latch BEFORE emit
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0

            lat_valid       = 1 if window_hops >= MIN_HOPS else 0
            lat_sonic       = window_sonic
            lat_maxspeedbin = window_maxbin
            lat_front_col   = window_front_col

            # Clear per-window accumulators; prev_col/prev_col_valid persist
            window_maxbin    = 0
            window_sonic     = 0
            window_hops      = 0

            wseq    = (wseq + 1) & WSEQ_MASK
            lat_seq = wseq

            latches.append((lat_valid, lat_sonic, lat_maxspeedbin,
                            lat_front_col, lat_seq))

        # Emit one word per batch from latched values
        word = ((lat_maxspeedbin << 13) |
                (lat_sonic       << 12) |
                (lat_front_col   <<  5) |
                (lat_seq         <<  1) |
                 lat_valid)
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one whip status word.

    bit[0]=valid, bits[4:1]=seq, bits[11:5]=front_col,
    bit[12]=sonic, bits[16:13]=maxspeedbin, bits[31:17]=0.
    Returns (valid, seq, front_col, sonic, maxspeedbin).
    """
    valid       =  word        & 0x1
    seq         = (word >>  1) & 0xF
    front_col   = (word >>  5) & 0x7F
    sonic       = (word >> 12) & 0x1
    maxspeedbin = (word >> 13) & 0xF
    return valid, seq, front_col, sonic, maxspeedbin


# ---------------------------------------------------------------------------
# Synthetic event stream builders for validation
# ---------------------------------------------------------------------------

def build_validated_sweep(dt_per_hop, sweep_cols=(0, 2, 4, 6)):
    """Build one window worth of events that produces a valid sweep.

    Design (mirrors noise-guard analysis in firmware header):

    Phase 1 -- WARMUP (200 interleaved batches): cycle through sweep_cols
    one event per col per batch so col_act[c] stays >= ACT_THRESH after each
    leak pass.  Warmup events DO pass the epoch guard once ACT_THRESH is
    crossed (they activate in each new epoch); this is normal and means
    col_epoch[c] == current epoch at the end of warmup.

    Phase 2 -- EPOCH GAP (24 idle batches): out-of-range events (x=200 >=
    SX=126) advance batch_in_window and epoch without touching col_epoch.
    Epoch advances at batch 224 (epoch=7); col_epoch[c]=6 from warmup,
    so the gap crosses exactly one epoch boundary.  col_act[c] is still
    >= ACT_THRESH at batch 224 (last leak was at batch 192; warmup added
    enough counts to survive until then).

    Phase 3 -- SWEEP (1 batch): one event per sweep column, dt_per_hop ticks
    apart.  epoch=7 != col_epoch[c]=6 -> epoch guard passes.
    BATCH=4 events per batch; sweep_cols has 4 entries:
      event 0: col[0] -> prev_col_valid may be 1 from warmup; if prev_col!=0
               sets new reference.  NOTE: prev_col and prev_col_ts persist
               across windows but col[0] arrival refreshes the reference.
      event 1..3: each produces a hop with dc=2, dt=dt_per_hop -> classified.
    At least 3 hops produced >= MIN_HOPS=2.

    Phase 4 -- PAD (31 batches): out-of-range events pad to 256 total batches
    = WINDOW_BATCHES, triggering the window latch.

    Total batches: 200 warmup + 24 gap + 1 sweep + 31 pad = 256.

    Returns (x, y, ts, pol) as numpy int64 arrays.
    """
    assert len(sweep_cols) == BATCH, "sweep_cols must have exactly BATCH entries"
    xs, ys, tss, pols = [], [], [], []
    t = 1000

    # Phase 1: warmup -- 200 batches, one event per sweep col per batch
    for _ in range(200):
        for c in sweep_cols:
            xs.append(c); ys.append(56); tss.append(t & TS_MASK); pols.append(0)
            t += 2

    # Phase 2: epoch gap -- 24 idle batches of out-of-range events
    # (x=200 > SX=126 dropped by `if xi >= SX`; advances epoch past warmup epoch)
    for _ in range(24):
        for _ in range(BATCH):
            xs.append(200); ys.append(56); tss.append(t & TS_MASK); pols.append(0)
            t += 1

    # Phase 3: sweep -- one event per sweep col, dt_per_hop ticks apart
    t_sweep = t
    for c in sweep_cols:
        xs.append(c); ys.append(56); tss.append(t_sweep & TS_MASK); pols.append(0)
        t_sweep = (t_sweep + dt_per_hop) & TS_MASK

    # Phase 4: pad -- 31 batches of out-of-range events to reach 256 batches
    t_pad = t_sweep + 10
    for _ in range(31):
        for _ in range(BATCH):
            xs.append(200); ys.append(56); tss.append(t_pad & TS_MASK); pols.append(0)
            t_pad += 1

    return (np.array(xs,   dtype=np.int64),
            np.array(ys,   dtype=np.int64),
            np.array(tss,  dtype=np.int64),
            np.array(pols, dtype=np.int64))


def build_hotpixel_stream(col, n_events=4096):
    """Build a stream of events all from the same column (static hot pixel).

    Events are spaced at 200 ticks to easily exceed ACT_THRESH but since
    dc=0 on every hop, no classified hop ever fires and valid stays 0.
    """
    xs   = np.full(n_events, col, dtype=np.int64)
    ys   = np.full(n_events, 56, dtype=np.int64)
    tss  = np.array([(1000 + 200 * i) & TS_MASK for i in range(n_events)],
                    dtype=np.int64)
    pols = np.zeros(n_events, dtype=np.int64)
    return xs, ys, tss, pols


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks.  Returns True if all pass.

    (a) Fast sweep (sonic, dt=1 per hop, dc=2) -> sonic=1, valid=1,
        maxspeedbin = NSPEEDBINS-1 = 15.
        classify_hop(2, 1): lut[2][15]=2, dt=1<2 -> bin 15 (sonic).
    (b) Slow sweep (subsonic, dt=3 per hop, dc=2) -> sonic=0, valid=1,
        maxspeedbin < 15.
        classify_hop(2, 3): lut[2][15]=2, dt=3>=2 -> not bin15;
        lut[2][14]=4, dt=3<4 -> bin 14.
    (c) Static hot pixel (same column fires repeatedly) -> no moving front,
        dc=0 on all hops -> window_hops stays 0 < MIN_HOPS -> valid=0.
    (d) Well-formedness: all word fields within legal ranges,
        upper bits[31:17]=0, word < 2^32.
    (e) LUT spot checks: verify classify_hop at key boundary values.
        Sonic bin 15 for dc=2 requires dt < lut[2][15]=2, so dt=1 is sonic.
        classify_hop(1,1): lut[1][15]=1, dt=1 not < 1 -> not bin15;
        lut[1][14]=2, dt=1 < 2 -> bin 14.
    """
    ok = True

    # --- (e) LUT spot checks first (foundational) ---
    e1 = classify_hop(2, 1) == 15    # sonic: dt=1 < lut[2][15]=2
    e2 = classify_hop(2, 2) < 15     # not sonic: dt=2 not < lut[2][15]=2
    e3 = classify_hop(1, 1) == 14    # dt=1 < lut[1][14]=2 but not < lut[1][15]=1
    e4 = classify_hop(4, 1) == 15    # dt=1 < lut[4][15]=4 -> sonic
    e5 = classify_hop(1, 0x7FFF) == 0  # lut[1][0]=32768; dt=32767 < 32768 -> bin 0
    e6 = classify_hop(17, 1) == -1   # dc > MAX_DC -> invalid

    lut_spot_ok = e1 and e2 and e3 and e4 and e5 and e6
    print(f"  (e) LUT spot checks: "
          f"classify_hop(2,1)==15={e1}, "
          f"classify_hop(2,2)<15={e2}, "
          f"classify_hop(1,1)==14={e3}, "
          f"classify_hop(4,1)==15={e4}, "
          f"classify_hop(1,0x7FFF)==0={e5}, "
          f"classify_hop(17,1)==-1={e6} -> "
          f"{'OK' if lut_spot_ok else 'FAIL'}")
    ok = ok and lut_spot_ok

    # --- (a) FAST SWEEP (sonic): dt=1 per hop, dc=2 -> bin 15 ---
    xa, ya, tsa, pa = build_validated_sweep(dt_per_hop=1, sweep_cols=(0, 2, 4, 6))
    words_a, latches_a = python_whip_words(xa, ya, tsa, pa)

    sonic_latches_a = [l for l in latches_a if l[0] == 1]
    if sonic_latches_a:
        latch_a = sonic_latches_a[-1]
        a_sonic  = latch_a[1]
        a_maxbin = latch_a[2]
    else:
        a_sonic  = -1
        a_maxbin = -1

    a_ok = (len(sonic_latches_a) > 0 and a_sonic == 1 and a_maxbin == NSPEEDBINS - 1)
    print(f"  (a) FAST SWEEP (sonic): valid latches={len(sonic_latches_a)}, "
          f"sonic={a_sonic} (want 1), maxspeedbin={a_maxbin} (want {NSPEEDBINS-1}) -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # --- (b) SLOW SWEEP (subsonic): dt=3 per hop, dc=2 -> bin 14 ---
    xb, yb, tsb, pb = build_validated_sweep(dt_per_hop=3, sweep_cols=(0, 2, 4, 6))
    words_b, latches_b = python_whip_words(xb, yb, tsb, pb)

    valid_latches_b = [l for l in latches_b if l[0] == 1]
    if valid_latches_b:
        latch_b  = valid_latches_b[-1]
        b_sonic  = latch_b[1]
        b_maxbin = latch_b[2]
    else:
        b_sonic  = -1
        b_maxbin = -1

    b_ok = (len(valid_latches_b) > 0 and b_sonic == 0 and 0 < b_maxbin < NSPEEDBINS - 1)
    print(f"  (b) SLOW SWEEP (subsonic): valid latches={len(valid_latches_b)}, "
          f"sonic={b_sonic} (want 0), maxspeedbin={b_maxbin} "
          f"(want 0 < x < {NSPEEDBINS-1}) -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # --- (c) STATIC HOT PIXEL ---
    xc, yc, tsc, pc = build_hotpixel_stream(col=63, n_events=4096)
    words_c, latches_c = python_whip_words(xc, yc, tsc, pc)

    all_invalid_c = all(l[0] == 0 for l in latches_c)
    no_sonic_c    = all(l[1] == 0 for l in latches_c)

    c_ok = all_invalid_c and no_sonic_c
    print(f"  (c) STATIC HOT PIXEL: {len(latches_c)} latches, "
          f"all valid=0={all_invalid_c}, all sonic=0={no_sonic_c} -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # --- (d) WELL-FORMEDNESS ---
    all_words_d = words_a + words_b + list(words_c)
    bad_d = []
    for word in all_words_d:
        valid_, seq_, front_col_, sonic_, maxbin_ = unpack_status(word)
        if valid_ > 1:
            bad_d.append(f"valid={valid_}")
        if seq_ > 15:
            bad_d.append(f"seq={seq_}")
        if front_col_ > 125:
            bad_d.append(f"front_col={front_col_}")
        if sonic_ > 1:
            bad_d.append(f"sonic={sonic_}")
        if maxbin_ > 15:
            bad_d.append(f"maxbin={maxbin_}")
        if (word >> 17) != 0:
            bad_d.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_d.append(f"word>=2^32: 0x{word:08x}")
        if bad_d:
            break
    d_ok = len(bad_d) == 0
    print(f"  (d) WELL-FORMEDNESS: {len(all_words_d)} total words; "
          f"all fields in range, upper bits=0, word<2^32 -> "
          f"{'OK' if d_ok else 'FAIL: ' + '; '.join(bad_d[:5])}")
    ok = ok and d_ok

    print()
    print("VALIDATION:", "PASS -- fast sweep sonic certified; slow sweep subsonic; "
          "static hot-pixel guarded (valid=0); LUT spot checks correct; "
          "word fields well-formed"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# Renderer: Mach-meter + SONIC BOOM badge
# ---------------------------------------------------------------------------

def render_whip(words, save=None, headless=False):
    """Compose one figure: Mach-meter gauge + sonic badge + front-col history."""
    if not words:
        print("no words to render")
        return

    valid_last, seq_last, front_col_last, sonic_last, maxbin_last = \
        unpack_status(words[-1])

    # Collect per-window samples (one per seq change)
    history_bin   = []
    history_sonic = []
    history_col   = []
    prev_seq = None
    for word in words:
        v_, s_, fc_, so_, mb_ = unpack_status(word)
        if s_ != prev_seq:
            history_bin.append(mb_)
            history_sonic.append(so_)
            history_col.append(fc_)
            prev_seq = s_

    BG     = "#0b0d14"
    TEXT   = "#e8dfc8"
    ORANGE = "#f0882a"
    RED    = "#d43030"
    GREEN  = "#5fd48a"
    GOLD   = "#e8b84b"
    STEEL  = "#8a94a6"
    DIM    = "#373a4a"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import FancyArrowPatch
        import math
    except Exception as e:
        print("matplotlib unavailable:", e)
        mach = maxbin_last / (NSPEEDBINS - 1)
        print(f"last: valid={valid_last} sonic={sonic_last} "
              f"maxspeedbin={maxbin_last} ({mach:.2f} Mach) "
              f"front_col={front_col_last}")
        if history_bin:
            print("per-window maxspeedbin history:", history_bin)
        return

    fig = plt.figure(figsize=(11, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1.1, 1.4], wspace=0.32,
                          top=0.88, bottom=0.09, left=0.07, right=0.96)
    ax_left  = fig.add_subplot(gs[0])
    ax_right = fig.add_subplot(gs[1])

    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_top   = fig.add_subplot(gs_right[0])
    ax_bot   = fig.add_subplot(gs_right[1])
    ax_right.remove()

    for ax in (ax_left, ax_top, ax_bot):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#2a2d3a")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left panel: Mach-meter gauge ---
    ax_left.set_xlim(-1.6, 1.6)
    ax_left.set_ylim(-1.6, 1.6)
    ax_left.set_aspect("equal")
    ax_left.set_xticks([])
    ax_left.set_yticks([])
    ax_left.set_title("Mach meter", color=TEXT, fontsize=9, pad=4)

    import math

    # Draw arc background (180 degrees from left to right, bottom = 0 speed)
    import numpy as np_local
    theta_min = math.pi           # 180° = left = zero speed
    theta_max = 0.0               # 0°  = right = max speed
    thetas = np_local.linspace(theta_min, theta_max, 200)
    r_arc = 1.1
    ax_left.plot(r_arc * np_local.cos(thetas), r_arc * np_local.sin(thetas),
                 color=STEEL, linewidth=3, alpha=0.5)

    # Tick marks at each speed bin
    for s in range(NSPEEDBINS):
        frac = s / (NSPEEDBINS - 1)
        theta = theta_min + frac * (theta_max - theta_min)
        r1, r2 = 0.95, 1.1
        ax_left.plot([r1 * math.cos(theta), r2 * math.cos(theta)],
                     [r1 * math.sin(theta), r2 * math.sin(theta)],
                     color=STEEL if s < NSPEEDBINS - 1 else RED, linewidth=1.2)
        if s in (0, 4, 8, 12, 15):
            ax_left.text(1.28 * math.cos(theta), 1.28 * math.sin(theta),
                         f"M{s/15:.1f}", ha="center", va="center",
                         color=RED if s == 15 else STEEL, fontsize=6.5)

    # Needle: points from centre to the arc at the current maxbin position
    if maxbin_last == 0 and not valid_last:
        needle_frac = 0.0
    else:
        needle_frac = maxbin_last / (NSPEEDBINS - 1)

    needle_theta = theta_min + needle_frac * (theta_max - theta_min)
    needle_color = RED if sonic_last else (ORANGE if maxbin_last > NSPEEDBINS // 2
                                           else GREEN)
    ax_left.annotate("",
                     xy=(0.95 * math.cos(needle_theta),
                         0.95 * math.sin(needle_theta)),
                     xytext=(0, 0),
                     arrowprops=dict(arrowstyle="-|>", color=needle_color,
                                     lw=2.0, mutation_scale=12))

    # Hub
    hub = mpatches.Circle((0, 0), 0.09, color=STEEL, zorder=5)
    ax_left.add_patch(hub)

    # Mach readout
    mach_val = maxbin_last / (NSPEEDBINS - 1)
    ax_left.text(0, -0.48, f"M {mach_val:.2f}",
                 ha="center", va="center", color=needle_color,
                 fontsize=14, fontweight="bold")
    ax_left.text(0, -0.72, f"bin {maxbin_last}/{NSPEEDBINS-1}  col {front_col_last}",
                 ha="center", va="center", color=STEEL, fontsize=7)

    # SONIC BOOM badge
    if sonic_last:
        badge = mpatches.FancyBboxPatch((-0.85, -1.45), 1.7, 0.42,
                                        boxstyle="round,pad=0.05",
                                        linewidth=2, edgecolor=RED,
                                        facecolor="#300808", zorder=6)
        ax_left.add_patch(badge)
        ax_left.text(0, -1.24, "SONIC BOOM", ha="center", va="center",
                     color=RED, fontsize=12, fontweight="bold", zorder=7)

    valid_str = "VALID" if valid_last else "NO FRONT"
    ax_left.text(0, 1.48, valid_str, ha="center", va="center",
                 color=GREEN if valid_last else DIM, fontsize=8)

    # --- Right top: per-window maxspeedbin history ---
    ax_top.axhline(NSPEEDBINS - 1, color=RED, linewidth=0.8, linestyle="--",
                   alpha=0.7, label=f"sonic bin={NSPEEDBINS-1}")
    if history_bin:
        xs = list(range(len(history_bin)))
        ax_top.step(xs, history_bin, where="post", color=ORANGE, linewidth=1.0)
        sonic_xs = [i for i, s in enumerate(history_sonic) if s]
        sonic_ys = [history_bin[i] for i in sonic_xs]
        if sonic_xs:
            ax_top.scatter(sonic_xs, sonic_ys, color=RED, s=25, zorder=3,
                           label="sonic!")
    ax_top.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_top.set_ylabel("max speed bin", color=TEXT, fontsize=8)
    ax_top.set_title("per-window max speed bin", color=TEXT, fontsize=9, pad=4)
    ax_top.set_ylim(-0.5, NSPEEDBINS - 0.5)
    ax_top.legend(fontsize=7, framealpha=0.2, labelcolor=TEXT,
                  facecolor=BG, edgecolor="#2a2d3a")

    # --- Right bottom: front column history ---
    if history_col:
        xs = list(range(len(history_col)))
        ax_bot.step(xs, history_col, where="post", color=GOLD, linewidth=1.0)
    ax_bot.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_bot.set_ylabel("front column", color=TEXT, fontsize=8)
    ax_bot.set_title("per-window activation column", color=TEXT, fontsize=9, pad=4)
    ax_bot.set_ylim(-2, SX + 2)
    ax_bot.axhline(0,   color=STEEL, linewidth=0.5, linestyle=":")
    ax_bot.axhline(SX - 1, color=STEEL, linewidth=0.5, linestyle=":")

    fig.suptitle('"The Whipcracker"', color=TEXT, fontsize=12,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# CSV loader (mirrors dvs_vital_view.py pattern)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col):
    """Load event CSV with columns x, y, pol and optional timestamp column.

    ts_col: column name for timestamp field (default 'le').
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
                    help="synthetic self-test: fast sweep sonic; slow sweep subsonic; "
                         "static hot-pixel guard; LUT spot checks; well-formedness")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le)")
    ap.add_argument("--headless", action="store_true",
                    help="non-interactive rendering (no display)")
    ap.add_argument("--save", help="write the whip PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing whip words "
              f"(bit-faithful mirror of firmware).")
        words, latches = python_whip_words(x, y, ts, pol)
        if words:
            valid_last, seq_last, front_col_last, sonic_last, maxbin_last = \
                unpack_status(words[-1])
            sonic_str = " *** SONIC BOOM ***" if sonic_last else ""
            print(f"final: valid={valid_last} sonic={sonic_last} "
                  f"maxspeedbin={maxbin_last} front_col={front_col_last} "
                  f"({len(words)} words emitted){sonic_str}")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_whip(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
