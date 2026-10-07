# Проверки

```bash
make test           # python3 -m unittest discover -s tests -t . -v
make test-hostile   # тот же прогон в локали C и с выводом latin-1, с проверкой изоляции
make validate       # claude plugin validate . и plugin: маркетплейс и плагин
make check          # test, test-hostile и validate
```

CI нет; цели — в `Makefile`. Проверяются два разных свойства:

1. Тесты изолированы от машины. `test-hostile` запускает тот же набор в неудобной локали и кодировке
   (`LC_ALL=C`, `PYTHONIOENCODING=latin-1`) и с чужими `GIT_CONFIG_GLOBAL` (`commit.gpgsign = true`,
   `core.excludesFile` с `*.py`), `CLAUDE_PROJECT_DIR` на репозиторий и `CLAUDE_CONFIG_DIR=/nonexistent`.
   До кода плагина эти три не доходят: `tests/__init__.py` и `Env.environ` их подменяют; их задача —
   упасть, если изоляция сломана.
2. Плагин работает под чужим git-конфигом пользователя — `tests/test_hostile_git.py`, в обеих целях.

## Устройство тестов

- По файлу `tests/test_<модуль>.py` на модуль `plugin/planka/`; `tests/test_contract.py` — контракт
  с настоящими текстами правил; `tests/test_hostile_git.py` — git-код нескольких модулей под чужим
  git-конфигом.
