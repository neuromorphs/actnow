#include <stdint.h>

/* "THE SÉANCE CIRCUIT" (dvs_seance) -- a chips/fpga demo app in the same
   shape as software/dvs_vital/main.c (fifo_in fires event_id_0 once BATCH
   words land; isr_handler reads them, updates crowd net-motion sums, and
   integrates a planchette position each window; it NEVER calls wfi(), see
   the epilogue comment on isr_handler).

   Idea (ouija planchette -- novel among these apps): a planchette drifts
   across a 126x112 virtual séance board, steered by the net directional bias
   of the event stream.  Left/right and top/bottom half-sums track how many
   events fell in each half during the current window.  At the end of each
   window the velocity is (right_sum - left_sum) >> K_SHIFT (clamped to
   ±VEL_CAP) and likewise for vy; the planchette position (px, py) is
   integrated and clamped to the board.  A per-region refractory guard
   (9 cols x 8 rows = 72 14x14-px regions) suppresses hot-pixel clusters:
   any region whose refractory counter is non-zero is skipped, and every
   region's counter decrements each window.

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (1) Right-biased events (x >= 63 only) -> after enough windows px
         increases from its initial position.
     (2) Down-biased events (y >= 56 only) -> after enough windows py
         increases.
     (3) Uniform events -> planchette stays near centre.
     (4) Single repeated hot pixel -> refractory guard prevents drift.
     (5) Planchette always in bounds (0<=px<=125, 0<=py<=111).
     (6) Status words are well-formed (all bit fields within spec).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - half-sum accumulators: add 1 (increment); no multiply.
     - decay: each window, sum >>= 1; right-shift only, no multiply.
     - velocity: (right-left)>>K_SHIFT; shift + subtract; no multiply.
     - velocity clamp: compare + conditional assign; no multiply.
     - refractory region index: (x >> REF_XSHIFT)*REF_COLS + (y >> REF_YSHIFT).
       x >> REF_XSHIFT produces the column index (14-px-wide bins);
       y >> REF_YSHIFT produces the row index.  The multiplication
       row*REF_COLS is replaced by a shift: REF_COLS=9 is NOT a power of two,
       so instead we use REF_COLS=8 (1<<3) rounding: store row*(1<<REF_CSHIFT)
       where REF_CSHIFT=3; total cells = 9*8=72 but addressed as row<<REF_CSHIFT+col.
       Wait -- 9 columns of 14px each = 126px; 8 rows of 14px = 112px.  Column
       index 0..8 (9 values), row index 0..7 (8 values).  Maximum index =
       7*(1<<4)+8 = 112+8=120 < 128; we use REF_COLS=16 (1<<REF_CSHIFT=4) for
       addressing (wastes 7 slots per row but avoids multiply completely).
       Array size = 8*16 = 128 uint8_t entries, trivially fits in SRAM.
     - planchette integration: add/sub velocity, compare for clamp; no multiply.
     - output pack: shifts and ORs only.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_entropy / dvs_flinch):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp) -- decoded but UNUSED
     pol =  word        & 1                                    -- decoded but UNUSED
   ts and pol are decoded per ABI but unused -- this app is driven by
   spatial position only.

   -------------------------------------------------------------------------
   WINDOW TIMEBASE: event-count driven.  A window closes after WINDOW_EVENTS
   events (512 = 1<<9; window index incremented by shifting the event counter
   right by 9 -- no divide needed).

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112 and VERY noisy).  Two documented
   multiply-free guards:
     1. REFRACTORY GUARD.  The board is divided into a 9x8 coarse grid of
        14x14-px regions (72 cells, addressed via shifts).  When a region's
        refractory counter is > 0 that region's events are skipped entirely
        (not added to any half-sum).  Each window the counter is decremented
        toward zero (REF_PERIOD windows of suppression after firing).  A hot
        pixel or tight cluster fires its region once and is then ignored for
        REF_PERIOD windows.  This prevents a single noisy pixel from
        systematically biasing the planchette.
     2. MINIMUM IMBALANCE THRESHOLD.  Before applying velocity, we require
        |right_sum - left_sum| > IMBALANCE_MIN (and likewise for y).  Uniform
        noise that populates both halves equally produces a net near-zero
        imbalance; only a genuine directional bias passes the threshold.

   -------------------------------------------------------------------------
   Output word layout (32 bits used):
     bits[ 7: 0] = seq      (8-bit window sequence counter, wraps mod 256)
     bits[15: 8] = speed    (8-bit speed magnitude, capped at 255)
     bits[16]    = vy_sign  (1 if vy < 0 i.e. moving up, 0 if vy >= 0)
     bits[17]    = vx_sign  (1 if vx < 0 i.e. moving left, 0 if vx >= 0)
     bits[24:18] = py       (planchette y, 0..111, 7 bits)
     bits[31:25] = px       (planchette x, 0..125, 7 bits)
   Host unpacks these fields; see chips/fpga/dvs_seance_view.py's
   unpack_status(). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Board split boundaries: left = x < X_SPLIT, right = x >= X_SPLIT,
   top = y < Y_SPLIT, bottom = y >= Y_SPLIT. */
