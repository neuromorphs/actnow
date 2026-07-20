#include <stdint.h>

/* Exercises GPIO end to end, both directions:
     - Input: an external device pulses one of chips/dvs/core.act's two
       GPIO input pins (gpio_in_0/gpio_in_1, wired to event_id_1/
       event_id_2), dispatched through a distinct ISR per line.
     - Output: each ISR writes a distinct 4-bit pattern to the GPIO output
       register (base=7), observed by the testbench on
       gpio_out_0..gpio_out_3. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR_1 ADDR(1, 1 * 4)
#define INT_CTRL_VECTOR_2 ADDR(1, 2 * 4)
#define INT_CTRL_ENABLE   ADDR(1, 64)
#define GPIO              ADDR(7, 0)

/* Each ISR must not call wfi() -- see software/application/main.c's
   isr_handler comment for why. */

/* gpio_in_0 (event_id_1) -> drive 0b0101 */
static __attribute__((noinline)) void isr_gpio_in_0(void) {
    *GPIO = 0b0101;
}

/* gpio_in_1 (event_id_2) -> drive 0b1010 */
static __attribute__((noinline)) void isr_gpio_in_1(void) {
    *GPIO = 0b1010;
}

void main(void) {
    *INT_CTRL_VECTOR_1 = (uint32_t)&isr_gpio_in_0;
    *INT_CTRL_VECTOR_2 = (uint32_t)&isr_gpio_in_1;
    *INT_CTRL_ENABLE = (1u << 1) | (1u << 2);  /* enable both, once vectors are ready */
    /* crt0.S executes wfi() for us when main() returns. */
}
