#include <stdint.h>

/* "SHIBBOLETH" (dvs_shibboleth) -- identify a light by its PWM "accent".
   Most LED torches and phone flashlights are PWM-dimmed at some kHz rate.
   Eyes and frame cameras cannot see it.  A DVS can: it fires events at the
   dimmer's switching frequency, producing a sharply periodic inter-event-
   interval (IEI) distribution at the PWM period.  Two lights produce different
   periods; broadband motion or sparkle spreads across all IEI bins without a
   dominant peak (noise guard).  This firmware recovers the dominant IEI period
   bin for the hottest pixel region and reports it every window.

   -------------------------------------------------------------------------
   Algorithm (multiply-free, shift/add/sub/compare/LUT only):

   COARSE GRID: The sensor (126x112) is divided into N_CX * N_CY = 16 * 14 = 224
   non-overlapping cells (~8x8 pixels each).  Each cell has:
     cell_cnt[c]     uint8_t  -- event count this window (saturates at 255)
     cell_last_ts[c] uint16_t -- last event timestamp in this cell (16-bit)

   HOTTEST CELL: At each window boundary, scan cell_cnt[] for the maximum.
   The cell with the most events is "hot_cidx" for the NEXT window.  Using the
   previous window's hot cell avoids look-ahead and is deterministic.

   IEI HISTOGRAM (log-scale, 32 half-octave bins, exactly like dvs_vital's
   IBI histogram): For each event arriving in the hot cell during the current
   window, compute dt = (ts - cell_last_ts[hot_cidx]) & TS_MASK.  If dt >= 1
   and dt < IEI_MAX, call log2bin32(dt) -> bin, increment hist[bin].

   NOISE GUARD: At window boundary, compute peak = max(hist[]).  A true PWM
   source produces a sharp single-period peak.  Broadband motion / sparkle
   spreads counts across many bins.  Require peak >= (iei_total >> CONF_SHIFT),
   i.e., the peak bin must hold at least 1/(2^CONF_SHIFT) fraction of all IEIs.
   If this is not met, set valid=0 and pbin=0 ("no accent detected").  Also
   require iei_total >= MIN_IEIS to guard against cold-start noise with only a
   few events.

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:

   (a) PWM_SINGLE: a clean periodic pulse train at a known period P, events
       arriving only in one cell (hot_cidx=0 by construction).  After one full
       window, pbin=log2bin32(P) and valid=1.

   (b) PWM_TWO: two lights at different periods P1 and P2 in different cells.
       After the hottest cell stabilises, the dominant pbin matches the hotter
       cell's period.

   (c) BROADBAND_NOISE: events at random/uniform short intervals spread across
       all bins -> peak < (total >> CONF_SHIFT) -> valid=0 (no accent).

   (d) COLD_START: fewer than MIN_IEIS IEIs in the window -> valid=0.

   (e) WSEQ_ARITHMETIC: word index i (0-based) carries
       wseq == ((i + 1) / WINDOW_BATCHES) & WSEQ_MASK.

   (f) WELL-FORMEDNESS: pbin<=31, iei_total<=255, hot_cidx<=223,
       valid<=1, wseq<=7, upper bits=0.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - log2bin32     : right-shift loop for floor(log2 v); one conditional
                       shift for the half-octave sub-bit; shift + OR for result.
                       Produces 32 half-octave bins covering 1..65535.
     - dt / wrap     : masked 16-bit wrap subtract (& TS_MASK); no multiply.
     - cell index    : (x >> X_CELL_SHIFT) + (y >> Y_CELL_SHIFT) * N_CX;
                       both operands are shifts; the product N_CX*row uses
                       a small loop add (N_CX=16 so N_CX*row = row<<4).
     - hist[]        : array index is log2bin32 output 0..31; shift/OR only.
     - peak scan     : 32-iteration compare loop; no multiply.
     - cell scan     : 224-iteration compare loop; no multiply.
     - noise guard   : peak >= (iei_total >> CONF_SHIFT); one shift + compare.
     - output pack   : (wseq<<21)|(hot_cidx<<14)|(iei_total<<6)|(valid<<5)|pbin;
                       shifts and ORs only.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, same as dvs_vital / dvs_flinch):
     x   = (word >> 24) & 0x7F     (0..125)   -- used for cell indexing
     y   = (word >> 17) & 0x7F     (0..111)   -- used for cell indexing
     ts  = (word >>  1) & 0xFFFF   (16-bit timestamp)         -- used for IEI
     pol =  word        & 1                                    -- unused

   -------------------------------------------------------------------------
   TIMEBASE: ts-driven.  16-bit wrapping tick counter; masked 16-bit
   subtraction gives exact deltas for true gaps < 65536 ticks.  IEI_MAX is
   set well below 32768 so IEI values never alias with wrap-around gaps.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112, very noisy):
     1. CELL ISOLATION: only IEIs from the hottest cell enter the histogram.
        Random hot-pixel events in other cells are ignored.
     2. PEAK-DOMINANCE GUARD: valid=1 only when peak >= (total >> CONF_SHIFT)
        (i.e. peak holds >= 1/8 of all IEIs).  Broadband noise spreads counts
        evenly, so peak ~ total/32; the guard rejects this.  A single-period
        PWM source concentrates counts in one or two adjacent bins; the guard
        accepts this easily.
     3. MINIMUM-IEI GUARD: valid=1 only when iei_total >= MIN_IEIS.  A light
        that just entered the frame cannot yet produce a reliable peak.
     4. IEI_MAX FILTER: IEIs longer than IEI_MAX ticks are ignored (the light
        is off or the cell is cold).  This prevents aliased wrap-around gaps
        from polluting the histogram.

   -------------------------------------------------------------------------
   Window timing note: latch and wseq advance happen BEFORE the emit on the
   closing batch of a window (same convention as dvs_vital).  Output word
   index i (0-based) carries wseq == ((i + 1) / WINDOW_BATCHES) & WSEQ_MASK.
   The first WINDOW_BATCHES words carry wseq=0 with the initial zeroed state.

   -------------------------------------------------------------------------
   Output word layout (24 bits used):
     bits[ 4: 0] = pbin      (0..31, dominant IEI log-bin; 0 when valid=0)
     bits[ 5]    = valid      (1=PWM accent detected; 0=no accent / noisy)
     bits[13: 6] = iei_total  (0..255, IEI count this window, saturated)
     bits[20:14] = hot_cidx   (0..127, latched hottest cell index >> 1)
     bits[23:21] = wseq       (3-bit window sequence counter, wraps mod 8)
     bits[31:24] = 0
   hot_cidx in the output word stores (latched_hot_cidx >> 1) so it fits 7 bits;
   multiply by 2 on the host to recover the even-index approximation, or use
   (hot_cidx_raw >> 1) * N_CX from the header to find the cell's row/column.
   Host unpacks these fields; see chips/fpga/dvs_shibboleth_view.py's
   unpack_status().

   -------------------------------------------------------------------------
   Frequency LUT (host-side, in dvs_shibboleth_view.py): the pbin value maps
   to a PWM frequency estimate via the half-octave bin centre.  Bin b covers
   the interval [2^(b/2), 2^((b+2)/2)) in ticks.  The centre is 2^((b+1)/2)
   ticks.  Actual frequency depends on the chip's tick rate (see dvs_replay.py
   for the tick-to-microsecond conversion used in recordings).  The host viewer
   provides a BIN_FREQ_HZ[] LUT mapping bin 0..31 to Hz assuming a 1 MHz
   tick rate -- adjust by the actual rate for real hardware. */

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