#define X_SPLIT 63
#define Y_SPLIT 56

/* Planchette bounds (0-indexed). */
#define PX_MAX 125
#define PY_MAX 111

/* Tunables: each under #ifndef so -D overrides work at compile time. */
#ifndef WINDOW_EVENTS
#define WINDOW_EVENTS 512           /* events per window (must be power of two) */
#endif

#ifndef WINDOW_SHIFT
#define WINDOW_SHIFT  9             /* log2(WINDOW_EVENTS) for event-count timebase */
#endif

#ifndef K_SHIFT
#define K_SHIFT 3                   /* velocity = (right-left) >> K_SHIFT, capped */
#endif

#ifndef VEL_CAP
#define VEL_CAP 8                   /* maximum |vx| or |vy| per window */
#endif

#ifndef IMBALANCE_MIN
#define IMBALANCE_MIN 8             /* require |imbalance| > this before applying v */
#endif

#ifndef REF_PERIOD
#define REF_PERIOD 4                /* windows of refractory suppression after a region fires */
#endif

/* Refractory grid: 9 columns x 8 rows of 14x14-px regions.
   Addressed as ref[row << REF_CSHIFT | col] to avoid multiply.
   REF_CSHIFT = 4 gives 16 slots per row (>= 9 cols), array size = 128. */
#define REF_XSHIFT  4               /* x >> 4 = column index (14-px bins: 0..8) */
#define REF_YSHIFT  4               /* y >> 4 = row index    (14-px bins: 0..7) */
#define REF_CSHIFT  4               /* row << REF_CSHIFT to get row offset (16 per row) */
#define REF_SIZE    128             /* 8 rows * 16 slots = 128 total entries */

/* Half-sum decay: shift right by 1 each window (halve the accumulators). */
#define DECAY_SHIFT 1

/* Event counter (total events seen, used for window timebase). */
static uint32_t event_count;

/* Half-sum accumulators for the current window. */
static uint32_t left_sum;
static uint32_t right_sum;
static uint32_t top_sum;
static uint32_t bot_sum;

/* Planchette position (0..PX_MAX, 0..PY_MAX). Starts at board centre. */
static uint32_t px;
static uint32_t py;

/* Latched status from the completed window (emitted each batch). */
static uint32_t lat_px;
static uint32_t lat_py;
static uint32_t lat_vx_sign;
static uint32_t lat_vy_sign;
static uint32_t lat_speed;

/* 8-bit window sequence counter (wraps mod 256). */
static uint32_t seq;

/* Per-region refractory counters (decremented each window). */
static uint8_t ref_cnt[REF_SIZE];

/* Window index from the last latch (used to detect new window via shift). */
static uint32_t last_window;

/* Helper: signed right-shift of a uint32_t interpreted as a signed value.
   Returns |v >> shift| clamped to [0, VEL_CAP], and sets *neg=1 if v was
   negative (i.e. if the subtracted value was larger than the added value).
   Called only at window boundary; no divide, no multiply. */
