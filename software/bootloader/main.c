#include <stdint.h>

/* Placed by the linker at the first ROM address after the bootloader image.
   Layout at this address: [uint32_t length][application bytes...]. */
extern uint32_t _app_load_start[];

#define SRAM_BASE ((uint32_t *)0x00000)

void main(void) {
  uint32_t len = _app_load_start[0];
  const uint32_t *src = &_app_load_start[1];
  uint32_t *dst = SRAM_BASE;

  uint32_t words = (len + 3u) >> 2;
  for (uint32_t i = 0; i < words; i++)
    dst[i] = src[i];

  ((void (*)(void))SRAM_BASE)();
}
