OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY
{
    SRAM (rwx) : ORIGIN = 0x00000, LENGTH = 0x8000
}

_stack_top = ORIGIN(SRAM) + LENGTH(SRAM);

SECTIONS
{
    /* The bootloader copies this image to 0x00000 and jumps there, so
       load address == run address for everything. */
    .text :
    {
        KEEP(*(.text.init))
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > SRAM

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
    } > SRAM
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
