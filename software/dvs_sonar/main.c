#include <stdint.h>

/* "Radial Motion Oracle" (dvs_sonar) -- a chips/fpga demo app in the same shape
   as software/dvs_apophenia/main.c and software/dvs_heartbeats/main.c (fifo_in
   fires event_id_0 once BATCH words land; isr_handler reads them, updates a tiny
   per-octant leaky histogram, writes ONE status word, and returns -- it NEVER
   calls wfi(), see the epilogue comment on isr_handler).

   Idea: every event's position relative to the FRAME CENTRE (CX,CY) is reduced to
   a coarse POLAR coordinate -- an OCTANT (0..7, which 45-degree wedge the event
   sits in) and a RADIUS (how far out it is, quantized to 5 bits). The chip emits,
   per batch, the DOMINANT octant's {octant, radius, pol, strength}. The HOST turns
   each emission into an expanding SONAR/RADAR RIPPLE: a ring spawned at that polar
   position that grows outward and fades over frames, coloured by octant, hued by
   polarity -- a living oracle display (chips/fpga/dvs_sonar_view.py + the
   dashboard renderer). Distinct from the grid-based apps: the geometry is radial,
   centred on the sensor, and the host animates *time* (expanding rings) rather
   than painting a static grid.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). Everything below is compares, shifts, adds:
     - signed offset      : dx = x - CX, dy = y - CY   (can be negative)
     - |offset|           : adx = dx<0 ? -dx : dx      (negate = two's-complement,
                            still no multiply), likewise ady
     - OCTANT (0..7)      : from sign(dx), sign(dy), and adx vs ady -- PURE
                            COMPARES, no atan2, no multiply. The 8 wedges are the
                            standard compass octants (see the octant() table
                            below): E, NE, N, NW, W, SW, S, SE with the DVS
                            convention that +y points DOWN the sensor.
     - RADIUS (Chebyshev) : r = max(adx, ady)          -- L-infinity distance, a
                            single compare. Chebyshev (not Euclidean) so there is
                            NO square/sqrt/multiply. Quantized to 5 bits by a right
                            shift RADIUS_SHIFT (r >> 1 -> 0..31, since the max
                            offset from centre is ~63 and 63>>1 = 31).
     - leaky histogram    : hist[octant] += STEP (saturating); every batch the
                            whole 8-entry histogram leaks by a right-shift so a
                            single stray event never dominates. argmax by compare.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_track / dvs_apophenia):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit ~microsecond timestamp, wraps)
     pol =  word        & 1
   (Several upstream apps read x/y/ts from the LOW bits -- the STALE layout; on the
   FPGA that reads the wrong bits. This app matches evt_pack.v + dvs_track. The
   chips/fpga mirror packs the same way.)

   The sensor frame is SX x SY = 126 x 112. Frame centre is CX=63, CY=56.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). Two multiply-free guards:
     1. Per-batch, we do not emit the polar coordinate of one arbitrary event.
        Instead each event's octant votes into an 8-entry LEAKY HISTOGRAM
        (hist[octant] += STEP, saturating). The histogram leaks by a right-shift
        every batch, so a single stray event in a wedge warms it to just STEP and
        is quickly overtaken by any wedge with sustained activity. We emit the
        DOMINANT octant (argmax over the 8 leaky counters).
     2. The reported RADIUS for that octant is the leaky-averaged radius of events
        that landed in it this batch (an accumulate-then-shift mean-ish estimate,
        shift-only), not a single event's radius, so one far-flung noise event does
        not fling the ripple to the frame edge.
   A dominant octant is only reported as a REAL peak (flag=1) once its leaky count
   crosses EMIT_THRESHOLD; below that we still emit the batch's dominant wedge but
   with flag=0 so the host can dim it. The stream therefore never stalls.

   -------------------------------------------------------------------------
   Emission cadence: ONE status word per BATCH-sized batch of events (exactly like
   dvs_apophenia / dvs_heartbeats emit once per batch).

   Output word layout (low 15 bits used):
     bits[2:0]   = octant   (0..7, 3 bits)   -- dominant wedge, compass octant
     bits[7:3]   = radius    (0..31, 5 bits) -- quantized Chebyshev distance r>>1
     bit [8]     = pol       (1 bit)         -- polarity of the dominant octant's
                                                last event (0=OFF, 1=ON) -> host hue
     bits[13:9]  = strength  (0..31, 5 bits) -- dominant leaky count >> 3 (how loud
                                                the ping is; drives ripple brightness)
     bit [14]    = flag      (1 bit)         -- 1 = real peak (leaky count crossed
                                                EMIT_THRESHOLD), 0 = below threshold
   Host unpacks these fields; see dvs_sonar_view.py's unpack_status(). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame + centre (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112
#define CX 63
#define CY 56

/* Input event ABI (evt_pack.v / dvs_track). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Radius quantization: the largest |dx| from centre is ~63 (0..125 around 63) and
   the largest |dy| is ~56; Chebyshev max is <= 63. r>>1 maps 0..63 -> 0..31, a
   clean 5-bit field. Shift-only. */
