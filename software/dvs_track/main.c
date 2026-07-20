#include <stdint.h>

/* chips/fpga variant that tracks a single moving object's real position,
   instead of software/dvs_motion/main.c's coarse 4x4-grid argmax or
   software/dvs_timesurface/main.c's per-cell recency map. Same interrupt/FIFO
   wiring: fifo_in fires event_id_0 once BATCH words land, isr_handler reads
   BATCH words -- and every DUMP_INTERVAL batches writes a status summary
   (centre + bounding box), not a per-event echo.

   Two tracking algorithms live here, chosen at build time by TRACK_ALGO so the
   dashboard's algorithm dropdown can switch between them (the output ABI is
   identical either way):

     TRACK_ALGO=1 (default) -- median + MAD. This core has no hardware multiply,
       divide, or sqrt (see dvs_rotate/main.c's header), so isr_handler keeps a
       ring buffer of the last TRACK_N surviving events' (x,y) and, at each dump,
       reports the *median* x and y as the centre (robust to stray events,
       computed multiply/divide-free by a counting histogram whose cumulative
       count crossing fill/2 is the median) and a box = median +/- BOX_K * MAD,
       where MAD (median absolute deviation, the median of |x-median_x|) is the
       standard robust, multiply-free stand-in for standard deviation
       (std ~= 1.4826*MAD). TRACK_N (default 256, -DTRACK_N=N) is the window
       length; BOX_K=2 ~= 1.35 sigma, a tight box around the object core.

     TRACK_ALGO=0 -- EMA centroid + min/max box. The centre is a multiply-free
       exponential moving average, ema += (x - ema) >> EMA_SHIFT, kept in
       FRAC-bit fixed point; the box is the plain min/max x/y of the surviving
       events this window. Cheaper, but the min/max box is not robust: one stray
       event stretches it, so on a busy scene it saturates toward the whole frame.

   Every DUMP_INTERVAL=64 batches (256 events) isr_handler writes two byte-packed
   status words (each coordinate fits under 128, one byte per field, MSB-first):

     word 0: (locked<<24) | (cx<<16) | (cy<<8) | count_capped
     word 1: (min_x<<24)  | (min_y<<16) | (max_x<<8) | max_y

   window_count (surviving events since the last dump) drives `locked`
   (>= LOCK_THRESHOLD) and the `count` byte, so a consumer can ignore a
   stale/empty reading. For TRACK_ALGO=1 the sliding window is not reset at a
   dump (it keeps sliding); for TRACK_ALGO=0 the min/max box is reset each dump
   and the EMA runs continuously.

   Noise filtering (spatio-temporal correlation), shared by both algorithms: a
   real event-camera sensor produces background-activity noise -- spatially
   isolated spurious events -- and hot pixels. isr_handler rejects them with a
   spatio-temporal correlation filter after jAER's SpatioTemporalCorrelationFilter
   (Guo & Delbruck, T-PAMI 2022) and this project's software/dvs_denoise/main.c.
   Over the same 32x28 4x4-pixel grid (CELL_SHIFT=2, GRID_CELLS=896),
   last_touched[cell] holds the event index a cell was last touched at. An event
   survives only if at least CORR_MIN of the eight cells in its 3x3 neighbourhood
   -- *excluding the cell itself* -- were touched within the last CORR_WINDOW
   events. Excluding self rejects hot pixels too: a stuck pixel firing into a
   quiet neighbourhood finds no support and is dropped. last_touched is updated
   for every event, even dropped ones, which lets a genuine new region bootstrap.
   "now" is isr_handler's own event counter, not a hardware timestamp (this rig
   has no per-event wall-clock; see dvs_timesurface's header). CORR_WINDOW=30 and
   CORR_MIN=2 (jAER's default) are overridable (-DCORR_WINDOW=N / -DCORR_MIN=N);
   CORR_MIN=0 disables the filter.

   Tracking gate, shared by both algorithms: once a window has locked
   (gate_active = the previous window's `locked`), an event is admitted only if
   it is within GATE_RADIUS (Chebyshev distance, multiply-free) of the current
   centre -- rejecting measurements far from the predicted state. The gate opens
   wide again after any window that fails to lock, so the tracker can reacquire an
   object that appears far from the centre-of-frame seed. -DGATE_RADIUS=N. */

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
   x in bits [30:24], y in bits [23:17], timestamp in [16:1], polarity in [0]. */
#define X_SHIFT 24
#define Y_SHIFT 17

