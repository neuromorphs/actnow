# e2e test rules for chips/bench (robustness.md Stage 1.5) -- delegated to
# from the top-level Makefile (`make e2e_fifo_test` etc., or as part of
# `make test`), invoked as `$(MAKE) -C chips/bench <target>`.
#
# Shared infrastructure (file-registry / $(ROM_IMAGE) / $(ROM_IMAGE_HANG) /
# $(ROM_IMAGE_APPLICATION)) stays owned by the top-level Makefile -- it's
# needed by every test (file_registry.txt requires all registered inputs to
# exist before gen/ can be regenerated at all), not just these -- so each
# target below calls back into it via an explicit sub-make
# ($(MAKE) -C $(ROOT) ...), same as these recipes already did when they
# lived at the top level.
#
# Every aflat/actsim invocation below explicitly `cd`s to $(ROOT) first: ACT
# resolves every `import` relative to the compiler's invocation directory,
# not the importing file, so these must run with actnow/ as their own cwd
# regardless of where this Makefile itself was invoked from (see the
# top-level Makefile's own comment on this, and the README's Toolchain
# section).

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

.PHONY: e2e_fifo_test e2e_fifo_stress_test e2e_multi_event_test e2e_reset_test e2e_reset_reload_test e2e_gpio_test e2e_boot_test e2e_multi_event_reset_test

# e2e_fifo_test.act exercises software/application/main.c's real
# interrupt/FIFO flow specifically -- it needs that exact ROM image, not
# whatever ROM_TEST happens to default to. $(ROM_IMAGE) is a shared file
# target that the top-level Makefile only rebuilds once per invocation (the
# first time anything needs it) -- a plain prerequisite can't force a
# *second* rebuild here if some earlier test in the same `make test` sweep
# already claimed it with a different ROM_TEST. And against a mismatched
# image, this doesn't fail loudly: the unconfigured interrupt vector just
# sends pc back to that image's own _start instead of a real ISR, this
# test's own pop blocks forever, and actsim quiesces -- a false PASS from
# the assertion-grep, since nothing ever actually asserts. So: force a fresh
# image via an explicit sub-make (a genuinely separate invocation,
# unaffected by whatever the outer one already built), exactly like
# software-tests already does per test, then restore the default afterward
# so a later `make rom_program_test` stays deterministic.
e2e_fifo_test:
	@echo "--- e2e_fifo_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_fifo_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_fifo_test.act e2e_fifo_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fifo_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fifo_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_fifo_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fifo_test: PASS"; \
	fi

# Continuous-push regression for the fifo_in single-batch hang: pushes a stream
# far past one batch while draining results in parallel (see the test's own
# comment). Same ROM image and pinning rationale as e2e_fifo_test above.
e2e_fifo_stress_test:
	@echo "--- e2e_fifo_stress_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_fifo_stress_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_fifo_stress_test.act e2e_fifo_stress_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_fifo_stress_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_fifo_stress_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete\|stress complete"; then \
		echo "e2e_fifo_stress_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_fifo_stress_test: PASS"; \
	fi

# Same rationale and pattern as e2e_fifo_test above, pinned to
# software/multi_event/main.c instead.
e2e_multi_event_test:
	@echo "--- e2e_multi_event_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=multi_event CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_multi_event_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_multi_event_test.act e2e_multi_event_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_multi_event_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_multi_event_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_multi_event_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_multi_event_test: PASS"; \
	fi

# Exercises core/soc.act's reset_ext port with a real compiled program
# through the real bootloader (unlike tests/core/reset_test.act, which
# hand-assembles instructions directly): boots software/application/main.c,
# runs one batch through it, fires external reset, then confirms the exact
# same bootloader+program combination cleanly reboots from scratch and
# correctly runs a second batch. Same ROM-pinning rationale as
# e2e_fifo_test above.
e2e_reset_test:
	@echo "--- e2e_reset_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=application CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_reset_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_reset_test.act e2e_reset_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_reset_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_reset_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_reset_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_reset_test: PASS"; \
	fi

