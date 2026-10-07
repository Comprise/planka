# tests/

unittest плагина; запуск — `make test` из корня.

## Инварианты

- Хук тестируется подпроцессом через `helpers.Env`: временные каталог плагина и данных, свои
  `philosophy.md` и `rules/`.
- Настоящий `claude` не вызывается: `tests/stub` первый в `PATH`, ответ задаёт `PLANKA_STUB`.
- Тест не зависит от окружения сессии, в которой запущен: `Env.environ` вычищает `PLANKA_*`,
  `CLAUDE_PLUGIN_OPTION_*`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR` и ставит `HOME` во временный
  каталог; новая переменная, которую читает код, — туда же. `tests/__init__.py` изолирует git на
  уровне процесса: глобальный и системный конфиг, `GIT_*`, `GIT_CEILING_DIRECTORIES`.
- Устойчивость плагина к чужому git-конфигу — `test_hostile_git.py`: враждебный `GIT_CONFIG_GLOBAL`
  передаётся явно только вызовам плагина, git подготовки репозитория — под изолированным конфигом.
  Новый вызов git в плагине проверяется там же.
- Вход хука — `Env.hook_input(event, **fields)`: `cwd` — каталог проекта `Env`, `transcript_path` —
  `Env.transcript` с ответом ассистента модели `claude-test-model`.
- `fixtures/` — корпуса настоящих форм входа: `plan-*.md` (`test_planparse`), `transcript-shapes.jsonl`
  (`test_common`), `bash-failure-errors.jsonl` (`test_debug_watch`); новый край разборщика — образцом в
  корпус.

## Читать перед правкой

- `../context/testing.md`
