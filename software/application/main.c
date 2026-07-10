#include <stdint.h>

/* Real-world-style interrupt-driven application, running from SRAM after
   the bootloader copies it there (see common/crt0.S / common/application.lds).

   Address layout (addr_t base:offset, matching globals.act):
     base=1 -> interrupt controller (interrupt.act):
                 offset 4*N            -> vectors[N] (ISR address, word-addressed)
                 offset ADDR_INT_CTRL_ENABLE (64) -> enable mask, bit N gates event_id_N
     base=5 -> input FIFO  (fifo_in.act): CPU reads pop it; a CPU write instead
               configures its trigger level (see below)
     base=6 -> output FIFO (fifo_out.act), single data register
   (base=4, ROM, isn't referenced here -- this program only runs from it.)

   main() registers isr_handler as event_id_0's ISR, sets fifo_in's trigger
   level to BATCH, enables event_id_0, then returns; crt0.S executes the WFI
   opcode right after `call main` returns, putting the core to sleep.

   fifo_in fires event_id_0 *itself* once BATCH values have been pushed into
   it -- there's no separate "interrupt line" a producer has to remember to
   assert after filling the FIFO; filling it to the configured level is what
   raises the interrupt. And until the enable write above, interrupt.act
   doesn't even offer to receive on event_id_0 -- so whoever's pushing just
   blocks at fifo_in's own push rendezvous until this program is actually
   ready, no arbitrary "wait for boot" delay needed anywhere upstream.

   Each time event_id_0 fires, pc jumps straight to isr_handler (not through
   main() or crt0.S again) -- it reads exactly BATCH words from the input
   FIFO, adds 1 to each, and writes each result to the output FIFO. A write
   to fifo_out blocks (real backpressure) rather than crashing if it's ever
   full, so this is safe even if the consumer draining fifo_out falls behind.
   Since &isr_handler is resolved by the linker, this all works unmodified
   whether the image runs XIP from ROM or, as here, copied into SRAM by the
   bootloader -- nothing here hardcodes an address. */

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
