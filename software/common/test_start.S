# Minimal reset glue for the riscv-tests suite (picorv32 harness).
# The tests define `mytest` (not _start) and jump to `mytest_ret` on pass;
# this file supplies the reset entry, data/bss init, and the ret handler.
#
# Pass/fail signalling for our simulator (no console MMIO, no prints):
#   pass -> custom instruction, opcode 0b0001011 (our "wfi")
#   fail -> ebreak  (emitted by the test's RVTEST_FAIL)

    .section .text.init, "ax", @progbits
    .global _start
_start:
    # Global pointer for gp-relative (relaxed) accesses.
.option push
.option norelax
    la      gp, __global_pointer$
.option pop

    la      sp, _stack_top

    # Copy .data from ROM (LMA) to SRAM (VMA).
    la      a0, _sdata
    la      a1, _edata
    la      a2, _sidata
1:
    bgeu    a0, a1, 2f
    lw      t0, 0(a2)
    sw      t0, 0(a0)
    addi    a0, a0, 4
    addi    a2, a2, 4
    j       1b
2:

    # Zero .bss.
    la      a0, _sbss
    la      a1, _ebss
3:
    bgeu    a0, a1, 4f
    sw      zero, 0(a0)
    addi    a0, a0, 4
    j       3b
4:

    call    mytest

    # Test passed (RVTEST_PASS jumps here). Signal success and halt.
    .global mytest_ret
mytest_ret:
    .word   0x0000000b          # opcode 0b0001011, our "wfi" = test OK
5:
    j       5b