/* 16-bit timestamp mask for wrapped subtraction. */
#define TS_MASK 0xFFFFu

/* Coarse cell grid: N_CX * N_CY cells.
   N_CX=16 -> 126/16=7.875 -> 8 pixels per cell column (X_CELL_SHIFT=3).
   N_CY=14 -> 112/14=8     -> 8 pixels per cell row    (Y_CELL_SHIFT=3).
   Total: 16*14 = 224 cells.  Each cell index = (x>>3) + (y>>3)*16.
   N_CX = 16 = 1<<4, so row*N_CX = row<<4 (shift, no multiply). */
#define N_CX         16u
#define N_CY         14u
#define N_CELLS      224u          /* 16 * 14 */
#define X_CELL_SHIFT 3u            /* x >> 3 = column in 0..15 */
#define Y_CELL_SHIFT 3u            /* y >> 3 = row in 0..13 */
/* N_CX_SHIFT: multiply by N_CX=16 = shift left by 4 */
#define N_CX_SHIFT   4u

/* Tunables. */
#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 256         /* verdict window = 256 batches = 1024 events */
#endif

#ifndef MIN_IEIS
#define MIN_IEIS 8                 /* fewer IEIs than this per window -> valid=0 */
#endif

#ifndef CONF_SHIFT
#define CONF_SHIFT 3u              /* valid=1 only if peak >= (total >> CONF_SHIFT) */
                                   /* i.e. peak holds >= 1/8 of IEIs */
#endif

