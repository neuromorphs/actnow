#include <stdint.h>

/* Generality/stress test for the interrupt controller: registers a
   DISTINCT ISR for every one of the 16 maskable event lines (event_id_0
   through event_id_15), each applying a different, easy-to-hand-verify
   transform. The point is to demonstrate that vector configuration, the
   enable mask, and dispatch genuinely work across the *full* width of the
   controller (see interrupt.act) -- not just the one or two lines the
   other demo programs exercise.

   ISR N reads one word from the input FIFO and writes back v + (N + 1) --
   e.g. ISR 0 adds 1, ISR 7 adds 8, ISR 15 adds 16. Sixteen genuinely
   different compiled functions, sixteen genuinely different vector-table
   entries, one shared FIFO pair.

   Same address layout as software/application/main.c: base=1 is the
   interrupt controller (vectors[N] at offset 4*N, enable mask at offset
   ADDR_INT_CTRL_ENABLE=64), base=5/6 are the input/output FIFOs. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_ENABLE ADDR(1, 64)
#define FIFO_IN         ADDR(5, 0)
#define FIFO_OUT        ADDR(6, 0)
#define N_EVENTS        16

static inline void wfi(void) {
    asm volatile (".word 0x0000000b");
}

#define MAKE_ISR(N)                                            \
    static __attribute__((noinline)) void isr_##N(void) {      \
        uint32_t v = *FIFO_IN;                                 \
        *FIFO_OUT = v + (N + 1);                                \
        wfi();                                                 \
    }

MAKE_ISR(0)
MAKE_ISR(1)
MAKE_ISR(2)
MAKE_ISR(3)
MAKE_ISR(4)
MAKE_ISR(5)
MAKE_ISR(6)
MAKE_ISR(7)
MAKE_ISR(8)
MAKE_ISR(9)
MAKE_ISR(10)
MAKE_ISR(11)
MAKE_ISR(12)
MAKE_ISR(13)
MAKE_ISR(14)
MAKE_ISR(15)

static void (*const isr_table[N_EVENTS])(void) = {
    isr_0, isr_1, isr_2,  isr_3,  isr_4,  isr_5,  isr_6,  isr_7,
    isr_8, isr_9, isr_10, isr_11, isr_12, isr_13, isr_14, isr_15,
};

void main(void) {
    /* fifo_in gates push acceptance on its trigger level having been
       configured at least once -- this test fires every event manually
       (see the testbench), so its auto-fire feature (event_out) is never
       used and, deliberately, never wired to anything by the testbench.
       That makes the *value* here matter a lot, despite not being used
       for triggering: if count ever reached this level, fifo_in would try
       to fire event_out, and since nothing is connected to receive it,
       that send blocks forever -- silently deadlocking fifo_in entirely
       (stuck mid-push, unable to service any further transaction, CPU or
       testbench). A small value like 1 would hit this on the very first
       push. Setting it beyond DEPTH (this test's fifo_in<4>) makes it
       unreachable by construction, so event_out is never attempted. */
    *FIFO_IN = 0xFFFFFFFF;

    for (uint32_t i = 0; i < N_EVENTS; i++)
        *ADDR(1, i * 4) = (uint32_t)isr_table[i];

    *INT_CTRL_ENABLE = 0xFFFF;  /* enable all 16 event lines */
    /* crt0.S executes wfi() for us when main() returns. */
}
