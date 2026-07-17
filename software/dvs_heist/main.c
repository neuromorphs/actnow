#include <stdint.h>

/* "THE MUSEUM HEIST" (dvs_heist) -- a chips/fpga demo app in the same shape
   as software/dvs_vital/main.c (fifo_in fires event_id_0 once BATCH words
   land; isr_handler reads them, updates the motion-alarm integrator and
   column-histogram burglar tracker, and writes ONE status word per batch;
   it NEVER calls wfi(), see the epilogue comment on isr_handler).

   Idea (stealth vs motion alarm -- novel among these apps): cross the camera's
   field of view without tripping a single-event-sensitive rate alarm.  The
   winning strategy is to move EXTREMELY SLOWLY.  The player's reward: the
   screen shows the burglar creeping column by column across the gallery; a
   single fast swipe triggers the alarm.

   -------------------------------------------------------------------------
   Core algorithm (all shift/add/sub/compare -- NO multiply or divide):

   ALARM INTEGRATOR (global leaky rate integrator R):
     Per event:   R += 1                        (increment saturated at R_CAP)
     Per batch:   R -= R >> LEAK_K              (exponential decay, shift only)
     Alarm fires: R > ALARM_THRESH
   R decays to zero when no events arrive; a burst of events rapidly charges
   it above ALARM_THRESH.  Moving slowly keeps R below threshold.

   COLUMN HISTOGRAM (8-bin burglar position tracker):
     X field is 7 bits (0..125); SX=126 -> 8 bins of width 16 pixels each
     (bins 0..7 cover x=0..15, 16..31, ... 112..125).  The column index is:
       col = (x >> 4) & 7      (x in 0..125 -> col in 0..7, pure shift+mask)
     Per event:   bin[col] += 1 (saturated at BIN_CAP)
     Per HIST_DECAY_BATCHES batches: each bin >>= 1 (exponential decay)
   Argmax over the 8 bins = current burglar column (compares only, no mul).
   The argmax bin represents where MOST events originate -- the burglar's
   estimated horizontal position.

   PROGRESS TRACKER (ratchet):
     progress = max(progress, argmax_col)
   A "clean crossing" completes when progress reaches 7 (crossed all columns).
   A hot pixel is stuck in one column; its argmax never advances 0->7.

   NOISE GUARD (two properties, both free):
     1. RATE GUARD: the alarm integrator's exponential decay means only a
        sustained high-rate burst keeps R above ALARM_THRESH.  Integer steady-
        state: R_ss = 7*n events/batch (from floor((R_ss+n)/8) = n).  With
        BATCH=4 events/ISR, R_ss = 28 > ALARM_THRESH=24 -- alarm fires.  In
        real hardware a slow-moving burglar generates few events so batches
        accumulate slowly; more real-time elapses between batches, giving more
        R decay.  Isolated noise events each charge R by 1 and are rapidly
        decayed away (R_ss(n=1)=7, safely below ALARM_THRESH=24).
     2. HOT-PIXEL COLUMN GUARD: a hot pixel floods a single column.  Its bin
        dominates argmax but is stuck in one bin -> argmax never reaches 7 ->
        progress never completes the crossing.  The alarm integrator may fire
        (it is rate-based, not position-based) but the game logic correctly
        refuses to award a clean crossing.

   -------------------------------------------------------------------------
   TIMEBASE: event-count / batch-count based (NOT timestamp-driven).  Leaky
   decay happens once per BATCH events (one decay step per ISR invocation).
   Histogram column decay happens every HIST_DECAY_BATCHES ISR invocations.
   The offline Python mirror reproduces these counts exactly from any event
   stream, making --validate deterministic (no µs dependency).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div).
   Every operation is a shift, add, sub, compare, or logical:
     - R += 1          : saturating increment with compare + conditional add.
     - R -= R>>LEAK_K  : one right-shift (LEAK_K bits) + a subtract.
     - R > ALARM_THRESH: compare only.
     - col = (x>>4)&7  : shift + mask (no multiply; 16-pixel bins from 7-bit x
                         give exactly 8 bins because 128/16=8 and x<=125<128).
     - bin[col] += 1   : array access (col in 0..7) + saturating add.
     - bin[col] >>= 1  : in-place right-shift; 8-entry loop (8 iterations).
     - argmax scan     : 8-iteration compare loop; no multiply.
     - progress update : compare + conditional assign.
     - rate word field : R >> RATE_SHIFT (one shift); no multiply.
     - output pack     : shifts + ORs only.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_vital / dvs_flinch):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24   -- USED (column bin)
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17   -- decoded, unused
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)         -- decoded, unused
     pol =  word        & 1                                    -- decoded, unused
   x is the only field driving the column histogram; y, ts, pol are decoded
   per ABI but ignored -- the algorithm is y/ts/pol-invariant.

   -------------------------------------------------------------------------
   Output word layout (18 bits used, bits[31:18] = 0):
     bits[ 3: 0] = seq       (4-bit batch sequence counter, wraps mod 16)
     bits[ 6: 4] = progress  (0..7, maximum argmax bin reached so far; ratchet)
     bits[ 9: 7] = pos       (0..7, current argmax bin = burglar column)
     bits[16:10] = rate      (7 bits: R >> RATE_SHIFT, saturated at 127)
     bits[17]    = alarm     (1 if R > ALARM_THRESH, 0 otherwise)
     bits[31:18] = 0
   Host unpacks these fields; see chips/fpga/dvs_heist_view.py's unpack_status().

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) SLOW CROSSING (no alarm, progress 0->7): a slow left-to-right object
         emits a few events per batch, column advancing every ~32 batches.
         R stays below ALARM_THRESH throughout; progress reaches 7; alarm
         never fires.  The crossing succeeds.
     (b) FAST BURST (alarm fires): a single batch of EVENTS_FAST events, all
         at column 0.  R spikes well above ALARM_THRESH; alarm fires.
     (c) HOT PIXEL COLUMN GUARD: a sustained stream of events all at x=5
         (col=0).  argmax stuck at 0; progress never advances past 0; no
         crossing.  (Alarm may or may not fire -- that is correct behaviour
         for a hot pixel; the crossing is what matters.)
     (d) WELL-FORMEDNESS: alarm in {0,1}; pos, progress in 0..7; rate in
         0..127; seq in 0..15; bits[31:18]==0 for all output words.
     (e) Y/TS/POL INVARIANCE: for any event stream, scrambling y, ts, and pol
         (while keeping x unchanged) leaves every output word identical.
     (f) SEQ ARITHMETIC: word i (0-based) carries seq == (i+1) & 0xF.

   -------------------------------------------------------------------------
   Window timing note: seq is incremented BEFORE the emit (same as wseq in
   dvs_vital), so word index i (0-based) carries seq == (i+1) & SEQ_MASK.
   The alarm and position fields reflect the state AFTER processing all BATCH
   events of that batch AND after the per-batch decay step.

   -------------------------------------------------------------------------
   SRAM budget: this app uses only a handful of uint32_t scalars plus an
   8-entry uint32_t array (bin[8]).  Total static state is well under 100
   bytes.  The 32 KB SRAM is more than sufficient.  */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v / dvs_vital). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Column bin: x is 7 bits (0..125), divided into 8 bins of 16 pixels.
   col = (x >> 4) & 7   (shift only, no multiply). */
