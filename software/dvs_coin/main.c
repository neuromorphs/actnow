#include <stdint.h>

/* "HEADS OR TAILS, MID-AIR" (dvs_coin) -- a chips/fpga demo app in the same
   shape as software/dvs_vital/main.c.  The chip watches a tossed coin in
   mid-air and predicts its face (HEADS / TAILS) at the APEX of the toss --
   before the coin lands.

   -------------------------------------------------------------------------
   CONCEPT.  A coin in flight:
     - Rises then falls: the vertical centroid of event activity traces a
       parabola.  APEX is where the centroid transitions from rising to falling.
     - Spins at a near-constant angular rate.  The flat faces of the coin
       alternate in the camera's line of sight twice per full revolution, each
       exposure creating a brief burst of events ("glint" = many events in a
       short time window).  The time between glints is the half-spin period.
     - Time symmetry at apex: remaining airtime ≈ elapsed airtime since the
       FIRST glint.  We measure elapsed time as (apex_ts - first_glint_ts)
       using 16-bit wrapped subtraction.
     - Total predicted half-turns from apex onward: computed by REPEATED
       SUBTRACTION of ts_half from remaining_time (no divide hardware).
     - Starting face at first glint assumed HEADS.  Parity of
       (glints_so_far + remaining_halfturns) gives the landing face.

   -------------------------------------------------------------------------
   COORDINATE SYSTEM.  The sensor is 126 wide × 112 tall.  Higher y means
   lower in the image (row 0 = top).  A rising coin moves toward lower y
   (centroid_y decreasing); a falling coin moves toward higher y
   (centroid_y increasing).  APEX: sign of Δcentroid_y flips from negative
   to positive after RISE_STEPS consecutive rising centroid windows.

   -------------------------------------------------------------------------
   ALGORITHM (ALL shift/add/sub/compare, NO multiply/divide).

   Per event-count WINDOW (WIN_SIZE = 1 << WIN_SHIFT, power-of-two):
     1. CENTROID TRACKING.  Accumulate y-coordinates; at window close, the
        approximate centroid is y_sum >> WIN_SHIFT (exact because WIN_SIZE is
        a power of 2).  Compare to previous centroid to detect rise / fall.

     2. GLINT DETECTION.  A "glint" is a compact burst of events from one
        face of the spinning coin.  Detected as a WIN_SIZE-event window whose
        timestamp span is SHORT: (last_ts - win_start_ts) & TS_MASK < GLINT_DT.
        Short span means many events per unit time = coin face in view.
        Long span means events are spread out (between glints or noise).
        This is purely timestamp arithmetic: compare, subtract, mask -- no mul.
        Consecutive glint onsets give ts_half (half-spin period).

     3. APEX DETECTION.  After MIN_GLINTS confirmed glints (noise guard):
        when centroid sign flips from ≥ RISE_STEPS rising windows to a
        falling window, record apex_ts and set apex_reached=1.

     4. HALF-TURN PREDICTION.  elapsed_time = (apex_ts - first_glint_ts) &
        TS_MASK.  remaining_time ≈ elapsed_time (time-symmetry).  Extra
        half-turns = divfloor_sat(remaining_time, ts_half) via repeated
        subtraction.  Total half-turns = glint_count + extra.  Landing face =
        HEADS if total is even, TAILS if odd.

   -------------------------------------------------------------------------
   NOISE GUARD.
     1. MIN_GLINTS guard: require >= MIN_GLINTS=3 confirmed compact windows
        before accepting an apex.  Dense sparkle fires every window (every
        WIN_SIZE events is short if the camera is overwhelmed) -- but dense
        sparkle also has no apex (no rising centroid), so it cannot produce a
        false prediction.
     2. RISE_STEPS trajectory guard: require >= RISE_STEPS centroid windows
        with strictly negative delta before accepting a sign flip as an apex.
        Monotone or jittery sparkle has no sustained rising trajectory.
     3. TS_HALF bounds guard: ts_half must be >= HS_MIN and <= HS_MAX.
        Out-of-range values from aperiodic noise are rejected.

   -------------------------------------------------------------------------
   MULTIPLY-FREE PROOF.
     - centroid_y   : y_sum >> WIN_SHIFT    (shift; WIN_SIZE = 1<<WIN_SHIFT)
     - sign delta   : subtraction + compare
     - glint span   : (last_ts - win_start_ts) & TS_MASK < GLINT_DT  (compare)
     - ts_half      : (ts - prev_glint_ts) & TS_MASK               (sub+mask)
     - elapsed_time : (apex_ts - first_glint_ts) & TS_MASK          (sub+mask)
     - extra ht     : divfloor_sat(): repeated subtraction loop
     - total ht     : add
     - parity       : & 1
     - output pack  : shifts and ORs only
     No multiply, no divide, no modulo anywhere.

   -------------------------------------------------------------------------
   TIMEBASE.  16-bit wrapping timestamp (same ABI as dvs_vital, dvs_entropy,
   dvs_flinch).  Masked subtraction gives correct deltas for spans < 65536
   ticks.

   -------------------------------------------------------------------------
   Output word layout (27 bits used):
     bits[ 1: 0] = prediction  (0=none, 1=HEADS, 2=TAILS, 3=reserved)
     bits[ 9: 2] = halfturns   (0..255, predicted remaining half-turns at apex,
                                saturated; stays latched after apex)
     bits[17:10] = glint_count (0..255, compact windows seen so far, saturated)
     bits[18]    = apex_reached (0 or 1)
     bits[19]    = valid        (1 when MIN_GLINTS seen + trajectory plausible
                                 + ts_half in [HS_MIN, HS_MAX])
     bits[23:20] = seq          (4-bit batch sequence counter, wraps mod 16)
     bits[31:24] = 0
   Host unpacks these fields; see chips/fpga/dvs_coin_view.py's unpack_status().

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (A) PARABOLIC APEX: synthetic centroid tracing parabola; compact windows
         injected at known half-spin period.  After MIN_GLINTS:
         apex_reached=1, valid=1, prediction in {1,2}, halfturns >= 0.
         Parity of (glint_count + halfturns) matches expected landing face.
     (B) MONOTONE (NO APEX): centroid only falls (y always increasing).
         valid=0, prediction=0 throughout.
     (C) WELL-FORMEDNESS: all field ranges and upper-bit invariant for all
         words from (A) and (B).
     (D) GLINT DETECTION: stream with alternating compact/sparse windows
         produces correct glint_count.
     (E) REPEATED-SUBTRACTION: divfloor_sat exact for tabulated (a,b) pairs.

   -------------------------------------------------------------------------
   ISR NOTE (same as dvs_vital): must NOT call wfi() itself; just return.
   crt0.S executes wfi() when main() returns. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame. */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v). */
