#include <stdint.h>

/* "Apophenia Engine" -- a living Rorschach. A chips/fpga demo app in the same
   shape as software/dvs_heartbeats/main.c and software/dvs_motion/main.c
   (fifo_in fires event_id_0 once BATCH words land; isr_handler reads them,
   updates a coarse activity grid, writes ONE status word, and returns -- it
   NEVER calls wfi(), see the epilogue comment on isr_handler).

   Idea: apophenia is the human tendency to see meaningful patterns (faces,
   figures) in random noise. This app keeps a small, coarse, DECAYING "activity"
   grid over the sensor. Each event warms the cell it lands in; every so often
   the whole grid cools by a right-shift. The chip streams out the hottest cell
   per batch. The HOST mirrors that grid into a 4-fold SYMMETRIC inkblot (reflect
   the quadrant across x and y), so a moving crowd/hand becomes a breathing,
   organic Rorschach shape that warps as people move. The chip only ever emits
   {xq, yq, val, flag}; all the mirroring/colour/animation happens on the
   computer (chips/fpga/dvs_apophenia_view.py + the dashboard renderer).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). Everything below is compares, shifts, adds:
     - cell index         : xq = x>>2 (0..31), yq = y>>3 (0..13); the grid is
                            padded to a power-of-two stride (GRID_STRIDE=32) so
                            cell = (yq<<GRID_STRIDE_SHIFT)|xq is a shift, not a
                            multiply.
     - warm (saturating +): grid[cell] += STEP, clamped at GRID_CAP
     - cool (leaky decay)  : c -= c>>DECAY_SHIFT  over ALL cells, every
                            DECAY_INTERVAL batches -- a periodic halving-ish leak
     - hottest cell        : argmax by compare (no divide)

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_track/main.c):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit ~microsecond timestamp, wraps)
     pol =  word        & 1
   (An earlier revision read x/y/ts from the low bits -- the STALE layout that
   the upstream dvs_motion/rotate still use; on the FPGA that reads the wrong
   bits. Match evt_pack.v + dvs_track. The chips/fpga mirror packs the same way.)

   The sensor frame is SX x SY = 126 x 112. Coarse cells are XQ_SIZE=4 px wide
   (x>>2 -> 0..31, since 125>>2 = 31) by YQ_SIZE=8 px tall (y>>3 -> 0..13, since
   111>>3 = 13). So the LOGICAL grid is GRID_COLS=32 x GRID_ROWS=14 = 448 cells,
   stored in a GRID_STRIDE=32 x GRID_ROWS=14 uint8 array (stride is already a
   power of two, so no padding is wasted -- 32*14 = 448 bytes of state).

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). Coarse 4x8-px binning
   already averages down isolated hot pixels. On top of that, a small per-cell
   EMIT_THRESHOLD means a cell must accumulate several events before it is ever
   reported, so a single stray event (which warms a cell to just STEP) never
   emits -- only a cell that has been genuinely, repeatedly active crosses the
   threshold. The decay keeps the grid bounded and lets the inkblot "breathe".

   -------------------------------------------------------------------------
   Emission cadence: ONE status word per BATCH-sized batch of events (exactly
   like dvs_heartbeats emits once per batch). We report the HOTTEST cell in the
   grid whose value is >= EMIT_THRESHOLD; if none qualifies we report the cell
   the last event of the batch touched (so the stream never stalls) but with the
   flag bit cleared to mark it as "below threshold / not a real peak".

   Output word layout (low 18 bits used):
     bits[4:0]   = xq    (0..31, 5 bits)   -- coarse X, x>>2
     bits[8:5]   = yq    (0..13, 4 bits)   -- coarse Y, y>>3
     bits[16:9]  = val   (0..255, 8 bits)  -- that cell's current activity
     bit [17]    = flag  (1 bit)           -- 1 = real peak (>=EMIT_THRESHOLD),
                                              0 = fallback (last-touched cell)
   Host unpacks these fields; see dvs_apophenia_view.py's unpack_status(). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v / dvs_track). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Coarse activity grid. XQ_SIZE=4-px columns (x>>2 -> 0..31), YQ_SIZE=8-px rows
   (y>>3 -> 0..13). GRID_STRIDE is a power of two so cell index is a shift, not a
   multiply; here stride == COLS == 32 so nothing is wasted. XQ_SHIFT/YQ_SHIFT
   and the col/row counts must move together (see dvs_motion's CELL_SHIFT note):
   a cell index must stay < GRID_CELLS or it walks off grid[]. */