#define DUMP_INTERVAL  64   /* batches; power of 2 -- batch_count & (DUMP_INTERVAL-1) tests "every 64th" */
#define LOCK_THRESHOLD 32   /* events needed in a window to report "locked" instead of a stale/empty box */

/* Which tracker: 1 = median + MAD (robust, default), 0 = EMA centroid + min/max
   box (old). Overridable via -DTRACK_ALGO=N from the dashboard's algorithm
   dropdown. */
#ifndef TRACK_ALGO
#define TRACK_ALGO 1
#endif

#if TRACK_ALGO == 0
#define FRAC      4   /* fractional bits kept in the EMA's fixed-point accumulator */
#define EMA_SHIFT 3   /* leaky-integrator rate: each event closes 1/8 of the gap to the centroid */
#else
/* Sliding window of the last TRACK_N surviving events for the median/MAD. */
#ifndef TRACK_N
#define TRACK_N 256
#endif
#define BOX_K 2   /* box half-width = BOX_K * MAD (constant -> compiler strength-reduces the multiply) */
#endif

/* Correlation-filter grid -- same 4x4-pixel cells as software/dvs_denoise/main.c
   (CELL_SHIFT=2: 126>>2=31, 112>>2=27, within GRID_COLS=32/GRID_ROWS=28). */
#define CELL_SHIFT 2
#define GRID_COLS  32
#define GRID_ROWS  28
#define GRID_CELLS (GRID_COLS * GRID_ROWS)   /* = 896 */

/* Spatio-temporal correlation noise filter -- see header. Both overridable. */
#ifndef CORR_WINDOW
#define CORR_WINDOW 30   /* events; temporal window a neighbour must have fired within to count as recent */
#endif
#ifndef CORR_MIN
#define CORR_MIN 2       /* of 8 neighbours that must be recent for an event to survive; 0 disables the filter */
#endif

/* Chebyshev distance from the current centre an event must stay within, once
   locked. -DGATE_RADIUS=N from the dashboard's tracking-radius control. */
#ifndef GATE_RADIUS
#define GATE_RADIUS 50
#endif

static uint32_t window_count;               /* surviving events since the last dump (drives locked/count) */
static uint32_t batch_count;
static uint32_t gate_active;                /* set from the previous window's `locked` -- see header */

static uint32_t last_touched[GRID_CELLS];   /* event index each cell was last touched at; 0 = never */
static uint32_t event_count;                /* "now": total events seen -- the time proxy (see header) */

#if TRACK_ALGO == 0
static int32_t ema_x_fp, ema_y_fp;          /* Q(FRAC) fixed-point running centroid */
static int32_t win_min_x, win_min_y, win_max_x, win_max_y;
#else
static int32_t med_x, med_y;                /* current median centre (integer sensor coords) */
static uint8_t ring_x[TRACK_N], ring_y[TRACK_N];   /* sliding window of the last TRACK_N surviving events */
static uint32_t ring_pos;                   /* next write slot in the ring */
static uint32_t ring_fill;                  /* events in the ring so far (caps at TRACK_N) */
static uint16_t hx[SX], hy[SY];             /* counting histograms, reused for value- and deviation-medians */
#endif

static int is_recent(uint32_t last, uint32_t now) {
    return (last != 0) && ((now - last) <= CORR_WINDOW);
}

#if TRACK_ALGO == 1
/* Median of a counting histogram h[len] holding `fill` samples: the bin at which
   the cumulative count first passes fill/2. Multiply/divide-free. */
static int32_t hist_median(uint16_t *h, int32_t len, uint32_t fill) {
    uint32_t half = fill >> 1, acc = 0;
    for (int32_t v = 0; v < len; v++) {
        acc += h[v];
        if (acc > half) return v;
    }
    return 0;
}
#endif

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
#if TRACK_ALGO == 0
            int32_t cx_now = ema_x_fp >> FRAC, cy_now = ema_y_fp >> FRAC;
#else
            int32_t cx_now = med_x, cy_now = med_y;
#endif
            int32_t gdx = x - cx_now; if (gdx < 0) gdx = -gdx;
            int32_t gdy = y - cy_now; if (gdy < 0) gdy = -gdy;
            int32_t gdist = (gdx > gdy) ? gdx : gdy;
            if (gdist > GATE_RADIUS) {
                continue;   /* too far from the current track -- not the tracked object */
            }
        }

#if TRACK_ALGO == 0
        ema_x_fp += ((x << FRAC) - ema_x_fp) >> EMA_SHIFT;
        ema_y_fp += ((y << FRAC) - ema_y_fp) >> EMA_SHIFT;
        if (x < win_min_x) win_min_x = x;
        if (x > win_max_x) win_max_x = x;
        if (y < win_min_y) win_min_y = y;
        if (y > win_max_y) win_max_y = y;
