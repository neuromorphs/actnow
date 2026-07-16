#include <stdint.h>

/* "Micro-Event Black Holes" (dvs_blackhole) -- a chips/fpga demo app in the same
   shape as software/dvs_apophenia/main.c and software/dvs_caustics/main.c (fifo_in
   fires event_id_0 once BATCH words land; isr_handler reads them, updates a coarse
   TWO-EMA density grid, writes ONE status word, and returns -- it NEVER calls
   wfi(), see the epilogue comment on isr_handler).

   Idea (novel -- detects motion COLLAPSE, not activity): find regions where local
   event density was HIGH and then abruptly VANISHES -- an object stops moving or
   leaves the frame. These "micro black holes" are the INVERSE of an activity
   heatmap: a heatmap lights up where things are busy; this lights up where things
   *just went quiet after being busy*. The chip keeps, per coarse region, two leaky
   EMAs of the event density: a FAST one `f` (short memory) and a SLOW baseline `s`
   (long memory). Events bump the FAST EMA directly; the SLOW baseline TRACKS
   (chases) the fast EMA a little each tick, so it lags behind changes. While a
   region is active `f` rides high and `s` chases up toward it (staying just below),
   so collapse = s - f clamps to ~0. When activity STOPS, `f` decays quickly while
   the slow baseline `s` lags well behind, so the gap collapse = s - f opens up --
   that gap is the signature of a collapsing region.
   The chip streams out the strongest collapsing cell per batch; the HOST renders
   each as a dark imploding gravity well with a bright gravitational-lensing ring.
   The chip only ever emits {xq, yq, strength, flag}; all the well/lensing/animation
   happens on the computer (chips/fpga/dvs_blackhole_view.py + the dashboard
   renderer). Distinct from every prior app (grids/radial/creatures/caustics).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). Everything below is compares, shifts, adds, subs:
     - cell index         : xq = x>>3 (0..15), yq = y>>3 (0..13); the grid is
                            stored with a power-of-two stride (CELL_STRIDE=16) so
                            cell = (yq<<STRIDE_SHIFT)|xq is a shift, not a multiply.
     - fast EMA bump      : f += STEP (saturating at EMA_CAP), per event
     - fast EMA decay     : f -= f>>FAST_DECAY_SHIFT  (drops ~1/4 per tick: quick),
                            once every DECAY_INTERVAL batches
     - slow baseline chase: s moves a fraction of the way toward f each tick, in
                            EITHER direction (a compare picks the direction), and the
                            fraction is ASYMMETRIC -- fast up, slow down:
                              if f > s:  s += (f - s) >> CHASE_UP_SHIFT   (up fast)
                              else:      s -= (s - f) >> CHASE_DOWN_SHIFT (down slow)
                            so s builds a baseline quickly during activity but retains
                            it long after -- a slow-leaking high-water mark of f.
                            add/sub/shift only.
     - collapse metric    : collapse = s - f, clamped >=0 (a compare + sub)
     - strongest cell     : argmax over collapse by compare (no divide)

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_caustics / dvs_apophenia):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit ~microsecond timestamp, wraps)
     pol =  word        & 1
   (Several earlier apps read x/y/ts from the LOW bits -- the STALE layout; on the
   FPGA that reads the wrong bits. This app matches evt_pack.v + dvs_caustics. The
   chips/fpga mirror packs the same way.)

   The sensor frame is SX x SY = 126 x 112. Coarse REGION cells are 8 px wide
   (x>>3 -> 0..15, since 125>>3 = 15) by 8 px tall (y>>3 -> 0..13, since 111>>3 =
   13). So the LOGICAL grid is CELL_COLS=16 x CELL_ROWS=14 = 224 cells. We keep two
   uint8 density measures per cell (fast_ema[] + slow_ema[]), stored in
   CELL_STRIDE=16 x CELL_ROWS=14 arrays. Stride 16 is already a power of two AND
   equals the column count, so no padding is wasted: 16*14 = 224 bytes each, 448
   bytes of state total -- small.

   Why 8x8-px cells / 16x14: an 8-px region is coarse enough to average down the
   per-pixel noise of the 126x112 sensor, fine enough that a hand-sized object
   occupies a handful of cells so a "stop" is localised. Stride = a power of two
   keeps cell indexing a shift.

   -------------------------------------------------------------------------
   COLLAPSE METRIC (the whole point). Each event bumps the FAST EMA by STEP. Every
   DECAY_INTERVAL batches we run a tick over ALL cells: the fast EMA sheds
   f>>FAST_DECAY_SHIFT (a big fraction -- short memory), then the slow baseline s
   chases the fast EMA by a small CHASE-fraction (long memory: it lags f).
   Consequences (verified in the host --validate):
     - STEADY-ACTIVE cell   : events keep re-bumping f (STEP is large, so even a
                              couple of events per tick re-saturate it, and the gentle
                              1/8 fast decay means normal bursty inter-event gaps
                              don't drain it), so f rides near saturation; the slow
                              baseline s chases up but always stays BELOW f (it only
                              ever moves a fraction of the way), so collapse = s - f
                              clamps to ~0. Does NOT fire, and there is NO post-decay
                              transient (unlike symmetric bumps, where the fast EMA
                              would dip below the slow one right after each decay tick
                              and briefly look like a collapse). (Busy != black hole.)
     - STEADY-EMPTY cell    : no events ever, both f and s stay ~0 -> collapse ~ 0.
                              Does NOT fire. (Empty != black hole.)
     - COLLAPSING cell      : was active (s chased up to a high baseline) then
                              activity STOPS. Now f decays fast toward 0 while s lags
                              behind, so collapse = s - f grows large -> FIRES. This
                              is exactly "was busy and just went quiet".
   A cell is a black hole when collapse >= COLLAPSE_THRESHOLD. collapse is computed
   with a compare (clamp s<f -> 0) and a subtract -- no multiply anywhere.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). Two guards, both multiply-free:
     1. Coarse 8x8-px binning already averages down isolated hot pixels: a lone
        stray event barely nudges an EMA.
     2. BASELINE FLOOR guard (S_MIN). A cell can only be a black hole if its SLOW
        baseline s has itself climbed above S_MIN first -- i.e. the region was
        *genuinely, sustainedly active* at some point. Hot-pixel sparkle that fires
        a few scattered events never builds a slow baseline (the slow baseline only
        chases f a fraction per tick, so a handful of events can't lift it far), so
        it can never manufacture a large s-f gap. Without this floor, a cell whose f
        briefly
        spiked and then decayed could look like a collapse from noise alone; the
        floor requires real prior activity. A cell reported as a REAL black hole
        (flag=1) must satisfy BOTH collapse >= COLLAPSE_THRESHOLD AND s >= S_MIN.

   -------------------------------------------------------------------------
   Emission cadence: ONE status word per BATCH-sized batch of events (exactly like
   dvs_apophenia / dvs_caustics emit once per batch). We report the cell with the
   strongest CURRENT collapse (argmax over collapse across the grid). If that best
   cell clears both gates (collapse >= COLLAPSE_THRESHOLD AND slow s >= S_MIN) we set
   flag=1 (a real black hole); otherwise we still emit the best cell (so the stream
   never stalls and the host can paint a faint well) with flag=0.

   The STRENGTH field is the collapse magnitude quantized by STRENGTH_SHIFT
   (collapse >> STRENGTH_SHIFT, capped to 5 bits) so the host can scale how deep /
   dark the well is drawn.

   Output word layout (low 15 bits used):
     bits[3:0]    = xq        (0..15, 4 bits) -- coarse region X, x>>3
     bits[7:4]    = yq        (0..13, 4 bits) -- coarse region Y, y>>3
     bits[12:8]   = strength  (0..31, 5 bits) -- collapse >> STRENGTH_SHIFT (well depth)
     bit [13]     = flag      (1 bit)         -- 1 = real black hole (collapse >=
                                                 COLLAPSE_THRESHOLD AND slow s >=
                                                 S_MIN), 0 = faint (below a gate)
   Host unpacks these fields; see dvs_blackhole_view.py's unpack_status(). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v / dvs_caustics). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Coarse region grid. 8-px columns (x>>3 -> 0..15) by 8-px rows (y>>3 -> 0..13).
   CELL_STRIDE is a power of two so the cell index is a shift, not a multiply; here
   stride == COLS == 16 so nothing is wasted. XQ_SHIFT/YQ_SHIFT and the col/row
   counts must move together: a cell index must stay < CELL_CELLS or it walks off
   the EMA arrays. */
