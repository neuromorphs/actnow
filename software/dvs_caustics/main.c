#include <stdint.h>

/* "Event-Caustic Refractor" (dvs_caustics) -- a chips/fpga demo app in the same
   shape as software/dvs_sonar/main.c and software/dvs_apophenia/main.c (fifo_in
   fires event_id_0 once BATCH words land; isr_handler reads them, warps a
   representative event through a fake wavy water surface, writes ONE status word,
   and returns -- it NEVER calls wfi(), see the epilogue comment on isr_handler).

   Idea: turn the event stream into shimmering underwater LIGHT-CAUSTICS. Each
   event is "refracted" through a fake wavy water surface -- a multiply-free
   sine-LUT warp of its (x,y) driven by the event TIMESTAMP -- and deposited into a
   decaying caustic field that the HOST paints as rippling liquid light. The chip
   emits, per batch, ONE refracted sample {xr, yr, pol, strength} that the host
   accumulates (additive splats + per-frame multiplicative decay) into a blue/cyan
   caustic field -- shimmering, decorative, distinct from the grid/radial/creature
   apps (chips/fpga/dvs_caustics_view.py + the dashboard renderer). The chip only
   ever emits the warped sample; all the field decay / colour / animation happens
   on the computer.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). Everything below is compares, shifts, adds, and
   TABLE LOOKUPS. The "wavy water surface" is a sine field sampled from a small
   quarter-sine LUT reflected/negated into the full circle:

     - QUARTER-SINE LUT   : SIN_Q[0..63] holds round(AMP*sin(theta)) for theta in
                            [0,90deg), AMP=8. Values are 0..8 (int8). This is a
                            quarter of one period; the full period (256 phases) is
                            reconstructed by quadrant reflection/negation using only
                            shifts, masks and compares -- NO multiply (see sinLUT()).
     - FULL SINE          : sinLUT(idx & 0xFF) returns a signed offset in [-AMP,AMP]
                            for the 8-bit phase idx:
                              quadrant q = (idx>>6)&3, i = idx&63
                                q=0:  +SIN_Q[i]        (0 -> +90)
                                q=1:  +SIN_Q[63 - i]   (+90 -> 0, cos side)
                                q=2:  -SIN_Q[i]        (0 -> -90)
                                q=3:  -SIN_Q[63 - i]
                            All reflection (63 - i = i^63 within the 6-bit field)
                            and negation (two's-complement) -- shift/mask/xor only.

     - REFRACTION WARP    : per event, a temporal phase ph = ts >> PHASE_SHIFT
                            (the water surface advances with time). The refracted
                            offsets are a TRAVELLING WAVE -- they depend on BOTH
                            position and time, so the field shimmers and moves:
                              ox = sinLUT[(ph + (y >> WAVE_SHIFT)) & MASK]
                              oy = sinLUT[(ph + (x >> WAVE_SHIFT)) & MASK]
                            Note ox uses the event's Y and oy uses its X so the warp
                            is not a trivial diagonal; |ox|,|oy| <= AMP by
                            construction. Refracted position:
                              xr = clamp(x + ox, 0, SX-1=125)
                              yr = clamp(y + oy, 0, SY-1=111)
                            clamp is two compares. Everything add/sub/shift/LUT.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_sonar / dvs_apophenia):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit ~microsecond timestamp, wraps)
     pol =  word        & 1
   (Several upstream apps read x/y/ts from the LOW bits -- the STALE layout; on the
   FPGA that reads the wrong bits. This app matches evt_pack.v + dvs_sonar. The
   chips/fpga mirror packs the same way.)

   The sensor frame is SX x SY = 126 x 112 (so xr in 0..125, yr in 0..111).

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). The caustic FIELD on the host
   already averages many refracted samples with a decay, so isolated stray pixels
   wash out visually. On top of that, two multiply-free on-chip guards keep a lone
   stray event from spawning a bright splat:
     1. Per-batch leaky ACTIVITY gate. A single global counter is warmed +STEP per
        event and leaked by a right-shift each batch (act -= act>>DECAY_SHIFT). A
        refracted sample is emitted as a REAL splat (flag=1) only once this leaky
        activity crosses EMIT_THRESHOLD -- i.e. only when the batch is part of
        sustained activity, not one stray click. Below threshold we still emit the
        warped sample (so the field stays alive and the stream never stalls) but
        with flag=0 so the host paints it dim.
     2. Coarse per-REGION leaky gate. The frame is split into a small REG_COLS x
        REG_ROWS grid (coarse 32x16-px cells via x>>REGX_SHIFT, y>>REGY_SHIFT). The
        representative event's region must itself be leaky-warm (region counter
        >= REGION_MIN) for a REAL splat; a first-ever event in an otherwise cold
        region only gets a dim (flag=0) splat. This means a single pixel firing in
        empty space cannot dominate. Both counters are uint8, warmed by add and
        cooled by right-shift -- no multiply anywhere.
   The STRENGTH field carried to the host is the leaky global activity (>>3), so the
   host can further scale splat brightness by how "busy" the batch was.

   -------------------------------------------------------------------------
   Emission cadence: ONE status word per BATCH-sized batch of events (exactly like
   dvs_sonar / dvs_apophenia emit once per batch). The REPRESENTATIVE event we warp
   is the LAST event of the batch (simplest; the field averages over batches anyway
   so any fixed choice is fine -- documented here so the host mirror matches).

   Output word layout (low 21 bits used):
     bits[6:0]    = xr       (0..125, 7 bits) -- refracted (warped) X
     bits[13:7]   = yr       (0..111, 7 bits) -- refracted (warped) Y
     bit [14]     = pol      (1 bit)          -- polarity of the representative
                                                 event (0=OFF, 1=ON) -> host hue
     bits[19:15]  = strength (0..31, 5 bits)  -- leaky global activity >> 3 (how
                                                 busy the batch was; splat brightness)
     bit [20]     = flag     (1 bit)          -- 1 = real splat (activity crossed
                                                 EMIT_THRESHOLD AND region warm),
                                                 0 = dim (below threshold / cold region)
   Host unpacks these fields; see dvs_caustics_view.py's unpack_status(). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112
#define XR_MAX 125   /* SX-1: refracted-X clamp */
#define YR_MAX 111   /* SY-1: refracted-Y clamp */

