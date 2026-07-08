# Build a single riscv-test into build/rom.mem (standalone, no bootloader).
# Usage: make TEST=addi
#
# Assembles ../common/test_start.S + <TEST>.S (cpp #includes resolve from this
# directory), links with ../common/test.lds, and converts the result to the
# simulator's memory image with ../common/bin2mem.sh.

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
LDSCRIPT := $(COMMON)/test.lds
BIN2MEM  := $(COMMON)/bin2mem.sh

CFLAGS := -march=rv32i -mabi=ilp32 -nostdlib -Wl,--build-id=none

BUILD      := build
ELF        := $(BUILD)/$(TEST).elf
BIN        := $(BUILD)/$(TEST).bin
LST        := $(BUILD)/$(TEST).lst
MEM        := $(BUILD)/rom.mem
MEM_ACTSIM := $(BUILD)/rom.actsim.mem

.PHONY: all clean
all: $(MEM) $(MEM_ACTSIM) $(LST)

$(ELF): $(START) $(TEST).S $(LDSCRIPT) | $(BUILD)
	$(CC) $(CFLAGS) -T $(LDSCRIPT) -o $@ $(START) $(TEST).S

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

$(MEM): $(BIN)
	sh $(BIN2MEM) $< > $@
	@echo "TEST $(TEST): $$(wc -c < $(BIN)) bytes -> $$(wc -l < $@) words ($@)"

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
