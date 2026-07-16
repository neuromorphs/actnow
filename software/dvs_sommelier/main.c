#include <stdint.h>

/* "THE SOMMELIER OF MOTION" (dvs_sommelier) -- a chips/fpga demo app in the
   same shape as software/dvs_vital/main.c.  Hold up anything that moves
   (cloth, water, a fan, wiggling fingers, a flame); the chip classifies the
   "substance" from motion statistics alone and narrates it like a pompous
   critic.

   -------------------------------------------------------------------------
   THE IDEA (multiply-free, no divide, pure RV32I).  Every WINDOW_BATCHES
   batches the ISR computes 8 integer FEATURES from counters/shifts and
   classifies by Manhattan distance (sum of |feature - centroid|, pure
   adds/abs) to one of N_CLASSES compile-time centroid vectors.  Nearest
   class wins if margin > MARGIN_MIN; else UNKNOWN (class 0).

   FEATURE DEFINITIONS (all shift/add/sub/compare, NO multiply/divide):
     F0  log2 event-rate = log2(total events in window) via CLZ shift loop
     F1  polarity balance = ON_count - OFF_count clamped to [-128,127] + 128
         (stored as uint8 so 128=balanced, >128=ON-heavy, <128=OFF-heavy)
     F2  spatial spread = number of occupied 8x8 coarse cells (0..220)
         SX=126 -> ceil(126/8)=16 columns; SY=112 -> ceil(112/8)=14 rows ->
         16*14=224 cells; stored in a 7-bit packed bitvector (32 uint32_t words
         of 7 bits each) to stay inside 32 KB; cleared per window.
     F3  burstiness = max short-bin count vs total >> BURST_K.
         Window is divided into NBURST_BINS=8 equal sub-bins of
         WINDOW_BATCHES/8 batches each; per-batch event count is accumulated
         into the active sub-bin.  At window close: max_sub >> BURST_SCALE vs
         total_events >> BURST_K.  If max_sub >> BURST_SCALE > total >> BURST_K
         then BURSTY=1, else 0.  Result: 1 bit (0 or 1 scaled to 0 or 64).
     F4  HV structure = column-transition count vs row-transition count.
         Track prev_x,prev_y; at each event: if |x-prev_x|>HV_THRESH increment
         col_trans; if |y-prev_y|>HV_THRESH increment row_trans.
         Clamp both to 255.  F4 = col_trans - row_trans + 128 (signed offset).
     F5  hot-pixel share = (events from the single most-active 8x8 cell) >> HP_K
         where HP_K is chosen so saturating counts -> large F5.  If the top cell
         contributed more than HOTPIX_THRESH events per window, mark as NOISY.
         NOISY -> force class UNKNOWN.
     F6  IEI (inter-event-interval) mode bucket = the most-populated half-octave
         log-bin of per-event dt values; computed identically to dvs_vital's
         log2bin32 but on each consecutive event pair's dt.  16-bin version
         (4 bits -> 0..15) to save RAM.  F6 = argmax(iei_hist).
     F7  perimeter/area proxy = boundary occupied cells vs total occupied cells.
         A cell is "boundary" if it is on the edge of the 16x14 grid.
         Total edge cells = 2*16 + 2*14 - 4 = 56.  F7 = boundary_count scaled
         to 0..127 via a 7-bit shift.

   -------------------------------------------------------------------------
   CLASSES (compile-time centroids; hand-tuned):
     0  UNKNOWN     -- below margin; default when uncertain
     1  RIGID-ROTOR -- fast fan or motor; high event rate, bursty, H-dominant
     2  LIQUID      -- water surface or pour; moderate rate, ON-heavy, low spread
     3  CLOTH       -- waving fabric; medium rate, spatial spread, border-heavy
     4  FINGERS     -- wiggling fingers; medium rate, balanced pol, V-dominant
     5  FLAME       -- candle or lighter; low rate, balanced, non-bursty, sparse

   -------------------------------------------------------------------------
   NOISE GUARD (SciDVS 126x112 is VERY noisy):
     1. HOT-PIXEL GUARD.  If F5 exceeds HOTPIX_THRESH at window close, the
        scene is dominated by a single active pixel -> UNKNOWN immediately.
        This correctly handles dark/static scenes whose sparkle does not form
        any interesting spatial pattern.
     2. MIN-EVENTS GUARD.  If total events in the window < MIN_EVENTS_CLASSIFY
        we have too few samples for reliable feature estimation -> UNKNOWN.
     3. MARGIN GUARD.  The nearest centroid's Manhattan distance is subtracted
        from the second-nearest's.  If the margin (gap between best and second
        best) is < MARGIN_MIN, the scene is ambiguous -> UNKNOWN.

   -------------------------------------------------------------------------
   MULTIPLY-FREE by construction (-march=rv32i -- no mul/div, see
   software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - F0 (log2 rate): right-shift loop to find floor(log2(count)); no multiply.
     - F1 (pol balance): counter increment/decrement, clamp; no multiply.
     - F2 (spread): bit-test and set in a 224-bit bitvector; no multiply.
     - F3 (burstiness): per-sub-bin accumulator; shift comparison; no multiply.
     - F4 (HV structure): abs(x-prev_x) compare, abs(y-prev_y) compare;
       counter; no multiply.
     - F5 (hot-pixel share): bitvector of per-cell event counts (saturating
       uint8 in 224-entry array -- 224 bytes); argmax; no multiply.
     - F6 (IEI log-bin): same log2bin32 as dvs_vital; right-shift loop;
       16-bin variant; no multiply.
     - F7 (perimeter proxy): bit-test on cell index for boundary membership;
       no multiply.
     - Classification: Manhattan distance loop over N_CLASSES centroids;
       abs(feature - centroid) sums; compare; no multiply.

   -------------------------------------------------------------------------
   The event word (evt_pack.v):
     x   = (word >> 24) & 0x7F     (0..125)
     y   = (word >> 17) & 0x7F     (0..111)
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)
     pol =  word        & 1

   -------------------------------------------------------------------------
   Output word layout (32 bits):
     bits[ 2: 0] = class    (0..5; 0=UNKNOWN)
     bits[10: 3] = margin   (0..255, Manhattan distance gap to second-nearest,
                             saturated; 0 when UNKNOWN)
     bits[11:11] = valid    (1 once first window complete, else 0)
     bits[15:12] = wseq     (4-bit window sequence counter, wraps mod 16)
     bits[23:16] = f_rate   (F0: log2 event rate, 0..31 clamped to uint8)
     bits[31:24] = f_spread (F2: occupied cells count >> SPREAD_SCALE, 0..255)
   Host unpacks these fields; see chips/fpga/dvs_sommelier_view.py's
   unpack_status(). */

