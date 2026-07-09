# Compile and simulate the ACT testbenches under tests/.
#
#   make            - run every test in tests/
#   make test       - same as above
#   make wfi_test   - run just tests/wfi_test.act (works for any test by name)
#   make list       - list the test names make has discovered
#   make clean      - remove local simulator artifacts
#
# Must be run from this directory (actnow/) -- ACT resolves every `import`
# relative to the working directory the compiler was invoked from, not
# relative to the importing file, so soc.act's own imports only resolve
# correctly when this is the cwd. See the README's Toolchain section.

AFLAT  := aflat
ACTSIM := actsim

TESTS := $(basename $(notdir $(wildcard tests/*.act)))
FILE_REGISTRY     := tests/files/file_registry.txt
FILE_REGISTRY_GEN := gen/file_ids.act gen/file_registry.conf

# Compiled program image consumed by tests/rom_program_test.act, registered as
# ROM_IMAGE in $(FILE_REGISTRY). It's a build artifact (see software/tests/), so
# the registry generator -- which requires every registered input to exist --
# depends on it below. ROM_TEST picks which program under software/tests/ to
# build (its .S must exist there); override on the command line to run another.
ROM_TEST  ?= simple
ROM_IMAGE := software/tests/build/rom.actsim.mem

# RISC-V cross-compiler prefix for building program images. Auto-detected from
# PATH (core is RV32I, so a 32- or 64-bit multilib toolchain both work); falls
# back to riscv64-unknown-elf-. Override on the command line: make CROSS=...
CROSS ?= $(firstword \
    $(foreach p,riscv32-unknown-elf- riscv64-unknown-elf- riscv-none-elf-, \
        $(if $(shell command -v $(p)gcc 2>/dev/null),$(p))) \
    riscv64-unknown-elf-)

.PHONY: all test list clean file-registry $(TESTS)

all: test

test: $(TESTS)
	@echo "=== all tests passed ==="

file-registry: $(FILE_REGISTRY_GEN)

$(ROM_IMAGE):
	$(MAKE) -C software/tests TEST=$(ROM_TEST) CROSS=$(CROSS)

$(FILE_REGISTRY_GEN): $(FILE_REGISTRY) tools/gen_file_registry.py $(ROM_IMAGE)
	@python3 tools/gen_file_registry.py $(FILE_REGISTRY) gen

list:
	@echo $(TESTS)

$(TESTS): $(FILE_REGISTRY_GEN)
	@echo "--- $@ ---"
	@$(AFLAT) tests/$@.act
	@out=$$(printf "cycle\nquit\n" | $(ACTSIM) -cnf=gen/file_registry.conf tests/$@.act $@ 2>&1); \
	status=$$?; \
	echo "$$out"; \
	if [ $$status -ne 0 ]; then \
		echo "$@: FAIL"; exit $$status; \
	elif echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "$@: FAIL"; exit 1; \
	else \
		echo "$@: PASS"; \
	fi

clean:
	rm -f .actsim_history
	rm -drf gen/