#define RADIUS_SHIFT 1
#define RADIUS_MAX   31   /* 5-bit clamp for the quantized radius */

/* Leaky per-octant histogram. STEP is added (saturating) to a wedge per event;
   every batch each counter leaks by (c >> DECAY_SHIFT). All shifts/adds/compares,
   no multiply. */
#define NUM_OCTANTS 8
#define STEP        24    /* activity added to a wedge per event (saturating) */
#define HIST_CAP    255   /* 8-bit saturation for the leaky wedge counter */
#define DECAY_SHIFT 2     /* leak: c -= c>>2 every batch (gentle ~quarter decay) */

/* A wedge's leaky count must reach this before it is reported as a REAL sonar
   ping (flag=1). Coarse octant binning + this threshold means a lone stray event
   (which warms a wedge to just STEP) never fires a real ping. Tunable at build
   time (the dashboard can pass -DEMIT_THRESHOLD=N). */
#ifndef EMIT_THRESHOLD
#define EMIT_THRESHOLD 64
#endif

/* Per-octant leaky activity, in .bss (zeroed by crt0.S) so it starts cold. */
static uint8_t hist[NUM_OCTANTS];

/* Absolute value without a multiply: negate is two's-complement (sub from 0). */
static inline int32_t iabs32(int32_t d) { return d < 0 ? -d : d; }

/* Compass octant (0..7) from the signed offset (dx,dy). DVS convention: +x is
   RIGHT, +y is DOWN the sensor, so "North" is dy<0 (upward). Pure sign+magnitude
   compares -- no atan2, no multiply. The wedge boundaries are the 45-degree
   diagonals (|dx| vs |dy|):
     0=E  (dx>0, |dx|>=|dy|)        1=NE (dx>0, dy<0, |dx|< |dy|)
     2=N  (dy<0, |dy|>=|dx|)        3=NW (dx<0, dy<0, |dx|< |dy|)
     4=W  (dx<0, |dx|>=|dy|)        5=SW (dx<0, dy>0, |dx|< |dy|)
     6=S  (dy>0, |dy|>=|dx|)        7=SE (dx>0, dy>0, |dx|< |dy|)
   Matches the OCTANT_VEC/compass table used by dvs_stabilize + the host mirror. */
