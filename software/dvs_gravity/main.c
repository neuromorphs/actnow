#include <stdint.h>

/* "THE GRAVITY NOTARY" (dvs_gravity) -- a chips/fpga demo app in the same
   shape as software/dvs_vital/main.c (fifo_in fires event_id_0 once BATCH
   words land; isr_handler reads them, updates the vertical-centroid tracker
   and D2 accumulator, latches a verdict every arc, and writes ONE status word
   per batch; it NEVER calls wfi(), see the epilogue comment on isr_handler).

   Idea: the chip watches a projectile arc and notarises which planet it is on
   by measuring the second difference of the arc's vertical centroid.  Under
   pure free-fall the centroid obeys cy(t) = cy0 + v0*t + (1/2)*g*t^2; the
   discrete second difference at fixed time steps is constant: D2 = g * dt^2.
   This algorithm works in event-count steps (SAMPLE_INTERVAL events per step)
   so dt is constant and no timestamp arithmetic is needed.

   Algorithm (ALL operations are shift/add/sub/compare/LUT -- no multiply/divide):
     1. CENTROID TRACKER.  cy is a running estimate of the object's vertical
        position in [0, SY-1].  For each event with coordinate y_ev:
          if y_ev > cy: cy++   (step up)
          if y_ev < cy: cy--   (step down)
        This is a median-toward tracker (online weighted median with step 1).
        It costs one compare and one add/sub per event -- no multiply.
     2. SAMPLING.  After every SAMPLE_INTERVAL events, capture cy as y[k].
        The slot index k advances from 0..ARC_LEN-1; this fills the arc buffer.
        The sample counter resets at each new arc.
     3. D2 ACCUMULATION.  For k >= 2, compute:
          d2 = y[k] - (y[k-1] << 1) + y[k-2]   (using shift for 2*)
        Collect ARC_D2 = ARC_LEN - 2 values: d2_buf[0..ARC_D2-1].
     4. ARC MEDIAN.  Once all ARC_D2 samples are collected, find their median
        via insertion sort on a copy (compare-only, no multiply).  Median of an
        ARC_D2-element array: element at index ARC_D2/2 after sorting (integer
        truncation using ARC_D2 = even number; either center works).
     5. PLANET LUT.  The median D2 value is compared against four gravity buckets
        (units: centroid pixels per sample^2, scaled to 10x to avoid fractions):
          Moon    (g~1.6 m/s^2) -> D2 in [-1, 1]    -> planet=0
          Mars    (g~3.7 m/s^2) -> D2 in [2, 4]     -> planet=1
          Earth   (g~9.8 m/s^2) -> D2 in [5, 10]    -> planet=2
          Jupiter (g~24.8 m/s^2)-> D2 in [11, 24]   -> planet=3
        Anything outside these ranges: planet=3 (clamped to Jupiter or Moon).
        Sign: free-fall accelerates downward (y increasing in image coords).
        The absolute value of median_d2 is compared against the LUT.
     6. FRAUD DETECTION.  Count how many D2 samples deviate from the median by
        more than FRAUD_TOL.  If fraud_count > FRAUD_THRESH, fraud=1.
        FRAUD_TOL and FRAUD_THRESH are shift-based (compare + count, no mul).
     7. NOISE GUARD.  Two guards (no multiply):
          a. ACTIVITY GUARD: require total event count in the arc to reach
             ACTIVITY_MIN (= SAMPLE_INTERVAL * ARC_LEN) before issuing "valid".
             Trivially satisfied when the arc fills; if the scene is empty and
             the centroid never moves, valid stays 0.
          b. DRIFT GUARD: require |max(cy) - min(cy)| over the arc >= DRIFT_MIN
             pixels (compare-only), ensuring the centroid actually moved.
             Purely static noise clusters that move the centroid < DRIFT_MIN
             get verdict valid=0.

   -------------------------------------------------------------------------
   Event word ABI (matches evt_pack.v / dvs_vital / dvs_flinch):
     x   = (word >> 24) & 0x7F    (0..125) -- X_SHIFT=24
     y   = (word >> 17) & 0x7F    (0..111) -- Y_SHIFT=17 -- USED
     ts  = (word >> 1)  & 0xFFFF  -- NOT used (event-order driven)
     pol =  word        & 1       -- decoded but unused

   -------------------------------------------------------------------------
   Output word layout (27 bits used):
     bits[ 2: 0] = seq     (3-bit arc sequence counter, wraps mod 8)
     bits[ 3: 3] = valid   (1 if the arc produced a confident verdict)
     bits[ 4: 4] = fraud   (1 if arc is non-ballistic)
     bits[ 6: 5] = planet  (0=Moon, 1=Mars, 2=Earth, 3=Jupiter)
     bits[13: 7] = g_est   (median D2 quantized, 7-bit, signed in 2's complement)
     bits[31:14] = 0
   Host unpacks these fields; see chips/fpga/dvs_gravity_view.py's
   unpack_status().

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div).
   Every operation is a shift, add, sub, compare, or LUT:
     - Centroid tracker:  one compare and one add/sub per event; no multiply.
     - D2 computation:    shift-left by 1 for the 2* factor; no multiply.
     - Insertion sort:    compare-only inner loop; no multiply anywhere.
     - Median index:      ARC_D2/2 = ARC_D2>>1 (compile-time constant and power-of-2 step); no multiply.
     - Planet LUT:        four compare branches on |median_d2|; no multiply.
     - Drift guard:       compare (max - min) >= DRIFT_MIN; no multiply.
     - Output pack:       shifts and ORs only; no multiply.
     No multiply anywhere.

   -------------------------------------------------------------------------
   Noise guard detail (SciDVS 126x112 and VERY noisy):
     ACTIVITY GUARD: The arc requires SAMPLE_INTERVAL * ARC_LEN events to fill
       (= 1024 events for defaults).  Occasional noise bursts that last fewer
       events than SAMPLE_INTERVAL cannot even produce a single sample point;
       the arc counter never advances and valid stays 0.
     DRIFT GUARD: Noise clusters that do not move vertically (static hot-pixel
       columns, uniform sparkle) produce a centroid that barely moves.  If
       |cy_max - cy_min| < DRIFT_MIN over the arc, valid stays 0.
     FRAUD GUARD: Non-ballistic trajectories (random walks, oscillations,
       multiple objects) produce D2 values that are erratic rather than
       constant.  If more than FRAUD_THRESH of the D2 samples deviate from the
       median by more than FRAUD_TOL, fraud=1.

   -------------------------------------------------------------------------
   Output timing: the latch happens at the END of each full arc; for all
   batches within an arc the previous latched values are emitted.  seq
   increments AFTER the latch, so the first ARC_BATCHES-1 batches carry seq=0
   with zero fields; the last batch of arc 0 emits seq=1 with arc-0 verdicts.
   Host code should treat seq=0 as "not yet valid".

   -------------------------------------------------------------------------
   The ISR must NOT call wfi() for the same stack-leak reason as dvs_vital
   (see that file's epilogue comment for the full explanation).  Just returning
   lands on the wfi() in main()'s epilogue via crt0.S's tail. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI. */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Tunables (all compile-time overridable). */
