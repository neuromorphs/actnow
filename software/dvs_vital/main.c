#include <stdint.h>

/* "THE VITALOMETER" (dvs_vital) -- a chips/fpga demo app in the same shape as
   software/dvs_entropy/main.c (fifo_in fires event_id_0 once BATCH words land;
   isr_handler reads them, updates the burst-timing IBI histogram, latches a
   verdict every window, and writes ONE sample word per batch; it NEVER calls
   wfi(), see the epilogue comment on isr_handler).

   Idea (séance gauge -- novel among these apps): the chip decides whether the
   moving thing in the camera's view is ALIVE (irregular, jittery, drifting
   rhythm) or a MECHANISM (metronomic periodicity) -- purely from burst-TIMING
   statistics.  x/y/pol are decoded per ABI but deliberately unused; the output
   word is position-invariant (scrambling x/y/pol leaves every output word
   identical, see identity (e) below).  A "burst" is a run of events with
   inter-event gaps dt < GAP_MIN; the burst onset timestamp is captured when it
   opens.  Each time a burst reaches exactly BURST_MIN_LEN events it is
   "confirmed" and the inter-burst interval (IBI) since the previous confirmed
   burst is binned into a 32-bin log-scale histogram.  Over a window of
   WINDOW_BATCHES batches the histogram's peak-bin and spread (number of bins
   above a floor of peak>>3) are latched and a four-way verdict issued:
     DORMANT   (0) -- too few confirmed IBIs to judge
     MECHANISM (1) -- histogram tightly peaked (spread <= SPREAD_MECH)
     ALIVE     (2) -- histogram widely spread (spread >= SPREAD_ALIVE)
     LIMINAL   (3) -- ambiguous, between the two thresholds

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) Metronome stream with constant period P=1000 ticks.  All IBIs equal
         1000.  log2bin32(1000): floor(log2(1000))=9, sub=(1000>>(9-1))&1=
         (1000>>8)&1=(3)&1=1, bin=(9<<1)|1=19.  All counts land in bin 19,
         spread=1 (<= SPREAD_MECH=2), verdict=MECHANISM -- confirmed single-bin.
     (b) Six-period jitter cycle with IBIs {600,900,1400,2100,3200,4800}.
         log2bin32 values: 600->bin18, 900->bin19, 1400->bin20, 2100->bin22,
         3200->bin23, 4800->bin24.  After enough complete cycles, spread=6
         (>= SPREAD_ALIVE=5), verdict=ALIVE.
     (c) Dense sparkle / hot-pixel chatter: events arrive continuously with
         dt < GAP_MIN on every step.  No burst onset is ever opened (GAP_MIN
         threshold never crossed), so no IBIs are ever confirmed, ibi_total
         remains 0, spread=0, pbin=0, verdict=DORMANT.
     (d) Sparse singleton events spaced far apart (dt >= GAP_MIN every step):
         each event opens a new 1-event burst and immediately the NEXT event
         re-opens a new burst (cur_len resets to 1), so cur_len never reaches
         BURST_MIN_LEN=4.  Zero confirmed bursts, zero IBIs, verdict=DORMANT.
     (e) Position invariance: scrambling x, y, and pol in any order changes
         nothing -- the algorithm only reads ts.  Every output word is
         identical before and after any x/y/pol permutation.
     (f) wseq arithmetic: word index i (0-based) carries
         wseq == ((i+1)/WINDOW_BATCHES) & 0xF.  wseq=0 is "not yet valid"
         (the first WINDOW_BATCHES-1 words carry the uninitialised latched
         state before the first window completes; word i=WINDOW_BATCHES-1
         already carries the completed first window's stats and wseq=1).

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - log2bin32    : a right-shift loop to find floor(log2 v) (multiply-free
                      by definition); one additional conditional shift for the
                      half-octave sub-bit; two shifts and an OR for the result.
                      Produces 32 half-octave bins covering 1..65535 with no
                      multiply anywhere.
     - dt / wrap    : masked 16-bit wrap subtract (& 0xFFFF); no multiply.
     - ibi          : same masked wrap subtract; no multiply.
     - saturating   : compare + conditional increment; compare only.
     - histogram    : array index into 32-entry uint8_t; index is the bin value
                      0..31 from log2bin32 -- shift/OR only, no multiply.
     - peak scan    : 32-iteration compare loop; no multiply.
     - spread scan  : 32-iteration compare loop; floor_ = peak >> 3 (shift).
     - verdict      : three compare branches; no multiply.
     - output pack  : (wseq<<21)|(lat_verdict<<19)|(lat_total<<11)|
                      (lat_spread<<5)|lat_pbin; shifts and ORs only.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_entropy / dvs_flinch):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24   -- decoded but UNUSED
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17   -- decoded but UNUSED
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)         -- USED (timing app)
     pol =  word        & 1                                    -- decoded but UNUSED
   x, y, and pol are decoded per ABI (see below) but unused -- this app is
   position-invariant; only ts drives the burst-timing logic.

   -------------------------------------------------------------------------
   TIMEBASE: this app IS ts-driven (unlike dvs_entropy and dvs_widdershins,
   which are event-ORDER driven).  The timestamp field is a 16-bit wrapping
   tick counter.  Masked 16-bit subtraction gives exact deltas for true gaps
   < 65536 ticks; gaps longer than 65535 ticks alias (a sustained silence of
   >65535 ticks can appear as a short gap).  For typical event-camera recordings
   with active scenes, IBIs are well below this alias point.  Recorded
   chips/fpga CSVs carry a wrapped coarse counter; offline validation builds
   its own synthetic timestamps to match the wrapped 16-bit arithmetic exactly.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112 and VERY noisy).  Three documented
   multiply-free guards:
     1. DENSITY GUARD.  Dense sparkle or hot-pixel chatter produces events with
        inter-event gaps dt < GAP_MIN on every step.  The burst-open threshold
        (dt >= GAP_MIN) is never crossed, so no burst onsets are ever recorded
        and no IBIs are ever confirmed.  ibi_total stays 0 throughout the
        window, and the verdict is DORMANT.  Noise cannot fake MECHANISM or
        ALIVE.  This guard costs nothing extra: it is a side-effect of the
        burst-open condition already required for correctness.
     2. SINGLETON GUARD.  Isolated noise events that ARE spaced by dt >= GAP_MIN
        each open a 1-event burst (cur_len = 1), but cur_len never reaches
        BURST_MIN_LEN=4 before the next gap re-opens a new burst (resetting
        cur_len to 1).  No burst is ever confirmed, no IBI is ever counted, and
        the verdict is DORMANT.  This guard is also free: it is the same
        BURST_MIN_LEN comparison already needed for real bursts.
     3. BOUNDED ONSET SHIFT.  A noise event landing within GAP_MIN ticks BEFORE
        a true burst onset can pull the measured onset timestamp early by at
        most GAP_MIN-1 ticks.  This perturbs each IBI by at most +/-GAP_MIN
        ticks.  The 32 half-octave bins each span ~41% of their centre value,
        so for rhythms with period P >= 8*GAP_MIN the perturbation shifts the
        IBI by less than one half-octave bin and smears at most into adjacent
        bins, growing spread by <= 2.  With SPREAD_MECH=2 and SPREAD_ALIVE=5
        this lands squarely in the LIMINAL dead-band: noise landing near a
        real onset can never alone push a true MECHANISM reading into ALIVE.

   -------------------------------------------------------------------------
   Window timing note: the latch and wseq advance happen BEFORE the emit on
   the closing batch of a window.  Consequently, output word index i (0-based)
   carries wseq == ((i + 1) / WINDOW_BATCHES) & 0xF.  The first
   WINDOW_BATCHES words (i=0..WINDOW_BATCHES-1) carry wseq=0 with the initial
   zeroed latched state; word i=WINDOW_BATCHES-1 already carries the completed
   first window's stats and wseq=1.  Host code should treat wseq=0 as "not
   yet valid" or skip the first window if a clean start matters.

   -------------------------------------------------------------------------
   Output word layout (25 bits used):
     bits[ 4: 0] = pbin    (0..31, dominant IBI log-bin, lowest-index wins ties)
     bits[10: 5] = spread  (0..32, #bins with count > peak>>3)
     bits[18:11] = total   (0..255, confirmed IBIs in the window, saturated)
     bits[20:19] = verdict (0=DORMANT, 1=MECHANISM, 2=ALIVE, 3=LIMINAL)
     bits[24:21] = wseq    (4-bit window sequence counter, wraps mod 16)
     bits[31:25] = 0
   Host unpacks these fields; see chips/fpga/dvs_vital_view.py's
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

/* 16-bit timestamp mask for wrapped subtraction. */
#define TS_MASK 0xFFFFu