/* Input event ABI (evt_pack.v / dvs_sonar). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* --- Wavy water surface: sine field -------------------------------------------
   AMP is the peak refraction offset in pixels (~8). The full sine has 256 phases
   (8-bit index); MASK wraps a phase into [0,255]. PHASE_SHIFT sets how fast the
   surface travels with the 16-bit timestamp (ts>>PHASE_SHIFT). WAVE_SHIFT sets the
   spatial wavelength of the surface (coarser -> longer, smoother waves). */
#define AMP         8
#define PHASE_BITS  8
#define MASK        0xFF          /* full-cycle phase mask (256 phases) */
#define PHASE_SHIFT 8             /* ts (0..65535) >> 8 -> ~256 phase steps over a wrap */
#define WAVE_SHIFT  4             /* spatial wavelength: (coord>>4) adds ~8 phase bins across the frame */

/* Quarter-sine LUT: SIN_Q[i] = round(AMP * sin(i/64 * pi/2)), i in 0..63, so the
   table sweeps 0 -> AMP over a quarter period. int8 (values 0..8). Reflected +
   negated into the full circle by sinLUT() below (no multiply). Generated offline
   (AMP=8). */
static const int8_t SIN_Q[64] = {
    0, 0, 0, 1, 1, 1, 1, 1,
    2, 2, 2, 2, 2, 3, 3, 3,
    3, 3, 3, 4, 4, 4, 4, 4,
    4, 5, 5, 5, 5, 5, 5, 6,
    6, 6, 6, 6, 6, 6, 6, 7,
    7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 8, 8, 8, 8, 8, 8,
    8, 8, 8, 8, 8, 8, 8, 8,
};

/* Full sine from the quarter LUT via quadrant reflection/negation -- shifts, masks
   and compares only, NO multiply. Returns a signed offset in [-AMP, AMP] for the
   8-bit phase idx (caller masks with MASK). i = idx & 63; the reflection 63 - i is
   i ^ 63 within the 6-bit field. */
static inline int32_t sinLUT(uint32_t idx) {
    uint32_t q = (idx >> 6) & 3u;      /* quadrant 0..3 */
    uint32_t i = idx & 63u;            /* position within the quadrant */
    switch (q) {
        case 0:  return  (int32_t)SIN_Q[i];          /* 0 -> +90:  +sin */
        case 1:  return  (int32_t)SIN_Q[i ^ 63u];    /* +90 -> 0:  +cos (mirror) */
        case 2:  return -(int32_t)SIN_Q[i];          /* 0 -> -90:  -sin */
        default: return -(int32_t)SIN_Q[i ^ 63u];    /* -90 -> 0:  -cos (mirror) */
    }
}

/* --- Leaky noise guards -------------------------------------------------------
   One global activity counter + a coarse per-region grid, both uint8, warmed by
   add (saturating) and cooled by right-shift (leaky). No multiply. */
#define STEP        32    /* activity added per event (saturating) */
#define ACT_CAP     255   /* 8-bit saturation for the leaky counters */
#define DECAY_SHIFT 2     /* leak: c -= c>>2 every batch (gentle ~quarter decay) */

/* Coarse region grid for the per-region gate: 32-px columns (x>>2>>3 == x>>5 ->
   0..3, since 125>>5 = 3) by 16-px rows (y>>4 -> 0..6, since 111>>4 = 6). Stored
   in a power-of-two stride so cell = (ry<<REG_STRIDE_SHIFT)|rx is a shift. */