#define X_SHIFT 24
#define Y_SHIFT 17
#define TS_MASK 0xFFFFu

/* -----------------------------------------------------------------------
   Tunables (overridable with -D at compile time).
   ----------------------------------------------------------------------- */

/* Centroid / glint window: WIN_SIZE = 1 << WIN_SHIFT events per window.
   Must be a power of 2.  Each window produces one centroid sample AND one
   glint verdict.  Larger = more stable but more latency. */
#ifndef WIN_SHIFT
#define WIN_SHIFT 4
#endif
#define WIN_SIZE (1u << WIN_SHIFT)   /* 16 events per window */

/* Glint time threshold: a window of WIN_SIZE events is a "glint" if the
   timestamp span (last_ts - win_start_ts) & TS_MASK < GLINT_DT.
   Dense coin-face glints have short spans; between glints events are spread
   over longer time.  Units: 16-bit timestamp ticks.
   Default: 12 ticks for a window of 16 events (high density). */
#ifndef GLINT_DT
#define GLINT_DT 24u
#endif

/* Half-spin period bounds (16-bit ticks). */
#ifndef HS_MIN
#define HS_MIN 32u                    /* ~30 Hz max spin (fast coin) */
#endif

#ifndef HS_MAX
#define HS_MAX 8000u                  /* ~2 Hz min spin (very slow coin) */
#endif

