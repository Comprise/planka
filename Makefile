.PHONY: test test-hostile validate check

test:
	python3 -m unittest discover -s tests -t . -v

# Тот же прогон в неудобной локали и кодировке вывода (LC_ALL=C, PYTHONIOENCODING=latin-1). Чужие git-конфиг
# (подпись коммитов, глобальный игнор *.py), CLAUDE_PROJECT_DIR и CLAUDE_CONFIG_DIR проверяют изоляцию тестов:
# tests/__init__.py и Env.environ их подменяют, до кода плагина они не доходят. Устойчивость плагина к чужому
# git-конфигу проверяет tests/test_hostile_git.py — в обеих целях.
test-hostile:
	@cfg=$$(mktemp) && ign=$$(mktemp) && \
	trap 'rm -f "$$cfg" "$$ign"' EXIT && \
	echo '*.py' > "$$ign" && \
	printf '[commit]\n\tgpgsign = true\n[core]\n\texcludesFile = %s\n' "$$ign" > "$$cfg" && \
	GIT_CONFIG_GLOBAL="$$cfg" CLAUDE_PROJECT_DIR="$(CURDIR)" PYTHONIOENCODING=latin-1 \
	CLAUDE_CONFIG_DIR=/nonexistent LC_ALL=C \
	python3 -m unittest discover -s tests -t . -v

validate:
	claude plugin validate .
	claude plugin validate plugin

check: test test-hostile validate
