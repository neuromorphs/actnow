#include <stdint.h>

/* chips/fpga OMS (Object-Motion Sensitivity) variant: DIRECTION-CONSENSUS
   independent-motion segmentation -- the empirically best OMS detector from the
   oms-meister benchmark (rank #2 "backbone", direction_consensus.py; rank #1
   dir_evidence.py is the same backbone + a persistence layer). On realistic
   camera-shake data these DIRECTION-based methods beat the classic rate-based
   OMS (which ranked 13th), because the discriminative invariant under shake is
   TIMING INCONSISTENCY WITH THE LOCAL WAVEFRONT, not unexpected event rate
   (rate depends on texture/contrast/threshold and does not transfer across
   backgrounds; propagation direction+delay is geometric and does).

   Same interrupt/FIFO wiring as software/dvs_motion/main.c and
   software/dvs_stabilize/main.c (fifo_in fires event_id_0 once BATCH words
   land; isr_handler pops them, does its work, writes ONE result word per batch,
   returns -- never wfi()). The "work" here is, per event, a CAUSAL
   independent-motion agreement score against the local background/reafference
   direction; the batch's output word reports the tile with the strongest
   independent motion (segmentation) PLUS the global background-motion direction
   (the stabilization vector -- see dvs_stabilize; both apps share the same
   direction-voting primitive, an 8-bin histogram of coincidence-derived
   direction votes).

   ==================== ALGORITHM (multiply-free, O(1)/event) =================

   Mechanism (all causal, all leaky -- ported verbatim from
   oms-meister/benchmark/detectors/direction_consensus.py):

     * A per-pixel, per-POLARITY LAST-TIMESTAMP surface ts[pol][ty][tx] holding
       a coarse (1-byte) quantized timestamp of the last same-polarity event.
     * For each event e=(x,y,pol,t): read the 8 compass-neighbour ages
       a_d = t - ts[pol][ny][nx] for d in {E,NE,N,NW,W,SW,S,SE}. A same-polarity
       coincidence whose age lands in delay band {1-3, 3-8, 8-20} ms is evidence
       the local wavefront moved FROM neighbour d INTO this pixel -- i.e. a
       DIRECTION vote for the OPPOSITE bin (-d). Nearer bands weight more
       (w_near=1, w_mid=1/2, w_far=1/4 -- all SHIFTS, no multiply). This gives
       the event's own 8-bin coincidence vector C_e[8].
     * Votes accumulate into a leaky per-TILE 8-bin direction histogram on a
       coarse 16x16-px grid (TW x TH tiles). The tile's dominant bin d_cons is
       the local background/reafference direction; confidence = winner/sum.
     * The event's C_e is compared to its tile consensus:
           z_e = (1 - C_e[d_cons]) + eta * max_{d != d_cons} C_e[d]
       An event MOVING WITH the background (C_e peaked at d_cons) scores LOW z;
       an event moving against/across it (independent motion) scores HIGH z.
     * Low-confidence tiles (little coherent flow) are SUPPRESSED: their
       consensus is meaningless, so we damp z toward 0 rather than accuse a
       quiescent background (a hard confidence gate here; the Python uses a
       logistic -- the integer version is a two-step ramp, see conf gate).
     * The score for e uses the histogram built from events strictly BEFORE e
       (the tile histogram and ts surface are updated AFTER scoring) -> causal.

   Per batch: pick the tile with the highest independent-motion score seen this
   batch (segmentation output) and read the GLOBAL dominant direction across all
   tiles (background-motion / stabilization vector). Emit one packed word.

   ==================== RV32I (no mul/div) WORKAROUNDS ========================

   Plain RV32I -- no hardware multiply/divide (software/common/program.mk's
   -march=rv32i). Every step is shifts/adds/compares:

   * BAND WEIGHTS ARE SHIFTS. w_near=1 (<<0), w_mid=1/2 -> represent votes in
     FIXED-POINT with W_SHIFT=2 fractional bits so 1, 1/2, 1/4 become the
     integers 4, 2, 1 (no fractions, no multiply). C_e bins and the histogram
     are integer accumulators of these weighted votes.

   * TILE INDEX WITHOUT MULTIPLY. tile=16 (TILE_SHIFT=4): tx=x>>4, ty=y>>4.
     TW=ceil(126/16)=8 tiles wide; the row stride is padded to a power of two
     (TW_P2=8=1<<3) so tidx = (ty<<TW_LOG2)|tx -- a shift, not a multiply.

   * TS-SURFACE INDEX WITHOUT MULTIPLY. Full-res per-pol surface is
     126*112*2 = 28224 B at 1 B/px -- with the histograms + stack that is too
     tight in 32 KB, and neighbour reads at full res are sparse (a neighbour
     pixel rarely fired recently). So DOWNSAMPLE by 2 (TS_SHIFT=1, exactly like
     dvs_stabilize): the surface is TSW x TSH = 63 x 56 super-pixels, stride
     padded to TSW_P2=64=1<<6 so idx=(sy<<TSW_LOG2)|sx is a shift. Two polarity
     planes (POL_STRIDE=TSW_P2*TSH apart). 64*56*2 = 7168 B. Downsampling also
     DENSIFIES the surface (a 2x2 super-pixel gathers ~4x the events), so a
     neighbour's timestamp is far more likely to be meaningful -- which sharpens
     the coincidence test, the same reasoning dvs_stabilize documents.
     (Neighbour offsets are +-1 SUPER-pixel = +-2 real px; the delay bands are
     unchanged, so this only coarsens the spatial grid of the direction test,
     not the timing.)

   * COARSE TIMESTAMP / RECENCY. The AER word carries a 16-bit timestamp
     (ts = (word>>1)&0xFFFF -- evt_pack.v's [16:1] field, decoded like
     software/dvs_track/main.c). We quantize it to a byte: q = (ts >>
     TS_TICK_SHIFT). With TS_TICK_SHIFT=8 one tick ~= 256 (timestamp LSBs); the 8-bit surface then
     spans 256 ticks before wrap. The delay bands, in ticks (see BAND_* below),
     top out at ~20 ms worth, comfortably inside one wrap. Age is computed as an
     UNSIGNED byte difference (t_q - stored) & 0xFF so a single wrap is handled
     correctly for the in-band range; a coincidence older than a full wrap can
     alias to a small age, a rare, self-correcting minority vote (same tradeoff
     dvs_stabilize accepts). A "never fired" cell stores a sentinel (0) and its
     age is forced out-of-band on first read via a seen[] bit-plane so an unseen
     neighbour never votes. evt_pack.v now supplies a REAL, live 16-bit
     timestamp, so on hardware / a live AER stream this detector works directly.
     NB: if a capture path delivers ts=0 for every event (as the recorded
     kr260_capture.py CSVs do -- see dvs_stabilize), ALL ages collapse to 0 (<
     BAND_LO) and NO direction votes fire -> the app degrades gracefully to
     "no motion", it does not misfire. Real per-event timestamps are REQUIRED
     for this detector to do anything (it is fundamentally a timing detector);
     the Python mirror validates on the timestamped oms-meister recordings.

   * HISTOGRAM LEAK WITHOUT exp(). The Python decays the tile histogram by
     exp(-dt/tau) lazily per event. exp() needs a multiply/LUT; we approximate
     the leaky-integrator forgetting with a periodic HALVING of every tile
     histogram once per DECAY_BATCHES batches (>>1 -- exactly dvs_motion's grid
     decay and dvs_stabilize's accumulator decay). This is a coarser but
     monotone, multiply-free stand-in for the continuous leak; tau_vote~60 ms in
     the reference => at ~100k ev/s and BATCH=4 a halving every DECAY_BATCHES
     batches gives a comparable forgetting horizon (tunable). The Python mirror
     replicates THIS integer halving, not exp(), so the two match bit-for-bit.

   * CONFIDENCE / z GATING WITHOUT DIVIDE. confidence = h_win/h_sum and the C_e
     normalisation C_e[d]/ce_sum are DIVIDES. We avoid them by comparing
     CROSS-MULTIPLIED integers (a/b >= thr  <=>  a >= thr*b, and thr is a shift
     fraction), and by scoring in the UN-normalised domain: the score we emit
     for a tile is the raw disagreement mass, and we compare tiles to each other
     (argmax) rather than to an absolute normalised threshold. The confidence
     gate is a shift-compare: keep the tile's consensus only if
     h_win*CONF_DEN >= h_sum*CONF_NUM (CONF_NUM/CONF_DEN ~= conf_thr=0.40 ->
     2/5, done as h_win*5 >= h_sum*2, two shifts+add each: *5 = (v<<2)+v,
     *2 = v<<1). No divide anywhere.

   ==================== OUTPUT WORD LAYOUT ===================================

   Mirrors dvs_motion's {flag,val,row,col} segmentation layout, and folds in
   dvs_stabilize's global 8-octant background direction (the "bonus" bits):

     bit    31..16 : 0
     bit    15     : reserved 0
     bit    14     : INDEPENDENT-MOTION flag (hottest tile's z cleared THRESHOLD)
     bits   13..9  : hottest tile's independent-motion score (5-bit, clamped)
     bits   8..6   : hottest tile row (0..7)   -- 3 bits (TH=7 uses 0..6)
     bits   5..3   : hottest tile col (0..7)   -- 3 bits (TW=8 uses 0..7)
     bits   2..0   : GLOBAL background direction, 8-bin code d (0=E..7=SE),
                     or a "no consensus" sentinel handled by the score field
                     (val==0 && flag==0 => no learned background yet).

   The Python mirror chips/fpga/dvs_oms_dirconsensus_view.py unpacks this
   identically; oms_dirconsensus_ref.py recomputes the same INTEGER math.

   ==================== STATE / COMPUTE BUDGET ==============================

   State (all .bss, no heap):
     ts_surface : POL_STRIDE * 2 = (TSW_P2*TSH) * 2 = 3584 * 2 = 7168 bytes
     seen       : same super-pixel geometry, 1 bit/px/pol packed = 7168/8 = 896 B
     hist       : NT * 8 * sizeof(u16) = 56 * 8 * 2 = 896 bytes
     ------------------------------------------------------------------
     total static state ~= 8960 bytes  (well inside the 32 KB SRAM; the rest of
     SRAM holds code (XIP-style from the same image) + the small ISR stack).

   Compute per event (hot path, no loop over pixels):
     8 neighbour reads + 8 age band-classifies (compares) + up to 8 vote adds
     + an 8-bin tile argmax/sum (8 compares+adds) + the z compare -- ~30-50
     integer ops, NO multiply/divide. Per batch: an 8-bin global argmax over
     the tiles' dominant directions + the pack (compares/shifts). At ~115k ev/s
     that is ~5 M ops/s of headroom-friendly integer work; if the input rate
     exceeds the core's budget, drop BATCH events by subsampling (process 1 in
     N) -- the leaky histogram is robust to decimation. Measured input on the
     oms-meister recordings is ~0.6 Mev/s; see the Python mirror's timing.

   ==================== LIMITATIONS (honest) ================================

   * NEEDS REAL TIMESTAMPS. This is a timing detector; a rig that reports a
     constant ts (some kr260 capture paths) makes every age 0 -> no votes -> no
     detections (fails safe, does not false-fire). Use a capture path that
     preserves the AER timestamp (the recorded CSV `le` column does).
   * DOWNSAMPLED DIRECTION GRID. The ts surface is /2, so the direction test is
     on 2x2 super-pixels; fine texture direction is coarsened. Full-res would be
     28 KB (too tight) -- /2 is the memory/quality knee, same as dvs_stabilize.
   * HISTOGRAM DECAY IS A HALVING, not exp(): coarser forgetting than the
     Python reference's continuous leak, chosen to stay multiply-free. The
     Python mirror matches THIS integer halving so validation is exact.
   * NO PERSISTENCE LAYER. dir_evidence's leaky per-cell evidence accumulator
     (rank #1, for slow/large objects below the instantaneous tail) is NOT
     ported here to keep the per-event path a few tens of ops; it is an additive
     stage over this same backbone and is the documented next step (it only ever
     RAISES a currently-disagreeing event's score, never manufactures
     foreground, so omitting it costs recall on big slow objects, not
     precision). */

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
   ts=(w>>1)&0xFFFF (16-bit), pol=w&1. */