/* -------------------------------------------------------------------------
   Hardware registers (same ABI as dvs_vital). */
#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

/* -------------------------------------------------------------------------
   Core parameters. */
#define BATCH 4

/* Sensor frame. */
#define SX 126
#define SY 112

/* Input event ABI. */
#define X_SHIFT 24
#define Y_SHIFT 17

/* 16-bit timestamp mask. */
#define TS_MASK 0xFFFFu

/* Window length. */
#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 256      /* 256 batches * 4 events = 1024 events per window */
#endif

/* Coarse grid dimensions (ceil(SX/8)=16, ceil(SY/8)=14). */
#define GRID_COLS 16
#define GRID_ROWS 14
#define N_CELLS   224           /* GRID_COLS * GRID_ROWS */

/* Bitvector words for N_CELLS=224 bits -> 7 uint32_t words. */
#define CELL_BV_WORDS 7        /* 7 * 32 = 224 bits exactly */

/* Noise guards. */
#ifndef MIN_EVENTS_CLASSIFY
#define MIN_EVENTS_CLASSIFY 64  /* minimum events in window before classifying */
#endif

#ifndef HOTPIX_THRESH
#define HOTPIX_THRESH 200       /* top-cell event count -> hot-pixel noise -> UNKNOWN */
#endif

