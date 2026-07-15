#include <stdint.h>

/* chips/fpga variant that denoises software/dvs_timesurface/main.c's grid:
   real event cameras produce spatially-isolated spurious events
   (background activity noise) alongside the spatially-clustered events
   real motion actually produces -- a moving edge lights up a neighborhood
   of nearby cells close together in time, while noise fires alone with no
   correlated neighbor. Same interrupt/FIFO wiring and 32x28 grid as
   dvs_timesurface (see that file's header for the grid geometry and why
   "now" is this firmware's own event counter, not a real timestamp), but
   isr_handler only counts an event as *signal* if at least one of its 4
   grid-adjacent neighbors (or the cell itself) was touched by ANY event --
   signal or not -- within the last CORRELATION_WINDOW=25 events.

   Two arrays, not one: last_touched[] records every event's raw touch
   unconditionally, which is what correlation checks against; signal_seen[]
   only ever updates for events that passed the filter, and is the only
   thing dumped to the host. Checking correlation against confirmed signal
   history alone (a single last_seen[] array, updated only when accepted)
   can't ever bootstrap -- the very first event of a genuine new motion
   region has no prior signal to correlate against, so it would always be
   rejected, meaning nothing downstream of it could ever be accepted
   either. Recording every raw touch regardless of the filter's own
   verdict is what lets a second nearby event, arriving shortly after,
   confirm the first one was real.

   CORRELATION_WINDOW=25 was picked empirically against
   chips/fpga/dvs_capture_20260714_151049.csv: smaller values (~10) reject
   enough of a genuine moving edge's own events to fragment it, larger
   values (~50+) start letting isolated noise back in as "correlated" by
   coincidence; 25 was the cleanest -- almost all scattered single-pixel
   noise gone, real motion region still coherent.

   Every DUMP_INTERVAL=64 batches, isr_handler writes `now` followed by all
   GRID_CELLS=896 cells of signal_seen[] (not last_touched[]) to the output
   FIFO -- a cell an isolated noise event touched but never got confirmed
   reads back as whatever it was before (0 if genuinely untouched by any
   confirmed event), never as that noise event's own timestamp. */

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

#define DUMP_INTERVAL 64        /* power of 2: batch_count & (DUMP_INTERVAL-1) tests "every 64th batch" */
#define CORRELATION_WINDOW 25   /* events; picked empirically -- see header */

static uint32_t last_touched[GRID_CELLS];
static uint32_t signal_seen[GRID_CELLS];
static uint32_t event_count;
static uint32_t batch_count;

static int is_recent(uint32_t last, uint32_t now) {
    return (last != 0) && ((now - last) <= CORRELATION_WINDOW);
}

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

        uint32_t correlated = is_recent(last_touched[cell], event_count);
        if (!correlated && col > 0)             correlated = is_recent(last_touched[cell - 1], event_count);
        if (!correlated && col < GRID_COLS - 1) correlated = is_recent(last_touched[cell + 1], event_count);
        if (!correlated && row > 0)             correlated = is_recent(last_touched[cell - GRID_COLS], event_count);
        if (!correlated && row < GRID_ROWS - 1) correlated = is_recent(last_touched[cell + GRID_COLS], event_count);

        last_touched[cell] = event_count;
        if (correlated) {
            signal_seen[cell] = event_count;
        }
    }

    batch_count++;
    if ((batch_count & (DUMP_INTERVAL - 1)) == 0) {
        *FIFO_OUT = event_count;
        for (uint32_t c = 0; c < GRID_CELLS; c++) {
            *FIFO_OUT = signal_seen[c];
        }
    }
}

void main(void) {
    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        last_touched[c] = 0;
        signal_seen[c] = 0;
    }
    event_count = 0;
    batch_count = 0;

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
