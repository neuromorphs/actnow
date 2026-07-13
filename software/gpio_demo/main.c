#include <stdint.h>

/* Exercises GPIO end to end, both directions in one program:
     - Input: an external device pulses one of core.act's two GPIO input
       pins (gpio_in_0/gpio_in_1, wired to event_id_14/event_id_15) -- an
       interrupt-controller event like any other, dispatched through a
       distinct ISR per line.
     - Output: each ISR writes a distinct 4-bit pattern to the GPIO output
       register (base=7), which the testbench observes land on
       gpio_out_0..gpio_out_3. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR_14 ADDR(1, 14 * 4)
#define INT_CTRL_VECTOR_15 ADDR(1, 15 * 4)
#define INT_CTRL_ENABLE    ADDR(1, 64)
#define GPIO               ADDR(7, 0)

static inline void wfi(void) {
    asm volatile (".word 0x0000000b");
}

/* gpio_in_0 (event_id_14) -> drive 0b0101 */
static __attribute__((noinline)) void isr_gpio_in_0(void) {
    *GPIO = 0b0101;
    wfi();
}

/* gpio_in_1 (event_id_15) -> drive 0b1010 */
static __attribute__((noinline)) void isr_gpio_in_1(void) {
    *GPIO = 0b1010;
    wfi();
}

void main(void) {
    *INT_CTRL_VECTOR_14 = (uint32_t)&isr_gpio_in_0;
    *INT_CTRL_VECTOR_15 = (uint32_t)&isr_gpio_in_1;
    *INT_CTRL_ENABLE = (1u << 14) | (1u << 15);  /* enable both, once vectors are ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
