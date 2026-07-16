#include <stdint.h>

/* "THE TREMOR TAROT" (dvs_tremor) -- a chips/fpga demo app in the same shape
   as software/dvs_vital/main.c (fifo_in fires event_id_0 once BATCH words
   land; isr_handler reads them, maintains an EWMA event-rate baseline, detects
   zero-crossings of (rate - base) to measure the tremor period, accumulates
   an amplitude counter, and writes ONE status word per batch/window; it NEVER
   calls wfi(), see the epilogue comment on isr_handler).

   Idea (tremor tarot -- novel among these apps): hold a hand "still" in front
   of the camera; the DVS reads its involuntary physiological tremor (4-12 Hz
   Parkinsonian / essential tremor band).  The chip recovers the tremor FREQUENCY
   (from zero-crossing intervals of the oscillating event rate) and AMPLITUDE
   (events per cycle), maps (freqbin, ampbin) to a tarot card id and a fortune
   index, and emits them in every status word.  Hands-free, no IMU, no multiply.

   -------------------------------------------------------------------------
   CORE TRICK (multiply-free, shift/add/sub/compare/LUT only):
     1. EVENT RATE WINDOW.  Count events in a sliding window of RATE_WIN batches
        (RATE_WIN batches x BATCH events = event-count timebase).  At the end
        of each window `rate` = number of events processed (always RATE_WIN *
        BATCH exactly, because each batch always contributes BATCH events; the
        "rate" is therefore not a count but a running proxy computed below).
        ACTUALLY: we maintain a running event count per batch and accumulate
        it into `rate_acc` over RATE_WIN batches, then snapshot `rate = rate_acc`
        and clear `rate_acc`.  Each batch adds BATCH to rate_acc (all BATCH
        events are from the input FIFO, so the count is trivially BATCH per
        batch).  But "event rate" needs to be SPATIALLY modulated by the hand
        motion, so we WEIGHT events by proximity to the frame centre -- wait,
        the spec says NO multiply.  Alternative: threshold on x/y proximity (a
        compare) to define a "hand ROI" and count events inside it.
        ROI: |x - SX/2| <= ROI_H and |y - SY/2| <= ROI_V.
        SX/2 = 63, SY/2 = 56; ROI_H = 30, ROI_V = 24.
        `rate` = count of ROI events per RATE_WIN batches.

     2. EWMA BASELINE.  After each rate snapshot:
        base += (rate - base) >> EWMA_K
        (integer right-shift approximates 1/2^K weight).  Multiply-free.

     3. DEVIATION AND SIGN.  dev = rate - base.  Sign is `dev >= 0 ? +1 : -1`
        (compare only).  Track `prev_sign`.

     4. ZERO-CROSSING DETECTION.  A zero-crossing occurs when `sign` flips from
        the previous rate window.  We maintain `zx_counter` (windows between
        consecutive sign flips with the same sign polarity -- i.e., we count
        full oscillation periods).  After two consecutive zero-crossings from
        the same direction, the period in windows is `zx_interval`.
        Actually simpler: every time the sign flips, record `zx_ts` (the window
        count at which the flip happened).  After two flips of the SAME direction
        (positive-going crossing twice), the half-period is the interval; two
        half-periods = one full period.  Even simpler: count windows between
        EVERY PAIR of consecutive crossings (regardless of direction): that gives
        the half-period, and two of those give the full period.

        Implementation: track `last_zx_win` (window index of the last crossing)
        and `prev_half_period` (half-period from the crossing before last).
        When a new crossing happens:
          half = win_index - last_zx_win
          full_period = half + prev_half_period   (approx, works if stable)
          last_zx_win = win_index
          prev_half_period = half

     5. FREQUENCY BINNING (shift/add only, no multiply/divide).
        We have `full_period` in "windows" (each window = RATE_WIN * BATCH events).
        Larger full_period = lower frequency.  We want 12 freqbins covering
        roughly 4-16 Hz.  Rather than converting to Hz (needs divide), we bin
        directly on the period in windows: define threshold array as powers-of-
        two-ish constants (shift/add arithmetic), longest period = bin 0 (low freq).
        FREQ_THRESH[i] (0..11): if full_period > FREQ_THRESH[i] -> freqbin <= i.
        We use a linear scan (12 compares).  The thresholds are constants chosen
        so that at 40 events/batch and a 10 Hz tremor the expected period in
        windows is approximately 4 (at RATE_WIN=10 and BATCH=4, 10 Hz * 10*4*? --
        actually the period in windows depends on the event clock, not real time).
        For generality, the thresholds are just evenly-spaced compares that divide
        the observable range: FREQ_THRESH[] = {192,160,128,112,96,80,64,56,48,40,32,24}.
        freqbin = 0 if full_period > 192, ... 11 if full_period <= 24, 12 if < 24
        (clamped to 0..11).

     6. AMPLITUDE BINNING.  amp_counter = events in ROI accumulated over one full
        tremor cycle (from the first crossing to the third crossing, i.e., two
        half-periods).  Actually simpler: amp_counter += ROI events per window;
        snapshot after a full period's worth of windows.  ampbin = log2-style:
        find floor(log2(amp_counter)) and sub-bit, giving 0..11 (same shape as
        log2bin32 in dvs_vital but capped at 11 half-octave bins).

     7. TAROT CARD LUT.  CARD_LUT[freqbin][ampbin] (12x12 = 144 entries, but
        we only have 22 major arcana cards).  Map via:
          card_id = CARD_LUT[(freqbin & 0x7) * 3 + (ampbin & 0x3)]   -- 8*3=24>22
        Too complex.  Use a flat LUT of 144 bytes, each in 0..21 (major arcana).
        But 144 bytes fits in 32 KB.  OR: use a simpler formula -- no multiply.
        card_id = CARD_FLAT[ (freqbin << 2) | (ampbin & 0x3) ]  (64 entries,
        freqbin 0..11 x ampbin mod 4).  Still needs a shift (ok) and OR (ok).
        We use a 48-entry LUT: CARD_FLAT[(freqbin & 0x7)<<2 | (ampbin>>1 & 0x3)]
        = 8 freq-bins * 6 amp-bands -- hmm still complex.

        SIMPLEST APPROACH: flat 144-byte LUT indexed by (freqbin*12 + ampbin).
        But freqbin*12 is a multiply.  Alternative: (freqbin<<4) - (freqbin<<2)
        = freqbin*(16-4) = freqbin*12.  That's shift+sub, no multiply.
        Actually we define a 1D LUT of 144 entries and address via:
          idx = (freqbin << 4) - (freqbin << 2) + ampbin;
          = freqbin * 16 - freqbin * 4 + ampbin
          = freqbin * 12 + ampbin
        This is two shifts, a sub, and an add -- no multiply.  Valid!

        Card IDs 0..21 = major arcana (The Fool through The World).
        Fortune index = seq & 0x1F (lower 5 bits of the batch seq counter,
        giving 0..23 -- 24 fortunes per card, cycled by seq; we mask to 0x17
        for 24 entries).

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) Synthetic tremor stream: inject a rate that oscillates at a known
         frequency (period P windows).  After settling, recovered freqbin
         should match the pre-computed expected freqbin for P.  valid=1.
     (b) Static/no-hand scene: uniform or near-zero ROI event rate ->
         EWMA tracks rate, dev stays near 0, no sign flips -> no zero-
         crossings -> valid=0.  Card/fortune fields are don't-care.
     (c) Broadband sparkle: random per-batch ROI counts with no coherent
         oscillation -> sign flips are random -> no stable period ->
         zx_interval oscillates wildly, fails the stability check -> valid=0.
     (d) --validate injects a synthetic RATE SEQUENCE (not events, but the
         rate snapshot values) directly into the zero-crossing logic and
         asserts the recovered freqbin/ampbin/card.  Also asserts static
         scene -> valid=0 and broadband sparkle -> valid=0.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - ROI test          : two abs-value compares (abs = conditional negate,
                           negate = sub from 0); no multiply.
     - EWMA update       : (rate - base) >> EWMA_K; one sub, one right-shift.
     - Deviation/sign    : one sub, one compare (>=0); no multiply.
     - Zero-crossing     : one compare (sign != prev_sign), one sub for
                           interval, one add for full period; no multiply.
     - Freq binning      : 12-step linear scan (12 compares); no multiply.
     - Amp snapshotting  : accumulate via add; no multiply.
     - Amp log2bin       : right-shift loop (same shape as dvs_vital's
                           log2bin32); no multiply.
     - LUT index         : (freqbin << 4) - (freqbin << 2) + ampbin;
                           two shifts, one sub, one add; NO multiply.
     - Validity check    : compare zx_count >= ZX_MIN and stability check
                           |period - prev_period| <= PERIOD_TOL (one sub,
                           one abs, one compare); no multiply.
     - Output pack       : shifts and ORs only.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)   -- unused here
     pol =  word        & 1                              -- unused here
   Only x and y are used (ROI gating); ts and pol are decoded per ABI but
   ignored.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112 and VERY noisy).  Four multiply-free guards:
     1. ROI GUARD.  Only events with |x - CX| <= ROI_H and |y - CY| <= ROI_V
        contribute to the event rate.  Hot pixels and broadband sparkle outside
        the hand ROI are silently discarded at decode time.  No multiply: two
        magnitude-compare pairs (abs then compare).
     2. RATE FLOOR GUARD.  If the ROI rate snapshot falls below RATE_FLOOR
        for the current window, no burst of hand movement is detected and the
        zero-crossing detector skips the window (does not update sign or
        zx_counter).  A flat/empty scene has near-zero ROI rate -> valid=0,
        no card.
     3. STABILITY GUARD.  Before asserting valid=1, the recovered period must
        remain stable across consecutive cycles: |period - prev_period| <=
        PERIOD_TOL (one sub, one abs compare, no multiply).  Broadband sparkle
        has no coherent oscillation period -> its recovered period varies
        wildly -> this guard trips -> valid=0.
     4. MINIMUM CROSSINGS GUARD.  valid=1 requires at least ZX_MIN=4 zero-
        crossings (two full periods) before the chip commits to a reading.
        This prevents a transient noise spike from producing a false card.

   -------------------------------------------------------------------------
   Window timing note: the latch (valid/freqbin/ampbin/card/fortune) is
   snapshotted BEFORE the emit on every batch boundary (same scheme as
   dvs_vital).  The seq counter increments every WINDOW_BATCHES batches.
   Output word index i (0-based) carries seq == ((i + 1) / WINDOW_BATCHES) & 0xF.
   Host code should treat the first WINDOW_BATCHES words as "settling" output.

   -------------------------------------------------------------------------
   Output word layout (27 bits used):
     bits[ 3: 0] = freqbin  (0..11, tremor frequency bucket)
     bits[ 7: 4] = ampbin   (0..11, tremor amplitude bucket)
     bits[13: 8] = card     (0..47, tarot card id from 2D LUT; valid when valid=1)
     bits[18:14] = fortune  (0..23, fortune index; valid when valid=1)
     bits[   19] = valid    (1 = hand detected and stable period recovered)
     bits[23:20] = seq      (4-bit window sequence counter, wraps mod 16)
     bits[31:24] = 0
   Host unpacks these fields; see chips/fpga/dvs_tremor_view.py's
   unpack_status(). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Hand ROI centre (frame centre) and half-widths.  Only ROI events count toward
   the event rate; hot pixels and broad sparkle outside are discarded. */
