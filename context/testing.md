# Проверки

```bash
make test        # python3 -m unittest discover -s tests -t . -v
make validate    # claude plugin validate . и plugin: маркетплейс и плагин
```

CI нет; обе цели — в `Makefile`.

## Устройство тестов

- Модульные тесты импортируют `planka/<модуль>.py` напрямую через `sys.path`.
- Входные данные тестов — `tests/fixtures/`: `tests/fixtures/plan-waves.md` — план с волнами для
  `test_planparse`.
- Тесты снимка в режиме git (`GitScanTest`) создают временный репозиторий и пропускаются без `git`.
- Тесты хуков запускают скрипт подпроцессом: `tests.helpers.Env` создаёт временные
  `CLAUDE_PLUGIN_ROOT` (свои `philosophy.md` и `rules/` из `helpers.PHILOSOPHY` и
  `helpers.RULES`), `CLAUDE_PLUGIN_DATA` и каталог проекта; `Env.run` подаёт вход хука в stdin.
  Ответ разбирают `helpers.output` и `helpers.messages` (строки `systemMessage`).
- Окружение `Env.environ` вычищает `PLANKA_*`, `CLAUDE_PLUGIN_OPTION_*` и `CLAUDE_PROJECT_DIR` и ставит
  `tests/stub` первым в `PATH`: вместо `claude` отвечает заглушка `tests/stub/claude`.

## Заглушка судьи

`tests/stub/claude` ведёт себя по `PLANKA_STUB`: `ok`, `deny` (причина из
`PLANKA_STUB_REASON`), `hang` (через `exec sleep`: висит сам процесс заглушки), `garbage`,
`notlogged`. При `PLANKA_STUB_RECORD=<файл>` пишет туда аргументы, stdin, `PLANKA_JUDGE`, `cwd` и
PID процесса. Новый вид ответа судьи — новая ветка заглушки.

## Тесты в том же процессе

Тестовых крючков в рабочем коде нет. Где нужно подменить модуль — порог `snapshot.MAX_FILES`,
сбой `snapshot.scan`, — тест зовёт `common.run_hook(remind.main)` в своём процессе с `mock.patch`
(`RemindTest.run_in_process`).

## Что тестами не покрыто

- Тесты хуков берут свои тексты правил (`helpers.PHILOSOPHY`, `helpers.RULES`); настоящие
  `plugin/philosophy.md` и `plugin/rules/*.md` проверяет только `tests/test_contract.py`: разделы и
  модули, которые код берёт по имени, полнота индекса «Модули», метки языков.
- Живой `claude`: вызов судьи и показ `systemMessage` проверяются только вручную.
