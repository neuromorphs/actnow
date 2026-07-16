#include <stdint.h>

/* "BALLROOM SEISMOLOGY" (dvs_seismo) -- a chips/fpga demo in the same shape as
   software/dvs_vital/main.c (fifo_in fires event_id_0 once BATCH words land;
   isr_handler reads them, updates the edge-displacement integrator and
   oscillation statistics, latches a verdict every window, and writes ONE sample
   word per batch; it NEVER calls wfi(), see the epilogue comment on
   isr_handler).

   Idea: the camera stares at a fixed high-contrast vertical edge.  Sub-pixel
   motion of that edge produces signed events inside a thin vertical-column ROI:
   edge drifts RIGHT -> net ON events in the leading column, net OFF events in
   the trailing column; drifts LEFT -> the reverse.  Summing polarity (ON=+1,
   OFF=-1) over only the ROI columns in each batch approximates the spatial
   derivative of edge displacement for that time-bin.  Integrating the per-batch
   signed sum into a running displacement proxy D (with a slow leak to prevent
   drift) gives a signal that oscillates when the building / surface sways.

   CORE TRICK (multiply-free): over the thin vertical-edge ROI, the signed
   polarity sum per batch (ON=+1, OFF=-1) approximates the derivative of edge
   displacement; integrate it into a displacement proxy D with a slow leak
   (D -= D>>LEAK_K); estimate the oscillation frequency by counting sign
   zero-crossings of D over a window and mapping the count to a frequency label
   via a small LUT; a resonance meter = leaky sum of |D| (conditional-negate +
   add).  All shift/add/sub/compare/LUT, NO multiply.

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) Sinusoidal oscillation at a known frequency injected into the ROI.
         The recovered freqbin must fall within +/-1 of the expected bin;
         D must oscillate (sign alternates multiple times per window);
         resonance must be non-zero in the window that sees oscillation.
     (b) Static edge (ROI events balanced ON/OFF or zero): D stays near 0;
         freqbin=0; resonance stays near zero (below threshold).
     (c) No-ROI stream (all events outside ROI column band): same as static --
         roi_sum is always 0 so D never moves; freqbin=0; resonance=0.
     (d) Well-formedness: every output word has disp_q in [-128,127],
         freqbin in [0,31], resonance_q in [0,1023], seq in [0,15],
         upper 5 bits zero, word < 2^32.
     (e) WSEQ arithmetic: word index i (0-based) carries
         seq == ((i+1)/WINDOW_BATCHES) & 0xF.
     (f) Noise guard: random polarity stream inside the ROI has
         zero-mean signed sum -> D random-walks but mean resonance stays
         well below a clean sine of the same total event count.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div).
   Every operation is a shift, add, sub, compare, or LUT lookup:
     - roi_sum     : conditional add (+1 or -1) per event inside the column
                     band; no multiply.
     - disp update : disp += roi_sum; disp -= disp >> LEAK_K; both shifts and
                     adds; no multiply.
     - disp clamp  : compare + conditional assign; no multiply.
     - |disp|      : compare + conditional negate (subtract from 0); no mul.
     - resonance   : resonance += abs_disp; resonance -= resonance >> RES_LEAK;
                     shift and add; no multiply.
     - resonance   : saturating clamp; compare only.
     - zero-cross  : compare last sign vs current sign; no multiply.
     - freqbin LUT : ZC_COUNT >> ZC_SHIFT indexes a uint8_t[16] LUT; array
                     index is a shift; no multiply.
     - disp_q      : disp >> DISP_SHIFT clamped to [-128,127]; shift + 2 cmp.
     - resonance_q : resonance >> RES_SHIFT clamped to [0,1023]; shift + cmp.
     - output pack : (seq<<23)|(resonance_q<<13)|(freqbin<<8)|disp_q_u8;
                     shifts and ORs only; no multiply.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >>  1) & 0xFFFF   (16-bit timestamp) -- decoded but unused
     pol =  word        & 1                   -- ON=1 / OFF=0

   -------------------------------------------------------------------------
   ROI: only events with x in [X_ROI_LO, X_ROI_HI] contribute to roi_sum.
   This is the primary noise guard: hot pixels outside the edge band are
   completely ignored.  The ROI is intentionally narrow (ROI_WIDTH columns)
   so random background noise averages near zero per batch.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112, very noisy).  Three documented guards:
     1. ROI COLUMN GUARD.  Only events inside the thin column band [X_ROI_LO,
        X_ROI_HI] contribute to roi_sum.  Hot pixels or random noise outside
        the ROI contribute nothing.  Cost: two comparisons per event.
     2. ZERO-MEAN CANCELLATION.  Random noise inside the ROI has equally
        likely ON and OFF events.  The signed sum per batch (ON=+1, OFF=-1)
        cancels to near zero over many events; D barely moves.  This is not a
        guard per se but a property of the signed-sum integrator: it is
        insensitive to balanced noise by construction.
     3. ACTIVITY GATE.  The resonance accumulator measures |D|; a resonance
        threshold MIN_RES_VALID gates whether the freqbin output is meaningful.
        Below threshold the freqbin is forced to 0 (NO_OSC).  Random walk of
        D (noise) produces much smaller resonance than a coherent oscillation
        of the same amplitude; the threshold separates the two regimes.

   -------------------------------------------------------------------------
   TIMEBASE: this app is event-COUNT driven (like dvs_entropy / dvs_widdershins,
   not ts-driven like dvs_vital).  BATCH events constitute one update step;
   WINDOW_BATCHES steps constitute one analysis window.  No timestamp field is
   read by the algorithm (ts decoded per ABI but unused), so --validate can
   inject deterministic synthetic streams without real timestamps.

   -------------------------------------------------------------------------
   Frequency estimation (multiply-free).  A building sway at frequency f Hz
   produces roughly 2f zero-crossings per second.  With an event-count timebase
   the number of zero-crossings per window depends on the chip's event rate.
   Rather than converting to Hz (requires divide), we map the raw crossing
   count via a small LUT (16 entries, each covering a 2-crossing range) to a
   freqbin label 0..31 that is monotone in frequency.  freqbin=0 means "no
   oscillation detected" (either activity gate failed or count was zero).

   Frequency LUT (FREQ_LUT[i], i = min(zc_count >> 1, 15)):
     i=0  -> freqbin  0  (0..1 crossings -- static / no oscillation)
     i=1  -> freqbin  4  (2..3 crossings)
     i=2  -> freqbin  7  (4..5 crossings)
     i=3  -> freqbin  9  (6..7 crossings)
     i=4  -> freqbin 11  (8..9 crossings)
     i=5  -> freqbin 13  (10..11 crossings)
     i=6  -> freqbin 15  (12..13 crossings)
     i=7  -> freqbin 16  (14..15 crossings)
     i=8  -> freqbin 17  (16..17 crossings)
     i=9  -> freqbin 19  (18..19 crossings)
     i=10 -> freqbin 21  (20..21 crossings)
     i=11 -> freqbin 23  (22..23 crossings)
     i=12 -> freqbin 25  (24..25 crossings)
     i=13 -> freqbin 27  (26..27 crossings)
     i=14 -> freqbin 29  (28..29 crossings)
     i=15 -> freqbin 31  (30+ crossings)
   Monotone in zc_count; no multiply.

   -------------------------------------------------------------------------
   Window timing note: latch and seq advance happen BEFORE the emit on the
   closing batch of a window.  Output word index i (0-based) carries
   seq == ((i+1)/WINDOW_BATCHES) & 0xF.  The first WINDOW_BATCHES words
   (i=0..WINDOW_BATCHES-1) carry seq=0 with the initial zeroed latched state;
   word i=WINDOW_BATCHES-1 already carries the completed first window's stats
   and seq=1.  Host code should treat seq=0 as "not yet valid" or skip the
   first window if a clean start matters.

   -------------------------------------------------------------------------
   Output word layout (27 bits used):
     bits[ 7: 0] = disp_q   (signed 8-bit displacement proxy, two's complement;
                              host sign-extends: (int8_t)(word & 0xFF))
     bits[12: 8] = freqbin  (0..31, oscillation frequency label; 0 = no osc)
     bits[22:13] = resonance_q (0..1023, scaled resonance / energy)
     bits[26:23] = seq       (4-bit window sequence counter, wraps mod 16)
     bits[31:27] = 0
   Host unpacks these fields; see chips/fpga/dvs_seismo_view.py's
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

/* Input event ABI (evt_pack.v / dvs_flinch). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* ROI column band for edge detection (tuneable). */
#ifndef X_ROI_LO
#define X_ROI_LO 55            /* inclusive; column band left edge */
#endif
#ifndef X_ROI_HI
#define X_ROI_HI 70            /* inclusive; column band right edge */
#endif

