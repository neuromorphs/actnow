# Top-level orchestrator.
# Usage: make PROG=application     (builds bootloader + <PROG>, stitches, -> build/rom.mem)
#        make TEST=addi test      (builds a single riscv-test        -> build/rom.mem)
#        make clean
#
# Each program lives in its own folder with its own Makefile producing
# <folder>/build/<folder>.bin. This stitches:
#     [ bootloader.bin ][ 4-byte LE length ][ application.bin ]
# then converts to the simulator's text image (build/rom.mem).

PROG ?= application

BUILD    := build
BOOT_BIN := bootloader/build/bootloader.bin
APP_BIN  := $(PROG)/build/$(PROG).bin
ROM_BIN  := $(BUILD)/rom.bin
ROM_MEM  := $(BUILD)/rom.mem
BIN2MEM  := common/bin2mem.sh

.PHONY: all clean $(BOOT_BIN) $(APP_BIN)
all: $(ROM_MEM)

# Delegate to each program's own Makefile (always, so edits are picked up).
$(BOOT_BIN):
	$(MAKE) -C bootloader

$(APP_BIN):
	$(MAKE) -C $(PROG)

$(ROM_BIN): $(BOOT_BIN) $(APP_BIN) | $(BUILD)
	cat $(BOOT_BIN) > $@
	# 4-byte little-endian length header for the application blob.
	len=$$(wc -c < $(APP_BIN)); \
	printf '%b' \
	  "$$(printf '\\%03o' $$((len & 255)) $$((len >> 8 & 255)) \
	                      $$((len >> 16 & 255)) $$((len >> 24 & 255)))" >> $@
	cat $(APP_BIN) >> $@

$(ROM_MEM): $(ROM_BIN)
	sh $(BIN2MEM) $< > $@
	@echo "ROM image: $$(wc -c < $(ROM_BIN)) bytes -> $$(wc -l < $@) words ($@)"

$(BUILD):
	mkdir -p $(BUILD)

.PHONY: test
test: | $(BUILD)
	$(MAKE) -C tests TEST=$(TEST)
	cp tests/build/rom.mem $(ROM_MEM)

clean:
	$(MAKE) -C bootloader clean
	$(MAKE) -C $(PROG) clean
	$(MAKE) -C tests clean
	rm -rf $(BUILD)
