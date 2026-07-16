#include <stdint.h>

/* "The Flinch" (dvs_flinch) -- a chips/fpga demo app in the same shape as
   software/dvs_blackhole/main.c (fifo_in fires event_id_0 once BATCH words land;
   isr_handler reads them, updates a coarse cell grid + a running spatial median,
   scores LOOMING at each window boundary, and writes ONE status word; it NEVER
   calls wfi(), see the epilogue comment on isr_handler).

   Idea (a real biological looming detector, novel among these apps): a giant eye
   on the host ignores waving / walking / panning, but FLINCHES when something
   LUNGES at the camera. It runs the locust LGMD looming-detector principle:
   objects approaching the sensor cause RADIAL EXPANSION of the event field (edges
   sweep OUTWARD from a focus of expansion); translation (a pan / a hand waving
   past) moves the whole field sideways WITHOUT net expansion. The chip measures
   that net radial expansion and, when it builds past a threshold, latches a FLINCH.
   The chip only ever emits {flinch, level, cx, cy}; all the eye / pupil-dilation /
   blink / screen-shake happens on the computer (chips/fpga/dvs_flinch_view.py +
   the dashboard renderer). Distinct from every prior app (grids/radial/creatures/
   caustics/black-holes).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). Everything below is compares, shifts, adds, subs:
     - cell index         : xq = x>>3 (0..15), yq = y>>3 (0..13); the grid is
                            stored with a power-of-two row stride (CELL_STRIDE=16)
                            so cell = (yq<<STRIDE_SHIFT)|xq is a shift, not a
                            multiply. 126/8 ~= 15 (125>>3 = 15 -> 16 cols), 112/8 =
                            14 (111>>3 = 13 -> 14 rows).
     - active ACREAGE     : per window, a cell is "active" iff it collected
                            >= MIN_EVENTS events (a noise floor). The looming
                            statistic is the ACTIVE CELL COUNT -- the on-screen AREA
                            (in cells) the object covers. A per-cell event count
                            lives in a uint8 array; we count how many cleared the
                            floor. Count of set flags only -- no multiply.
     - focus of expansion : running spatial median center (cx,cy), tracked
                            divide-free by NUDGING one step toward each event:
                              if (x>cx) cx++; else if (x<cx) cx--;   (same for y)
                            This converges on the coordinate median of the active
                            field (the point with as many events left as right /
                            up as down) -- the natural focus of a looming object --
                            using only compares and +/-1. No sum, no divide. cx/cy
                            are emitted so the host eye glares at the action.
     - looming score S    : the per-window CHANGE in active area,
                              S = area - prev_area
                            (a subtract), then CLAMPED to [-S_CLAMP, +S_CLAMP]. A
                            real approach grows the covered area by a few cells per
                            window, sustained over many windows; the clamp caps any
                            single-window jump so a sudden whole-field flash (an
                            object simply APPEARING) cannot fire on its own -- only a
                            SUSTAINED area increase can. compare + sub only.
     - leaky accumulator  : A += S - (A>>ACC_LEAK_SHIFT). Shift/add/sub only.

   Why the AREA TREND is a looming detector, and why translation is silent (the crux):
     Approaching objects grow larger in the image; that is the whole LGMD principle.
     The active AREA (count of covered cells) is a direct, robust proxy:
       * EXPANSION (looming / approach): the object covers more and more cells every
         window -> area RISES monotonically -> S is +ve and SUSTAINED over many
         windows -> A climbs past FLINCH_THRESHOLD -> flinch.
       * TRANSLATION (pan / wave-past): a constant-size object merely SLIDES across
         the frame -- the number of cells it covers stays ~CONSTANT -> area is flat
         -> S ~= 0 every window -> A never builds -> silent. (This is the crux case,
         and it holds because area is translation-invariant: moving a shape does not
         change how many cells it fills.)
       * RECEDE / SHRINK: the object covers fewer cells each window -> area FALLS ->
         S is -ve -> A sinks -> silent.
       * APPEAR (a shape that pops in and then sits still): area jumps once (0 ->
         its size) then stays flat. The S CLAMP caps that one-window jump to
         S_CLAMP, and with nothing sustaining it the leaky A decays back down before
         it can reach threshold -> silent. Only a MULTI-window rise (a real approach)
         accumulates past the clamp+leak.
     Verified in the host --validate: an EXPANDING disc FIRES; a constant-size
     TRANSLATING disc and a SHRINKING disc do NOT (nor does a static appearance).
     See chips/fpga/dvs_flinch_view.py.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_blackhole / dvs_caustics):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit ~microsecond timestamp, wraps)
     pol =  word        & 1
   (Several earlier apps read x/y/ts from the LOW bits -- the STALE layout; on the
   FPGA that reads the wrong bits. This app matches evt_pack.v + dvs_blackhole. The
   chips/fpga mirror packs the same way.)

   TIMEBASE. The looming mechanism is EVENT-DRIVEN, not microsecond-driven: a
   "window" is a fixed number of BATCH-sized batches (WINDOW_BATCHES). We do NOT
   use the ts field for the core mechanism, so the app validates identically on any
   recording regardless of its timestamp scale. ts is decoded (ABI) but unused.

   The sensor frame is SX x SY = 126 x 112. Coarse cells are 8 px wide (x>>3 ->
   0..15) by 8 px tall (y>>3 -> 0..13): a 16 x 14 = 224-cell grid. An 8-px cell is
   coarse enough to average down the per-pixel noise of the very noisy 126x112
   sensor, fine enough that an object's expanding edge crosses cell boundaries.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). A cell counts toward the
   active AREA ONLY if it collected >= MIN_EVENTS events in that window. Isolated
   hot-pixel sparkle fires one stray event in scattered cells and never reaches
   MIN_EVENTS, so it never enters the area count -- scattered noise adds a roughly
   CONSTANT small area (S ~= 0), it cannot manufacture a sustained rise. Per-window
   event counts live in a small uint8 array (cell_count[], cleared each window).
   Coarse 8-px binning already averages isolated pixels down; MIN_EVENTS is the
   second guard; the S clamp is the third (a noise burst can't spike the score).
   All compares/adds -- no multiply. WINDOW_BATCHES is large enough that a window
   accumulates plenty of events (WINDOW_BATCHES*BATCH), so a genuinely covered cell
   reliably clears MIN_EVENTS while sparse noise does not.

   REFRACTORY. Once a flinch latches we hold a short refractory (REFRACTORY_WINDOWS
   windows) during which we do not re-latch, so a single lunge produces ONE flinch
   rather than a stutter of them every batch while A stays high.

   -------------------------------------------------------------------------
   Emission cadence: ONE status word per BATCH (like dvs_blackhole / dvs_caustics),
   so the host stream never stalls and the eye animates smoothly. The looming SCORE
   is recomputed only at each WINDOW boundary (every WINDOW_BATCHES batches); on the
   other batches we re-emit the latched state (flinch pulse decays, level from the
   current accumulator, focus cx/cy which keep tracking every event). flinch is a
   1-batch PULSE latched at the window where A crosses FLINCH_THRESHOLD.

   Output word layout (low 22 bits used):
     bit [0]      = flinch    (1 bit)   -- 1 on the batch a looming flinch latched
     bits[6:1]    = level     (6 bits)  -- A >> LEVEL_SHIFT, capped 63 (rising
                                           "tension": pupil dilation on the host)
     bits[13:7]   = cx        (7 bits)  -- focus-of-expansion X (0..125)
     bits[20:14]  = cy        (7 bits)  -- focus-of-expansion Y (0..111)
   Host unpacks these fields; see dvs_flinch_view.py's unpack_status(). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v / dvs_blackhole). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Coarse cell grid. 8-px columns (x>>3 -> 0..15) by 8-px rows (y>>3 -> 0..13).
   CELL_STRIDE is a power of two so the cell index is a shift, not a multiply; here
   stride == COLS == 16 so nothing is wasted. */