/* Displacement integrator leak: D -= D >> LEAK_K each batch.
   LEAK_K=6 gives a time constant of ~64 batches = 256 events, slow enough
   to track sub-Hz sway but fast enough to prevent permanent drift. */
#ifndef LEAK_K
#define LEAK_K 6
#endif

/* Resonance accumulator leak: slower than LEAK_K to smooth the energy
   estimate over many batches. */
#ifndef RES_LEAK
#define RES_LEAK 5
#endif

/* Resonance saturation ceiling (avoid overflow; 24-bit headroom). */
#ifndef RES_CAP
#define RES_CAP 0xFFFFFF
#endif

/* Minimum resonance for the freqbin to be considered valid.
   Below this threshold freqbin is forced to 0 (no oscillation). */
#ifndef MIN_RES_VALID
#define MIN_RES_VALID 64
#endif

/* Displacement quantisation shift: disp_q = clamp(disp >> DISP_SHIFT, -128, 127). */
#ifndef DISP_SHIFT
#define DISP_SHIFT 3
#endif

/* Resonance quantisation shift: resonance_q = clamp(resonance >> RES_SHIFT, 0, 1023).
   RES_SHIFT=0 preserves the raw leaky-|D| accumulator value; the 1023 clamp
   handles the rare case of extreme uniform illumination (all events in ROI,
   one polarity).  For typical building-sway amplitudes the accumulator stays
   well below 1023. */