/* Polarity balance feature. */
/* F1 stored as on_count - off_count + 128 (uint8). */

/* Burstiness parameters. */
#define NBURST_BINS   8
#define BURST_BATCHES_PER_BIN (WINDOW_BATCHES / NBURST_BINS)   /* =32 */
#define BURST_K       3         /* total_events >> BURST_K for threshold */
#define BURST_SCALE   0         /* max_sub >> BURST_SCALE (no shift; compare raw) */

/* HV structure. */
#define HV_THRESH 4             /* |x-prev_x| > HV_THRESH counts as column transition */

/* IEI log-bin (16-bin version). */
#define IEI_NBINS    16
#define IEI_HIST_CAP 255

/* Hot-pixel cell count cap. */
#define CELL_CAP 255u

/* Spread output scale. */
#define SPREAD_SCALE 0          /* F2 stored directly (0..224), fit in uint8 via >>0 */

/* F5 hot-pixel shift. */
#define HP_K 0

/* Classification. */
#define N_CLASSES    6          /* including UNKNOWN=0 */
#define MARGIN_MIN   8          /* Manhattan gap < this -> UNKNOWN */

/* Output word layout constants. */
#define WSEQ_MASK    0xFu

/* -------------------------------------------------------------------------
   Class centroid table [N_CLASSES][8].  Row 0 = UNKNOWN (unused in distance;
   just a sentinel).  Feature columns: F0..F7.
     F0  log2 event-rate (0..31)
     F1  pol balance offset (0..255; 128=balanced)
     F2  spatial spread cell count (0..224)
     F3  burstiness (0 or 64)
     F4  HV offset (0..255; 128=equal)
     F5  hot-pixel max count (0..255)
     F6  IEI mode bin (0..15)
     F7  perimeter proxy (0..127)
   Centroid values are hand-tuned to plausible representative scenes:
     RIGID-ROTOR  high rate, balanced pol, high spread, bursty, H-dominant,
                  low hot-pixel, fast IEI, border-heavy (edge of rotor blade)
     LIQUID       moderate rate, slightly ON-heavy, low spread, non-bursty,
                  balanced HV, low hot-pixel, medium IEI, low perimeter
     CLOTH        medium rate, balanced pol, high spread, non-bursty,
                  balanced HV, low hot-pixel, medium IEI, border-heavy
     FINGERS      medium rate, balanced pol, medium spread, non-bursty,
                  V-dominant, low hot-pixel, medium IEI, low perimeter
     FLAME        low rate, slightly ON-heavy, low spread, non-bursty,
                  balanced HV, low hot-pixel, slow IEI, low perimeter       */
/* Centroids calibrated to match feature vectors produced by the synthetic
   stream builders in chips/fpga/dvs_sommelier_view.py.  With WINDOW_BATCHES=256
   and BATCH=4 every window has exactly 1024 events, giving F0=floor(log2(1024))=10
   always and F3=0 always (the per-sub-bin accumulation is uniform).
   F0 and F3 are constant across all classes; the discriminating features are
   F1 (pol balance), F2 (spread), F4 (HV structure), F5 (hotpix), F6 (IEI bin),
   F7 (perimeter proxy).

   Hand-derived centroid verification (view: chips/fpga/dvs_sommelier_view.py):
     RIGID-ROTOR: full spread (F2=224), ON-heavy (F1=255), H-dominant (F4=255),
                  fast IEI (F6=13), border-heavy (F7=112).
     LIQUID:      small patch (F2=30), ON-heavy (F1=234), H-dominant (F4=212),
                  low hotpix (F5=35), mid IEI (F6=12), no border (F7=0).
     CLOTH:       medium spread (F2=126), balanced pol (F1=128), balanced HV (F4=128),
                  low hotpix (F5=9), mid IEI (F6=10), partial border (F7=60).
     FINGERS:     narrow spread (F2=14), balanced pol (F1=128), V-dominant (F4=0),
                  moderate hotpix (F5=74), mid IEI (F6=10), no border (F7=4).
     FLAME:       tiny spread (F2=12), ON-heavy (F1=234), balanced HV (F4=128),
                  moderate hotpix (F5=86), slow IEI (F6=15), no border (F7=0). */
