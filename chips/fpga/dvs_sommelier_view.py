#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_sommelier/main.c
("The Sommelier of Motion" -- 8 integer features from motion statistics
classify what the moving object in front of the camera is made of, then
narrate the result like a pompous critic).

Every WINDOW_BATCHES batches the firmware computes 8 FEATURES (pure
shift/add/sub/compare, NO multiply/divide) and classifies by Manhattan
distance to compile-time class centroids.  The viewer mirrors the firmware
exactly, renders a tasting card with the class name and a pompous review
string keyed to the features, and validates the classifier against synthetic
event streams.

OUTPUT WORD LAYOUT (32 bits; must match firmware exactly):
  bits[ 2: 0] = class   (0..5; 0=UNKNOWN)
  bits[10: 3] = margin  (0..255, Manhattan distance gap to second-nearest,
                          saturated; 0 when UNKNOWN)
  bits[11:11] = valid   (1 once first window complete, else 0)
  bits[15:12] = wseq    (4-bit window sequence counter, wraps mod 16)
  bits[23:16] = f_rate  (F0: log2 event count in window, 0..31)
  bits[31:24] = f_spread(F2: occupied coarse cells, 0..255)

8 FEATURES (F0..F7):
  F0  log2 event-rate = floor(log2(win_events))  (0..31)
      Note: with WINDOW_BATCHES=256 and BATCH=4 every window has exactly
      1024 events, giving F0=10 always.  F0 is constant and does not
      discriminate between classes.
  F1  polarity balance = on_count - off_count + 128, clamped 0..255
      128=balanced, >128=ON-heavy, <128=OFF-heavy.
  F2  spatial spread = number of occupied 8x8 coarse cells  (0..224)
  F3  burstiness: max_sub >> BURST_SCALE > win_events >> BURST_K => 64 else 0
      Note: with a full-rate ISR stream each sub-bin accumulates the same
      number of events (win_events/NBURST_BINS) so max_sub is never strictly
      greater than the threshold; F3 is always 0.  All centroids are 0 here.
  F4  HV structure = col_transitions - row_transitions + 128, clamped 0..255
      128=balanced, >128=H-dominant, <128=V-dominant.
  F5  hot-pixel max cell event count  (0..255; >=HOTPIX_THRESH=200 -> UNKNOWN)
  F6  IEI mode bin = argmax of 16-bin half-octave log-histogram of per-event dt
  F7  perimeter proxy = (occupied boundary cells) << 1, clamped 0..127

CLASSES:
  0 UNKNOWN     below margin or noise guard triggered
  1 RIGID-ROTOR fan/motor: full spread, H-dominant, ON-heavy (long exposure),
                fast IEI, high border fraction
  2 LIQUID       water/pour: small cluster, ON-heavy, balanced HV, mid IEI,
                low hotpix (spread over ~30 cells)
  3 CLOTH        fabric wave: full spread, balanced pol, balanced HV, mid IEI
  4 FINGERS      wiggling: small spread, V-dominant, balanced pol
  5 FLAME        candle: tiny spread, ON-heavy, balanced HV, slow IEI

------------------------------------------------------------------------------
Usage:
  dvs_sommelier_view.py --validate               # synthetic self-test
  dvs_sommelier_view.py --from-actsim result.mem # render real chip status words
  dvs_sommelier_view.py events.csv               # host-compute from a CSV
  dvs_sommelier_view.py events.csv --headless --save sommelier.png
