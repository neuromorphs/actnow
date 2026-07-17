#include <stdint.h>

/* "WHO IS THE MIRROR?" (dvs_mirror) -- a chips/fpga demo app.  Two players
   occupy the left (x < 63) and right (x >= 63) halves of the 126x112 frame.
   One leads a motion; the other mirrors it.  The chip measures the CAUSALITY
   LAG between the two halves and names the leader.

   -------------------------------------------------------------------------
   ALGORITHM -- multiply-free sign-correlation:

   1. EVENT ACCUMULATION (per half, per time bin).
      Time is derived from the ts field (16-bit wrapping tick counter, same ABI
      as every other app).  A "bin" covers BIN_TICKS=16 ticks (BIN_SHIFT=4).
      Bin index: bin = (ts >> BIN_SHIFT) & BIN_MASK.  BIN_MASK = NBINS-1 with
      NBINS=32 (power of 2, so masking is a shift -- NO modulo/divide).
      Per-event: accumulate into left_cnt[bin] or right_cnt[bin] (uint8_t,
      saturates at HALF_CAP=255) depending on whether x < SPLIT_X (63).

   2. BINARIZE AGAINST MEAN (median-style multiply-free guard).
      At each correlation update: sum both halves' bins to get total_L/R.
      Threshold = total >> LOG2_NBINS (LOG2_NBINS=5, so this is total/32, the
      per-bin mean -- computed with a single right shift, NO divide/multiply).
      Bin i is SET (1) if cnt[i] > threshold, else CLEAR (0).  This rejects
      sparkle (hot pixels fire uniformly -- every bin slightly elevated, none
      stands above mean) and short isolated noise bursts (below threshold).
      Result: left_bits and right_bits, each a 32-bit mask, one bit per bin.

      ACTIVITY GUARD (noise gate): if total_L < MIN_ACTIVITY or
      total_R < MIN_ACTIVITY, emit verdict NONE (no leader) with lag=0,
      confidence=0.  This prevents noise from producing false leader calls
      when one or both halves are dark.

   3. LAG CORRELATION -- AND + popcount, NO multiply.
      Search lag k in 1..LAG_MAX for two cases (plus k=0).
      ROTATION CONVENTION: rotate_bits32(right_bits, n) shifts each bit at
      position r to (r+n)%NBINS.  AND with left_bits hits when left has a bit
      at l=(r+n)%NBINS, i.e. r=l-n.  Interpretation:
        - rotate +k (k=1..LAG_MAX): right fired at bin r=l-k, which is k bins
          BEFORE left at l -> RIGHT leads by k bins.
        - rotate (NBINS-k) (k=1..LAG_MAX): r=l+k, right fired k bins AFTER
          left -> LEFT leads by k bins.
        - k=0: no shift, simultaneous (NONE).
      corr = popcount32(left_bits & rotated_right_bits).
      popcount32: 5-stage shift-and-add tree, NO multiply, NO divide.
      rotate_bits32: two shifts and an OR, NO multiply.

   4. LEADER DECISION.
      best_k = first argmax over (k=0, right-leads k=1..LAG_MAX, left-leads
      k=1..LAG_MAX).  First-max-wins (strict >) means k=0 wins ties.
      If right-leads scan finds the maximum: RIGHT is the leader.
      If left-leads scan finds the maximum: LEFT is the leader.
      If k=0 has the maximum: NONE (simultaneous / ambiguous).
      Confidence = corr[best_k] (0..32, popcount of 32-bit mask).
      If confidence < MIN_CONFIDENCE, verdict overridden to NONE (insufficient
      correlation for a trustworthy reading).

   5. WINDOW AND EMIT.
      Correlation is recomputed every WINDOW_BATCHES=128 batches (512 events).
      Every batch emits ONE status word from the LATCHED previous-window result.
      The ring buffers are NOT cleared on window boundaries; they age naturally
      as old bins are overwritten by new bins (oldest bin advances with ts).

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) LEFT-LEADS EXACT: left events at bins 0..3 (x=30), right events at
         bins 4..7 (x=90), 64 events per bin each.  total_L=total_R=256;
         threshold=256>>5=8; all active bins > threshold.
         left_bits=0x0F, right_bits=0xF0.  Left-leads scan k=4:
         rotate_bits32(0xF0, NBINS-4=28): bits 4..7 -> 0..3 = 0x0F;
         popcount(0x0F & 0x0F)=4>=MIN_CONFIDENCE.  => leader==LEFT(1), lag==4.
     (b) RIGHT-LEADS EXACT: swap bins -- right at bins 0..3 (right_bits=0x0F),
         left at bins 4..7 (left_bits=0xF0).  Right-leads scan k=4:
         rotate_bits32(0x0F, 4): bits 0..3 -> 4..7 = 0xF0;
         popcount(0xF0 & 0xF0)=4>=MIN_CONFIDENCE.  => leader==RIGHT(2), lag==4.
     (c) SIMULTANEOUS SAME-PATTERN: both at bins 0..3.  left_bits=right_bits=0x0F.
         k=0: popcount(0x0F & 0x0F)=4; all nonzero lags give <4.  => NONE.
     (d) ACTIVITY GUARD: events on left only (right dark) -> verdict NONE.
     (e) WELL-FORMEDNESS: leader<=2, lag_mag<=LAG_MAX, confidence<=NBINS,
         seq<=15, upper bits zero.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i).  All ops:
     - bin index      : (ts >> BIN_SHIFT) & BIN_MASK  -- shifts and AND only
     - threshold      : total >> LOG2_NBINS             -- one right shift
     - bitmask build  : 1u << i in 0..NBINS-1          -- shift only
     - rotate_bits32  : (x << n) | (x >> (NBINS-n))   -- shifts and OR
     - popcount32     : 5-stage shift-and-add          -- shifts, ANDs, adds
     - argmax         : compare loop                   -- compare only
     - output pack    : shifts and ORs                 -- shifts and OR
     No multiply anywhere; no divide anywhere.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112 and VERY noisy):
     1. MEAN BINARIZATION: hot pixels fire at nearly constant rate across all
        time bins.  Their contribution raises every bin roughly equally, so no
        bin stands above the per-bin mean threshold.  The bitmask for a noisy-
        only half remains all-zeros.  A noisy half cannot fake a causal signal.
     2. ACTIVITY GUARD: if either half has total < MIN_ACTIVITY counts in the
        ring buffer, we call NONE rather than correlating noise against noise.
        MIN_ACTIVITY = NBINS (32): requires at least one event per bin average
        before trusting any leader call.
     3. SATURATION CLAMP: bin counts saturate at HALF_CAP=255 so a single hot
        pixel cannot overflow or corrupt the total.
     4. CONFIDENCE GATE: even if noise happens to produce a spurious argmax,
        the popcount confidence is low (<<NBINS) and below MIN_CONFIDENCE=4.
        Leader==NONE when confidence < MIN_CONFIDENCE.

   -------------------------------------------------------------------------
   The event word (evt_pack.v):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)
     pol =  word        & 1
   x and ts are used; y and pol are decoded per ABI but unused.

   -------------------------------------------------------------------------
   TIMEBASE: ts-driven.  16-bit wrapping counter; masked 16-bit subtraction
   gives correct bin index via (ts >> BIN_SHIFT) & BIN_MASK (no subtract
   needed -- bin index IS the upper bits of ts).  Recorded CSVs carry a
   wrapped coarse counter; --validate uses synthetic timestamps.

   -------------------------------------------------------------------------
   Output word layout (23 bits used):
     bits[ 1: 0] = leader    (0=NONE, 1=LEFT leads, 2=RIGHT leads, 3=reserved)
     bits[ 9: 2] = lag_mag   (0..LAG_MAX, in units of BIN_TICKS ticks,
                               saturated at LAG_MAX; lag_mag=0 when leader==NONE)
     bits[18:10] = confidence (0..NBINS popcount score, saturated at NBINS;
                               this is the AND-popcount at the best lag,
                               divided by nothing -- raw count 0..32)
     bits[22:19] = seq        (4-bit window sequence counter, wraps mod 16)
     bits[31:23] = 0
   Host unpacks these fields; see chips/fpga/dvs_mirror_view.py's
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

/* 16-bit timestamp mask. */
#define TS_MASK 0xFFFFu