#define XQ_SHIFT 3                               /* 8-px columns: 125>>3 = 15 -> cols 0..15 */
#define YQ_SHIFT 3                               /* 8-px rows:    111>>3 = 13 -> rows 0..13 */
#define CELL_COLS 16                             /* logical columns (0..15) */
#define CELL_ROWS 14                             /* logical rows    (0..13) */
#define STRIDE_SHIFT 4                           /* row*CELL_STRIDE via shift: STRIDE==16==1<<4 */
#define CELL_STRIDE (1 << STRIDE_SHIFT)          /* = 16 (power-of-two stride for shift indexing) */
#define CELL_CELLS (CELL_STRIDE * CELL_ROWS)     /* = 224 cells (uint8 per EMA -> 448 B state) */

/* Two-EMA parameters. All shifts+adds+subs+compares, no multiply.
   STEP           : density added to the FAST EMA per event (saturating). Large (128)
                    so a region touched by even ~2 events per tick re-saturates its
                    fast EMA -- a genuinely active region's fast EMA then never dips
                    just because events arrive in bursts (only a real STOP, several
                    consecutive quiet ticks, can drain it). This is what stops a
                    bursty steady-active region from faking a transient collapse.
   EMA_CAP        : 8-bit saturation for the leaky counters.
   FAST_DECAY_SHIFT: fast EMA leak per tick, f -= f>>3 (sheds ~1/8: SHORT memory --
                    gentle enough that normal inter-burst gaps in an active region
                    don't drain it, but a sustained absence does).
   The slow baseline chases the fast EMA ASYMMETRICALLY -- fast when climbing, slow
   when falling -- so it builds a baseline quickly during activity but retains it for
   a long time after activity stops (a long-memory "high-water mark" that leaks
   slowly). This is what lets a collapse stay visible for many batches after motion
   ends, while still tracking up promptly so a steady-active cell keeps s just below f.
   CHASE_UP_SHIFT  : while f > s, s += (f-s)>>1 (chase UP fast: ~half the gap/tick).
   CHASE_DOWN_SHIFT: while f < s, s -= (s-f)>>5 (chase DOWN slow: ~1/32 the gap/tick,
                     so the baseline lingers long after f has decayed to 0). Because
                     s only ever moves a fraction of the way toward f, while a region
                     is active s stays BELOW the re-bumped f (collapse clamps to 0, no
                     transient); once f decays away, s lags behind and the s-f
                     collapse gap opens and persists. */
