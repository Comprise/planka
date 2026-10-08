# Проверки

```bash
make test           # python3 -m unittest discover -s tests -t . -v
make test-hostile   # тот же прогон в локали C и с выводом latin-1, с проверкой изоляции
make validate       # claude plugin validate . и plugin: маркетплейс и плагин
make check          # test, test-hostile и validate
```

CI нет; цели — в `Makefile`. Проверяются два разных свойства:

1. Тесты изолированы от машины. `test-hostile` запускает тот же набор в неудобной локали и кодировке
   (`LC_ALL=C`, `PYTHONIOENCODING=latin-1`); хуки в нём работают в кодировке ascii: `Env.environ`
   ставит им `PYTHONUTF8=0`, иначе Python в локали C сам включает UTF-8 mode. Процесс тестов остаётся
   в UTF-8 mode. Чужие git-настройки — подпись коммитов и игнор `*.py` через `GIT_CONFIG_GLOBAL`,
   `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`, `GIT_CONFIG_PARAMETERS` (его экспортирует
   `git -c`, например при `rebase -x`) и `XDG_CONFIG_HOME/git/ignore` и `attributes` (`* binary`), — и
   `CLAUDE_PROJECT_DIR` на репозиторий, `CLAUDE_CONFIG_DIR=/nonexistent` до кода плагина не доходят:
   `tests/__init__.py` и `Env.environ` их подменяют; их задача — упасть, если изоляция сломана.
2. Плагин работает под чужим git-конфигом пользователя — `tests/test_hostile_git.py`, в обеих целях.

## Устройство тестов

- По файлу `tests/test_<модуль>.py` на модуль `plugin/planka/`; `tests/test_contract.py` — контракт
  с настоящими текстами правил; `tests/test_hostile_git.py` — git-код нескольких модулей под чужим
  git-конфигом.
