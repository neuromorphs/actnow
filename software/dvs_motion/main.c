#include <stdint.h>

/* chips/fpga variant that reports a coarse moving-object location instead of
   dvs_rotate's per-event geometric transform. Same interrupt/FIFO wiring as
   software/application and software/dvs_rotate (fifo_in fires event_id_0
   once BATCH words land; isr_handler reads them, does its work, and
   returns), but the "work" here is a decaying activity grid rather than a
   per-event transform, and the output is one status word per BATCH-sized
   batch of events (not one per event) -- the interesting signal is the
   aggregate hottest cell after a batch, not any single event.

   The sensor frame (SX x SY, matching chips/fpga/dvs_replay.py) is divided
   into GRID_COLS x GRID_ROWS cells of CELL_SIZE=1<<CELL_SHIFT pixels. This
   core is plain RV32I (no multiply/divide -- see software/common/
   program.mk's -march=rv32i), so the cell size is deliberately a power of 2:
   col = x >> CELL_SHIFT, row = y >> CELL_SHIFT, cell = (row << 3) | col
   (row*GRID_COLS via shift since GRID_COLS==8==1<<3) -- no divide routine
   needed anywhere.

   Each cell is an 8-bit saturating counter. Every batch: halve every cell
   (exponential decay, so activity fades out over a few batches instead of
   latching forever), then add STEP to whichever cell(s) this batch's events
   land in, capped at CAP. After that, argmax over the grid gives the
   hottest cell; if its value clears THRESHOLD, the motion flag is set.
   Output word: bit14=motion, bits[13:6]=hottest value, bits[5:3]=row,
   bits[2:0]=col.

   chips/fpga/tests/e2e/e2e_fpga_motion_test.act drives this with real
   recorded events and asserts every result against the same grid math
   computed in Python. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

#define CELL_SHIFT 4                          /* 16x16-pixel cells */
#define GRID_COLS  8                           /* covers SX (126>>4 = 7, so col range is 0..7) */
#define GRID_ROWS  8                           /* covers SY with margin (112>>4 = 7 exactly; the
                                                   7-bit y field can in principle reach 127, so one
                                                   extra row avoids ever indexing out of bounds) */
#define GRID_CELLS (GRID_COLS * GRID_ROWS)     /* = 64 */

#define STEP      32   /* activity added per event landing in a cell */
#define CAP       255  /* 8-bit saturation, matches the output word's 8-bit activity field */
#define THRESHOLD 96   /* hottest-cell activity needed to raise the motion flag */

static uint8_t grid[GRID_CELLS];

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to
   the instruction after it, so a wfi() call inside an ISR permanently skips
   that ISR's own epilogue (the stack pointer's restore), leaking 16 bytes of
   stack every interrupt until it eventually collides with this program's own
   code (see software/application/main.c's isr_handler comment for the full
   explanation). Just returning is correct: this function's own `ret` lands
   on the same cached wfi() site main()'s return already relies on. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        grid[c] = (uint8_t)(grid[c] - (grid[c] >> 1));
    }

    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x = v[i] & 0x7F;
        uint32_t y = (v[i] >> 7) & 0x7F;
        uint32_t col = x >> CELL_SHIFT;
        uint32_t row = y >> CELL_SHIFT;
        uint32_t cell = (row << 3) | col;

        uint32_t updated = grid[cell] + STEP;
        grid[cell] = (uint8_t)((updated > CAP) ? CAP : updated);
    }

    uint32_t best_cell = 0;
    uint32_t best_val = grid[0];
    for (uint32_t c = 1; c < GRID_CELLS; c++) {
        if (grid[c] > best_val) {
            best_val = grid[c];
            best_cell = c;
        }
    }

    uint32_t best_col = best_cell & 0x7;
    uint32_t best_row = best_cell >> 3;
    uint32_t motion = (best_val >= THRESHOLD) ? 1u : 0u;

    *FIFO_OUT = (motion << 14) | (best_val << 6) | (best_row << 3) | best_col;
}

void main(void) {
    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        grid[c] = 0;
    }
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
