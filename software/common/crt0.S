# Minimal rv32i startup, shared by bootloader and application.
# Sets the stack pointer, initialises .data, clears .bss, calls main().

    .section .text.init, "ax", @progbits
    .global _start
_start:
    # Initialise the global pointer for gp-relative (relaxed) accesses.
    # norelax so the linker doesn't collapse this load into a no-op before
    # gp is established.
.option push
.option norelax
    la      gp, __global_pointer$
.option pop

    la      sp, _stack_top

    # Copy .data from its load address (_sidata) to its run address.
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

    call    main
5:
    j       5b