#else
        /* push into the sliding window of the last TRACK_N surviving events */
        ring_x[ring_pos] = (uint8_t)x;
        ring_y[ring_pos] = (uint8_t)y;
        ring_pos++;
        if (ring_pos >= TRACK_N) ring_pos = 0;   /* wrap without a modulo */
        if (ring_fill < TRACK_N) ring_fill++;
#endif
        window_count++;
    }

    batch_count++;
    if ((batch_count & (DUMP_INTERVAL - 1)) == 0) {
        uint32_t cx, cy;
        int32_t bx0, by0, bx1, by1;

#if TRACK_ALGO == 0
        cx = (uint32_t)(ema_x_fp >> FRAC);
        cy = (uint32_t)(ema_y_fp >> FRAC);
        int32_t have_box = (win_max_x >= 0);
        bx0 = have_box ? win_min_x : 0;
        by0 = have_box ? win_min_y : 0;
        bx1 = have_box ? win_max_x : 0;
        by1 = have_box ? win_max_y : 0;
#else
        int32_t mad_x = 0, mad_y = 0;
        if (ring_fill > 0) {
            for (int32_t b = 0; b < SX; b++) hx[b] = 0;
            for (uint32_t i = 0; i < ring_fill; i++) hx[ring_x[i]]++;
            med_x = hist_median(hx, SX, ring_fill);
            for (int32_t b = 0; b < SX; b++) hx[b] = 0;
            for (uint32_t i = 0; i < ring_fill; i++) {
                int32_t d = (int32_t)ring_x[i] - med_x; if (d < 0) d = -d;
                hx[d]++;
            }
            mad_x = hist_median(hx, SX, ring_fill);

            for (int32_t b = 0; b < SY; b++) hy[b] = 0;
            for (uint32_t i = 0; i < ring_fill; i++) hy[ring_y[i]]++;
            med_y = hist_median(hy, SY, ring_fill);
            for (int32_t b = 0; b < SY; b++) hy[b] = 0;
            for (uint32_t i = 0; i < ring_fill; i++) {
                int32_t d = (int32_t)ring_y[i] - med_y; if (d < 0) d = -d;
                hy[d]++;
            }
            mad_y = hist_median(hy, SY, ring_fill);
        }
        cx = (uint32_t)med_x;
        cy = (uint32_t)med_y;
        int32_t hxw = BOX_K * mad_x, hyw = BOX_K * mad_y;
        bx0 = med_x - hxw; if (bx0 < 0) bx0 = 0;
        bx1 = med_x + hxw; if (bx1 > SX - 1) bx1 = SX - 1;
        by0 = med_y - hyw; if (by0 < 0) by0 = 0;
        by1 = med_y + hyw; if (by1 > SY - 1) by1 = SY - 1;
#endif

        uint32_t count_capped = (window_count > 255) ? 255u : window_count;
        uint32_t locked = (window_count >= LOCK_THRESHOLD) ? 1u : 0u;

        *FIFO_OUT = (locked << 24) | (cx << 16) | (cy << 8) | count_capped;
        *FIFO_OUT = ((uint32_t)bx0 << 24) | ((uint32_t)by0 << 16) |
                    ((uint32_t)bx1 << 8) | (uint32_t)by1;

        gate_active = locked;
#if TRACK_ALGO == 0
        win_min_x = SX; win_max_x = -1;   /* reset the per-window min/max box */
        win_min_y = SY; win_max_y = -1;
#endif
        window_count = 0;   /* per-window count resets; the median window keeps sliding */
    }
}

void main(void) {
    window_count = 0;
    batch_count = 0;
    event_count = 0;
    gate_active = 0;   /* wide open on a cold start -- no track yet to gate around */

#if TRACK_ALGO == 0
    ema_x_fp = (int32_t)((SX / 2) << FRAC);   /* centre-of-frame seed */
    ema_y_fp = (int32_t)((SY / 2) << FRAC);
    win_min_x = SX; win_max_x = -1;
    win_min_y = SY; win_max_y = -1;
#else
    med_x = SX / 2;   /* centre-of-frame seed until the first window locks */
    med_y = SY / 2;
    ring_pos = 0;
    ring_fill = 0;
#endif

    for (uint32_t c = 0; c < GRID_CELLS; c++) {
        last_touched[c] = 0;
    }

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