#ifndef RES_SHIFT
#define RES_SHIFT 0
#endif

/* Window length in batches.  One latch per window.  256 batches = 1024 events. */
#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 256
#endif

/* 4-bit window sequence counter mask. */
#define SEQ_MASK 0xFu

/* -------------------------------------------------------------------------
   Frequency LUT: FREQ_LUT[i] = freqbin for i = min(zc_count >> 1, 15).
   Monotone non-decreasing; freqbin=0 means no oscillation.
   Must match dvs_seismo_view.py's FREQ_LUT exactly. */
static const uint8_t FREQ_LUT[16] = {
     0,  4,  7,  9, 11, 13, 15, 16,
    17, 19, 21, 23, 25, 27, 29, 31
};

/* -------------------------------------------------------------------------
   Per-batch running state (all zeroed by crt0.S at cold start). */

/* Signed displacement proxy (integration of roi_sum). */
static int32_t disp;

/* Resonance accumulator (leaky sum of |disp|). */
static uint32_t resonance;

/* Sign of disp at the end of the previous batch (1 = positive/zero, 0 = negative). */
static uint32_t prev_sign;

/* Zero-crossing count in the current window. */
static uint32_t zc_count;

/* Batch-within-window counter (0..WINDOW_BATCHES-1). */
static uint32_t batch_in_window;

/* 4-bit window sequence counter (0..15, wraps). */
static uint32_t seq;

/* Latched fields from the last completed window (emitted every batch). */
static uint32_t lat_disp_u8;        /* (uint8_t)(int8_t)disp_q */
static uint32_t lat_freqbin;        /* 0..31 */
static uint32_t lat_resonance_q;    /* 0..1023 */