- Модульные тесты импортируют `planka/<модуль>.py` напрямую через `sys.path`.
- `tests/__init__.py` изолирует процесс тестов от git-настроек и окружения машины: глобальный и
  системный конфиг git не читаются (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`), переменные `GIT_DIR`,
  `GIT_WORK_TREE` и подобные и `CLAUDE_PROJECT_DIR` сняты, `GIT_CEILING_DIRECTORIES` — временный
  каталог: внешний репозиторий не находится.
- Входные данные тестов — `tests/fixtures/`, корпуса настоящих форм входа (`plugin/rules/heuristics.md`);
  источник формы назван в каждом файле:
  - `plan-*.md` — планы (волны, формат superpowers, канонический формат `planning.md`); `test_planparse`
    (`CorpusTest`) прогоняет каждый `plan-*.md` и требует для него ожидание;
  - `transcript-shapes.jsonl` — формы записей транскрипта Claude Code с заменённым содержимым;
    `test_common` (`ReadTranscriptTest`) прогоняет корпус и его префиксы;
  - `bash-failure-errors.jsonl` — поле `error` упавшего Bash во входе `PostToolUseFailure`;
    `test_debug_watch` прогоняет хук на каждом образце.
- Тесты в git-репозитории (`GitCaptureTest`, `ChangedSinceGitTest`, git-тесты `test_comments`,
  `test_judge_stop`, `test_remind`, `test_common`, `test_hostile_git`) создают временный репозиторий
  и пропускаются без `git`.
- `tests/test_hostile_git.py` передаёт враждебный конфиг явно — `GIT_CONFIG_GLOBAL` на временный файл —
  только вызовам плагина (`snapshot.capture`, `snapshot.changed_since`, `comments.extract`,
  `common.project_root` через `mock.patch.dict(os.environ)`, хукам `remind.py` и `judge_stop.py` через
  `Env.run`); git подготовки репозитория идёт под изолированным конфигом. Настройки `HOSTILE` не должны
  менять результат: каждая — отдельный `subTest` против ожидаемого результата под изолированным
  конфигом, все вместе — отдельный тест и тест хуков. Внешний diff, пейджер и монитор файловой системы —
  скрипт, печатающий строки вида diff и завершающийся с ошибкой. `core.excludesFile` законно меняет
  результат: `ExcludesFileTest` проверяет, что игнорируемый пользователем файл не попадает в изменения.
  Новый вызов git в плагине — его флаги проверяются здесь; новая настройка, которую вызов должен
  отключать, — в `HOSTILE`.
- Тесты хуков запускают скрипт подпроцессом: `tests.helpers.Env` создаёт временные
  `CLAUDE_PLUGIN_ROOT` (свои `philosophy.md` и `rules/` из `helpers.PHILOSOPHY` и
  `helpers.RULES`), `CLAUDE_PLUGIN_DATA`, каталог проекта и транскрипт `Env.transcript` с ответом
  ассистента модели `claude-test-model`; `Env.run` подаёт вход хука в stdin. Вход собирает
  `Env.hook_input(event, **fields)`: `session_id`, `prompt_id`, `cwd` — каталог проекта,
  `transcript_path` — `Env.transcript`. Ответ разбирают `helpers.output` и `helpers.messages`
  (строки `systemMessage`), журнал — `Env.log_lines`.
- Окружение `Env.environ` вычищает `PLANKA_*`, `CLAUDE_PLUGIN_OPTION_*`, `CLAUDE_PROJECT_DIR` и
  `CLAUDE_CONFIG_DIR`, ставит `HOME` во временный каталог и `tests/stub` первым в `PATH`: вместо
  `claude` отвечает заглушка `tests/stub/claude`. `judge_model` не задан — действует `session`, модель
  берётся из `Env.transcript`. Новая переменная окружения, которую читает код, вычищается там же.

## Заглушка судьи

`tests/stub/claude` ведёт себя по `PLANKA_STUB`: `ok`, `deny` (причина из `PLANKA_STUB_REASON`),
`violated` (`violated` из `PLANKA_STUB_VIOLATED`, сырой JSON), `hang` (через `exec sleep`: висит сам
процесс заглушки), `garbage` (не JSON), `nostructured` (JSON без `structured_output`), `unparsed`
(строка с `{`, но не JSON), `notlogged` (`is_error`). При `PLANKA_STUB_RECORD=<файл>` пишет туда
аргументы, stdin, `PLANKA_JUDGE`, `cwd` и PID процесса. Новый вид ответа судьи — новая ветка
заглушки.

## Тесты в том же процессе

Тестовых крючков в рабочем коде нет. Где нужно подменить модуль или платформу, тест зовёт код в
своём процессе с `mock.patch`: порог `snapshot.MAX_FILES` и сбой снимка — `common.run_hook(remind.main)`
(`RemindTest.run_in_process`); сторож судьи вне Linux — `common.run_judge` с `sys.platform` =
`darwin` (`WatchdogTest`); смерть судьи вместе с хуком на Linux — `JudgeDiesWithHookTest`, только на
Linux.

## Что тестами не покрыто

- Тесты хуков берут свои тексты правил (`helpers.PHILOSOPHY`, `helpers.RULES`); настоящие
  `plugin/philosophy.md` и `plugin/rules/*.md` проверяет только `tests/test_contract.py`: разделы и
  модули, которые код берёт по имени, заголовок и строка условия «Читай» каждого модуля, только
  известные метки, пункты «Решения» 7 и «Планы» 9, полнота индекса «Модули», метки языков.
- Живой `claude`: вызов судьи, показ `systemMessage`, хуки `guard_memory` и `debug_watch` в
  настоящей сессии проверяются только вручную.
- Сторож судьи на платформе не Linux: проверен только подменой `sys.platform`.
- Настройки `HOSTILE`, на которые вызовы плагина не реагируют по устройству git (`color.status`,
  `diff.mnemonicPrefix`, `diff.relative`, `diff.ignoreSubmodules`, `core.quotePath`, `core.pager`,
  `status.*` кроме `showUntrackedFiles`, `commit.gpgsign`, `log.showSignature`), не краснеют при снятии
  флага из вызова: тест держит их от будущих вызовов без флагов. `core.fsmonitor` проверен только со
  сбойным хуком; монитор, который отвечает успехом и врёт, обманывает и сам git; встроенный демон не
  запускается.
