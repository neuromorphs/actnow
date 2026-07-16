#include <stdint.h>

/* chips/fpga variant that estimates the GLOBAL BACKGROUND MOTION direction (a
   2-DOF vector) for scene stabilization -- the complement of object-motion
   (OMS) work. OMS suppresses the coherent background flow to pop out an
   independently-moving object; here we WANT that background flow vector, so a
   downstream stabilizer can subtract it (de-rotate/de-translate the frame, or
   steer a gimbal). Same interrupt/FIFO wiring as software/dvs_motion/main.c
   and software/dvs_rotate/main.c (fifo_in fires event_id_0 once BATCH words
   land; isr_handler pops them, does its work, writes result word(s), returns),
   but the "work" is a per-event time-surface flow estimator, and the output is
   one packed motion-vector word per BATCH-sized batch (aggregate, not one word
   per event -- the useful signal is the batch's net direction, not any single
   event).

   ===================== ALGORITHM (multiply-free, O(1)/event) ================

   A DVS edge moving across the array leaves a trail: pixels it has ALREADY
   crossed fired recently, pixels ahead of it have not fired yet. So at a fresh
   event (x,y), the neighbour that fired MOST RECENTLY is the one the edge came
   FROM -- the local flow points from that neighbour toward (x,y). We read that
   off a "time surface" T[y][x] (a.k.a. Surface of Active Events, SAE): a map
   holding, per pixel, a coarse timestamp of its last event.

   Per event (x,y):
     1. Read the recency of the 4 axis-neighbours (left,right,up,down) of (x,y)
        from T.
     2. Horizontal vote: if the LEFT neighbour is more recent than the RIGHT
        one, the edge arrived from the left => it is moving in +x; vote sgn=+1.
        If RIGHT is more recent, vote -1. (Symmetric for vertical with up/down.)
        These are the sign of the normal-flow component along each axis --
        obtained by a single integer compare per axis, no gradient, no divide.
     3. Accumulate: sum_dx += hvote; sum_dy += vvote.
     4. Stamp T[y][x] = now  (this pixel is now the most-recent one).

   Per batch (every BATCH events):
     - Decay the running vector by halving (exponential forget, exactly like
       dvs_motion's grid) so (acc_dx,acc_dy) tracks the CURRENT pan and forgets
       old motion over a few batches instead of latching.
     - Add this batch's (sum_dx,sum_dy).
     - Emit a packed word: sign+magnitude of dx and dy, plus an 8-octant
       direction code and a coarse magnitude -- all derived by compares/shifts
       only (NO atan2, NO multiply, NO divide; see pack_result()).

   For coherent background pan (translation), most events vote the same way, so
   (acc_dx,acc_dy) points consistently in the pan direction; incoherent sensor
   noise votes cancel. That is exactly the global-motion vector a stabilizer
   subtracts.

   ===================== NOISE STRATEGY =====================================

   The SciDVS is 126x112 and VERY noisy: isolated background-activity events and
   hot pixels fire with no correlated neighbour. Two defences, both multiply-free:

   (1) The vote structure already self-cancels INCOHERENT noise: a scattered
       noise event votes in a random direction, and those votes cancel over a
       batch while the coherent pan survives. So mild noise costs magnitude, not
       direction.
   (2) An OPTIONAL spatio-temporal correlation pre-filter (the same technique as
       software/dvs_track/main.c and dvs_denoise, jAER's SpatioTemporalCorrelation
       Filter, Guo & Delbruck T-PAMI 2022): an event feeds the time surface/votes
       only if >= CORR_MIN of its 8 cell-neighbours (EXCLUDING itself, so hot
       pixels find no support) fired within the last CORR_WINDOW events. This
       stops a hot pixel from repeatedly stamping the surface and skewing a
       neighbour's recency comparison. Tunable via -DCORR_MIN / -DCORR_WINDOW;
       CORR_MIN=0 disables it (default keeps it on). The correlation grid reuses
       the time surface's own super-pixel cells, so it costs no extra memory
       beyond a per-cell last-touched index array.

   ===================== RV32I (no mul/div) WORKAROUNDS =======================

   This core is plain RV32I -- no hardware multiply/divide (see
   software/common/program.mk's -march=rv32i). Every step is shifts/adds/
   compares:

   * TIME SURFACE ADDRESSING. T is a 2-D map flattened to 1-D. A real
     row*WIDTH+col needs a multiply. We DOWNSAMPLE by 2 (TS_SHIFT=1): tx=x>>1,
     ty=y>>1, so the surface is TW x TH = 63 x 56. To index it without a
     multiply, TW is padded up to the next power of two (TW_P2 = 64 = 1<<6), so
     row*stride is a shift: idx = (ty << TW_LOG2) | tx. The few unused columns
     (63..63 padded to 64) cost 56 bytes and buy a shift-only index. Downsample
     by 2 also (a) quarters the memory, (b) makes the recency comparison robust
     -- one 2x2 super-pixel collects ~4x the events, so a neighbour's timestamp
     is far more likely to be populated and meaningful than at full res, which
     sharpens the "which side is more recent" decision. Full-res (126x112,
     ~14 KB) would also fit the 32 KB SRAM, but the shift-index trick and the
     denser super-pixels make >>1 the better default; raise TS_SHIFT for more
     smoothing / less memory, lower it (to 0) for full spatial resolution --
     but then TW_P2/TW_LOG2 must be re-derived so TW_P2 >= TW.

   * RECENCY CLOCK. evt_pack.v now supplies a real 16-bit timestamp field
     ((word>>1)&0xFFFF) that is live and monotonic on hardware, but the recorded
     kr260_capture.py CSVs still store events in arrival order WITHOUT per-event
     timestamps (ts constant), so for portability across both we derive "time"
     from a monotonic per-event COUNTER `now`, quantized
     to a byte by `now >> TICK_SHIFT`. Arrival order IS the time order for an
     event stream, so this is correct regardless of whether the sensor supplies
     real timestamps; if it does, swapping `now` for the real ts costs nothing
     else. The byte wraps every 256<<TICK_SHIFT events; a neighbour older than
     one wrap can read as "newer" (a stale vote), but decay + the sheer event
     rate make this a rare, self-correcting minority vote, never a bias.

   * NORMAL-FLOW SIGN, NOT ANGLE. We never compute a gradient magnitude or an
     angle per event -- only `T[left] > T[right]` style compares giving a unit
     vote in {-1,0,+1} per axis. Summing unit votes over a batch is an integer
     accumulate; the aperture-averaged result approximates the true flow
     direction without a single multiply. (Aperture problem: one edge only
     reveals its NORMAL-flow component; averaging many edge orientations over a
     batch recovers the global translation direction. See limitations below.)

   * OUTPUT PACKING. Octant + magnitude come from compares and shifts only
     (abs via branch, octant via 3 sign/compare tests, magnitude via a
     saturating clamp), never atan2/sqrt/multiply -- see pack_result().

   ===================== OUTPUT WORD LAYOUT ==================================

     bit    31..16 : 0
     bit    15     : sign of acc_dx (1 = negative)
     bits   14..11 : |acc_dx| clamped to 0..15
     bit    10     : sign of acc_dy (1 = negative)
     bits    9..6  : |acc_dy| clamped to 0..15
     bits    5..3  : 8-octant direction code (0=E,1=NE,2=N,...,7=SE), 7 if still
     bits    2..0  : coarse magnitude 0..7 (Chebyshev max(|dx|,|dy|) clamped)

   The Python mirror chips/fpga/dvs_stabilize_view.py unpacks this identically,
   and chips/fpga/tests/e2e/e2e_fpga_stabilize_test.act asserts every result
   against the same integer math computed in Python.

   ===================== STATE / COMPUTE BUDGET =============================

   State: time surface  TW_P2 * TH = 64 * 56 = 3584 bytes (uint8), plus two
   int32 accumulators -- ~3.6 KB. With the correlation gate on (CORR_MIN>0) a
   parallel last_touched[TS_CELLS] uint32 array adds 4*3584 = 14336 bytes, for
   ~18 KB total -- still comfortably inside the 32 KB SRAM. (Full-res surface
   would be ~14 KB, also fine.)
   Compute per event: 4 surface reads + 2 compares + 2 adds + 1 store + 1
   counter increment -- all O(1), no loop, no mul/div. Per batch: 2 halve+add
   decays + the pack (a handful of compares/shifts).

   ===================== LIMITATIONS (honest) ==============================

   * APERTURE PROBLEM: a single moving edge only reveals the flow component
     NORMAL to it; the tangential component is invisible locally. This estimator
     recovers the true translation only in AGGREGATE, by averaging normal-flow
     votes over many differently-oriented edges in a batch. A scene dominated by
     one edge orientation biases the estimate toward that edge's normal.
   * TRANSLATION ONLY: (acc_dx,acc_dy) is a 2-DOF translational vector. Camera
     ROLL/rotation and looming/zoom produce a spatially-varying flow field whose
     votes partly cancel -- this reports their net translation, not the rotation.
   * FOREGROUND CONTAMINATION: a large independently-moving object contributes
     its own votes and pulls the mean. Decay limits its persistence, but for
     robustness a downstream 8-bin direction histogram (dominant mode = the
     background, robust to a minority object) is the standard mitigation; it is
     left out here to keep the per-event path a handful of ops, and noted as the
     next step. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112

/* Input event ABI -- the word evt_pack.v packs on real hardware and
   software/dvs_track/main.c decodes: x in bits [30:24], y in [23:17],
   timestamp in [16:1], polarity in [0]. Reading x/y from the low bits (as an
   earlier revision and the stale upstream dvs_motion/rotate do) reads the
   timestamp/polarity bits on the FPGA instead -- so the flow tracks noise, not
   the pan. The chips/fpga mirrors pack the same way (see dvs_track_live.py). */