/* Tunables: each under #ifndef so -D overrides work at compile time. */
#ifndef GAP_MIN
#define GAP_MIN 48              /* dt >= GAP_MIN ticks opens a new burst */
#endif

#ifndef BURST_MIN_LEN
#define BURST_MIN_LEN 4         /* burst confirmed when its length reaches EXACTLY this */
#endif

#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 256      /* verdict window = 256 batches = 1024 events */
#endif

#ifndef MIN_IBIS
#define MIN_IBIS 6              /* fewer confirmed IBIs than this per window -> DORMANT */
#endif

#ifndef SPREAD_MECH
#define SPREAD_MECH 2           /* spread <= this -> MECHANISM */
#endif

#ifndef SPREAD_ALIVE
#define SPREAD_ALIVE 5          /* spread >= this -> ALIVE */
#endif

/* Non-tunable constants. */
#define HIST_CAP  255u          /* saturating ceiling for histogram bin counts */
#define TOTAL_CAP 255u          /* saturating ceiling for ibi_total */
#define NBINS     32            /* number of half-octave log-scale IBI bins */
#define WSEQ_MASK 0xFu          /* 4-bit window sequence counter mask */

/* IBI log-scale bin histogram over the current window.  32 half-octave bins
   covering IBI values 1..65535 ticks.  Bin counts saturate at HIST_CAP=255.
   Cleared on each new window; zeroed by crt0.S at cold start. */
