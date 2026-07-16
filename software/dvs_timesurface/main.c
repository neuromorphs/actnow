#include <stdint.h>

/* chips/fpga variant that builds a fine-grained "time surface": a per-cell
   record of when that cell was last hit by an event, instead of
   software/dvs_motion/main.c's single decaying activity value collapsed
   down to one argmax cell. Same interrupt/FIFO wiring as
   software/dvs_motion -- fifo_in fires event_id_0 once BATCH words land,
   isr_handler reads BATCH words -- but here each event just stamps its own
   running index into whichever cell it landed in, and nothing ever decays
   on-chip. Recency/decay is computed entirely off-chip: the host receives
   raw (now, last_seen[]) snapshots and derives a decayed heatmap itself
   (elapsed = now - last_seen), so the grid can be far finer than
   dvs_motion's 4x4 without paying the interrupt-time cost of touching
   every cell on every batch.

   The grid is 32 cols x 28 rows of 4x4-pixel cells (CELL_SHIFT=2): 126>>2
   =31 and 112>>2=27, both within range, so GRID_COLS=32/GRID_ROWS=28 cover
   the whole SXxSY frame exactly -- 8x finer per axis than dvs_motion's
   4x4. GRID_COLS is a power of 2 so row*GRID_COLS is a plain shift
   (row<<5), keeping cell indexing multiply-free on this RV32I core.

   There is no timer/cycle-counter peripheral anywhere on this SoC (see
   core/globals.act's MMIO map), and the AER word's own ts[16:0] field is
   always 0 in every capture this project has (see chips/fpga/tests/e2e/
   e2e_fpga_rotate_test.act's header) -- neither is a usable clock.
   Instead "now" is this firmware's own running event counter: it means
   nothing in wall-clock terms, only relative order, which is exactly what
   a host-side decay computed from elapsed = now - last_seen needs.

   Every DUMP_INTERVAL=64 batches (256 events -- a power of 2, so "every
   Nth batch" is a plain AND-mask test, not a divide), isr_handler writes
   the whole grid out to the output FIFO: one word for `now`, then one word
   per cell (row-major, GRID_CELLS=896 words) holding that cell's
   last_seen value. A write to a full output FIFO blocks (real
   backpressure) instead of dropping data, same as every other program
   here. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

#define CELL_SHIFT 2                          /* 4x4-pixel cells: 126>>2=31, 112>>2=27,
                                                   both within GRID_COLS=32/GRID_ROWS=28 */
#define GRID_COLS  32
#define GRID_ROWS  28
#define GRID_CELLS (GRID_COLS * GRID_ROWS)     /* = 896 */

#define DUMP_INTERVAL 64   /* power of 2: batch_count & (DUMP_INTERVAL-1) tests "every 64th batch" */

static uint32_t last_seen[GRID_CELLS];
static uint32_t event_count;
static uint32_t batch_count;

/* isr_handler must not call wfi() -- see software/application/main.c's
   isr_handler comment for why. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x = v[i] & 0x7F;
        uint32_t y = (v[i] >> 7) & 0x7F;
        uint32_t col = x >> CELL_SHIFT;
        uint32_t row = y >> CELL_SHIFT;
        uint32_t cell = (row << 5) | col;   /* row*GRID_COLS via shift -- GRID_COLS==32==1<<5 */

        event_count++;
        last_seen[cell] = event_count;
    }

    batch_count++;
    if ((batch_count & (DUMP_INTERVAL - 1)) == 0) {
        *FIFO_OUT = event_count;
        for (uint32_t c = 0; c < GRID_CELLS; c++) {
            *FIFO_OUT = last_seen[c];
        }
    }
}

void main(void) {
    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        last_seen[c] = 0;
    }
    event_count = 0;
    batch_count = 0;

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
