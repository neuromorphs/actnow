#include <stdint.h>

/* "ENTROPY'S BLOODHOUND" (dvs_entropy) -- a chips/fpga demo app in the same
   shape as software/dvs_loom/main.c (fifo_in fires event_id_0 once BATCH
   words land; isr_handler reads them, updates the per-pixel last-polarity
   state and the window forward/reverse transition counters, and writes ONE
   sample word per batch; it NEVER calls wfi(), see the epilogue comment on
   isr_handler).

   Idea (arrow-of-time detector -- novel among these apps): the chip sniffs
   the ARROW OF TIME in the event stream. Per pixel it remembers the last
   event polarity; an ON->OFF transition at the same pixel (brightness rose,
   then fell -- "decay") bumps the forward counter C_fwd; an OFF->ON
   transition ("kindle") bumps the reverse counter C_rev. Over a sliding
   window, D = C_fwd - C_rev is a time-asymmetry statistic: scenes dominated
   by fading/decay push D positive ("time runs FORWARD"), while scenes
   dominated by brightening push D negative ("time runs BACKWARD"). The party
   trick: feeding the SAME recording with its event order REVERSED exactly
   swaps the two counters, so the thermodynamic verdict provably flips.
   Symmetric noise (hot-pixel chatter alternating polarity) cancels in the
   difference. The host draws a thermodynamic verdict gauge.

   -------------------------------------------------------------------------
   Exact symmetry identities (offline validation checklist):
     (1) Reversing the event order alone swaps C_fwd<->C_rev exactly (per
         pixel, each a->b adjacent-pair transition becomes b->a; the
         "unseen-state" first event of each pixel's sequence contributes no
         transition in either direction) -> verdict flips sign.
     (2) Flipping ALL polarities alone swaps C_fwd<->C_rev exactly (every
         ON->OFF becomes OFF->ON and vice versa) -> verdict flips sign.
     (3) Reversal + polarity-flip TOGETHER preserves both counters exactly:
         a backwards movie with inverted contrast is statistically
         indistinguishable to this detector (Loschmidt symmetry) -> verdict
         preserved.
     (4) A strictly alternating-polarity chattering pixel contributes at most
         |C_fwd - C_rev| <= 1 to D, so K hot pixels shift D by at most K.
         Setting MARGIN > K (default 16) makes pure chatter provably UNDECIDED.

   -------------------------------------------------------------------------
   Multiply-free by construction (plain RV32I, -march=rv32i -- no mul/div,
   see software/common/program.mk). Every operation is a shift, add, sub,
   compare, or logical:
     - pixel index       : idx = (y << 7) | x  -- stride 128 = 1<<7 is a
                           power of two so the row index is a shift, the
                           column an OR; no multiply. The 16384-entry array
                           spans every 7-bit (x,y) so the masked fields can
                           never index out of bounds.
     - state comparison  : compare state[idx] with STATE_ON / STATE_OFF;
                           just integer compares.
     - saturating counts : if (c_fwd < COUNT_CAP) c_fwd++;  compare + add.
     - window latch      : batch_in_window++; compare with WINDOW_BATCHES;
                           c_fwd=0; c_rev=0; wseq=(wseq+1)&0xF; all
                           shift/add/and/compare.
     - verdict           : D = lat_fwd - lat_rev; compare D with +MARGIN and
                           -MARGIN; two compares, one subtraction.
     - output pack       : (wseq<<22)|(v<<20)|(lat_rev<<10)|lat_fwd; shifts
                           and ORs only.
     No multiply anywhere.

   -------------------------------------------------------------------------
   The event word (evt_pack.v, decoded like software/dvs_flinch / dvs_loom):
     x   = (word >> 24) & 0x7F     (0..125)   -- X_SHIFT=24
     y   = (word >> 17) & 0x7F     (0..111)   -- Y_SHIFT=17
     ts  = (word >> 1)  & 0xFFFF   (16-bit timestamp field; decoded per ABI
                                    but UNUSED by this app -- the window
                                    timebase is event-order driven, so
                                    dvs_entropy validates identically on any
                                    recording regardless of whether ts is real
                                    microseconds or a wrapped coarse counter)
     pol =  word        & 1
   (Several earlier apps read x/y/ts from the LOW bits -- the STALE layout;
   on the FPGA that reads the wrong bits. This app matches evt_pack.v +
   dvs_flinch.)

   -------------------------------------------------------------------------
   TIMEBASE: this app is event-ORDER driven. Timestamps are decoded per ABI
   (above) but never used for any arithmetic. The window advances every
   WINDOW_BATCHES batches (= WINDOW_BATCHES*BATCH events). Replay-speed and
   timestamp wrapping have no effect; identity (1) holds on any player.

   -------------------------------------------------------------------------
   NOISE STRATEGY (SciDVS is 126x112 and VERY noisy). Two guards, both
   multiply-free:
     1. PER-PIXEL DIFFERENCE CANCELLATION. Hot pixels tend to chatter with
        strictly alternating polarity: ON,OFF,ON,OFF,... Each alternating
        adjacent pair contributes exactly one C_fwd++ and one C_rev++, so
        they cancel in D. By identity (4), K hot pixels shift D by at most K;
        MARGIN > K makes pure chatter provably UNDECIDED. This is the primary
        multiply-free noise guard.
     2. MARGIN DEAD-BAND (UNDECIDED zone). The verdict is 0 (undecided)
        whenever |D| < MARGIN. This dead-band absorbs residual chatter that
        has not perfectly cancelled (e.g., truncated alternating runs at
        window boundaries). No multiply needed: just two signed comparisons
        against +/-MARGIN.

   -------------------------------------------------------------------------
   Window timing note: the latch and wseq advance happen BEFORE the emit on
   the closing batch of a window. Consequently, output word index i (0-based)
   carries wseq == ((i + 1) / WINDOW_BATCHES) & 0xF. The first WINDOW_BATCHES
   words (i=0..WINDOW_BATCHES-1) carry wseq=0 with lat_fwd=lat_rev=0
   (uninitialised) until the very last of them triggers the first latch; word
   i=WINDOW_BATCHES-1 already carries the completed first window's counts and
   wseq=1. Host code should treat wseq=0 as "not yet valid" or skip the first
   window if a clean start matters.

   -------------------------------------------------------------------------
   Output word layout (26 bits used):
     bits[ 9: 0] = fwd   (latched ON->OFF count, saturated at COUNT_CAP=1023)
     bits[19:10] = rev   (latched OFF->ON count, saturated at COUNT_CAP=1023)
     bits[21:20] = verdict  (0=undecided, 1=time-FORWARD/decay-dominated,
                             2=time-BACKWARD/kindle-dominated)
     bits[25:22] = wseq  (4-bit window sequence counter, wraps mod 16)
   Host unpacks these fields; see chips/fpga/dvs_entropy_view.py's
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

/* Per-pixel last-polarity state.  Stored as uint8 values:
     STATE_UNSEEN -- pixel has not fired yet this session (no transition possible)
     STATE_OFF    -- last event at this pixel was an OFF (pol=0) event
     STATE_ON     -- last event at this pixel was an ON  (pol=1) event
   Index = (y << 7) | x; stride 128 = 1<<7 is a power of two (a shift, not a
   multiply). */
