#include <stdint.h>

/* Deliberately erroneous program: a genuine infinite loop with no side
   effects, modeling a hung/bugged firmware image that never services any
   peripheral. Used to exercise core/soc.act's reset_ext recovery path --
   the fetch_answerer-style testbenches elsewhere prove reset can recover a
   hand-assembled hang; this proves it against a real compiled binary
   running through the real bootloader. */

void main(void) {
    for (;;) {
    }
}