#define X_SHIFT 24
#define Y_SHIFT 17

/* ---- NOISE STRATEGY (SciDVS is 126x112 and VERY noisy) ---
   This detector is noise-robust by construction and needs no extra pre-filter:
     - It fires only on TIMING INCONSISTENCY with the local wavefront, so an
       isolated noise event (no correlated same-polarity neighbour in the delay
       bands) casts no votes and scores z=0 -- background activity is silent.
     - Low-confidence tiles (no coherent flow) are hard-GATED off the consensus
       (h_win*CONF_DEN >= h_sum*CONF_NUM), so a quiescent/noisy tile can't accuse.
     - The leaky per-tile histogram (periodic halving) forgets stale votes, so a
       transient noise burst decays instead of latching.
   CONF_NUM/CONF_DEN and THRESHOLD are the tunable noise knobs. */

/* ---- time-surface geometry: downsample by 2, power-of-two padded stride ---- */
#define TS_SHIFT  1                                      /* /2 downsample        */
#define TSW  ((SX + (1 << TS_SHIFT) - 1) >> TS_SHIFT)    /* = 63 super-px wide    */
#define TSH  ((SY + (1 << TS_SHIFT) - 1) >> TS_SHIFT)    /* = 56 super-px tall    */
#define TSW_LOG2 6                                       /* 1<<6 = 64 >= TSW      */
#define TSW_P2 (1 << TSW_LOG2)                           /* = 64 padded stride    */
#define POL_STRIDE (TSW_P2 * TSH)                        /* = 3584 per-pol plane  */
#define TS_CELLS (POL_STRIDE * 2)                        /* = 7168 bytes (u8)     */

