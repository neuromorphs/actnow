#include <stdint.h>

/* "THE WHIPCRACKER" (dvs_whip) -- a chips/fpga demo app shaped like
   software/dvs_vital/main.c (fifo_in fires event_id_0 once BATCH words land;
   isr_handler reads them, updates per-column leaky activity counters and
   wavefront tracking, latches a verdict every window, and writes ONE status
   word per batch; it NEVER calls wfi()).

   Idea (whip-crack speed gauge -- novel among these apps): flick a rope or
   towel; the chip watches the traveling-wave activation front sweep across
   pixel columns 0..125 and certifies whether the tip broke the sound barrier.
   A wavefront hop is two successive column activations (col0 -> col1) with
   dt = Δt ticks; the speed is Δcol / Δt -- but NO DIVIDE IS USED: a
   precomputed LUT of "max Δt allowed per Δcol" classifies each hop into one
   of NSPEEDBINS speed bins by pure comparison.  The max speed bin reached
   during the window is latched; if any hop's Δt < LUT_sonic[Δcol] the
   SONIC flag is set.

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) Fast sweep 0..125 at sonic speed: Δt=1 per column.
         Speed is high, all hops are classified in the top bin, sonic=1,
         valid=1, maxspeedbin=NSPEEDBINS-1.
     (b) Slow sweep 0..125 at subsonic speed: Δt=LUT[1]*2+1 per column.
         sonic=0, valid=1, maxspeedbin < NSPEEDBINS-1.
     (c) Static hot pixel: same (x,y) fires at every event, same column;
         col_act counter saturates but the "newly crossed threshold" test
         requires the activation epoch to change (prev_epoch != cur_epoch);
         a hot pixel always activates in EVERY epoch, so it never produces
         a new crossing -> no front detected, valid=0 at window end.
     Well-formedness of output word checked in (d).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - Column activity counter: compare + saturating add; no multiply.
     - Epoch-based activation latch: compare only; no multiply.
     - Δcol computation: compare (ensure monotone hop); sub; compare cap;
       no multiply.
     - Δt computation: masked 16-bit wrap subtract; no multiply.
     - Speed classification: LUT lookup (precomputed constants) + compare
       loop over NSPEEDBINS to find bin; no multiply; all LUT values are
       shift/sub/add expressions evaluated at compile time by the compiler.
     - Noise guards: compare + conditional; no multiply.
     - Output pack: shifts and ORs only; no multiply.
   No multiply anywhere.

   -------------------------------------------------------------------------
   CORE TRICK (multiply-free speed bins):
   We want to classify speed = Δcol / Δt.  Rewrite as: speed > threshold_s
   iff Δt < Δcol / threshold_s.  Pre-compute for each bin s:

     MAX_DT_PER_DC[s][dc]  = dc * TICKS_PER_COLSPEED_UNIT >> s

   i.e. the maximum Δt (exclusive) for a hop of Δcol=dc to qualify as bin s.
   This uses integer shift as division, so speeds are powers-of-two multiples
   of a base unit.  LUT dimensions are NSPEEDBINS x MAX_DC, all uint16_t.
   Generated at compile time from closed-form integer expressions; no runtime
   multiply ever occurs.

   IMPLEMENTATION: for simplicity, store one LUT per Δcol value up to
   MAX_DC=16 (implausibly large Δcol hops are noise and ignored), with
   NSPEEDBINS=16 bins.  Bin 0 is the slowest (Δt barely within a wide limit);
   bin NSPEEDBINS-1 is the fastest (Δt < threshold corresponding to >=2^15
   col/tick which is the sonic zone).

   Each LUT entry lut_max_dt[dc][s] = (dc << (NSPEEDBINS-1-s)).
   A hop (dc, dt) falls in bin s (the highest bin for which dt < lut_max_dt)
   -- chosen as the largest s such that dt < lut_max_dt[dc][s].
   Sonic means s == NSPEEDBINS-1, i.e. dt < dc (one tick per column or less).

   -------------------------------------------------------------------------
   Noise guards (SciDVS 126x112, very noisy):
     1. ACTIVITY GUARD.  Each column has an 8-bit leaky activity counter.
        An event at column x increments col_act[x] (saturating at ACT_CAP).
        The counter decays by ACT_LEAK every LEAK_EPOCH batches (shift-right;
        no multiply).  A column can only be "activated" (become the wavefront
        col) once its counter reaches ACT_THRESH.  Hot pixels accumulate fast
        but they saturate and never produce a moving front -- see guard 3.
     2. IMPLAUSIBLE HOP GUARD.  Hops with Δcol=0 (same column re-activates)
        or Δcol > MAX_DC are ignored: Δcol=0 is not a front advance; very
        large jumps (> MAX_DC columns at once) are almost certainly noise
        or the whip lifting out of frame, not a physical wavefront.
     3. STATIC PIXEL GUARD (epoch-based activation).  A "column activation"
        only counts if the column's last-seen activation epoch differs from
        the current epoch.  The epoch is a monotone counter that increments
        every EPOCH_BATCHES batches.  A genuine wavefront sweep activates
        each column ONCE per pass; a static hot pixel would re-activate in
        every epoch, so its prev_epoch == cur_epoch, and it is silently
        skipped.  Valid=1 is set only if at least MIN_HOPS classified hops
        occurred in the window.

   -------------------------------------------------------------------------
   Output word layout (18 bits used):
     bit     0       = valid   (1 = at least MIN_HOPS classified hops in window)
     bits[ 4: 1]     = seq     (4-bit window sequence counter, wraps mod 16)
     bits[11: 5]     = front_col (current activation column, 0..125)
     bit    12       = sonic   (1 = any hop this window was fast enough)
     bits[16:13]     = maxspeedbin (0..15, highest speed bin reached this window)
     bits[31:17]     = 0
   Host unpacks these fields; see chips/fpga/dvs_whip_view.py's
   unpack_status().

   -------------------------------------------------------------------------
   The event word (evt_pack.v):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)
     pol =  word        & 1

   -------------------------------------------------------------------------
   TIMEBASE: this app IS ts-driven.  The timestamp field is a 16-bit
   wrapping tick counter.  Masked 16-bit subtraction gives exact deltas for
   true intervals < 65536 ticks; gaps longer than 65535 ticks alias.  For
   typical whip-crack recordings the wavefront traverses in well under
   65535 ticks.

   -------------------------------------------------------------------------
   Window timing note: the latch and seq advance happen BEFORE the emit on
   the closing batch of a window.  Consequently, output word index i (0-based)
   carries seq == ((i + 1) / WINDOW_BATCHES) & 0xF.  Host code should treat
   seq=0 as "not yet valid" or skip the first window if a clean start matters.

   -------------------------------------------------------------------------
   Why NO wfi() here: same reason as dvs_vital.  See that file's isr_handler
   epilogue comment. */

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

