#include <stdint.h>

/* "THE WIDDERSHINS ENGINE" (dvs_widdershins) -- a chips/fpga demo app in the
   same shape as software/dvs_entropy/main.c (fifo_in fires event_id_0 once
   BATCH words land; isr_handler reads them, steps a median-tracker toward the
   scene's activity locus, and writes ONE sample word per batch; it NEVER calls
   wfi(), see the epilogue comment on isr_handler).

   Idea (communal WINDING-NUMBER counter -- novel among these apps): "widdershins"
   is the old word for counter-clockwise.  A single-pixel median tracker follows
   the activity centroid; every SAMPLE_BATCHES batches its compass OCTANT around
   the frame centre is sampled.  Circular octant differences accumulate into a
   signed winding register `wind` (units: eighth-turns): circling the camera
   clockwise (deosil, octant index increasing) ramps wind positive; circling
   counter-clockwise (widdershins) ramps wind negative.  wind>>3 == whole turns.
   Stillness and noise freeze the engine via two multiply-free guards.

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) Driving the tracker through 8 anchor positions in octant order 0,1,...,7
         for K complete laps gives wind == 8*K-1 exactly.  (The first valid sample
         only sets prev_oct and does not accumulate; each of the remaining
         8*K-1 sample-to-sample transitions adds WLUT[1]=1.)
     (b) The REVERSED anchor order 7,6,...,0 for K laps gives exactly -(8*K-1).
         WLUT is odd: WLUT[(8-d)&7] == -WLUT[d] for all d (d=0 and d=4 both
         map to 0 trivially), so reversing the sampled octant sequence exactly
         negates all increments.
     (c) Events scattered uniformly within +/-4 px of centre keep the tracker
         within rad < RMIN (Chebyshev) on every sample.  Every sample sets
         lat_valid=0 and have_prev=0; wind never changes and is exactly 0.
     (d) Alternating two diametrically opposite anchors (e.g. oct 0, oct 4, oct
         0, oct 4, ...) gives d==4 every step.  WLUT[4]==0 by design (half-turn
         ambiguity dropped), so wind stays exactly 0 regardless of how many
         alternations are applied.
     (e) turns == floor(wind/8) on every output word.  wind is in [-1023,+1023];
         arithmetic right-shift by 3 on a two's-complement int32_t is floor
         division by 8 on all values in this range.
     (f) wseq advances on every sample (valid or not) by +1 mod 16.  Word index i
         (0-based) carries wseq == ((i+1)/SAMPLE_BATCHES) & 0xF.  wseq=0 is
         "not yet valid" (first SAMPLE_BATCHES-1 words are all pre-first-sample).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - median tracker  : compare+inc/dec (step toward x/y, no multiply, no array
                         over the frame -- total state is two int32_t words).
     - abs             : compare + negate (sub from 0); no multiply.
     - Chebyshev max   : compare (one branch to pick adx or ady).
     - octant          : sign compares + one adx/ady compare; 3 branches, no mul.
     - winding         : 8-entry LUT lookup + add + two clamp compares; no mul.
     - turns           : one arithmetic right-shift by 3 (floor division by 8).
     - output pack     : shifts, ands, ors only.
     No multiply anywhere.  idx-free: NO arrays over the frame at all -- total
     state is a handful of scalar words.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_entropy / dvs_loom):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp field; decoded per ABI
                                    but UNUSED by this app -- the sample timebase
                                    is event-count driven, so dvs_widdershins
                                    validates identically on any recording
                                    regardless of whether ts is real microseconds
                                    or a wrapped coarse counter)
     pol =  word        & 1
   (Several earlier apps read x/y/ts from the LOW bits -- the STALE layout; on
   the FPGA that reads the wrong bits.  This app matches evt_pack.v + dvs_flinch.)

   -------------------------------------------------------------------------
   TIMEBASE: this app is event-ORDER driven.  Timestamps are decoded per ABI
   (above) but never used for any arithmetic.  A sample fires every
   SAMPLE_BATCHES batches (= SAMPLE_BATCHES*BATCH events).  Replay-speed and
   timestamp wrapping have no effect; identity (f) holds on any player.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112 and VERY noisy).  Two multiply-free guards:
     1. MEDIAN TRACKER.  The tracker steps +/-1 toward each event's (x,y) in
        arrival order, so it converges to the spatial median of recent activity
        -- isolated hot pixels and uniform background sparkle merely pull it one
        step per event toward the frame centre; they cannot yank it to an extreme
        position.  This is the primary noise guard.
     2. RMIN CHEBYSHEV DEAD-ZONE.  With noise only the tracker hovers near
        centre (cx~CX0, cy~CY0), Chebyshev radius rad < RMIN, every sample is
        invalid (lat_valid=0), the winding chain is broken (have_prev=0), and
        wind freezes.  Noise can NEVER wind the engine.  The dead-zone test is a
        single integer compare; no multiply.

   -------------------------------------------------------------------------
   Sample/latch timing note: the octant sample and wseq advance happen BEFORE
   the emit on the batch that closes a sample interval.  Consequently, output
   word index i (0-based) always carries wseq == ((i+1)/SAMPLE_BATCHES) & 0xF.
   The first SAMPLE_BATCHES-1 words carry wseq=0 with the initial (zeroed)
   latched state; word i=SAMPLE_BATCHES-1 carries the first completed sample
   and wseq=1.  Host code should treat wseq=0 as "not yet valid".

   -------------------------------------------------------------------------
   Output word layout (32 bits):
     bits[ 2: 0] = oct     (0..7, last valid sampled octant; stale-held when
                            invalid -- host ignores when valid=0)
     bit [    3] = valid   (1 = last sample had Chebyshev rad >= RMIN; 0 = too
                            close to centre, winding chain broken)
     bits[15: 4] = wind    (12-bit two's complement, eighth-turns, clamped to
                            +/-1023; host sign-extends bit 15 to recover sign)
     bits[23:16] = turns   (8-bit two's complement = wind>>3 arithmetic shift,
                            i.e. floor(wind/8); host sign-extends bit 23)
     bits[27:24] = wseq    (4-bit sample sequence counter, wraps mod 16;
                            advances on every sample, valid or not)
     bits[31:28] = radq    (lat_rad>>3, coarse Chebyshev radius quantised 0..15;
                            stale-held when invalid; host uses for display scale)
   Host unpacks these fields; see chips/fpga/dvs_widdershins_view.py's
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

/* Input event ABI (evt_pack.v / dvs_flinch). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Frame centre (integer, half-open pixel grid). */
#define CX0 63
#define CY0 56

