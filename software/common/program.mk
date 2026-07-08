# Shared rv32i build rules for a single program.
# A program's Makefile sets PROG (and optionally LDSCRIPT), then includes this.
# Output: build/$(PROG).bin  (raw little-endian binary, same format for every program)

CROSS   ?= riscv32-unknown-elf-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump

# Directory this include lives in (e.g. ../common/).
COMMON_DIR := $(dir $(lastword $(MAKEFILE_LIST)))

LDSCRIPT ?= $(COMMON_DIR)application.lds
CRT0     := $(COMMON_DIR)crt0.S

ARCHFLAGS := -march=rv32i -mabi=ilp32
CFLAGS    := $(ARCHFLAGS) -O3 -g -ffreestanding -nostdlib -fno-builtin \
             -Wall -Wextra -ffunction-sections -fdata-sections
LDFLAGS   := $(ARCHFLAGS) -nostdlib -Wl,--gc-sections -Wl,--build-id=none

SRCS  := $(CRT0) $(wildcard *.c) $(wildcard *.S)
BUILD := build
ELF   := $(BUILD)/$(PROG).elf
BIN   := $(BUILD)/$(PROG).bin
LST   := $(BUILD)/$(PROG).lst

.PHONY: all clean
all: $(BIN) $(LST)

$(ELF): $(SRCS) $(LDSCRIPT) | $(BUILD)
	$(CC) $(CFLAGS) $(LDFLAGS) -T $(LDSCRIPT) -o $@ $(SRCS)

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

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -rf $(BUILD)