/* FOV split: x < SPLIT_X -> left half; x >= SPLIT_X -> right half. */
#define SPLIT_X 63

/* Time-bin parameters.  One bin = 2^BIN_SHIFT timestamp ticks.
   NBINS must be a power of 2 <= 32 (fits in one uint32_t for the bitmask).
   BIN_MASK = NBINS - 1 (used with & for modular bin index). */
#ifndef BIN_SHIFT
#define BIN_SHIFT 4             /* 1 bin = 16 ts ticks */
#endif

#define NBINS      32u          /* number of time bins (must be <= 32) */
#define LOG2_NBINS 5u           /* log2(NBINS); threshold = total >> LOG2_NBINS */
#define BIN_MASK   (NBINS - 1u)

/* Lag search range: search k in [-LAG_MAX .. +LAG_MAX] (inclusive). */
#ifndef LAG_MAX
#define LAG_MAX 8               /* 8 bins each side */
#endif

/* Tunables: each under #ifndef so -D overrides work at compile time. */
#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 128      /* correlation update period = 128 batches = 512 events */
#endif

#ifndef MIN_ACTIVITY
#define MIN_ACTIVITY 32u        /* min events in ring-buffer per half before correlating */
#endif

#ifndef MIN_CONFIDENCE
#define MIN_CONFIDENCE 4u       /* min AND-popcount to trust a leader call */
#endif

