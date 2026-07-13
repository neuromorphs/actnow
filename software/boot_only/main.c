/* Minimal program: boots, does nothing, and goes straight to WFI -- zero
   peripheral interaction, no interrupt configured. Proves the plain boot ->
   WFI path in isolation, so a regression there can't get lost inside a
   more complex test's own failure. */

void main(void) {
    /* crt0.S executes wfi() for us when main() returns. */
}