#define STEP             128
#define EMA_CAP          255
#define FAST_DECAY_SHIFT 3
#define CHASE_UP_SHIFT   1
#define CHASE_DOWN_SHIFT 5

#ifndef DECAY_INTERVAL
#define DECAY_INTERVAL 8  /* run the fast-decay + slow-chase tick every this-many batches */
#endif

/* A cell fires as a REAL black hole only when its collapse (s-f) crosses this AND
   its slow baseline cleared S_MIN. Tunable at build time (the dashboard could pass
   -DCOLLAPSE_THRESHOLD=N). */
#ifndef COLLAPSE_THRESHOLD
#define COLLAPSE_THRESHOLD 48
#endif

/* Baseline floor: the slow EMA must have climbed above this before a cell may be a
   black hole -- so a region that never built real, sustained activity (hot-pixel
   sparkle) can never fake a collapse. See the NOISE STRATEGY note. */
#ifndef S_MIN
#define S_MIN 64
#endif

/* collapse >> STRENGTH_SHIFT -> 0..31 (5-bit strength, well depth on the host). */
#define STRENGTH_SHIFT 3

/* Two leaky density measures per cell, in .bss (zeroed by crt0.S) so they start
   cold. fast_ema: short-memory density bumped by events; slow_ema: long-memory
   baseline that chases fast_ema. */
