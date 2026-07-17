#include <stdint.h>

/* "Computational Mayfly" -- a chips/fpga demo app in the same shape as
   software/dvs_motion/main.c (fifo_in fires event_id_0 once BATCH words land;
   isr_handler reads them, updates state, writes output word(s), and returns --
   NEVER calls wfi(), see the epilogue comment on isr_handler).

   Idea: every incoming DVS event spawns a tiny ephemeral "creature" -- a
   mayfly -- that lives for a few steps in a bit-packed world and then dies.
   The creature's whole life (birth point, direction, the cells it toggles) is
   derived purely from the event's own coordinate/polarity bits via a cheap
   multiply-free hash -- so the ecosystem is driven entirely by the event
   stream, with NO dependence on timestamps. Cover the lens and events stop, so
   the world stops changing and the organisms freeze. The chip emits one
   compact word per step so the host (chips/fpga/dvs_mayfly_view.py) can redraw
   the evolving world.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). The pseudo-random hash is XOR + rotate + shift
   only (an xorshift-style mix); world updates are shift/mask on a bit-packed
   occupancy grid; the per-step walk is a bounded fixed loop (no recursion, no
   unbounded growth). Every event does a constant, small amount of work.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_track/main.c):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit; unused here -- timestamp-independent)
     pol =  word        & 1
   (The stale low-bit layout the upstream dvs_motion/rotate still use reads the
   wrong bits on the FPGA; match evt_pack.v + dvs_track. Mirror packs the same.)

   World: a bit-packed occupancy grid, SX x SY = 126 x 112 bits. Packed as
   WORDS_PER_ROW = 4 uint32 per row (128 bits, 126 used) x SY rows =
   4*112*4 = 1792 bytes. A cell (cx,cy) is bit (cx & 31) of
   world[cy][cx>>5]. Toggling is world[...] ^= (1u << (cx & 31)) -- pure
   shift/mask, no divide.

   -------------------------------------------------------------------------
   Per event -> one mayfly:
     1. hash the event word (xorshift mix, multiply-free) into h.
     2. spawn point = the event's own (x,y) (so creatures appear where the
        sensor actually fired -- the world tracks real activity).
     3. direction = h & 7  (one of 8 compass steps, via DX[]/DY[]).
     4. lifespan  = MIN_LIFE + (h>>3 & LIFE_MASK)  (a few steps).
     5. walk that many steps from the spawn point, re-hashing h each step so
        the path wanders; at every step toggle the current cell (a walker that
        flips occupancy -- births and deaths interleave, so the world stays
        sparse and bounded rather than filling up). Coordinates wrap at the
        frame edge (mask), so a walker can't index off the world.

   The walk is capped at MAX_LIFE steps (a fixed, small bound), so per-event
   work is bounded no matter what the hash produces.

   -------------------------------------------------------------------------
   Output: one word per WALK STEP (so the host sees the creature's whole path,
   not just its birth). Word layout:
     bits[6:0]   = cx        (0..125, 7 bits)
     bits[13:7]  = cy        (0..111, 7 bits)
     bit[14]     = new_state (1 = cell now occupied, 0 = now empty, after toggle)
     bit[15]     = step0     (1 on the creature's first step -- lets the host
                              group steps back into individual mayflies)
   Host unpacks these; see dvs_mayfly_view.py's unpack_step().

   Emitting per-step means BATCH events can produce up to BATCH*MAX_LIFE output
   words. That's bounded and small (BATCH=4, MAX_LIFE=8 -> <=32 words/batch),
   well within the output FIFO's appetite, and matches dvs_rotate's precedent
   of emitting multiple words per batch.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). Left ungated, every
   isolated background/hot-pixel event would spawn a mayfly, so pure sensor
   noise would keep the world churning even on a static scene. An OPTIONAL
   spatio-temporal correlation SPAWN GATE (same technique as
   software/dvs_track/main.c / dvs_denoise, jAER's SpatioTemporalCorrelation
   Filter) only lets an event spawn a creature if >= CORR_MIN of its 8 cell-
   neighbours (EXCLUDING self, so hot pixels find no support) fired within the
   last CORR_WINDOW events. Real moving edges (correlated) spawn life; scattered
   noise does not -- so covering the lens really does freeze the ecosystem
   instead of leaving it twitching on noise. Tunable via -DCORR_MIN /
   -DCORR_WINDOW; CORR_MIN=0 disables it (default: on). The gate runs on a coarse
   4x4-px cell grid (CELL_SHIFT=2, like dvs_denoise), costing one uint32 per cell.

   The hash is seeded from a CANONICAL (x,y,pol)-only word so the firmware and
   the Python mirror agree bit-for-bit regardless of the raw hardware word's
   spare/timestamp bits.

   -------------------------------------------------------------------------
   Timestamp-independent by design, so this validates on ANY event stream,
   including the recorded chips/fpga CSVs whose `le` column is not a usable
   timestamp. The Python reference (dvs_mayfly_view.py --validate) runs the
   identical integer logic on a real capture and confirms the occupancy world
   stays bounded (population never exceeds the bit-world, no runaway growth). */

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

