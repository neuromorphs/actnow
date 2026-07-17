#include <stdint.h>

/* ===========================================================================
 * dvs_oms_meister -- Markus Meister's retinal Object-Motion-Sensitivity (OMS)
 * computation as an event-driven RV32I firmware app.
 *
 * This is the CANONICAL / PRINCIPLED reference port of the rate-based OMS
 * model from ~/git/oms-meister (software/oms_pipeline.py + hardware/DESIGN.md).
 * It is a retinal LNLN circuit:
 *
 *   per-subunit dual-leaky difference-of-exponentials BANDPASS (tau_fast=12ms,
 *   tau_slow=120ms) on SEPARATE ON/OFF channels
 *     -> HALF-WAVE RECTIFY *before* pooling   (the load-bearing invariant:
 *          sum_i [x_i]+  !=  [sum_i x_i]+ , and ON must not cancel OFF)
 *     -> tanh compression (one hot edge can't dominate the wide surround)
 *     -> narrow CENTER pool + wide SURROUND annulus (both unit-DC normalized,
 *          surround delay-matched)
 *     -> DIVISIVE inhibition  z = [ E/(sigma_floor + alpha*S_delayed) - theta ]+
 *          with a SHARED wide denominator (never per-subunit local AGC)
 *     -> leaky integrate-and-fire per subunit emits OMS "spikes" on
 *          INDEPENDENTLY-moving objects, and stays silent on coherent global
 *          motion (a saccade drives center and surround identically -> cancel).
 *
 *   Coherent global motion  -> E and S rise together -> divide out -> SILENT.
 *   Independent object motion -> center decorrelates from surround -> a center
 *                                residual survives -> the OMS cell FIRES,
 *                                localized on the object.
 *
 * ---------------------------------------------------------------------------
 * HONEST LIMITATION (read this):
 *   This RATE-based OMS model is the biologically-principled canonical
 *   reference, but the oms-meister overnight benchmark
 *   (~/git/oms-meister/benchmark/OVERNIGHT_REPORT.md) found it ranks LOW on
 *   realistic CAMERA-SHAKE data (`corrected_oms` placed 13th/15; direction-
 *   /flow-based methods won). Under real ego-motion the center/surround
 *   cancellation is imperfect and rate contrast alone is a weak object cue.
 *   A separate firmware app ports the direction-based winner. Use THIS app as
 *   the clean textbook OMS baseline, not as the SOTA shake-robust detector.
 * ---------------------------------------------------------------------------
 *
 * TARGET (hard constraints):
 *   - Async RV32I, NO multiply/divide (software/common/program.mk: -march=rv32i).
 *   - 32 KB SRAM total incl. stack; code XIP from ROM. Event-driven: FIFO_IN
 *     interrupts every BATCH events; the ISR pops BATCH, processes, writes
 *     FIFO_OUT, returns (no wfi() in the ISR -- see the isr_handler comment).
 *   - Sensor 126x112 (NOT 240x180 -- the subunit grid is rescaled here).
 *   - Event word (evt_pack.v, decoded like software/dvs_track/main.c):
 *     x=(word>>24)&0x7F, y=(word>>17)&0x7F, ts=(word>>1)&0xFFFF (16-bit),
 *     pol=word&1.  X_SHIFT=24, Y_SHIFT=17. (The stale low-bit layout the
 *     upstream dvs_motion/rotate still use reads the wrong bits on the FPGA.)
 *
 * NO-MULTIPLY / NO-DIVIDE techniques used (all documented at their sites):
 *   - Leaky integrators (the DoE bandpass): shift-leak  s -= s>>k  instead of
 *     s *= exp(-dt/tau). k chosen so the per-batch decay ~ exp(-BIN/tau).
 *   - Center/surround box pooling: a SUMMED-AREA TABLE (integral image) makes
 *     every rectangular pool O(1) adds/subtracts. The annulus surround is
 *     (outer box) - (inner box) -- multiply-free. Boxcar rings approximate the
 *     Gaussian/annulus (DESIGN.md discusses this).
 *   - Divisive inhibition E/(sigma+alpha*S): a reciprocal LUT (recip[] in ROM,
 *     1/x in Q15) then a shift -- no hardware divide.
 *   - tanh: small ROM LUT (tanh_lut[]). Rectify = compare + select. Unit-DC
 *     normalization of a pool = multiply by a per-window-area reciprocal, again
 *     via the reciprocal LUT.
 *
 * FIXED-POINT (Q-format), documented per-stage:
 *   - Event drive into the integrators is a small integer per event (DRIVE).
 *   - Leaky-integrator state s_fast/s_slow and bandpass b are plain int16-range
 *     integers (call it Q0 "activity units"): b = s_fast - s_slow.
 *   - tanh LUT output rc is Q8 (0..256 == 0..1.0).
 *   - The SAT (summed-area table) accumulates rc values -> int32.
 *   - A pool (box sum) normalized to unit-DC is: sum * recip(area) >> RSHIFT,
 *     yielding a Q8-ish pooled value E, S.
 *   - Divisive output z = (E<<EGAIN) * recip(sigma_floor + alpha*S) >> RECIP_Q,
 *     then subtractive theta, then the slow ADAPTIVE global threshold gthr
 *     (z2 = [z - gthr*5/4]+). z2 drives the per-subunit LIF (membrane in Q0).
 *
 * VALIDATION (chips/fpga/oms_meister_ref.py, the bit-identical twin, on the full
 * oms-meister clips rescaled 240x180->126x112): LIF-spike fraction of batches
 * global_only 0.005%, object_coherent 0.006%, object_independent 0.008% --
 * near-silent everywhere, independent fires 1.8x more than global_only / 1.4x
 * more than coherent and LOCALIZES the hottest cell on the object (row0,col9 vs
 * (0,0) for the silent controls). The MODEST 1.4-1.8x margin is the HONEST
 * limitation above made quantitative: rate-based OMS is a weak discriminator,
 * and the 8x spatial downscale to 16x16 (needed to fit 32 KB) spreads the object
 * over a large frame fraction, further eroding the center/surround contrast.
 *
 * OUTPUT WORD (reuses dvs_motion's {flag,val,row,col} 15-bit layout so the
 * existing dvs_motion_view.py unpack_status() and the e2e harness style apply):
 *     bit14      = oms  (1 if the hottest cell's OMS drive cleared THRESHOLD:
 *                        an independently-moving object was detected this batch)
 *     bits[13:6] = val  (hottest cell's 8-bit OMS drive z, saturated to 255)
 *     bits[5:3]  = row  (hottest subunit's row>>1, 3-bit -- 16 rows folded to 8)
 *     bits[2:0]  = col  (hottest subunit's col>>1, 3-bit -- 16 cols folded to 8)
 *   One status word per BATCH-sized batch, exactly like dvs_motion. row/col are
 *   the coarse (8x8) location of the hottest OMS cell; val is its drive.
 *
 * A bit-identical INTEGER Python twin lives at chips/fpga/oms_meister_ref.py;
 * it validates this exact arithmetic against the oms-meister data/ npz clips
 * (near-silent on global_only/object_coherent, fires on object_independent).
 * =========================================================================== */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI (evt_pack.v / dvs_track): x=(w>>24)&0x7F, y=(w>>17)&0x7F,
   pol=w&1, ts=(w>>1)&0xFFFF. */
