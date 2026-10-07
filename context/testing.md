# Проверки

```bash
make test        # python3 -m unittest discover -s tests -t . -v
make validate    # claude plugin validate .
```

CI нет; обе цели — в `Makefile`.

## Устройство тестов

- Модульные тесты импортируют `planka/<модуль>.py` напрямую через `sys.path`.
- Тесты хуков запускают скрипт подпроцессом: `tests.helpers.Env` создаёт временные
  `CLAUDE_PLUGIN_ROOT` (свои `philosophy.md` и `rules/` из `helpers.PHILOSOPHY` и
  `helpers.RULES`), `CLAUDE_PLUGIN_DATA` и каталог проекта; `Env.run` подаёт вход хука в stdin.
  Ответ разбирают `helpers.output` и `helpers.messages` (строки `systemMessage`).
- Окружение `Env.environ` вычищает `PLANKA_*`, `CLAUDE_PLUGIN_OPTION_*` и `CLAUDE_PROJECT_DIR` и ставит
  `tests/stub` первым в `PATH`: вместо `claude` отвечает заглушка `tests/stub/claude`.

## Заглушка судьи

`tests/stub/claude` ведёт себя по `PLANKA_STUB`: `ok`, `deny` (причина из
`PLANKA_STUB_REASON`), `hang`, `garbage`, `notlogged`. При `PLANKA_STUB_RECORD=<файл>` пишет
туда аргументы, stdin, `PLANKA_JUDGE` и `cwd`. Новый вид ответа судьи — новая ветка заглушки.

## Тестовые крючки в коде

- `PLANKA_TEST_MAX_FILES` — порог снимка в `remind.take_snapshot`.

## Что тестами не покрыто

- Реальные `philosophy.md` и `rules/*.md`: тесты берут свои тексты, поэтому переименование
  раздела или модуля из контракта (`context/architecture.md`, «Контракт кода с текстами
  правил») тесты не ловят.
- Живой `claude`: вызов судьи и показ `systemMessage` проверяются только вручную.
