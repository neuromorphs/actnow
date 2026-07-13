/* Minimal program: boots (bootloader copies it into SRAM, jumps there),
   does nothing, and goes straight to WFI -- zero peripheral interaction, no
   interrupt configured. Proves the plain boot -> WFI path works in
   isolation (robustness.md Stage 1.7 scenario 1). Every other e2e test in
   this directory implicitly relies on this same path succeeding as a
   precondition of its own setup; this is the one dedicated to proving it by
   itself, so a regression here can't get lost inside a more complex
   scenario's own failure. */

void main(void) {
    /* crt0.S executes wfi() for us when main() returns. */
}
