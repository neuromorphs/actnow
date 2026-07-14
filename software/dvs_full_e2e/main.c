#include <stdint.h>

/* Full pipeline e2e test, combining every dvs mechanism in one program:
     1. Boot genuinely XIP out of spi_boot (this program itself, and the
        bootloader that loads it, both run straight out of spi_boot --
        nothing special to do here, it's just how the chip boots).
     2. Load a data blob into memory via spi_prog's *read* direction --
        NOT XIP: each word is its own explicit SPI transaction, triggered
        one at a time by reading spi_prog's data register, unlike
        spi_boot's continuous fetch-as-you-go.
     3. Configure the AER input's trigger level and register an ISR, same
        as software/dvs_application/main.c.
     4. On the AER interrupt, read BATCH pixel-events and write each one
        back out over SPI via spi_prog's *write* direction -- combined
        with the value loaded in step 2, so a wrong load would show up as
        a wrong output, not just a silently-ignored one. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define AER_IN           ADDR(5, 0)
#define SPI_PROG_ADDR    ADDR(6, 0)
#define SPI_PROG_DATA    ADDR(6, 4)

#define BATCH       3
#define LOAD_WORDS  3
#define LOAD_BASE   0x100  /* SPI address range the load reads from -- distinct from the output writes' 0/4/8 */

static uint32_t loaded[LOAD_WORDS];

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
        *SPI_PROG_DATA = v[i] + loaded[i];
    }

    wfi();
}

void main(void) {
    /* step 2: load LOAD_WORDS words in via spi_prog, one explicit SPI
       transaction per word */
    for (uint32_t i = 0; i < LOAD_WORDS; i++) {
        *SPI_PROG_ADDR = LOAD_BASE + i * 4;
        loaded[i] = *SPI_PROG_DATA;
    }

    /* step 3: configure and enable the AER interrupt */
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *AER_IN = BATCH;
    *INT_CTRL_ENABLE = 0x1;
    /* crt0.S executes wfi() for us when main() returns. */
}
