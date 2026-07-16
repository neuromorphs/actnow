#include <stdint.h>

/* Interrupt-driven AER application: runs from SRAM after the bootloader
   copies it there. main() registers isr_handler on event_id_0 (already
   wired to the AER input's own event_out in chips/dvs/core.act), sets the
   AER input's trigger level to BATCH, then enables event_id_0 and returns
   -- crt0.S puts the core to sleep with WFI until the AER input fires the
   interrupt.

   isr_handler reads BATCH values from the AER input, adds 1 to each, and
   pushes the results out over SPI via spi_prog's two-register interface:
   stage an address, then write the data register to trigger the actual
   SPI transaction. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define AER_IN           ADDR(5, 0)
#define SPI_PROG_ADDR    ADDR(6, 0)
#define SPI_PROG_DATA    ADDR(6, 4)

#define BATCH 3

/* isr_handler must not call wfi() -- see software/application/main.c's
   isr_handler comment for why. */
static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *AER_IN;
    }
    for (uint32_t i = 0; i < BATCH; i++) {
        *SPI_PROG_ADDR = i * 4;
        *SPI_PROG_DATA = v[i] + 1;
    }
}

void main(void) {
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *AER_IN = BATCH;         /* configure the AER input's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