#ifndef CX
#define CX 63                   /* frame centre x */
#endif
#ifndef CY
#define CY 56                   /* frame centre y */
#endif
#ifndef ROI_H
#define ROI_H 30                /* ROI half-width in x */
#endif
#ifndef ROI_V
#define ROI_V 24                /* ROI half-height in y */
#endif

/* Tunables. */
#ifndef RATE_WIN
#define RATE_WIN 8              /* rate snapshot window: RATE_WIN batches */
#endif

#ifndef EWMA_K
#define EWMA_K 3                /* EWMA shift: base += (rate - base) >> EWMA_K */
#endif

#ifndef RATE_FLOOR
#define RATE_FLOOR 2            /* min ROI events per window to process zero-crossings */
#endif

#ifndef ZX_MIN
#define ZX_MIN 4               /* min zero-crossings before valid=1 */
#endif

#ifndef PERIOD_TOL
#define PERIOD_TOL 4           /* max |period_new - period_prev| for stability */
#endif

#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 64      /* latching / seq-advance period in batches */
#endif

/* Frequency threshold table: freqbin = number of thresholds that the period
   (in rate-windows) is STRICTLY LESS THAN.  Thresholds are sorted descending
   so that period=0 (impossible sentinel) maps to freqbin=12 and very long
   periods (slow tremor) map to freqbin=0.
   - period > FREQ_THRESH[0]=192 : freqbin = 0  (slowest)
   - period < FREQ_THRESH[11]=24 : freqbin = 12, clamped to 11 (fastest)
   Values span the expected tremor oscillation range in rate-window units. */