#ifndef SAMPLE_INTERVAL
#define SAMPLE_INTERVAL 64      /* events between centroid samples */
#endif

#ifndef ARC_LEN
/* 6 samples per arc (ARC_D2=4 D2 values).  With SAMPLE_INTERVAL=64 and
   D2 up to 6 px/step^2, the trajectory stays within [0, SY-1]: at k=5,
   y[5] = y0 + 3*25 = y0+75 <= 80 (with y0=5), well within SY=112.
   Centroid velocity at k=5 is 6*5=30 < SAMPLE_INTERVAL=64, so the tracker
   converges to each true y within a single interval. */
#define ARC_LEN 6
#endif

/* D2 samples per arc = ARC_LEN - 2 = 4.  Even, so median index = 4>>1 = 2
   (upper of the two middle elements). */
#define ARC_D2  (ARC_LEN - 2)  /* = 4 */

#ifndef DRIFT_MIN
#define DRIFT_MIN 3             /* min |cy_max - cy_min| over arc for valid */
#endif

#ifndef FRAUD_TOL
#define FRAUD_TOL 2             /* |d2 - median_d2| > this -> deviation */
#endif

#ifndef FRAUD_THRESH
#define FRAUD_THRESH 1          /* more than this many deviations -> fraud */
#endif