#define WORDS_PER_ROW 4                      /* 4 * 32 = 128 bits per row, 126 used */
#define WORLD_ROWS    SY                     /* 112 rows */

/* Optional spatio-temporal correlation SPAWN GATE (see NOISE STRATEGY). Coarse
   4x4-px cell grid; CORR_MIN=0 disables it. */
#define CELL_SHIFT 2
#define GRID_COLS  32                        /* 126>>2 = 31 < 32 */
#define GRID_ROWS  28                        /* 112>>2 = 28 */
#define GRID_CELLS (GRID_COLS * GRID_ROWS)   /* = 896 */
#ifndef CORR_WINDOW
#define CORR_WINDOW 30
#endif
#ifndef CORR_MIN
#define CORR_MIN 2
#endif

/* Mayfly lifespan: MIN_LIFE .. MIN_LIFE+LIFE_MASK steps, hard-capped at
   MAX_LIFE so the per-event walk loop is a fixed small bound. */
#define MIN_LIFE  2
#define LIFE_MASK 7                           /* extra steps in 0..7 */
#define MAX_LIFE  (MIN_LIFE + LIFE_MASK)      /* = 9, the loop's hard cap */

/* Bit-packed occupancy world (.bss, zeroed by crt0.S). */
static uint32_t world[WORLD_ROWS][WORDS_PER_ROW];

#if CORR_MIN > 0
static uint32_t last_touched[GRID_CELLS];   /* event index each cell last fired at; 0=never */
static uint32_t event_count;                /* "now" for the spawn gate */
static int is_recent(uint32_t last, uint32_t nowc) {
    return (last != 0) && ((nowc - last) <= CORR_WINDOW);
}
#endif

/* 8-connected compass steps, indexed by (hash & 7). No multiply anywhere. */
static const int8_t DX[8] = { 1, 1, 0, -1, -1, -1,  0,  1 };
static const int8_t DY[8] = { 0, 1, 1,  1,  0, -1, -1, -1 };

/* xorshift-style mix -- multiply-free pseudo-random step. Used both to seed a
   creature from its event word and to advance its wandering path each step. */
static uint32_t hash_step(uint32_t h) {
    h ^= h << 13;
    h ^= h >> 17;
    h ^= h << 5;
    return h;
}

/* Toggle a world cell and return its NEW occupancy (1 = now set). Pure
   shift/mask on the bit-packed grid -- no divide. cx/cy are pre-wrapped by the
   caller so indexing stays in range. */