static const uint8_t centroids[N_CLASSES][8] = {
    /* F0  F1   F2   F3   F4   F5   F6  F7  */
    {  10, 128,   0,   0, 128,   0,   0,  0 },  /* 0 UNKNOWN    (sentinel) */
    {  10, 255, 224,   0, 255,   6,  13, 112},  /* 1 RIGID-ROTOR */
    {  10, 234,  30,   0, 212,  35,  12,  0 },  /* 2 LIQUID      */
    {  10, 128, 126,   0, 128,   9,  10,  60},  /* 3 CLOTH       */
    {  10, 128,  14,   0,   0,  74,  10,  4 },  /* 4 FINGERS     */
    {  10, 234,  12,   0, 128,  86,  15,  0 },  /* 5 FLAME       */
};

/* -------------------------------------------------------------------------
   ISR state (all zeroed by crt0.S). */

/* Per-cell event counts: 224 saturating uint8_t, cleared per window. */
static uint8_t cell_count[N_CELLS];

/* Occupied-cell bitvector: 7 uint32_t = 224 bits, cleared per window. */
static uint32_t cell_bv[CELL_BV_WORDS];

/* Polarity counters (reset per window). */
static uint32_t on_count;
static uint32_t off_count;

/* Burstiness: per-sub-bin event count, batch index within sub-bin. */
static uint32_t burst_bin_count[NBURST_BINS];
static uint32_t burst_sub;       /* current sub-bin index 0..7 */
static uint32_t burst_sub_batch; /* batches elapsed in current sub-bin */

/* HV structure counters (reset per window). */
static uint32_t col_trans;
static uint32_t row_trans;
static uint32_t prev_x;
static uint32_t prev_y;
static uint32_t hv_first;        /* 0 until first event processed */

/* IEI histogram (16 bins, reset per window). */
static uint8_t iei_hist[IEI_NBINS];
static uint32_t last_ts;         /* last event timestamp, for IEI */
static uint32_t iei_first;       /* 0 until first event of this window */

/* Total events in this window (reset per window). */
static uint32_t win_events;

/* Batch-within-window counter. */
static uint32_t batch_in_window;

/* 4-bit window sequence counter. */
static uint32_t wseq;

/* Latched output fields from last completed window. */
static uint32_t lat_class;
static uint32_t lat_margin;
static uint32_t lat_valid;
static uint32_t lat_f0;
static uint32_t lat_f2;

/* -------------------------------------------------------------------------
   log2bin16 -- map dt value v (1..65535) to a half-octave log-scale bin
   (0..15).  Same algorithm as dvs_vital's log2bin32 but clamped to 4 bits.
   Multiply-free: right-shift loop + conditional shift + AND. */
static uint32_t log2bin16(uint32_t v) {
    uint32_t m = 0u, t = v;
    while (t >= 2u) { t >>= 1u; m++; }
    uint32_t sub = (m >= 1u) ? ((v >> (m - 1u)) & 1u) : 0u;
    uint32_t bin = (m << 1) | sub;
    return (bin > 15u) ? 15u : bin;   /* clamp to 4 bits */
}

/* -------------------------------------------------------------------------
   abs32 -- absolute value of a signed difference, multiply-free. */
static uint32_t abs32(int32_t x) {
    return (x < 0) ? (uint32_t)(-x) : (uint32_t)x;
}