#define X_SHIFT 24
#define Y_SHIFT 17

/* --- NOISE STRATEGY (SciDVS is 126x112 and VERY noisy) ---
   OMS is inherently noise-robust by construction, so no per-event correlation
   gate is added here (it would blunt the very edges OMS relies on). Instead the
   suppression is built into the model, all multiply-free:
     - THETA_RECT: a half-wave-rectify DEAD-ZONE on the bandpass, so sub-threshold
       jitter/background contributes zero drive (sigma-scaled noise floor).
     - tanh COMPRESSION: one hot pixel can't dominate the wide surround pool.
     - DIVISIVE inhibition by the wide, delayed surround: uniform background/noise
       lifts denominator and numerator together and divides out.
     - The slow ADAPTIVE global threshold (gthr, the reference's p90 AGC): tracks
       the array-wide drive and subtracts it, so a noisy-but-uniform frame nets to
       ~0 everywhere. Only a spatially-localized, correlated object survives.
   THETA_RECT / SIGMA_FLOOR / THRESHOLD are the tunable noise knobs. */

/* --- Subunit grid (rescaled from the reference's 240x180/BLK4 = 60x45). ---
   BLK=8 (power of two -> shift-index, no divide): col=x>>3, row=y>>3.
   126>>3 = 15 and 112>>3 = 13, so cols land in 0..15, rows in 0..13. We use a
   16x16 grid (256 subunits): the two unused bottom rows (14,15) simply stay
   empty. 16 is a power of two so cell = (row<<GW_SHIFT)|col is shift-only. */
