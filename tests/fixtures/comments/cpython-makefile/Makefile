# источник: CPython 3.14.7, Makefile (из Makefile.pre.in), строки 1105–1140 и 1787–1790
	@rm -rf $(COVERAGE_REPORT)
	@lcov $(COVERAGE_LCOV_OPTIONS) --capture \
	    --directory $(abs_builddir) \
	    --base-directory $(realpath $(abs_builddir)) \
	    --path $(realpath $(abs_srcdir)) \
	    --output-file $(COVERAGE_INFO)
	@ # remove 3rd party modules, system headers and internal files with
	@ # debug, test or dummy functions.
	@lcov $(COVERAGE_LCOV_OPTIONS) --remove $(COVERAGE_INFO) \
	    '*/Modules/_hacl/*' \
	    '*/Modules/_ctypes/libffi*/*' \
	    '*/Modules/_decimal/libmpdec/*' \
	    '*/Modules/expat/*' \
	    '*/Modules/xx*.c' \
	    '*/Python/pyfpe.c' \
	    '*/Python/pystrcmp.c' \
	    '/usr/include/*' \
	    '/usr/local/include/*' \
	    '/usr/lib/gcc/*' \
	    --output-file $(COVERAGE_INFO)
	@genhtml $(COVERAGE_INFO) \
	    --output-directory $(COVERAGE_REPORT) \
	    $(COVERAGE_REPORT_OPTIONS)
	@echo
	@echo "lcov report at $(COVERAGE_REPORT)/index.html"
	@echo

# Force regeneration of parser and frozen modules
.PHONY: coverage-report
coverage-report: regen-token regen-frozen
	@ # build with coverage info
	$(MAKE) coverage
	@ # run tests, ignore failures
	$(TESTRUNNER) --fast-ci --timeout=$(TESTTIMEOUT) $(TESTOPTS) || true
	@ # build lcov report
	$(MAKE) coverage-lcov

Modules/Setup.local:
	@# Create empty Setup.local when file was deleted by user
	echo "# Edit this file for local setup changes" > $@