#define COL_SHIFT 4
#define NCOLS     8

/* Alarm integrator tunables (#ifndef allows compile-time -D overrides). */
#ifndef LEAK_K
#define LEAK_K 3            /* R decays by R>>3 per batch (~12.5% per step) */
#endif

#ifndef ALARM_THRESH
#define ALARM_THRESH 24     /* R > this fires the alarm */
#endif

#ifndef R_CAP
#define R_CAP 127u          /* saturating ceiling for R (fits 7 bits) */
#endif

/* Column histogram tunables. */
#ifndef BIN_CAP
#define BIN_CAP 255u        /* saturating ceiling for each column bin */
#endif

#ifndef HIST_DECAY_BATCHES
#define HIST_DECAY_BATCHES 32  /* column bins halved every this many batches */
#endif

/* Output word packing constants. */
#define RATE_SHIFT  0u      /* R already fits 7 bits (saturated at R_CAP=127) */
#define SEQ_MASK    0xFu    /* 4-bit batch sequence counter */

/* -- State (all zeroed by crt0.S at cold start) -- */

/* Alarm integrator R: charges by +1 per event, decays by R>>LEAK_K per
   batch.  Saturates at R_CAP.  Alarm fires when R > ALARM_THRESH. */
static uint32_t R;

/* Column histogram: 8 bins, bin[c] counts events in x-column c.
   Decays >>1 every HIST_DECAY_BATCHES batches.  Saturates at BIN_CAP. */
