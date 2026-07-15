#include <stdint.h>

/* dvs equivalent of software/gpio_demo/main.c: exercises GPIO end to end,
   both directions in one program:
     - Input: an external device pulses one of chips/dvs/core.act's two
       GPIO input pins (gpio_in_0/gpio_in_1, wired to event_id_1/
       event_id_2 -- dvs's two spare event lines, unlike chips/bench's
       event_id_14/event_id_15) -- an interrupt-controller event like any
       other, dispatched through a distinct ISR per line.
     - Output: each ISR writes a distinct 4-bit pattern to the GPIO output
       register (base=7, unchanged from chips/bench), which the testbench
       observes land on gpio_out_0..gpio_out_3. */

#define ADDR(base, offset) ((volatile uint32_t *)(((uint32_t)(base) << 16) | (uint32_t)(offset)))

#define INT_CTRL_VECTOR_1 ADDR(1, 1 * 4)
#define INT_CTRL_VECTOR_2 ADDR(1, 2 * 4)
#define INT_CTRL_ENABLE   ADDR(1, 64)
#define GPIO              ADDR(7, 0)

/* Must NOT call wfi() itself: soc.act's WFI-decode never returns control to
   the instruction after it, so a wfi() call inside an ISR permanently skips
   that ISR's own epilogue (the stack pointer's restore), leaking 16 bytes of
   stack every interrupt until it eventually collides with this program's own
   code (see software/application/main.c's isr_handler comment for the full
   explanation). Just returning is correct: this function's own `ret` lands
   on the same cached wfi() site main()'s return already relies on. */

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
