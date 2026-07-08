OUTPUT_ARCH(riscv)
ENTRY(_start)

/* riscv-tests layout for our chip: test code runs XIP from ROM (reset at
   0x40000), writable test data lives in SRAM with its load image in ROM and
   is copied there by common/test_start.S. Mirrors bootloader.lds without the
   appended-application blob. */
MEMORY
{
    SRAM (rwx) : ORIGIN = 0x00000, LENGTH = 0x8000
    ROM  (rx)  : ORIGIN = 0x40000, LENGTH = 0x10000
}

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

    /DISCARD/ : { *(.comment) *(.eh_frame) *(.riscv.attributes) }
}