#define X_SHIFT 24
#define Y_SHIFT 17

/* Time-surface geometry. Downsample by 2 (TS_SHIFT=1): the surface is
   TW x TH super-pixels. TW is padded to the next power of two (TW_P2) so the
   row stride is a shift, not a multiply -- idx = (ty << TW_LOG2) | tx.
   TS_SHIFT=1 -> TW = ceil(126/2) = 63, TH = ceil(112/2) = 56; TW_P2 = 64. */
#define TS_SHIFT 1
#define TW   ((SX + (1 << TS_SHIFT) - 1) >> TS_SHIFT)   /* = 63 */
#define TH   ((SY + (1 << TS_SHIFT) - 1) >> TS_SHIFT)   /* = 56 */
#define TW_LOG2 6                                       /* 1<<6 = 64 >= TW */
#define TW_P2   (1 << TW_LOG2)                          /* = 64 padded stride  */
#define TS_CELLS (TW_P2 * TH)                           /* = 3584 bytes        */

/* Recency clock: monotonic per-event counter quantized to a byte. TICK_SHIFT
   spreads the 8-bit surface value over 256<<TICK_SHIFT events before wrap. */
#define TICK_SHIFT 4

/* Output packing clamps. */
#define DXY_MAX 15   /* 4-bit |dx|/|dy| field cap        */
#define MAG_MAX 7    /* 3-bit coarse magnitude field cap */

