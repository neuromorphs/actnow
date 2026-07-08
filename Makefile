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

.PHONY: all test list clean $(TESTS)

all: test

test: $(TESTS)
	@echo "=== all tests passed ==="

list:
	@echo $(TESTS)

$(TESTS):
	@echo "--- $@ ---"
	@$(AFLAT) tests/$@.act
	@out=$$(printf "cycle\nquit\n" | $(ACTSIM) tests/$@.act $@ 2>&1); \
	echo "$$out"; \
	if echo "$$out" | grep -qiE "ASSERTION failed|EBREAK -- test FAILED"; then \
		echo "$@: FAIL"; exit 1; \
	else \
		echo "$@: PASS"; \
	fi

clean:
	rm -f .actsim_history
