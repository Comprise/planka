# tests/

unittest плагина; запуск — `make test` из корня.

## Инварианты

- Хук тестируется подпроцессом через `helpers.Env`: временные каталог плагина и данных, свои
  `philosophy.md` и `rules/`.
- Настоящий `claude` не вызывается: `tests/stub` первый в `PATH`, ответ задаёт `PLANKA_STUB`.
- Тест не зависит от окружения сессии, в которой запущен: `Env.environ` вычищает `PLANKA_*`,
  `CLAUDE_PLUGIN_OPTION_*`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_REMOTE_MEMORY_DIR`,
  `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`, ставит `HOME` во временный
  каталог и `PYTHONUTF8=0` хуку; новая переменная, которую читает код, — туда же. `tests/__init__.py`
  изолирует git на уровне процесса: снимает все `GIT_*`, отключает глобальный и системный конфиг и
  системные атрибуты, подменяет `XDG_CONFIG_HOME` пустым каталогом, ставит `GIT_CEILING_DIRECTORIES`.
  Изоляцию проверяет `make test-hostile`.
- Устойчивость плагина к чужому git-конфигу — `test_hostile_git.py`: враждебный `GIT_CONFIG_GLOBAL`
  передаётся явно только вызовам плагина, git подготовки репозитория — под изолированным конфигом.
  Новый вызов git в плагине проверяется там же (`git ls-files` в `common.project_root` — `HostileRootTest`,
  `git check-ignore` в `guard_memory` — `HostileMemoryTest`,
  `git ls-files`, `git cat-file --batch` (версии ref, стороны конфликта индекса, дерево HEAD в `project_names` и
  `compare`) и `git ls-tree` в `manifest_watch` — `HostileManifestTest`).
- Тест не импортирует другой тест: помощники, нужные нескольким файлам (`run_in_process`, `fill_budget`,
  `assert_not_logged`, `author_block`, `cpu_seconds`, `assert_linear`, `comment_lines`), — в `helpers.py`.
- Вход хука — `Env.hook_input(event, **fields)`: `cwd` — каталог проекта `Env`, `transcript_path` —
  `Env.transcript` с ответом ассистента модели `claude-test-model`.
- `fixtures/` — корпуса настоящих форм входа: `plan-*.md` (`test_planparse`, ожидаемая структура
  каждого плана — `CORPUS`), `transcript-shapes.jsonl` (`test_common`; в том числе отказы
  инструмента `user-rejected`, `permission-rule`, `automode-blocked`), `bash-failure-errors.jsonl`
  (`test_debug_watch`), `bash-commands.jsonl` (`test_depcheck`, `CorpusTest`: `source` — `transcript` или `edge`,
  `expect` — `add`, `doubt` или `pass`), `manifests/` (`test_manifests`, имена каждого манифеста — `CORPUS`, имя его
  пакета — `OWN`;
  файлы `setup.py`, `setup.cfg`, `Pipfile` в `legacy-*/` — `LEGACY_CORPUS`, первая строка — источник), `comments/`
  (`test_comments`, номера строк комментариев каждого файла — `CORPUS`); корпус форм импорта `@путь` памяти —
  `ImportParseTest.CORPUS` в `test_guard_memory.py`;
  новый край разборщика — образцом в корпус.

## Читать перед правкой

- `../context/testing.md`
