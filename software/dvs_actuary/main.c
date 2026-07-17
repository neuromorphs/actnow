#include <stdint.h>

/* "THE ACTUARY OF SPINNING TOPS" (dvs_actuary) -- a chips/fpga demo app in
   the same shape as software/dvs_vital/main.c (fifo_in fires event_id_0 once
   BATCH words land; isr_handler reads them, updates the precession tracker,
   and writes ONE status word per batch; it NEVER calls wfi()).

   Idea (predict when a spinning top will topple): watch a spinning top; as
   its precession wobble grows, extrapolate the moment of death and show a
   countdown measured in precession cycles.  The top precesses (wobbles
   around its tilt axis) with a period that can be measured from the event
   stream.  As the top slows, precession amplitude A (the bounding extent of
   the activity centroid) grows.  Once the amplitude reaches a calibrated
   critical threshold A_CRIT the top will topple.

   --------------------------------------------------------------------------
   CORE TRICK (multiply-free, NO divide):
     1. CENTROID.  Each batch: accumulate sum_x = sum of event X-coords,
        sum_y = sum of event Y-coords, count = number of events.  Centroid
        (cx, cy) is approximated integer-shift: cx = sum_x >> BATCH_LOG2.
        (BATCH=4 so BATCH_LOG2=2; exact integer division for power-of-two
        batch sizes, but we use shift-and-round for robustness.)
     2. BOUNDING EXTENT (wobble amplitude A).  Over a sliding window of
        EXTENT_WINDOW batches: track cmin_x, cmax_x, cmin_y, cmax_y with
        compares only.  A = (cmax_x - cmin_x) + (cmax_y - cmin_y)  -- a
        single Manhattan extent.  Resets every EXTENT_WINDOW batches.
     3. ZERO-CROSSING PERIOD.  A precession cycle = two sign flips of the
        centroid-x deviation (cx - cx_mean) around a running mean.  A sign
        flip is detected when the sign of (cx - cx_prev_smooth) changes.
        Period P = batch-count between consecutive zero-crossings ×2 (half-
        cycle).  Smoothed: cx_smooth is a leaky integrand (cx_smooth =
        cx_smooth - (cx_smooth >> SMOOTH_SH) + (cx >> SMOOTH_SH)) -- shift
        arithmetic only, no divide.
     4. COUNTDOWN by repeated subtraction (NO divide).  After two complete
        extent windows, ΔA = A_now - A_prev.  If ΔA > 0 and A < A_CRIT:
          cycles_left = 0;
          tmp = A;
          while (tmp + ΔA <= A_CRIT) { tmp += ΔA; cycles_left++; }
          cycles_left++ (one more to reach/exceed A_CRIT)
        Cap at COUNTDOWN_CAP.  The repeated subtraction loop is O(COUNTDOWN_CAP)
        iterations max -- bounded.  NO multiply, NO divide.

   --------------------------------------------------------------------------
   Noise guard (SciDVS 126×112 is VERY noisy -- random sparkle everywhere):
     1. DENSITY GUARD.  If a batch has fewer than ACT_MIN events (the ISR
        always processes BATCH events from the FIFO, but if many are hot-
        pixel duplicates the centroid can still be computed; however the
        PERIOD-validity gate below handles this).
     2. CENTROID COHERENCE GUARD.  A stationary or noisy scene produces a
        centroid that sits near the sensor centre and does not oscillate.
        We require at least CROSS_MIN zero-crossings in a PERIOD_WINDOW to
        declare the period valid (valid_period=1).  Isolated sparkle that
        doesn't produce a periodic centroid oscillation fails this gate.
     3. GROWTH GUARD.  Before issuing a countdown we require ΔA > 0 for at
        least GROW_MIN consecutive extent windows (sustained positive growth).
        A stable scene (ΔA=0) stays in state DORMANT -- no countdown.
     4. STATIC GUARD.  If A < A_FLOOR (amplitude so small the top hasn't
        started to wobble visibly) the state is DORMANT.

   --------------------------------------------------------------------------
   Exact identities the offline validation checks (see dvs_actuary_view.py):
     (a) Growing oscillation: centroid-x oscillates as sin with linearly
         growing amplitude.  ΔA per extent window is known.  After GROW_MIN
         windows of growth the countdown must equal (A_CRIT - A) / ΔA
         computed by repeated subtraction (same integer path).
     (b) Constant-amplitude oscillation (ΔA=0): state never leaves DORMANT,
         valid flag=0, no countdown.
     (c) Static scene (zero centroid motion): zero-crossings never reach
         CROSS_MIN, valid_period=0, no countdown.
     (d) Well-formedness: all packed fields within bounds.

   --------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - centroid   : sum of BATCH=4 X values >> 2  (shift); no multiply.
     - cx_smooth  : cx_smooth -= cx_smooth >> SMOOTH_SH;
                    cx_smooth += cx >> SMOOTH_SH;  two shifts + sub + add.
     - extent     : compare + conditional assign (4 compares per batch); sub.
     - zero-cross : sign bit comparison (x >> 31 for 32-bit signed); sub.
     - period     : sub of batch counters; no multiply.
     - repeated-subtraction loop: add ΔA, compare with A_CRIT; no multiply.
     - output pack: shifts and ORs only.
     No multiply anywhere.

   --------------------------------------------------------------------------
   The event word (evt_pack.v -- same as dvs_vital / dvs_entropy / dvs_flinch):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24   -- USED (centroid)
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17   -- USED (centroid)
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)         -- decoded but unused
     pol =  word        & 1                                    -- decoded but unused

   --------------------------------------------------------------------------
   Output word layout (27 bits used):
     bits[ 5: 0] = countdown   (0..63, cycles until predicted topple, capped; 0=no pred)
     bits[13: 6] = amplitude   (0..255, current wobble extent A, saturated at 255)
     bits[20:14] = period      (0..127, current precession period in batches, capped)
     bits[21]    = valid       (1 = growing oscillation detected, countdown meaningful)
     bits[25:22] = seq         (4-bit batch sequence counter, wraps mod 16)
     bits[31:26] = 0
   Host unpacks these fields; see chips/fpga/dvs_actuary_view.py's unpack_status().

   --------------------------------------------------------------------------
   Window/latch note: seq increments every batch (wraps mod 16).  All output
   fields are emitted from latched values updated on the window boundary
   (every EXTENT_WINDOW batches) -- the same "latch before emit" discipline
   as dvs_vital.  Implementations that use EXTENT_WINDOW=32 get a window
   latch every 32×4=128 events.

   --------------------------------------------------------------------------
   TIMEBASE: event-count based (batch-count ticks), not raw timestamp ticks.
   This validates cleanly offline and is deterministic.  The timestamp field
   in each event word is decoded per ABI but not used by this algorithm. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame. */