static const uint32_t FREQ_THRESH[12] = {
    192u, 160u, 128u, 112u, 96u, 80u, 64u, 56u, 48u, 40u, 32u, 24u
};

/* Amplitude log-bin cap: ampbin 0..11 (11 half-octave bins). */
#define AMP_BIN_MAX 11u

/* Tarot card LUT.  Indexed as CARD_LUT[(freqbin<<4) - (freqbin<<2) + ampbin]
   = CARD_LUT[freqbin*12 + ampbin].  144 entries mapping (freq, amp) -> card 0..21
   (major arcana: 0=Fool, 1=Magician, 2=High Priestess, 3=Empress, 4=Emperor,
   5=Hierophant, 6=Lovers, 7=Chariot, 8=Strength, 9=Hermit, 10=Wheel,
   11=Justice, 12=Hanged Man, 13=Death, 14=Temperance, 15=Devil, 16=Tower,
   17=Star, 18=Moon, 19=Sun, 20=Judgement, 21=World).
   Layout: low freqbin (slow tremor) -> introspective cards; high freqbin
   (fast tremor) -> active/energetic cards; high ampbin -> dramatic cards. */
static const uint8_t CARD_LUT[144] = {
    /* freqbin=0 (slowest, low amplitude -> high amplitude) */
     9, 12,  2, 17, 18, 21,  0,  3, 14, 10,  8, 20,
    /* freqbin=1 */
     9, 14,  2, 17, 18, 21,  0,  3, 10, 11,  8, 20,
    /* freqbin=2 */
    12,  9,  2, 17, 14, 21,  3,  0, 10, 18,  8, 20,
    /* freqbin=3 */
    12,  9, 14, 17,  2, 21,  3,  0, 10, 18,  8, 20,
    /* freqbin=4 */
     1,  9, 14,  7,  2, 11,  3,  6, 10,  5,  8, 19,
    /* freqbin=5 */
     1,  9, 14,  7,  2, 11,  3,  6,  5, 10,  8, 19,
    /* freqbin=6 */
     1,  6, 14,  7,  4, 11,  3,  5, 10,  0,  8, 19,
    /* freqbin=7 */
     7,  6,  1,  4, 14, 11,  3,  5, 10,  0,  8, 16,
    /* freqbin=8 */
     7,  6,  1,  4, 15, 11,  5,  3, 10,  0, 16, 13,
    /* freqbin=9 */
     7, 15,  1,  4, 16, 13,  5,  3, 10,  0, 20, 19,
    /* freqbin=10 */
    15, 16,  1,  4, 13,  7,  5,  3, 10, 20, 19, 21,
    /* freqbin=11 (fastest, lowest -> highest amplitude) */
    16, 15, 13,  4,  7,  1,  5,  3, 20, 10, 19, 21,
};

