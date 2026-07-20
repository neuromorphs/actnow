#include <stdint.h>

/* chips/fpga variant that reports a coarse moving-object location instead
   of a per-event transform. Same interrupt/FIFO wiring as
   software/application and software/dvs_rotate, but isr_handler maintains
   a decaying activity grid and emits one status word per batch (not one
   per event) -- the signal of interest is the hottest grid cell after a
   batch, not any single event.

   The sensor frame (SX x SY) is divided into GRID_COLS x GRID_ROWS cells
   of CELL_SIZE=1<<CELL_SHIFT pixels. This core has no multiply/divide, so
   the cell size is a power of 2: col = x >> CELL_SHIFT, row = y >>
   CELL_SHIFT, cell = (row << 2) | col (row*GRID_COLS via shift since
   GRID_COLS==4==1<<2). CELL_SHIFT and the grid dimensions must be picked
   together so the cells cover the whole SXxSY frame -- otherwise col/row
   can exceed the grid and index off the end of grid[] below.

   Each cell is an 8-bit saturating counter. Every batch: halve every cell
   (exponential decay, so activity fades over a few batches instead of
   latching forever), then add STEP to whichever cell(s) this batch's
   events land in, capped at CAP. Argmax over the grid gives the hottest
   cell; if its value clears THRESHOLD, the motion flag is set. Output
   word: bit14=motion, bits[13:6]=hottest value, bits[5:3]=row,
   bits[2:0]=col. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

#define CELL_SHIFT 5                          /* 32x32-pixel cells: 126>>5=3 and 112>>5=3, so
                                                   col/row land in 0..3, matching GRID_COLS/ROWS=4 */
#define GRID_COLS  4
#define GRID_ROWS  4
#define GRID_CELLS (GRID_COLS * GRID_ROWS)     /* = 16 */

#define STEP      32   /* activity added per event landing in a cell */
#define CAP       255  /* 8-bit saturation, matches the output word's 8-bit activity field */
#define THRESHOLD 96   /* hottest-cell activity needed to raise the motion flag */

static uint8_t grid[GRID_CELLS];

/* isr_handler must not call wfi() -- see software/application/main.c's
   isr_handler comment for why. */
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
        uint32_t cell = (row << 2) | col;   /* row*GRID_COLS via shift -- GRID_COLS==4==1<<2 */

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

    uint32_t best_col = best_cell & 0x3;   /* GRID_COLS==4 -> 2-bit column */
    uint32_t best_row = best_cell >> 2;
    uint32_t motion = (best_val >= THRESHOLD) ? 1u : 0u;

    /* row/col fields are 3 bits wide (bits[5:3]/[2:0]) to match
       dvs_motion_view.py's unpack_status. */
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
