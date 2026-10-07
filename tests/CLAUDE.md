# tests/

unittest плагина; запуск — `make test` из корня.

## Инварианты

- Хук тестируется подпроцессом через `helpers.Env`: временные каталог плагина и данных, свои
  `philosophy.md` и `rules/`.
- Настоящий `claude` не вызывается: `tests/stub` первый в `PATH`, ответ задаёт `PLANKA_STUB`.
- Тест не зависит от окружения сессии, в которой запущен: `Env.environ` вычищает `PLANKA_*`,
  `CLAUDE_PLUGIN_OPTION_*` и `CLAUDE_PROJECT_DIR`; новая переменная, которую читает код, — туда же.
- `../docs/superpowers/plans/2026-09-30-planka.md` — фикстура `test_planparse`.

## Читать перед правкой

- `../context/testing.md`
