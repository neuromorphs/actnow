#include <stdint.h>

/* Interrupt-driven application, running from SRAM after the bootloader
   copies it there. main() registers isr_handler as event_id_0's ISR, sets
   fifo_in's trigger level to BATCH, enables event_id_0, then returns --
   crt0.S executes WFI right after, putting the core to sleep.

   fifo_in fires event_id_0 itself once BATCH values have been pushed: no
   separate trigger needed, and until the enable write above, the
   interrupt controller won't even accept event_id_0, so a producer
   pushing early just blocks until this program is ready.

   Each firing jumps straight to isr_handler, which reads BATCH words from
   the input FIFO, adds 1 to each, and writes the results to the output
   FIFO. A write to a full output FIFO blocks (real backpressure) instead
   of crashing. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 3

static inline void wfi(void) {
    asm volatile (".word 0x0000000b");
}

static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }
    for (uint32_t i = 0; i < BATCH; i++) {
        *FIFO_OUT = v[i] + 1;
    }
    
    wfi();
}

void main(void) {
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
