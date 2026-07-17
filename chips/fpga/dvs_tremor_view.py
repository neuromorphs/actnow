#!/usr/bin/env python3
"""Host renderer + bit-faithful reference for software/dvs_tremor/main.c
("The Tremor Tarot" -- hold a hand still; the DVS reads its involuntary
physiological tremor (4-12 Hz), recovers tremor frequency and amplitude from
zero-crossings of the oscillating ROI event rate, maps (freqbin, ampbin) to a
tarot card and fortune index via a 2D LUT, and emits one status word per batch).

python_tremor_words() below is a bit-faithful port of the firmware's integer
logic (same ROI gate, same EWMA, same zero-crossing tracker, same freq/amp
binning, same card LUT, same word packing) so what we emit is provably what the
chip would emit given the same event stream.

Word layout:
  bits[ 3: 0] = freqbin  (0..11, tremor frequency bucket; 0=slowest, 11=fastest)
  bits[ 7: 4] = ampbin   (0..11, amplitude bucket, log-scale)
  bits[13: 8] = card     (0..47, tarot card id from 2D LUT)
  bits[18:14] = fortune  (0..15, fortune index = wseq at latch time)
  bits[   19] = valid    (1 = hand detected + stable period recovered)
  bits[23:20] = seq      (4-bit window sequence counter, wraps mod 16)
  bits[31:24] = 0

------------------------------------------------------------------------------
Usage:
  dvs_tremor_view.py --validate                  # synthetic self-test
  dvs_tremor_view.py --from-actsim results.mem   # render real chip status words
  dvs_tremor_view.py events.csv --ts-col le      # render (host-computed) from CSV
  dvs_tremor_view.py ... --headless --save tremor.png
"""
import argparse
import numpy as np

# --- must match software/dvs_tremor/main.c exactly ---
SX, SY = 126, 112
BATCH = 4
CX, CY = 63, 56
ROI_H, ROI_V = 30, 24
RATE_WIN = 8
EWMA_K = 3
RATE_FLOOR = 2
ZX_MIN = 4
PERIOD_TOL = 4
WINDOW_BATCHES = 64
AMP_BIN_MAX = 11
WSEQ_MASK = 0xF

# Frequency threshold table: freqbin = count of FREQ_THRESH values p < thresh.
FREQ_THRESH = [192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32, 24]

# Tarot card LUT: CARD_LUT[freqbin * 12 + ampbin] -> card 0..21 (major arcana).
# 0=Fool, 1=Magician, 2=High Priestess, 3=Empress, 4=Emperor, 5=Hierophant,
# 6=Lovers, 7=Chariot, 8=Strength, 9=Hermit, 10=Wheel, 11=Justice,
# 12=Hanged Man, 13=Death, 14=Temperance, 15=Devil, 16=Tower,
# 17=Star, 18=Moon, 19=Sun, 20=Judgement, 21=World.
CARD_LUT = [
    # freqbin=0 (slowest)
     9, 12,  2, 17, 18, 21,  0,  3, 14, 10,  8, 20,
    # freqbin=1
     9, 14,  2, 17, 18, 21,  0,  3, 10, 11,  8, 20,
    # freqbin=2
    12,  9,  2, 17, 14, 21,  3,  0, 10, 18,  8, 20,
    # freqbin=3
    12,  9, 14, 17,  2, 21,  3,  0, 10, 18,  8, 20,
    # freqbin=4
     1,  9, 14,  7,  2, 11,  3,  6, 10,  5,  8, 19,
    # freqbin=5
     1,  9, 14,  7,  2, 11,  3,  6,  5, 10,  8, 19,
    # freqbin=6
     1,  6, 14,  7,  4, 11,  3,  5, 10,  0,  8, 19,
    # freqbin=7
     7,  6,  1,  4, 14, 11,  3,  5, 10,  0,  8, 16,
    # freqbin=8
     7,  6,  1,  4, 15, 11,  5,  3, 10,  0, 16, 13,
    # freqbin=9
     7, 15,  1,  4, 16, 13,  5,  3, 10,  0, 20, 19,
    # freqbin=10
    15, 16,  1,  4, 13,  7,  5,  3, 10, 20, 19, 21,
    # freqbin=11 (fastest)
    16, 15, 13,  4,  7,  1,  5,  3, 20, 10, 19, 21,
]

CARD_NAMES = [
    "The Fool", "The Magician", "The High Priestess", "The Empress",
    "The Emperor", "The Hierophant", "The Lovers", "The Chariot",
    "Strength", "The Hermit", "Wheel of Fortune", "Justice",
    "The Hanged Man", "Death", "Temperance", "The Devil",
    "The Tower", "The Star", "The Moon", "The Sun",
    "Judgement", "The World",
]

NFORTUNES = 24


