#include <stdint.h>

/* "NECROPSY OF A POP" (dvs_necropsy) -- a chips/fpga demo app.
   Pattern: identical to software/dvs_vital/main.c (fifo_in fires event_id_0
   once BATCH words land; isr_handler reads them, updates the burst/tear
   state, writes ONE status word per batch; it NEVER calls wfi()).

   Idea (balloon-burst autopsy -- novel among these apps): a balloon pop is
   invisible violence to a normal camera but a leisurely µs-stamped parade to
   a DVS.  This app detects the burst and, during a burst, measures the
   tear-front expansion speed: the horizontal extent (max_x - min_x) of active
   events grows as the rupture races across the frame.  The peak Δextent per
   BIN_BATCHES batches is the tear-front speed in pixels per time-bin, reported
   every batch.  A separate rolling baseline gate prevents slow scene motion or
   steady sparkle from falsely triggering a burst.

   -------------------------------------------------------------------------
   BURST DETECTION -- multiply-free, shift/add/sub/compare only:
     The event-rate counter (rate_count) accumulates events per RATE_WIN
     batches.  At each window boundary:
       rate_count is compared against RATE_THRESH (absolute floor).
       A slow-decaying baseline tracks the long-run rate:
         baseline = baseline - (baseline >> BASELINE_SHR) + (rate_count >> BASELINE_SHR)
       A burst fires when BOTH:
         (1) rate_count >= RATE_THRESH   (minimum absolute density)
         (2) rate_count >= baseline + (baseline >> 1)   (150% of baseline,
             i.e. a factor-of-1.5 spike; computed as sub + (sub>>1) where
             sub=baseline, no multiply; equivalent to 3*baseline/2)
       burst_active is set for one MEASURE_WIN * BIN_BATCHES batch window after
       the trigger; no latching until burst clears.

   -------------------------------------------------------------------------
   TEAR-FRONT SPEED -- multiply-free, shift/add/sub/compare only:
     During a burst, every BIN_BATCHES batches we record a time-bin:
       - track min_x and max_x of x-coordinates among events in the bin
       - extent = max_x - min_x (0..125)
       - delta_extent = extent - prev_extent  (signed; negative if front collapses)
       - if delta_extent > peak_speed: peak_speed = delta_extent
     After MEASURE_WIN bins, the final peak_speed is latched as
     lat_peak_speed; extent is latched as lat_extent; burst_active clears.

   -------------------------------------------------------------------------
   NOISE GUARDS (SciDVS 126×112, very noisy):
     1. ABSOLUTE RATE GUARD (RATE_THRESH): steady-state noise below RATE_THRESH
        events/window never sets burst_active, regardless of the baseline.
     2. RELATIVE RATE GUARD (150% baseline): slow drift or gentle motion that
        lifts the long-run baseline proportionally never triggers -- the spike
        must be a factor of 1.5× above the long-run mean.  A slowly-moving
        object that raises baseline gradually never trips this gate.
     3. SPATIAL INCOHERENCE GUARD (peak_speed threshold in the viewer): random
        sparkle fires incoherently across the frame; its horizontal extent is
        not monotone (it fluctuates) so Δextent stays near zero even if
        rate_count crosses RATE_THRESH momentarily.  A burst with
        lat_peak_speed == 0 (or very small) is considered noise in the viewer.
     4. BASELINE DECAY: the long-run baseline is updated with a shift-and-add
        IIR (alpha = 1/BASELINE_SHR = 1/16 per RATE_WIN window).  This lets
        the baseline track slowly-varying background activity without multiply.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div).
   Every operation is a shift, add, sub, compare, or logical:
     - rate_count/RATE_WIN  : accumulate events; compare vs RATE_THRESH; no mul.
     - baseline IIR         : sub + shift + add; one shift each direction; no mul.
     - 150% threshold       : baseline + (baseline >> 1); shift + add; no mul.
     - extent               : max_x - min_x; pure subtract; no mul.
     - delta / peak         : subtract + compare; no mul.
     - output pack          : shifts and ORs; no mul.

   -------------------------------------------------------------------------
   Event ABI (evt_pack.v, identical across all dvs_* apps):
     x   = (word >> 24) & 0x7F   (0..125)  -- X_SHIFT=24  -- USED (extent)
     y   = (word >> 17) & 0x7F   (0..111)  -- Y_SHIFT=17  -- decoded, unused
     ts  = (word >>  1) & 0xFFFF (16-bit)  -- TS_SHIFT=1   -- decoded, unused
     pol =  word        & 1                -- POL_SHIFT=0  -- decoded, unused
   Only x drives the tear-front extent measurement.

   -------------------------------------------------------------------------
   Timebase: this app is event-ORDER and event-COUNT driven, NOT ts-driven.
   Batch boundaries are the time axis.  BIN_BATCHES batches form one tear-bin.
   MEASURE_WIN tear-bins form one burst measurement window.  Offline validation
   uses a deterministic injected stream, making --validate independent of
   real timestamps.

   -------------------------------------------------------------------------
   Output word layout (23 bits used):
     bits[ 5: 0] = seq        (6-bit batch counter mod 64, wraps; 0 = no valid
                               measurement yet, same semantic as wseq=0 in
                               dvs_vital -- host treats seq=0 as pre-valid)
     bits[ 6: 6] = burst      (1 = burst currently active or just completed)
     bits[14: 7] = peak_speed (0..125, peak Δextent per bin during last burst,
                               in pixels per BIN_BATCHES batches; saturated
                               at 127; 0 if no burst has occurred)
     bits[22:15] = extent     (0..125, current x-extent max_x-min_x for the
                               current bin; 0 outside a burst)
     bits[31:23] = 0
   Host unpacks these fields; see chips/fpga/dvs_necropsy_view.py's
   unpack_status().

   -------------------------------------------------------------------------
   Window timing: latch and seq advance happen BEFORE the emit within any
   window-close batch.  Consequently output word index i (0-based) carries
   seq == ((i + 1) >> SEQ_PERIOD_SHIFT) & SEQ_MASK, where the period in
   batches is MEASURE_WIN * BIN_BATCHES.  For the --validate check the host
   mirrors this arithmetic exactly. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor dimensions. */