/* Planet D2 bucket boundaries (absolute value of median D2, pixels/step^2).
   Calibrated for SAMPLE_INTERVAL=64 events/step, SciDVS 126x112 sensor:
     Moon    (g~1.6  m/s^2) -> |D2| in [0, 1]  -> planet=0
     Mars    (g~3.7  m/s^2) -> |D2| in [2, 3]  -> planet=1
     Earth   (g~9.8  m/s^2) -> |D2| in [4, 5]  -> planet=2
     Jupiter (g~24.8 m/s^2) -> |D2| >= 6        -> planet=3
   These are compare-only constants -- no multiply at runtime. */
#ifndef MOON_MAX
#define MOON_MAX   1
#endif
#ifndef MARS_MAX
#define MARS_MAX   3
#endif
#ifndef EARTH_MAX
#define EARTH_MAX  5
#endif
#ifndef JUPITER_MIN
#define JUPITER_MIN 6
#endif

/* seq wraps mod 8 (3 bits). */
#define SEQ_MASK  0x7u
/* g_est 7-bit signed field mask. */
#define GEST_MASK 0x7Fu

/* Total events per arc = SAMPLE_INTERVAL * ARC_LEN.
   Also used as the arc batch count (rounded up to batch boundary). */
#define ARC_EVENTS (SAMPLE_INTERVAL * ARC_LEN)

/* -------------------------------------------------------------------------
   State.  All zeroed by crt0.S at cold start.
   -------------------------------------------------------------------------*/

/* Centroid (0..SY-1). Zeroed by crt0.S -> starts at top of frame. */
static uint32_t cy;

/* y[] samples for the current arc (ARC_LEN entries). */
static int32_t  arc_y[ARC_LEN];

/* D2 buffer for the current arc (ARC_D2 entries). */
static int32_t  d2_buf[ARC_D2];

/* Number of events processed in the current SAMPLE_INTERVAL period. */
static uint32_t ev_in_interval;

/* Number of y[] samples filled in the current arc. */
static uint32_t arc_slot;

/* Min and max cy seen during arc (for drift guard). */
static uint32_t cy_min;
static uint32_t cy_max;

/* Number of D2 samples computed in the current arc. */
static uint32_t d2_slot;

/* Latched output from last completed arc. */
static uint32_t lat_planet;
static uint32_t lat_fraud;
static uint32_t lat_valid;
static int32_t  lat_g_est;   /* signed median D2 (clamped to 7-bit signed) */

/* Arc sequence counter (3-bit, 0..7 wraps). Zeroed by crt0.S. */
static uint32_t seq;

/* -------------------------------------------------------------------------
   isort_median: insertion sort a copy of d2_buf[0..ARC_D2-1], return the
   element at index ARC_D2>>1.  ARC_D2=4: sorted indices 0,1,2,3; median
   index = 2 (upper-middle of even array, matching the Python mirror).
   Only compare operations -- no multiply, no divide.
   -------------------------------------------------------------------------*/
static int32_t isort_median(void) {
    /* Work on a local copy to leave d2_buf intact for fraud counting. */
    int32_t s[ARC_D2];
    for (uint32_t i = 0u; i < (uint32_t)ARC_D2; i++) s[i] = d2_buf[i];

    /* Insertion sort (ascending). */
    for (uint32_t i = 1u; i < (uint32_t)ARC_D2; i++) {
        int32_t key = s[i];
        uint32_t j = i;
        while (j > 0u && s[j - 1u] > key) {
            s[j] = s[j - 1u];
            j--;
        }
        s[j] = key;
    }
    /* Median: element at ARC_D2 >> 1 (= 8 for default ARC_D2=16). */
    return s[ARC_D2 >> 1];
}