#define BLK_SHIFT 3
#define GW        16                 /* grid width  (cols), power of two */
#define GH        16                 /* grid height (rows), padded to 16 */
#define GW_SHIFT  4                  /* log2(GW): cell = (row<<4)|col */
#define NCELL     (GW * GH)          /* 256 subunits */

/* --- Leaky-integrator (DoE bandpass) as two DC-matched EMA low-passes. ---
   The reference bins at BIN=2 ms and feeds an event-COUNT field into two leaky
   integrators with different tau, then bandpass b = s_fast - s_slow. The two
   poles MUST have equal DC gain or b never goes positive; the clean multiply-
   free way is an EMA that tracks the same input with different time constants:

       s_fast += (inp - s_fast) >> K_FAST     (fast pole, short tau)
       s_slow += (inp - s_slow) >> K_SLOW     (slow pole, long tau)
       b       = s_fast - s_slow              (difference of exponentials)

   Both s_fast and s_slow settle to `inp` at DC (equal DC gain -> b=0 at rest);
   on a burst of events the FAST pole leads the slow one, so b>0 transiently --
   exactly the DoE bandpass. `>>k` is the multiply-free stand-in for the (1-a)
   EMA coefficient; k sets tau (per-BATCH cadence, an event-count proxy for time
   just as dvs_motion decays its grid per batch):
     fast: K_FAST=2 -> coeff 1/4  (short tau ~ tau_fast=12 ms)
     slow: K_SLOW=5 -> coeff 1/32 (long  tau ~ tau_slow=120 ms)
   (ratio 32/4 = 8 ~ 120/12 = 10, the DoE tau ratio). */
#define K_FAST 2
#define K_SLOW 5

/* Per-event input gain into the EMA. Each event this batch adds INP_GAIN to the
   cell's per-batch input `inp` before the EMA update, lifting the sparse event
   counts (~0-4 per cell per batch) into a range where the bandpass b clears the
   THETA_RECT dead-zone during genuine motion. Steady state s ~ mean(inp), well
   inside int16 for realistic rates. */
#define INP_GAIN 64

/* Half-wave rectify dead-zone (theta ~ 2.5*sigma_noise, in bandpass units).
   Subtracted from the bandpass before rectification, per polarity. */
#define THETA_RECT 6

/* tanh compression: rc = tanh_lut[min(r, 255)], Q8 (0..256). Built in ROM. */
static const uint16_t tanh_lut[256] = {
  /* tanh(i/64) * 256, i=0..255. Saturates toward 256 (==1.0 in Q8). Multiply-
     free at run time: index by the rectified value (clamped to 255). */
    0,  4,  8, 12, 16, 20, 24, 28, 32, 36, 39, 43, 47, 51, 55, 59,
   63, 66, 70, 74, 78, 81, 85, 89, 92, 96,100,103,107,110,114,117,
  121,124,128,131,135,138,141,145,148,151,154,158,161,164,167,170,
  173,176,179,182,185,188,191,194,197,199,202,205,208,210,213,216,
  218,221,223,226,228,231,233,235,238,240,242,244,246,248,250,252,
  254,256,258,260,262,263,265,267,268,270,271,273,274,276,277,278,
  280,281,282,283,285,286,287,288,289,290,291,292,293,294,295,296,
  296,297,298,299,300,300,301,302,302,303,304,304,305,305,306,306,
  307,308,308,309,309,309,310,310,311,311,312,312,312,313,313,313,
  314,314,314,315,315,315,315,316,316,316,316,317,317,317,317,317,
  318,318,318,318,318,318,319,319,319,319,319,319,319,320,320,320,
  320,320,320,320,320,320,321,321,321,321,321,321,321,321,321,321,
  321,321,321,322,322,322,322,322,322,322,322,322,322,322,322,322,
  322,322,322,322,322,322,323,323,323,323,323,323,323,323,323,323,
  323,323,323,323,323,323,323,323,323,323,323,323,323,323,323,323,
  323,323,323,323,323,323,323,323,323,323,323,323,323,323,323,323
};