/* Sample every SAMPLE_BATCHES batches (= SAMPLE_BATCHES*BATCH events). */
#ifndef SAMPLE_BATCHES
#define SAMPLE_BATCHES 8
#endif

/* Chebyshev dead-zone radius: samples with rad < RMIN are invalid. */
#ifndef RMIN
#define RMIN 10
#endif

/* Winding register clamp: wind stays in [-WIND_CAP, +WIND_CAP]. */
#define WIND_CAP 1023

/* wseq wraps mod 16 (4-bit counter). */
#define WSEQ_MASK 0xF

/* Octant-difference winding LUT.  d = (oct - prev_oct) & 7.
   d=1: one step CW (+1 eighth-turn); d=7: one step CCW (-1 eighth-turn).
   d=4: half-turn, ambiguous -> 0 by design (identity (d)).
   WLUT is odd: WLUT[(8-d)&7] == -WLUT[d] for all d (identity (b)).  */
static const int32_t WLUT[8] = {0, 1, 2, 3, 0, -3, -2, -1};

/* Median tracker: follows the activity centroid with +/-1 steps.
   MUST be initialised to CX0/CY0 in main() BEFORE enabling interrupts;
   do NOT rely on .bss zeroing for these two variables. */
static int32_t cx;
static int32_t cy;

/* Winding accumulator (eighth-turns, clamped to +/-WIND_CAP). */
static int32_t wind;

/* Previous valid octant and guard flag. */
static int32_t prev_oct;
static int32_t have_prev;

/* Latched output state (updated once per SAMPLE_BATCHES batches). */
static int32_t lat_oct;
static int32_t lat_valid;
static int32_t lat_rad;

/* Batch-within-sample counter (0..SAMPLE_BATCHES-1). */
static int32_t batch_in_sample;