/* Saturating caps. */
#define HALF_CAP  255u          /* per-bin count ceiling */
#define WSEQ_MASK 0xFu          /* 4-bit window sequence counter mask */

/* Leader codes (match dvs_mirror_view.py). */
#define LEADER_NONE  0u
#define LEADER_LEFT  1u
#define LEADER_RIGHT 2u

/* Per-half ring-buffer bin counts.  32 bins, one per BIN_TICKS interval.
   Indexed by (ts >> BIN_SHIFT) & BIN_MASK.  Old bins are overwritten as ts
   advances; no explicit clear needed (see ring-buffer aging note above).
   Zeroed by crt0.S at cold start. */
static uint8_t left_cnt[NBINS];
static uint8_t right_cnt[NBINS];

/* Latched stats from the last completed window.  All 0 before first window.
   Zeroed by crt0.S. */
static uint32_t lat_leader;
static uint32_t lat_lag_mag;
static uint32_t lat_confidence;

/* Batch-within-window counter (0..WINDOW_BATCHES-1).  Zeroed by crt0.S. */
static uint32_t batch_in_window;

/* 4-bit window sequence counter (0..15, wraps).  Zeroed by crt0.S. */
static uint32_t wseq;

/* popcount32 -- count set bits in a uint32_t.  5-stage shift-and-add,
   NO multiply, NO divide.  Classic Hamming-weight calculation. */
static uint32_t popcount32(uint32_t x) {
    x = x - ((x >> 1) & 0x55555555u);
    x = (x & 0x33333333u) + ((x >> 2) & 0x33333333u);
    x = (x + (x >> 4)) & 0x0f0f0f0fu;
    x = x + (x >> 8);
    x = x + (x >> 16);
    return x & 0x3fu;
}

/* FULL32_MASK -- the low NBINS bits mask.  With NBINS=32 this equals 0xFFFFFFFF
   (cannot write 1u<<32 -- shift overflow); with NBINS<32 it is (1u<<NBINS)-1. */
#define FULL32_MASK 0xFFFFFFFFu