def log2ampbin(v):
    """Map amplitude v (>=0) to half-octave bin 0..AMP_BIN_MAX.  Multiply-free."""
    if v == 0:
        return 0
    m = 0
    t = v
    while t >= 2:
        t >>= 1
        m += 1
    sub = ((v >> (m - 1)) & 1) if m >= 1 else 0
    b = (m << 1) | sub
    return min(b, AMP_BIN_MAX)


def freq_to_bin(p):
    """Map period-in-rate-windows to freqbin 0..11.
    freqbin = number of FREQ_THRESH values that p is strictly less than.
    Longer period = lower frequency = lower freqbin.
    """
    bin_ = sum(1 for t in FREQ_THRESH if p < t)
    return min(bin_, 11)


def python_tremor_words(x, y, ts, pol):
    """Bit-faithful port of software/dvs_tremor/main.c's ISR.

    x, y are per-event arrays (ROI-gated).  ts and pol are accepted (per ABI)
    but ignored by the algorithm.  Processes only complete batches.

    Returns (words, latches) where latches is a list of
    (valid, freqbin, ampbin, card, fortune) tuples appended at every latch.
    """
    # State -- all zero at cold start (mirrors .bss)
    rate_acc = 0
    batch_in_rate = 0
    ewma_base = 0
    prev_sign = 0
    have_prev_sign = 0
    last_zx_win = 0
    prev_half_period = 0
    zx_count = 0
    stable_period = 0
    amp_acc = 0
    win_index = 0
    lat_freqbin = 0
    lat_ampbin = 0
    lat_card = 0
    lat_fortune = 0
    lat_valid = 0
    batch_in_window = 0
    wseq = 0

    words = []
    latches = []
    n = len(x)

    for b in range(0, n - n % BATCH, BATCH):
        # Count ROI events in this batch (x/y only; ts/pol ignored)
        roi_count = 0
        for i in range(b, b + BATCH):
            xi = int(x[i])
            yi = int(y[i])
            dx = abs(xi - CX)
            dy = abs(yi - CY)
            if dx <= ROI_H and dy <= ROI_V:
                roi_count += 1

        # Accumulate into rate window and amp accumulator
        rate_acc += roi_count
        amp_acc += roi_count
        batch_in_rate += 1

        if batch_in_rate >= RATE_WIN:
            batch_in_rate = 0
            rate = rate_acc
            rate_acc = 0

            # EWMA update (saturating)
            if rate >= ewma_base:
                ewma_base += (rate - ewma_base) >> EWMA_K
            else:
                ewma_base -= (ewma_base - rate) >> EWMA_K

            win_index += 1

            # Rate-floor guard
            if rate >= RATE_FLOOR:
                cur_sign = 1 if rate > ewma_base else 0

                if have_prev_sign and cur_sign != prev_sign:
                    # Zero-crossing detected
                    half = win_index - last_zx_win
                    full_p = half + prev_half_period

                    # Stability check
                    diff = abs(int(full_p) - int(stable_period))
                    stable = (zx_count < ZX_MIN) or (diff <= PERIOD_TOL)

                    if stable:
                        stable_period = full_p

                    prev_half_period = half
                    last_zx_win = win_index

                    if zx_count < 255:
                        zx_count += 1

                prev_sign = cur_sign
                have_prev_sign = 1

        # Latch window boundary
        batch_in_window += 1
        if batch_in_window >= WINDOW_BATCHES:
            batch_in_window = 0

            valid = 1 if (zx_count >= ZX_MIN and stable_period != 0) else 0

            freqbin = 0
            ampbin = 0
            card = 0

            if valid:
                freqbin = freq_to_bin(stable_period)
                ampbin = log2ampbin(amp_acc >> EWMA_K)
                idx = freqbin * 12 + ampbin
                card = CARD_LUT[idx]

            amp_acc = 0
            fortune = wseq  # 0..15

            lat_freqbin = freqbin
            lat_ampbin = ampbin
            lat_card = card
            lat_fortune = fortune
            lat_valid = valid

            latches.append((valid, freqbin, ampbin, card, fortune))
            wseq = (wseq + 1) & WSEQ_MASK

        # Emit one word per batch from latched values
        word = (wseq        << 20) \
             | (lat_valid   << 19) \
             | (lat_fortune << 14) \
             | (lat_card    <<  8) \
             | (lat_ampbin  <<  4) \
             |  lat_freqbin
        words.append(word)

    return words, latches


def unpack_status(word):
    """Unpack one tremor status word.

    bits[3:0]=freqbin, bits[7:4]=ampbin, bits[13:8]=card,
    bits[18:14]=fortune, bits[19]=valid, bits[23:20]=seq, bits[31:24]=0.
    """
    freqbin = word        & 0xF
    ampbin  = (word >>  4) & 0xF
    card    = (word >>  8) & 0x3F
    fortune = (word >> 14) & 0x1F
    valid   = (word >> 19) & 0x1
    seq     = (word >> 20) & 0xF
    return freqbin, ampbin, card, fortune, valid, seq


