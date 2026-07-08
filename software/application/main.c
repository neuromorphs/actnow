#include <stdint.h>

/* Minimal example application. Runs from SRAM at 0x00000 after the
   bootloader copies it there. Replace with real work. */
void main(void) {
  volatile uint32_t counter = 0;
  for (;;)
    counter++;
}