/* ---- coarse consensus tile grid (full-res 16px tiles) ---- */
#define TILE_SHIFT 4                                      /* tile = 16 px         */
#define TW  ((SX + (1 << TILE_SHIFT) - 1) >> TILE_SHIFT)  /* = 8 tiles wide       */
#define TH  ((SY + (1 << TILE_SHIFT) - 1) >> TILE_SHIFT)  /* = 7 tiles tall       */
#define TW_LOG2 3                                         /* 1<<3 = 8 >= TW       */
#define TW_P2 (1 << TW_LOG2)                              /* = 8 padded stride    */
#define NT (TW_P2 * TH)                                   /* = 56 tiles           */

/* ---- coarse timestamp quantization ---- */
/* AER ts field is (word>>1)&0xFFFF (16-bit). One byte-tick = 1<<TS_TICK_SHIFT ts-LSBs.
   With the recorded/replayed streams the ts unit is ~1 us; TS_TICK_SHIFT=8 ->
   ~256 us/tick, so the 8-bit surface spans ~65 ms before wrap. Delay bands
   below are expressed in these ticks. */
#define TS_TICK_SHIFT 8

/* Delay bands in TICKS (256 us/tick). Reference bands: 1,3,8,20 ms.
   1 ms /256us ~= 4 ; 3 ms ~= 12 ; 8 ms ~= 31 ; 20 ms ~= 78. */