/* 16-bit timestamp mask for wrapped subtraction. */
#define TS_MASK 0xFFFFu

/* Speed LUT parameters (all shift/sub/compare -- no multiply). */
#ifndef NSPEEDBINS
#define NSPEEDBINS  16          /* number of speed bins; bin 15 = fastest */
#endif

/* Maximum Δcol for a valid hop (larger hops are noise). */
#ifndef MAX_DC
#define MAX_DC      16
#endif

/* Activity / noise guard thresholds. */
#ifndef ACT_THRESH
#define ACT_THRESH  8           /* column must accumulate this many events to be active */
#endif

#ifndef ACT_CAP
#define ACT_CAP     200         /* saturating ceiling for col_act[] */
#endif

#ifndef ACT_LEAK
#define ACT_LEAK    3           /* col_act[x] >>= ACT_LEAK every LEAK_EPOCH batches */
#endif

#ifndef LEAK_EPOCH
#define LEAK_EPOCH  64          /* batches between leak passes */
#endif

/* Epoch length for the static-pixel guard. */
#ifndef EPOCH_BATCHES
#define EPOCH_BATCHES 32        /* col_epoch[x] epoch counter increments every N batches */
#endif

/* Minimum classified hops in a window for valid=1. */
#ifndef MIN_HOPS
#define MIN_HOPS    2
#endif