#define SX 126
#define SY 112

/* Input event ABI. */
#define X_SHIFT  24
#define Y_SHIFT  17
#define TS_SHIFT  1

/* Tunables -- each under #ifndef so -D overrides work at compile time. */

/* Number of BATCH-sized batches per rate-counting window. */
#ifndef RATE_WIN
#define RATE_WIN 16
#endif

/* Absolute minimum event count per RATE_WIN to be considered a burst. */
#ifndef RATE_THRESH
#define RATE_THRESH 32
#endif

/* Baseline IIR shift: alpha = 1/2^BASELINE_SHR per rate window. */
#ifndef BASELINE_SHR
#define BASELINE_SHR 4
#endif

/* Number of batches per tear-front time-bin. */
#ifndef BIN_BATCHES
#define BIN_BATCHES 8
#endif

/* Number of tear-front time-bins per burst measurement window. */
#ifndef MEASURE_WIN
#define MEASURE_WIN 16
#endif

/* Non-tunable constants. */
#define SEQ_MASK   0x3Fu    /* 6-bit seq counter mask (0..63) */
#define SPEED_CAP  127u     /* saturating ceiling for peak_speed */
#define X_MIN_INIT 127u     /* sentinel for "no event yet this bin" */
#define X_MAX_INIT 0u       /* sentinel for "no event yet this bin" */

/* Events per RATE_WIN batches (rolling count). */
static uint32_t rate_count;

/* Long-run baseline (slow-decaying IIR, units: events per RATE_WIN batches). */
static uint32_t baseline;

/* Counter within the current rate window (0..RATE_WIN-1). */
static uint32_t rate_batch;

/* 1 if a burst is currently being measured; 0 otherwise. */
static uint32_t burst_active;

/* Current x-extent tracking over the current BIN_BATCHES tear-bin. */
static uint32_t bin_min_x;    /* smallest x seen this tear-bin */
static uint32_t bin_max_x;    /* largest  x seen this tear-bin */

/* Batch counter within the current tear-bin (0..BIN_BATCHES-1). */
static uint32_t bin_batch;

/* Tear-bin counter within the current measurement window (0..MEASURE_WIN-1). */
static uint32_t bin_count;

/* x-extent from the previous tear-bin (for Δextent). */
static uint32_t prev_extent;

/* Peak Δextent so far in the current burst measurement window. */
static uint32_t peak_speed;

/* Latched outputs -- emitted every batch, updated at latch boundaries.
   All 0 before the first latch; zeroed by crt0.S. */
static uint32_t lat_burst;
static uint32_t lat_peak_speed;
static uint32_t lat_extent;

/* 6-bit batch sequence counter (wraps mod 64).  Zeroed by crt0.S.
   seq=0 means "pre-valid" (no measurement completed yet). */
static uint32_t seq;

/* Helper: reset per-bin tracking state to sentinel values. */
static void reset_bin(void) {
    bin_min_x   = X_MIN_INIT;
    bin_max_x   = X_MAX_INIT;
    bin_batch   = 0u;
}