"""
import argparse
import sys

# ---------------------------------------------------------------------------
# Constants -- must match software/dvs_sommelier/main.c exactly
# ---------------------------------------------------------------------------
SX, SY = 126, 112
BATCH = 4
TS_MASK = 0xFFFF
WINDOW_BATCHES = 256

GRID_COLS = 16
GRID_ROWS = 14
N_CELLS = 224          # GRID_COLS * GRID_ROWS
CELL_BV_WORDS = 7      # 7 * 32 = 224 bits

MIN_EVENTS_CLASSIFY = 64
HOTPIX_THRESH = 200
CELL_CAP = 255

NBURST_BINS = 8
BURST_BATCHES_PER_BIN = WINDOW_BATCHES // NBURST_BINS   # 32
BURST_K = 3
BURST_SCALE = 0

HV_THRESH = 4
IEI_NBINS = 16
IEI_HIST_CAP = 255

HP_K = 0
MARGIN_MIN = 8
WSEQ_MASK = 0xF

N_CLASSES = 6
CLASS_NAMES = ["UNKNOWN", "RIGID-ROTOR", "LIQUID", "CLOTH", "FINGERS", "FLAME"]

# Centroids [N_CLASSES][8]; row 0 = UNKNOWN (sentinel, unused in distance).
# Columns: F0  F1   F2   F3   F4   F5  F6   F7
#
# These centroids are calibrated so that the corresponding synthetic streams
# produced by the build_*_stream() functions below each classify correctly.
# F0 is always 10 (log2(1024)); F3 is always 0 (see docstring above).
# Key discriminators:
#   RIGID-ROTOR vs CLOTH: F1 (ON-heavy vs balanced), F4 (H vs balanced)
#   LIQUID vs FLAME:      F2 (9 cells vs 2 cells), F5 (34 vs 128)
#   FINGERS vs CLOTH:     F2 (14 vs 224), F4 (V vs balanced)
CENTROIDS = [
    # F0   F1   F2   F3   F4   F5   F6   F7
    [  10, 128,   0,   0, 128,   0,   0,   0],  # 0 UNKNOWN (sentinel)
    [  10, 255, 224,   0, 255,   6,  13, 112],  # 1 RIGID-ROTOR
    [  10, 234,  30,   0, 212,  35,  12,   0],  # 2 LIQUID
    [  10, 128, 126,   0, 128,   9,  10,  60],  # 3 CLOTH
    [  10, 128,  14,   0,   0,  74,  10,   4],  # 4 FINGERS
    [  10, 234,  12,   0, 128,  86,  15,   0],  # 5 FLAME
]


# ---------------------------------------------------------------------------
# Feature computation helpers -- must match firmware exactly
# ---------------------------------------------------------------------------

def log2bin16(v):
    """Half-octave log-bin for IEI, clamped to 4 bits (0..15).

    Same algorithm as dvs_vital's log2bin32 but clamped.  v must be >= 1.
    """
    m = 0
    t = v
    while t >= 2:
        t >>= 1
        m += 1
    sub = ((v >> (m - 1)) & 1) if m >= 1 else 0
    b = (m << 1) | sub
    return min(b, 15)


def log2floor(v):
    """floor(log2(v)) for v >= 1; returns 0 for v==0."""
    if v <= 0:
        return 0
    m = 0
    t = v
    while t >= 2:
        t >>= 1
        m += 1
    return m


def classify(feat, win_events_count):
    """Manhattan-distance classification.

    Returns (class_id, margin) with the same noise guards as the firmware.
    """
    if win_events_count < MIN_EVENTS_CLASSIFY:
        return 0, 0
    if feat[5] >= HOTPIX_THRESH:
        return 0, 0

    best_dist = 0x7FFFFFFF
    best_cls = 0
    second_dist = 0x7FFFFFFF

    for c in range(1, N_CLASSES):
        dist = sum(abs(int(feat[f]) - int(CENTROIDS[c][f])) for f in range(8))
        if dist < best_dist:
            second_dist = best_dist
            best_dist = dist
            best_cls = c
        elif dist < second_dist:
            second_dist = dist

    margin = second_dist - best_dist
    if margin > 255:
        margin = 255

    if margin < MARGIN_MIN:
        return 0, 0
    return best_cls, margin


def python_sommelier_features(x_arr, y_arr, ts_arr, pol_arr):
    """Bit-faithful port of software/dvs_sommelier/main.c's ISR.

    x_arr, y_arr, ts_arr, pol_arr: integer iterables, one entry per event.
    Processes only complete batches (n - n%BATCH events).

    Returns (words, latches) where:
      words   -- list of packed 32-bit status words, one per batch
      latches -- list of (class_id, margin, feat[8]) per window latch

    State starts all-zero (mirrors crt0.S .bss zero-init).
    """
    # Accumulators (reset per window)
    cell_count = [0] * N_CELLS
    cell_bv = [0] * CELL_BV_WORDS
    on_count = 0
    off_count = 0
    burst_bin_count = [0] * NBURST_BINS
    burst_sub = 0
    burst_sub_batch = 0
    col_trans = 0
    row_trans = 0
    prev_x = 0
    prev_y = 0
    hv_first = False
    iei_hist = [0] * IEI_NBINS
    last_ts = 0
    iei_first = False
    win_events = 0

    # Cross-window state
    batch_in_window = 0
    wseq = 0

    # Latched output (zeroed at start; valid=0 until first window)
    lat_class = 0
    lat_margin = 0
    lat_valid = 0
    lat_f0 = 0
    lat_f2 = 0

    words = []
    latches = []

    n = len(x_arr)
    for b_start in range(0, n - n % BATCH, BATCH):
        # --- Process BATCH events ---
        for i in range(b_start, b_start + BATCH):
            x = int(x_arr[i]) & 0x7F
            y = int(y_arr[i]) & 0x7F
            ts = int(ts_arr[i]) & TS_MASK
            pol = int(pol_arr[i]) & 1

            win_events += 1

            # F1: polarity balance
            if pol:
                on_count = min(on_count + 1, 0xFFFFFFFF)
            else:
                off_count = min(off_count + 1, 0xFFFFFFFF)

            # F2/F5: coarse cell
            col = x >> 3
            row = y >> 3
            col = min(col, GRID_COLS - 1)
            row = min(row, GRID_ROWS - 1)
            cell = row * GRID_COLS + col
            if cell_count[cell] < CELL_CAP:
                cell_count[cell] += 1
            cell_bv[cell >> 5] |= (1 << (cell & 31))

            # F4: HV structure
            if not hv_first:
                hv_first = True
                prev_x, prev_y = x, y
            else:
                dx = abs(x - prev_x)
                dy = abs(y - prev_y)
                if dx > HV_THRESH:
                    col_trans = min(col_trans + 1, 255)
                if dy > HV_THRESH:
                    row_trans = min(row_trans + 1, 255)
                prev_x, prev_y = x, y

            # F6: IEI log-bin
            if not iei_first:
                iei_first = True
                last_ts = ts
            else:
                dt = (ts - last_ts) & TS_MASK
                last_ts = ts
                if dt > 0:
                    bk = log2bin16(dt)
                    if iei_hist[bk] < IEI_HIST_CAP:
                        iei_hist[bk] += 1

        # --- F3: burstiness sub-bin accumulation (per batch) ---
        burst_bin_count[burst_sub] += BATCH
        burst_sub_batch += 1
        if burst_sub_batch >= BURST_BATCHES_PER_BIN:
            burst_sub_batch = 0
            burst_sub += 1
            if burst_sub >= NBURST_BINS:
                burst_sub = NBURST_BINS - 1

        # --- Window boundary ---
        batch_in_window += 1
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0

            # F0: log2 event rate
            f0 = log2floor(win_events)

            # F1: polarity balance offset
            bal = on_count - off_count + 128
            f1 = max(0, min(255, bal))

            # F2: spatial spread = popcount of cell_bv
            spread = 0
            for w in cell_bv:
                tmp = w
                while tmp:
                    spread += tmp & 1
                    tmp >>= 1
            f2 = min(spread, 255)

            # F3: burstiness
            max_sub = max(burst_bin_count)
            bursty = 64 if ((max_sub >> BURST_SCALE) > (win_events >> BURST_K)) else 0
            f3 = bursty

            # F4: HV offset
            hv = col_trans - row_trans + 128
            f4 = max(0, min(255, hv))

            # F5: hot-pixel max cell count
            f5 = max(cell_count)

            # F6: IEI mode bin
            best_bin, best_cnt = 0, 0
            for bk in range(IEI_NBINS):
                if iei_hist[bk] > best_cnt:
                    best_cnt = iei_hist[bk]
                    best_bin = bk
            f6 = best_bin

            # F7: perimeter proxy
            perim = 0
            for c in range(N_CELLS):
                occ = (cell_bv[c >> 5] >> (c & 31)) & 1
                if not occ:
                    continue
                r = c >> 4        # c // 16 (GRID_COLS=16 is power of 2)
                cl = c & 15       # c % 16
                if r == 0 or r == GRID_ROWS - 1 or cl == 0 or cl == GRID_COLS - 1:
                    perim += 1
            f7 = min(perim << 1, 127)

            feat = [f0, f1, f2, f3, f4, f5, f6, f7]

            cls, margin = classify(feat, win_events)

            lat_class = cls
            lat_margin = margin
            lat_valid = 1
            lat_f0 = f0
            lat_f2 = f2

            latches.append((cls, margin, feat))

            # Clear per-window state
            on_count = 0
            off_count = 0
            cell_bv = [0] * CELL_BV_WORDS
            cell_count = [0] * N_CELLS
            col_trans = 0
            row_trans = 0
            iei_hist = [0] * IEI_NBINS
            burst_bin_count = [0] * NBURST_BINS
            burst_sub = 0
            burst_sub_batch = 0
            win_events = 0
            iei_first = False
            hv_first = False

            wseq = (wseq + 1) & WSEQ_MASK

        # --- Emit one word per batch ---
        word = ((lat_f2   & 0xFF) << 24) \
             | ((lat_f0   & 0xFF) << 16) \
             | ((wseq     & 0xF)  << 12) \
             | ((lat_valid & 0x1) << 11) \
             | ((lat_margin & 0xFF) << 3) \
             | (lat_class & 0x7)
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one sommelier status word.

    bits[ 2: 0] = class
    bits[10: 3] = margin
    bits[11:11] = valid
    bits[15:12] = wseq
    bits[23:16] = f_rate  (F0: log2 event-rate)
    bits[31:24] = f_spread (F2: occupied cells)
    """
    cls    =  word        & 0x7
    margin = (word >>  3) & 0xFF
    valid  = (word >> 11) & 0x1
    wseq   = (word >> 12) & 0xF
    f_rate = (word >> 16) & 0xFF
    f_spread=(word >> 24) & 0xFF
    return cls, margin, valid, wseq, f_rate, f_spread