#define SX 126
#define SY 112

/* Input event ABI. */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Tunables: each under #ifndef so -D overrides work at compile time. */

/* Batches per extent window (amplitude A is re-measured each window). */
#ifndef EXTENT_WINDOW
#define EXTENT_WINDOW 32
#endif

/* Number of extent windows with ΔA > 0 required before issuing countdown. */
#ifndef GROW_MIN
#define GROW_MIN 2
#endif

/* Minimum amplitude to be considered non-static (Manhattan pixel units). */
#ifndef A_FLOOR
#define A_FLOOR 4
#endif

/* Critical amplitude at which the top topples (Manhattan pixel units). */
#ifndef A_CRIT
#define A_CRIT 80
#endif

/* Countdown cap (max cycles reported before predicted topple). */
#ifndef COUNTDOWN_CAP
#define COUNTDOWN_CAP 63
#endif

/* Smoothing shift for centroid-x low-pass filter: cx_smooth updated by
   ±(cx >> SMOOTH_SH) each batch.  SMOOTH_SH=3 -> 1/8 contribution. */
#ifndef SMOOTH_SH
#define SMOOTH_SH 3
#endif

/* Number of batches in the zero-crossing period window. */
#ifndef PERIOD_WINDOW
#define PERIOD_WINDOW 64
#endif

/* Minimum zero-crossings in PERIOD_WINDOW to declare period valid. */
#ifndef CROSS_MIN
#define CROSS_MIN 3
#endif

/* Maximum period in batches that the output field can represent (7 bits). */
#define PERIOD_MAX 127u

/* Sequence counter mask (4 bits). */
#define SEQ_MASK 0xFu