/* Reciprocal LUT: recip[i] = round(2^15 / i) for i=1..RECIP_N-1 (recip[0]=0).
   Used for (a) unit-DC pool normalization (divide a box sum by its area) and
   (b) the divisive denominator 1/(sigma_floor+alpha*S). x/y == (x*recip[y])>>15
   for 1<=y<RECIP_N. Built in main() (no float/divide at run time; the build is
   the only place a divide happens, and it's the C compiler's, not the core's --
   see main()'s note: we fill it with a multiply-free restoring loop instead). */
#define RECIP_N 512
#define RECIP_Q 15
static uint16_t recip[RECIP_N];

/* Divisive-inhibition parameters (denominator in pooled Q8 units).
   z = (E << EGAIN) / (SIGMA_FLOOR + ALPHA*S) - THETA_G, rectified.  ALPHA=1
   (best in the reference sweep) so alpha*S is just S -- no multiply. EGAIN lifts
   the normalized center pool E (a small Q8 average) so the reciprocal-LUT
   divide lands z in the LIF's useful band; SIGMA_FLOOR is the small denominator
   floor (the contrast/event-rate AGC that keeps the divider finite at rest,
   analogous to the reference's 0.02). */
#define SIGMA_FLOOR 4           /* denominator floor (contrast/rate AGC) */
#define EGAIN       3           /* left-shift on E before the divide (Q gain) */
#define THETA_G     2           /* subtractive global threshold on z */

/* --- Slow ADAPTIVE global output threshold (the reference's "p90" AGC). ---
   This is the second silence mechanism and, empirically, the load-bearing one
   for global-motion rejection at this coarse 16x16 grid: it tracks a slow EMA
   of the array-wide peak drive and SUBTRACTS it from every cell's z before the
   LIF. Under coherent GLOBAL motion the whole array is uniformly elevated -> the
   threshold rises to meet it -> every cell nets to ~0 -> SILENCE. Under
   INDEPENDENT object motion only the object's cells exceed the (background-set)
   threshold -> they survive and fire. Multiply-free: the EMA is a shift-leak and
   the 5/4 gain is (thr + thr>>2). Tracked in gthr (a running Q0 state). */
#define K_GTHR    4             /* adaptive-threshold EMA leak: thr += (peak-thr)>>4 */

/* LIF OMS cell: membrane leaks by >>K_LIF each batch, integrates z, fires at
   VTH, then holds a short refractory. Multiply-free. */
#define K_LIF     2             /* membrane leak: v -= v>>2 (~tau) */
#define LIF_VTH   64            /* fire threshold on the membrane */
#define LIF_REFRAC 3            /* refractory batches after a spike */

/* Output detection threshold on the hottest cell's reported drive. */
#define THRESHOLD 40

/* ---- State (all in .bss; see the memory budget in the header/report). ----
   Per subunit: two integrators x2 polarities, plus one LIF membrane + refrac.
   The SAT is (GH+1)x(GW+1) int32. Everything below sums well under 32 KB. */
static int16_t s_fast_on[NCELL];
static int16_t s_slow_on[NCELL];
static int16_t s_fast_off[NCELL];
static int16_t s_slow_off[NCELL];
static uint8_t lif_v[NCELL];
static uint8_t lif_refrac[NCELL];

/* rc (tanh-compressed rectified drive) per cell, summed ON+OFF -- rebuilt each
   batch, feeds the SAT. Q8. */