#define BAND_LO  4     /* < this age: too fresh (same-wave jitter / self)   */
#define BAND_MID 12    /* (BAND_LO, BAND_MID]  -> w_near                     */
#define BAND_HI  31    /* (BAND_MID, BAND_HI]  -> w_mid                      */
#define BAND_FAR 78    /* (BAND_HI, BAND_FAR]  -> w_far ; older -> no vote   */

/* Vote weights as fixed-point integers (W_SHIFT=2 fractional bits): the
   reference w_near=1, w_mid=0.5, w_far=0.25 become 4, 2, 1 -- pure shifts. */
#define W_NEAR 4
#define W_MID  2
#define W_FAR  1

/* eta (weight of the strongest DISAGREEING direction in z) = 1/2 -> a >>1. */

/* Confidence gate ~= conf_thr 0.40 = 2/5, tested by cross-multiply (no divide):
   keep consensus iff  h_win*CONF_DEN >= h_sum*CONF_NUM. */
#define CONF_NUM 2
#define CONF_DEN 5

/* Histogram leak: halve every tile bin once per DECAY_BATCHES batches
   (multiply-free stand-in for exp(-dt/tau), tau_vote ~60 ms in the reference). */
#define DECAY_BATCHES 64

/* Per-tile histogram bin saturation (u16 head-room so *5 in the conf test and
   the score adds never overflow 32-bit temporaries). */
#define HIST_CAP 4000