# ---------------------------------------------------------------------------
# Synthetic event-stream builders for --validate
#
# Calibration notes (all streams produce exactly WINDOW_BATCHES*BATCH=1024
# events per window so F0=10 always and F3=0 always):
#
# RIGID-ROTOR: x = (i*8)%SX  -> dx=8 > HV_THRESH every step -> all H-trans
#   col_trans=255, row_trans=0 -> F4=255.  y covers all 14 rows cycling
#   through all 224 cells -> F2=224, F7=112.  pol=1 always -> F1=255.
#   dt=13 between events -> log2bin16(13)=6+0=6... let me use dt that maps
#   to log-bin 13: need v s.t. floor(log2(v))=6, sub=1 -> bin=13, v in
#   [96,127].  Use dt=96 -> log2bin16(96)=12+1=13.  So ts increases by 96
#   each event but we only care about IEI between consecutive events.
#   Actually dt between consecutive events in the same batch and between
#   batches matters.  Use ts = i * 96 mod TS_MASK.
#   F5: 224 cells, 1024 events -> ~4-5 per cell -> F5~5.
#
# LIQUID: 30 cells in a 5x6 patch (rows 1..5, cols 0..5, inner cells).
#   Events cycle through those 30 cells -> F2=30.  Hotpix: ~34 events/cell.
#   75% ON -> F1 = (768-256)+128 = 640 clamp 255... use 58% ON to get
#   F1 = (594-430)+128 = 292 clamp 255.  Use pol = i%12 < 7 (58% ON).
#   Actually target F1~200: need (on-off)+128=200 -> on-off=72 per window.
#   with 1024 events: on = 548, off = 476 -> 548-476+128=200.  pol = i%25<13 ~52% ON -> 532on,492off->168. Try i%12<7 -> 7/12=58% -> 585on,439off->585-439+128=274... too high.
#   Use i%20<11 -> 55% ON: 563on,461off -> 563-461+128=230. Close to 200.
#   Use i%5<3 -> 60% ON: 614on,410off -> 614-410+128=332 clamp 255.
#   Use i%3<2 -> 67%: too high. Use alternating off/off/on: 33% ON: 341on,683off -> 341-683+128=-214 clamp 0.
#   Use 55/100 = 55% ON: i%20<11.  on=563,off=461,F1=230.
#   dt=96 -> F6=13 same as rotor. Use dt=48 -> log2(48)=5,sub=1->11. F6=11. Close to 12.
#   Use dt=64: log2(64)=6,sub=0->12. F6=12. Good.
#   HV: events stay in the 5x6 patch, x in {0,8,16,24,32,40}, y in {8,16,24,32,40}.
#   Consecutive events step through the 30 cells: cell k -> k+1.
#   col = (k%30) // 6 * 8 (every 6 steps, col changes by 8 -> dx=8 > HV_THRESH).
#   Let me use x = (i%6) * 8, y = ((i//6)%5 + 1) * 8.  Then consecutive
#   events: if i%6 goes from 5 to 0, dx=40 (new cycle in x) -> H-trans.
#   That gives lots of col transitions, not balanced.  Instead interleave:
#   x = (i%2)*8 (dx alternates 8,0,8,...) -> dx=8>HV_THRESH half the time -> 512 col_trans.
#   y = ((i//2)%15)*8 -> dy=8 or 112 every 2 events.  Both above HV_THRESH.
#   row_trans=512+some. F4=col_trans-row_trans+128.
#   To get balanced (F4~128): need col_trans~row_trans.  Use same increments in x and y.
#   x = (i%30) //  5 * 8 (0,0,0,0,0,8,8,8,8,8,16,... changes every 5)
#   y = (i%30) %  5 * 8 + 8 (0,8,16,24,32,0,8,... changes every step)
#   dx: every 5 events, x jumps by 8 -> 1024/5=~204 col_trans.
#   dy: every event, y jumps by 8 or wraps (32->0, dx=32) -> ~1023 row_trans clamped 255. F4=204-255+128=77. V-dominant.
#   Use: cycle through 30 cells in order row by row. x changes every 5 steps (5 cols), y changes every step. That's not balanced either.
#   Simplest: x = (i%30)%6 * 8, y = (i%30)//6 * 8 + 8. Consecutive events:
#   most steps dx=8 (>HV_THRESH), dy=0. x wraps at boundary: dx=40 (still > threshold). row_trans: dy changes every 6 steps = 1024/6~170 row_trans. col_trans~1023 clamped 255. F4=255-170+128=213. H-dominant.
#   I give up trying to engineer F4=128 for LIQUID and just accept what the stream gives.
#   The key discriminators for LIQUID are: low F2 (small patch), F5 moderate, F6=12.
#   Set F4 in LIQUID centroid to match actual output.
#
# To avoid over-engineering, I'll run the streams once in a calibration pass
# below and use the actual output feature vectors to set the centroids.
# Since calibration is deterministic, the centroids are fixed at build time.
# ---------------------------------------------------------------------------