# Baseline scenario: boot software/boot_only/main.c (zero peripheral
# interaction) and confirm it reaches WFI cleanly. Unlike every other e2e
# rule here, e2e_boot_test.act has no chp body to emit a "test complete"
# line -- there's nothing for it to synchronize against -- so completion is
# judged directly from soc's own log line instead, same convention
# `make software-tests` already uses for programs with no testbench-side
# interaction at all.
e2e_boot_test:
	@echo "--- e2e_boot_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=boot_only CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_boot_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_boot_test.act e2e_boot_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_boot_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_boot_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "decoded wfi"; then \
		echo "e2e_boot_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_boot_test: PASS"; \
	fi

# Same rationale and pattern as e2e_multi_event_test above, pinned to
# software/multi_event/main.c as well -- extends its coverage with
# back-to-back (no artificial delay) event firing across a reset boundary.
# See chips/bench/tests/e2e/e2e_multi_event_reset_test.act's own comment.
e2e_multi_event_reset_test:
	@echo "--- e2e_multi_event_reset_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=multi_event CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_multi_event_reset_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_multi_event_reset_test.act e2e_multi_event_reset_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_multi_event_reset_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_multi_event_reset_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_multi_event_reset_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_multi_event_reset_test: PASS"; \
	fi

# Exercises GPIO (core/peripherals/gpio.act) end to end, both directions in
# one real program (software/gpio_demo/main.c): firing gpio_in_0/gpio_in_1
# wakes the CPU through the existing interrupt-controller vectoring, and
# each ISR's own MMIO store drives the GPIO output pins, observed here.
# Same ROM-pinning rationale as e2e_fifo_test above.
e2e_gpio_test:
	@echo "--- e2e_gpio_test ---"
	@rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem
	@$(MAKE) -s -C $(ROOT) ROM_TEST=gpio_demo CROSS=$(CROSS) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_gpio_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_gpio_test.act e2e_gpio_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	rm -f $(ROOT)/$(ROM_IMAGE) $(ROOT)/software/tests/build/rom.mem $(ROOT)/software/build/rom.mem; \
	$(MAKE) -s -C $(ROOT) ROM_TEST=$(ROM_TEST) BOOT=$(BOOT) CROSS=$(CROSS) file-registry >/dev/null 2>&1 || true; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_gpio_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_gpio_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_gpio_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_gpio_test: PASS"; \
	fi

# The full "oh shit, wrong firmware -- reset and reboot into the corrected
# one" scenario: unlike e2e_reset_test above (which reboots the *same*
# image), this boots a genuinely broken program (software/hang), then flips
# core/peripherals/rom_selector.act's bank select to the corrected program
# (software/application) and asserts reset_ext. $(ROM_IMAGE_HANG)/
# $(ROM_IMAGE_APPLICATION) are permanent fixtures owned by the top-level
# Makefile's own file-registry chain (built once, not rebuilt per test like
# $(ROM_IMAGE) above), so no rebuild/restore dance is needed here -- just
# make sure gen/ (and those two fixtures) exist.
e2e_reset_reload_test:
	@echo "--- e2e_reset_reload_test ---"
	@$(MAKE) -s -C $(ROOT) file-registry
	@cd $(ROOT) && $(AFLAT) chips/bench/tests/e2e/e2e_reset_reload_test.act
	@out=$$(cd $(ROOT) && printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf chips/bench/tests/e2e/e2e_reset_reload_test.act e2e_reset_reload_test 2>&1); \
	status=$$?; \
	echo "$$out"; \
	if [ $$status -ne 0 ]; then \
		echo "e2e_reset_reload_test: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "e2e_reset_reload_test: FAIL"; exit 1; \
	elif ! echo "$$out" | grep -qi "test complete"; then \
		echo "e2e_reset_reload_test: FAIL (no completion)"; exit 1; \
	else \
		echo "e2e_reset_reload_test: PASS"; \
	fi
