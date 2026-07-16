#include <stdint.h>

/* "The Finish-Line Loom" (dvs_loom) -- a chips/fpga demo app in the same shape
   as software/dvs_flinch/main.c (fifo_in fires event_id_0 once BATCH words land;
   isr_handler reads them, updates per-(slit,y-bin) hit counters, selects the best
   candidate from three fixed vertical slits, and writes ONE sample word; it NEVER
   calls wfi(), see the epilogue comment on isr_handler).

   Idea (slit-scan / photo-finish loom -- novel among these apps): three fixed
   4-px-wide vertical slits watch the scene. Whatever crosses a slit is WOVEN into
   a cloth strip on the host: y is the warp direction (thread rows), TIME is the
   weft direction (thread columns, advancing in fixed batches), and event polarity
   is the thread colour (on/off edge). Think of the old photo-finish camera in
   athletics: a thin vertical slit photographs the line, time unrolling sideways,
   so a runner becomes a taffy-stretched silhouette -- only the chip here runs
   THREE slits simultaneously and the timebase is events, not microseconds. The
   cloth unfolds on the host (chips/fpga/dvs_loom_view.py); the chip only ever
   emits tiny {slit, y, pol, weft, flag} samples -- one per BATCH of 4 events --
   plus a slit=3 sentinel on batches that hit no slit. Distinct from every prior
   app (grids/radial/creatures/caustics/black-holes/looming).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). Every operation is a shift, add, sub, compare,
   or logical:
     - xq (slit column bin): xq = x>>2, compared with three constants (SLIT0_XQ,
                             SLIT1_XQ, SLIT2_XQ); only shifts+compares.
     - hit array index     : idx = (slit<<5) | ybin (stride 32 = 1<<5 is a power
                             of two so the row index is a shift, the column a mask;
                             no multiply). ybin = y>>YBIN_SHIFT (a shift).
     - saturating add      : if (hits[idx] < 255) hits[idx]++; (compare + add).
     - flag computation    : f = (hits[idx] >= MIN_HITS) ? 1 : 0; (one compare).
     - candidate selection : keep-if-higher-flag rule via two compares + branch;
                             no multiply anywhere.
     - weft advance        : batch_count++; compare with WEFT_BATCHES; weft =
                             (weft+1) & WEFT_MASK; zero hits[] with a byte loop;
                             all shift/add/and/compare.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_flinch / dvs_blackhole):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp field; decoded per ABI but
                                    UNUSED by this app -- the weft timebase is
                                    event-count based, so dvs_loom validates
                                    identically on any recording regardless of
                                    whether ts is real microseconds or a wrapped
                                    coarse counter)
     pol =  word        & 1
   (Several earlier apps read x/y/ts from the LOW bits -- the STALE layout; on
   the FPGA that reads the wrong bits. This app matches evt_pack.v + dvs_flinch.)

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). Two guards, both
   multiply-free:
     1. Per-(slit, 4-px y-bin) SATURATING HIT COUNTER, cleared every weft step.
        A coarse 4-px y-bin already averages several pixel rows; sparse hot-pixel
        sparkle fires roughly one stray event per cell per weft step (64 events)
        and never reaches MIN_HITS. A real edge crossing a slit fires many
        same-cell events within one step -- it reliably clears the floor.
        Only shifts, adds, and compares; no multiply.
     2. FLAG-RANKING CANDIDATE SELECTION. Flagged hits (f=1) always outrank
        unflagged hits (f=0) for the per-batch emission slot, so a hot-pixel
        sparkle that squeaks through as f=0 can never displace a genuine f=1
        signal from the same batch. The host renders flag=0 threads faint (thin,
        translucent weft yarns) and flag=1 threads bold, giving a natural
        noise floor on the woven cloth without any explicit gating on the host.
     The sentinel (slit=3) still carries the live weft value on every batch, so
     the host loom advances its column counter without interruption and the cloth
     never has missing weft columns.

   -------------------------------------------------------------------------
   Emission cadence: ONE word per BATCH-sized batch of events (exactly like
   dvs_flinch / dvs_blackhole), so the host stream never stalls and the cloth
   weaves at a constant rate. On batches that hit no slit: slit=3, y=0, pol=0,
   flag=0, weft=current -- the sentinel advances the loom column on the host.

   Output word layout (low 18 bits used):
     bits[1:0]   = slit  (0..2; 3 = no slit hit this batch)  -- 2 bits
     bits[8:2]   = y     (0..111, raw pixel row)              -- 7 bits
     bit [9]     = pol   (0=OFF edge, 1=ON edge)              -- 1 bit
     bits[16:10] = weft  (0..127, wrapping event-count weft column) -- 7 bits
     bit [17]    = flag  (1 = sample cleared the MIN_HITS noise floor) -- 1 bit
   Host unpacks these fields; see dvs_loom_view.py's unpack_status(). */

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

/* Slit geometry: 4-px-wide vertical bands, selected by xq = x>>SLIT_XQ_SHIFT.
   Slit 0: x=20..23 (xq=5), slit 1: x=60..63 (xq=15), slit 2: x=100..103 (xq=25).
   Spacing is ~40 px so the three slits sample left / centre / right thirds of
   the 126-px-wide frame without overlap. */
#define SLIT_XQ_SHIFT 2                  /* xq = x>>2 (4-px-wide bands) */
#define SLIT0_XQ 5                       /* slit 0: x=20..23 */
#define SLIT1_XQ 15                      /* slit 1: x=60..63 */
#define SLIT2_XQ 25                      /* slit 2: x=100..103 */