/* Optional spatio-temporal correlation noise gate (see NOISE STRATEGY in the
   header). Reuses the time-surface super-pixel grid. CORR_MIN=0 disables it.
   Overridable at build time so the dashboard can retune without editing here. */
#ifndef CORR_WINDOW
#define CORR_WINDOW 30   /* events; a neighbour must have fired within this to count */
#endif
#ifndef CORR_MIN
#define CORR_MIN 2       /* of 8 neighbours that must be recent; 0 disables the gate */
#endif

static uint8_t ts_surface[TS_CELLS];   /* the time surface T[ty][tx]           */
static int32_t acc_dx;                 /* decaying global-motion x accumulator */
static int32_t acc_dy;                 /* decaying global-motion y accumulator */
static uint32_t now;                   /* monotonic event counter (recency)    */
#if CORR_MIN > 0
static uint32_t last_touched[TS_CELLS];/* event index each cell last fired at; 0=never */
static uint32_t event_count;           /* "now" for the correlation gate (see track) */
static int is_recent(uint32_t last, uint32_t nowc) {
    return (last != 0) && ((nowc - last) <= CORR_WINDOW);
}
#endif

static int32_t absi(int32_t v) {
    return (v < 0) ? -v : v;
}

/* Clamp a signed value's magnitude into an n-bit unsigned field and return the
   sign bit separately -- shift-and-compare only. */