static uint8_t  fast_ema[CELL_CELLS];   /* short-memory density */
static uint8_t  slow_ema[CELL_CELLS];   /* long-memory baseline (chases fast_ema) */
static uint32_t batch_count;            /* how many batches -> drives decay cadence */

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

    /* Bump the FAST EMA for every event's coarse region (saturating add). */
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x = (v[i] >> X_SHIFT) & 0x7F;
        uint32_t y = (v[i] >> Y_SHIFT) & 0x7F;

        uint32_t xq   = x >> XQ_SHIFT;                     /* 0..15 */
        uint32_t yq   = y >> YQ_SHIFT;                     /* 0..13 */
        uint32_t cell = (yq << STRIDE_SHIFT) | xq;         /* row*STRIDE via shift */

        uint32_t fu = fast_ema[cell] + STEP;
        fast_ema[cell] = (uint8_t)((fu > EMA_CAP) ? EMA_CAP : fu);
    }

    /* Periodic tick over the whole grid: the fast EMA sheds a big fraction (short
       memory), then the slow baseline chases the fast EMA by a small fraction (long
       memory: it lags). While active, f is re-bumped high and s stays below it; once
       activity stops, f decays away and s lags behind -> the s-f collapse gap opens.
       Shift/add/sub/compare only, no multiply. */
    batch_count++;
    if (batch_count >= DECAY_INTERVAL) {
        batch_count = 0;
        for (uint32_t c = 0; c < CELL_CELLS; c++) {
            uint32_t f = fast_ema[c];
            f = f - (f >> FAST_DECAY_SHIFT);
            fast_ema[c] = (uint8_t)f;

            uint32_t s = slow_ema[c];
            if (f > s) s = s + ((f - s) >> CHASE_UP_SHIFT);     /* chase up   (fast) */
            else       s = s - ((s - f) >> CHASE_DOWN_SHIFT);   /* chase down (slow) */
            slow_ema[c] = (uint8_t)s;
        }
    }

    /* Find the cell with the strongest CURRENT collapse (argmax over s-f). collapse
       is clamped to >=0 with a compare (a busy cell has f>=s -> collapse 0). */
    uint32_t best_cell     = 0;
    uint32_t best_collapse = 0;
    uint32_t best_slow     = 0;
    for (uint32_t c = 0; c < CELL_CELLS; c++) {
        uint32_t s = slow_ema[c];
        uint32_t f = fast_ema[c];
        uint32_t collapse = (s > f) ? (s - f) : 0u;        /* clamp >=0 */
        if (collapse > best_collapse) {
            best_collapse = collapse;
            best_cell     = c;
            best_slow     = s;
        }
    }

    /* A REAL black hole (flag=1) needs BOTH a large collapse AND a slow baseline
       that actually cleared the floor (so noise can't fake it). Otherwise still
       emit the best cell (flag=0) so the stream never stalls. */
    uint32_t flag = (best_collapse >= COLLAPSE_THRESHOLD && best_slow >= S_MIN) ? 1u : 0u;

    /* strength (0..31): collapse magnitude quantized by STRENGTH_SHIFT (well depth). */
    uint32_t strength = best_collapse >> STRENGTH_SHIFT;
    if (strength > 31u) strength = 31u;

    uint32_t xq = best_cell & (CELL_STRIDE - 1);   /* low 4 bits: xq (0..15) */
    uint32_t yq = best_cell >> STRIDE_SHIFT;       /* high bits:  yq (0..13) */

    /* bits[3:0]=xq, bits[7:4]=yq, bits[12:8]=strength, bit[13]=flag. */
    *FIFO_OUT = (flag << 13) | (strength << 8) | (yq << 4) | xq;
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so fast_ema/slow_ema/batch_count start cold. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
