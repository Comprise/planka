# tests/

unittest плагина; запуск из корня — тесты затронутого модуля (`python3 -m unittest tests.test_<модуль>`), весь
набор (`make test`, `make check`) — только по просьбе автора.

## Инварианты

- Хук тестируется подпроцессом через `helpers.Env`: временные каталог плагина и данных, свои
  `philosophy.md` и `rules/`.
- Настоящий `claude` не вызывается: `tests/stub` первый в `PATH`, ответ задаёт `PLANKA_STUB`.
- Тест не зависит от окружения сессии, в которой запущен: `Env.environ` вычищает `PLANKA_*`,
  `CLAUDE_PLUGIN_OPTION_*`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_REMOTE_MEMORY_DIR`,
  `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`, ставит `HOME` во временный каталог и `PYTHONUTF8=0` хуку; новая переменная,
  которую читает код, — туда же. `tests/__init__.py` изолирует git на уровне процесса: снимает все `GIT_*`,
  отключает глобальный и системный конфиг и системные атрибуты, подменяет `XDG_CONFIG_HOME` пустым каталогом,
  ставит `GIT_CEILING_DIRECTORIES`. Изоляцию проверяет `make test-hostile`.
- Устойчивость плагина к чужому git-конфигу — `test_hostile_git.py`: враждебный `GIT_CONFIG_GLOBAL` передаётся явно
  только вызовам плагина, git подготовки репозитория — под изолированным конфигом. Новый вызов git в плагине проверяется
  там же (`git ls-files` в `common.project_root` — `HostileRootTest`, `git check-ignore` в `guard_memory` —
  `HostileMemoryTest`, `git ls-files` (в том числе жёсткой ссылки в `edit_targets`), `git cat-file --batch` (версии ref,
  стороны конфликта индекса, `cat-file` package.json предков версии, блобы по SHA дерева HEAD в `project_names` и
  `compare`), `git ls-tree -r -z --full-tree` (одно на дерево, общее у npm workspace версий `package.json` в
  `head_names` и у `_tree_names`; `git ls-files -s` — стороны конфликта) в `manifest_watch` — `HostileManifestTest`).
- Тест не импортирует другой тест: помощники, нужные нескольким файлам (`run_in_process`, `fill_budget`,
  `assert_not_logged`, `prompt_block`, `author_block`, `cpu_seconds`, `assert_linear`, `comment_lines`), — в
  `helpers.py`.
- Вход хука — `Env.hook_input(event, **fields)`: `cwd` — каталог проекта `Env`, `transcript_path` —
  `Env.transcript` с ответом ассистента модели `claude-test-model`.
- `tools/` — скрипты разработки без `__init__.py` (unittest их не собирает): `build_corpus.py` собирает
  `fixtures/bash-transcripts.jsonl`, `bashdiff.py` — фаззер `make bashdiff` в песочнице `bwrap`. Команды корпусов не
  исполняются никогда; в bash идут только формы генератора фаззера.
- В `plugin/planka/*.py` имя константы с текстом правил (например `planparse._READ_NOTE`) не складывается в выражения,
  попадающие в возвращаемые значения функций: `test_contract._flow_strings` выводит тексты для модели по потоку значений
  и принял бы такую константу за текст для модели.
- `fixtures/` — корпуса настоящих форм входа; какой тест читает какой корпус и что сверяет —
  `context/testing.md`, «Устройство тестов». Новый край разборщика — образцом в корпус.

## Читать перед правкой

- `../context/testing.md`