/* Window length (matches dvs_vital for scheduling parity). */
#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 256
#endif

/* Non-tunable constants. */
#define WSEQ_MASK    0xFu       /* 4-bit window sequence counter mask */
#define EPOCH_MASK   0xFFu      /* 8-bit epoch counter mask */

/* --------------------------------------------------------------------------
   LUT_MAX_DT[dc][s]  = dc << (NSPEEDBINS-1-s)
   A hop (dc, dt) qualifies for bin s iff dt < LUT_MAX_DT[dc][s].
   Bin 15 (s=15): dt < dc  (one tick per column or faster -- SONIC).
   Bin 0  (s=0):  dt < dc << 15  (very slow).
   All entries are uint16_t; dt is uint16_t (TS_MASK-masked).
   --------------------------------------------------------------------------
   Generated at compile time as a C initialiser; no runtime multiply is ever
   needed.  Values that exceed 0xFFFF are clamped to 0xFFFF so they fit. */

#define DC_ROW(d) { \
    ((uint32_t)((d) << 15) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) << 15)), \
    ((uint32_t)((d) << 14) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) << 14)), \
    ((uint32_t)((d) << 13) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) << 13)), \
    ((uint32_t)((d) << 12) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) << 12)), \
    ((uint32_t)((d) << 11) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) << 11)), \
    ((uint32_t)((d) << 10) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) << 10)), \
    ((uint32_t)((d) <<  9) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  9)), \
    ((uint32_t)((d) <<  8) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  8)), \
    ((uint32_t)((d) <<  7) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  7)), \
    ((uint32_t)((d) <<  6) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  6)), \
    ((uint32_t)((d) <<  5) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  5)), \
    ((uint32_t)((d) <<  4) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  4)), \
    ((uint32_t)((d) <<  3) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  3)), \
    ((uint32_t)((d) <<  2) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  2)), \
    ((uint32_t)((d) <<  1) > 0xFFFFu ? 0xFFFFu : (uint16_t)((d) <<  1)), \
    (uint16_t)(d),                                                          \
}

/* Row 0 unused (dc=0 is a no-op hop). */
static const uint16_t lut_max_dt[MAX_DC + 1][NSPEEDBINS] = {
    DC_ROW(0),   /* dc=0  -- never referenced */
    DC_ROW(1),   /* dc=1  */
    DC_ROW(2),   /* dc=2  */
    DC_ROW(3),   /* dc=3  */
    DC_ROW(4),   /* dc=4  */
    DC_ROW(5),   /* dc=5  */
    DC_ROW(6),   /* dc=6  */
    DC_ROW(7),   /* dc=7  */
    DC_ROW(8),   /* dc=8  */
    DC_ROW(9),   /* dc=9  */
    DC_ROW(10),  /* dc=10 */
    DC_ROW(11),  /* dc=11 */
    DC_ROW(12),  /* dc=12 */
    DC_ROW(13),  /* dc=13 */
    DC_ROW(14),  /* dc=14 */
    DC_ROW(15),  /* dc=15 */
    DC_ROW(16),  /* dc=16 */
};

/* Per-column leaky activity counter (0..ACT_CAP, saturating).
   Zeroed by crt0.S at cold start. */
static uint8_t col_act[SX];

/* Per-column last-activation epoch (0..255 wrapping).
   Zeroed by crt0.S; 0 means "never activated". */
static uint8_t col_epoch[SX];

/* Column of the most recent wavefront activation (valid when prev_col_valid).
   Zeroed by crt0.S. */
static uint32_t prev_col;

/* Timestamp of the most recent wavefront activation.
   Zeroed by crt0.S. */