static uint16_t rc[NCELL];

/* per-batch input to the EMA integrators (event count * INP_GAIN), per polarity.
   Only the <=BATCH touched cells are set/cleared each batch (kept sparse). */
static int16_t inp_on[NCELL];
static int16_t inp_off[NCELL];

/* Summed-area table of rc, (GH+1) x (GW+1). sat[r][c] = sum of rc over the
   rectangle [0..r-1]x[0..c-1]. int32: max ~ 256cells*323(Q8) ~ 83k, fits. */
static int32_t sat[GH + 1][GW + 1];

/* Delay-matched surround: keep the previous batch's per-cell surround pool so
   the divisive denominator uses S_delayed (surround lags the center, as in the
   reference's SURR_DELAY). One batch of delay is the coarse stand-in. */
static uint16_t s_surr_prev[NCELL];

/* Slow adaptive global output threshold state (EMA of array-wide peak z). */
static int32_t gthr;

/* Center/surround pool geometry, in subunit (cell) units. Center is a small
   box (approx the narrow Gaussian); surround is a wide box MINUS the center box
   (an annulus that EXCLUDES the center -- the invariant that makes global
   motion cancel). Radii scaled from the reference (sigma_c=5, ann 6..22 at
   60x45) down to this 16x16 grid. */
#define RC_CTR 3                 /* center half-width: 7x7 box  (narrow-ish, sigma_c) */
#define RC_INN 2                 /* annulus inner half-width: 5x5 hole */
#define RC_OUT 5                 /* annulus outer half-width: 11x11 box (wide) */