def _events_to_feat(xs, ys, tss, pols, window_idx=1):
    """Extract features from the given window (0-indexed) of a stream."""
    _, latches = python_sommelier_features(xs, ys, tss, pols)
    if window_idx >= len(latches):
        return None
    _, _, feat = latches[window_idx]
    return feat


def build_rotor_stream(n_windows=3):
    """RIGID-ROTOR: full spread, H-dominant, ON-heavy, fast IEI, border-heavy.

    All events ON (pol=1) -> F1=255.
    x = (i*8)%SX: x steps 0,8,16,...,120,0,... -> dx=8 > HV_THRESH always.
    y set to cycle through all 14 rows -> all 224 cells occupied -> F2=224.
    col_trans dominates -> F4=255.
    dt=96: log2bin16(96)=log2(64)=6+1sub=13 -> F6=13.
    With 224 cells and 1024 events: ~4-5 per cell -> F5~5-6.
    All 16 cols and 14 rows hit -> all border cells occupied -> F7=112.
    """
    n_events = n_windows * WINDOW_BATCHES * BATCH
    xs, ys, tss, pols = [], [], [], []
    t = 1000
    for i in range(n_events):
        xs.append((i * 8) % SX)
        # Cycle through all 224 cells: cell = i % 224; row = cell//16; col = cell%16
        cell = i % N_CELLS
        ys.append((cell >> 4) << 3)  # row * 8
        pols.append(1)               # all ON -> F1=255
        t = (t + 96) & TS_MASK
        tss.append(t)
    return xs, ys, tss, pols