/* Minimum confirmed compact-window (glint) peaks before apex is accepted. */
#ifndef MIN_GLINTS
#define MIN_GLINTS 3u
#endif

/* Minimum centroid windows with NEGATIVE delta (rising) before apex. */
#ifndef RISE_STEPS
#define RISE_STEPS 2u
#endif

/* Saturating caps. */
#define COUNT_CAP   255u
#define SEQ_MASK    0xFu

/* -----------------------------------------------------------------------
   State (all zeroed by crt0.S at cold start).
   ----------------------------------------------------------------------- */

/* Centroid / glint window accumulation. */
static uint32_t y_sum;           /* y-sum in current window */
static uint32_t win_cnt;         /* events in current window (0..WIN_SIZE) */
static uint32_t win_start_ts;    /* timestamp of first event in current window */
static uint32_t have_win_start;  /* 1 once win_start_ts is set for current window */

/* Centroid trajectory state. */
static uint32_t prev_centy;      /* centroid_y of the previous window */
static uint32_t have_prev_centy; /* 1 once prev_centy is valid */
static uint32_t rise_streak;     /* consecutive windows with rising centroid */

/* Glint detection state. */
static uint32_t in_glint;        /* 1 while last window was compact */
static uint32_t prev_glint_ts;   /* win_start_ts of the previous compact window */
static uint32_t first_glint_ts;  /* win_start_ts of the first compact window */
static uint32_t have_prev_glint; /* 1 once prev_glint_ts is valid */
static uint32_t have_first_glint;/* 1 once first_glint_ts is set */
static uint32_t ts_half;         /* half-spin period (ticks) from consecutive glints */
static uint32_t glint_count;     /* compact windows (glints) detected; saturates at COUNT_CAP */
static uint32_t last_ts;         /* most recent event timestamp */

/* Apex and prediction state. */
static uint32_t apex_reached;    /* 1 after apex is detected */
static uint32_t apex_ts;         /* 16-bit ts of the apex-triggering event */
static uint32_t valid;           /* 1 once conditions for a valid prediction are met */

/* Latched output fields. */
static uint32_t lat_prediction;  /* 0=none, 1=HEADS, 2=TAILS */
static uint32_t lat_halfturns;   /* predicted extra half-turns beyond glint_count */
static uint32_t lat_glint_count; /* glint_count at last status update */
static uint32_t lat_apex_reached;
static uint32_t lat_valid;

/* Batch sequence counter. */
static uint32_t seq;

/* -----------------------------------------------------------------------
   divfloor_sat: floor(a / b) via repeated subtraction, saturating at
   COUNT_CAP.  Returns 0 if b==0.  Only called once per apex event.
   ----------------------------------------------------------------------- */
static uint32_t divfloor_sat(uint32_t a, uint32_t b) {
    uint32_t q = 0u;
    while (a >= b && q < COUNT_CAP) {
        a -= b;
        q++;
    }
    return q;
}