/* -------------------------------------------------------------------------
   classify -- Manhattan-distance classification.
   Returns (class, margin) where margin is distance-gap between best and 2nd-best.
   Returns class=0 (UNKNOWN) with margin=0 if margin < MARGIN_MIN or hot-pixel
   or too few events.  Multiply-free: loop of abs + add + compare. */
static void classify(uint8_t feat[8], uint32_t total_events,
                     uint32_t *out_class, uint32_t *out_margin) {
    /* Check noise guards. */
    if (total_events < (uint32_t)MIN_EVENTS_CLASSIFY) {
        *out_class = 0u;
        *out_margin = 0u;
        return;
    }
    /* Hot-pixel guard: F5 is feat[5]; threshold already applied in ISR. */
    if (feat[5] >= (uint8_t)HOTPIX_THRESH) {
        *out_class = 0u;
        *out_margin = 0u;
        return;
    }

    uint32_t best_dist  = 0xFFFFFFFFu;
    uint32_t best_cls   = 0u;
    uint32_t second_dist = 0xFFFFFFFFu;

    /* Classes 1..N_CLASSES-1 (skip UNKNOWN sentinel at 0). */
    for (uint32_t c = 1u; c < (uint32_t)N_CLASSES; c++) {
        uint32_t dist = 0u;
        for (uint32_t f = 0u; f < 8u; f++) {
            dist += abs32((int32_t)(uint32_t)feat[f]
                         - (int32_t)(uint32_t)centroids[c][f]);
        }
        if (dist < best_dist) {
            second_dist = best_dist;
            best_dist   = dist;
            best_cls    = c;
        } else if (dist < second_dist) {
            second_dist = dist;
        }
    }

    uint32_t margin = second_dist - best_dist;
    if (margin > 255u) margin = 255u;

    if (margin < (uint32_t)MARGIN_MIN) {
        *out_class  = 0u;
        *out_margin = 0u;
    } else {
        *out_class  = best_cls;
        *out_margin = margin;
    }
}