/* Saturation cap for amplitude output field (8 bits). */
#define AMP_SAT 255u

/* -----------------------------------------------------------------------
   State -- all zeroed by crt0.S on cold start.
   ----------------------------------------------------------------------- */

/* Smoothed centroid X (integer leaky integrator, multiply-free). */
static uint32_t cx_smooth;

/* Sign of previous (cx - cx_smooth_prev) for zero-crossing detection.
   0 = positive/zero; 1 = negative.  Stored as 0 or 1. */
static uint32_t prev_sign;
static uint32_t have_prev_sign;

/* Zero-crossing count in the current period window. */
static uint32_t cross_count;

/* Batch counter within the period window (0..PERIOD_WINDOW-1). */
static uint32_t batch_in_period;

/* Last two zero-crossing batch timestamps (for period estimation). */
static uint32_t zc_ts[2];   /* circular: zc_ts[0] = earlier, [1] = later */
static uint32_t zc_count;   /* total crossings seen */
static uint32_t have_period;

/* Current estimated half-period in batches (distance between consecutive
   crossings), converted to full period (×2) approximated as << 1. */
static uint32_t period_batches;

/* Batch counter within the current extent window (0..EXTENT_WINDOW-1). */
static uint32_t batch_in_extent;

/* Running min/max centroid coords across the extent window. */
static uint32_t cmin_x, cmax_x, cmin_y, cmax_y;
static uint32_t extent_init;   /* 1 once the first batch has been seen */

/* Amplitude from the previous extent window (for ΔA). */
static uint32_t amp_prev;
static uint32_t have_amp_prev;

/* Consecutive extent windows with ΔA > 0. */
static uint32_t grow_streak;

/* Delta-A from the last latch (used in repeated-subtraction countdown). */
static uint32_t delta_a;

/* Global batch sequence counter (wraps mod 16). */
static uint32_t seq;

/* Latched output fields (emitted every batch, updated on window boundary). */
static uint32_t lat_countdown;
static uint32_t lat_amplitude;
static uint32_t lat_period;
static uint32_t lat_valid;