/* Output field clamps / threshold. */
#define ZVAL_MAX  31   /* 5-bit hottest-tile score field                       */
#define THRESHOLD 6    /* hottest-tile z (fixed-point, W_SHIFT scale) to flag   */

/* ---- 8 neighbour offsets (dx,dy) in SUPER-pixels: E,NE,N,NW,W,SW,S,SE. A
   coincidence with the OLDER event at offset i means the wavefront came FROM
   i; the motion direction is the OPPOSITE bin OPP[i] = (i+4)&7. ---- */
static const int8_t ODX[8] = { 1,  1,  0, -1, -1, -1,  0,  1 };
static const int8_t ODY[8] = { 0, -1, -1, -1,  0,  1,  1,  1 };
static const uint8_t OPP[8] = { 4,  5,  6,  7,  0,  1,  2,  3 };

/* ---- state (all .bss) ---- */
static uint8_t  ts_surface[TS_CELLS];      /* per-pol quantized last-timestamp   */
static uint8_t  seen[TS_CELLS >> 3];       /* 1 bit/super-px/pol: has ever fired  */
static uint16_t hist[NT * 8];              /* per-tile leaky 8-bin direction hist */
static uint32_t batch_ctr;                 /* batches since last decay            */

static inline int seen_get(uint32_t idx) {
    return (seen[idx >> 3] >> (idx & 7)) & 1u;
}
static inline void seen_set(uint32_t idx) {
    seen[idx >> 3] |= (uint8_t)(1u << (idx & 7));
}