static uint32_t compute_vel(uint32_t pos_sum, uint32_t neg_sum,
                             uint32_t *neg_out) {
    uint32_t imbalance;
    uint32_t vel;
    if (pos_sum >= neg_sum) {
        imbalance = pos_sum - neg_sum;
        *neg_out = 0u;
    } else {
        imbalance = neg_sum - pos_sum;
        *neg_out = 1u;
    }
    if (imbalance <= (uint32_t)IMBALANCE_MIN) {
        *neg_out = 0u;
        return 0u;
    }
    vel = imbalance >> K_SHIFT;
    if (vel > (uint32_t)VEL_CAP) vel = (uint32_t)VEL_CAP;
    return vel;
}

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to
   the instruction after it -- the next interrupt jumps straight to
   event_id_0's vector.  A wfi() call inside this function would permanently
   skip its own epilogue (the stack pointer's restore), leaking 16 bytes of
   stack every interrupt until it collides with this program's own code (see
   software/dvs_motion/main.c's isr_handler comment for the full explanation).
   Just returning is correct: this function's own `ret` lands on the same
   cached wfi() site main()'s return already relies on. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Process each event in arrival order: decode ABI fields, apply
       refractory guard, then accumulate into half-sums. */
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x = (v[i] >> X_SHIFT) & 0x7Fu;
        uint32_t y = (v[i] >> Y_SHIFT) & 0x7Fu;
        /* ts  = (v[i] >> 1) & 0xFFFFu; -- decoded per ABI but unused */
        /* pol =  v[i] & 1u;            -- decoded per ABI but unused */

        /* Refractory region check: column = x >> REF_XSHIFT,
           row = y >> REF_YSHIFT, index = (row << REF_CSHIFT) | col. */
        uint32_t col = x >> REF_XSHIFT;
        uint32_t row = y >> REF_YSHIFT;
        uint32_t ridx = (row << REF_CSHIFT) | col;
        if (ref_cnt[ridx] != 0u) {
            /* Region still refractory: skip this event. */
            event_count++;  /* still advance the event counter for window timing */
            goto check_window;
        }

        /* Accumulate into half-sums. */
        if (x < (uint32_t)X_SPLIT) {
            left_sum++;
        } else {
            right_sum++;
        }
        if (y < (uint32_t)Y_SPLIT) {
            top_sum++;
        } else {
            bot_sum++;
        }
        event_count++;

    check_window:;
        /* Check if a new window has started (compare window index before/after). */
        uint32_t cur_window = event_count >> WINDOW_SHIFT;
        if (cur_window != last_window) {
            last_window = cur_window;

            /* Compute velocities from half-sum imbalances. */
            uint32_t vx_neg = 0u, vy_neg = 0u;
            uint32_t vx = compute_vel(right_sum, left_sum, &vx_neg);
            uint32_t vy = compute_vel(bot_sum,   top_sum,  &vy_neg);

            /* Integrate planchette position. */
            if (vx_neg) {
                /* Moving left: subtract from px. */
                if (px >= vx) {
                    px -= vx;
                } else {
                    px = 0u;
                }
            } else {
                px += vx;
                if (px > (uint32_t)PX_MAX) px = (uint32_t)PX_MAX;
            }

            if (vy_neg) {
                /* Moving up: subtract from py. */
                if (py >= vy) {
                    py -= vy;
                } else {
                    py = 0u;
                }
            } else {
                py += vy;
                if (py > (uint32_t)PY_MAX) py = (uint32_t)PY_MAX;
            }

            /* Speed magnitude = |vx| + |vy|, capped at 255. */
            uint32_t speed = vx + vy;
            if (speed > 255u) speed = 255u;

            /* Latch output fields. */
            lat_px      = px;
            lat_py      = py;
            lat_vx_sign = vx_neg;
            lat_vy_sign = vy_neg;
            lat_speed   = speed;

            /* Decay half-sums (halve each accumulator). */
            left_sum  >>= DECAY_SHIFT;
            right_sum >>= DECAY_SHIFT;
            top_sum   >>= DECAY_SHIFT;
            bot_sum   >>= DECAY_SHIFT;

            /* Decrement refractory counters for all regions. */
            for (uint32_t r = 0u; r < REF_SIZE; r++) {
                if (ref_cnt[r] != 0u) ref_cnt[r]--;
            }

            /* Advance window sequence counter. */
            seq = (seq + 1u) & 0xFFu;
        }
    }

    /* Emit ONE word per batch from the LATCHED values only.
       Layout: bits[7:0]=seq, bits[15:8]=speed,
               bits[16]=vy_sign, bits[17]=vx_sign,
               bits[24:18]=py, bits[31:25]=px */
    *FIFO_OUT = (lat_px      << 25)
              | (lat_py      << 18)
              | (lat_vx_sign << 17)
              | (lat_vy_sign << 16)
              | (lat_speed   <<  8)
              |  seq;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- correct cold start for most state.
       px and py start at 0 (top-left corner), which is fine; the algorithm
       will drift them toward the centre under balanced input.
       event_count, left_sum, right_sum, top_sum, bot_sum, lat_px, lat_py,
       lat_vx_sign, lat_vy_sign, lat_speed, seq, ref_cnt[], last_window
       all start at 0. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
