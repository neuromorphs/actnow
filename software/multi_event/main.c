#include <stdint.h>

/* Stress test for the interrupt controller: registers a distinct ISR for
   every one of the 16 maskable event lines, to prove vector configuration,
   the enable mask, and dispatch work across the full width of the
   controller -- not just the one or two lines the other demo programs
   exercise.

   ISR N reads one word from the input FIFO and writes back v + (N + 1),
   e.g. ISR 0 adds 1, ISR 15 adds 16 -- sixteen genuinely different
   functions and vector-table entries sharing one FIFO pair. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_ENABLE ADDR(1, 64)
#define FIFO_IN         ADDR(5, 0)
#define FIFO_OUT        ADDR(6, 0)
#define N_EVENTS        16

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to
   the instruction after it, so a wfi() call inside an ISR permanently skips
   that ISR's own epilogue (the stack pointer's restore), leaking 16 bytes of
   stack every interrupt until it eventually collides with this program's own
   code (see software/application/main.c's isr_handler comment for the full
   explanation). Just returning is correct: each ISR's own `ret` lands on the
   same cached wfi() site main()'s return already relies on. */
#define MAKE_ISR(N)                                            \
    static __attribute__((noinline)) void isr_##N(void) {      \
        uint32_t v = *FIFO_IN;                                 \
        *FIFO_OUT = v + (N + 1);                                \
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
    /* This test fires every event manually, so fifo_in's auto-fire
       (event_out) is deliberately left unwired by the testbench. If count
       ever reached trigger_level, fifo_in would try to fire event_out and
       block forever with nothing to receive it -- so set it unreachable
       (beyond DEPTH) to make sure that never happens. */
    *FIFO_IN = 0xFFFFFFFF;

    for (uint32_t i = 0; i < N_EVENTS; i++)
        *ADDR(1, i * 4) = (uint32_t)isr_table[i];

    *INT_CTRL_ENABLE = 0xFFFF;  /* enable all 16 event lines */
    /* crt0.S executes wfi() for us when main() returns. */
}