static uint8_t hist[NBINS];

/* Last event's timestamp (16-bit value).  Zeroed by crt0.S. */
static uint32_t last_ts;

/* Onset timestamp of the current (open, unconfirmed) burst.  Zeroed by
   crt0.S; see cold-start wart note below. */
static uint32_t cur_onset_ts;

/* Number of events in the current burst (saturates at 255).  Zeroed by
   crt0.S. */
static uint32_t cur_len;

/* Onset timestamp of the last CONFIRMED burst (used to compute IBI).
   Zeroed by crt0.S. */
static uint32_t prev_onset_ts;

/* 0 until the first burst confirmation; guards the first IBI measurement.
   Zeroed by crt0.S. */
static uint32_t have_prev;

/* Confirmed IBI count this window (saturates at TOTAL_CAP).  Cleared on each
   new window; zeroed by crt0.S. */
static uint32_t ibi_total;

/* Latched stats from the last completed window (emitted each batch).
   All 0 before the first window completes; zeroed by crt0.S. */
static uint32_t lat_pbin;
static uint32_t lat_spread;
static uint32_t lat_total;
static uint32_t lat_verdict;

/* Batch-within-window counter (0..WINDOW_BATCHES-1).  Zeroed by crt0.S. */
static uint32_t batch_in_window;

/* 4-bit window sequence counter (0..15, wraps).  Zeroed by crt0.S. */
static uint32_t wseq;

/* Cold-start wart (deterministic, mirrored by the host): last_ts starts 0 and
   cur_onset_ts starts 0, so a stream that opens with dt < GAP_MIN (i.e., the
   first event's ts is within GAP_MIN of 0) can "confirm" a phantom burst with
   onset ts 0.  At confirmation, have_prev was 0, so no IBI is counted -- only
   prev_onset_ts is set to 0 and have_prev becomes 1.  This is the only side-
   effect: the first real IBI measurement will be anchored to ts 0 rather than
   to the true first burst's onset.  The host mirrors this behaviour exactly. */

/* log2bin32 -- map IBI value v (1..65535) to a half-octave log-scale bin
   (0..31).  Multiply-free by construction: the floor(log2 v) step is a
   right-shift loop; the half-octave sub-bit is one conditional shift and an
   AND; the result is one shift and an OR.  32 bins cover 16 octaves in pairs
   of half-octave sub-bins.
     m   = floor(log2 v), range 0..15 for v in 1..65535
     sub = bit just below the leading 1 of v (0 if m==0)
     bin = (m << 1) | sub
   Examples: v=1 -> m=0,sub=0,bin=0; v=2 -> m=1,sub=0,bin=2;
             v=3 -> m=1,sub=1,bin=3; v=1000 -> m=9,sub=1,bin=19. */