#define STATE_UNSEEN 0
#define STATE_OFF    1
#define STATE_ON     2

/* 16384 bytes = the full span of (y<<7)|x for 7-bit masked x and y (max index
   (127<<7)|127 = 16383), so even a glitched event word with y>111 or x>125
   can never index past the array -- mask-safe by construction, no clamp
   needed.  The live sensor only uses 126*112 = 14112 of these slots.  Zeroed
   by crt0.S, which matches STATE_UNSEEN=0. */
static uint8_t state[16384];

/* Window length: a new verdict is latched every WINDOW_BATCHES batches
   (= WINDOW_BATCHES * BATCH events). */
#ifndef WINDOW_BATCHES
#define WINDOW_BATCHES 256      /* window = 256 batches = 1024 events */
#endif

/* Verdict dead-band: |D| must exceed MARGIN to declare FORWARD or BACKWARD.
   Default 16 exceeds identity-(4)'s bound for up to 16 hot pixels -- any
   scene with <= 16 chattering hot pixels that produce no real asymmetry will
   read UNDECIDED. */
#ifndef MARGIN
#define MARGIN 16
#endif

/* Saturating ceiling for the 10-bit window counters (fits bits[9:0]). */
#define COUNT_CAP 1023u

/* Live window counters (cleared on each new window). */
static uint32_t c_fwd;          /* ON->OFF transitions in current window   */
static uint32_t c_rev;          /* OFF->ON transitions in current window   */

