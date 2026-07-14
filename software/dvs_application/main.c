#include <stdint.h>

/* dvs equivalent of software/application/main.c: interrupt-driven, running
   from SRAM after the bootloader copies it there. main() registers
   isr_handler as event_id_0's ISR (already hardwired to the AER input's
   own event_out in chips/dvs/core.act -- no separate wiring needed, unlike
   chips/bench where the testbench wires fifo_event itself), sets the AER
   input's trigger level to BATCH, enables event_id_0, then returns --
   crt0.S executes WFI right after, putting the core to sleep.

   The AER input (core/peripherals/fifo_in.act instantiated with a 20-bit
   element width) fires event_id_0 itself once BATCH pixel-events have
   landed, exactly like chips/bench's fifo_in. Each firing jumps straight
   to isr_handler, which reads BATCH values from the AER input, adds 1 to
   each, and pushes the results out over SPI via spi_prog's two-register
   interface (core/peripherals/spi_prog.act): stage an address, then
   write the data register to trigger the actual SPI transaction. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define AER_IN           ADDR(5, 0)
#define SPI_PROG_ADDR    ADDR(6, 0)
#define SPI_PROG_DATA    ADDR(6, 4)

#define BATCH 3

static inline void wfi(void) {
    asm volatile (".word 0x0000000b");
}

static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *AER_IN;
    }
    for (uint32_t i = 0; i < BATCH; i++) {
        *SPI_PROG_ADDR = i * 4;
        *SPI_PROG_DATA = v[i] + 1;
    }

    wfi();
}

void main(void) {
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *AER_IN = BATCH;         /* configure the AER input's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