/* -----------------------------------------------------------------------
   ISR: fires every BATCH events.  Must NOT call wfi().
   ----------------------------------------------------------------------- */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    for (uint32_t i = 0u; i < BATCH; i++) {
        /* x is decoded per ABI but unused (position-invariant centroid uses y). */
        uint32_t y  = (v[i] >> (uint32_t)Y_SHIFT) & 0x7Fu;
        uint32_t ts = (v[i] >> 1u) & TS_MASK;

        last_ts = ts;

        /* ----------------------------------------------------------------
           Centroid + glint window accumulation.
           ---------------------------------------------------------------- */
        if (!have_win_start) {
            win_start_ts   = ts;
            have_win_start = 1u;
        }
        /* Saturating y-sum (avoids hypothetical 32-bit wrap; y <= 111). */
        if (y_sum < 0xFFFFFFFFu - y) y_sum += y;
        win_cnt++;

        if (win_cnt >= WIN_SIZE) {
            /* Window closed. */
            uint32_t centy = y_sum >> WIN_SHIFT;   /* approximate centroid */
            uint32_t span  = (ts - win_start_ts) & TS_MASK;

            /* ----- GLINT DETECTION -----
               compact window = short timestamp span. */
            uint32_t is_compact = (span < (uint32_t)GLINT_DT) ? 1u : 0u;

            if (is_compact && !in_glint) {
                /* Rising edge of a glint peak. */
                in_glint = 1u;

                if (have_prev_glint) {
                    uint32_t dt = (win_start_ts - prev_glint_ts) & TS_MASK;
                    if (dt >= (uint32_t)HS_MIN && dt <= (uint32_t)HS_MAX) {
                        ts_half = dt;
                    }
                }
                if (!have_first_glint) {
                    first_glint_ts   = win_start_ts;
                    have_first_glint = 1u;
                }
                prev_glint_ts  = win_start_ts;
                have_prev_glint = 1u;

                if (glint_count < COUNT_CAP) glint_count++;

                /* Update valid flag. */
                if (!apex_reached
                        && glint_count >= (uint32_t)MIN_GLINTS
                        && ts_half >= (uint32_t)HS_MIN
                        && ts_half <= (uint32_t)HS_MAX) {
                    valid = 1u;
                }
                lat_glint_count  = (glint_count < COUNT_CAP) ? glint_count : COUNT_CAP;
                lat_valid        = valid;
            } else if (!is_compact) {
                in_glint = 0u;
            }

            /* ----- CENTROID TRACKING ----- */
            if (have_prev_centy && !apex_reached) {
                if (centy > prev_centy) {
                    /* Centroid moved DOWN (coin falling). */
                    if (rise_streak >= (uint32_t)RISE_STEPS) {
                        /* Sign flip: rising -> falling.  APEX. */
                        apex_reached  = 1u;
                        apex_ts       = ts;

                        if (glint_count >= (uint32_t)MIN_GLINTS
                                && ts_half >= (uint32_t)HS_MIN
                                && ts_half <= (uint32_t)HS_MAX
                                && have_first_glint) {
                            valid = 1u;

                            uint32_t elapsed   = (apex_ts - first_glint_ts) & TS_MASK;
                            uint32_t extra     = divfloor_sat(elapsed, ts_half);
                            uint32_t total_ht  = glint_count + extra;
                            if (total_ht > COUNT_CAP) total_ht = COUNT_CAP;

                            lat_halfturns  = extra;
                            lat_prediction = ((total_ht & 1u) == 0u) ? 1u : 2u;
                        } else {
                            lat_prediction = 0u;
                            lat_halfturns  = 0u;
                        }

                        lat_apex_reached = 1u;
                        lat_valid        = valid;
                        lat_glint_count  = (glint_count < COUNT_CAP)
                                           ? glint_count : COUNT_CAP;
                    }
                    rise_streak = 0u;
                } else if (centy < prev_centy) {
                    /* Centroid moved UP (coin rising). */
                    if (rise_streak < COUNT_CAP) rise_streak++;
                }
                /* centy == prev_centy: unchanged. */
            }
            prev_centy      = centy;
            have_prev_centy = 1u;

            /* Reset window state for next window. */
            y_sum          = 0u;
            win_cnt        = 0u;
            have_win_start = 0u;
        }
    }

    /* Emit ONE word per batch with current latched state.
       Layout: bits[1:0]=prediction, bits[9:2]=halfturns,
               bits[17:10]=glint_count, bits[18]=apex_reached,
               bits[19]=valid, bits[23:20]=seq, bits[31:24]=0. */
    seq = (seq + 1u) & SEQ_MASK;
    *FIFO_OUT = (seq              << 20)
              | (lat_valid        << 19)
              | (lat_apex_reached << 18)
              | (lat_glint_count  << 10)
              | (lat_halfturns    <<  2)
              |  lat_prediction;
}

void main(void) {
    /* .bss is zeroed by crt0.S: all state starts at 0, correct cold start. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;
    *INT_CTRL_ENABLE = 0x1;
    /* crt0.S executes wfi() for us when main() returns. */
}
