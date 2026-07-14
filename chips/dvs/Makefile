# e2e test rules for chips/dvs (robustness.md Stage 2.5) -- delegated to
# from the top-level Makefile (`make -C chips/dvs <target>`), modeled
# directly on chips/bench/Makefile's own rules. Same rationale for the
# ROOT/aflat/actsim/cd dance -- see that file's header comment.
#
# Unlike chips/bench, there's no ROM to preload: chips/dvs boots by
# pulling the same ROM_IMAGE file over a real (simulated) SPI bus via
# chips/dvs/tests/e2e/spi_flash_stub.act, so the file-registry dance is
# identical, just consumed differently on the ACT side.

ROOT   := ../..
AFLAT  := aflat
ACTSIM := actsim

CROSS ?= $(firstword \
    $(foreach p,riscv32-unknown-elf- riscv64-unknown-elf- riscv-none-elf-, \
        $(if $(shell command -v $(p)gcc 2>/dev/null),$(p))) \
    riscv64-unknown-elf-)

BOOT     ?=
ROM_TEST ?= simple

ROM_IMAGE := software/tests/build/rom_image.mem

.PHONY: test e2e_boot_test e2e_aer_test e2e_reset_test e2e_gpio_test e2e_spi_read_test e2e_full_test

test: e2e_boot_test e2e_aer_test e2e_reset_test e2e_gpio_test e2e_spi_read_test e2e_full_test
	@echo "=== dvs: all e2e tests passed ==="

# Baseline scenario: boot software/boot_only/main.c (zero peripheral
# interaction) genuinely XIP out of spi_boot and confirm it reaches WFI
# cleanly. Same completion-detection rationale as chips/bench's own
# e2e_boot_test: no chp body of its own to emit a "test complete" line, so
# judged directly from soc's own "decoded wfi" log line instead.
e2e_boot_test:
	@echo "--- dvs e2e_boot_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=boot_only CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/dvs/tests/e2e/e2e_boot_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/dvs/tests/e2e/e2e_boot_test.act e2e_boot_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "dvs e2e_boot_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "dvs e2e_boot_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "decoded wfi"; then \
		echo "dvs e2e_boot_test: FAIL (no completion)"; exit 1; \
	else \
		echo "dvs e2e_boot_test: PASS"; \
	fi

# dvs equivalent of chips/bench's e2e_fifo_test: boots
# software/dvs_application/main.c, pushes two batches into the AER input,
# checks each batch's results as (address, data) pairs observed over SPI
# via spi_prog (chips/dvs/tests/e2e/e2e_aer_test.act's own spi_sink).
e2e_aer_test:
	@echo "--- dvs e2e_aer_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/dvs/tests/e2e/e2e_aer_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/dvs/tests/e2e/e2e_aer_test.act e2e_aer_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "dvs e2e_aer_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "dvs e2e_aer_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "dvs e2e_aer_test: FAIL (no completion)"; exit 1; \
	else \
		echo "dvs e2e_aer_test: PASS"; \
	fi

# dvs equivalent of chips/bench's e2e_reset_test: boots
# software/dvs_application/main.c, runs one AER batch, fires external
# reset, then runs the identical batch again -- only passes if the
# rebooted program genuinely re-registered its ISR vector, AER trigger
# level, and enable bit from a clean interrupt controller.
e2e_reset_test:
	@echo "--- dvs e2e_reset_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/dvs/tests/e2e/e2e_reset_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/dvs/tests/e2e/e2e_reset_test.act e2e_reset_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "dvs e2e_reset_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "dvs e2e_reset_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "dvs e2e_reset_test: FAIL (no completion)"; exit 1; \
	else \
		echo "dvs e2e_reset_test: PASS"; \
	fi

# dvs equivalent of chips/bench's e2e_gpio_test: boots
# software/dvs_gpio_demo/main.c, fires gpio_in_0/gpio_in_1 (wired to dvs's
# two spare event lines, event_id_1/event_id_2) and checks each ISR's own
# MMIO store lands on gpio_out_0..gpio_out_3.
e2e_gpio_test:
	@echo "--- dvs e2e_gpio_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_gpio_demo CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/dvs/tests/e2e/e2e_gpio_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/dvs/tests/e2e/e2e_gpio_test.act e2e_gpio_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "dvs e2e_gpio_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "dvs e2e_gpio_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "dvs e2e_gpio_test: FAIL (no completion)"; exit 1; \
	else \
		echo "dvs e2e_gpio_test: PASS"; \
	fi

# spi_prog's *read* direction, end to end -- every other dvs e2e test only
# drives its write side. Boots software/dvs_spi_read_demo/main.c, which
# reads a value through spi_prog from external_source
# (chips/dvs/tests/e2e/e2e_spi_read_test.act) and drives a self-checking
# pattern onto GPIO (0b1111 = matched).
e2e_spi_read_test:
	@echo "--- dvs e2e_spi_read_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_spi_read_demo CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/dvs/tests/e2e/e2e_spi_read_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/dvs/tests/e2e/e2e_spi_read_test.act e2e_spi_read_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "dvs e2e_spi_read_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "dvs e2e_spi_read_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "dvs e2e_spi_read_test: FAIL (no completion)"; exit 1; \
	else \
		echo "dvs e2e_spi_read_test: PASS"; \
	fi

# The full pipeline in one test: boot XIP out of spi_boot, load a data blob
# via spi_prog's read direction (not XIP), push AER events, let the
# interrupt fire, and observe each result (loaded value + pushed value)
# written back out over SPI via spi_prog's write direction --
# chips/dvs/tests/e2e/e2e_full_test.act chains every dvs mechanism through
# one real program (software/dvs_full_e2e/main.c) so a wiring mistake
# anywhere shows up as a wrong final value, not a silently-skipped step.
e2e_full_test:
	@echo "--- dvs e2e_full_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_full_e2e CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/dvs/tests/e2e/e2e_full_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/dvs/tests/e2e/e2e_full_test.act e2e_full_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "dvs e2e_full_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "dvs e2e_full_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "dvs e2e_full_test: FAIL (no completion)"; exit 1; \
	else \
		echo "dvs e2e_full_test: PASS"; \
	fi