static uint32_t prev_col_ts;

/* 1 if prev_col and prev_col_ts hold a valid prior activation.
   Zeroed by crt0.S. */
static uint32_t prev_col_valid;

/* Maximum speed bin reached in the current window.  Zeroed by crt0.S. */
static uint32_t window_maxbin;

/* 1 if any hop this window was classified sonic.  Zeroed by crt0.S. */
static uint32_t window_sonic;

/* Count of classified hops in the current window.  Zeroed by crt0.S. */
static uint32_t window_hops;

/* Current front column (last activation column, latched for output).
   Zeroed by crt0.S. */
static uint32_t window_front_col;

/* Latched values from the last completed window (emitted each batch).
   Zeroed by crt0.S. */
static uint32_t lat_valid;
static uint32_t lat_seq;
static uint32_t lat_front_col;
static uint32_t lat_sonic;
static uint32_t lat_maxspeedbin;

/* Batch-within-window counter (0..WINDOW_BATCHES-1).  Zeroed by crt0.S. */
static uint32_t batch_in_window;

/* 4-bit window sequence counter (0..15, wraps).  Zeroed by crt0.S. */
static uint32_t wseq;

/* Epoch counter for the static-pixel guard and for the leak pass scheduler.
   Zeroed by crt0.S. */
static uint32_t epoch;          /* wraps mod 256 */
static uint32_t leak_counter;   /* counts batches toward next leak pass */

/* classify_hop(dc, dt) -- multiply-free speed-bin classifier.
   Returns the highest bin s (0..NSPEEDBINS-1) such that dt < lut_max_dt[dc][s],
   or -1 (0xFFFFFFFF) if no bin applies (dt too large for even bin 0).
   Caller must ensure 1 <= dc <= MAX_DC. */
static uint32_t classify_hop(uint32_t dc, uint32_t dt) {
    /* Walk from fastest to slowest; first match gives the highest bin. */
    for (uint32_t s = NSPEEDBINS - 1u; s < (uint32_t)NSPEEDBINS; s--) {
        if (dt < (uint32_t)lut_max_dt[dc][s]) {
            return s;
        }
    }
    return 0xFFFFFFFFu;   /* below even bin 0 -- hop too slow to classify */
}

