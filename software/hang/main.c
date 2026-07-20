#include <stdint.h>

/* Deliberately erroneous program: an infinite loop with no side effects,
   modeling hung firmware that never services any peripheral. Used to
   exercise core/soc.act's reset_ext recovery path against a real compiled
   binary running through the real bootloader. */

void main(void) {
    for (;;) {
    }
}