- Модульные тесты импортируют `planka/<модуль>.py` напрямую через `sys.path`.
- `tests/__init__.py` изолирует процесс тестов от git-настроек и окружения машины: все переменные
  `GIT_*` и `CLAUDE_PROJECT_DIR` сняты; глобальный и системный конфиг и системные атрибуты git не
  читаются (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`, `GIT_ATTR_NOSYSTEM`); `XDG_CONFIG_HOME` — пустой
  временный каталог (`ignore` и `attributes` из `XDG_CONFIG_HOME/git` git читает и при
  `GIT_CONFIG_GLOBAL`); `GIT_CEILING_DIRECTORIES` — временный каталог: внешний репозиторий не находится.
- Входные данные тестов — `tests/fixtures/`, корпуса настоящих форм входа (`plugin/rules/heuristics.md`);
  источник формы назван в каждом файле:
  - `plan-*.md` — планы (волны, формат superpowers, канонический формат `planning.md`); `test_planparse`
    (`CorpusTest`) прогоняет каждый `plan-*.md` и сверяет со структурой из `CORPUS` — волна, номер и
    файлы каждой задачи и конфликты владения; ожидание есть у каждого плана корпуса;
  - `transcript-shapes.jsonl` — формы записей транскрипта Claude Code с заменённым содержимым, в том
    числе `system` (`turn_duration`, `stop_hook_summary`, `local_command`), вложения `hook_*`, ответ
    из одного `thinking` и `tool_result` списком блоков; `test_common` (`ReadTranscriptTest`)
    прогоняет корпус и его префиксы;
  - `bash-failure-errors.jsonl` — поле `error` упавшего Bash во входе `PostToolUseFailure`;
    `test_debug_watch` прогоняет хук на каждом образце.
- Тесты в git-репозитории (`GitCaptureTest`, `ChangedSinceGitTest`, git-тесты `test_comments`,
  `test_judge_stop`, `test_remind`, `test_common`, `test_hostile_git`,
  `MemoryHookTest.test_auto_memory_directory_in_repository`, `…_above_project`, `…_in_home_project`,
  `…_nested_repository`, `…_in_foreign_repository`) создают временный репозиторий
  и пропускаются без `git`.
- `tests/test_hostile_git.py` передаёт враждебный конфиг явно — `GIT_CONFIG_GLOBAL` на временный файл —
  только вызовам плагина (`snapshot.capture`, `snapshot.changed_since`, `comments.extract`,
  `common.project_root` через `mock.patch.dict(os.environ)`, хукам `remind.py` и `judge_stop.py` через
  `Env.run`); git подготовки репозитория идёт под изолированным конфигом. Настройки `HOSTILE` не должны
  менять результат: каждая — отдельный `subTest` против ожидаемого результата под изолированным
  конфигом, все вместе — отдельный тест и тест хуков. Внешний diff, пейджер, монитор файловой системы и
  textconv-драйвер `diff.junk.textconv` — скрипт, печатающий строки вида diff и завершающийся с
  ошибкой; `core.attributesFile` — файл `ATTRIBUTES` (`*.py -diff`, у `a.py` драйвер `junk`). Репозиторий
  сценария держит свой `.gitattributes` (`b.py binary`, `ü.py -diff`), а `old.py` переименован в
  `moved.py` коммитом с дописанной строкой: комментарии `moved.py` — только новые, против старого пути.
  `NO_RENAMES` (`diff.renames=false`) и `INTER_HUNK` — отдельные тесты того же сценария.
  `core.excludesFile` законно меняет
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
- Окружение `Env.environ` вычищает `PLANKA_*`, `CLAUDE_PLUGIN_OPTION_*`, `CLAUDE_PROJECT_DIR`,
  `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_REMOTE_MEMORY_DIR` и `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`, ставит `HOME`
  во временный каталог, `PYTHONUTF8=0` (хук — в кодировке локали,
  как `python3` пользователя без UTF-8 mode) и `tests/stub` первым в `PATH`; `Env.run` обменивается с
  хуком в UTF-8, как Claude Code, при любой локали процесса тестов. Вместо
  `claude` отвечает заглушка `tests/stub/claude`. `judge_model` не задан — действует `session`, модель
  берётся из `Env.transcript`. Новая переменная окружения, которую читает код, вычищается там же.

## Заглушка судьи

`tests/stub/claude` ведёт себя по `PLANKA_STUB`: `ok`, `deny` (причина из `PLANKA_STUB_REASON`),
`violated` (`violated` из `PLANKA_STUB_VIOLATED`, сырой JSON), `hang` (через `exec sleep`: висит сам
процесс заглушки), `garbage` (не JSON), `nostructured` (JSON без `structured_output`), `unparsed`
(строка с `{`, но не JSON), `notlogged` (`is_error`). При `PLANKA_STUB_RECORD=<файл>` пишет туда
аргументы, stdin, `PLANKA_JUDGE`, `cwd` и PID процесса. Ответ с текстом собирает `python3` с
`PYTHONUTF8=1`: аргументы читаются в UTF-8, как у `claude`, в любой локали. Новый вид ответа судьи —
новая ветка заглушки.

## Тесты в том же процессе

Тестовых крючков в рабочем коде нет. Где нужно подменить модуль или платформу, тест зовёт код в
своём процессе с `mock.patch`:

- общий помощник — `helpers.run_in_process(env, main, hook_input, **environ)`: `main` хука через
  `common.run_hook` с окружением `Env.environ` и подменой stdin и stdout, ответ — словарём; подмены
  модулей ставит вызывающий;
- порог `snapshot.MAX_FILES` и сбой снимка — `remind.main` через `RemindTest.run_in_process` (обёртка
  над `helpers.run_in_process`);
- гонка лимита отказов (параллельный вызов исчерпал лимит между проверкой и отказом) —
  `helpers.run_in_process` в `tests/test_judge_tool.py` и `tests/test_judge_stop.py`; счётчик на
  пределе ставит `helpers.fill_budget`;
- `judge_stop.changed_this_turn` с подменой `snapshot` — `DocsFilterTest.in_process`; сроки сверки и
  разбора комментариев, переданные в `snapshot.changed_since` и `comments.extract`, —
  `DocsFilterTest.test_deadlines_passed_to_changed_since_and_extract`;
- `autoMemoryDirectory` из managed-настроек — `guard_memory.is_memory_path` с подменой
  `guard_memory.managed_dir` на временный каталог (`ManagedSettingsTest`): системный каталог тест не пишет;
- `git check-ignore` в `guard_memory._repository_file` под чужим git-конфигом — `HostileMemoryTest` в
  `tests/test_hostile_git.py` (каталог cowork равен проекту: git проекта решает, что файл репозитория,
  что память): результат не меняется ни от одной настройки `HOSTILE`; законное влияние
  `core.excludesFile` — `ExcludesFileTest.test_user_excluded_file_is_memory`; срок вызова —
  `RepositoryFileTest` в `tests/test_guard_memory.py`; ответ 128 (проект не репозиторий) —
  `MemoryHookTest.test_auto_memory_directory_in_project_without_git`;
- сторож судьи вне Linux — `common.run_judge` с `sys.platform` = `darwin` (`WatchdogTest`); смерть
  судьи вместе с хуком на Linux — `JudgeDiesWithHookTest`, только на Linux.

Локаль с кодировкой ascii проверяет `NonUtf8LocaleTest` своим скриптом `_LOCALE_HOOK` подпроцессом
(`LC_ALL=C`, `PYTHONUTF8=0`): судья с русским промптом на Linux и через сторож, корень проекта и
транскрипт по путям с кириллицей. Если кодировка файловой системы там не ascii, тест пропускается. Цель
записи в память и `cwd` с кириллицей в той же локали — `MemoryHookTest.test_non_utf8_locale_cyrillic_paths`
в `tests/test_guard_memory.py`, хуком подпроцессом, с тем же пропуском.

Журнал без содержимого проверяет `helpers.assert_not_logged`: ни одна строка `judge.log` не содержит
переданных текстов (реплики автора, плана, сообщений); блок `<author>` из записи заглушки достаёт
`helpers.author_block`. Помощники, нужные нескольким файлам тестов, лежат в `tests/helpers.py`: тест не
импортирует другой тест.

## Что тестами не покрыто

- Тесты хуков берут свои тексты правил (`helpers.PHILOSOPHY`, `helpers.RULES`); настоящие
  `plugin/philosophy.md` и `plugin/rules/*.md` проверяет только `tests/test_contract.py`: разделы и
  модули, которые код берёт по имени (ручные `SECTIONS`, `MODULES` и имена, выведенные из кода, —
  `test_names_taken_by_code_exist`), заголовок и строка условия «Читай» каждого модуля, только
  известные метки, пункты «Решения» 7 и «Планы» 9, полнота индекса «Модули», метки языков. Там же
  `TimeoutsTest`: сроки внутри хуков против таймаутов `hooks/hooks.json`
  (`context/architecture.md`, «Сроки»), в сумме — `guard_memory.CHECK_IGNORE_TIMEOUT`.
- Живой `claude`: вызов судьи, показ `systemMessage`, хуки `guard_memory` и `debug_watch` в
  настоящей сессии проверяются только вручную.
- Сторож судьи на платформе не Linux: проверен только подменой `sys.platform`.
- Вложения с `attachment.planFilePath` в корпусе `transcript-shapes.jsonl` нет: тесты собирают его
  сами (`type: plan_mode`), с настоящего транскрипта форма не снята.
- Набор целиком без UTF-8 mode не гоняется: в `test-hostile` в кодировке ascii работают только хуки
  подпроцессом (`Env.environ`) и `NonUtf8LocaleTest`; модульные тесты в процессе тестов идут в UTF-8 mode.
- Настройки `HOSTILE`, на которые вызовы плагина не реагируют по устройству git (`color.status`,
  `diff.mnemonicPrefix`, `diff.relative`, `diff.ignoreSubmodules`, `core.quotePath`, `core.pager`,
  `status.*` кроме `showUntrackedFiles`, `commit.gpgsign`, `log.showSignature`), не краснеют при снятии
  флага из вызова: тест держит их от будущих вызовов без флагов. `core.attributesFile` и
  `diff.junk.textconv` по отдельности тоже не краснеют при снятии `--no-textconv`: драйвер действует
  только вместе с файлом атрибутов, снятие ловят тесты всех настроек вместе. `core.fsmonitor` проверен
  только со
  сбойным хуком; монитор, который отвечает успехом и врёт, обманывает и сам git; встроенный демон не
  запускается.