static uint32_t log2bin32(uint32_t v) {
    uint32_t m = 0u, t = v;
    while (t >= 2u) { t >>= 1; m++; }         /* m = floor(log2 v), 0..15 */
    uint32_t sub = (m >= 1u) ? ((v >> (m - 1u)) & 1u) : 0u;
    return (m << 1) | sub;
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

    /* Process each event in arrival order: decode ABI fields, update the
       burst tracker, and record confirmed IBIs into the histogram. */
    for (uint32_t i = 0; i < BATCH; i++) {
        /* x   = (v[i] >> X_SHIFT) & 0x7Fu; -- decoded per ABI but unused
                 (position-invariant; see the header comment). */
        /* y   = (v[i] >> Y_SHIFT) & 0x7Fu; -- decoded per ABI but unused
                 (position-invariant; see the header comment). */
        /* pol =  v[i] & 1u;                -- decoded per ABI but unused
                 (position-invariant; see the header comment). */
        uint32_t ts = (v[i] >> 1) & TS_MASK;

        uint32_t dt = (ts - last_ts) & TS_MASK;   /* masked 16-bit wrap subtract */
        last_ts = ts;

        if (dt >= (uint32_t)GAP_MIN) {
            /* Gap large enough to open a new burst. */
            cur_onset_ts = ts;
            cur_len = 1u;
        } else {
            /* Still within the current burst: grow it. */
            if (cur_len < 255u) cur_len++;

            if (cur_len == (uint32_t)BURST_MIN_LEN) {
                /* Burst confirmed EXACTLY once (cur_len reaches BURST_MIN_LEN
                   exactly; subsequent increments go to 255+, never re-trigger). */
                if (have_prev) {
                    uint32_t ibi = (cur_onset_ts - prev_onset_ts) & TS_MASK;
                    if (ibi != 0u) {   /* skip 65536-tick wrap alias */
                        uint32_t bin = log2bin32(ibi);
                        if (hist[bin] < HIST_CAP) hist[bin]++;
                        if (ibi_total < TOTAL_CAP) ibi_total++;
                    }
                }
                prev_onset_ts = cur_onset_ts;
                have_prev = 1u;
            }
        }
    }

    /* Advance the batch-within-window counter and latch on window boundary.
       NOTE: the latch and wseq increment happen BEFORE the emit below, so the
       word written for the closing batch of window W already carries window W's
       latched stats and the incremented wseq.  Word index i (0-based) always
       carries wseq == ((i + 1) / WINDOW_BATCHES) & 0xF.
       NOTE: the burst tracker (last_ts, cur_onset_ts, cur_len, prev_onset_ts,
       have_prev) deliberately PERSISTS across windows; only hist and ibi_total
       are cleared.  This ensures that a burst straddling a window boundary is
       confirmed in the window where it completes, not lost. */
    batch_in_window++;
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;

        /* Peak scan: find the maximum histogram bin count.
           pbin = argmax with LOWEST bin index winning ties (strict > keeps
           the first maximum found, which is the lowest-index bin). */
        uint32_t peak = 0u, pbin = 0u;
        for (uint32_t b = 0u; b < NBINS; b++) {
            if (hist[b] > peak) { peak = hist[b]; pbin = b; }
        }

        /* Spread scan: count bins with count strictly above floor = peak>>3. */
        uint32_t floor_ = peak >> 3;
        uint32_t spread = 0u;
        for (uint32_t b = 0u; b < NBINS; b++) {
            if (hist[b] > floor_) spread++;
        }

        /* Four-way verdict. */
        uint32_t verdict;
        if (ibi_total < (uint32_t)MIN_IBIS)        verdict = 0u;  /* DORMANT   */
        else if (spread <= (uint32_t)SPREAD_MECH)  verdict = 1u;  /* MECHANISM */
        else if (spread >= (uint32_t)SPREAD_ALIVE) verdict = 2u;  /* ALIVE     */
        else                                       verdict = 3u;  /* LIMINAL   */

        lat_pbin    = pbin;
        lat_spread  = spread;
        lat_total   = ibi_total;
        lat_verdict = verdict;

        /* Clear per-window accumulators (burst tracker persists). */
        for (uint32_t b = 0u; b < NBINS; b++) hist[b] = 0u;
        ibi_total = 0u;

        wseq = (wseq + 1u) & WSEQ_MASK;
    }

    /* Emit ONE word per batch from the LATCHED values only.
       Layout: bits[4:0]=pbin, bits[10:5]=spread, bits[18:11]=total,
               bits[20:19]=verdict, bits[24:21]=wseq, bits[31:25]=0 */
    *FIFO_OUT = (wseq        << 21)
              | (lat_verdict << 19)
              | (lat_total   << 11)
              | (lat_spread  <<  5)
              |  lat_pbin;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- the correct cold start for ALL state
       in this app.  hist, last_ts, cur_onset_ts, cur_len, prev_onset_ts,
       have_prev, ibi_total, lat_pbin, lat_spread, lat_total, lat_verdict,
       batch_in_window, and wseq all start at 0 without any explicit
       initialisation here.  See the cold-start wart note above log2bin32 for
       the one documented deterministic artefact of starting last_ts=0. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