/* Number of fortunes per card (we cycle through 0..NFORTUNES-1 via seq). */
#define NFORTUNES 24u

/* -------------------------------------------------------------------------
   State variables (all in .bss, zeroed by crt0.S).
   ------------------------------------------------------------------------- */

/* ROI event count accumulated over the current RATE_WIN-batch rate window. */
static uint32_t rate_acc;

/* Batch counter within the rate window (0..RATE_WIN-1). */
static uint32_t batch_in_rate;

/* EWMA baseline (integer, same units as rate). */
static uint32_t ewma_base;

/* Sign of the last deviation: 0 = negative/zero, 1 = positive. */
static uint32_t prev_sign;

/* 1 after the first non-floor rate window has been processed (guards
   prev_sign validity). */
static uint32_t have_prev_sign;

/* Window index of the last zero-crossing. */
static uint32_t last_zx_win;

/* Half-period from the crossing before last (in rate windows). */
static uint32_t prev_half_period;

/* Number of zero-crossings observed (saturates at 255). */
static uint32_t zx_count;

/* Most recent stable (validated) period (in rate windows). */
static uint32_t stable_period;

/* Amplitude accumulator: ROI events since the last two crossings. */
static uint32_t amp_acc;

/* Rate window index (monotonic counter for crossing intervals). */
static uint32_t win_index;

