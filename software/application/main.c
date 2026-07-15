#include <stdint.h>

/* Interrupt-driven application, running from SRAM after the bootloader
   copies it there. main() registers isr_handler as event_id_0's ISR, sets
   fifo_in's trigger level to BATCH, enables event_id_0, then returns --
   crt0.S executes WFI right after, putting the core to sleep.

   fifo_in fires event_id_0 itself once BATCH values have been pushed: no
   separate trigger needed, and until the enable write above, the
   interrupt controller won't even accept event_id_0, so a producer
   pushing early just blocks until this program is ready.

   Each firing jumps straight to isr_handler, which reads BATCH event words
   from the input FIFO, rotates each event's (x,y) coordinate 45 degrees
   around the sensor center, and writes the rotated event words to the output
   FIFO. A write to a full output FIFO blocks (real backpressure) instead
   of crashing.

   isr_handler must NOT call wfi() itself: soc.act's WFI-decode never
   returns control to the instruction after it -- the next interrupt jumps
   straight to event_id_0's vector instead. A wfi() call inside this
   function would permanently skip its own epilogue (the stack pointer's
   restore), leaking 16 bytes of stack every single interrupt until it
   eventually collides with this program's own code. Just returning here
   is correct and sufficient: this function's own `ret` lands on the same
   cached wfi() site main()'s return already relies on (see main()'s
   comment below), only now with the epilogue having actually run first. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame. */
#define SX 126
#define SY 112
#define CX (SX / 2)
#define CY (SY / 2)

/* Requirements event ABI:
   bit 31 pad, bits 30:24 x, bits 23:17 y, bits 16:1 timestep, bit 0 polarity. */
#define X_SHIFT 24
#define Y_SHIFT 17
#define XY_MASK  (0x7Fu << Y_SHIFT | 0x7Fu << X_SHIFT)

static int32_t clampi(int32_t v, int32_t lo, int32_t hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static uint32_t rotate45(uint32_t word) {
    int32_t x = (int32_t)((word >> X_SHIFT) & 0x7Fu);
    int32_t y = (int32_t)((word >> Y_SHIFT) & 0x7Fu);

    int32_t tx = x - CX;
    int32_t ty = y - CY;
    int32_t rx = (tx - ty) >> 1;
    int32_t ry = (tx + ty) >> 1;

    uint32_t nx = (uint32_t)clampi(rx + CX, 0, SX - 1);
    uint32_t ny = (uint32_t)clampi(ry + CY, 0, SY - 1);

    return (word & ~XY_MASK) | (nx << X_SHIFT) | (ny << Y_SHIFT);
}

static __attribute__((noinline)) void isr_handler(void) {
    uint32_t v[BATCH];
    for (uint32_t i = 0; i < BATCH; i++) {
        v[i] = *FIFO_IN;
    }
    for (uint32_t i = 0; i < BATCH; i++) {
        *FIFO_OUT = rotate45(v[i]);
    }
}

void main(void) {
    *INT_CTRL_VECTOR0 = (uint32_t)&isr_handler;
    *FIFO_IN = BATCH;        /* configure fifo_in's trigger level */
    *INT_CTRL_ENABLE = 0x1;  /* enable event_id_0 -- last, once everything above is ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
