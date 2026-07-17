#include <stdint.h>

/* "THE HUMAN QUARTZ" (dvs_quartz) -- a chips/fpga demo app in the same shape as
   software/dvs_vital/main.c (fifo_in fires event_id_0 once BATCH words land;
   isr_handler reads them, updates the tap-timing ITI collector, latches a grade
   every 16 taps, and writes ONE sample word per batch; it NEVER calls wfi(),
   see the epilogue comment on isr_handler).

   Idea (quartz grade -- novel among these apps): grade a person's finger-TAP
   timing regularity like a crystal oscillator, purely from burst TIMING
   statistics.  x/y/pol are decoded per ABI but deliberately unused; the output
   word is position-invariant (scrambling x/y/pol leaves every output word
   identical, see identity (f) below).  A "burst" is a run of events with
   inter-event gaps dt < GAP_MIN; when the burst reaches exactly BURST_MIN_LEN
   events it is "confirmed" as a tap.  The inter-tap interval (ITI) between
   successive confirmed taps is collected until 16 ITIs have accumulated; the
   mean and the mean absolute deviation (MAD) of those 16 ITIs are then computed
   multiply-free (>>4 because exactly 16 samples), and a four-way grade is
   latched:
     QUARTZ     (3) -- MAD jitter <= J_QUARTZ=16  ticks
     METRONOME  (2) -- MAD jitter <= J_STEADY=64  ticks
     MORTAL HAND(1) -- MAD jitter <= J_MORTAL=256 ticks
     JELLY      (0) -- MAD jitter  > J_MORTAL ticks

   -------------------------------------------------------------------------
   Exact identities the offline validation checks:
     (a) Metronome test: P=2000 ticks, 60 taps of 8 intra-burst events with
         intra-burst dt=2, t0=1000.  Total events = 60*8 = 480; total batches
         = 480/4 = 120 words.  Tap j (0-based) is confirmed at the 4th event
         of burst j; the 4th event of burst j lands in batch 2j (0-based),
         because burst j spans events [8j,8j+7] and its 4th event is at index
         8j+3, in batch (8j+3)/4 = 2j.  ITI j (0-based, connecting tap j to
         tap j+1) is accepted during the batch that confirms tap j+1, i.e.
         batch 2(j+1) = 2j+2.  The 16th ITI (index 15, j=15) is accepted
         during batch 2*16=32; the latch fires mid-batch 32; word[31] (batch
         31, 0-based) carries prog=15, sseq=0; word[32] carries prog=0, sseq=1,
         meanq=62, jit=0, grade=3.  Second latch at batch 64: word[63] carries
         prog=15 sseq=1; word[64] carries prog=0 sseq=2 meanq=62 jit=0 grade=3.
         Third latch at batch 96: word[96] carries prog=0 sseq=3 meanq=62
         jit=0 grade=3.  Word[119] (last word) carries prog=11 sseq=3.
         (mean=2000, mean>>5=62.5 truncated to 62 = lat_meanq=62; jit=0.)
     (b) Grade ladder (all with mean=2000, meanq=62):
           [1900,2100]: jit=MAD({1900,2100,...}>>4)=(|100|+|100|+...) = 100
                        -> grade=1 (MORTAL HAND), lat_jit=100.
           [1984,2016]: jit=16, grade=3 (QUARTZ, boundary inclusive).
           [1950,2050]: jit=50, grade=2 (METRONOME).
           [1000,3000]: jit=1000, grade=0 (JELLY), lat_jit=1000 (below 1023
                        clamp -- the clamp is 1023 and 1000<1023 so stored as-is).
     (c) Dense sparkle (dt=8, < GAP_MIN=48): burst onset is never opened, no
         taps confirmed, iti_count stays 0, all output words are all-zero
         (prog=0, meanq=0, jit=0, grade=0, sseq=0).
     (d) Singletons (dt=5000 >= GAP_MIN): each event opens a 1-event burst
         that never reaches BURST_MIN_LEN=4; no taps confirmed; all-zero words.
     (e) Tempo gate: if the ITI between successive confirmed taps is < ITI_MIN
         (e.g., period 100 -> ITI=100 < 512), or > ITI_MAX (e.g., period 40000
         > 32768, or period=65536 which masks to ITI=0 < ITI_MIN), the ITI is
         rejected AND iti_count is reset to 0.  A graded measurement can only
         come from 16 CONSECUTIVE in-tempo confirmed taps.  All-zero output
         for these streams.
     (f) Position invariance: scrambling x, y, and pol in any order changes
         nothing -- the algorithm only reads ts.  Every output word is identical
         before and after any x/y/pol permutation.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk).  Every operation is a shift, add, sub,
   compare, or logical:
     - wrap subtract    : masked 16-bit wrap subtract (&0xFFFF); no multiply.
     - dt / burst track : compare against GAP_MIN; no multiply.
     - burst confirm    : compare cur_len == BURST_MIN_LEN; no multiply.
     - ITI compute      : masked wrap subtract; no multiply.
     - tempo gate       : compare iv against ITI_MIN/ITI_MAX; no multiply.
     - sum (16 ITIs)    : 16 additions; max 16*32768=524288, fits uint32_t.
     - mean             : sum >> 4 (exact because NTAPS=16 is a power of 2).
     - abs deviation    : compare-select (a>b ? a-b : b-a); no multiply.
     - MAD sum          : 16 additions of abs deviations.
     - jit (MAD)        : madsum >> 4 (exact because NTAPS=16).
     - grade            : three compare branches; no multiply.
     - clamp meanq/jit  : compare + conditional assign; no multiply.
     - sseq             : increment and mask (&0xF); no multiply.
     - output pack      : shift/OR of all fields; no multiply.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_entropy / dvs_vital):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24   -- decoded but UNUSED
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17   -- decoded but UNUSED
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp)         -- USED (timing app)
     pol =  word        & 1                                    -- decoded but UNUSED
   x, y, and pol are decoded per ABI (see below) but unused -- this app is
   position-invariant; only ts drives the tap-timing logic.

   -------------------------------------------------------------------------
   TIMEBASE: this app IS ts-driven (same as dvs_vital).  The timestamp field
   is a 16-bit wrapping tick counter.  Masked 16-bit subtraction gives exact
   deltas for true gaps < 65536 ticks; gaps longer than 65535 ticks alias (a
   sustained silence of >65535 ticks can appear as a short gap).  For typical
   human tapping rates (periods 500..5000 ticks) ITIs are well below this alias
   point.  Recorded chips/fpga CSVs carry a wrapped coarse counter; offline
   validation builds its own synthetic timestamps to match the wrapped 16-bit
   arithmetic exactly.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS 126x112 and VERY noisy).  Three documented
   multiply-free guards:
     1. DENSITY GUARD.  Dense sparkle or hot-pixel chatter produces events with
        inter-event gaps dt < GAP_MIN on every step.  The burst-open threshold
        (dt >= GAP_MIN) is never crossed, so no burst onsets are ever recorded
        and no taps are ever confirmed.  iti_count stays 0 throughout and all
        output words carry prog=0 sseq=0 (all zero).  Noise cannot fake a
        graded measurement.  This guard costs nothing extra: it is a side-effect
        of the burst-open condition already required for correctness.
     2. SINGLETON GUARD.  Isolated noise events that ARE spaced by dt >= GAP_MIN
        each open a 1-event burst (cur_len=1), but cur_len never reaches
        BURST_MIN_LEN=4 before the next gap re-opens a new burst.  No burst is
        ever confirmed, no ITI is ever counted, and all output words are all-zero.
        This guard is also free: it is the same BURST_MIN_LEN comparison already
        needed for real taps.
     3. TEMPO GATE.  A confirmed-burst pair closer than ITI_MIN (contact bounce,
        a noise burst adjacent to a real tap) or farther than ITI_MAX (walk-away
        pause, or a >65535-tick alias wrapping to a small or zero ITI) is
        rejected AND resets iti_count to 0.  Consequently a graded measurement
        can only come from 16 CONSECUTIVE in-tempo confirmed taps.  Caveat
        (documented honestly): a genuinely periodic strobe or mechanical actuator
        would legitimately grade QUARTZ -- the instrument measures whatever
        rhythm is present.

   -------------------------------------------------------------------------
   Cold-start wart (deterministic, mirrored by the host): last_ts starts 0 and
   cur_onset_ts starts 0, so a stream that opens with dt < GAP_MIN (i.e., the
   first event's ts is within GAP_MIN of 0) can "confirm" a phantom burst with
   onset ts 0.  At that confirmation have_prev=0, so no ITI is counted -- only
   prev_onset_ts is set to 0 and have_prev becomes 1.  This is the only side-
   effect: the first real ITI measurement will be anchored to ts 0 rather than
   to the true first burst's onset.  The host mirrors this behaviour exactly.

   -------------------------------------------------------------------------
   LATCH-before-emit: the latch happens inline when the 16th ITI is accepted
   (mid-batch, before the current batch's emit).  Consequently the word emitted
   for the batch that latches the 16th ITI already carries the new grade, the
   incremented sseq, and prog=0 (since iti_count was reset to 0 after the
   latch).  Word[31] still carries prog=15, sseq=0; word[32] carries prog=0,
   sseq=1, meanq=62, jit=0, grade=3 (for the P=2000 metronome identity above).

   -------------------------------------------------------------------------
   Output word layout (32 bits, bit[31]=0):
     bits[ 3: 0] = prog  (iti_count, 0..15, live progress toward next grade)
     bits[14: 4] = meanq (latched mean ITI >> 5, clamped to 2047)
     bits[24:15] = jit   (latched MAD jitter in ticks, clamped to 1023)
     bits[26:25] = grade (0=JELLY, 1=MORTAL HAND, 2=METRONOME, 3=QUARTZ)
     bits[30:27] = sseq  (4-bit session counter, wraps mod 16; 0=no measurement yet)
     bit [31]    = 0
   Host unpacks these fields to read tap quality. */

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

