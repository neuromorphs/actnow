#include <stdint.h>

/* "Objects Have Secret Heartbeats" -- a chips/fpga demo app in the same shape
   as software/dvs_motion/main.c (fifo_in fires event_id_0 once BATCH words
   land; isr_handler reads them, updates state, writes output word(s), and
   returns -- NEVER calls wfi(), see the epilogue comment on isr_handler).

   Idea: every physical object that flickers, vibrates, or is PWM-driven emits
   DVS events at a characteristic *period*. This app estimates, per coarse
   spatial region, the dominant inter-event period and streams it out so a host
   (chips/fpga/dvs_heartbeats_view.py) can paint a period map and sonify each
   region as a "heartbeat"/tone. The chip only ever emits {region, period_bin,
   confidence}; all visualization/sound happens on the computer.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div, see
   software/common/program.mk). Everything below is compares, shifts, adds:
     - region index      : x>>REGION_SHIFT, y>>REGION_SHIFT, row*cols via shift
     - period-bin classify: a compare ladder on the inter-event delta (the bin
                            edges are powers of two, so it's shift-free too)
     - leaky decay        : bin -= bin>>DECAY_SHIFT   (periodic halving-ish)
     - confidence         : winner-vs-sum by compare (no divide)

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_track/main.c):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit ~microsecond timestamp, wraps)
     pol =  word        & 1
   (An earlier revision read x/y/ts from the low bits -- the STALE layout that
   the upstream dvs_motion/rotate still use; on the FPGA that reads the wrong
   bits. Match evt_pack.v + dvs_track. The chips/fpga mirror packs the same way.)

   The sensor frame is SX x SY. Regions are REGION_SIZE = 1<<REGION_SHIFT pixel
   squares: REGION_SHIFT=4 -> 16x16-px regions -> REGION_COLS=8 (126>>4=7, so
   col in 0..7) by REGION_ROWS=7 (112>>4=6, row in 0..6) = 56 regions. Like
   dvs_motion's CELL_SHIFT, REGION_SHIFT and the col/row counts must move
   together: a region index must stay < REGION_CELLS or it walks off region[].

   -------------------------------------------------------------------------
   Per region we keep:
     - last_ts      : timestamp of the previous event in that region (16-bit)
     - seen         : 0 until the first event arrives (so the first Delta,
                      which has no valid predecessor, is discarded)
     - bins[NBINS]  : NBINS=8 leaky counters, one per power-of-two period band
                      (see BIN_EDGES). On each event we bump the bin its
                      inter-event Delta falls into and decay the rest.

   On each event:
     1. dt = (ts - last_ts) & TS_MASK   -- masked subtract handles the 16-bit
        wrap for free (a wrapped Delta just comes out as a large positive
        number and lands in the top "too slow / aperiodic" bin, which is fine).
     2. classify dt into a bin by the compare ladder in classify_bin().
     3. decay every bin (bin -= bin>>DECAY_SHIFT), then add BUMP to the winner,
        saturating at BIN_CAP.

   Emission cadence: one status word per BATCH-sized batch of events (exactly
   like dvs_motion emits its argmax once per batch), for the region that the
   *last* event in the batch touched. We report that region's current argmax
   bin and a 4-bit confidence = how dominant the winning bin is versus the
   region's total (winner*8 >= total*k comparisons -- multiply-free, k folded
   into shifts). Output word:
     bits[5:0]   = region index   (0..55, 6 bits)
     bits[9:6]   = period_bin      (0..7, but 4 bits reserved)
     bits[13:10] = confidence      (0..8, 4 bits)
   Host unpacks these fields; see dvs_heartbeats_view.py's unpack_status().

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). Two multiply-free defences:
     (1) An OPTIONAL spatio-temporal correlation gate on the region-updating path
         (the same technique as software/dvs_track/main.c / dvs_denoise, jAER's
         SpatioTemporalCorrelationFilter): an event bumps its region's period
         bins only if >= CORR_MIN of its 8 region-neighbours (EXCLUDING self, so
         a hot pixel finds no support) fired within the last CORR_WINDOW events.
         This stops an isolated hot pixel or scattered background event from
         manufacturing a spurious inter-event Delta and a fake heartbeat. Tunable
         via -DCORR_MIN / -DCORR_WINDOW; CORR_MIN=0 disables it (default: on).
     (2) The 4-bit CONFIDENCE field (winner bin vs region total) lets the host
         reject a region whose activity is spread across bins (no coherent period
         -> low confidence); the mirror's --validate trusts only conf>=4.
   The gate reuses the region grid, so it costs one uint32 per region (56*4=224 B).

   -------------------------------------------------------------------------
   TIMESTAMP note: evt_pack.v now supplies a REAL, live 16-bit timestamp, so on
   hardware / a live AER stream period detection works directly. The recorded
   actnow CSVs (chips/fpga) still store `le`, a wrapped coarse event counter --
   NOT a usable microsecond ts -- so the RECORDED-CSV path is not meaningful for
   period detection; use the live stream or the mirror's SYNTHETIC self-test.
   The Python reference validates the identical integer logic on SYNTHETIC
   periodic events with KNOWN frequencies (see dvs_heartbeats_view.py --validate). */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