/* Must NOT call wfi() -- see dvs_vital/main.c epilogue comment. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Accumulate signed polarity sum over ROI column band.
       ON (pol=1) -> +1; OFF (pol=0) -> -1; outside ROI -> 0. */
    int32_t roi_sum = 0;
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x = (v[i] >> X_SHIFT) & 0x7Fu;
        /* y = (v[i] >> Y_SHIFT) & 0x7Fu -- decoded per ABI but unused */
        /* ts = (v[i] >> 1) & 0xFFFFu  -- decoded per ABI but unused */
        uint32_t pol = v[i] & 1u;
        if (x >= (uint32_t)X_ROI_LO && x <= (uint32_t)X_ROI_HI) {
            roi_sum += (pol != 0u) ? 1 : -1;
        }
    }

    /* Integrate roi_sum into displacement proxy, then apply leak. */
    disp += roi_sum;
    disp -= disp >> LEAK_K;    /* slow leak: keeps disp bounded near 0 for static scene */

    /* Clamp disp to a safe range to prevent runaway (32 ROI events max per
       batch, WINDOW_BATCHES batches, so theoretically up to ~8192 before leak
       saturates; clamp to +/-16384 is well inside int32_t). */
    if (disp >  16384) disp =  16384;
    if (disp < -16384) disp = -16384;

    /* Resonance: leaky accumulator of |disp|. */
    uint32_t abs_disp = (disp >= 0) ? (uint32_t)disp : (uint32_t)(-disp);
    resonance += abs_disp;
    resonance -= resonance >> RES_LEAK;
    if (resonance > (uint32_t)RES_CAP) resonance = (uint32_t)RES_CAP;

    /* Zero-crossing detection: current sign of disp (0=negative, 1=pos or zero). */
    uint32_t cur_sign = (disp >= 0) ? 1u : 0u;
    if (cur_sign != prev_sign) {
        zc_count++;
    }
    prev_sign = cur_sign;

    /* Advance batch-within-window counter; latch on window boundary (BEFORE emit). */
    batch_in_window++;
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;

        /* Quantise displacement: disp_q = clamp(disp >> DISP_SHIFT, -128, 127). */
        int32_t dq = disp >> DISP_SHIFT;
        if (dq >  127) dq =  127;
        if (dq < -128) dq = -128;
        lat_disp_u8 = (uint32_t)((uint8_t)(int8_t)dq);  /* two's-complement 8-bit */

        /* Frequency bin from zero-crossing count via LUT.
           Index = min(zc_count >> 1, 15); gate by resonance threshold. */
        uint32_t lut_idx = zc_count >> 1;
        if (lut_idx > 15u) lut_idx = 15u;
        if (resonance >= (uint32_t)MIN_RES_VALID) {
            lat_freqbin = FREQ_LUT[lut_idx];
        } else {
            lat_freqbin = 0u;
        }

        /* Quantise resonance: resonance_q = clamp(resonance >> RES_SHIFT, 0, 1023). */
        uint32_t rq = resonance >> RES_SHIFT;
        if (rq > 1023u) rq = 1023u;
        lat_resonance_q = rq;

        /* Clear per-window accumulators. */
        zc_count = 0u;

        seq = (seq + 1u) & SEQ_MASK;
    }

    /* Emit ONE word per batch from the LATCHED values only.
       Layout: bits[7:0]=disp_q, bits[12:8]=freqbin,
               bits[22:13]=resonance_q, bits[26:23]=seq, bits[31:27]=0 */
    *FIFO_OUT = (seq                << 23)
              | (lat_resonance_q    << 13)
              | (lat_freqbin        <<  8)
              |  lat_disp_u8;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- the correct cold start for ALL state
       in this app.  disp, resonance, prev_sign, zc_count, batch_in_window, seq,
       lat_disp_u8, lat_freqbin, lat_resonance_q all start at 0 without any
       explicit initialisation here. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