#ifndef ITI_MIN
#define ITI_MIN 512             /* minimum acceptable inter-tap interval (tempo gate low) */
#endif

#ifndef ITI_MAX
#define ITI_MAX 32768           /* maximum acceptable inter-tap interval (tempo gate high) */
#endif

#ifndef J_QUARTZ
#define J_QUARTZ 16             /* MAD jitter <= this -> QUARTZ grade */
#endif

#ifndef J_STEADY
#define J_STEADY 64             /* MAD jitter <= this -> METRONOME grade */
#endif

#ifndef J_MORTAL
#define J_MORTAL 256            /* MAD jitter <= this -> MORTAL HAND grade */
#endif

/* Non-tunable: NTAPS must be exactly 16 so the mean and MAD divides reduce to
   >>4 right-shifts (a power-of-2 shift, not a multiply).  Changing this to any
   non-power-of-2 value would require a software divide, violating the RV32I
   multiply-free constraint.  Do NOT make NTAPS tunable. */
#define NTAPS 16

/* Non-tunable clamp ceilings for the latched output fields. */
#define MEANQ_MAX 2047u         /* 11 bits in the output word: bits[14:4] */
#define JIT_MAX   1023u         /* 10 bits in the output word: bits[24:15] */

/* Non-tunable session-sequence counter mask (4-bit, wraps mod 16). */
#define SSEQ_MASK 0xFu