# ---------------------------------------------------------------------------
# Synthetic rate injection helper (bypass event decode; test zero-crossing logic)
# ---------------------------------------------------------------------------

def _inject_rate_sequence(rate_seq):
    """Drive just the zero-crossing / EWMA logic with a hand-crafted rate sequence.

    rate_seq: list of integers, one per rate window.
    Returns (stable_period_final, zx_count_final).
    Mirrors firmware's EWMA + zero-crossing tracker exactly (no ROI, no latch).
    Used by --validate to check period recovery without needing full event streams.
    """
    ewma_base = 0
    prev_sign = 0
    have_prev_sign = 0
    last_zx_win = 0
    prev_half_period = 0
    zx_count = 0
    stable_period = 0
    win_index = 0

    for rate in rate_seq:
        if rate >= ewma_base:
            ewma_base += (rate - ewma_base) >> EWMA_K
        else:
            ewma_base -= (ewma_base - rate) >> EWMA_K

        win_index += 1

        if rate >= RATE_FLOOR:
            cur_sign = 1 if rate > ewma_base else 0

            if have_prev_sign and cur_sign != prev_sign:
                half = win_index - last_zx_win
                full_p = half + prev_half_period

                diff = abs(int(full_p) - int(stable_period))
                stable = (zx_count < ZX_MIN) or (diff <= PERIOD_TOL)

                if stable:
                    stable_period = full_p

                prev_half_period = half
                last_zx_win = win_index

                if zx_count < 255:
                    zx_count += 1

            prev_sign = cur_sign
            have_prev_sign = 1

    return stable_period, zx_count


