OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY
{
    SRAM (rwx) : ORIGIN = 0x00000, LENGTH = 0x8000
    ROM  (rx)  : ORIGIN = 0x40000, LENGTH = 0x10000
}

/* Bootloader runs XIP from ROM; its stack lives at the top of SRAM. */
_stack_top = ORIGIN(SRAM) + LENGTH(SRAM);

SECTIONS
{
    /* Reset vector (0x40000) must land on _start, so keep .text.init first. */
    .text :
    {
        KEEP(*(.text.init))
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > ROM

    /* Writable data runs from SRAM but is loaded from ROM. */
    .data :
    {
        . = ALIGN(4);
        _sdata = .;
        *(.data*)
        . = ALIGN(4);
        /* __global_pointer$ sits 0x800 into the small-data region so a
           signed 12-bit gp-relative offset reaches ~2 KiB of .sdata/.sbss. */
        __global_pointer$ = . + 0x800;
        *(.sdata*)
        . = ALIGN(4);
        _edata = .;
    } > SRAM AT> ROM
    _sidata = LOADADDR(.data);

    .bss (NOLOAD) :
    {
        . = ALIGN(4);
        _sbss = .;
        *(.sbss*)
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        _ebss = .;
    } > SRAM

    /* First ROM byte after the bootloader image: this is where the
       application blob (4-byte length header + application binary) is
       concatenated by the Makefile. */
    _app_load_start = LOADADDR(.data) + SIZEOF(.data);

    /DISCARD/ : { *(.comment) *(.eh_frame) *(.riscv.attributes) }
}