/* Last event's timestamp (16-bit value).  Zeroed by crt0.S. */
static uint32_t last_ts;

/* Onset timestamp of the current (open, unconfirmed) burst.  Zeroed by
   crt0.S; see cold-start wart note in the header. */
static uint32_t cur_onset_ts;

/* Number of events in the current burst (saturates at 255).  Zeroed by
   crt0.S. */
static uint32_t cur_len;

/* Onset timestamp of the last CONFIRMED tap (used to compute ITI).
   Zeroed by crt0.S. */
static uint32_t prev_onset_ts;

/* 0 until the first tap confirmation; guards the first ITI measurement.
   Zeroed by crt0.S. */
static uint32_t have_prev;

/* Accumulated inter-tap intervals (16 slots).  Zeroed by crt0.S; reset to 0
   after each latch by setting iti_count=0 (the next NTAPS writes overwrite). */
static uint32_t iti[16];

/* Number of ITIs collected since the last latch (0..15).  Zeroed by crt0.S. */
static uint32_t iti_count;

/* Latched stats from the last completed 16-tap measurement (emitted each
   batch).  All 0 before the first measurement completes; zeroed by crt0.S. */
static uint32_t lat_meanq;
static uint32_t lat_jit;
static uint32_t lat_grade;

/* 4-bit session sequence counter (0..15, wraps).  0 means no measurement yet.
   Zeroed by crt0.S. */