static uint32_t clamp_mag(int32_t v, int32_t cap) {
    int32_t a = absi(v);
    if (a > cap) a = cap;
    return (uint32_t)a;
}

/* 8-octant direction code from (dx,dy), compares only -- no atan2. Octants,
   CCW from East: 0=E,1=NE,2=N,3=NW,4=W,5=SW,6=S,7=SE. A perfectly still batch
   (dx==0 && dy==0) returns 7 as a sentinel "no motion" (magnitude field will
   be 0, so a decoder distinguishes it from a real SE by mag==0).
   Note: +y is DOWN in sensor space, so dy>0 means motion toward the bottom,
   which we label "South". */
static uint32_t octant(int32_t dx, int32_t dy) {
    if (dx == 0 && dy == 0) return 7u;

    int32_t ax = absi(dx);
    int32_t ay = absi(dy);

    /* Is the vector closer to an axis (E/N/W/S) or a diagonal (NE/NW/SW/SE)?
       Diagonal when |dx| and |dy| are comparable; axis when one dominates.
       "Comparable" = neither more than ~2x the other, tested with shifts:
       ax <= 2*ay  <=>  ax <= (ay<<1), and vice versa. */
    int32_t diagonal = (ax <= (ay << 1)) && (ay <= (ax << 1));

    if (!diagonal) {
        /* pure axis: pick by which magnitude dominates and its sign */
        if (ax >= ay) return (dx >= 0) ? 0u : 4u;   /* E : W */
        return (dy < 0) ? 2u : 6u;                  /* N : S (dy<0 is up=N) */
    }
    /* diagonal quadrant by the two signs (dy<0 = up = North half) */
    if (dx >= 0) return (dy < 0) ? 1u : 7u;         /* NE : SE */
    return (dy < 0) ? 3u : 5u;                       /* NW : SW */
}

static uint32_t pack_result(void) {
    uint32_t sx = (acc_dx < 0) ? 1u : 0u;
    uint32_t sy = (acc_dy < 0) ? 1u : 0u;
    uint32_t mx = clamp_mag(acc_dx, DXY_MAX);
    uint32_t my = clamp_mag(acc_dy, DXY_MAX);

    uint32_t oct = octant(acc_dx, acc_dy);

    /* Coarse magnitude = Chebyshev max(|dx|,|dy|) clamped to 3 bits (compares
       only; L1/L2 would need adds/sqrt -- max is enough for a stabilizer's
       "how fast" hint). */
    int32_t ax = absi(acc_dx);
    int32_t ay = absi(acc_dy);
    int32_t cheb = (ax > ay) ? ax : ay;
    uint32_t mag = clamp_mag(cheb, MAG_MAX);

    return (sx << 15) | (mx << 11) | (sy << 10) | (my << 6) | (oct << 3) | mag;
}

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to
   the instruction after it, so a wfi() call inside an ISR permanently skips
   that ISR's own epilogue (the stack pointer's restore), leaking 16 bytes of
   stack every interrupt until it eventually collides with this program's own
   code (see software/dvs_motion/main.c's isr_handler comment for the full
   explanation). Just returning is correct: this function's own `ret` lands on
   the same cached wfi() site main()'s return already relies on. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }

    /* Decay first (halve), exactly like dvs_motion's grid, so old motion fades
       over a few batches and the vector tracks the current pan. */
    acc_dx = acc_dx - (acc_dx >> 1);
    acc_dy = acc_dy - (acc_dy >> 1);

    int32_t sum_dx = 0;
    int32_t sum_dy = 0;

    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x = (v[i] >> X_SHIFT) & 0x7F;
        uint32_t y = (v[i] >> Y_SHIFT) & 0x7F;

        /* Guard: a packed word could carry x/y outside the SXxSY frame; clamp
           to the surface so neighbour reads stay in bounds (no divide). */
        uint32_t tx = x >> TS_SHIFT;
        uint32_t ty = y >> TS_SHIFT;
        if (tx >= TW) tx = TW - 1;
        if (ty >= TH) ty = TH - 1;

        uint32_t idx = (ty << TW_LOG2) | tx;