#define REGION_SHIFT 4                      /* 16x16-px regions. 126>>4=7 -> cols 0..7,
                                               112>>4=6 -> rows 0..6. REGION_SHIFT and the
                                               col/row counts below must change together
                                               (see dvs_motion/main.c's CELL_SHIFT note). */
#define REGION_COLS  8
#define REGION_ROWS  7
#define REGION_COL_SHIFT 3                  /* row*REGION_COLS via shift: REGION_COLS==8==1<<3 */
#define REGION_CELLS (REGION_COLS * REGION_ROWS)   /* = 56 */

/* Input event ABI (evt_pack.v / dvs_track). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* 16-bit timestamp field (evt_pack.v packs ts in bits [16:1]). */
#define TS_MASK 0xFFFFu

/* Optional spatio-temporal correlation noise gate (see NOISE STRATEGY in the
   header). Reuses the region grid. CORR_MIN=0 disables it. */
#ifndef CORR_WINDOW
#define CORR_WINDOW 30   /* events; a neighbour must have fired within this to count */
#endif
#ifndef CORR_MIN
#define CORR_MIN 2       /* of 8 region-neighbours that must be recent; 0 disables */
#endif

/* NBINS power-of-two period bands classified from the inter-event Delta.
   Bin i captures Delta in [BIN_EDGES[i-1], BIN_EDGES[i]); bin 0 = fastest.
   Edges are powers of two so classify_bin() is a pure compare ladder. The unit
   is whatever the ts LSB is (~1 us on the live chip); the host maps bin->Hz.

     bin  Delta range (ts units)     ~period if 1 LSB ~= 32 us
      0   [   0,   64)                ~ <2 ms      (fast flicker / high PWM)
      1   [  64,  128)                ~ 2-4 ms
      2   [ 128,  256)                ~ 4-8 ms
      3   [ 256,  512)                ~ 8-16 ms
      4   [ 512, 1024)                ~ 16-32 ms   (~30-60 Hz mains flicker)
      5   [1024, 2048)                ~ 32-65 ms
      6   [2048, 4096)                ~ 65-130 ms  (slow blink)
      7   [4096, +inf)               too slow / aperiodic

   The exact us-per-LSB is a host-side scale; the chip only emits the bin. */
#define NBINS 8

#define BUMP       16   /* activity added to the winning bin per event */
#define BIN_CAP    255  /* 8-bit saturation for a leaky counter */
#define DECAY_SHIFT 3   /* leak: bin -= bin>>3  (~1/8 per event) -- periodic halving-ish */

/* Per-region state. Kept in .bss (zeroed by crt0.S), so seen==0 and last_ts==0
   at start; the first event in each region just seeds last_ts and is skipped. */
static uint8_t  bins[REGION_CELLS][NBINS];
static uint32_t last_ts[REGION_CELLS];
static uint8_t  seen[REGION_CELLS];
#if CORR_MIN > 0
static uint32_t last_touched[REGION_CELLS];  /* event index each region last fired at; 0=never */
static uint32_t event_count;                 /* "now" for the correlation gate */
static int is_recent(uint32_t last, uint32_t nowc) {
    return (last != 0) && ((nowc - last) <= CORR_WINDOW);
}
#endif

/* Classify an inter-event Delta into one of NBINS power-of-two bands. A plain
   compare ladder: multiply-free, branch-predictable, and the edges being 2^k
   means the compiler emits bare immediates (no shift needed at the call site).
   Anything >= 4096 falls through to the top "aperiodic / too slow" bin. */
static uint32_t classify_bin(uint32_t dt) {
    if (dt <   64u) return 0;
    if (dt <  128u) return 1;
    if (dt <  256u) return 2;
    if (dt <  512u) return 3;
    if (dt < 1024u) return 4;
    if (dt < 2048u) return 5;
    if (dt < 4096u) return 6;
    return 7;
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

    uint32_t last_region = 0;

    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x  = (v[i] >> X_SHIFT) & 0x7F;
        uint32_t y  = (v[i] >> Y_SHIFT) & 0x7F;
        uint32_t ts = (v[i] >> 1)       & TS_MASK;

        uint32_t col = x >> REGION_SHIFT;
        uint32_t row = y >> REGION_SHIFT;
        uint32_t r   = (row << REGION_COL_SHIFT) | col;  /* row*REGION_COLS via shift */
        last_region = r;

#if CORR_MIN > 0
        /* Spatio-temporal correlation gate over the region grid: count recent
           touches in the 3x3 region neighbourhood EXCLUDING self (so a lone hot
           pixel/region finds no support). Record last_touched for every event
           (dropped or not) so a genuine new source can bootstrap. If the event
           is uncorrelated, still advance last_region (so the batch reports a
           real region) but DON'T let it perturb the period bins / last_ts. */
        event_count++;
        {
            uint32_t nc = 0;
            int has_l = col > 0, has_r = col < REGION_COLS - 1;
            int has_u = row > 0, has_d = row < REGION_ROWS - 1;
            if (has_l)          nc += is_recent(last_touched[r - 1],                event_count);
            if (has_r)          nc += is_recent(last_touched[r + 1],                event_count);
            if (has_u)          nc += is_recent(last_touched[r - REGION_COLS],      event_count);
            if (has_d)          nc += is_recent(last_touched[r + REGION_COLS],      event_count);
            if (has_l && has_u) nc += is_recent(last_touched[r - REGION_COLS - 1],  event_count);
            if (has_r && has_u) nc += is_recent(last_touched[r - REGION_COLS + 1],  event_count);
            if (has_l && has_d) nc += is_recent(last_touched[r + REGION_COLS - 1],  event_count);
            if (has_r && has_d) nc += is_recent(last_touched[r + REGION_COLS + 1],  event_count);
            last_touched[r] = event_count;
            if (nc < CORR_MIN) {
                continue;   /* uncorrelated -- don't feed the period estimator */
            }
        }
#endif

        if (seen[r]) {
            uint32_t dt = (ts - last_ts[r]) & TS_MASK;   /* masked sub -> 16-bit wrap safe */
            uint32_t b  = classify_bin(dt);

            /* Leaky decay of every bin, then bump the winner (saturating). */
            for (uint32_t k = 0; k < NBINS; k++) {
                bins[r][k] = (uint8_t)(bins[r][k] - (bins[r][k] >> DECAY_SHIFT));
            }
            uint32_t updated = bins[r][b] + BUMP;
            bins[r][b] = (uint8_t)((updated > BIN_CAP) ? BIN_CAP : updated);
        } else {
            seen[r] = 1;
        }
        last_ts[r] = ts;
    }

    /* Report the region the last event in this batch touched: its argmax bin
       and a confidence = how dominant that bin is vs the region's total. */
    uint32_t r = last_region;
    uint32_t best_bin = 0;
    uint32_t best_val = bins[r][0];
    uint32_t total    = bins[r][0];
    for (uint32_t k = 1; k < NBINS; k++) {
        uint32_t val = bins[r][k];
        total += val;
        if (val > best_val) {
            best_val = val;
            best_bin = k;
        }
    }

    /* Confidence in 0..8: how many eighths of the total the winner holds,
       computed by compares only (winner*8 >= total*conf  <=>  winner >= total
       scaled) -- but without a multiply. We instead accumulate total>>3 as a
       "one-eighth" unit and count how many units the winner covers. */
    uint32_t eighth = total >> 3;      /* total/8, floor -- shift, no divide */
    uint32_t conf   = 0;
    if (eighth == 0) {
        /* Region barely active: treat any winner as full confidence so a lone
           strong periodic source in a quiet region still reads as a heartbeat. */
        conf = (best_val > 0) ? 8u : 0u;
    } else {
        uint32_t acc = eighth;
        while (conf < 8u && best_val >= acc) {
            conf++;
            acc += eighth;
        }
    }

    /* bits[5:0]=region, bits[9:6]=period_bin, bits[13:10]=confidence. */
    *FIFO_OUT = (conf << 10) | (best_bin << 6) | r;
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so bins/last_ts/seen start clean. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
