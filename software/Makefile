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
# Pin PROG=bootloader: a command-line PROG=<app> propagates to sub-makes and
# would otherwise override the bootloader's own PROG, misnaming its output.
$(BOOT_BIN):
	$(MAKE) -C bootloader PROG=bootloader

$(APP_BIN):
	$(MAKE) -C $(PROG) PROG=$(PROG)

$(ROM_BIN): $(BOOT_BIN) $(APP_BIN) | $(BUILD)
	sh common/stitch.sh $(BOOT_BIN) $(APP_BIN) $@

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