/* Y binning: ybin = y>>YBIN_SHIFT (4-px rows -> 0..27, 28 bins for SY=112).
   The hit array uses (slit<<5)|ybin as the index; stride 32 = 1<<5 is a power
   of two so the index is a shift+or, not a multiply. 3 slits * 32 slots = 96
   bytes (84 used: 28 bins * 3 slits). */
#define YBIN_SHIFT 2                     /* 4-px y-bins: 111>>2 = 27 -> bins 0..27 */
#define SLIT_STRIDE_SHIFT 5              /* stride per slit = 32 = 1<<5 */

/* Weft advance: after WEFT_BATCHES batches (= WEFT_BATCHES*BATCH events), the
   weft column advances by 1 and hit counters are cleared. Event-driven timebase.
   WEFT_MASK wraps the 7-bit weft counter (0..127). */
#ifndef WEFT_BATCHES
#define WEFT_BATCHES 16                  /* weft advances every 16 batches = 64 events */
#endif

/* Noise-floor: a y-bin must accumulate at least MIN_HITS hits within one weft
   step to earn flag=1 (a genuine edge crossing). Isolated hot-pixel sparkle
   typically fires ~1 event per bin per step and never reaches this floor. */
#ifndef MIN_HITS
#define MIN_HITS 4
#endif

/* Weft wraps at 128 (7 bits). */
#define WEFT_MASK 0x7F

/* Hit counters: one saturating uint8 per (slit, y-bin) cell.
   Index = (slit<<SLIT_STRIDE_SHIFT) | ybin.  All in .bss (zeroed by crt0.S). */
static uint8_t  hits[96];               /* 3 slits * 32 y-slots; 96 bytes */
static uint32_t weft;                   /* current weft column (0..127) */
static uint32_t batch_count;            /* batches since last weft advance */

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to the
   instruction after it -- the next interrupt jumps straight to event_id_0's
   vector. A wfi() call inside this function would permanently skip its own
   epilogue (the stack pointer's restore), leaking 16 bytes of stack every
   interrupt until it collides with this program's own code (see
   software/dvs_motion/main.c's isr_handler comment for the full explanation).
   Just returning is correct: this function's own `ret` lands on the same cached
   wfi() site main()'s return already relies on. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Process each event in order, tracking the best candidate for this batch.
       Candidate selection: flagged hits (flag=1) outrank unflagged ones (flag=0);
       within the same flag class the last event wins (last-wins within class). */
    uint32_t have_candidate = 0;
    uint32_t cand_slit = 0;
    uint32_t cand_y    = 0;
    uint32_t cand_pol  = 0;
    uint32_t cand_flag = 0;

    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x   = (v[i] >> X_SHIFT) & 0x7F;
        uint32_t y   = (v[i] >> Y_SHIFT) & 0x7F;
        uint32_t pol =  v[i]             & 0x1u;
        /* ts = (v[i] >> 1) & 0xFFFF; -- decoded per ABI but unused (event-count
           timebase; see the TIMEBASE note in the header comment). */

        uint32_t xq = x >> SLIT_XQ_SHIFT;    /* 4-px column bin */

        uint32_t slit;
        if      (xq == SLIT0_XQ) slit = 0u;
        else if (xq == SLIT1_XQ) slit = 1u;
        else if (xq == SLIT2_XQ) slit = 2u;
        else                      continue;    /* not in any slit */

        uint32_t ybin = y >> YBIN_SHIFT;
        uint32_t idx  = (slit << SLIT_STRIDE_SHIFT) | ybin;

        /* Saturating hit counter for this (slit, y-bin). */
        if (hits[idx] < 255u) hits[idx]++;

        /* Flag: 1 if this bin has accumulated enough hits to clear the noise floor. */
        uint32_t f = (hits[idx] >= MIN_HITS) ? 1u : 0u;

        /* Candidate selection (EXACT contract):
             - no candidate yet                -> always take
             - f >= candidate's flag           -> replace (flagged beats unflagged;
                                                 last-wins within the same flag class)
             - f <  candidate's flag           -> do NOT replace (unflagged never
                                                 displaces a flagged candidate)       */
        if (!have_candidate || (f >= cand_flag)) {
            have_candidate = 1u;
            cand_slit = slit;
            cand_y    = y;
            cand_pol  = pol;
            cand_flag = f;
        }
    }

    /* Emit ONE word to FIFO_OUT for this batch, using the weft counter that is in
       effect DURING this batch (i.e. before the weft-advance step below).
       If a candidate exists: pack {flag, weft, pol, y, slit}.
       Otherwise (no slit hit): slit=3 sentinel -- the host loom advances its column
       counter and keeps the cloth moving without a missing weft gap. */
    uint32_t out;
    if (have_candidate) {
        out = (cand_flag << 17)
            | (weft      << 10)
            | (cand_pol  <<  9)
            | (cand_y    <<  2)
            |  cand_slit;
    } else {
        out = (weft << 10) | 3u;   /* slit=3 sentinel; y=0, pol=0, flag=0 */
    }
    *FIFO_OUT = out;

    /* Advance weft counter and clear hit array every WEFT_BATCHES batches. */
    batch_count++;
    if (batch_count >= WEFT_BATCHES) {
        batch_count = 0u;
        weft = (weft + 1u) & WEFT_MASK;
        for (uint32_t k = 0; k < 96u; k++) hits[k] = 0u;
    }
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so hits/weft/batch_count start cold. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