#define REGX_SHIFT 5                          /* 32-px cols: 125>>5 = 3 -> cols 0..3 */
#define REGY_SHIFT 4                          /* 16-px rows: 111>>4 = 6 -> rows 0..6 */
#define REG_COLS 4                            /* logical cols (0..3) */
#define REG_ROWS 7                            /* logical rows (0..6) */
#define REG_STRIDE_SHIFT 2                    /* stride 4 == 1<<2 (power of two) */
#define REG_STRIDE (1 << REG_STRIDE_SHIFT)   /* = 4 */
#define REG_CELLS (REG_STRIDE * REG_ROWS)    /* = 28 uint8 cells */
#define REGION_MIN 48   /* region must be this leaky-warm for a REAL (flag=1) splat */

/* Global leaky activity must reach this before a REAL splat (flag=1). Coarse
   gating + this threshold means a lone stray event never fires a bright splat.
   Tunable at build time (the dashboard can pass -DEMIT_THRESHOLD=N). */
#ifndef EMIT_THRESHOLD
#define EMIT_THRESHOLD 64
#endif

/* Leaky state, in .bss (zeroed by crt0.S) so it starts cold. */
static uint8_t activity;             /* global leaky activity */
static uint8_t region[REG_CELLS];    /* coarse per-region leaky activity */

/* Clamp v into [0, hi] with two compares (no multiply). */
static inline int32_t clamp_hi(int32_t v, int32_t hi) {
    if (v < 0) return 0;
    if (v > hi) return hi;
    return v;
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

    /* Warm the global + per-region leaky activity from every event in the batch,
       and remember the LAST event (our representative sample to warp). */
    uint32_t rx_last = 0, ry_last = 0;
    uint32_t x_last = 0, y_last = 0, ts_last = 0, pol_last = 0;
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x   = (v[i] >> X_SHIFT) & 0x7F;
        uint32_t y   = (v[i] >> Y_SHIFT) & 0x7F;
        uint32_t ts  = (v[i] >> 1)       & 0xFFFF;
        uint32_t pol =  v[i]             & 1;

        /* Global leaky warm (saturating). */
        uint32_t g = activity + STEP;
        activity = (uint8_t)((g > ACT_CAP) ? ACT_CAP : g);

        /* Region leaky warm (saturating). */
        uint32_t rx = x >> REGX_SHIFT;                     /* 0..3 */
        uint32_t ry = y >> REGY_SHIFT;                     /* 0..6 */
        uint32_t rcell = (ry << REG_STRIDE_SHIFT) | rx;    /* row*stride via shift */
        uint32_t rw = region[rcell] + STEP;
        region[rcell] = (uint8_t)((rw > ACT_CAP) ? ACT_CAP : rw);

        rx_last = rx; ry_last = ry;
        x_last = x; y_last = y; ts_last = ts; pol_last = pol;
    }

    /* Leak the global counter once per batch (shift-only decay). */
    activity = (uint8_t)(activity - (activity >> DECAY_SHIFT));
    /* Leak the whole region grid too, so a region cools when quiet. */
    for (uint32_t c = 0; c < REG_CELLS; c++) {
        region[c] = (uint8_t)(region[c] - (region[c] >> DECAY_SHIFT));
    }

    /* --- Refract the representative (last) event through the wavy surface ------
       Temporal phase advances the water surface with the timestamp; the offsets
       are a travelling wave (depend on both position and time). ox uses Y, oy uses
       X so the warp is not a plain diagonal. */
    uint32_t ph = ts_last >> PHASE_SHIFT;
    int32_t ox = sinLUT((ph + (y_last >> WAVE_SHIFT)) & MASK);
    int32_t oy = sinLUT((ph + (x_last >> WAVE_SHIFT)) & MASK);

    int32_t xr = clamp_hi((int32_t)x_last + ox, XR_MAX);
    int32_t yr = clamp_hi((int32_t)y_last + oy, YR_MAX);

    /* strength (0..31): leaky global activity >> 3 (how busy the batch was). */
    uint32_t strength = activity >> 3;
    if (strength > 31u) strength = 31u;

    /* flag: a REAL splat only when the batch is sustained (global activity crossed
       EMIT_THRESHOLD) AND the representative event's region is itself leaky-warm.
       Otherwise still emit the warped sample but dim (flag=0). */
    uint32_t rcell_last = (ry_last << REG_STRIDE_SHIFT) | rx_last;
    uint32_t flag = (activity >= EMIT_THRESHOLD && region[rcell_last] >= REGION_MIN) ? 1u : 0u;

    /* bits[6:0]=xr, bits[13:7]=yr, bit[14]=pol, bits[19:15]=strength, bit[20]=flag. */
    *FIFO_OUT = (flag << 20) | (strength << 15) | (pol_last << 14)
              | (((uint32_t)yr) << 7) | (uint32_t)xr;
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so activity/region[] start cold. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