#if CORR_MIN > 0
        /* Spatio-temporal correlation gate: count recent touches in the 3x3
           super-pixel neighbourhood, EXCLUDING self (so hot pixels find no
           support). Update last_touched for every event, dropped or not, so a
           genuine new edge can bootstrap. */
        event_count++;
        {
            uint32_t ncorr = 0;
            int has_l = tx > 0, has_r = tx < TW - 1;
            int has_u = ty > 0, has_d = ty < TH - 1;
            if (has_l)          ncorr += is_recent(last_touched[idx - 1],         event_count);
            if (has_r)          ncorr += is_recent(last_touched[idx + 1],         event_count);
            if (has_u)          ncorr += is_recent(last_touched[idx - TW_P2],     event_count);
            if (has_d)          ncorr += is_recent(last_touched[idx + TW_P2],     event_count);
            if (has_l && has_u) ncorr += is_recent(last_touched[idx - TW_P2 - 1], event_count);
            if (has_r && has_u) ncorr += is_recent(last_touched[idx - TW_P2 + 1], event_count);
            if (has_l && has_d) ncorr += is_recent(last_touched[idx + TW_P2 - 1], event_count);
            if (has_r && has_d) ncorr += is_recent(last_touched[idx + TW_P2 + 1], event_count);
            last_touched[idx] = event_count;
            if (ncorr < CORR_MIN) {
                continue;   /* uncorrelated -- background noise or a hot pixel */
            }
        }
#endif

        /* Neighbour recencies (0 where never seen). Edge super-pixels read
           their own cell for the missing side, which yields no vote there. */
        uint32_t rleft  = (tx > 0)      ? ts_surface[idx - 1]       : ts_surface[idx];
        uint32_t rright = (tx < TW - 1) ? ts_surface[idx + 1]       : ts_surface[idx];
        uint32_t rup    = (ty > 0)      ? ts_surface[idx - TW_P2]   : ts_surface[idx];
        uint32_t rdown  = (ty < TH - 1) ? ts_surface[idx + TW_P2]   : ts_surface[idx];

        /* The more-recent side is where the edge came FROM; flow points away
           from it, toward this pixel. Unit vote per axis, compares only. */
        if (rleft > rright)      sum_dx += 1;   /* came from left  -> moving +x */
        else if (rright > rleft) sum_dx -= 1;   /* came from right -> moving -x */

        if (rup > rdown)         sum_dy += 1;   /* came from above -> moving +y (down) */
        else if (rdown > rup)    sum_dy -= 1;   /* came from below -> moving -y (up)   */

        /* Stamp this pixel as the most-recent one. */
        now++;
        ts_surface[idx] = (uint8_t)(now >> TICK_SHIFT);
    }

    acc_dx += sum_dx;
    acc_dy += sum_dy;

    *FIFO_OUT = pack_result();
}

void main(void) {
    for (uint32_t c = 0; c < TS_CELLS; c++) {
        ts_surface[c] = 0;
    }
    acc_dx = 0;
    acc_dy = 0;
    now = 0;
#if CORR_MIN > 0
    for (uint32_t c = 0; c < TS_CELLS; c++) last_touched[c] = 0;
    event_count = 0;
#endif

    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