/* Must NOT call wfi(): see software/dvs_vital/main.c epilogue comment. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Decode x from each event and update:
         (a) rate_count for burst detection
         (b) bin_min_x / bin_max_x for tear-front extent during a burst */
    for (uint32_t i = 0u; i < BATCH; i++) {
        uint32_t x = (v[i] >> X_SHIFT) & 0x7Fu;   /* 0..125 */
        /* y   = (v[i] >> Y_SHIFT) & 0x7Fu; -- decoded per ABI but unused */
        /* ts  = (v[i] >> TS_SHIFT) & 0xFFFFu; -- decoded per ABI but unused */
        /* pol =  v[i] & 1u; -- decoded per ABI but unused */

        rate_count++;

        if (burst_active) {
            if (x < bin_min_x) bin_min_x = x;
            if (x > bin_max_x) bin_max_x = x;
        }
    }

    /* ----------------------------------------------------------------
       Rate window boundary -- runs every RATE_WIN batches.
       Update baseline IIR and check for burst trigger. */
    rate_batch++;
    if (rate_batch >= (uint32_t)RATE_WIN) {
        rate_batch = 0u;

        /* Update slow baseline with a shift-and-add IIR (no multiply):
             baseline += (rate_count - baseline) >> BASELINE_SHR
           equivalently: baseline = baseline - (baseline>>SHR) + (rate_count>>SHR)
           This decays toward rate_count with time-constant ~2^BASELINE_SHR windows. */
        baseline = baseline
                 - (baseline    >> BASELINE_SHR)
                 + (rate_count  >> BASELINE_SHR);

        /* Burst trigger: absolute density + 150%-of-baseline spike.
             threshold = baseline + (baseline >> 1)  = 3/2 * baseline (shift+add, no mul) */
        if (!burst_active) {
            uint32_t threshold = baseline + (baseline >> 1);
            if (rate_count >= (uint32_t)RATE_THRESH && rate_count >= threshold) {
                /* Burst fires: start tear-front measurement window. */
                burst_active = 1u;
                bin_count    = 0u;
                prev_extent  = 0u;
                peak_speed   = 0u;
                reset_bin();
            }
        }

        rate_count = 0u;
    }

    /* ----------------------------------------------------------------
       Tear-bin boundary (only runs when burst_active) -- every BIN_BATCHES batches.
       Advance within the measurement window; latch when window complete. */
    if (burst_active) {
        bin_batch++;
        if (bin_batch >= (uint32_t)BIN_BATCHES) {
            /* Close this tear-bin: compute extent and delta. */
            uint32_t extent = 0u;
            if (bin_max_x >= bin_min_x && bin_min_x != (uint32_t)X_MIN_INIT) {
                extent = bin_max_x - bin_min_x;
            }

            /* Δextent: signed advance of the tear front.
               We only track POSITIVE expansion (tear opening wider).
               Use saturating compare; no subtract that can underflow a
               uint32_t to a huge positive -- test first. */
            if (extent > prev_extent) {
                uint32_t delta = extent - prev_extent;   /* always positive here */
                if (delta > peak_speed) {
                    peak_speed = (delta <= (uint32_t)SPEED_CAP) ? delta : (uint32_t)SPEED_CAP;
                }
            }

            prev_extent = extent;
            bin_count++;

            /* Update latched extent for the current batch word. */
            lat_extent = extent;

            /* Reset bin tracking for the next tear-bin. */
            reset_bin();

            /* Measurement window complete? */
            if (bin_count >= (uint32_t)MEASURE_WIN) {
                /* Latch final outputs and clear burst state. */
                lat_burst      = 1u;
                lat_peak_speed = peak_speed;
                /* lat_extent already set above (last bin's extent). */

                burst_active   = 0u;
                bin_count      = 0u;
                prev_extent    = 0u;
                peak_speed     = 0u;
                reset_bin();

                seq = (seq + 1u) & SEQ_MASK;
                /* seq=0 is the wrap value; after the first completed window
                   seq becomes 1, which the host treats as "valid". */
            }
        }
    } else {
        /* Outside a burst: zero the per-batch extent so the host sees 0. */
        lat_burst  = 0u;
        lat_extent = 0u;
    }

    /* Emit ONE word per batch from the latched values.
       Layout: bits[5:0]=seq, bits[6]=burst, bits[14:7]=peak_speed,
               bits[22:15]=extent, bits[31:23]=0 */
    *FIFO_OUT = (lat_extent     << 15)
              | (lat_peak_speed <<  7)
              | (lat_burst      <<  6)
              |  seq;
}

void main(void) {
    /* .bss zeroed by crt0.S: rate_count, baseline, rate_batch, burst_active,
       bin_min_x (sentinel set at first reset_bin call), bin_max_x, bin_batch,
       bin_count, prev_extent, peak_speed, lat_burst, lat_peak_speed,
       lat_extent, seq -- all start 0.  bin_min_x starts 0 (not X_MIN_INIT),
       but burst_active is also 0, so the bin tracking is never read before
       the first burst resets it via reset_bin(). */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 */
    /* crt0.S executes wfi() for us when main() returns. */
}