static inline int32_t clampi(int32_t v, int32_t lo, int32_t hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

/* Multiply-free small multiply (shift-and-add over the multiplier's bits). The
   core is RV32I (no `mul`); the earlier "for(i<w) a+=area" idiom is legal C but
   -O3 strength-reduces it straight back into a __mulsi3 call, so we do an
   explicit bitwise shift-add that the optimizer cannot collapse. Both operands
   are small here (grid areas <= 121, coords <= 16), so the loop is a handful of
   iterations. Marked noinline so the compiler can't re-derive a multiply across
   the inlined call site. */
static __attribute__((noinline)) int32_t mul_small(int32_t a, int32_t b) {
    int32_t r = 0;
    uint32_t m = (b < 0) ? (uint32_t)(-b) : (uint32_t)b;
    int32_t addend = a;
    while (m) {
        if (m & 1u) r += addend;
        addend <<= 1;
        m >>= 1;
    }
    return (b < 0) ? -r : r;
}

/* Box sum over the inclusive cell rectangle [r0..r1] x [c0..c1] via the SAT.
   O(1): four table lookups, three adds. Coords are clamped into the grid. */
static inline int32_t box_sum(int32_t r0, int32_t c0, int32_t r1, int32_t c1) {
    r0 = clampi(r0, 0, GH - 1); r1 = clampi(r1, 0, GH - 1);
    c0 = clampi(c0, 0, GW - 1); c1 = clampi(c1, 0, GW - 1);
    /* +1 because sat is exclusive-upper. */
    return sat[r1 + 1][c1 + 1] - sat[r0][c1 + 1] - sat[r1 + 1][c0] + sat[r0][c0];
}

/* Unit-DC normalize a box sum: divide by the (clamped) window area using the
   reciprocal LUT.  norm = sum / area = (sum * recip[area]) >> RECIP_Q.
   area is always < RECIP_N here (max 11*11=121). No divide instruction. */
static inline int32_t norm_area(int32_t sum, int32_t r0, int32_t c0,
                                int32_t r1, int32_t c1) {
    int32_t rc0 = clampi(r0, 0, GH - 1), rc1 = clampi(r1, 0, GH - 1);
    int32_t cc0 = clampi(c0, 0, GW - 1), cc1 = clampi(c1, 0, GW - 1);
    int32_t h = (rc1 - rc0 + 1);                 /* height */
    int32_t w = (cc1 - cc0 + 1);                 /* width  */
    int32_t a = mul_small(h, w);                 /* area = h*w, multiply-free */
    if (a <= 0) return 0;
    if (a >= RECIP_N) a = RECIP_N - 1;
    return mul_small(sum, (int32_t)recip[a]) >> RECIP_Q;  /* sum/area, Q15 recip */
}

/* Must NOT call wfi() itself -- see dvs_motion/main.c's isr_handler comment:
   soc.act's WFI-decode never returns to the following instruction, so a wfi()
   inside the ISR permanently skips its own epilogue (the sp restore), leaking
   16 bytes of stack per interrupt until it collides with this program's code.
   Just returning is correct: this function's `ret` lands on the same cached
   wfi() site main()'s return already relies on. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* --- 1. Build this batch's per-cell input field inp = (event count)*INP_GAIN
       per polarity. Sparse: only the (<=BATCH) touched cells are nonzero. We set
       them here and clear them at the end of step 2 so the array stays zero for
       the untouched majority (no full-array clear needed). --- */
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x   = (v[i] >> X_SHIFT) & 0x7F;
        uint32_t y   = (v[i] >> Y_SHIFT) & 0x7F;
        uint32_t pol = v[i] & 1;
        uint32_t col = x >> BLK_SHIFT;
        uint32_t row = y >> BLK_SHIFT;
        if (col >= GW) col = GW - 1;
        if (row >= GH) row = GH - 1;
        uint32_t cell = (row << GW_SHIFT) | col;
        if (pol) inp_on[cell]  = (int16_t)(inp_on[cell]  + INP_GAIN);
        else     inp_off[cell] = (int16_t)(inp_off[cell] + INP_GAIN);
    }

    /* --- 2. EMA-update BOTH poles of BOTH polarities toward inp with different
       time constants: s += (inp - s) >> k. Untouched cells have inp==0, so this
       reduces to the shift-leak s -= s>>k -- one unified DC-matched pass, the
       multiply-free difference-of-exponentials. --- */
    for (uint32_t c = 0; c < NCELL; c++) {
        int32_t io = (int32_t)inp_on[c], iof = (int32_t)inp_off[c];
        s_fast_on[c]  = (int16_t)((int32_t)s_fast_on[c]  + ((io  - (int32_t)s_fast_on[c])  >> K_FAST));
        s_slow_on[c]  = (int16_t)((int32_t)s_slow_on[c]  + ((io  - (int32_t)s_slow_on[c])  >> K_SLOW));
        s_fast_off[c] = (int16_t)((int32_t)s_fast_off[c] + ((iof - (int32_t)s_fast_off[c]) >> K_FAST));
        s_slow_off[c] = (int16_t)((int32_t)s_slow_off[c] + ((iof - (int32_t)s_slow_off[c]) >> K_SLOW));
    }
    /* clear the sparse input field for next batch (only the touched cells). */
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x = (v[i] >> X_SHIFT) & 0x7F, y = (v[i] >> Y_SHIFT) & 0x7F;
        uint32_t col = x >> BLK_SHIFT, row = y >> BLK_SHIFT;
        if (col >= GW) col = GW - 1;
        if (row >= GH) row = GH - 1;
        uint32_t cell = (row << GW_SHIFT) | col;
        inp_on[cell] = 0; inp_off[cell] = 0;
    }

    /* --- 3. Per subunit: bandpass b = fast - slow, half-wave RECTIFY (with the
       theta dead-zone) BEFORE pooling, per polarity, then tanh-compress; sum
       ON+OFF into rc[] (the pooling invariant: rectify each polarity first). --- */
    for (uint32_t c = 0; c < NCELL; c++) {
        int32_t b_on  = (int32_t)s_fast_on[c]  - (int32_t)s_slow_on[c];
        int32_t b_off = (int32_t)s_fast_off[c] - (int32_t)s_slow_off[c];
        int32_t r_on  = b_on  - THETA_RECT;  if (r_on  < 0) r_on  = 0;
        int32_t r_off = b_off - THETA_RECT;  if (r_off < 0) r_off = 0;
        if (r_on  > 255) r_on  = 255;
        if (r_off > 255) r_off = 255;
        /* tanh compress each polarity separately, then sum -- keeps ON/OFF
           matched through the nonlinearity, no cross cancellation. */
        rc[c] = (uint16_t)(tanh_lut[r_on] + tanh_lut[r_off]);
    }

    /* --- 4. Build the summed-area table of rc (integral image). Row-prefix +
       column-prefix, all adds. --- */
    for (uint32_t cc = 0; cc <= GW; cc++) sat[0][cc] = 0;
    for (uint32_t r = 0; r < GH; r++) {
        int32_t rowsum = 0;
        sat[r + 1][0] = 0;
        for (uint32_t cc = 0; cc < GW; cc++) {
            rowsum += (int32_t)rc[(r << GW_SHIFT) | cc];
            sat[r + 1][cc + 1] = sat[r][cc + 1] + rowsum;
        }
    }

    /* --- 5. Per subunit: center pool E (narrow box), surround pool S (wide box
       MINUS center-exclusion box = annulus). Both unit-DC normalized. Then the
       DIVISIVE inhibition with the delayed, shared wide surround:
          z = E * recip(SIGMA_FLOOR + S_delayed) >> RECIP_Q  - THETA_G   [+]
       and drive the per-subunit LIF; track the hottest firing cell. --- */
    uint32_t best_cell = 0, best_val = 0, any_fire = 0;
    int32_t batch_peak = 0;   /* array-wide peak raw-z, feeds the adaptive thr */
    for (uint32_t r = 0; r < GH; r++) {
        for (uint32_t cc = 0; cc < GW; cc++) {
            uint32_t cell = (r << GW_SHIFT) | cc;

            /* center excitation E (narrow, normalized) */
            int32_t e_sum = box_sum(r - RC_CTR, cc - RC_CTR, r + RC_CTR, cc + RC_CTR);
            int32_t E = norm_area(e_sum, r - RC_CTR, cc - RC_CTR, r + RC_CTR, cc + RC_CTR);

            /* surround annulus S = wide box - inner hole box, normalized by the
               annulus area (outer area - inner area). */
            int32_t out_sum = box_sum(r - RC_OUT, cc - RC_OUT, r + RC_OUT, cc + RC_OUT);
            int32_t inn_sum = box_sum(r - RC_INN, cc - RC_INN, r + RC_INN, cc + RC_INN);
            int32_t ann_sum = out_sum - inn_sum;
            /* annulus area via the two clamped window areas. */
            int32_t or0 = clampi(r - RC_OUT, 0, GH - 1), or1 = clampi(r + RC_OUT, 0, GH - 1);
            int32_t oc0 = clampi(cc - RC_OUT, 0, GW - 1), oc1 = clampi(cc + RC_OUT, 0, GW - 1);
            int32_t ir0 = clampi(r - RC_INN, 0, GH - 1), ir1 = clampi(r + RC_INN, 0, GH - 1);
            int32_t ic0 = clampi(cc - RC_INN, 0, GW - 1), ic1 = clampi(cc + RC_INN, 0, GW - 1);
            int32_t oarea = mul_small(or1 - or0 + 1, oc1 - oc0 + 1);
            int32_t iarea = mul_small(ir1 - ir0 + 1, ic1 - ic0 + 1);
            int32_t aarea = oarea - iarea;
            int32_t S;
            if (aarea <= 0) {
                S = 0;
            } else {
                if (aarea >= RECIP_N) aarea = RECIP_N - 1;
                S = mul_small(ann_sum, (int32_t)recip[aarea]) >> RECIP_Q;
            }

            /* delay-match: use last batch's surround in the denominator, store
               this batch's for next time (S_delayed). */
            int32_t S_del = (int32_t)s_surr_prev[cell];
            s_surr_prev[cell] = (uint16_t)clampi(S, 0, 65535);

            /* DIVISIVE inhibition (shared wide denominator), reciprocal LUT. */
            int32_t denom = SIGMA_FLOOR + S_del;          /* ALPHA=1 */
            if (denom < 1) denom = 1;
            if (denom >= RECIP_N) denom = RECIP_N - 1;
            /* z = (E<<EGAIN)/denom - THETA_G, rectified. E is a normalized Q8
               average, recip is Q15: (E*recip)>>RECIP_Q == E/denom, and the
               EGAIN left-shift lifts that quotient into the LIF's useful band.
               Combined: >> (RECIP_Q - EGAIN). */
            int32_t z = mul_small(E, (int32_t)recip[denom]) >> (RECIP_Q - EGAIN);
            z = z - THETA_G;
            if (z < 0) z = 0;

            /* raw-z array peak this batch (drives the adaptive threshold EMA). */
            if (z > batch_peak) batch_peak = z;

            /* Slow ADAPTIVE global threshold, delay-matched (uses last batch's
               gthr, updated after the loop). t = gthr*5/4 = gthr + gthr>>2 --
               multiply-free. Subtract it: under global motion the whole array is
               lifted so t rises to cancel it; localized object drive survives. */
            int32_t t = gthr + (gthr >> 2);
            int32_t z2 = z - t;
            if (z2 < 0) z2 = 0;

            /* --- LIF OMS cell: leak, integrate z2, fire+refractory. --- */
            if (lif_refrac[cell] > 0) {
                lif_refrac[cell]--;
            } else {
                int32_t vv = (int32_t)lif_v[cell];
                vv = vv - (vv >> K_LIF) + z2;      /* leaky integrate */
                if (vv >= LIF_VTH) {
                    vv = 0;
                    lif_refrac[cell] = LIF_REFRAC;
                    any_fire = 1;
                }
                if (vv > 255) vv = 255;
                lif_v[cell] = (uint8_t)vv;
            }

            /* track hottest (post-adaptive) OMS drive for the status word. */
            if (z2 > (int32_t)best_val) {
                best_val = (uint32_t)z2;
                best_cell = cell;
            }
        }
    }

    /* update the slow adaptive global threshold from this batch's array peak. */
    gthr = gthr + ((batch_peak - gthr) >> K_GTHR);

    /* --- 6. Emit one status word for this batch (dvs_motion's layout). --- */
    uint32_t best_row = best_cell >> GW_SHIFT;      /* 0..15 */
    uint32_t best_col = best_cell & (GW - 1);       /* 0..15 */
    uint32_t val = best_val > 255 ? 255 : best_val;
    uint32_t oms = (val >= THRESHOLD || any_fire) ? 1u : 0u;
    /* fold 16 rows/cols into the 3-bit fields by >>1 (8 coarse regions each). */
    *FIFO_OUT = (oms << 14) | (val << 6) | ((best_row >> 1) << 3) | (best_col >> 1);
}