#define XQ_SHIFT 2                              /* 4-px columns: 125>>2 = 31 -> cols 0..31 */
#define YQ_SHIFT 3                              /* 8-px rows:    111>>3 = 13 -> rows 0..13 */
#define GRID_COLS 32                            /* logical columns (0..31) */
#define GRID_ROWS 14                            /* logical rows    (0..13) */
#define GRID_STRIDE_SHIFT 5                     /* row*GRID_STRIDE via shift: STRIDE==32==1<<5 */
#define GRID_STRIDE (1 << GRID_STRIDE_SHIFT)    /* = 32 (power-of-two stride for shift indexing) */
#define GRID_CELLS (GRID_STRIDE * GRID_ROWS)    /* = 448 cells (uint8 each -> 448 B of state) */

/* Warm / cool / clamp parameters. All shifts+adds+compares, no multiply. */
#define STEP        24   /* activity added to a cell per event (saturating) */
#define GRID_CAP    255  /* 8-bit saturation for the leaky activity counter */
#define DECAY_SHIFT 1    /* cool: c -= c>>1  (halve) every DECAY_INTERVAL batches */

#ifndef DECAY_INTERVAL
#define DECAY_INTERVAL 8 /* decay the whole grid once every this-many batches */
#endif

/* A cell must reach this before it is reported as a real inkblot peak. Coarse
   binning + this threshold means a lone stray event (which warms a cell to just
   STEP) never emits. Tunable at build time. */
#ifndef EMIT_THRESHOLD
#define EMIT_THRESHOLD 64
#endif

/* Per-cell activity, in .bss (zeroed by crt0.S) so the grid starts cold. */
static uint8_t  grid[GRID_CELLS];
static uint32_t batch_count;    /* how many batches processed -> drives decay cadence */

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

    uint32_t last_cell = 0;
    uint32_t last_pol  = 0;

    /* Warm the grid: each event saturating-increments its coarse cell. */
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x   = (v[i] >> X_SHIFT) & 0x7F;
        uint32_t y   = (v[i] >> Y_SHIFT) & 0x7F;
        uint32_t pol =  v[i]             & 1;

        uint32_t xq   = x >> XQ_SHIFT;                       /* 0..31 */
        uint32_t yq   = y >> YQ_SHIFT;                       /* 0..13 */
        uint32_t cell = (yq << GRID_STRIDE_SHIFT) | xq;      /* row*STRIDE via shift */
        last_cell = cell;
        last_pol  = pol;

        uint32_t updated = grid[cell] + STEP;
        grid[cell] = (uint8_t)((updated > GRID_CAP) ? GRID_CAP : updated);
    }

    /* Periodic cool-down: halve every cell so the inkblot breathes and stays
       bounded (leaky decay, shift-only). */
    batch_count++;
    if (batch_count >= DECAY_INTERVAL) {
        batch_count = 0;
        for (uint32_t c = 0; c < GRID_CELLS; c++) {
            grid[c] = (uint8_t)(grid[c] - (grid[c] >> DECAY_SHIFT));
        }
    }

    /* Find the hottest cell in the grid (argmax by compare). */
    uint32_t best_cell = 0;
    uint32_t best_val  = 0;
    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        if (grid[c] > best_val) {
            best_val  = grid[c];
            best_cell = c;
        }
    }

    /* Report the hottest cell if it crosses the emit threshold (flag=1, real
       peak); otherwise fall back to the last-touched cell (flag=0) so the stream
       never stalls but the host can tell it is below threshold. */
    uint32_t out_cell, out_val, flag;
    if (best_val >= EMIT_THRESHOLD) {
        out_cell = best_cell;
        out_val  = best_val;
        flag     = 1;
    } else {
        out_cell = last_cell;
        out_val  = grid[last_cell];
        flag     = 0;
    }

    uint32_t xq = out_cell & (GRID_STRIDE - 1);   /* low 5 bits: xq (0..31) */
    uint32_t yq = out_cell >> GRID_STRIDE_SHIFT;  /* high bits:  yq (0..13) */

    /* bits[4:0]=xq, bits[8:5]=yq, bits[16:9]=val, bit[17]=flag. */
    *FIFO_OUT = (flag << 17) | (out_val << 9) | (yq << 5) | xq;

    (void)last_pol;   /* polarity decoded but not used by this app */
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so grid/batch_count start clean. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