#define XQ_SHIFT 3                               /* 8-px cols: 125>>3 = 15 -> cols 0..15 */
#define YQ_SHIFT 3                               /* 8-px rows: 111>>3 = 13 -> rows 0..13 */
#define CELL_COLS 16                             /* logical columns (0..15) */
#define CELL_ROWS 14                             /* logical rows    (0..13) */
#define STRIDE_SHIFT 4                           /* row*CELL_STRIDE via shift: STRIDE==16==1<<4 */
#define CELL_STRIDE (1 << STRIDE_SHIFT)          /* = 16 (power-of-two stride) */
#define CELL_CELLS (CELL_STRIDE * CELL_ROWS)     /* = 224 cells */

/* A window = this many BATCH-sized batches. Event-driven timebase (no ts). Large
   enough that a window accumulates plenty of events (WINDOW_BATCHES*BATCH = 480), so
   an object's covered cells reliably clear the MIN_EVENTS floor while sparse noise
   does not, and the covered AREA actually reflects the object's size. */
#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 120
#endif

/* A cell counts toward the active area for a window only if it saw >= MIN_EVENTS
   events in it (kills isolated hot-pixel sparkle on the very noisy 126x112 sensor). */
#ifndef MIN_EVENTS
#define MIN_EVENTS 3
#endif

/* Per-window looming score S = area - prev_area, CLAMPED to [-S_CLAMP, +S_CLAMP] so
   a single-window whole-field jump (an object simply APPEARING) can't fire on its
   own -- only a SUSTAINED, multi-window area rise (a real approach) accumulates. */
#ifndef S_CLAMP
#define S_CLAMP 3
#endif

/* Leaky accumulator: A += S - (A>>ACC_LEAK_SHIFT). ACC_LEAK_SHIFT sets its memory
   (÷16 per window: long enough to integrate the slow looming trend, short enough
   that a flat/negative stream drains it). ACC_CAP saturates it. */
#define ACC_LEAK_SHIFT 4
#define ACC_CAP 2047