/* -------------------------------------------------------------------------
   isr_handler -- processes BATCH events, updates centroid + arc tracker.
   -------------------------------------------------------------------------*/
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Process each event: decode y, update centroid. */
    for (uint32_t i = 0u; i < BATCH; i++) {
        uint32_t y_ev = (v[i] >> Y_SHIFT) & 0x7Fu;

        /* Step-toward-median centroid tracker.  Uses compare + add/sub only. */
        if (y_ev > cy) {
            if (cy < (uint32_t)(SY - 1)) cy++;
        } else if (y_ev < cy) {
            if (cy > 0u) cy--;
        }

        /* Drift guard bookkeeping: track cy_min, cy_max. */
        if (cy < cy_min) cy_min = cy;
        if (cy > cy_max) cy_max = cy;

        /* Advance event-in-interval counter. */
        ev_in_interval++;
        if (ev_in_interval >= (uint32_t)SAMPLE_INTERVAL) {
            ev_in_interval = 0u;

            /* Capture this sample. */
            if (arc_slot < (uint32_t)ARC_LEN) {
                arc_y[arc_slot] = (int32_t)cy;
                arc_slot++;

                /* Compute D2 once we have at least 3 samples. */
                if (arc_slot >= 3u && d2_slot < (uint32_t)ARC_D2) {
                    uint32_t k = arc_slot - 1u;
                    int32_t d2 = arc_y[k]
                               - (arc_y[k - 1u] << 1)
                               + arc_y[k - 2u];
                    d2_buf[d2_slot] = d2;
                    d2_slot++;
                }

                /* Full arc reached: compute verdict. */
                if (arc_slot >= (uint32_t)ARC_LEN) {
                    /* Drift guard: valid only if centroid moved enough. */
                    uint32_t drift = (cy_max >= cy_min)
                                   ? (cy_max - cy_min)
                                   : (cy_min - cy_max);
                    uint32_t valid = (drift >= (uint32_t)DRIFT_MIN) ? 1u : 0u;

                    /* Median D2 via insertion sort. */
                    int32_t med = isort_median();

                    /* Absolute value of median (no multiply; branches only). */
                    int32_t abs_med = (med < 0) ? (-med) : med;

                    /* Planet LUT (compare-only):
                       |D2| 0..MOON_MAX  -> Moon    (0)
                       |D2| ..MARS_MAX   -> Mars    (1)
                       |D2| ..EARTH_MAX  -> Earth   (2)
                       |D2| >= JUPITER_MIN -> Jupiter (3) */
                    uint32_t planet;
                    if (abs_med <= (int32_t)MOON_MAX)         planet = 0u;
                    else if (abs_med <= (int32_t)MARS_MAX)    planet = 1u;
                    else if (abs_med <= (int32_t)EARTH_MAX)   planet = 2u;
                    else                                      planet = 3u;

                    /* Fraud detection: count deviations from median. */
                    uint32_t fraud_count = 0u;
                    for (uint32_t di = 0u; di < (uint32_t)ARC_D2; di++) {
                        int32_t dev = d2_buf[di] - med;
                        int32_t abs_dev = (dev < 0) ? (-dev) : dev;
                        if (abs_dev > (int32_t)FRAUD_TOL) {
                            fraud_count++;
                        }
                    }
                    uint32_t fraud = (fraud_count > (uint32_t)FRAUD_THRESH) ? 1u : 0u;

                    /* If fraud, valid is downgraded. */
                    if (fraud) valid = 0u;

                    /* Clamp g_est to 7-bit signed range [-64, 63]. */
                    int32_t g_est = med;
                    if (g_est > 63)  g_est =  63;
                    if (g_est < -64) g_est = -64;

                    /* Latch. */
                    lat_planet = planet;
                    lat_fraud  = fraud;
                    lat_valid  = valid;
                    lat_g_est  = g_est;

                    /* Advance seq BEFORE emit (word for this batch carries new seq). */
                    seq = (seq + 1u) & SEQ_MASK;

                    /* Reset arc state for next arc. */
                    arc_slot       = 0u;
                    d2_slot        = 0u;
                    ev_in_interval = 0u;
                    cy_min         = cy;
                    cy_max         = cy;
                }
            }
        }
    }

    /* Emit ONE word per batch from latched values.
       Layout: bits[2:0]=seq, bits[3]=valid, bits[4]=fraud,
               bits[6:5]=planet, bits[13:7]=g_est (7-bit signed),
               bits[31:14]=0. */
    uint32_t g_est_bits = (uint32_t)(lat_g_est) & GEST_MASK;
    *FIFO_OUT = (g_est_bits  <<  7)
              | (lat_planet  <<  5)
              | (lat_fraud   <<  4)
              | (lat_valid   <<  3)
              |  seq;
}

void main(void) {
    /* .bss is zeroed by crt0.S.  Initialise cy to mid-frame so the tracker
       starts from a neutral position (SY/2 = 56; no multiply: 56 is a literal
       constant assigned at start). */
    cy     = (uint32_t)(SY >> 1);   /* = 56 */
    cy_min = cy;
    cy_max = cy;

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 */
    /* crt0.S executes wfi() when main() returns. */
}