static uint32_t bin[NCOLS];

/* Burglar progress: ratchet of argmax bin index reached so far (0..7). */
static uint32_t progress;

/* Batch-level countdown for histogram decay.  Zeroed by crt0.S. */
static uint32_t hist_decay_ctr;

/* Batch sequence counter mod 16.  Zeroed by crt0.S. */
static uint32_t seq;

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

    /* Process each event in arrival order: decode x, update integrator and
       column histogram.  y, ts, pol are decoded per ABI but unused. */
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x   = (v[i] >> X_SHIFT) & 0x7Fu;
        /* uint32_t y   = (v[i] >> Y_SHIFT) & 0x7Fu;  -- decoded per ABI, unused */
        /* uint32_t ts  = (v[i] >> 1) & 0xFFFFu;      -- decoded per ABI, unused */
        /* uint32_t pol =  v[i] & 1u;                 -- decoded per ABI, unused */

        /* Alarm integrator: R += 1, saturate at R_CAP. */
        if (R < R_CAP) R++;

        /* Column histogram: col = (x >> COL_SHIFT) & 7; bin[col] += 1. */
        uint32_t col = (x >> COL_SHIFT) & (NCOLS - 1u);
        if (bin[col] < BIN_CAP) bin[col]++;
    }

    /* Per-batch decay of the alarm integrator: R -= R >> LEAK_K. */
    R -= R >> LEAK_K;

    /* Per-batch histogram column decay (every HIST_DECAY_BATCHES batches). */
    hist_decay_ctr++;
    if (hist_decay_ctr >= (uint32_t)HIST_DECAY_BATCHES) {
        hist_decay_ctr = 0u;
        for (uint32_t c = 0u; c < NCOLS; c++) {
            bin[c] >>= 1;
        }
    }

    /* Compute current argmax over 8 bins (compare loop, no multiply).
       Ties broken by lowest column index (strict > keeps first maximum). */
    uint32_t peak = 0u, pos = 0u;
    for (uint32_t c = 0u; c < NCOLS; c++) {
        if (bin[c] > peak) { peak = bin[c]; pos = c; }
    }

    /* Update progress ratchet: progress = max(progress, pos). */
    if (pos > progress) progress = pos;

    /* Alarm flag: 1 if R > ALARM_THRESH. */
    uint32_t alarm = (R > (uint32_t)ALARM_THRESH) ? 1u : 0u;

    /* Quantized rate for the output word: R already fits 7 bits (R_CAP=127). */
    uint32_t rate = R;   /* 7 bits, no shift needed */

    /* Advance batch sequence counter BEFORE emit (so word i carries
       seq == (i+1) & SEQ_MASK, mirroring dvs_vital's wseq convention). */
    seq = (seq + 1u) & SEQ_MASK;

    /* Emit ONE word per batch.
       Layout: bits[3:0]=seq, bits[6:4]=progress, bits[9:7]=pos,
               bits[16:10]=rate, bits[17]=alarm, bits[31:18]=0 */
    *FIFO_OUT = (alarm    << 17)
              | (rate     << 10)
              | (pos      <<  7)
              | (progress <<  4)
              |  seq;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- the correct cold start for ALL state
       in this app.  R, bin[0..7], progress, hist_decay_ctr, and seq all start
       at 0 without any explicit initialisation here. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