/* Flinch when A crosses this; refractory holds off re-latch for a few windows. */
#ifndef FLINCH_THRESHOLD
#define FLINCH_THRESHOLD 20
#endif
#ifndef REFRACTORY_WINDOWS
#define REFRACTORY_WINDOWS 6
#endif

/* level = A >> LEVEL_SHIFT, capped to 6 bits (0..63) for the host pupil dilation. */
#define LEVEL_SHIFT 3

/* State, mostly in .bss (zeroed by crt0.S) so it starts cold. */
static uint8_t  cell_count[CELL_CELLS];   /* per-cell event count this window (sat) */
static int32_t  prev_area;                /* active cell count from the PREVIOUS window */
static int32_t  cx = SX / 2;              /* focus of expansion X (running median) */
static int32_t  cy = SY / 2;              /* focus of expansion Y (running median) */
static int32_t  acc;                      /* leaky looming accumulator A */
static uint32_t window_batches;           /* batches since last window boundary */
static uint32_t refractory;               /* windows left before a flinch may re-latch */
static uint32_t flinch_pulse;             /* 1 for the batch a flinch latched */

/* cx/cy carry frame-centre initialisers (not zero) so the median starts unbiased;
   crt0.S zeroes .bss but these live in .data (copied from flash by crt0.S). */

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to the
   instruction after it -- the next interrupt jumps straight to event_id_0's
   vector. A wfi() call inside this function would permanently skip its own
   epilogue (the stack pointer's restore), leaking stack every interrupt until it
   collides with this program's own code (see software/dvs_motion/main.c's
   isr_handler comment for the full explanation). Just returning is correct: this
   function's own `ret` lands on the same cached wfi() site main()'s return relies
   on. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Per event: nudge the running-median focus one step toward it (divide-free
       spatial median), and bump this window's per-cell event count. */
    for (uint32_t i = 0; i < BATCH; i++) {
        int32_t x = (int32_t)((v[i] >> X_SHIFT) & 0x7F);
        int32_t y = (int32_t)((v[i] >> Y_SHIFT) & 0x7F);

        if (x > cx) cx++; else if (x < cx) cx--;     /* running median toward events */
        if (y > cy) cy++; else if (y < cy) cy--;

        uint32_t xq   = (uint32_t)x >> XQ_SHIFT;              /* 0..15 */
        uint32_t yq   = (uint32_t)y >> YQ_SHIFT;              /* 0..13 */
        uint32_t cell = (yq << STRIDE_SHIFT) | xq;            /* row*STRIDE via shift */
        if (cell_count[cell] < 255) cell_count[cell]++;
    }

    window_batches++;
    flinch_pulse = 0;

    if (window_batches >= WINDOW_BATCHES) {
        window_batches = 0;

        /* Count the active AREA: how many cells cleared the MIN_EVENTS noise floor
           this window. Clear the counts for the next window as we go. This is the
           object's covered size in cells -- the looming signal. Count of a compare
           only, no multiply. */
        int32_t area = 0;
        for (uint32_t c = 0; c < CELL_CELLS; c++) {
            if (cell_count[c] >= MIN_EVENTS) area++;
            cell_count[c] = 0;
        }

        /* Looming score S = per-window CHANGE in covered area, clamped so a single
           whole-field jump (an object appearing) can't fire alone -- only a sustained
           multi-window rise (a real approach) accumulates. compare + sub only. */
        int32_t S = area - prev_area;
        prev_area = area;
        if (S >  S_CLAMP) S =  S_CLAMP;
        if (S < -S_CLAMP) S = -S_CLAMP;

        /* Leaky accumulator: A += S - (A>>SHIFT). Clamp to [0, ACC_CAP]. A rising area
           (loom) keeps S positive and drives A up; flat (translation) or falling
           (recede) area leaves S ~<=0 so A leaks back down. */
        acc = acc + S - (acc >> ACC_LEAK_SHIFT);
        if (acc < 0) acc = 0;
        if (acc > ACC_CAP) acc = ACC_CAP;

        /* Flinch latches when A crosses the threshold and we're out of refractory. */
        if (refractory > 0) {
            refractory--;
        } else if (acc > FLINCH_THRESHOLD) {
            flinch_pulse = 1;
            refractory = REFRACTORY_WINDOWS;
        }
    }

    /* Emit one status word every batch. level from the current accumulator (rising
       tension); cx/cy are the live focus so the eye keeps glaring at the action. */
    uint32_t level = (uint32_t)(acc >> LEVEL_SHIFT);
    if (level > 63u) level = 63u;

    uint32_t ecx = (uint32_t)cx & 0x7F;
    uint32_t ecy = (uint32_t)cy & 0x7F;

    /* bit0=flinch, bits[6:1]=level, bits[13:7]=cx, bits[20:14]=cy. */
    *FIFO_OUT = (ecy << 14) | (ecx << 7) | (level << 1) | (flinch_pulse & 1u);
}

void main(void) {
    /* cx/cy carry frame-centre initialisers (.data); the rest of .bss is zeroed by
       crt0.S so counts/prev_area/accumulator start cold. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
