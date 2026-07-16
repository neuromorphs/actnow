#include <stdint.h>

/* chips/fpga variant that tracks a single moving object's real position,
   instead of software/dvs_motion/main.c's coarse 4x4-grid argmax (which
   can only report "which 32x32 block moved") or software/dvs_timesurface/
   main.c's per-cell recency map (which reports *when* every cell was last
   touched, not *where the object is*). Same interrupt/FIFO wiring: fifo_in
   fires event_id_0 once BATCH words land, isr_handler reads BATCH words --
   but here every event updates a running centroid estimate and a
   this-window bounding box, and only a status summary is written out
   every DUMP_INTERVAL batches, not a per-event echo.

   This core has no multiply/divide (see dvs_rotate/main.c's header), so
   the centroid can't be a literal sum-of-x / count. Instead it's an
   exponential moving average (EMA) -- a standard multiply-free "leaky
   integrator" estimator: each event nudges the running estimate a
   1/2^EMA_SHIFT fraction of the way toward its own (x,y), via a single
   subtract-then-shift-then-add:

       ema_x += (x - ema_x) >> EMA_SHIFT

   ema_x/ema_y are kept in FRAC-bit fixed point (values scaled up by
   1<<FRAC before the shift) so the >>EMA_SHIFT rounding doesn't quantize
   the estimate to a dead integer step and get stuck a pixel or two off --
   the fractional remainder that would otherwise be discarded stays live
   in the low FRAC bits and keeps accumulating. This is the same
   coordinate-decay idea dvs_motion's grid already uses (halve, then add),
   just applied to a running position estimate instead of a per-cell
   activity counter.

   Alongside the centroid, isr_handler tracks the plain min/max x and y of
   every event seen since the last dump -- a real bounding box of this
   window's activity, cheap (comparisons only) and exact (unlike the
   smoothed centroid). window_count (events since last dump) becomes the
   "locked" flag: fewer than LOCK_THRESHOLD events in a window means there
   wasn't enough activity to trust the box/centroid, so a consumer
   (e.g. a servo loop centering a camera on the tracked object, or a UI
   drawing a tracking box) knows to ignore a stale/empty reading instead
   of quietly acting on noise.

   Every DUMP_INTERVAL=64 batches (256 events), isr_handler writes two
   status words to the output FIFO, both plain byte-packed fields (each
   coordinate fits under 128, so one byte per field, MSB-first):

     word 0: (locked<<24) | (cx<<16) | (cy<<8) | count_capped
     word 1: (min_x<<24)  | (min_y<<16) | (max_x<<8) | max_y

   then resets the bounding box and window_count for the next window (the
   centroid EMA is *not* reset -- it's a continuously-running estimate, not
   a per-window one).

   Hot-pixel filtering: a real event-camera sensor can have a handful of
   defective pixels that fire spuriously at a high, roughly constant rate
   regardless of the scene -- unlike software/dvs_denoise/main.c's
   correlation filter (built for isolated single-shot background-activity
   noise), a *sustained* hot pixel actually passes that filter, because its
   own repeated firing correlates with its own immediately-prior firing.
   Left unfiltered here, a single hot pixel would blow win_min_x/max_x/etc.
   out to include its own fixed address every window and drag the EMA
   centroid toward it.

   isr_handler instead keeps two small per-cell arrays over the same 32x28
   4x4-pixel grid software/dvs_denoise/main.c already uses (CELL_SHIFT=2,
   GRID_CELLS=896): hot_last_seen[cell], the event index that cell was last
   touched at, and hot_streak[cell], how many consecutive times it's been
   *re*-touched within HOT_GAP_LIMIT events of its previous touch. Real
   motion sweeps across many different cells, so any single cell being
   retouched that quickly, that many times in a row, is a strong signal of
   a stuck pixel rather than a moving edge; once hot_streak[cell] reaches
   HOT_STREAK_THRESHOLD, further events landing in that cell are dropped
   before they reach the centroid/bbox/window_count -- filtered, not
   reported. HOT_GAP_LIMIT=4 and HOT_STREAK_THRESHOLD=20 are a starting
   heuristic (no capture with a known real hot pixel was available to tune
   against, unlike dvs_denoise's empirically-picked CORRELATION_WINDOW), so
   treat them as a reasonable default rather than a validated constant. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

#define FRAC      4   /* fractional bits kept in the EMA's fixed-point accumulator */
#define EMA_SHIFT 3   /* leaky-integrator rate: each event closes 1/8 of the gap to the centroid */

#define DUMP_INTERVAL  64   /* batches; power of 2 -- batch_count & (DUMP_INTERVAL-1) tests "every 64th" */
#define LOCK_THRESHOLD 32   /* events needed in a window to report "locked" instead of a stale/empty box */