static uint32_t toggle_cell(uint32_t cx, uint32_t cy) {
    uint32_t widx = cx >> 5;          /* which uint32 in the row (0..3) */
    uint32_t bit  = 1u << (cx & 31);  /* which bit within it */
    world[cy][widx] ^= bit;
    return (world[cy][widx] & bit) ? 1u : 0u;
}

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

    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t word = v[i];
        uint32_t x   = (word >> X_SHIFT) & 0x7F;
        uint32_t y   = (word >> Y_SHIFT) & 0x7F;
        uint32_t pol =  word            & 1u;

#if CORR_MIN > 0
        /* Spatio-temporal correlation SPAWN GATE (coarse 4x4 cells): only a
           correlated event spawns a mayfly. Record last_touched for every event
           (dropped or not) so a real new edge can bootstrap. */
        {
            uint32_t gcol = x >> CELL_SHIFT;
            uint32_t grow = y >> CELL_SHIFT;
            uint32_t cell = (grow << 5) | gcol;   /* GRID_COLS==32==1<<5 */
            event_count++;
            uint32_t nc = 0;
            int has_l = gcol > 0, has_r = gcol < GRID_COLS - 1;
            int has_u = grow > 0, has_d = grow < GRID_ROWS - 1;
            if (has_l)          nc += is_recent(last_touched[cell - 1],              event_count);
            if (has_r)          nc += is_recent(last_touched[cell + 1],              event_count);
            if (has_u)          nc += is_recent(last_touched[cell - GRID_COLS],      event_count);
            if (has_d)          nc += is_recent(last_touched[cell + GRID_COLS],      event_count);
            if (has_l && has_u) nc += is_recent(last_touched[cell - GRID_COLS - 1],  event_count);
            if (has_r && has_u) nc += is_recent(last_touched[cell - GRID_COLS + 1],  event_count);
            if (has_l && has_d) nc += is_recent(last_touched[cell + GRID_COLS - 1],  event_count);
            if (has_r && has_d) nc += is_recent(last_touched[cell + GRID_COLS + 1],  event_count);
            last_touched[cell] = event_count;
            if (nc < CORR_MIN) {
                continue;   /* uncorrelated -- no spawn (noise/hot pixel) */
            }
        }
#endif

        /* Seed the creature from a CANONICAL (x,y,pol)-only word so the firmware
           and the Python mirror agree regardless of the raw word's spare bits
           (fold in a nonzero constant so an all-zero event still hashes live). */
        uint32_t seed = (x << X_SHIFT) | (y << Y_SHIFT) | pol;
        uint32_t h = hash_step(seed | 0x9E3779B9u);

        uint32_t life = MIN_LIFE + (h & LIFE_MASK);   /* MIN_LIFE .. MAX_LIFE */
        if (life > MAX_LIFE) life = MAX_LIFE;          /* belt-and-braces cap */

        uint32_t cx = x;
        uint32_t cy = y;

        for (uint32_t s = 0; s < life; s++) {
            /* Clamp spawn into frame on step 0 (x/y are already valid, but a
               hash-derived nudge below could push out); wrap thereafter. */
            if (cx >= SX) cx = (cx >= 0x80000000u) ? 0u : (SX - 1);  /* underflow -> 0 */
            if (cy >= SY) cy = (cy >= 0x80000000u) ? 0u : (SY - 1);

            uint32_t new_state = toggle_cell(cx, cy);
            uint32_t step0 = (s == 0) ? 1u : 0u;

            /* bits: [6:0]=cx, [13:7]=cy, [14]=new_state, [15]=step0 */
            *FIFO_OUT = (step0 << 15) | (new_state << 14) | (cy << 7) | cx;

            /* Advance the walk: re-hash and take a compass step. Use signed
               deltas added to unsigned coords; the >=SX / >=SY checks at the
               top of the next iteration catch both overflow and underflow
               (an underflow wraps to a huge unsigned value >= 0x80000000). */
            h = hash_step(h);
            uint32_t dir = h & 7u;
            cx = (uint32_t)((int32_t)cx + DX[dir]);
            cy = (uint32_t)((int32_t)cy + DY[dir]);
        }
    }
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so the world starts empty. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
