#include <stdint.h>

/* chips/fpga variant of software/application/main.c: rotates each AER
   pixel-event's (x,y) coordinate 45 degrees around the sensor's center.
   Same interrupt/FIFO wiring -- fifo_in fires event_id_0 once BATCH words
   land, isr_handler reads BATCH words and writes BATCH results out.

   Each word is one packed AER event: evt_pack.v's {ts[16:0], pol, y[6:0],
   x[6:0]}. This core is plain RV32I (no multiply/divide), so the rotation
   uses the multiply-free 45-degree identity:

       tx = x - cx, ty = y - cy
       rx = (tx - ty) >> 1, ry = (tx + ty) >> 1
       x' = rx + cx,        y' = ry + cy

   which points in the true 45-degree direction, scaled by ~0.707, using
   only shifts and adds. ts/pol pass through unchanged; x'/y' are clamped
   to the sensor frame so a corner event can't rotate off the edge into
   another event's address. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR0 ADDR(1, 0)
#define INT_CTRL_ENABLE  ADDR(1, 64)
#define FIFO_IN          ADDR(5, 0)
#define FIFO_OUT         ADDR(6, 0)

#define BATCH 4

/* Sensor frame (matches chips/fpga/dvs_replay.py's SX, SY). */
#define SX 126
#define SY 112
#define CX (SX / 2)
#define CY (SY / 2)

/* evt_pack.v's bit layout: bits[31:14] = ts(17)+pol(1), unchanged by the
   rotation; bits[13:7] = y, bits[6:0] = x. */
#define HIGH_BITS_MASK 0xFFFFC000u

static int32_t clampi(int32_t v, int32_t lo, int32_t hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static uint32_t rotate45(uint32_t word) {
    int32_t x = (int32_t)(word & 0x7F);
    int32_t y = (int32_t)((word >> 7) & 0x7F);

    int32_t tx = x - CX;
    int32_t ty = y - CY;
    int32_t rx = (tx - ty) >> 1;
    int32_t ry = (tx + ty) >> 1;

    int32_t nx = clampi(rx + CX, 0, SX - 1);
    int32_t ny = clampi(ry + CY, 0, SY - 1);

    return (word & HIGH_BITS_MASK) | ((uint32_t)ny << 7) | (uint32_t)nx;
}

/* isr_handler must not call wfi() -- see software/application/main.c's
   isr_handler comment for why. */
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