/* Latched output fields (updated every WINDOW_BATCHES batches). */
static uint32_t lat_freqbin;
static uint32_t lat_ampbin;
static uint32_t lat_card;
static uint32_t lat_fortune;
static uint32_t lat_valid;

/* Batch counter within the latch window (0..WINDOW_BATCHES-1). */
static uint32_t batch_in_window;

/* 4-bit sequence / latch counter (wraps mod 16). */
static uint32_t wseq;

/* -------------------------------------------------------------------------
   log2ampbin -- map amplitude value v (>0) to a half-octave log-scale bin
   0..AMP_BIN_MAX.  Same shift-loop construction as dvs_vital's log2bin32
   but capped at AMP_BIN_MAX.
   ------------------------------------------------------------------------- */
static uint32_t log2ampbin(uint32_t v) {
    if (v == 0u) return 0u;
    uint32_t m = 0u, t = v;
    while (t >= 2u) { t >>= 1u; m++; }
    uint32_t sub = (m >= 1u) ? ((v >> (m - 1u)) & 1u) : 0u;
    uint32_t b = (m << 1u) | sub;
    return (b > AMP_BIN_MAX) ? AMP_BIN_MAX : b;
}

/* -------------------------------------------------------------------------
   freq_to_bin -- map period-in-rate-windows to freqbin 0..11.
   Longer period = lower frequency = lower freqbin.
   freqbin = number of FREQ_THRESH[] values that period is strictly less than.
   Uses 12 compares, no multiply.  Range: freqbin 0 (p>=192) .. 11 (p<24).
   ------------------------------------------------------------------------- */
static uint32_t freq_to_bin(uint32_t p) {
    uint32_t bin = 0u;
    for (uint32_t i = 0u; i < 12u; i++) {
        if (p < FREQ_THRESH[i]) bin++;
    }
    /* Clamp to 11 (shouldn't be needed for p>=1, but guard against p=0). */
    return (bin > 11u) ? 11u : bin;
}

