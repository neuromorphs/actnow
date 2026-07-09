#include <stdint.h>

/* Minimal example application. Runs from SRAM at 0x00000 after the bootloader
   copies it there, then returns -- crt0.S signals completion (WFI) on return.
   Replace with real work. */
void main(void) {
  volatile uint32_t sum = 0;
  for (uint32_t i = 0; i < 100; i++)
    sum += i;
}
