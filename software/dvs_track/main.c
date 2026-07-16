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

   Noise filtering (spatio-temporal correlation): a real event-camera sensor
   produces background-activity noise -- spatially isolated spurious events
   scattered across the array -- and can have hot pixels that fire at a high
   rate regardless of the scene. Left unfiltered, that scattered activity
   blows win_min/max out toward the frame edges every window and drags the EMA
   centroid around. isr_handler rejects it with a spatio-temporal correlation
   filter, after jAER's SpatioTemporalCorrelationFilter (Guo & Delbruck,
   T-PAMI 2022) and this project's own software/dvs_denoise/main.c: a real
   moving edge lights up a spatial neighbourhood of cells close together in
   time, while noise fires alone with no correlated neighbour.

   Over the same 32x28 4x4-pixel grid software/dvs_denoise/main.c uses
   (CELL_SHIFT=2, GRID_CELLS=896), last_touched[cell] holds the event index
   that cell was last touched at. An event survives only if at least CORR_MIN
   of the eight cells in its 3x3 grid neighbourhood -- *excluding the cell
   itself* -- were touched within the last CORR_WINDOW events. Excluding self
   is what lets this reject hot pixels too, where dvs_denoise (which counts the
   cell itself) cannot: a stuck pixel re-firing into a quiet neighbourhood
   finds no support and is dropped, however fast it fires. last_touched is
   updated for every event, even dropped ones -- that unconditional record is
   what lets a genuine new region bootstrap, since the first event of a fresh
   edge seeds its cell for a second, nearby event arriving moments later to
   correlate against (same reasoning as dvs_denoise's header).

   "now" is isr_handler's own event counter, not a hardware timestamp -- the
   same event-count-as-time proxy dvs_denoise and dvs_timesurface use (this
   rig has no per-event wall-clock; see dvs_timesurface's header). CORR_WINDOW
   =30 events and CORR_MIN=2 correlated neighbours (jAER's own default) are
   reasonable starting points, both overridable at build time
   (-DCORR_WINDOW=N / -DCORR_MIN=N) so the dashboard's correlation control can
   retune the filter without editing this file; CORR_MIN=0 disables it.

   Tracking gate: without it, win_min/max is a literal min/max over every
   surviving event in the window, and on a real busy scene that saturates
   to nearly the whole SXxSY frame almost every window -- correct as
   defined, but useless as "where is the object" (verified against a real
   173k-event handheld recording: bounding boxes like (30,0)-(125,71) or
   (4,52)-(125,111), i.e. most of the sensor, every window). isr_handler
   now only lets an event feed the centroid/bbox/window_count if it's
   within GATE_RADIUS (Chebyshev distance, multiply-free like dvs_rotate's
   rotation) of the *current* ema_x/ema_y -- a standard tracking-gate
   technique: reject measurements far from the predicted state as
   "probably not the tracked object" rather than folding every scattered
   event in the frame into one box.

   The gate only activates once a window has actually locked onto
   something (gate_active is set to the previous window's `locked` value
   at each dump) -- if it stayed on unconditionally from a cold start, an
   object that first appears far from the initial center-of-frame seed
   would have every one of its events rejected as "too far," and the
   estimate would never move to find it. So the gate opens back up wide
   (accepts everything) any time the previous window failed to lock,
   letting the tracker search the whole frame again to reacquire, and
   narrows back down once it's found something worth trusting. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI. The harness packs camera events exactly as evt_pack.v does
   on real hardware -- and as software/application/main.c decodes them:
   x in bits [30:24], y in bits [23:17], timestamp in [16:1], polarity in [0].
   Reading x/y from the low bits instead (as an earlier revision did) happens to
   match the chips/fpga sim's direct fifo_push captures, but on the FPGA it reads
   the timestamp/polarity bits -- so the centroid and box track noise, not the
   object. The sim event sources are packed this same way; see the e2e tests. */
#define X_SHIFT 24
#define Y_SHIFT 17

#define FRAC      4   /* fractional bits kept in the EMA's fixed-point accumulator */
#define EMA_SHIFT 3   /* leaky-integrator rate: each event closes 1/8 of the gap to the centroid */

#define DUMP_INTERVAL  64   /* batches; power of 2 -- batch_count & (DUMP_INTERVAL-1) tests "every 64th" */
#define LOCK_THRESHOLD 32   /* events needed in a window to report "locked" instead of a stale/empty box */

/* Correlation-filter grid -- same 4x4-pixel cells as software/dvs_denoise/
   main.c and software/dvs_timesurface/main.c (CELL_SHIFT=2: 126>>2=31,
   112>>2=27, both within GRID_COLS=32/GRID_ROWS=28). */
#define CELL_SHIFT 2
#define GRID_COLS  32
#define GRID_ROWS  28
#define GRID_CELLS (GRID_COLS * GRID_ROWS)   /* = 896 */

/* Spatio-temporal correlation noise filter -- see header. Both overridable at
   build time so the dashboard's correlation control can retune them live. */
#ifndef CORR_WINDOW
#define CORR_WINDOW 30   /* events; temporal window a neighbour must have fired within to count as recent */
#endif
#ifndef CORR_MIN
#define CORR_MIN 2       /* of 8 neighbours that must be recent for an event to survive; 0 disables the filter */
#endif

/* Chebyshev distance from the current centroid an event must stay within, once
   locked. Overridable at build time (-DGATE_RADIUS=N), which is how the
   dashboard's tracking-radius control retunes the gate without editing this file. */
#ifndef GATE_RADIUS
#define GATE_RADIUS 50
#endif

static int32_t ema_x_fp, ema_y_fp;             /* Q(FRAC) fixed-point running centroid */
static int32_t win_min_x, win_min_y, win_max_x, win_max_y;
static uint32_t window_count;
static uint32_t batch_count;
static uint32_t gate_active;   /* set from the previous window's `locked` -- see header */

static uint32_t last_touched[GRID_CELLS];   /* event index each cell was last touched at; 0 = never */
static uint32_t event_count;                /* "now": total events seen -- the time proxy (see header) */

static int is_recent(uint32_t last, uint32_t now) {
    return (last != 0) && ((now - last) <= CORR_WINDOW);
}

static void reset_window(void) {
    win_min_x = SX; win_max_x = -1;   /* max_x staying -1 after a dump means "no events this window" */
    win_min_y = SY; win_max_y = -1;
    window_count = 0;
    /* last_touched is a rolling temporal record, not per-window -- not reset here. */
}

/* isr_handler must not call wfi() -- see software/application/main.c's
   isr_handler comment for why. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    for (uint32_t i = 0; i < BATCH; i++) {
        int32_t x = (int32_t)((v[i] >> X_SHIFT) & 0x7F);
        int32_t y = (int32_t)((v[i] >> Y_SHIFT) & 0x7F);

        uint32_t col = (uint32_t)x >> CELL_SHIFT;
        uint32_t row = (uint32_t)y >> CELL_SHIFT;
        uint32_t cell = (row << 5) | col;   /* row*GRID_COLS via shift -- GRID_COLS==32==1<<5 */

        event_count++;

        /* Spatio-temporal correlation filter: count recent touches in the 3x3
           cell neighbourhood, excluding the cell itself, and drop the event if
           too few neighbours support it (background noise or a hot pixel). */
        uint32_t ncorr = 0;
        int has_l = col > 0, has_r = col < GRID_COLS - 1;
        int has_u = row > 0, has_d = row < GRID_ROWS - 1;
        if (has_l)          ncorr += is_recent(last_touched[cell - 1], event_count);
        if (has_r)          ncorr += is_recent(last_touched[cell + 1], event_count);
        if (has_u)          ncorr += is_recent(last_touched[cell - GRID_COLS], event_count);
        if (has_d)          ncorr += is_recent(last_touched[cell + GRID_COLS], event_count);
        if (has_l && has_u) ncorr += is_recent(last_touched[cell - GRID_COLS - 1], event_count);
        if (has_r && has_u) ncorr += is_recent(last_touched[cell - GRID_COLS + 1], event_count);
        if (has_l && has_d) ncorr += is_recent(last_touched[cell + GRID_COLS - 1], event_count);
        if (has_r && has_d) ncorr += is_recent(last_touched[cell + GRID_COLS + 1], event_count);

        last_touched[cell] = event_count;   /* record every event, dropped or not -- bootstraps new regions */

        if (ncorr < CORR_MIN) {
            continue;   /* uncorrelated -- background noise or a hot pixel */
        }

        if (gate_active) {
            int32_t cx_now = ema_x_fp >> FRAC;
            int32_t cy_now = ema_y_fp >> FRAC;
            int32_t gdx = x - cx_now; if (gdx < 0) gdx = -gdx;
            int32_t gdy = y - cy_now; if (gdy < 0) gdy = -gdy;
            int32_t gdist = (gdx > gdy) ? gdx : gdy;
            if (gdist > GATE_RADIUS) {
                continue;   /* too far from the current track -- not the tracked object */
            }
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

        gate_active = locked;
        reset_window();
    }
}

void main(void) {
    ema_x_fp = (int32_t)((SX / 2) << FRAC);
    ema_y_fp = (int32_t)((SY / 2) << FRAC);
    reset_window();
    batch_count = 0;
    event_count = 0;
    gate_active = 0;   /* wide open on a cold start -- no track yet to gate around */

    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        last_touched[c] = 0;
    }

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
