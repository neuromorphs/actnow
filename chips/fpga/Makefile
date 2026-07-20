# e2e test rules for chips/fpga -- run as `$(MAKE) -C chips/fpga <target>`.
#
# Same conventions as chips/bench/Makefile: shared infrastructure (file-registry
# / $(ROM_IMAGE)) stays owned by the top-level Makefile and is invoked via an
# explicit sub-make ($(MAKE) -C $(ROOT) ...); every aflat/actsim invocation
# `cd`s to $(ROOT) first, because ACT resolves every `import` relative to the
# compiler's invocation directory, not the importing file.

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

.PHONY: test e2e_fpga_boot_test e2e_fpga_fifo_test e2e_fpga_fifo_stress e2e_fpga_reset_test e2e_fpga_reset_reload_test e2e_fpga_rotate_test e2e_fpga_track_test e2e_fpga_track_hotpixel_test e2e_fpga_track_gate_test

# Run every fpga e2e test. multi_event / gpio scenarios from chips/bench are
# deliberately absent: this variant wires only a single interrupt (event_id_0)
# and has no GPIO peripheral, so neither is applicable here.
test: e2e_fpga_boot_test e2e_fpga_fifo_test e2e_fpga_fifo_stress e2e_fpga_reset_test e2e_fpga_reset_reload_test e2e_fpga_rotate_test e2e_fpga_track_test e2e_fpga_track_hotpixel_test e2e_fpga_track_gate_test
	@echo "=== all fpga e2e tests passed ==="

# Boot software/boot_only/main.c XIP from the raw ROM route and confirm it
# reaches WFI. Like chips/bench's e2e_boot_test, completion is judged from soc's
# own "decoded wfi" log line (the test has no chp body to emit a "test complete"
# line). Force a fresh boot_only image via an explicit sub-make, then restore
# the default ROM afterward so a later `make rom_program_test` stays
# deterministic -- same ROM-pinning rationale as chips/bench/Makefile.
e2e_fpga_boot_test:
	@echo "--- e2e_fpga_boot_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=boot_only CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_boot_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_boot_test.act e2e_fpga_boot_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_boot_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_boot_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "decoded wfi"; then \
		echo "e2e_fpga_boot_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_boot_test: PASS"; \
	fi

# Boot software/application through the bootloader and run two batches through
# its real ISR: input events on base 5, results captured on the base-6 raw route
# (modelled by a testbench fifo_out). Force a fresh application image via an
# explicit sub-make, then restore the default ROM afterward -- same ROM-pinning
# rationale as chips/bench/Makefile.
# Continuous-push regression for the fifo_in single-batch hang: streams past one
# batch while draining the base-6 results in parallel (see the test's own
# comment). Same ROM image and pinning as e2e_fpga_fifo_test below.
e2e_fpga_fifo_stress:
	@echo "--- e2e_fpga_fifo_stress ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_fifo_stress.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_fifo_stress.act e2e_fpga_fifo_stress 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_fifo_stress: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_fifo_stress: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "stress complete"; then \
		echo "e2e_fpga_fifo_stress: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_fifo_stress: PASS"; \
	fi

e2e_fpga_fifo_test:
	@echo "--- e2e_fpga_fifo_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_fifo_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_fifo_test.act e2e_fpga_fifo_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_fifo_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_fifo_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fpga_fifo_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_fifo_test: PASS"; \
	fi

# Same as e2e_fpga_fifo_test, but fires reset_ext between two batches to prove a
# clean reboot of the same bootloader+application image.
e2e_fpga_reset_test:
	@echo "--- e2e_fpga_reset_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_reset_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_reset_test.act e2e_fpga_reset_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_reset_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_reset_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fpga_reset_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_reset_test: PASS"; \
	fi

# Dual-bank boot recovery: boot the broken software/hang image (bank A), flip a
# rom_selector to the corrected software/application (bank B), reset, and run a
# batch. Uses the permanent ROM_IMAGE_HANG / ROM_IMAGE_APPLICATION fixtures the
# top-level Makefile's file-registry chain builds once -- no per-test ROM rebuild
# dance needed, just ensure gen/ (and those fixtures) exist.
e2e_fpga_reset_reload_test:
	@echo "--- e2e_fpga_reset_reload_test ---"
	@$(MAKE) -s -C $(ROOT) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_reset_reload_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_reset_reload_test.act e2e_fpga_reset_reload_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_reset_reload_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_reset_reload_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fpga_reset_reload_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_reset_reload_test: PASS"; \
	fi

# Replays 200 events from a real recorded capture (dvs_capture_*.csv) through
# software/dvs_rotate/main.c's ISR three times in a row and asserts every
# rotated result against the reference values baked into
# e2e_fpga_rotate_events.act -- see that file and e2e_fpga_rotate_test.act's
# header for the rotation math. Same ROM-pinning rationale as the other e2e_*
# targets: force a fresh dvs_rotate image, restore the default ROM afterward.
e2e_fpga_rotate_test:
	@echo "--- e2e_fpga_rotate_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_rotate CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_rotate_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_rotate_test.act e2e_fpga_rotate_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_rotate_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_rotate_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fpga_rotate_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_rotate_test: PASS"; \
	fi

# Replays the same 256 recorded events (64 batches, one DUMP_INTERVAL) as
# e2e_fpga_denoise_test.act through software/dvs_track/main.c's ISR and
# asserts the resulting EMA-centroid/bounding-box/lock status word and bbox
# word against the reference values computed in Python when this file was
# generated -- see e2e_fpga_track_test.act's header for the exact math and
# packing. Same ROM-pinning rationale as the other e2e_* targets: force a
# fresh dvs_track image, restore the default ROM afterward.
e2e_fpga_track_test:
	@echo "--- e2e_fpga_track_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_track CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_track_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_track_test.act e2e_fpga_track_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_track_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_track_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fpga_track_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_track_test: PASS"; \
	fi

# Same 256-event/one-DUMP_INTERVAL shape as e2e_fpga_track_test, but the
# first 40 events are 40 copies of one synthetic stuck-pixel word -- proves
# software/dvs_track/main.c's hot-pixel filter (see that file's header)
# actually drops a sustained repeat-firing pixel's events (21 of the 40, in
# this fixture) instead of letting them corrupt the reported centroid/bbox,
# while leaving the following 216 real events untouched. Same ROM-pinning
# rationale as the other e2e_* targets.
e2e_fpga_track_hotpixel_test:
	@echo "--- e2e_fpga_track_hotpixel_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_track CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_track_hotpixel_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_track_hotpixel_test.act e2e_fpga_track_hotpixel_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_track_hotpixel_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_track_hotpixel_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fpga_track_hotpixel_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_track_hotpixel_test: PASS"; \
	fi

# Two-window test proving software/dvs_track/main.c's tracking gate actually
# keeps a locked-on bounding box tight instead of ballooning out to include
# unrelated activity elsewhere in the frame -- see e2e_fpga_track_gate_test.act's
# header for the exact scenario (a tight cluster locks in window 1, then a
# far-away 56-event burst in window 2 must be rejected by the gate). Same
# ROM-pinning rationale as the other e2e_* targets.
e2e_fpga_track_gate_test:
	@echo "--- e2e_fpga_track_gate_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=dvs_track CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/fpga/tests/e2e/e2e_fpga_track_gate_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/fpga/tests/e2e/e2e_fpga_track_gate_test.act e2e_fpga_track_gate_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fpga_track_gate_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fpga_track_gate_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fpga_track_gate_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fpga_track_gate_test: PASS"; \
	fi