/* rotate_bits32 -- rotate a NBINS-bit value LEFT by n positions (0..NBINS-1).
   Only the low NBINS bits of x and the result are meaningful.
   Two shifts and one OR, NO multiply.  n=0 returns x unchanged.
   With NBINS=32: FULL32_MASK == 0xFFFFFFFF (all bits) and the AND is a no-op
   (NBINS=32 fills the entire uint32_t), so the mask is correct without a
   shift-by-32 (which would be undefined behaviour). */
static uint32_t rotate_bits32(uint32_t x, uint32_t n) {
    if (n == 0u) return x & FULL32_MASK;
    return ((x << n) | (x >> (NBINS - n))) & FULL32_MASK;
}

/* build_bitmask -- binarize a half's ring-buffer against its per-bin mean.
   total = sum of all NBINS bin counts (already computed by caller).
   threshold = total >> LOG2_NBINS (= total / NBINS, the per-bin mean).
   bit i of the returned mask is set iff cnt[i] > threshold. */
static uint32_t build_bitmask(const uint8_t cnt[/* NBINS */], uint32_t total) {
    uint32_t threshold = total >> LOG2_NBINS;
    uint32_t mask = 0u;
    for (uint32_t i = 0u; i < NBINS; i++) {
        if ((uint32_t)cnt[i] > threshold) {
            mask |= (1u << i);
        }
    }
    return mask;
}