static uint32_t sseq;

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
       burst tracker, and collect confirmed ITIs. */
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
                /* Tap confirmed EXACTLY once (cur_len reaches BURST_MIN_LEN
                   exactly; subsequent increments go past BURST_MIN_LEN, never
                   re-trigger). */
                if (have_prev) {
                    uint32_t iv = (cur_onset_ts - prev_onset_ts) & TS_MASK;
                    if (iv >= (uint32_t)ITI_MIN && iv <= (uint32_t)ITI_MAX) {
                        /* In-tempo ITI: store and advance collector. */
                        iti[iti_count] = iv;
                        iti_count++;
                        if (iti_count == (uint32_t)NTAPS) {
                            /* LATCH: 16 ITIs collected.  Compute mean and MAD
                               using only >>4 shifts (NTAPS=16 is a power of 2). */
                            uint32_t sum = 0u;
                            for (uint32_t k = 0u; k < (uint32_t)NTAPS; k++) {
                                sum += iti[k];
                            }
                            uint32_t mean = sum >> 4;   /* exact: NTAPS=16 */

                            uint32_t madsum = 0u;
                            for (uint32_t k = 0u; k < (uint32_t)NTAPS; k++) {
                                uint32_t d = (iti[k] > mean)
                                             ? (iti[k] - mean)
                                             : (mean - iti[k]);
                                madsum += d;
                            }
                            uint32_t jit = madsum >> 4; /* exact: NTAPS=16 */

                            /* Grade from unclamped jit (contract: grade computed
                               before any clamping). */
                            uint32_t grade;
                            if      (jit <= (uint32_t)J_QUARTZ) grade = 3u;
                            else if (jit <= (uint32_t)J_STEADY)  grade = 2u;
                            else if (jit <= (uint32_t)J_MORTAL)  grade = 1u;
                            else                                  grade = 0u;

                            /* Clamp and store latched fields. */
                            lat_meanq = mean >> 5;
                            if (lat_meanq > MEANQ_MAX) lat_meanq = MEANQ_MAX;
                            lat_jit   = (jit > JIT_MAX) ? JIT_MAX : jit;
                            lat_grade = grade;

                            sseq = (sseq + 1u) & SSEQ_MASK;
                            iti_count = 0u;
                        }
                    } else {
                        /* Tempo gate: out-of-range ITI; reject and reset
                           collection so the next measurement starts fresh. */
                        iti_count = 0u;
                    }
                }
                prev_onset_ts = cur_onset_ts;
                have_prev = 1u;
            }
        }
    }

    /* Emit ONE word per batch from the LATCHED values and the live iti_count.
       Layout: bits[3:0]=prog, bits[14:4]=meanq, bits[24:15]=jit,
               bits[26:25]=grade, bits[30:27]=sseq, bit[31]=0 */
    *FIFO_OUT = (sseq      << 27)
              | (lat_grade << 25)
              | (lat_jit   << 15)
              | (lat_meanq <<  4)
              |  iti_count;
}

void main(void) {
    /* .bss is already zeroed by crt0.S -- the correct cold start for ALL state
       in this app.  last_ts, cur_onset_ts, cur_len, prev_onset_ts, have_prev,
       iti[], iti_count, lat_meanq, lat_jit, lat_grade, and sseq all start at
       0 without any explicit initialisation here.  See the cold-start wart
       note in the header comment for the one documented deterministic artefact
       of starting last_ts=0. */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