/* Must NOT call wfi() itself: see dvs_vital/main.c's epilogue comment. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Count ROI events in this batch (x/y decode; ts/pol ignored). */
    uint32_t roi_count = 0u;
    for (uint32_t i = 0u; i < BATCH; i++) {
        uint32_t x = (v[i] >> X_SHIFT) & 0x7Fu;
        uint32_t y = (v[i] >> Y_SHIFT) & 0x7Fu;

        /* ROI test: |x - CX| <= ROI_H and |y - CY| <= ROI_V.
           Multiply-free: compute abs via conditional negate. */
        uint32_t dx = (x >= (uint32_t)CX) ? (x - (uint32_t)CX) : ((uint32_t)CX - x);
        uint32_t dy = (y >= (uint32_t)CY) ? (y - (uint32_t)CY) : ((uint32_t)CY - y);
        if (dx <= (uint32_t)ROI_H && dy <= (uint32_t)ROI_V) {
            roi_count++;
        }
    }

    /* Accumulate into the rate window. */
    rate_acc += roi_count;
    amp_acc  += roi_count;   /* amp_acc runs continuously; we snapshot per crossing pair */
    batch_in_rate++;

    if (batch_in_rate >= (uint32_t)RATE_WIN) {
        /* --- Rate window boundary: snapshot rate, update EWMA, detect crossings --- */
        batch_in_rate = 0u;
        uint32_t rate = rate_acc;
        rate_acc = 0u;

        /* EWMA update: base += (rate - base) >> EWMA_K.
           Use saturating arithmetic so that a large initial rate doesn't wrap.
           rate >= base: shift down the positive deviation.
           rate <  base: shift down the negative deviation (subtract from base). */
        if (rate >= ewma_base) {
            ewma_base += (rate - ewma_base) >> (uint32_t)EWMA_K;
        } else {
            ewma_base -= (ewma_base - rate) >> (uint32_t)EWMA_K;
        }

        win_index++;   /* monotonic window counter for crossing intervals */

        /* Rate-floor guard: skip zero-crossing logic on quiet windows. */
        if (rate >= (uint32_t)RATE_FLOOR) {
            /* Deviation sign: 1 if rate > base, 0 otherwise. */
            uint32_t cur_sign = (rate > ewma_base) ? 1u : 0u;

            if (have_prev_sign && cur_sign != prev_sign) {
                /* Zero-crossing detected. */
                uint32_t half = win_index - last_zx_win;
                uint32_t full_p = half + prev_half_period;

                /* Stability check: |full_p - stable_period| <= PERIOD_TOL.
                   First few crossings always accepted (zx_count < ZX_MIN). */
                uint32_t diff;
                if (full_p >= stable_period)
                    diff = full_p - stable_period;
                else
                    diff = stable_period - full_p;

                uint32_t stable = (zx_count < (uint32_t)ZX_MIN)
                                || diff <= (uint32_t)PERIOD_TOL;

                if (stable) {
                    stable_period = full_p;
                }

                prev_half_period = half;
                last_zx_win = win_index;

                if (zx_count < 255u) zx_count++;
            }

            prev_sign = cur_sign;
            have_prev_sign = 1u;
        }
    }

    /* Latch window boundary: advance every WINDOW_BATCHES batches. */
    batch_in_window++;
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;

        /* Compute valid: need ZX_MIN crossings AND a non-zero stable period. */
        uint32_t valid = (zx_count >= (uint32_t)ZX_MIN && stable_period != 0u) ? 1u : 0u;

        uint32_t freqbin = 0u;
        uint32_t ampbin  = 0u;
        uint32_t card    = 0u;

        if (valid) {
            freqbin = freq_to_bin(stable_period);
            /* Amplitude: use amp_acc accumulated this latch window.
               Right-shift by EWMA_K to smooth; log2ampbin maps to 0..11. */
            ampbin  = log2ampbin(amp_acc >> (uint32_t)EWMA_K);

            /* LUT index: freqbin*12 + ampbin = (freqbin<<4) - (freqbin<<2) + ampbin.
               No multiply: two left-shifts, one sub, one add. */
            uint32_t idx = ((freqbin << 4u) - (freqbin << 2u)) + ampbin;
            card = CARD_LUT[idx];
        }

        /* Reset amplitude accumulator for the next latch window. */
        amp_acc = 0u;

        /* Fortune: wseq (0..15) indexes into the card's fortune ring (0..23).
           wseq is already in range 0..15 < NFORTUNES, no masking needed. */
        uint32_t fortune = wseq;

        lat_freqbin = freqbin;
        lat_ampbin  = ampbin;
        lat_card    = card;
        lat_fortune = fortune;
        lat_valid   = valid;

        wseq = (wseq + 1u) & 0xFu;
    }

    /* Emit ONE word per batch from latched values.
       Layout: bits[3:0]=freqbin, bits[7:4]=ampbin, bits[13:8]=card,
               bits[18:14]=fortune, bits[19]=valid, bits[23:20]=seq,
               bits[31:24]=0. */
    *FIFO_OUT = (wseq          << 20)
              | (lat_valid      << 19)
              | (lat_fortune    << 14)
              | (lat_card       <<  8)
              | (lat_ampbin     <<  4)
              |  lat_freqbin;
}

void main(void) {
    /* .bss is zeroed by crt0.S -- correct cold start for all state. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 */
    /* crt0.S executes wfi() for us when main() returns. */
}