void main(void) {
    /* zero state */
    for (uint32_t c = 0; c < NCELL; c++) {
        s_fast_on[c] = 0; s_slow_on[c] = 0;
        s_fast_off[c] = 0; s_slow_off[c] = 0;
        lif_v[c] = 0; lif_refrac[c] = 0;
        rc[c] = 0; s_surr_prev[c] = 0;
        inp_on[c] = 0; inp_off[c] = 0;
    }
    gthr = 0;

    /* Build the reciprocal LUT recip[i] = round(2^15 / i), i>=1, WITHOUT a
       divide instruction (this core is rv32i). Restoring long division by
       repeated subtract-and-shift would be one option; simpler and exact for
       our small RECIP_N: for each i, subtract i from a running numerator until
       it goes negative, counting quotient bits. We do the classic shift-add
       reciprocal: q = floor(2^15 / i) via bitwise long division. */
    recip[0] = 0;
    for (uint32_t i = 1; i < RECIP_N; i++) {
        uint32_t num = 1u << RECIP_Q;   /* 2^15 */
        uint32_t q = 0, rem = 0;
        for (int32_t b = RECIP_Q; b >= 0; b--) {
            rem = (rem << 1) | ((num >> b) & 1u);
            if (rem >= i) { rem -= i; q |= (1u << b); }
        }
        /* round-to-nearest: if 2*rem >= i, bump. */
        if ((rem << 1) >= i) q += 1;
        recip[i] = (uint16_t)q;
    }

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