/* Hot-pixel filter grid -- same 4x4-pixel cells as software/dvs_denoise/
   main.c and software/dvs_timesurface/main.c (CELL_SHIFT=2: 126>>2=31,
   112>>2=27, both within GRID_COLS=32/GRID_ROWS=28). See header for why
   this is per-cell rather than per-exact-pixel (cheaper, and a stuck
   pixel still dominates its own cell's retouch statistics far beyond
   anything real motion produces). */
#define CELL_SHIFT 2
#define GRID_COLS  32
#define GRID_ROWS  28
#define GRID_CELLS (GRID_COLS * GRID_ROWS)   /* = 896 */

#define HOT_GAP_LIMIT      4    /* events; a retouch this close (or closer) to the cell's last touch extends its streak */
#define HOT_STREAK_THRESHOLD 20 /* consecutive fast retouches before a cell's events get dropped as a hot pixel */

static int32_t ema_x_fp, ema_y_fp;             /* Q(FRAC) fixed-point running centroid */
static int32_t win_min_x, win_min_y, win_max_x, win_max_y;
static uint32_t window_count;
static uint32_t batch_count;
static uint32_t event_count;

static uint16_t hot_last_seen[GRID_CELLS];   /* event_count (mod 65536) this cell was last touched at; 0 = never */
static uint8_t hot_streak[GRID_CELLS];       /* consecutive fast retouches of this cell */

static void reset_window(void) {
    win_min_x = SX; win_max_x = -1;   /* max_x staying -1 after a dump means "no events this window" */
    win_min_y = SY; win_max_y = -1;
    window_count = 0;
}

/* isr_handler must not call wfi() -- see software/application/main.c's
   isr_handler comment for why. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    for (uint32_t i = 0; i < BATCH; i++) {
        int32_t x = (int32_t)(v[i] & 0x7F);
        int32_t y = (int32_t)((v[i] >> 7) & 0x7F);

        event_count++;

        uint32_t col = (uint32_t)x >> CELL_SHIFT;
        uint32_t row = (uint32_t)y >> CELL_SHIFT;
        uint32_t cell = (row << 5) | col;   /* row*GRID_COLS via shift -- GRID_COLS==32==1<<5 */

        uint16_t now16 = (uint16_t)event_count;
        uint16_t gap = (uint16_t)(now16 - hot_last_seen[cell]);
        if (hot_last_seen[cell] != 0 && gap <= HOT_GAP_LIMIT) {
            if (hot_streak[cell] < 255) hot_streak[cell]++;
        } else {
            hot_streak[cell] = 1;
        }
        hot_last_seen[cell] = now16;

        if (hot_streak[cell] >= HOT_STREAK_THRESHOLD) {
            continue;   /* stuck/hot pixel -- drop this event before it reaches the tracker */
        }

        ema_x_fp += ((x << FRAC) - ema_x_fp) >> EMA_SHIFT;
        ema_y_fp += ((y << FRAC) - ema_y_fp) >> EMA_SHIFT;

        if (x < win_min_x) win_min_x = x;
        if (x > win_max_x) win_max_x = x;
        if (y < win_min_y) win_min_y = y;
        if (y > win_max_y) win_max_y = y;
        window_count++;
    }

    batch_count++;
    if ((batch_count & (DUMP_INTERVAL - 1)) == 0) {
        uint32_t cx = (uint32_t)(ema_x_fp >> FRAC);
        uint32_t cy = (uint32_t)(ema_y_fp >> FRAC);
        uint32_t count_capped = (window_count > 255) ? 255u : window_count;
        uint32_t locked = (window_count >= LOCK_THRESHOLD) ? 1u : 0u;

        int32_t have_box = (win_max_x >= 0);
        uint32_t bx0 = have_box ? (uint32_t)win_min_x : 0u;
        uint32_t by0 = have_box ? (uint32_t)win_min_y : 0u;
        uint32_t bx1 = have_box ? (uint32_t)win_max_x : 0u;
        uint32_t by1 = have_box ? (uint32_t)win_max_y : 0u;

        *FIFO_OUT = (locked << 24) | (cx << 16) | (cy << 8) | count_capped;
        *FIFO_OUT = (bx0 << 24) | (by0 << 16) | (bx1 << 8) | by1;

        reset_window();
    }
}

void main(void) {
    ema_x_fp = (int32_t)((SX / 2) << FRAC);
    ema_y_fp = (int32_t)((SY / 2) << FRAC);
    reset_window();
    batch_count = 0;
    event_count = 0;

    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        hot_last_seen[c] = 0;
        hot_streak[c] = 0;
    }

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