#ifndef IEI_MAX
#define IEI_MAX 32768u             /* IEIs >= this (half wrap-around) are ignored */
#endif

/* Non-tunable constants. */
#define HIST_CAP   255u            /* saturating ceiling for histogram bin counts */
#define TOTAL_CAP  255u            /* saturating ceiling for iei_total */
#define NBINS      32u             /* number of half-octave log-scale IEI bins */
#define WSEQ_MASK  0x7u            /* 3-bit window sequence counter mask */

/* Per-cell event count this window (saturates at 255).
   Cleared on each new window; zeroed by crt0.S. */
static uint8_t cell_cnt[N_CELLS];

/* Per-cell last event timestamp (16-bit, wrapping).
   Zeroed by crt0.S. */
static uint16_t cell_last_ts[N_CELLS];

/* IEI log-scale bin histogram over the current window.
   32 half-octave bins covering IEI values 1..32767 ticks.
   Bin counts saturate at HIST_CAP=255.  Histogram entries are ONLY incremented
   when iei_total < TOTAL_CAP, so that hist[] and iei_total count the SAME set
   of IEIs (consistency is required for the peak-dominance noise guard).
   Cleared on each new window; zeroed by crt0.S. */
static uint8_t hist[NBINS];

/* Index of the hottest cell (set from previous window; 0 at cold start).
   Zeroed by crt0.S. */
static uint32_t hot_cidx;

/* IEI count this window (saturates at TOTAL_CAP).
   Cleared on each new window; zeroed by crt0.S. */
static uint32_t iei_total;

/* Latched stats from the last completed window.
   All 0 before the first window completes; zeroed by crt0.S. */
static uint32_t lat_pbin;
static uint32_t lat_valid;
static uint32_t lat_total;
static uint32_t lat_hot_cidx;

/* Batch-within-window counter (0..WINDOW_BATCHES-1).
   Zeroed by crt0.S. */
static uint32_t batch_in_window;

/* 3-bit window sequence counter (0..7, wraps).
   Zeroed by crt0.S. */
static uint32_t wseq;

/* log2bin32 -- map IEI value v (1..32767) to a half-octave log-scale bin
   (0..31).  Identical to dvs_vital's implementation.  Multiply-free:
     m   = floor(log2 v), range 0..14 for v in 1..32767
     sub = bit just below the leading 1 of v (0 if m==0)
     bin = (m << 1) | sub
   Examples: v=1 -> m=0,sub=0,bin=0; v=2 -> m=1,sub=0,bin=2;
             v=3 -> m=1,sub=1,bin=3; v=1000 -> m=9,sub=1,bin=19. */
static uint32_t log2bin32(uint32_t v) {
    uint32_t m = 0u, t = v;
    while (t >= 2u) { t >>= 1; m++; }         /* m = floor(log2 v), 0..14 */
    uint32_t sub = (m >= 1u) ? ((v >> (m - 1u)) & 1u) : 0u;
    return (m << 1) | sub;
}

/* Compute cell index from (x, y).  No multiply: row * 16 = row << 4.
   x clamped to 0..125 (SX-1); y clamped to 0..111 (SY-1) by ABI.
   col = x >> X_CELL_SHIFT (0..15); row = y >> Y_CELL_SHIFT (0..13).
   cidx = col + (row << N_CX_SHIFT), range 0..223. */
static inline uint32_t cell_of(uint32_t x, uint32_t y) {
    uint32_t col = x >> X_CELL_SHIFT;    /* 0..15 */
    uint32_t row = y >> Y_CELL_SHIFT;    /* 0..13 */
    return col + (row << N_CX_SHIFT);    /* 0..223 */
}