static inline uint32_t octant_of(int32_t dx, int32_t dy) {
    int32_t adx = iabs32(dx);
    int32_t ady = iabs32(dy);
    if (adx >= ady) {
        /* Horizontal-dominant wedge: E or W (with the diagonal split above). */
        return (dx >= 0) ? 0u : 4u;
    } else {
        /* Vertical-dominant wedge: N or S. */
        if (dy < 0) {
            return (dx >= 0) ? 1u : 3u;   /* NE : NW (upper diagonals) */
        } else {
            return (dx >= 0) ? 7u : 5u;   /* SE : SW (lower diagonals) */
        }
    }
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

    /* Per-octant accumulated radius + count THIS batch, so the reported radius is
       an average-ish over the wedge's events (shift-only mean), not one event's. */
    uint32_t radius_sum[NUM_OCTANTS] = {0};
    uint32_t radius_cnt[NUM_OCTANTS] = {0};
    uint32_t last_pol[NUM_OCTANTS]   = {0};

    /* Vote each event into its octant + accumulate its quantized radius. */
    for (uint32_t i = 0; i < BATCH; i++) {
        int32_t x   = (int32_t)((v[i] >> X_SHIFT) & 0x7F);
        int32_t y   = (int32_t)((v[i] >> Y_SHIFT) & 0x7F);
        uint32_t pol = v[i] & 1;

        int32_t dx = x - CX;
        int32_t dy = y - CY;

        uint32_t oct = octant_of(dx, dy);

        /* Chebyshev radius = max(|dx|,|dy|), quantized to 5 bits (shift-only). */
        int32_t adx = iabs32(dx);
        int32_t ady = iabs32(dy);
        int32_t cheb = (adx >= ady) ? adx : ady;
        uint32_t rq = (uint32_t)(cheb >> RADIUS_SHIFT);
        if (rq > RADIUS_MAX) rq = RADIUS_MAX;

        /* Warm the wedge (saturating +STEP). */
        uint32_t warmed = hist[oct] + STEP;
        hist[oct] = (uint8_t)((warmed > HIST_CAP) ? HIST_CAP : warmed);

        radius_sum[oct] += rq;
        radius_cnt[oct] += 1;
        last_pol[oct]    = pol;
    }

    /* Leak the whole histogram once per batch (shift-only decay) so a single
       stray wedge event fades and cannot dominate over sustained activity. */
    for (uint32_t o = 0; o < NUM_OCTANTS; o++) {
        hist[o] = (uint8_t)(hist[o] - (hist[o] >> DECAY_SHIFT));
    }

    /* Dominant wedge = argmax over the leaky counters (compare, no divide). */
    uint32_t best_oct = 0;
    uint32_t best_val = 0;
    for (uint32_t o = 0; o < NUM_OCTANTS; o++) {
        if (hist[o] > best_val) {
            best_val = hist[o];
            best_oct = o;
        }
    }

    /* Reported radius for the dominant wedge: the leaky-averaged radius of the
       events that landed in it this batch. Division-by-count is avoided with a
       count-based right shift (cnt is 1..BATCH=4): shift by 0/1/2 for cnt
       1/2/(3..4). This is a multiply-free mean-ish estimate. If the dominant
       wedge got no event THIS batch (it is only leaky-hot from earlier batches),
       fall back to radius 0 (a ping at the centre for that wedge). */
    uint32_t out_radius = 0;
    uint32_t cnt = radius_cnt[best_oct];
    if (cnt > 0) {
        uint32_t sum = radius_sum[best_oct];
        uint32_t shift = (cnt >= 3) ? 2u : (cnt == 2 ? 1u : 0u);
        out_radius = sum >> shift;
        if (out_radius > RADIUS_MAX) out_radius = RADIUS_MAX;
    }

    uint32_t out_pol = last_pol[best_oct] & 1;

    /* strength (0..31): how loud the ping is, dominant leaky count >> 3. */
    uint32_t strength = best_val >> 3;
    if (strength > 31u) strength = 31u;

    /* flag: real ping only once the wedge crosses EMIT_THRESHOLD. */
    uint32_t flag = (best_val >= EMIT_THRESHOLD) ? 1u : 0u;

    /* bits[2:0]=octant, bits[7:3]=radius, bit[8]=pol, bits[13:9]=strength,
       bit[14]=flag. */
    *FIFO_OUT = (flag << 14) | (strength << 9) | (out_pol << 8)
              | (out_radius << 3) | best_oct;
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so hist[] starts cold. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