/* Latched counts from the LAST COMPLETED window (emitted each batch).
   Both 0 before the first window completes. */
static uint32_t lat_fwd;
static uint32_t lat_rev;

/* Batch-within-window counter (0..WINDOW_BATCHES-1). */
static uint32_t batch_in_window;

/* 4-bit window sequence counter (0..15, wraps). */
static uint32_t wseq;

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to
   the instruction after it -- the next interrupt jumps straight to
   event_id_0's vector. A wfi() call inside this function would permanently
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

    /* Process each event in order, updating per-pixel state and window
       transition counters. */
    for (uint32_t i = 0; i < BATCH; i++) {
        uint32_t x   = (v[i] >> X_SHIFT) & 0x7Fu;
        uint32_t y   = (v[i] >> Y_SHIFT) & 0x7Fu;
        uint32_t pol =  v[i]             & 0x1u;
        /* ts = (v[i] >> 1) & 0xFFFF; -- decoded per ABI but unused (event-order
           timebase; see the TIMEBASE note in the header comment). */

        uint32_t idx = (y << 7) | x;   /* stride 128 = 1<<7, no multiply */
        uint8_t  s   = state[idx];

        if (s == STATE_ON && pol == 0u) {
            /* ON->OFF: brightness decayed -- forward-time transition. */
            if (c_fwd < COUNT_CAP) c_fwd++;
        } else if (s == STATE_OFF && pol == 1u) {
            /* OFF->ON: brightness kindled -- reverse-time transition. */
            if (c_rev < COUNT_CAP) c_rev++;
        }
        /* STATE_UNSEEN: first event at this pixel -- no transition to count. */

        state[idx] = (uint8_t)(pol ? STATE_ON : STATE_OFF);
    }

    /* Advance the batch-within-window counter and latch on window boundary.
       NOTE: the latch and wseq increment happen BEFORE the emit below, so the
       word written for the closing batch of window W already carries window W's
       latched counts and the incremented wseq. Word index i (0-based) always
       carries wseq == ((i + 1) / WINDOW_BATCHES) & 0xF. */
    batch_in_window++;
    if (batch_in_window >= (uint32_t)WINDOW_BATCHES) {
        batch_in_window = 0u;
        lat_fwd = c_fwd;
        lat_rev = c_rev;
        c_fwd   = 0u;
        c_rev   = 0u;
        wseq    = (wseq + 1u) & 0xFu;
    }

    /* Compute verdict from the LATCHED counts (stable across the whole window). */
    int32_t  D       = (int32_t)lat_fwd - (int32_t)lat_rev;
    uint32_t verdict = (D >= (int32_t)MARGIN)  ? 1u   /* FORWARD: decay-dominated */
                     : (D <= -(int32_t)MARGIN) ? 2u   /* BACKWARD: kindle-dominated */
                     : 0u;                             /* UNDECIDED: within dead-band */

    /* Emit ONE word per batch to FIFO_OUT.
       Layout: bits[9:0]=fwd, bits[19:10]=rev, bits[21:20]=verdict, bits[25:22]=wseq */
    *FIFO_OUT = (wseq    << 22)
              | (verdict << 20)
              | (lat_rev << 10)
              |  lat_fwd;
}

void main(void) {
    /* .bss is already zeroed by crt0.S, so state/c_fwd/c_rev/lat_fwd/lat_rev/
       batch_in_window/wseq all start cold (STATE_UNSEEN=0, counters=0). */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
