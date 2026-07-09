# Build a single test into build/rom.mem (standalone, no bootloader).
# Usage: make TEST=addi
#
# Tests live in two places: the official RISC-V suite under unit/ (assembly),
# and our own tests here in this directory (.S or .c). Assembles/compiles
# ../common/test_start.S + the test source, links with ../common/test.lds, and
# converts the result to the simulator's memory image with ../common/bin2mem.sh.
# cpp #includes resolve from the source's own directory, plus -Iunit so a custom
# test here can reuse the official framework headers (riscv_test.h etc.).

ifeq ($(filter clean,$(MAKECMDGOALS)),)
ifeq ($(TEST),)
$(error set TEST=<name>, e.g. make TEST=addi)
endif
endif

CROSS   ?= riscv64-unknown-elf-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump

COMMON   := ../common
START    := $(COMMON)/test_start.S
BIN2MEM  := $(COMMON)/bin2mem.sh

# BOOT (non-empty): build the test to run from internal SRAM after the bootloader
# copies it there (fast memory), instead of executing in place from external ROM.
# The only build differences are the link script (SRAM vs ROM) and, below,
# prepending the bootloader image; the reset glue (test_start.S) is unchanged.
ifeq ($(BOOT),)
LDSCRIPT := $(COMMON)/test.lds
VARIANT  :=
else
LDSCRIPT := $(COMMON)/application.lds
VARIANT  := .boot
endif

CFLAGS := -march=rv32i -mabi=ilp32 -nostdlib -ffreestanding -O2 -Iunit -Wl,--build-id=none

# A test's source is either <TEST>.S (assembly) or <TEST>.c (C), found either
# here (our own tests) or under unit/ (the official RISC-V suite). gcc assembles
# or compiles it and links it with the shared reset glue in the same call.
SRC := $(firstword $(wildcard $(TEST).S $(TEST).c unit/$(TEST).S unit/$(TEST).c))

BUILD      := build
# XIP and BOOT link at different addresses; tag their objects (.boot) so
# switching BOOT always relinks instead of silently reusing the other variant's
# ELF (a stale reuse runs code at the wrong address -> memory ops go astray).
ELF        := $(BUILD)/$(TEST)$(VARIANT).elf
BIN        := $(BUILD)/$(TEST)$(VARIANT).bin
LST        := $(BUILD)/$(TEST)$(VARIANT).lst
MEM        := $(BUILD)/rom.mem
MEM_ACTSIM := $(BUILD)/rom_image.mem

.PHONY: all clean
all: $(MEM) $(MEM_ACTSIM) $(LST)

$(ELF): $(START) $(SRC) $(LDSCRIPT) | $(BUILD)
	$(CC) $(CFLAGS) -T $(LDSCRIPT) -o $@ $(START) $(SRC)

$(BIN): $(ELF)
	$(OBJCOPY) -O binary $< $@

# Disassembly listings for debug:
#   .lst     - source interleaved (-S), easy to read
#   .raw.lst - section headers + plain disassembly (-hd), matches raw addresses
# Both append a hex dump of .rodata (tolerated if the section is absent).
$(LST): $(ELF)
	$(OBJDUMP) -S -d $< > $@
	$(OBJDUMP) -s -j .rodata $< >> $@ 2> /dev/null || true
	$(OBJDUMP) -hd $< > $(basename $@).raw.lst
	$(OBJDUMP) -s -j .rodata $< >> $(basename $@).raw.lst 2> /dev/null || true

BOOT_BIN := ../bootloader/build/bootloader.bin
ROM_BIN  := $(BUILD)/rom.bin

ifeq ($(BOOT),)
# XIP: the test binary is itself the ROM image.
$(MEM): $(BIN)
	sh $(BIN2MEM) $< > $@
	@echo "TEST $(TEST): $$(wc -c < $(BIN)) bytes -> $$(wc -l < $@) words ($@)"
else
# Bootloader: the ROM image is [ bootloader ][ len ][ test-app ]; the bootloader
# copies the test into SRAM and jumps there, so it runs from internal memory.
.PHONY: $(BOOT_BIN)
$(BOOT_BIN):
	$(MAKE) -C ../bootloader PROG=bootloader CROSS=$(CROSS)
$(ROM_BIN): $(BOOT_BIN) $(BIN) | $(BUILD)
	sh $(COMMON)/stitch.sh $(BOOT_BIN) $(BIN) $@
$(MEM): $(ROM_BIN)
	sh $(BIN2MEM) $< > $@
	@echo "TEST $(TEST) [boot]: $$(wc -c < $(ROM_BIN)) bytes -> $$(wc -l < $@) words ($@)"
endif

# actsim's sim::file_private::read parses each line as an integer and only
# recognizes base via a 0x/0b/0o prefix (else it assumes base 10) -- $(MEM)'s
# raw bitstring-per-line format (no prefix, kept as-is for Verilog
# $readmemb compatibility) isn't directly readable by it, so this just adds
# the "0b" prefix each line needs.
$(MEM_ACTSIM): $(MEM)
	sed 's/^/0b/' $< > $@

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -rf $(BUILD)
