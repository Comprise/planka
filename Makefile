.PHONY: test test-hostile validate check bashdiff

test:
	python3 -m unittest discover -s tests -t . -v

# Тот же прогон в неудобной локали и кодировке вывода (LC_ALL=C, PYTHONIOENCODING=latin-1); хуки в нём работают
# в кодировке ascii (Env.environ ставит им PYTHONUTF8=0). Чужие git-настройки — подпись коммитов и игнор *.py
# через GIT_CONFIG_GLOBAL, GIT_CONFIG_COUNT/KEY/VALUE, GIT_CONFIG_PARAMETERS (их экспортирует git -c при
# rebase -x) и XDG_CONFIG_HOME/git/{ignore,attributes} — и CLAUDE_PROJECT_DIR, CLAUDE_CONFIG_DIR проверяют
# изоляцию тестов: tests/__init__.py и Env.environ их подменяют, до кода плагина они не доходят. Тесты guard_memory
# ставят CLAUDE_CONFIG_DIR сами: что Env.environ вычищает каждую переменную окружения сессии, проверяет
# EnvIsolationTest в tests/test_common.py. Устойчивость плагина к чужому git-конфигу проверяет
# tests/test_hostile_git.py — в обеих целях.
test-hostile:
	@cfg=$$(mktemp) && ign=$$(mktemp) && xdg=$$(mktemp -d) && \
	trap 'rm -rf "$$cfg" "$$ign" "$$xdg"' EXIT && \
	echo '*.py' > "$$ign" && mkdir "$$xdg/git" && echo '*.py' > "$$xdg/git/ignore" && \
	echo '* binary' > "$$xdg/git/attributes" && \
	printf '[commit]\n\tgpgsign = true\n[core]\n\texcludesFile = %s\n' "$$ign" > "$$cfg" && \
	GIT_CONFIG_GLOBAL="$$cfg" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=true \
	GIT_CONFIG_PARAMETERS="'core.excludesfile'='$$ign'" XDG_CONFIG_HOME="$$xdg" \
	CLAUDE_PROJECT_DIR="$(CURDIR)" PYTHONIOENCODING=latin-1 CLAUDE_CONFIG_DIR=/nonexistent LC_ALL=C \
	python3 -m unittest discover -s tests -t . -v

validate:
	claude plugin validate .
	claude plugin validate plugin

check: test test-hostile validate

# Дифференциальный фаззер детектора зависимостей против настоящего bash (tests/tools/bashdiff.py): формы только
# генератора, исполнение — только в bwrap (без него — выход с кодом 2). Зерно и число форм фиксированы; каталог
# расхождений — BASHDIFF_OUT, по умолчанию новый временный. В check не входит: нужен bwrap, прогон долгий.
BASHDIFF_OUT ?= $(shell mktemp -d)
bashdiff:
	python3 tests/tools/bashdiff.py --target detector --engine old --seed 1 --count 20000 --out $(BASHDIFF_OUT)