/* Must NOT call wfi() itself -- see software/dvs_vital/main.c's ISR comment
   for the full explanation.  Just returning is correct. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0u; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Process each event: decode ABI, accumulate into the appropriate half's
       ring-buffer bin indexed by the time bin of ts. */
    for (uint32_t i = 0u; i < BATCH; i++) {
        uint32_t x  = (v[i] >> X_SHIFT) & 0x7Fu;
        /* y   = (v[i] >> Y_SHIFT) & 0x7Fu; -- decoded per ABI but unused */
        /* pol =  v[i] & 1u;                -- decoded per ABI but unused */
        uint32_t ts = (v[i] >> 1) & TS_MASK;

        /* Bin index: (ts >> BIN_SHIFT) & BIN_MASK.  Pure shift + AND; no mul.
           This is a modular ring: old bins are silently overwritten as ts
           wraps around NBINS bins, so the ring always holds the most recent
           NBINS * BIN_TICKS ticks of activity. */
        uint32_t bin = (ts >> BIN_SHIFT) & BIN_MASK;

        if (x < (uint32_t)SPLIT_X) {
            if (left_cnt[bin]  < HALF_CAP) left_cnt[bin]++;
        } else {
            if (right_cnt[bin] < HALF_CAP) right_cnt[bin]++;
        }
    }

    /* Advance the batch-within-window counter and run correlation on boundary.
       NOTE: latch and wseq increment happen BEFORE the emit so the word for
       the closing batch of window W already carries window W's latched stats
       and the incremented wseq.  Word index i (0-based) always carries
       wseq == ((i + 1) / WINDOW_BATCHES) & WSEQ_MASK. */
    batch_in_window++;
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;

        /* Sum each half's ring buffer (total_L, total_R). */
        uint32_t total_L = 0u, total_R = 0u;
        for (uint32_t i = 0u; i < NBINS; i++) {
            total_L += (uint32_t)left_cnt[i];
            total_R += (uint32_t)right_cnt[i];
        }

        /* Activity guard: if either half is too quiet, emit NONE immediately
           (no correlation -- noise would dominate). */
        if (total_L < MIN_ACTIVITY || total_R < MIN_ACTIVITY) {
            lat_leader     = LEADER_NONE;
            lat_lag_mag    = 0u;
            lat_confidence = 0u;
        } else {
            /* Binarize both halves. */
            uint32_t left_bits  = build_bitmask(left_cnt,  total_L);
            uint32_t right_bits = build_bitmask(right_cnt, total_R);

            /* AND-popcount correlation sweep over lags -LAG_MAX .. +LAG_MAX.
               ROTATION CONVENTION (derived carefully from bit semantics):
               rotate_bits32(right_bits, k) shifts each bit at position r to
               position (r+k)%NBINS.  AND with left_bits hits when left has a
               bit at l == (r+k)%NBINS, i.e. r == l-k.  This means right fired
               at bin (l-k), which is k bins BEFORE left fired at l:
                 RIGHT fired k bins before LEFT -> RIGHT leads.
               rotate_bits32(right_bits, NBINS-k) shifts bit r to (r-k)%NBINS.
               AND hits when l == (r-k)%NBINS, i.e. r == l+k.  Right fired at
               bin l+k, which is k bins AFTER left at l:
                 LEFT fired k bins before RIGHT -> LEFT leads.
               So: the +k rotation detects RIGHT-leads; the (NBINS-k) rotation
               detects LEFT-leads.  First-max wins (strict >), so k=0 wins ties
               over all nonzero lags. */
            uint32_t best_corr = 0u;
            uint32_t best_lag_mag = 0u;
            uint32_t best_leader  = LEADER_NONE;

            /* k == 0: no shift, simultaneous motion. */
            {
                uint32_t corr = popcount32(left_bits & right_bits);
                if (corr > best_corr) {
                    best_corr    = corr;
                    best_lag_mag = 0u;
                    best_leader  = LEADER_NONE;
                }
            }

            /* k in 1 .. LAG_MAX: rotate +k -> RIGHT leads (right fired k bins
               before left).  bit r of right_bits maps to position (r+k)%NBINS;
               AND with left_bits hits where left has a bit l = r+k, meaning
               right fired at bin l-k < l. */
            for (uint32_t k = 1u; k <= (uint32_t)LAG_MAX; k++) {
                uint32_t rk   = rotate_bits32(right_bits, k);
                uint32_t corr = popcount32(left_bits & rk);
                if (corr > best_corr) {
                    best_corr    = corr;
                    best_lag_mag = k;
                    best_leader  = LEADER_RIGHT;
                }
            }

            /* k in 1 .. LAG_MAX: rotate (NBINS-k) -> LEFT leads (left fired k
               bins before right).  bit r maps to (r-k)%NBINS; AND hits where
               left has bit l = r-k, meaning right fired at bin l+k > l. */
            for (uint32_t k = 1u; k <= (uint32_t)LAG_MAX; k++) {
                uint32_t rk   = rotate_bits32(right_bits, NBINS - k);
                uint32_t corr = popcount32(left_bits & rk);
                if (corr > best_corr) {
                    best_corr    = corr;
                    best_lag_mag = k;
                    best_leader  = LEADER_LEFT;
                }
            }

            /* Confidence gate: low correlation -> call NONE. */
            if (best_corr < MIN_CONFIDENCE) {
                best_leader  = LEADER_NONE;
                best_lag_mag = 0u;
            }

            /* Saturate lag_mag at LAG_MAX (should already be <= LAG_MAX). */
            if (best_lag_mag > (uint32_t)LAG_MAX) best_lag_mag = (uint32_t)LAG_MAX;

            lat_leader     = best_leader;
            lat_lag_mag    = best_lag_mag;
            lat_confidence = best_corr;
        }

        /* Note: we do NOT clear the ring buffers here.  They age naturally:
           as ts advances, new events overwrite old bins, so activity from
           more than NBINS*BIN_TICKS ticks ago is erased automatically.
           This gives a sliding-window effect without any explicit clear loop. */

        wseq = (wseq + 1u) & WSEQ_MASK;
    }

    /* Emit ONE word per batch from the LATCHED values only.
       Layout: bits[1:0]=leader, bits[9:2]=lag_mag, bits[18:10]=confidence,
               bits[22:19]=seq, bits[31:23]=0. */
    *FIFO_OUT = (wseq           << 19)
              | (lat_confidence << 10)
              | (lat_lag_mag    <<  2)
              |  lat_leader;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- correct cold start for all state.
       left_cnt, right_cnt, lat_leader, lat_lag_mag, lat_confidence,
       batch_in_window, and wseq all start at 0. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