/* -----------------------------------------------------------------------
   isr_handler -- called on every BATCH-event trigger from fifo_in.
   Must NOT call wfi(): see dvs_vital/main.c epilogue comment for the full
   explanation.
   ----------------------------------------------------------------------- */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Decode X/Y from each event and accumulate batch sums. */
    uint32_t sx = 0u, sy = 0u;
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t ex = (v[i] >> X_SHIFT) & 0x7Fu;
        uint32_t ey = (v[i] >> Y_SHIFT) & 0x7Fu;
        sx += ex;
        sy += ey;
    }

    /* Batch centroid (BATCH=4 -> shift by 2; exact for power-of-two batch). */
    uint32_t cx = sx >> 2;  /* range 0..125 */
    uint32_t cy = sy >> 2;  /* range 0..111 */

    /* Update smoothed centroid-x (leaky integrator, shift arithmetic only).
       cx_smooth = cx_smooth - (cx_smooth >> SMOOTH_SH) + (cx >> SMOOTH_SH)
       This tracks the DC level of cx so we detect oscillation around it. */
    cx_smooth = cx_smooth - (cx_smooth >> SMOOTH_SH) + (cx >> SMOOTH_SH);

    /* Zero-crossing detection: sign of (cx - cx_smooth) changes -> crossing.
       We use 32-bit signed arithmetic.  (cx - cx_smooth) fits in 32 bits
       since both are bounded by SX=126.  Shift to extract sign bit. */
    int32_t dev = (int32_t)cx - (int32_t)cx_smooth;
    uint32_t sign = (dev < 0) ? 1u : 0u;

    if (have_prev_sign) {
        if (sign != prev_sign) {
            /* Zero crossing detected. */
            cross_count++;
            uint32_t half_period = seq - zc_ts[1];  /* batch distance, unsigned */
            zc_ts[0] = zc_ts[1];
            zc_ts[1] = seq;
            zc_count++;
            if (zc_count >= 2u) {
                /* Period = 2 × half-period (shift left by 1). */
                uint32_t p = half_period << 1;
                if (p > PERIOD_MAX) p = PERIOD_MAX;
                period_batches = p;
                have_period = 1u;
            }
        }
    } else {
        zc_ts[1] = seq;
    }
    prev_sign = sign;
    have_prev_sign = 1u;

    /* Update extent window min/max. */
    if (!extent_init) {
        cmin_x = cx; cmax_x = cx;
        cmin_y = cy; cmax_y = cy;
        extent_init = 1u;
    } else {
        if (cx < cmin_x) cmin_x = cx;
        if (cx > cmax_x) cmax_x = cx;
        if (cy < cmin_y) cmin_y = cy;
        if (cy > cmax_y) cmax_y = cy;
    }

    /* Advance period window; check zero-crossing validity. */
    batch_in_period++;
    if (batch_in_period >= (uint32_t)PERIOD_WINDOW) {
        batch_in_period = 0u;
        /* valid_period: enough zero-crossings in this window */
        if (cross_count < (uint32_t)CROSS_MIN) {
            have_period = 0u;   /* oscillation not detected */
            grow_streak = 0u;   /* reset growth streak too */
        }
        cross_count = 0u;
    }

    /* Advance extent window; compute amplitude and growth. */
    batch_in_extent++;
    if (batch_in_extent >= (uint32_t)EXTENT_WINDOW) {
        batch_in_extent = 0u;

        /* Manhattan extent. */
        uint32_t amp = (cmax_x - cmin_x) + (cmax_y - cmin_y);
        if (amp > AMP_SAT) amp = AMP_SAT;

        /* Reset extent accumulators for next window. */
        extent_init = 0u;

        /* Compute ΔA and update growth streak. */
        uint32_t da = 0u;
        if (have_amp_prev) {
            if (amp > amp_prev) {
                da = amp - amp_prev;
                grow_streak++;
            } else {
                da = 0u;
                grow_streak = 0u;
            }
        }
        amp_prev = amp;
        have_amp_prev = 1u;

        /* Only issue a countdown when:
           - period detection is valid (have_period),
           - growth streak >= GROW_MIN,
           - current amplitude >= A_FLOOR (not static),
           - current amplitude < A_CRIT (top still alive). */
        uint32_t valid = 0u;
        uint32_t countdown = 0u;

        if (have_period
                && grow_streak >= (uint32_t)GROW_MIN
                && amp >= (uint32_t)A_FLOOR
                && amp < (uint32_t)A_CRIT
                && da > 0u) {
            valid = 1u;
            /* Repeated subtraction: count how many ΔA steps until A_CRIT.
               countdown = ceil((A_CRIT - amp) / da)
               implemented as: tmp = amp; countdown = 0;
               while (tmp < A_CRIT) { tmp += da; countdown++; }
               with a safety cap at COUNTDOWN_CAP iterations. */
            uint32_t tmp = amp;
            countdown = 0u;
            while (tmp < (uint32_t)A_CRIT && countdown < (uint32_t)COUNTDOWN_CAP) {
                tmp += da;
                countdown++;
            }
            if (tmp < (uint32_t)A_CRIT) {
                countdown = (uint32_t)COUNTDOWN_CAP;  /* hit cap */
            }
        }

        delta_a = da;

        lat_amplitude = amp;
        lat_period    = period_batches;
        lat_valid     = valid;
        lat_countdown = countdown;
    }

    /* Advance and wrap sequence counter. */
    seq = (seq + 1u) & SEQ_MASK;

    /* Emit ONE word per batch from latched values.
       Layout: bits[5:0]=countdown, bits[13:6]=amplitude, bits[20:14]=period,
               bits[21]=valid, bits[25:22]=seq, bits[31:26]=0 */
    *FIFO_OUT = (seq           << 22)
              | (lat_valid     << 21)
              | (lat_period    << 14)
              | (lat_amplitude <<  6)
              |  lat_countdown;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- correct cold start for all state.
       batch_sum_x/y, cx_smooth, prev_sign, have_prev_sign, cross_count,
       batch_in_period, zc_ts, zc_count, have_period, period_batches,
       batch_in_extent, cmin_x/cmax_x/cmin_y/cmax_y, extent_init,
       amp_prev, have_amp_prev, grow_streak, delta_a, seq,
       lat_countdown, lat_amplitude, lat_period, lat_valid:
       all start 0. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