static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Periodic histogram decay (halve all bins) -- multiply-free forgetting. */
    if (++batch_ctr >= DECAY_BATCHES) {
        batch_ctr = 0;
        for (uint32_t k = 0; k < NT * 8; k++) {
            hist[k] = (uint16_t)(hist[k] >> 1);
        }
    }

    /* per-batch segmentation winner (hottest independent-motion tile). */
    int32_t  best_z    = -1;
    uint32_t best_tile = 0;

    for (uint32_t e = 0; e < BATCH; e++) {
        uint32_t word = v[e];
        uint32_t x  = (word >> X_SHIFT) & 0x7F;
        uint32_t y  = (word >> Y_SHIFT) & 0x7F;
        uint32_t pol = word & 1u;
        uint32_t ts = (word >> 1) & 0xFFFF;     /* 16-bit AER timestamp        */
        uint8_t  tq = (uint8_t)(ts >> TS_TICK_SHIFT);   /* quantized to a byte  */

        /* super-pixel coords for the ts surface (downsampled). */
        uint32_t sx = x >> TS_SHIFT;
        uint32_t sy = y >> TS_SHIFT;
        if (sx >= TSW) sx = TSW - 1;
        if (sy >= TSH) sy = TSH - 1;
        uint32_t base = pol * POL_STRIDE;
        uint32_t sidx = base + (sy << TSW_LOG2) + sx;

        /* tile coords for the consensus histogram (full-res 16px tiles). */
        uint32_t tx = x >> TILE_SHIFT;
        uint32_t ty = y >> TILE_SHIFT;
        if (tx >= TW) tx = TW - 1;
        if (ty >= TH) ty = TH - 1;
        uint32_t tidx = (ty << TW_LOG2) + tx;
        uint16_t *hrow = &hist[tidx << 3];      /* 8 bins for this tile         */

        /* ---- 1. this event's 8 age-band coincidence vector C_e[8] ---- */
        int32_t ce[8] = {0,0,0,0,0,0,0,0};
        int32_t ce_sum = 0;
        for (uint32_t i = 0; i < 8; i++) {
            int32_t nx = (int32_t)sx + ODX[i];
            int32_t ny = (int32_t)sy + ODY[i];
            if (nx < 0 || nx >= (int32_t)TSW || ny < 0 || ny >= (int32_t)TSH)
                continue;
            uint32_t nidx = base + ((uint32_t)ny << TSW_LOG2) + (uint32_t)nx;
            if (!seen_get(nidx))
                continue;                        /* never fired -> no vote       */
            uint32_t age = (uint32_t)((tq - ts_surface[nidx]) & 0xFF);
            if (age < BAND_LO)      continue;    /* too fresh / self             */
            int32_t w;
            if      (age <= BAND_MID) w = W_NEAR;
            else if (age <= BAND_HI)  w = W_MID;
            else if (age <= BAND_FAR) w = W_FAR;
            else                      continue;  /* older than far band          */
            ce[OPP[i]] += w;
            ce_sum += w;
        }

        /* ---- 2. tile consensus from strictly earlier events (argmax+sum) ---- */
        int32_t h_sum = 0, h_win = -1;
        uint32_t d_cons = 0;
        for (uint32_t d = 0; d < 8; d++) {
            int32_t hv = hrow[d];
            h_sum += hv;
            if (hv > h_win) { h_win = hv; d_cons = d; }
        }

        /* ---- z_e (independent-motion agreement), all integer, no divide ----
           z_raw = (ce_sum - ce[d_cons]) + (max_{d!=d_cons} ce[d] >> 1)   [eta=1/2]
           expressed in the UN-normalised (fixed-point) vote domain: a bg event
           (mass at d_cons) -> small z; an independent event (mass off d_cons)
           -> large z. Suppress low-confidence tiles (hard gate via cross-mul).
           No learned background (h_sum==0) -> neutral: z=0 (do not accuse a
           quiescent tile; matches the reference damping quiescent bg toward the
           low end rather than raising a false positive). */
        int32_t z = 0;
        if (h_sum > 0 && ce_sum > 0) {
            /* confidence gate: h_win/h_sum >= conf_thr  <=>  h_win*DEN>=h_sum*NUM */
            int32_t hw5 = (h_win << 2) + h_win;      /* h_win * 5 (= CONF_DEN)    */
            int32_t hs2 = h_sum << 1;                /* h_sum * 2 (= CONF_NUM)    */
            if (hw5 >= hs2) {
                int32_t c_at = ce[d_cons];
                int32_t c_other = 0;
                for (uint32_t d = 0; d < 8; d++) {
                    if (d == d_cons) continue;
                    if (ce[d] > c_other) c_other = ce[d];
                }
                z = (ce_sum - c_at) + (c_other >> 1);   /* eta = 1/2 -> >>1      */
            }
        }

        if (z > best_z) { best_z = z; best_tile = tidx; }

        /* ---- 3. update state AFTER scoring (causal) ---- */
        if (ce_sum > 0) {
            for (uint32_t d = 0; d < 8; d++) {
                if (ce[d]) {
                    int32_t nv = (int32_t)hrow[d] + ce[d];
                    hrow[d] = (uint16_t)((nv > HIST_CAP) ? HIST_CAP : nv);
                }
            }
        }
        ts_surface[sidx] = tq;
        seen_set(sidx);
    }

    /* ---- per-batch GLOBAL background direction (stabilization bonus) ----
       Sum every tile's histogram into a single 8-bin global histogram; its
       argmax is the dominant background/reafference direction across the frame
       -- exactly the vector dvs_stabilize computes, via the shared
       direction-voting primitive. Compares/adds only. */
    int32_t g_bin[8] = {0,0,0,0,0,0,0,0};
    for (uint32_t t = 0; t < NT; t++) {
        uint16_t *hr = &hist[t << 3];
        for (uint32_t d = 0; d < 8; d++) g_bin[d] += hr[d];
    }
    int32_t g_win = -1;
    uint32_t g_dir = 0;
    for (uint32_t d = 0; d < 8; d++) {
        if (g_bin[d] > g_win) { g_win = g_bin[d]; g_dir = d; }
    }

    /* ---- pack the output word ---- */
    uint32_t zclamp = (best_z < 0) ? 0u : (uint32_t)best_z;
    if (zclamp > ZVAL_MAX) zclamp = ZVAL_MAX;
    uint32_t flag = (best_z >= THRESHOLD) ? 1u : 0u;
    uint32_t row  = (best_tile >> TW_LOG2) & 0x7;
    uint32_t col  = best_tile & 0x7;

    *FIFO_OUT = (flag << 14) | (zclamp << 9) | (row << 6) | (col << 3) | (g_dir & 0x7);
}

void main(void) {
    for (uint32_t c = 0; c < TS_CELLS; c++) ts_surface[c] = 0;
    for (uint32_t c = 0; c < (TS_CELLS >> 3); c++) seen[c] = 0;
    for (uint32_t k = 0; k < NT * 8; k++) hist[k] = 0;
    batch_ctr = 0;

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