/* 4-bit sample sequence counter (0..15, wraps). */
static int32_t wseq;

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to
   the instruction after it -- the next interrupt jumps straight to
   event_id_0's vector.  A wfi() call inside this function would permanently
   skip its own epilogue (the stack pointer's restore), leaking 16 bytes of
   stack every interrupt until it collides with this program's own code (see
   software/dvs_motion/main.c's isr_handler comment for the full explanation).
   Just returning is correct: this function's own `ret` lands on the same
   cached wfi() site main()'s return already relies on. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Process each event in order: decode fields and step the median tracker. */
    for (uint32_t i = 0; i < BATCH; i++) {
        int32_t x   = (int32_t)((v[i] >> X_SHIFT) & 0x7Fu);
        int32_t y   = (int32_t)((v[i] >> Y_SHIFT) & 0x7Fu);
        /* pol = v[i] & 1; -- decoded per ABI but unused (winding is position-
           only; see the TIMEBASE note in the header comment). */
        /* ts = (v[i] >> 1) & 0xFFFF; -- decoded per ABI but unused (event-order
           timebase; see the TIMEBASE note in the header comment). */

        /* Median tracker: one +/-1 step per event toward (x,y). */
        if (x > cx) cx++; else if (x < cx) cx--;
        if (y > cy) cy++; else if (y < cy) cy--;
    }

    /* Advance batch counter and sample the tracker's octant every
       SAMPLE_BATCHES batches.  The sample (and wseq advance) happen BEFORE
       the emit below, so word index i (0-based) carries
       wseq == ((i+1)/SAMPLE_BATCHES) & 0xF. */
    batch_in_sample++;
    if (batch_in_sample >= SAMPLE_BATCHES) {
        batch_in_sample = 0;

        int32_t dx  = cx - CX0;
        int32_t dy  = cy - CY0;
        int32_t adx = (dx < 0) ? -dx : dx;
        int32_t ady = (dy < 0) ? -dy : dy;
        int32_t rad = (adx > ady) ? adx : ady;   /* Chebyshev radius */

        if (rad >= RMIN) {
            int32_t oct;
            if (dy >= 0) {
                if (dx > 0) oct = (ady <= adx) ? 0 : 1;
                else        oct = (ady >  adx) ? 2 : 3;
            } else {
                if (dx < 0) oct = (ady <= adx) ? 4 : 5;
                else        oct = (ady >  adx) ? 6 : 7;
            }

            if (have_prev) {
                uint32_t d = ((uint32_t)(oct - prev_oct)) & 7u;
                wind += WLUT[d];
                if (wind >  WIND_CAP) wind =  WIND_CAP;
                if (wind < -WIND_CAP) wind = -WIND_CAP;
            }
            prev_oct  = oct;
            have_prev = 1;
            lat_oct   = oct;
            lat_valid = 1;
            lat_rad   = rad;
        } else {
            lat_valid = 0;
            have_prev = 0;   /* stillness breaks the chain: no winding across a gap */
            /* lat_oct / lat_rad intentionally keep their last valid values */
        }

        wseq = (wseq + 1) & WSEQ_MASK;   /* advances on EVERY sample, valid or not */
    }

    /* Emit ONE word per batch (latched state; sample above happened first).
       turns = wind >> 3 (arithmetic shift = floor(wind/8);
               wind in [-1023,+1023] -> turns in [-128,+127]).
       Output word layout:
         bits[ 2: 0] = oct   (last valid octant; stale-held when invalid)
         bit [    3] = valid
         bits[15: 4] = wind  (12-bit two's complement, clamped +/-1023)
         bits[23:16] = turns (8-bit two's complement = wind>>3)
         bits[27:24] = wseq  (4-bit sample sequence, wraps mod 16)
         bits[31:28] = radq  (lat_rad>>3, coarse radius 0..15; stale-held) */
    int32_t turns = wind >> 3;
    *FIFO_OUT = (((uint32_t)(lat_rad >> 3) & 0xFu) << 28)
              | ((uint32_t)wseq             << 24)
              | (((uint32_t)turns & 0xFFu)  << 16)
              | (((uint32_t)wind  & 0xFFFu) <<  4)
              | ((uint32_t)lat_valid         <<  3)
              |  (uint32_t)lat_oct;
}

void main(void) {
    /* Initialise median tracker to frame centre BEFORE enabling interrupts.
       All other state (wind, prev_oct, have_prev, lat_oct, lat_valid, lat_rad,
       batch_in_sample, wseq) starts cold at 0, matching .bss zeroing by crt0.S. */
    cx = CX0;
    cy = CY0;
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