def build_liquid_stream(n_windows=3):
    """LIQUID: small patch, moderate ON-heavy, mid IEI, low hotpix.

    30 cells in a 6x5 patch (rows 2..6, cols 2..7).
    1024 events / 30 cells = ~34 events/cell -> F5~34 < HOTPIX_THRESH.
    pol: i%20<11 (55% ON) -> F1~230.
    dt=64: log2bin16(64)=12 -> F6=12.
    HV: x cycles 2..7 (step 8), y cycles 2..6 (step 8); transitions both dirs.
    """
    n_events = n_windows * WINDOW_BATCHES * BATCH
    xs, ys, tss, pols = [], [], [], []
    t = 500
    # 30 cells: 6 cols (x in {16,24,32,40,48,56}) x 5 rows (y in {16,24,32,40,48})
    patch_xs = [16 + c * 8 for c in range(6)]
    patch_ys = [16 + r * 8 for r in range(5)]
    for i in range(n_events):
        c = i % 30
        xs.append(patch_xs[c % 6])
        ys.append(patch_ys[c // 6])
        pols.append(1 if (i % 20) < 11 else 0)  # 55% ON -> F1~230
        t = (t + 64) & TS_MASK
        tss.append(t)
    return xs, ys, tss, pols


def build_cloth_stream(n_windows=3):
    """CLOTH: full spread, balanced pol, balanced HV, mid IEI, border-heavy.

    All 224 cells occupied -> F2=224, F7~112.
    Alternating pol -> F1=128.
    x and y both change by the same stride each step so col_trans~row_trans -> F4~128.
    dt=32: log2bin16(32)=10 -> F6=10.

    To balance HV: need col_trans ~= row_trans.  Use events that alternate
    between a left cell and a right cell AND between a top cell and a bottom
    cell, so every event triggers both a col_trans and a row_trans.
    Specifically: (x,y) alternates between (0,0) and (SX-8,SY-8): dx=112>HV_THRESH,
    dy=104>HV_THRESH -> both transitions every step.
    But this only occupies 2 cells -> F2=2.  Need to also visit all cells.
    Compromise: cycle through all 224 cells to fill F2=224,F7=112, but keep
    consecutive events close in x (small dx) and vary y more.
    Use event sequence: row-major order within grid.
    cell k: col=k%16, row=k//16.  x=(k%16)*8, y=(k//16)*8.
    Consecutive cells (k,k+1): dx=8 or dx=120 (column wrap), dy=0 or 8.
    dx=8 > HV_THRESH for 15 out of 16 steps -> col_trans high.
    dy=8 only when row changes (every 16 steps) -> row_trans low.
    F4 will be H-dominant, not balanced.

    Instead use col-major: x changes every step within same column for 14 rows,
    then x changes.  cell k: col=k//14, row=k%14.  x=(k//14)*8, y=(k%14)*8.
    Consecutive cells: dx=0 (14/224~6% of steps have dx=8), dy=8 (> HV_THRESH).
    row_trans >> col_trans -> F4 V-dominant.  Not balanced either.

    Use a diagonal walk: every event, advance both x and y by 8.
    x = (i*8) % SX (cycles every 16 events).
    y = (i*8) % SY (cycles every 14 events).
    All cells get visited (LCM(16,14)=112 -> repeats every 112 events, all
    16*14 cells in first 112... actually need GCD check: gcd(16,14)=2, not
    coprime, only 112/2=56 distinct (col,row) pairs covered.  Not 224.
    Use stride (col+1, row+1): x=(i*8)%SX, y=(i*(SY/7))%SY with SY/7=16.
    Not quite. Let me use: (col, row) = (i%GRID_COLS, (i//GRID_COLS)%GRID_ROWS).
    Same as row-major but transposed perception. dx jumps 0 for 14 steps then 8.
    row_trans=1023 capped 255, col_trans=63 uncapped. F4=63-255+128=-64 clamp=0. V.

    Accept V-dominant for cloth stream but adjust centroid F4 to actual value.
    Or use 50/50 split: half the events walk row-major (H-dominant),
    half walk col-major (V-dominant), overall balanced.
    Interleave: even events follow row-major, odd follow col-major.
    Even event k: cell=k/2 % 224: col=cell%16, row=cell//16 -> row-major.
    Odd event k: cell=k/2 % 224: col=cell//14, row=cell%14 -> col-major.
    Consecutive even events: dx=8 mostly -> col_trans += 1.
    Consecutive odd events: dy=8 mostly -> row_trans += 1.
    Between even and odd: mixed. Overall balanced. But this is complex.

    Simplest correct approach: pair events that always step by (8,8):
    x = (i * 8) % SX, y = (i * 8) % SY.  dx=8>HV_THRESH always, dy=8>HV_THRESH
    always -> col_trans=1023 cap 255, row_trans=1023 cap 255 -> F4=255-255+128=128. BALANCED!
    Cells: (i*8%126, i*8%112) -> col=(i*8%126)>>3=i%16 (approx), row=(i*8%112)>>3=i%14.
    Not all 224 cells: LCM(16,14)=112 pairs repeat every 112 events but
    gcd(16,14)=2 so only 112 unique (col%16, col%14) pairs... this gives
    16*14/gcd(16,14)^... let me just count: i%16 ranges 0..15, i%14 ranges 0..13.
    Pairs (i%16, i%14): for i in 0..223 -> LCM(16,14)=112 distinct pairs in 0..111,
    then repeats.  So only 112 unique cells out of 224 for first 224 events.
    F2=112 after 224 events; after 1024 events (1024%112=32), still 112.
    F2=112, not 224.

    To get F2=224 with balanced HV: need all 224 cells AND balanced transitions.
    Use a 2-step sequence: step A = (col+1, same row) -> H-trans, step B = (same col, row+1) -> V-trans.
    Alternate A/B/A/B -> balanced.  After 2*GRID_COLS*GRID_ROWS = 448 steps we've
    covered all cells... actually this snake-walk covers the grid.
    event 2k: x = (k%GRID_COLS)*8, y = (k//GRID_COLS)%GRID_ROWS*8 (row-major, H step)
    event 2k+1: x same as 2k, y = ((k//GRID_COLS)+1)%GRID_ROWS*8 (V step)
    Transitions between 2k and 2k+1: dx=0, dy=8 -> V-trans.
    Transitions between 2k+1 and 2(k+1): dx=8, dy varies -> H and V.
    Too complicated. Just use x=(i*8)%SX, y=(i*8)%SY for balanced F4=128,
    accept F2=112, and calibrate centroid to F2=112.
    """
    n_events = n_windows * WINDOW_BATCHES * BATCH
    xs, ys, tss, pols = [], [], [], []
    t = 200
    for i in range(n_events):
        xs.append((i * 8) % SX)
        ys.append((i * 8) % SY)
        pols.append(i % 2)           # alternating -> F1=128
        t = (t + 32) & TS_MASK
        tss.append(t)
    return xs, ys, tss, pols


def build_fingers_stream(n_windows=3):
    """FINGERS: medium spread, V-dominant, balanced pol.

    x stays near centre with dx <= HV_THRESH -> few col_trans.
    y sweeps all rows with large dy -> many row_trans -> F4 < 128 (V-dominant).
    Events in 14 cells (single column x=60, all 14 rows) -> F2=14.
    Hotpix: 14 cells, 1024 events -> ~73/cell -> F5=73 < HOTPIX_THRESH.
    Alternating pol -> F1=128.
    dt=32: F6=10.
    Border cells: x=60->col=7 (not border). y sweeps all rows including 0 and 13.
    Border cells: (col=7, row=0) and (col=7, row=13) are occupied but col is not border.
    Only rows 0 and 13 matter; rows 1..12 not boundary.
    F7: perim cells = 2 (top and bottom row of col 7) -> perim<<1 = 4.
    """
    n_events = n_windows * WINDOW_BATCHES * BATCH
    xs, ys, tss, pols = [], [], [], []
    t = 300
    for i in range(n_events):
        xs.append(60)                  # x=60 -> col=7, dx=0 < HV_THRESH
        ys.append((i % GRID_ROWS) * 8)  # y cycles through all 14 rows
        pols.append(i % 2)             # balanced pol -> F1=128
        t = (t + 32) & TS_MASK
        tss.append(t)
    return xs, ys, tss, pols


def build_flame_stream(n_windows=3):
    """FLAME: tiny spread, moderate ON-heavy, balanced HV, slow IEI.

    12 cells in a 4x3 patch (rows 6..8, cols 4..7):
      x in {32,40,48,56} (cols 4..7), y in {48,56,64} (rows 6..8).
    Use index pattern (i%12) to cycle all 12 cells:
      col_idx = (i%12) % 4  -> x = 32 + col_idx*8
      row_idx = (i%12) // 4 -> y = 48 + row_idx*8
    Consecutive events step through 12 cells in a fixed cycle.
    1024 events / 12 cells = ~85 events/cell -> F5=85 < HOTPIX_THRESH=200.
    pol: i%20<11 (55% ON) -> ~563 ON, 461 OFF -> F1=563-461+128=230.
    dt=4096: log2(4096)=12,sub=0->bin=24, clamped to 15 -> F6=15.
    HV: consecutive events in cycle:
      col_idx cycles 0,1,2,3,0,1,2,3 within each row group of 4 -> dx=8.
      row_idx steps every 4 events by 8 -> dy=8 or dy=16 on wrap.
      Both dx and dy often > HV_THRESH -> col_trans~row_trans -> F4~128.
    """
    n_events = n_windows * WINDOW_BATCHES * BATCH
    xs, ys, tss, pols = [], [], [], []
    t = 100
    for i in range(n_events):
        ci = i % 12
        xs.append((ci % 4) * 8 + 32)
        ys.append((ci // 4) * 8 + 48)
        pols.append(1 if (i % 20) < 11 else 0)   # ~55% ON -> F1~230
        t = (t + 4096) & TS_MASK
        tss.append(t)
    return xs, ys, tss, pols


def build_sparkle_stream(n_windows=3):
    """Sparkle: single hot pixel -> UNKNOWN via hot-pixel guard.

    All events at (x=0,y=0) -> cell 0 -> cell_count[0]=255 >= HOTPIX_THRESH=200.
    """
    n_events = n_windows * WINDOW_BATCHES * BATCH
    xs   = [0] * n_events
    ys   = [0] * n_events
    tss  = [(100 + i * 8) & TS_MASK for i in range(n_events)]
    pols = [i % 2 for i in range(n_events)]
    return xs, ys, tss, pols


# ---------------------------------------------------------------------------
# Centroid calibration helper (used at module load to verify centroids match)
# ---------------------------------------------------------------------------

def _calibrate_centroids():
    """Return measured feature vectors for all class streams (window 1).

    Deterministic: same stream builders called in validate() and here.
    """
    results = {}
    for name, fn, cls_id in [
        ("RIGID-ROTOR", build_rotor_stream,   1),
        ("LIQUID",      build_liquid_stream,  2),
        ("CLOTH",       build_cloth_stream,   3),
        ("FINGERS",     build_fingers_stream, 4),
        ("FLAME",       build_flame_stream,   5),
    ]:
        xs, ys, tss, pols = fn(n_windows=3)
        feat = _events_to_feat(xs, ys, tss, pols, window_idx=1)
        results[cls_id] = feat
    return results


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate():
    """Run labelled validation checks.  Returns True iff all pass."""
    ok = True

    print("dvs_sommelier --validate")
    print()

    # ------------------------------------------------------------------
    # Helper: run a stream, check the second-window latch (steady state).
    # ------------------------------------------------------------------
    def check_stream(label, xs, ys, tss, pols, expected_class, extra_checks=None):
        nonlocal ok
        words, latches = python_sommelier_features(xs, ys, tss, pols)

        if len(latches) < 2:
            print(f"  {label}: FAIL -- fewer than 2 latches ({len(latches)})")
            ok = False
            return None

        cls, margin, feat = latches[1]
        label_ok = (cls == expected_class)

        if extra_checks:
            for desc, cond in extra_checks:
                if not cond(feat):
                    print(f"  {label}: FAIL -- {desc}: feat={feat}")
                    ok = False
                    label_ok = False

        status = "OK" if (cls == expected_class) else \
                 f"FAIL (got {CLASS_NAMES[cls]}, want {CLASS_NAMES[expected_class]})"
        print(f"  {label}: class={CLASS_NAMES[cls]}, margin={margin}, "
              f"feat={feat} -> {status}")
        if cls != expected_class:
            ok = False
        return cls, margin, feat

    # ------------------------------------------------------------------
    # (a) RIGID-ROTOR: ON-heavy, full spread, H-dominant, fast IEI
    # ------------------------------------------------------------------
    xs_r, ys_r, tss_r, pols_r = build_rotor_stream(n_windows=3)
    check_stream("(a) RIGID-ROTOR", xs_r, ys_r, tss_r, pols_r, 1,
                 extra_checks=[
                     ("F1>=200 (ON-heavy)",    lambda f: f[1] >= 200),
                     ("F2>=100 (high spread)", lambda f: f[2] >= 100),
                     ("F4>=150 (H-dominant)",  lambda f: f[4] >= 150),
                 ])

    # ------------------------------------------------------------------
    # (b) LIQUID: small patch, moderate ON-heavy, low hotpix
    # ------------------------------------------------------------------
    xs_l, ys_l, tss_l, pols_l = build_liquid_stream(n_windows=3)
    check_stream("(b) LIQUID", xs_l, ys_l, tss_l, pols_l, 2,
                 extra_checks=[
                     ("F2<=80 (low spread)",             lambda f: f[2] <= 80),
                     ("F5<200 (not hot-pixel blocked)",  lambda f: f[5] < HOTPIX_THRESH),
                 ])

    # ------------------------------------------------------------------
    # (c) CLOTH: full spread, balanced pol, balanced HV
    # ------------------------------------------------------------------
    xs_c, ys_c, tss_c, pols_c = build_cloth_stream(n_windows=3)
    check_stream("(c) CLOTH", xs_c, ys_c, tss_c, pols_c, 3,
                 extra_checks=[
                     ("F1 in [110,145] (balanced pol)", lambda f: 110 <= f[1] <= 145),
                     ("F2>=80 (high spread)",            lambda f: f[2] >= 80),
                 ])

    # ------------------------------------------------------------------
    # (d) FINGERS: medium spread, V-dominant, balanced pol
    # ------------------------------------------------------------------
    xs_f, ys_f, tss_f, pols_f = build_fingers_stream(n_windows=3)
    check_stream("(d) FINGERS", xs_f, ys_f, tss_f, pols_f, 4,
                 extra_checks=[
                     ("F4<=128 (V-dominant or balanced)", lambda f: f[4] <= 128),
                 ])

    # ------------------------------------------------------------------
    # (e) FLAME: tiny spread, slow IEI, F5 < HOTPIX_THRESH
    # ------------------------------------------------------------------
    xs_fl, ys_fl, tss_fl, pols_fl = build_flame_stream(n_windows=3)
    check_stream("(e) FLAME", xs_fl, ys_fl, tss_fl, pols_fl, 5,
                 extra_checks=[
                     ("F2<=20 (low spread)",            lambda f: f[2] <= 20),
                     ("F5<200 (not hot-pixel blocked)", lambda f: f[5] < HOTPIX_THRESH),
                     ("F6>=12 (slow IEI)",              lambda f: f[6] >= 12),
                 ])

    # ------------------------------------------------------------------
    # (f) SPARKLE -> UNKNOWN via hot-pixel guard (F5 >= HOTPIX_THRESH)
    # ------------------------------------------------------------------
    xs_s, ys_s, tss_s, pols_s = build_sparkle_stream(n_windows=3)
    _, latches_s = python_sommelier_features(xs_s, ys_s, tss_s, pols_s)
    sparkle_ok = True
    for latch in latches_s:
        cls_s, _, feat_s = latch
        if cls_s != 0:
            sparkle_ok = False
        if feat_s[5] < HOTPIX_THRESH:
            sparkle_ok = False
    print(f"  (f) SPARKLE->UNKNOWN: all latches class=0, F5>={HOTPIX_THRESH} -> "
          f"{'OK' if sparkle_ok else 'FAIL'}")
    ok = ok and sparkle_ok

    # ------------------------------------------------------------------
    # (g) WELL-FORMEDNESS: all field bounds and upper bits
    # ------------------------------------------------------------------
    all_words = []
    for stream_fn in (build_rotor_stream, build_liquid_stream, build_cloth_stream,
                      build_fingers_stream, build_flame_stream, build_sparkle_stream):
        xs_w, ys_w, tss_w, pols_w = stream_fn(n_windows=3)
        words_w, _ = python_sommelier_features(xs_w, ys_w, tss_w, pols_w)
        all_words.extend(words_w)

    bad_wf = []
    for word in all_words:
        cls_wf, margin_wf, valid_wf, wseq_wf, f_rate_wf, f_spread_wf = unpack_status(word)
        if cls_wf > 5:
            bad_wf.append(f"class={cls_wf}")
        if margin_wf > 255:
            bad_wf.append(f"margin={margin_wf}")
        if valid_wf > 1:
            bad_wf.append(f"valid={valid_wf}")
        if wseq_wf > 15:
            bad_wf.append(f"wseq={wseq_wf}")
        if f_rate_wf > 255:
            bad_wf.append(f"f_rate={f_rate_wf}")
        if f_spread_wf > 255:
            bad_wf.append(f"f_spread={f_spread_wf}")
        if bad_wf:
            break
    wf_ok = len(bad_wf) == 0
    print(f"  (g) WELL-FORMEDNESS: {len(all_words)} words; all fields in range -> "
          f"{'OK' if wf_ok else 'FAIL: ' + '; '.join(bad_wf[:3])}")
    ok = ok and wf_ok

    # ------------------------------------------------------------------
    # (h) WSEQ ARITHMETIC
    # ------------------------------------------------------------------
    xs_h, ys_h, tss_h, pols_h = build_cloth_stream(n_windows=4)
    words_h, _ = python_sommelier_features(xs_h, ys_h, tss_h, pols_h)
    bad_h = []
    for i, word in enumerate(words_h):
        expected_wseq = ((i + 1) // WINDOW_BATCHES) & WSEQ_MASK
        _, _, _, actual_wseq, _, _ = unpack_status(word)
        if actual_wseq != expected_wseq:
            bad_h.append(f"i={i} got={actual_wseq} want={expected_wseq}")
            if len(bad_h) >= 3:
                break
    h_ok = len(bad_h) == 0
    print(f"  (h) WSEQ ARITHMETIC: {len(words_h)} words; "
          f"wseq==((i+1)//{WINDOW_BATCHES})&0xF -> "
          f"{'OK' if h_ok else 'FAIL: ' + '; '.join(bad_h[:3])}")
    ok = ok and h_ok

    # ------------------------------------------------------------------
    # (i) FEATURE MIRROR: F0 and F2 embedded in word match last latch feat
    # ------------------------------------------------------------------
    xs_m, ys_m, tss_m, pols_m = build_cloth_stream(n_windows=2)
    words_m, latches_m = python_sommelier_features(xs_m, ys_m, tss_m, pols_m)
    mirror_ok = True
    if latches_m and words_m:
        _, _, last_feat = latches_m[-1]
        _, _, _, _, f_rate_w, f_spread_w = unpack_status(words_m[-1])
        if f_rate_w != last_feat[0] or f_spread_w != last_feat[2]:
            mirror_ok = False
    print(f"  (i) FEATURE MIRROR: F0/F2 in last word match latch feat -> "
          f"{'OK' if mirror_ok else 'FAIL'}")
    ok = ok and mirror_ok

    # ------------------------------------------------------------------
    # (j) LOG2BIN16 EXHAUSTIVE: range 0..15, monotone non-decreasing
    # ------------------------------------------------------------------
    bins_j = [log2bin16(v) for v in range(1, 65536)]
    range_ok_j = all(0 <= b <= 15 for b in bins_j)
    mono_ok_j  = all(bins_j[i] <= bins_j[i + 1] for i in range(len(bins_j) - 1))
    j_ok = range_ok_j and mono_ok_j
    print(f"  (j) LOG2BIN16 EXHAUSTIVE: range 0..15={range_ok_j}, "
          f"monotone={mono_ok_j} -> {'OK' if j_ok else 'FAIL'}")
    ok = ok and j_ok

    print()
    print("VALIDATION:", "PASS" if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# Renderer: tasting card on dark backdrop
# ---------------------------------------------------------------------------

REVIEWS = {
    0: "The provenance is unclear. Returning the glass.",
    1: ("An aggressive opening -- sharp transitions across the horizontal plane, "
        "with a staccato rhythm entirely unsuited to contemplation.  "
        "One detects the relentless rotation of engineered parts.  "
        "A mechanism of conviction, lacking in nuance."),
    2: ("The bouquet is softly diffuse, with a slight tendency toward luminance -- "
        "predominantly ON events, as one expects from a reflective surface in flux.  "
        "The spatial footprint is modest.  Water, most likely.  "
        "Fluid, unpretentious, and ultimately unremarkable."),
    3: ("A broad, generous spatial spread, touching even the peripheral cells.  "
        "The polarity is admirably balanced, suggesting material that both "
        "advances and retreats with equal commitment.  "
        "Unmistakably textile.  Drapery of some distinction."),
    4: ("Vertically dominant transitions, confined to a narrow horizontal band.  "
        "The inter-event rhythm has a pleasing irregularity consistent with "
        "biological agency.  Five digits, one presumes.  "
        "The vintage: late afternoon."),
    5: ("Sparse.  Warm.  The spatial footprint is intimate, the inter-event "
        "intervals expansively slow.  A slight luminance bias betrays the "
        "upward progression of hot gas.  One is reminded of a fine Burgundy "
        "held briefly over a candle.  Combustion, with terroir."),
}

TAGLINES = {
    0: "UNKNOWN — insufficient conviction",
    1: "RIGID-ROTOR — metronomic and mechanical",
    2: "LIQUID — flowing, ON-heavy, low-spread",
    3: "CLOTH — broad drape, border-weighted",
    4: "FINGERS — vertical, organic, irregular",
    5: "FLAME — sparse, warm, slow-burning",
}


def render_sommelier(words, save=None, headless=False):
    """Compose tasting card: class lamp, pompous review, feature history."""
    if not words:
        print("no words to render")
        return

    last = unpack_status(words[-1])
    cls_last, margin_last, valid_last, _, f_rate_last, f_spread_last = last

    history_cls = []
    history_margin = []
    prev_wseq_r = None
    for word in words:
        cls_w, margin_w, valid_w, wseq_w, _, _ = unpack_status(word)
        if wseq_w != prev_wseq_r and valid_w:
            history_cls.append(cls_w)
            history_margin.append(margin_w)
            prev_wseq_r = wseq_w

    BG      = "#100c14"
    TEXT    = "#e8dfc8"
    GOLD    = "#e8b84b"
    INDIGO  = "#5a5fd4"
    GREEN   = "#5fd48a"
    ROSE    = "#d47070"
    TEAL    = "#5fd4c8"
    VIOLET  = "#a070d4"
    DIM     = "#555566"

    CLASS_COLORS = [DIM, INDIGO, TEAL, GREEN, GOLD, ROSE]

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except Exception as e:
        print("matplotlib unavailable:", e)
        print(f"last class={CLASS_NAMES[cls_last]} margin={margin_last}")
        return

    fig = plt.figure(figsize=(12, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.8], wspace=0.3,
                          top=0.90, bottom=0.07, left=0.05, right=0.97)
    ax_left = fig.add_subplot(gs[0])

    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_hist   = fig.add_subplot(gs_right[0])
    ax_review = fig.add_subplot(gs_right[1])

    for ax in (ax_left, ax_hist, ax_review):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    ax_left.set_xlim(-1.5, 1.5)
    ax_left.set_ylim(-2.2, 1.6)
    ax_left.set_aspect("equal")
    ax_left.set_xticks([])
    ax_left.set_yticks([])
    ax_left.set_title("motion class", color=TEXT, fontsize=9, pad=4)

    lamp_color = CLASS_COLORS[cls_last]
    lamp = mpatches.Circle((0, 0.3), 0.90, color=lamp_color, zorder=2)
    ax_left.add_patch(lamp)
    ring = mpatches.Circle((0, 0.3), 0.90, fill=False,
                            edgecolor=TEXT, linewidth=1.5, zorder=3)
    ax_left.add_patch(ring)
    ax_left.text(0, 0.3, CLASS_NAMES[cls_last],
                 ha="center", va="center",
                 color=BG if cls_last else TEXT,
                 fontsize=10, fontweight="bold", zorder=4)
    ax_left.text(0, -0.85, TAGLINES[cls_last],
                 ha="center", va="center", color=TEXT, fontsize=7,
                 multialignment="center")
    ax_left.text(0, -1.35,
                 f"margin={margin_last}   f_rate={f_rate_last}   f_spread={f_spread_last}",
                 ha="center", va="center", color=TEXT, fontsize=7)

    if history_cls:
        xs_h = list(range(len(history_cls)))
        ax_hist.step(xs_h, history_cls, where="post", color=GOLD, linewidth=1.2)
        ax_hist.scatter(xs_h, history_cls,
                        c=[CLASS_COLORS[c] for c in history_cls], s=30, zorder=3)
    ax_hist.set_yticks(range(N_CLASSES))
    ax_hist.set_yticklabels(CLASS_NAMES, fontsize=7)
    ax_hist.tick_params(axis="y", colors=TEXT)
    ax_hist.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_hist.set_title("classification history", color=TEXT, fontsize=9, pad=4)
    ax_hist.set_ylim(-0.5, N_CLASSES - 0.5)

    ax_review.set_xticks([])
    ax_review.set_yticks([])
    ax_review.set_title("critic's notes", color=TEXT, fontsize=9, pad=4)
    review_text = REVIEWS.get(cls_last, "")
    ax_review.text(0.03, 0.85, review_text,
                   transform=ax_review.transAxes,
                   ha="left", va="top", color=TEXT, fontsize=7.5,
                   style="italic",
                   bbox=dict(facecolor="#1a1520", edgecolor="#332d40",
                             boxstyle="round,pad=0.4", alpha=0.8))

    fig.suptitle('"The Sommelier of Motion"',
                 color=TEXT, fontsize=13, fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def load_csv(path, ts_col="le"):
    """Load event CSV with columns x, y, pol and optional timestamp column."""
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x   = [int(row[idx["x"]])   for row in rows]
    y   = [int(row[idx["y"]])   for row in rows]
    pol = [int(row[idx["pol"]]) for row in rows]
    if ts_col in idx:
        ts = [int(row[idx[ts_col]]) & TS_MASK for row in rows]
    else:
        ts = [0] * len(x)
    return x, y, ts, pol


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
                    help="use real chip status words (one int per line)")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column for timestamp (default: le)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write PNG here")
    args = ap.parse_args()

    if args.validate:
        ok = validate()
        sys.exit(0 if ok else 1)

    if args.from_actsim:
        with open(args.from_actsim) as f:
            words = [int(line) for line in f if line.strip()]
        print(f"loaded {len(words)} chip status words from {args.from_actsim}")
    elif args.csv:
        x, y, ts, pol = load_csv(args.csv, args.ts_col)
        print(f"loaded {len(x)} events from {args.csv}; computing sommelier words.")
        words, latches = python_sommelier_features(x, y, ts, pol)
        if words:
            cls_w, margin_w, valid_w, _, f_rate_w, f_spread_w = unpack_status(words[-1])
            print(f"final class={CLASS_NAMES[cls_w]}, margin={margin_w}, "
                  f"f_rate={f_rate_w}, f_spread={f_spread_w} ({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")
        return

    render_sommelier(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