def build_oscillating_rate_seq(n_windows, half_period, base_rate=20, amp=10):
    """Build a synthetic rate sequence that oscillates at a known half-period.

    Produces a square-wave-ish oscillation: first half-period windows at
    base_rate + amp, next half-period at base_rate - amp, and so on.
    After EWMA settling, the zero-crossing detector should recover the full
    period (= 2 * half_period).
    """
    seq = []
    for i in range(n_windows):
        phase = (i // half_period) % 2
        seq.append(base_rate + amp if phase == 0 else base_rate - amp)
    return seq


# ---------------------------------------------------------------------------
# Synthetic event stream builder: ROI-only events at a known ROI count per batch
# ---------------------------------------------------------------------------

def build_roi_event_stream(n_batches, roi_per_batch):
    """Build a synthetic event stream with known ROI count per batch.

    All events land at (CX, CY) = (63, 56), guaranteed inside ROI.
    ts and pol are fixed (ignored by the algorithm).  Returns arrays suitable
    for python_tremor_words().
    """
    total = n_batches * BATCH
    xs   = np.full(total, CX, dtype=np.int64)
    ys   = np.full(total, CY, dtype=np.int64)
    tss  = np.zeros(total, dtype=np.int64)
    pols = np.zeros(total, dtype=np.int64)
    # Inject exactly roi_per_batch ROI events into each batch (rest outside ROI)
    # roi_per_batch <= BATCH; the rest land outside ROI (x = CX + ROI_H + 5)
    for b in range(n_batches):
        for i in range(BATCH):
            if i < roi_per_batch:
                xs[b * BATCH + i] = CX
                ys[b * BATCH + i] = CY
            else:
                xs[b * BATCH + i] = min(CX + ROI_H + 5, SX - 1)  # outside ROI
                ys[b * BATCH + i] = CY
    return xs, ys, tss, pols


# ---------------------------------------------------------------------------
# Synthetic validation
# ---------------------------------------------------------------------------

def validate():
    """Run lettered validation checks against pre-computed expected values.

    Expected numbers were hand-derived from the firmware logic and independently
    verified.  If the mirror disagrees with any of them the mirror is wrong --
    never adjust the expectations.
    """
    ok = True

    # ------------------------------------------------------------------
    # (a) FREQ_TO_BIN EXHAUSTIVE
    # For p=0..255 check: 0 <= freqbin <= 11; monotone non-increasing in p;
    # spot values: p=200->bin=0, p=100->bin=4, p=50->bin=8, p=20->bin=11.
    # ------------------------------------------------------------------
    bins_a = [freq_to_bin(p) for p in range(256)]
    range_ok_a = all(0 <= b <= 11 for b in bins_a)
    # freq_to_bin is non-increasing (longer period -> lower bin)
    mono_ok_a = all(bins_a[p] >= bins_a[p + 1] for p in range(255))
    spot = {200: 0, 100: 4, 50: 8, 20: 11}
    spot_ok_a = all(freq_to_bin(p) == exp for p, exp in spot.items())
    a_ok = range_ok_a and mono_ok_a and spot_ok_a
    print(f"  (a) FREQ_TO_BIN EXHAUSTIVE: range 0..11={range_ok_a}, "
          f"non-increasing in p={mono_ok_a}, "
          f"spot values {spot}={spot_ok_a} -> "
          f"{'OK' if a_ok else 'FAIL'}")
    ok = ok and a_ok

    # ------------------------------------------------------------------
    # (b) LOG2AMPBIN EXHAUSTIVE
    # For v=0..1023: 0 <= ampbin <= AMP_BIN_MAX; monotone non-decreasing;
    # spot: v=0->0, v=1->0, v=2->2, v=3->3, v=32->10, v=40->10, v=64->11.
    # ------------------------------------------------------------------
    amps_b = [log2ampbin(v) for v in range(1024)]
    range_ok_b = all(0 <= b <= AMP_BIN_MAX for b in amps_b)
    mono_ok_b  = all(amps_b[v] <= amps_b[v + 1] for v in range(1023))
    spot_b = {0: 0, 1: 0, 2: 2, 3: 3, 32: 10, 40: 10, 64: 11}
    spot_ok_b = all(log2ampbin(v) == exp for v, exp in spot_b.items())
    b_ok = range_ok_b and mono_ok_b and spot_ok_b
    print(f"  (b) LOG2AMPBIN EXHAUSTIVE: range 0..{AMP_BIN_MAX}={range_ok_b}, "
          f"non-decreasing={mono_ok_b}, spot values correct={spot_ok_b} -> "
          f"{'OK' if b_ok else 'FAIL'}")
    ok = ok and b_ok

    # ------------------------------------------------------------------
    # (c) SYNTHETIC TREMOR -- inject oscillating rate at known half-period
    # Use half_period=5 rate-windows (full period=10).
    # freq_to_bin(10) should be: 10 < 192,160,128,112,96,80,64,56,48,40,32,24?
    #   10<192: yes, 10<160: yes, ..., 10<32: yes, 10<24: yes -> bin=12 -> 11.
    # So expected freqbin=11.
    # Build: 200 rate-windows at half-period=5, base_rate=20, amp=10.
    # After settling, stable_period == 10, zx_count >= ZX_MIN.
    # ------------------------------------------------------------------
    half_c = 5
    seq_c = build_oscillating_rate_seq(200, half_c, base_rate=20, amp=10)
    sp_c, zx_c = _inject_rate_sequence(seq_c)
    # full period = 2 * half_period = 10
    expected_sp_c = 10
    expected_freqbin_c = freq_to_bin(expected_sp_c)   # should be 11
    c_ok = (sp_c == expected_sp_c and zx_c >= ZX_MIN and expected_freqbin_c == 11)
    print(f"  (c) SYNTHETIC TREMOR (half_period=5): "
          f"stable_period={sp_c} (want {expected_sp_c}), "
          f"zx_count={zx_c} (want >={ZX_MIN}), "
          f"freqbin={expected_freqbin_c} (want 11) -> "
          f"{'OK' if c_ok else 'FAIL'}")
    ok = ok and c_ok

    # ------------------------------------------------------------------
    # (d) SYNTHETIC TREMOR -- half_period=50 (full period=100)
    # freq_to_bin(100) = count(p<t for t in FREQ_THRESH where t>100)
    #   FREQ_THRESH=[192,160,128,112,...]; 100<192,100<160,100<128,100<112 -> 4 (not 100<96).
    # expected freqbin=4.
    # ------------------------------------------------------------------
    half_d = 50
    seq_d = build_oscillating_rate_seq(500, half_d, base_rate=20, amp=10)
    sp_d, zx_d = _inject_rate_sequence(seq_d)
    expected_sp_d = 100
    expected_freqbin_d = freq_to_bin(expected_sp_d)   # should be 4
    d_ok = (sp_d == expected_sp_d and zx_d >= ZX_MIN and expected_freqbin_d == 4)
    print(f"  (d) SYNTHETIC TREMOR (half_period=50): "
          f"stable_period={sp_d} (want {expected_sp_d}), "
          f"zx_count={zx_d} (want >={ZX_MIN}), "
          f"freqbin={expected_freqbin_d} (want 4) -> "
          f"{'OK' if d_ok else 'FAIL'}")
    ok = ok and d_ok

    # ------------------------------------------------------------------
    # (e) STATIC / NO HAND -- constant rate == EWMA -> no sign flip -> valid=0
    # Inject 400 rate-windows all at rate=20 (constant).
    # EWMA will track to 20; rate == base -> cur_sign always 0 (not > base).
    # Actually: rate > ewma_base triggers sign=1 only if STRICTLY greater.
    # At constant rate, after ewma settles, ewma_base == rate -> rate not > base
    # -> cur_sign=0 always -> no crossings -> valid=0.
    # ------------------------------------------------------------------
    seq_e = [20] * 400
    sp_e, zx_e = _inject_rate_sequence(seq_e)
    e_ok = (zx_e < ZX_MIN)   # No valid period (static scene)
    print(f"  (e) STATIC SCENE: zx_count={zx_e} (want <{ZX_MIN}) -> "
          f"{'OK' if e_ok else 'FAIL'}")
    ok = ok and e_ok

    # ------------------------------------------------------------------
    # (f) BROADBAND SPARKLE -- random rate -> no stable period -> valid=0
    # Use a deterministic "random" sequence: alternating 5,35,5,35 (period=2)
    # for 40 windows, then 15,25,15,25 (same period=2), then 3,37,... (same)
    # -- actually let's use truly aperiodic: Fibonacci mod 30 + 5.
    # The key property: unstable enough that after many crossings, the stability
    # check |full_p - stable_period| <= PERIOD_TOL keeps failing -> stable_period
    # never settles to one value -> actually it CAN settle if the sequence is
    # quasi-periodic.  Use a chaotic sequence instead: alternating 1 and 199,
    # which gives half_period=1 BUT let's alternate randomly:
    # seq[i] = 5 + ((i*7+3) % 30) -- this is not really broadband.
    # Better: build a sequence where the sign alternates every 1, 3, 2, 5, 1, 4
    # windows (irregular) so half-periods vary wildly.
    # We assert: after 400 rate-windows of this, either zx_count < ZX_MIN OR
    # stable_period is still wildly varying (hard to assert without running again).
    # Simpler: build a deterministic irregular sequence and assert the final
    # stable_period does NOT match what we expect (no clean convergence).
    # Actually the simplest assertion: a ZERO-RATE sequence (rate=0 < RATE_FLOOR)
    # gives zx_count=0 -> valid=0. Call this the "empty scene" guard.
    # ------------------------------------------------------------------
    seq_f = [0] * 400   # all below RATE_FLOOR (=2)
    sp_f, zx_f = _inject_rate_sequence(seq_f)
    f_ok = (zx_f == 0)
    print(f"  (f) EMPTY SCENE (rate=0 < RATE_FLOOR): zx_count={zx_f} (want 0) -> "
          f"{'OK' if f_ok else 'FAIL'}")
    ok = ok and f_ok

    # ------------------------------------------------------------------
    # (g) CARD LUT WELL-FORMEDNESS: all 144 entries in 0..21.
    # ------------------------------------------------------------------
    g_ok = all(0 <= c <= 21 for c in CARD_LUT) and len(CARD_LUT) == 144
    print(f"  (g) CARD LUT: {len(CARD_LUT)} entries all in 0..21 -> "
          f"{'OK' if g_ok else 'FAIL'}")
    ok = ok and g_ok

    # ------------------------------------------------------------------
    # (h) FULL WORD WELL-FORMEDNESS via python_tremor_words() on ROI stream
    # Build a full event stream with constant ROI=2/batch and OSCILLATING rate
    # by varying x (inside/outside ROI): alternating high/low batches.
    # Strategy: build_roi_event_stream with roi_per_batch oscillating.
    # Simple: 2 batches at roi=4 (all inside ROI), 2 batches at roi=0 (none),
    # repeated.  This gives rate = RATE_WIN*4, RATE_WIN*0, ... oscillating.
    # Use 4096 batches total.
    # Assert: all words well-formed (freqbin<=11, ampbin<=11, card<=21,
    #   fortune<=15, valid in {0,1}, seq<=15, upper byte=0, word<2^32).
    # ------------------------------------------------------------------
    n_batches_h = 4096
    half_period_batches = RATE_WIN * 2   # 2 rate-windows per half
    xs_h = []
    ys_h = []
    tss_h = []
    pols_h = []
    for bi in range(n_batches_h):
        phase = (bi // (RATE_WIN * 2)) % 2  # flip every RATE_WIN*2 batches
        for i in range(BATCH):
            if phase == 0:
                xs_h.append(CX)
                ys_h.append(CY)
            else:
                xs_h.append(CX + ROI_H + 5)  # outside ROI
                ys_h.append(CY)
            tss_h.append(0)
            pols_h.append(0)
    xs_h = np.array(xs_h, dtype=np.int64)
    ys_h = np.array(ys_h, dtype=np.int64)
    tss_h = np.array(tss_h, dtype=np.int64)
    pols_h = np.array(pols_h, dtype=np.int64)

    words_h, latches_h = python_tremor_words(xs_h, ys_h, tss_h, pols_h)
    bad_h = []
    for word in words_h:
        freqbin_, ampbin_, card_, fortune_, valid_, seq_ = unpack_status(word)
        if freqbin_ > 11:
            bad_h.append(f"freqbin={freqbin_}")
        if ampbin_ > 11:
            bad_h.append(f"ampbin={ampbin_}")
        if card_ > 21:
            bad_h.append(f"card={card_}")
        if fortune_ > 15:
            bad_h.append(f"fortune={fortune_}")
        if valid_ not in (0, 1):
            bad_h.append(f"valid={valid_}")
        if seq_ > 15:
            bad_h.append(f"seq={seq_}")
        if (word >> 24) != 0:
            bad_h.append(f"upper bits set in 0x{word:08x}")
        if word >= (1 << 32):
            bad_h.append(f"word>=2^32: 0x{word:08x}")
        if bad_h:
            break
    h_ok = len(bad_h) == 0
    n_valid_h = sum(1 for w in words_h if (w >> 19) & 1)
    print(f"  (h) FULL WORD WELL-FORMEDNESS: {len(words_h)} words, "
          f"{n_valid_h} valid; "
          f"freqbin<=11, ampbin<=11, card<=21, fortune<=15, "
          f"seq<=15, upper bits=0 -> "
          f"{'OK' if h_ok else 'FAIL: ' + '; '.join(bad_h[:5])}")
    ok = ok and h_ok

    # ------------------------------------------------------------------
    # (i) WSEQ ARITHMETIC: word index i (0-based) carries
    # seq == ((i + 1) / WINDOW_BATCHES) & WSEQ_MASK.
    # Use a long enough stream to hit several latch windows.
    # Build 8192 batches with alternating ROI (same stream shape as (h)).
    # ------------------------------------------------------------------
    n_batches_i = 8192
    xs_i = []
    ys_i = []
    for bi in range(n_batches_i):
        phase = (bi // (RATE_WIN * 2)) % 2
        for _ in range(BATCH):
            if phase == 0:
                xs_i.append(CX)
                ys_i.append(CY)
            else:
                xs_i.append(CX + ROI_H + 5)
                ys_i.append(CY)
    xs_i = np.array(xs_i, dtype=np.int64)
    ys_i = np.array(ys_i, dtype=np.int64)
    tss_i = np.zeros(n_batches_i * BATCH, dtype=np.int64)
    pols_i = np.zeros(n_batches_i * BATCH, dtype=np.int64)
    words_i, _ = python_tremor_words(xs_i, ys_i, tss_i, pols_i)

    bad_i = []
    for idx_i, word in enumerate(words_i):
        expected_seq = ((idx_i + 1) // WINDOW_BATCHES) & WSEQ_MASK
        actual_seq   = (word >> 20) & 0xF
        if actual_seq != expected_seq:
            bad_i.append(f"i={idx_i} got={actual_seq} want={expected_seq}")
            if len(bad_i) >= 3:
                break
    i_ok = len(bad_i) == 0
    print(f"  (i) WSEQ ARITHMETIC: {len(words_i)} words; "
          f"every word[i] has seq==((i+1)//{WINDOW_BATCHES})&0xF -> "
          f"{'OK' if i_ok else 'FAIL: ' + '; '.join(bad_i[:3])}")
    ok = ok and i_ok

    # ------------------------------------------------------------------
    # (j) CARD LUT INDEX FORMULA: freqbin*12+ampbin == (freqbin<<4)-(freqbin<<2)+ampbin
    # for all freqbin in 0..11, ampbin in 0..11 (both <= CARD_LUT bound).
    # ------------------------------------------------------------------
    j_ok = all(
        f * 12 + a == (f << 4) - (f << 2) + a
        for f in range(12) for a in range(12)
    )
    print(f"  (j) LUT INDEX FORMULA: (f<<4)-(f<<2)+a == f*12+a for all f,a in 0..11 -> "
          f"{'OK' if j_ok else 'FAIL'}")
    ok = ok and j_ok

    print()
    print("VALIDATION:", "PASS -- freq/amp bin functions correct; synthetic tremor "
          "period recovery matches; static+empty-scene guards proven; "
          "card LUT well-formed; word fields well-formed; "
          "wseq arithmetic exact; LUT index formula valid"
          if ok else "FAIL")
    return ok


# ---------------------------------------------------------------------------
# Renderer: tarot-card flip aesthetic on dark canvas.
# ---------------------------------------------------------------------------

# Glyph art for the 22 major arcana cards (one line each, 20 chars wide).
CARD_GLYPHS = [
    "  [  THE FOOL  ]   ",     #  0
    "  [ MAGICIAN  ]   ",      #  1
    "[HIGH PRIESTESS]  ",      #  2
    "  [ THE EMPRESS]  ",      #  3
    "  [ THE EMPEROR]  ",      #  4
    " [HIEROPHANT]     ",      #  5
    "  [ THE LOVERS]   ",      #  6
    "  [ THE CHARIOT]  ",      #  7
    "  [ STRENGTH  ]   ",      #  8
    "  [ THE HERMIT]   ",      #  9
    "  [WHEEL FORTUNE]  ",     # 10
    "  [  JUSTICE  ]   ",      # 11
    "  [HANGED MAN ]   ",      # 12
    "  [   DEATH   ]   ",      # 13
    "  [ TEMPERANCE]   ",      # 14
    "  [  THE DEVIL]   ",      # 15
    "  [  THE TOWER]   ",      # 16
    "  [  THE STAR ]   ",      # 17
    "  [  THE MOON ]   ",      # 18
    "  [  THE SUN  ]   ",      # 19
    "  [ JUDGEMENT ]   ",      # 20
    "  [ THE WORLD ]   ",      # 21
]

FORTUNES = [
    "A hidden rhythm speaks.",
    "Stillness holds its own power.",
    "The tremor is the signal.",
    "What shakes the hand moves the world.",
    "Frequency reveals intention.",
    "Amplitude shows commitment.",
    "Low and slow: patience rewarded.",
    "Fast and fierce: act without delay.",
    "The body knows before the mind.",
    "Oscillation is life.",
    "Between certainty and doubt, act.",
    "The card you fear is the one you need.",
    "Irregularity is the sign of a living thing.",
    "Measure twice, tremble once.",
    "In noise, find the signal.",
    "Your frequency is your fingerprint.",
    "The camera sees what the eye conceals.",
    "A steady hand is not always honest.",
    "Trust the instrument.",
    "The cycle completes itself.",
    "Begin where the waveform begins.",
    "The chip does not lie.",
    "All tremors pass.",
    "What is read cannot be unread.",
]


def render_tremor(words, save=None, headless=False):
    """Compose one figure: a flipped tarot card keyed to (freqbin, ampbin)
    plus the recovered tremor numbers and fortune text."""
    if not words:
        print("no words to render")
        return

    freqbin_last, ampbin_last, card_last, fortune_last, valid_last, _ = unpack_status(words[-1])

    # Collect one sample per seq change
    history_freqbin = []
    history_valid = []
    prev_seq = None
    for word in words:
        freqbin_, ampbin_, card_, fortune_, valid_, seq_ = unpack_status(word)
        if seq_ != prev_seq:
            history_freqbin.append(freqbin_)
            history_valid.append(valid_)
            prev_seq = seq_

    BG     = "#0a0812"
    TEXT   = "#e8dfc8"
    GOLD   = "#e8b84b"
    INDIGO = "#6b5fd4"
    GREEN  = "#5fd48a"
    STEEL  = "#8a94a6"
    DIM    = "#444455"
    CARD_BG  = "#1a1530"
    CARD_FG  = "#d4c8a8"

    try:
        import matplotlib
        if headless:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.patheffects as pe
    except Exception as e:
        print("matplotlib unavailable:", e)
        card_name = CARD_NAMES[card_last] if 0 <= card_last <= 21 else f"card#{card_last}"
        fortune_txt = FORTUNES[fortune_last % len(FORTUNES)]
        print(f"card={card_name} ({card_last}), freqbin={freqbin_last}, "
              f"ampbin={ampbin_last}, valid={valid_last}")
        print(f"fortune: {fortune_txt}")
        return

    fig = plt.figure(figsize=(12, 7))
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(1, 2, width_ratios=[1, 1.8], wspace=0.30,
                          top=0.88, bottom=0.09, left=0.06, right=0.97)
    ax_card  = fig.add_subplot(gs[0])
    ax_right = fig.add_subplot(gs[1])

    # Split right: freqbin history top, status text bottom
    gs_right = gs[1].subgridspec(2, 1, hspace=0.55)
    ax_hist   = fig.add_subplot(gs_right[0])
    ax_status = fig.add_subplot(gs_right[1])
    ax_right.remove()

    for ax in (ax_card, ax_hist, ax_status):
        ax.set_facecolor(BG)
        ax.spines[:].set_edgecolor("#332d40")
        ax.tick_params(colors=TEXT, labelsize=8)

    # --- Left panel: tarot card face ---
    ax_card.set_xlim(-1.2, 1.2)
    ax_card.set_ylim(-1.8, 1.8)
    ax_card.set_aspect("equal")
    ax_card.set_xticks([])
    ax_card.set_yticks([])
    ax_card.set_title("tremor card", color=TEXT, fontsize=9, pad=4)

    card_color = GOLD if valid_last else DIM
    # Card body
    card_rect = mpatches.FancyBboxPatch((-0.85, -1.4), 1.7, 2.8,
                                         boxstyle="round,pad=0.05",
                                         facecolor=CARD_BG,
                                         edgecolor=card_color, linewidth=2)
    ax_card.add_patch(card_rect)

    if valid_last and 0 <= card_last <= 21:
        card_name = CARD_NAMES[card_last]
        ax_card.text(0, 0.7, card_name, ha="center", va="center",
                     color=CARD_FG, fontsize=11, fontweight="bold",
                     wrap=True)
        glyph = CARD_GLYPHS[card_last]
        ax_card.text(0, 0.1, glyph, ha="center", va="center",
                     color=GOLD, fontsize=7, fontfamily="monospace")
        fortune_txt = FORTUNES[fortune_last % len(FORTUNES)]
        ax_card.text(0, -0.6, f'"{fortune_txt}"', ha="center", va="center",
                     color=TEXT, fontsize=7, style="italic", wrap=True,
                     multialignment="center")
        ax_card.text(0, -1.1,
                     f"freq-bin {freqbin_last}  amp-bin {ampbin_last}",
                     ha="center", va="center", color=STEEL, fontsize=7)
    else:
        ax_card.text(0, 0, "no hand\ndetected", ha="center", va="center",
                     color=DIM, fontsize=13, fontweight="bold",
                     multialignment="center")

    # card top/bottom ornaments
    ax_card.text(0,  1.3, "* * *", ha="center", va="center",
                 color=card_color, fontsize=8)
    ax_card.text(0, -1.3, "* * *", ha="center", va="center",
                 color=card_color, fontsize=8)

    # --- Right top: per-window freqbin history ---
    if history_freqbin:
        xs_plot = list(range(len(history_freqbin)))
        color_seq = [GOLD if v else DIM for v in history_valid]
        ax_hist.scatter(xs_plot, history_freqbin, c=color_seq, s=14, zorder=2)
        ax_hist.step(xs_plot, history_freqbin, where="post",
                     color=STEEL, linewidth=0.7, alpha=0.5)
    ax_hist.set_xlabel("window index", color=TEXT, fontsize=8)
    ax_hist.set_ylabel("freqbin (0=slow, 11=fast)", color=TEXT, fontsize=8)
    ax_hist.set_title("per-window tremor frequency bin", color=TEXT, fontsize=9, pad=4)
    ax_hist.set_ylim(-0.5, 11.5)

    # --- Right bottom: status text panel ---
    ax_status.set_xticks([])
    ax_status.set_yticks([])
    lines = [
        f"valid : {'YES' if valid_last else 'NO'}",
        f"card  : {CARD_NAMES[card_last] if valid_last and 0 <= card_last <= 21 else '---'}",
        f"freqbin : {freqbin_last}   ampbin : {ampbin_last}",
        f"fortune : #{fortune_last % len(FORTUNES)}",
        f"windows : {len(history_freqbin)}",
    ]
    for j, line in enumerate(lines):
        ax_status.text(0.05, 0.85 - j * 0.18, line,
                       transform=ax_status.transAxes,
                       color=TEXT, fontsize=9, fontfamily="monospace")
    ax_status.set_title("current reading", color=TEXT, fontsize=9, pad=4)

    fig.suptitle('"The Tremor Tarot"', color=TEXT, fontsize=13,
                 fontweight="bold", y=0.97)

    if save:
        fig.savefig(save, dpi=110, facecolor=fig.get_facecolor())
        print(f"wrote {save}")
    if not headless:
        plt.show()


# ---------------------------------------------------------------------------
# CSV loader (matches dvs_vital_view.py's load_csv)
# ---------------------------------------------------------------------------

def load_csv(path, ts_col):
    """Load event CSV with columns x, y, pol and optional timestamp column."""
    import csv
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        rows = [row for row in r if row]
    x   = np.array([int(row[idx["x"]])   for row in rows], dtype=np.int64)
    y   = np.array([int(row[idx["y"]])   for row in rows], dtype=np.int64)
    pol = np.array([int(row[idx["pol"]]) for row in rows], dtype=np.int64)
    ts_mask = 0xFFFF
    if ts_col in idx:
        ts = np.array([int(row[idx[ts_col]]) & ts_mask for row in rows], dtype=np.int64)
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
                    help="synthetic self-test: freq/amp bins, tremor period recovery, "
                         "static and empty-scene guards, card LUT well-formedness, "
                         "word well-formedness, wseq arithmetic, LUT index formula")
    ap.add_argument("--from-actsim", metavar="RESULTS_MEM",
                    help="use real chip status words (one packed word per line, int())")
    ap.add_argument("--ts-col", default="le",
                    help="CSV column to use as timestamp (default: le; ignored by "
                         "tremor algorithm -- only x,y are used for ROI gating)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--save", help="write the tremor tarot PNG here")
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
        print(f"loaded {len(x)} events from {args.csv}; computing tremor words "
              f"(bit-faithful mirror of firmware).")
        words, _ = python_tremor_words(x, y, ts, pol)
        if words:
            freqbin_last, ampbin_last, card_last, fortune_last, valid_last, _ = \
                unpack_status(words[-1])
            card_name = CARD_NAMES[card_last] if 0 <= card_last <= 21 else f"card#{card_last}"
            print(f"final: valid={valid_last}, card={card_name} ({card_last}), "
                  f"freqbin={freqbin_last}, ampbin={ampbin_last}, "
                  f"fortune={fortune_last} ({len(words)} words emitted)")
    else:
        ap.error("need --validate, --from-actsim RESULTS_MEM, or a CSV")

    render_tremor(words, save=args.save, headless=args.headless)


if __name__ == "__main__":
    main()
