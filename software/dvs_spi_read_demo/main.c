#include <stdint.h>

/* Exercises spi_prog's *read* direction end to end -- every other dvs e2e
   test only ever drives spi_prog's write side (CPU pushes data out over
   SPI); nothing before this program ever had the CPU pull data in.

   At boot, stages an address via spi_prog's address register, then reads
   its data register -- that read is what triggers the actual SPI READ
   transaction (R/W=0 + staged address out, 32 bits back over miso).
   Compares the result against a known value and drives a self-checking
   pattern onto GPIO: 0b1111 if it matches, 0b0000 if it doesn't -- the
   testbench doesn't need to observe the full 32-bit value itself, just
   this one pass/fail bit pattern. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define SPI_PROG_ADDR ADDR(6, 0)
#define SPI_PROG_DATA ADDR(6, 4)
#define GPIO          ADDR(7, 0)

#define READ_ADDR     0x10
#define EXPECT_VALUE  0x1234ABCDu

static inline void wfi(void) {
    asm volatile (".word 0x0000000b");
}

void main(void) {
    *SPI_PROG_ADDR = READ_ADDR;
    uint32_t v = *SPI_PROG_DATA;

    *GPIO = (v == EXPECT_VALUE) ? 0b1111 : 0b0000;
    /* crt0.S executes wfi() for us when main() returns. */
}