/* -------------------------------------------------------------------------
   isr_handler -- fires every BATCH events (fifo_in trigger level = BATCH).
   Reads BATCH words, updates all feature accumulators, latches on window
   boundary, emits ONE status word.  Must NOT call wfi() (same reason as
   dvs_vital: soc.act's WFI-decode never returns control to the instruction
   after it -- calling wfi() inside the ISR would permanently skip the
   epilogue, leaking stack until collision with program code). */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Process each event. */
    for (uint32_t i = 0u; i < BATCH; i++) {
        uint32_t x   = (v[i] >> X_SHIFT) & 0x7Fu;
        uint32_t y   = (v[i] >> Y_SHIFT) & 0x7Fu;
        uint32_t ts  = (v[i] >> 1) & TS_MASK;
        uint32_t pol =  v[i] & 1u;

        win_events++;

        /* F1: polarity balance. */
        if (pol) {
            if (on_count  < 0xFFFFFFFFu) on_count++;
        } else {
            if (off_count < 0xFFFFFFFFu) off_count++;
        }

        /* F2 / F5: spatial spread and hot-pixel.
           Coarse cell index: row = y>>3, col = x>>3.
           cell = row*GRID_COLS + col. */
        uint32_t col  = x >> 3u;
        uint32_t row  = y >> 3u;
        if (col >= (uint32_t)GRID_COLS) col = (uint32_t)GRID_COLS - 1u;
        if (row >= (uint32_t)GRID_ROWS) row = (uint32_t)GRID_ROWS - 1u;
        uint32_t cell = row * (uint32_t)GRID_COLS + col;
        /* cell_count: saturating uint8. */
        if (cell_count[cell] < (uint8_t)CELL_CAP) cell_count[cell]++;
        /* Occupied bitvector. */
        cell_bv[cell >> 5u] |= (1u << (cell & 31u));

        /* F4: HV structure. */
        if (!hv_first) {
            hv_first = 1u;
            prev_x = x;
            prev_y = y;
        } else {
            uint32_t dx = (x > prev_x) ? (x - prev_x) : (prev_x - x);
            uint32_t dy = (y > prev_y) ? (y - prev_y) : (prev_y - y);
            if (dx > (uint32_t)HV_THRESH) {
                if (col_trans < 255u) col_trans++;
            }
            if (dy > (uint32_t)HV_THRESH) {
                if (row_trans < 255u) row_trans++;
            }
            prev_x = x;
            prev_y = y;
        }

        /* F6: IEI log-bin. */
        if (!iei_first) {
            iei_first = 1u;
            last_ts = ts;
        } else {
            uint32_t dt = (ts - last_ts) & TS_MASK;
            last_ts = ts;
            if (dt > 0u) {
                uint32_t bin = log2bin16(dt);
                if (iei_hist[bin] < IEI_HIST_CAP) iei_hist[bin]++;
            }
        }
    }

    /* F3: burstiness sub-bin accumulation (per-batch, not per-event). */
    burst_bin_count[burst_sub] += BATCH;
    burst_sub_batch++;
    if (burst_sub_batch >= (uint32_t)BURST_BATCHES_PER_BIN) {
        burst_sub_batch = 0u;
        burst_sub++;
        if (burst_sub >= (uint32_t)NBURST_BINS) burst_sub = (uint32_t)NBURST_BINS - 1u;
    }

    /* Advance batch-within-window and latch on window boundary. */
    batch_in_window++;
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;

        /* ---- Compute features ---- */
        uint8_t feat[8];

        /* F0: log2 event rate. */
        {
            uint32_t m = 0u, t = win_events;
            while (t >= 2u) { t >>= 1u; m++; }
            feat[0] = (uint8_t)(m > 255u ? 255u : m);
        }

        /* F1: polarity balance = on - off + 128, clamped 0..255. */
        {
            int32_t bal = (int32_t)on_count - (int32_t)off_count + 128;
            if (bal < 0)   bal = 0;
            if (bal > 255) bal = 255;
            feat[1] = (uint8_t)bal;
        }

        /* F2: spatial spread = number of set bits in cell_bv.
           Popcount via shift loop over 7 words. */
        {
            uint32_t spread = 0u;
            for (uint32_t w = 0u; w < (uint32_t)CELL_BV_WORDS; w++) {
                uint32_t x32 = cell_bv[w];
                while (x32) { spread += x32 & 1u; x32 >>= 1u; }
            }
            if (spread > 255u) spread = 255u;
            feat[2] = (uint8_t)spread;
        }

        /* F3: burstiness.
           max_sub = max over NBURST_BINS of burst_bin_count[k].
           Bursty if max_sub >> BURST_SCALE > win_events >> BURST_K. */
        {
            uint32_t max_sub = 0u;
            for (uint32_t k = 0u; k < (uint32_t)NBURST_BINS; k++) {
                if (burst_bin_count[k] > max_sub) max_sub = burst_bin_count[k];
            }
            uint32_t bursty = ((max_sub >> (uint32_t)BURST_SCALE)
                               > (win_events >> (uint32_t)BURST_K)) ? 64u : 0u;
            feat[3] = (uint8_t)bursty;
        }

        /* F4: HV structure offset = col_trans - row_trans + 128, clamped 0..255. */
        {
            int32_t hv = (int32_t)col_trans - (int32_t)row_trans + 128;
            if (hv < 0)   hv = 0;
            if (hv > 255) hv = 255;
            feat[4] = (uint8_t)hv;
        }

        /* F5: hot-pixel max cell count (raw uint8, already saturated). */
        {
            uint8_t hpmax = 0u;
            for (uint32_t c = 0u; c < (uint32_t)N_CELLS; c++) {
                if (cell_count[c] > hpmax) hpmax = cell_count[c];
            }
            feat[5] = hpmax;
        }

        /* F6: IEI mode bin = argmax(iei_hist). */
        {
            uint32_t best_bin = 0u, best_cnt = 0u;
            for (uint32_t b = 0u; b < (uint32_t)IEI_NBINS; b++) {
                if (iei_hist[b] > best_cnt) {
                    best_cnt = iei_hist[b];
                    best_bin = b;
                }
            }
            feat[6] = (uint8_t)best_bin;
        }

        /* F7: perimeter proxy.
           Boundary cells: row==0, row==GRID_ROWS-1, col==0, col==GRID_COLS-1.
           Count occupied boundary cells; scale to 0..127 via >> 1 (max=56 cells
           so raw 0..56, scale by 2 to stretch to 0..112, capped at 127). */
        {
            uint32_t perim = 0u;
            for (uint32_t c = 0u; c < (uint32_t)N_CELLS; c++) {
                /* bit test */
                uint32_t occ = (cell_bv[c >> 5u] >> (c & 31u)) & 1u;
                if (!occ) continue;
                uint32_t r = c / (uint32_t)GRID_COLS;
                uint32_t cl = c % (uint32_t)GRID_COLS;
                if (r == 0u || r == (uint32_t)(GRID_ROWS - 1)
                    || cl == 0u || cl == (uint32_t)(GRID_COLS - 1)) {
                    perim++;
                }
            }
            uint32_t f7 = perim << 1u;  /* scale */
            if (f7 > 127u) f7 = 127u;
            feat[7] = (uint8_t)f7;
        }

        /* ---- Classify ---- */
        uint32_t cls = 0u, margin = 0u;
        classify(feat, win_events, &cls, &margin);

        lat_class  = cls;
        lat_margin = margin;
        lat_valid  = 1u;
        lat_f0     = feat[0];
        lat_f2     = feat[2];

        /* ---- Clear per-window accumulators ---- */
        on_count  = 0u;
        off_count = 0u;
        for (uint32_t w = 0u; w < (uint32_t)CELL_BV_WORDS; w++) cell_bv[w] = 0u;
        for (uint32_t c = 0u; c < (uint32_t)N_CELLS; c++) cell_count[c] = 0u;
        col_trans = 0u;
        row_trans = 0u;
        for (uint32_t b = 0u; b < (uint32_t)IEI_NBINS; b++) iei_hist[b] = 0u;
        for (uint32_t k = 0u; k < (uint32_t)NBURST_BINS; k++) burst_bin_count[k] = 0u;
        burst_sub = 0u;
        burst_sub_batch = 0u;
        win_events = 0u;
        iei_first = 0u;
        hv_first  = 0u;

        wseq = (wseq + 1u) & WSEQ_MASK;
    }

    /* Emit ONE word per batch from latched values.
       bits[ 2: 0] = class  (0..5)
       bits[10: 3] = margin (0..255)
       bits[11:11] = valid  (0 or 1)
       bits[15:12] = wseq   (0..15)
       bits[23:16] = f_rate (F0, log2 event-rate)
       bits[31:24] = f_spread (F2, spatial cell count, 0..255) */
    *FIFO_OUT = (lat_f2     << 24)
              | (lat_f0     << 16)
              | (wseq       << 12)
              | (lat_valid  << 11)
              | (lat_margin <<  3)
              |  lat_class;
}

void main(void) {
    /* .bss is already zeroed by crt0.S.  All state (on_count, off_count,
       cell_bv, cell_count, col_trans, row_trans, prev_x, prev_y, hv_first,
       iei_hist, last_ts, iei_first, win_events, burst_bin_count, burst_sub,
       burst_sub_batch, batch_in_window, wseq, lat_class, lat_margin,
       lat_valid, lat_f0, lat_f2) starts at 0. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;       /* configure fifo_in trigger level */
    *INT_CTRL_ENABLE = 0x1; /* enable event_id_0 last, once everything is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