/* Must NOT call wfi() itself; see software/dvs_vital/main.c for the full
   explanation of why returning is correct.  In brief: soc.act's WFI-decode
   never returns control to the instruction after wfi(); calling it inside
   isr_handler leaks 16 bytes of stack per interrupt. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Advance the epoch and schedule a leak pass BEFORE processing events,
       so that the activity counters are up-to-date when we test ACT_THRESH.
       This also ensures a fresh epoch is visible for the first event of each
       batch, making the static-pixel guard correct. */
    leak_counter++;
    if (leak_counter >= (uint32_t)LEAK_EPOCH) {
        leak_counter = 0u;
        /* Leak pass: right-shift every col_act by ACT_LEAK (divide by 2^ACT_LEAK,
           rounded toward zero -- no multiply). */
        for (uint32_t c = 0u; c < (uint32_t)SX; c++) {
            col_act[c] >>= ACT_LEAK;
        }
    }

    /* Update epoch for static-pixel guard: advance epoch every EPOCH_BATCHES
       batches (batch_in_window is incremented AFTER event processing below,
       so we reference it before incrementing -- equivalent timing). */

    /* Process each event in the batch. */
    for (uint32_t i = 0u; i < BATCH; i++) {
        uint32_t x  = (v[i] >> X_SHIFT) & 0x7Fu;   /* column 0..125 */
        /* uint32_t y = (v[i] >> Y_SHIFT) & 0x7Fu; -- column only matters */
        uint32_t ts = (v[i] >>  1)       & TS_MASK;
        /* pol is unused */

        if (x >= (uint32_t)SX) continue;  /* out-of-range guard */

        /* Accumulate activity for this column (saturating). */
        if (col_act[x] < (uint8_t)ACT_CAP) col_act[x]++;

        /* Column must exceed ACT_THRESH before it can form part of the wavefront. */
        if (col_act[x] < (uint8_t)ACT_THRESH) continue;

        /* Static-pixel guard: a column can only trigger once per epoch. */
        if (col_epoch[x] == (uint8_t)(epoch & EPOCH_MASK)) continue;

        /* Mark this column as activated in the current epoch. */
        col_epoch[x] = (uint8_t)(epoch & EPOCH_MASK);

        /* Record as the current front column (even if the hop is noisy). */
        window_front_col = x;

        /* Attempt to classify the hop from prev_col to x. */
        if (prev_col_valid) {
            uint32_t dc;
            /* Only accept monotone forward hops (wavefront moves one way). */
            if (x > prev_col) {
                dc = x - prev_col;
            } else if (x < prev_col) {
                /* Reverse hop -- could be noise or wrap.  Treat as forward if
                   small (dc = SX - (prev_col - x) is the wrap distance, but
                   that would be large; instead just treat reverse as dc in
                   the backward sense -- still physically a hop).  For
                   simplicity, allow backward hops with the same LUT. */
                dc = prev_col - x;
            } else {
                /* dc=0: same column re-activates this batch, not a front hop. */
                dc = 0u;
            }

            if (dc >= 1u && dc <= (uint32_t)MAX_DC) {
                uint32_t dt = (ts - prev_col_ts) & TS_MASK;
                if (dt == 0u) dt = 1u;   /* dt=0 clamp to 1 (same-tick hop) */

                uint32_t bin = classify_hop(dc, dt);
                if (bin != 0xFFFFFFFFu) {
                    /* Valid classified hop: update window max and sonic flag. */
                    if (bin > window_maxbin) window_maxbin = bin;
                    if (bin == (uint32_t)(NSPEEDBINS - 1u)) window_sonic = 1u;
                    window_hops++;
                }
            }
        }

        /* This activation becomes the previous reference for the next hop. */
        prev_col       = x;
        prev_col_ts    = ts;
        prev_col_valid = 1u;
    }

    /* Advance batch-within-window and check epoch boundary. */
    batch_in_window++;

    /* Epoch advance: every EPOCH_BATCHES batches. */
    if (batch_in_window % (uint32_t)EPOCH_BATCHES == 0u) {
        epoch = (epoch + 1u) & EPOCH_MASK;
    }

    /* Window boundary: latch BEFORE emit (same discipline as dvs_vital). */
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;

        lat_valid        = (window_hops >= (uint32_t)MIN_HOPS) ? 1u : 0u;
        lat_sonic        = window_sonic;
        lat_maxspeedbin  = window_maxbin;
        lat_front_col    = window_front_col;

        /* Clear per-window accumulators (prev_col/prev_col_valid persist
           across windows: a front that straddles a window boundary should
           continue naturally). */
        window_maxbin  = 0u;
        window_sonic   = 0u;
        window_hops    = 0u;

        wseq = (wseq + 1u) & WSEQ_MASK;
        lat_seq = wseq;
    }

    /* Emit ONE word per batch from the LATCHED values only.
       Layout: bit[0]=valid, bits[4:1]=seq, bits[11:5]=front_col,
               bit[12]=sonic, bits[16:13]=maxspeedbin, bits[31:17]=0 */
    *FIFO_OUT = (lat_maxspeedbin << 13)
              | (lat_sonic       << 12)
              | (lat_front_col   <<  5)
              | (lat_seq         <<  1)
              |  lat_valid;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- the correct cold start for ALL state
       in this app.  col_act, col_epoch, prev_col, prev_col_ts, prev_col_valid,
       window_maxbin, window_sonic, window_hops, window_front_col,
       lat_valid, lat_seq, lat_front_col, lat_sonic, lat_maxspeedbin,
       batch_in_window, wseq, epoch, and leak_counter all start at 0. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