/* Must NOT call wfi() (same reason as dvs_vital -- see that header for the
   full WFI-decode stack-leak explanation).  Just returning is correct. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Process each event in arrival order: decode ABI fields, update cell
       counts, and accumulate IEIs for the hot cell. */
    for (uint32_t i = 0u; i < BATCH; i++) {
        uint32_t x  = (v[i] >> X_SHIFT) & 0x7Fu;
        uint32_t y  = (v[i] >> Y_SHIFT) & 0x7Fu;
        uint32_t ts = (v[i] >>       1) & TS_MASK;
        /* pol = v[i] & 1u -- unused */

        uint32_t c = cell_of(x, y);

        /* Accumulate cell activity count (saturating). */
        if (cell_cnt[c] < 255u) cell_cnt[c]++;

        /* Accumulate IEI only for the currently-tracked hot cell. */
        if (c == hot_cidx) {
            uint32_t prev_ts = cell_last_ts[c];
            uint32_t dt      = (ts - prev_ts) & TS_MASK;   /* masked 16-bit wrap subtract */

            /* Only count IEIs >= 1 tick and < IEI_MAX (filter cold/wrap).
               IEI_MAX=32768 ensures dt < 32768, so the gap is genuine.
               CONSISTENCY: hist[] is only updated when iei_total < TOTAL_CAP,
               so hist[] and iei_total count exactly the same set of IEIs.
               This ensures the peak-dominance noise guard (peak >= total >> CONF_SHIFT)
               has a consistent denominator: for broadband noise spreading across
               N bins, peak ~ total/N < total/8 when N > 8 (CONF_SHIFT=3). */
            if (dt >= 1u && dt < (uint32_t)IEI_MAX && iei_total < TOTAL_CAP) {
                uint32_t bin = log2bin32(dt);
                if (hist[bin] < HIST_CAP) hist[bin]++;
                iei_total++;
            }
        }

        /* Update per-cell last timestamp always (for next-event IEI in the hot cell). */
        cell_last_ts[c] = (uint16_t)(ts & TS_MASK);
    }

    /* Advance the batch-within-window counter and latch on window boundary.
       Latch and wseq increment happen BEFORE the emit below.  Word index i
       (0-based) always carries wseq == ((i + 1) / WINDOW_BATCHES) & WSEQ_MASK.
       The cell tracker (cell_last_ts[]) persists across windows; only
       cell_cnt[], hist[], and iei_total are cleared. */
    batch_in_window++;
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;

        /* Find the hottest cell: scan cell_cnt[] for the maximum.
           Lowest cell index wins ties (strict > keeps first maximum). */
        uint32_t best_cnt = 0u, best_c = 0u;
        for (uint32_t c = 0u; c < N_CELLS; c++) {
            if (cell_cnt[c] > best_cnt) {
                best_cnt = cell_cnt[c];
                best_c   = c;
            }
        }

        /* Peak scan: find maximum histogram bin count.
           Lowest bin index wins ties (strict >). */
        uint32_t peak = 0u, pbin = 0u;
        for (uint32_t b = 0u; b < NBINS; b++) {
            if (hist[b] > peak) { peak = hist[b]; pbin = b; }
        }

        /* Noise guard: valid=1 only when the peak bin dominates.
           Require: iei_total >= MIN_IEIS AND peak >= (iei_total >> CONF_SHIFT).
           The second condition means the peak bin holds >= 1/8 of all IEIs.
           A sharp PWM source easily satisfies this; broadband sparkle does not. */
        uint32_t valid;
        if (iei_total >= (uint32_t)MIN_IEIS &&
            peak >= (iei_total >> (uint32_t)CONF_SHIFT)) {
            valid = 1u;
        } else {
            valid = 0u;
            pbin  = 0u;   /* clear pbin when no accent detected */
        }

        lat_pbin     = pbin;
        lat_valid    = valid;
        lat_total    = iei_total;
        lat_hot_cidx = hot_cidx;   /* latch the cell that WAS hot this window */

        /* Advance hot_cidx to the cell found this window for the NEXT window. */
        hot_cidx = best_c;

        /* Clear per-window accumulators (cell_last_ts[] persists). */
        for (uint32_t c = 0u; c < N_CELLS; c++) cell_cnt[c] = 0u;
        for (uint32_t b = 0u; b < NBINS; b++) hist[b] = 0u;
        iei_total = 0u;

        wseq = (wseq + 1u) & WSEQ_MASK;
    }

    /* Emit ONE word per batch from the LATCHED values only.
       Layout: bits[4:0]=pbin, bits[5]=valid, bits[13:6]=iei_total,
               bits[20:14]=hot_cidx>>1 (7 bits), bits[23:21]=wseq, bits[31:24]=0.
       hot_cidx is right-shifted by 1 before packing so it fits in 7 bits
       (lat_hot_cidx range 0..223; >> 1 gives 0..111).  Host recovers the
       approximate cell by multiplying by 2. */
    *FIFO_OUT = (wseq                   << 21)
              | ((lat_hot_cidx >> 1u)   << 14)
              | (lat_total              <<  6)
              | (lat_valid              <<  5)
              |  lat_pbin;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- correct cold start for all state.
       cell_cnt[], cell_last_ts[], hist, hot_cidx, iei_total, lat_pbin,
       lat_valid, lat_total, lat_hot_cidx, batch_in_window, wseq all start 0.
       hot_cidx=0 is a valid cell index; the first window will update it to
       the true hottest cell from the stream. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
