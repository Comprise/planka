# Архитектура

planka — плагин Claude Code уровня пользователя: пять скриптов хуков из `hooks/hooks.json` подмешивают
правила из `philosophy.md` и `rules/*.md` в контекст агента и отклоняют его действия через
вложенного судью-модель или детерминированные проверки. Пользовательское описание поведения —
`README.md`, разделы «Как это работает» и «Известные ограничения».

Плагин — каталог `plugin/`: маркетплейс отдаёт пользователям только его (`"source": "./plugin"` в
`.claude-plugin/marketplace.json`). Пути ниже, кроме `.claude-plugin/marketplace.json`, — от
`plugin/`, он же `CLAUDE_PLUGIN_ROOT` хуков.

## Компоненты

| Файл | Роль |
| --- | --- |
| `hooks/hooks.json` | регистрация хуков: `UserPromptSubmit` → `remind.py`; `PreToolUse` на `^(AskUserQuestion\|ExitPlanMode\|Bash\|Write\|Edit\|MultiEdit)$` → `judge_tool.py`; `PreToolUse` на файловые инструменты записи и MCP-инструменты с `memor` или `remember` в имени в любом регистре (регулярное выражение — в `hooks/hooks.json`) → `guard_memory.py`; `Stop` → `judge_stop.py`; `PostToolUse` и `PostToolUseFailure` на `Bash` → `debug_watch.py` и `judge_tool.py` |
| `planka/remind.py` | `philosophy.md` целиком как `additionalContext`; снимок дерева (`take_snapshot`); строка `NO_DOCS_LINE` об отсутствии `CLAUDE.md` |
| `planka/judge_tool.py` | судья вопроса (`judge_question`), плана (`judge_plan`), отказ на добавление пакета (`judge_bash`, причина `DEP_REASON`) и снимок манифестов перед командой (`snapshot_manifests`); отказ правке манифеста (`judge_manifest_edit`, `MANIFEST_REASON`); после команды `Bash` — блок на новые имена в манифестах (`check_command_manifests`, `COMMAND_REASON`) |
| `planka/guard_memory.py` | судья записи в постоянную память: цель — `is_memory_path`, `is_memory_mcp`; содержимое — `render_content` |
| `planka/judge_stop.py` | фильтры «варианты» (`looks_like_options`), «готово» (`claims_done`) по последнему сообщению, «документация» (`docs_check`) по изменениям со снимка; один вызов судьи на сообщения реплики (`turn_messages`, до `prompts.MAX_TURN_CHARS`) |
| `planka/debug_watch.py` | счётчик неудач подряд одной команды `Bash` (`update`); с `REPEAT_THRESHOLD`-й неудачи — модуль `debugging.md` контекстом |
| `planka/common.py` | барьер, чтение входа, тексты правил и рубрика, транскрипт (`read_transcript`), модель и запуск судьи (`judge_model`, `run_judge`), лимит отказов, журнал, классы путей, формат ответа (`run_hook`) |
| `planka/prompts.py` | системный промпт, схема ответа `JUDGE_SCHEMA`, вопросы судье по видам проверки, сборка содержимого |
| `planka/planparse.py` | разбор плана на волны и задачи, владение файлами (`shared_files`) |
| `planka/manifests.py` | разбор манифестов: вид по имени (`kind`), имена внешних зависимостей (`names`), они же с транзитивными (`known_names`), имя пакета манифеста (`own_name`) |
| `planka/manifest_watch.py` | текст манифеста после правки файловым инструментом (`edit_texts`, `check_edit`), новые имена (`fresh_names`, `edit_names`), имена версии HEAD (`head_names`) и других манифестов проекта (`project_names`), снимок манифестов проекта и сравнение после команды (`take`, `compare`, `store`, `pop`), имена ref до начала сессии, откуда команда git возвращает файлы (`command_refs`, `restored_names`; начало сессии — `mark_start`, `session_start`), файлы вывода генераторов requirements (`generated_requirements`) |
| `planka/depcheck.py` | разбор команды Bash: добавляет ли она пакет (`dependency_add`), маркер `DEP_OK_MARKER`, heredoc (`heredocs`) |
| `planka/snapshot.py` | снимок дерева (`capture`, `store`, `load`) и изменения с него (`changed_since`) в режимах git и walk; порог `MAX_FILES` грязных путей в git |
| `planka/comments.py` | строки комментариев изменённых файлов для судьи документации (`extract`) |
| `philosophy.md` | ядро правил; индекс «Модули» в конце |
| `rules/*.md` | модули правил, по файлу на область |
| `.claude-plugin/plugin.json` | манифест и `userConfig`: `judge_model`, `comment_lang`, `doc_lang` |
| `.claude-plugin/marketplace.json` | маркетплейс `planka` в корне репозитория, источник плагина — `./plugin` |

## Контракт кода с текстами правил

Код ищет тексты правил по именам; переименование ломает хук без ошибки теста хука — хук пропускает
проверку с предупреждением. Настоящие тексты сверяет `tests/test_contract.py`.

- Разделы ядра берутся по заголовку `## <имя>` (`common.philosophy_sections`, `common.rubric`): `Решения` —
  рубрика вопроса, плана и фильтра «варианты»; `Планы` — рубрика плана; `Границы` — рубрика записи в память,
  её же называют `judge_tool.DEP_REASON`, `MANIFEST_REASON` и `COMMAND_REASON`. Пункт 7 «Решений» называют
  вопросы `prompts._QUESTION_CHECKS`.
- Модули берутся по имени файла (`common.rule_texts`, `common.rubric`): `planning`, `subagents`,
  `refactoring`, `design-patterns`, `heuristics` — план (`judge_tool.judge_plan`); `verification` — фильтр «готово»;
  `docs`, `comments`, `design-patterns`, `refactoring` — фильтр «документация» (`judge_stop.main`);
  `memory` — запись в память (`guard_memory.main`); `debugging` — `debug_watch.MODULE`.
  `rules/dependencies.md` называют причины отказа `judge_tool.DEP_REASON`, `judge_tool.MANIFEST_REASON`
  и `judge_tool.COMMAND_REASON`.
- Метки `{RULES}`, `{COMMENT_LANG}`, `{DOC_LANG}` заменяет `common.substitute` при каждом чтении:
  путь к `rules/` плагина и значения настроек; незаданный язык — `DEFAULT_LANG` (`ru`). Ссылка на
  модуль из ядра и модулей пишется как `{RULES}/<имя>.md`, строка `remind.NO_DOCS_LINE` — так же;
  `debug_watch.LINE` и `judge_tool.DEP_REASON` подставляют `common.rules_dir()`: агент получает
  абсолютный путь к правилам плагина, а `rules/` проекта с ним не путается.
- Имена, которые код берёт, `tests/test_contract.py` выводит из кода сам (`_code_names`, через `ast`):
  литералы в вызовах `common.rubric`, `common.philosophy_sections`, `common.rule_texts`, константа
  `MODULE` и всё, что кладут в список, переданный в `rubric` (литерал, `append`, `+=`).
  `test_names_taken_by_code_exist` требует каждое такое имя в `philosophy.md` и `rules/`. Ручные
  `SECTIONS` и `MODULES` — обратное направление: имена, которые код обязан брать; тест требует, чтобы
  сборщик нашёл их в коде, кроме `dependencies` — его код называет только текстом причин отказа.

## Ответ хука

Все хуки запускаются через `common.run_hook`: stdout — один JSON (ответ из `common.emit` плюс
`systemMessage` из `common.warn`) или пусто; stderr не пишется; исключение превращается в
предупреждение «внутренняя ошибка». Ввод и вывод — байтами через `sys.stdin.buffer` и
`sys.stdout.buffer` в UTF-8, независимо от локали, в том числе при кодировке файловой системы ascii
(локаль C без UTF-8 mode); JSON — через `common.dumps`. Путь из входа или транскрипта приводится к
кодировке файловой системы `common.input_path`: Claude Code пишет пути байтами UTF-8, и в локали не
UTF-8 строка с кириллицей иначе не кодируется для `open` и `subprocess`. Через неё идут `cwd` в
`common.project_root`, `transcript_path` и `planFilePath` в `common.read_transcript`, путь цели и `cwd`
в `guard_memory.target_path` и `guard_memory._input_cwd`. Причина отказа
начинается с `planka: `. Отказ `PreToolUse` — `common.deny_output`, отказ `Stop` —
`common.block_output` (им же `judge_tool.check_command_manifests` отвечает на `PostToolUse` и
`PostToolUseFailure`: команда уже выполнена, `decision: block` отдаёт причину агенту), контекст
`UserPromptSubmit` — `common.context_output`; `debug_watch` отвечает
`hookSpecificOutput` с `additionalContext` своего события. Ответ запоминается до записи журнала:
сбой записи не отменяет отказ.

## Судья

`common.run_judge` запускает `claude` с `JUDGE_FLAGS` (`-p`, `--setting-sources ""`,
`--strict-mcp-config`, `--no-session-persistence`, `--output-format json`, `--tools ""`), схемой
`JUDGE_SCHEMA`, системным промптом и моделью, с `cwd` в каталоге данных и окружением
`PLANKA_JUDGE=1`. Каждый хук первым делом проверяет `common.barrier_active` и внутри судьи не
работает.

Результат — `common.Verdict`. Любая ошибка судьи — `Verdict` с `error`: короткое описание без текста
модели, оно идёт в журнал; фрагмент ответа (до `MAX_DETAIL` = 200 символов) — в `detail`, только для
пользователя. Предупреждение о пропуске собирает `common.skip_message`. `reason` обрезается до
`MAX_REASON`. Аргументы после `claude` и stdin судьи — байты `common._utf8` (claude читает их в
UTF-8 при любой локали; суррогат U+DC80..U+DCFF — исходный байт пути), ответ декодируется из UTF-8
с заменой; `ValueError` при запуске (нулевой байт в аргументе) — пропуск «claude не запущен».

Модель — `common.judge_model(data, transcript)`: `CLAUDE_PLUGIN_OPTION_JUDGE_MODEL`, по умолчанию
`SESSION_MODEL` (`session`) — тогда модель из транскрипта. Не нашлась — `None`: судья без `--model`,
с предупреждением раз на сессию (`warn_once`, ключ `session-model`). Модель сессии хуки иначе не
получают: поле `model` во входе есть только у `SessionStart` (`context/deferred/session-model-from-transcript.md`).

Транскрипт (`transcript_path` входа) читает `common.read_transcript` за один проход и отдаёт
`common.Transcript`: `model` — `message.model` последнего ответа ассистента основной ветки, кроме
моделей вида `<synthetic>`; `plan_file` — последний `attachment.planFilePath`; `turn_messages` —
тексты ответов ассистента после последней реплики автора; `author_turn` — текст этой реплики и
сообщений человека посреди хода; `author_answers` — ответы на `AskUserQuestion` после неё;
`message_before_author` — последнее сообщение ассистента до неё. Записи `isSidechain` пропускаются.

Реплика автора (`common._is_author_turn`) — запись `user` основной ветки без `isMeta`, которую написал
человек: с полем `origin` — только `origin.kind == "human"` (`task-notification` — уведомление о
фоновой задаче, `peer` — сообщение другого агента, оно же `isMeta`). Без `origin` в транскрипте, где
`origin` уже встречался, — локальная команда (`<command-name>`, `<local-command-stdout>`), не реплика;
в транскрипте старого формата, без `origin`, — запись с текстом без `tool_result`. Сообщение человека
посреди хода — вложение `attachment.type == "queued_command"` с `origin.kind == "human"`: его текст
дописывается к `author_turn`, ход не сбрасывается. Формы записей Claude Code 2.1.293 без текста —
корпус `tests/fixtures/transcript-shapes.jsonl`. Нет файла, путь с нулевым байтом или ошибка чтения —
пустой `Transcript`.
Хук читает транскрипт один раз и передаёт его в `judge_model`.

Проверяемое содержимое идёт судье блоком `<content>…</content>` (`prompts._wrap`). Судьи вопроса,
плана и `Stop` получают перед ним блок `<author>` — `prompts.author_context(author_turn,
author_answers)`: реплика автора текущего хода и его ответы на `AskUserQuestion` после неё, каждое
поле до `prompts.MAX_AUTHOR_FIELD` (8000) символов; передают его `judge_tool._author` и
`judge_stop.main`. Пояснение `prompts._AUTHOR_NOTE`: явная просьба автора побеждает рубрику,
проверяется только `<content>`, `<author>` — тоже данные. Зачем: без реплики автора судья не отличает
решение агента от решения, которое автор принял сам, и отклоняет сделанное по его явной просьбе;
судья записи в память те же поля получает внутри `<content>` (`guard_memory.render_content`), потому
что согласие автора там и есть проверяемое. Блок `<author>` отдельный, а не часть `<content>`:
журнал хранит длину и SHA-256 только проверяемого содержимого, текст автора в `judge.log` не попадает
ни целиком, ни в хэше. Закрывающие теги `</content>` и `</author>` внутри данных — в любом регистре и
с пробелами (`prompts._CLOSING_TAG`) — экранирует `prompts._escape`: каждый блок закрывает
единственный свой тег промпта.
Вопросы по видам: `prompts.question_prompt`, `prompts.plan_prompt`, `prompts.memory_prompt`,
`prompts.stop_prompt` (вопросы совпавших фильтров в порядке options, done, docs; сообщения реплики
склеивает `prompts.turn_content` с пояснением `prompts.TURN_LABEL`, не больше `prompts.MAX_TURN_CHARS`
символов: ранние сообщения сверх предела опущены с пометкой их числа), содержимое фильтра
«документация» — `prompts.render_docs_content`.

Судья — группа процессов (`start_new_session`) со сторожем `common._WATCHDOG` (`python3 -I -c`) во
главе (`common._start_judge`): сторож запускает `claude` потомком и раз в `WATCHDOG_POLL` (0,5 с)
сверяет своего родителя с PID хука; хук умер — `SIGKILL` всей группе, `claude` и его потомкам. Таймаут
`JUDGE_TIMEOUT` (60 с) убивает группу из хука (`common._kill_group`, ожидание до `common.KILL_WAIT`,
5 с); сторож, убитый отдельно от группы, оставляет её этому таймауту. Сторож — на всех платформах:
`PR_SET_PDEATHSIG` Linux действует только на сам `claude`, не на его потомков. Цена — лишний запуск
интерпретатора на вызов судьи, порядка 20 мс. Путь к `claude` для сторожа ищется по `PATH` окружения
судьи; нет или файл не исполняемый — `FileNotFoundError`, пропуск «claude не найден в PATH».

## Сроки

Таймауты хуков в `hooks/hooks.json`: `UserPromptSubmit` 10 с, `PreToolUse` 90 с у обоих хуков, `Stop` 120 с,
`PostToolUse` и `PostToolUseFailure` — 10 с у `debug_watch` и 30 с у `judge_tool`. У `Stop` до судьи ещё
`git rev-parse` (до `common.GIT_ROOT_TIMEOUT`, 5 с), сверка со снимком со сроком `judge_stop.SNAPSHOT_BUDGET`
(20 с) и извлечение комментариев со сроком `judge_stop.COMMENTS_BUDGET` (20 с) — вместе с судьёй (до 65 с) не
больше 110 с. У `UserPromptSubmit` снимок ограничен сроком `remind.SNAPSHOT_BUDGET` (7 с от старта хука); не
уложился — `TimeoutError`, снимок пропускается с предупреждением раз на сессию (`common.warn_once`, ключ
`snapshot`, общий с `TooManyFiles`), напоминание выдаётся. Один вызов git в
`snapshot` и `comments` — не дольше `GIT_TIMEOUT` (10 с) и остатка срока. У хуков `PreToolUse` с судьёй срок —
`JUDGE_TIMEOUT` и `KILL_WAIT` (65 с) против 90 с; у `guard_memory` до судьи ещё `git check-ignore`
(`guard_memory._repository_file`, срок `guard_memory.CHECK_IGNORE_TIMEOUT`, 5 с) — для цели под
`autoMemoryDirectory` или cowork, который равен проекту или содержит его; вместе с судьёй 70 с против 90 с. У
`judge_tool` на `Bash` снимок манифестов — `git rev-parse` корня (`common.GIT_ROOT_TIMEOUT`) и срок
`manifest_watch.SNAPSHOT_BUDGET` (5 с) на `git ls-files` и разбор, а у команды с ref (`command_refs`) — и на
`git cat-file --batch` и `git ls-tree` ref до начала сессии (`restored_names`; срок вышел — имён ref нет), у
правки манифеста — один `git cat-file --batch` версий `_REPO_REFS` (`manifest_watch.HEAD_TIMEOUT`, 5 с) и
после него корень проекта и обход манифестов проекта (`judge_tool._project_names`, ещё `SNAPSHOT_BUDGET`):
`HEAD_TIMEOUT` + `GIT_ROOT_TIMEOUT` + `SNAPSHOT_BUDGET`; на
`PostToolUse` сравнение со снимком — срок `manifest_watch.CHECK_BUDGET` (5 с) против 30 с, в него входят и `git
cat-file --batch` изменённых манифестов. Вышел срок — пропуск с предупреждением.

Эти суммы проверяет `tests/test_contract.py`, `TimeoutsTest`, против таймаутов из `hooks/hooks.json`
константами модулей (`common.JUDGE_TIMEOUT`, `common.KILL_WAIT`, `common.GIT_ROOT_TIMEOUT`,
`judge_stop.SNAPSHOT_BUDGET`, `judge_stop.COMMENTS_BUDGET`, `remind.SNAPSHOT_BUDGET`,
`guard_memory.CHECK_IGNORE_TIMEOUT`, `manifest_watch.SNAPSHOT_BUDGET`, `CHECK_BUDGET`, `HEAD_TIMEOUT`); там же
`test_post_tool_use_has_no_judge`: на `PostToolUse` ни `debug_watch`, ни `manifest_watch`, ни
`judge_tool.check_command_manifests` судью не зовут. Что код передаёт именно эти константы, проверяют
`KillGroupTest` и `ProjectRootTest` в `tests/test_common.py` и
`DocsFilterTest.test_deadlines_passed_to_changed_since_and_extract` в `tests/test_judge_stop.py`; что
`guard_memory._repository_file` передаёт `CHECK_IGNORE_TIMEOUT` и `git -C <проект>` — `RepositoryFileTest` в
`tests/test_guard_memory.py`; что `judge_tool` передаёт `SNAPSHOT_BUDGET`, `CHECK_BUDGET` и `HEAD_TIMEOUT` —
`ManifestDeadlineTest` в `tests/test_judge_tool.py`.

## Лимит отказов

`MAX_DENIES` = 2 на ключ `<prompt_id>:<hook>`, хук — `question`, `plan`, `memory`, `stop`. До вызова
судьи `common.deny_budget_left` проверяет счётчик, не меняя его (сбой чтения — проверка идёт); лимит
исчерпан — пропуск с предупреждением и записью `budget`. При отказе `common.deny_budget_exhausted`
увеличивает счётчик; если к этому моменту он уже на пределе (параллельный вызов), отказ заменяется
пропуском. Сбой записи счётчика — исключение, `run_hook` выдаёт «внутреннюю ошибку» без отказа: без
счётчика нечем остановить цикл отказов. Отказ `judge_bash`, отказ правке манифеста
(`judge_manifest_edit`) и блок после команды (`check_command_manifests`) лимитом не ограничены: проверка
детерминированная, без модели (`ManifestEditTest.test_not_limited_by_budget`).

## Разбор плана

`planparse.parse_plan` разбирает план за время, линейное по длине строки: пункт «целиком из путей» —
`planparse._is_whole_path`, текст заголовка ATX без закрывающих `#` — `planparse._atx_text`, пометка в
скобках в конце строки файлов — `planparse._trailing_note`; регулярные выражения в них — без
вложенных повторов. Регулярные выражения с вложенными повторами на тех же местах уходят в перебор
с возвратами: в версии 0.3.0 пункт вида `- a.a.a.…` в 40 000 символов разбирался 29 с, `- a/a/…` в
16 000 — 4,5 с, заголовок `# a` с 30 000 пробелов — 6,6 с (замер), при таймауте `PreToolUse` 90 с и
плане, который пишет агент. Линейность держит `RobustnessTest.test_adversarial_input_is_linear`
(каждый вход — меньше 1 с). Формы, которые разбор понимает, — `README.md`, «Известные ограничения».
Строку файлов `planparse._split` делит на пути и остаток после серии путей в кавычках (связки серии —
`planparse._CONN`); повторная строка файлов задачи с остатком-предложением
(`planparse._continues_description`) — описание, её пути владением не считаются.

## Запись в память

`guard_memory` срабатывает на файловые инструменты (`FILE_TOOLS`) и MCP-инструменты. Каталог
настроек — `guard_memory.config_dir` (`CLAUDE_CONFIG_DIR`, без него `~/.claude`). Путь цели —
`guard_memory.target_path` (`common.input_path`, `~` раскрыта, относительный — от `cwd` входа
`guard_memory._input_cwd`, ссылки не разрешены; путь с NUL — `None`, такой файл инструмент не откроет);
память — `guard_memory.is_memory_path(path, project)`, `project` — `guard_memory._project_dir`
(`CLAUDE_PROJECT_DIR`, без неё `cwd` входа, без него текущий каталог): `CLAUDE.md` и `rules/**.md`
каталога настроек, `projects/<проект>/memory/**` и `projects/<проект>/agent-memory-local/**`, память
субагентов — `agent-memory/` каталога настроек (`memory: user`) и `.claude/agent-memory-local/` проекта
(`memory: local`), — при `CLAUDE_CODE_REMOTE_MEMORY_DIR` (`guard_memory._remote_memory_dir`, облачная
сессия) ещё его `agent-memory/` и `projects/`, — и каждое абсолютное значение `autoMemoryDirectory`
(`guard_memory._auto_memory_overrides`) и `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`
(`guard_memory._cowork_memory_override`: только абсолютный путь, `~` не раскрывается, как у Claude
Code). Значение `autoMemoryDirectory` ищется во всех файлах
`guard_memory._settings_files`: `managed-settings.json` и `managed-settings.d/*.json` каталога
`guard_memory.managed_dir`, `.claude/settings.local.json` и `.claude/settings.json` проекта,
`settings.json` каталога настроек. Claude Code берёт одно значение по приоритету источников, хук —
объединение всех: настройки `--settings` и серверные managed-настройки хуку не видны, порядок
приоритета им не воспроизвести, а лишнее место памяти стоит только лишнего вызова судьи (дешёвая
ошибка ниже). Относительное значение пропускается. `.claude/agent-memory/` проекта (`memory: project`)
памятью не считается: это отслеживаемый файл репозитория (`MemoryHookTest.test_agent_memory`). По той
же причине под каталогом `autoMemoryDirectory` или cowork, который равен проекту или содержит его
(`guard_memory.is_memory_path`: каждый совпавший каталог содержит проект; сравниваются все формы
`_forms` каталога и проекта), файл репозитория проекта — не
память (`guard_memory._repository_file`, спрашивается только для пути внутри проекта). Под каталогом
строго внутри проекта (`test_auto_memory_directory_in_repository`, `test_auto_memory_directory_in_home_project`)
или вне него (`test_auto_memory_directory_in_foreign_repository`) git не спрашивается, запись судится
всегда: автор назначил его местом памяти. Родитель проекта, он же корень репозитория, — первый случай
(`test_auto_memory_directory_above_project`; путь вне проекта судится). Путь во вложенном репозитории
между путём и проектом (`guard_memory._nested_repository`: каталог с `.git`) — не файл рабочего дерева
проекта, память даже при ответе 1 (`test_auto_memory_directory_nested_repository`). Вопрос git проекта:
`git -C <ближайший существующий каталог проекта> check-ignore -q` отвечает 1 —
путь в рабочем дереве и не исключён `.gitignore`, `info/exclude`, `core.excludesFile` (отслеживаемый
файл исключённым не считается). Иначе значение из `.claude/settings.json` проекта, равное самому проекту,
отдавало бы судье памяти каждую запись в проекте. Код 0 (исключён), 128 (не репозиторий,
`test_auto_memory_directory_in_project_without_git`), нет `git`, ошибка или таймаут — память (код 1 —
файл репозитория): лишний вызов судьи дешевле пропуска. Места каталога настроек и
`CLAUDE_CODE_REMOTE_MEMORY_DIR` git не проверяются.
Путь цели и места памяти (`_memory_places`) сравниваются в двух формах — как написаны и через `realpath`
(`_forms`), а `memory/` проекта, если ссылка она или `projects/<проект>`, — и разрешённой целью:
каталог настроек под dotfiles судится и при записи через ссылку, и при записи прямо в её цель. Запись
через ссылку вне каталога настроек ловит `realpath` пути цели. Ссылка на файл внутри
`rules/` или `memory/` ловится только записью через неё.
MCP — `guard_memory.is_memory_mcp`: слово из `WRITE_VERBS` в имени инструмента при слове из
`MEMORY_WORDS` в имени сервера; иначе `guard_memory._writes_memory` — в имени инструмента глагол записи
перед словом памяти не дальше чем через одно слово или сразу после него (слова по порядку —
`guard_memory._words`). Корпус настоящих и ложных имён — `MemoryMcpNameTest.CORPUS` в
`tests/test_guard_memory.py`. Дешёвая ошибка — ложное срабатывание: лишний вызов судьи (не больше
`common.MAX_DENIES` отказов на реплику) дешевле записи в память без согласия автора; поэтому
`set_memory_limit` и `clear_memory_cache` судятся как запись. Matcher хука в `hooks/hooks.json`
пропускает `mcp__` с `memor` или `remember` в любом регистре — надмножество `MEMORY_WORDS`.
Содержимое судьи — `guard_memory.render_content` из полей `Transcript` и текста записи
(`guard_memory.write_text`): реплика автора и его ответы — тем же текстом `prompts.author_context`,
что блок `<author>` других судей (каждое поле до `prompts.MAX_AUTHOR_FIELD`), сообщения агента и текст
записи — до `guard_memory.MAX_FIELD` символов (`guard_memory._clip`).

## Повторная неудача команды

`debug_watch` считает по ключу `debug_watch.command_key(command, agent_id)` (SHA-256 команды с
нормализованными пробелами; у субагента — `agent_id` входа, `\0` и команда, у основного агента — одна
команда) в `state/<session>.debug.json`: `counts` — неудачи подряд по ключам, `shown` —
`{agent_id: prompt_id}`, реплика, в которой модуль уже показан этому агенту (у основного ключ `""`).
Субагенты и основной агент считают и получают модуль порознь: неудача субагента не делает «второй раз»
у другого. `debug_watch.update` пишет файл заново только из `counts` и `shown`: поля прежних форматов
(`shown_prompt`) не читаются и при записи не переносятся.

Неудача — событие `PostToolUseFailure` без `is_interrupt`, кроме кода 1 команды-ответа; `PostToolUse`
той же команды и код 1 команды-ответа удаляют её ключ.
Код выхода — первая строка `error` вида `Exit code N` (`debug_watch.exit_code`; документация хуков
велит опираться только на неё, остальное — вывод без стабильного формата). Команда-ответ
(`debug_watch.code1_is_answer`) — последняя команда строки (`_segments`: комментарий от `#` в начале слова, в
том числе после `;`, `|`; разделители вне кавычек `|`,
`|&`, `||`, `&&`, `;`, перевод строки; внутри `[[ … ]]` разделителей нет; тело heredoc — открытие
`debug_watch.HEREDOC`, `<<` или `<<-` со словом, возможно в кавычках, кроме `<<<` — отбрасывает
`_skip_heredocs` до строки-терминатора; внутри арифметики `$((…))` и `((…))` в начале слова `<<` —
сдвиг, не heredoc: счётчик скобок `arith` в том же посимвольном проходе), а если она после `&&` и из `PASS_THROUGH` без
перенаправлений (`<`, `>` вне кавычек и экранирования, `UNQUOTED`) — команда перед ней
(`_last_pipeline_command`); присваивание одной
подстановки `x=$(…)` (`SUBSTITUTION_ASSIGNMENT`) — по команде подстановки. Затем — после присваиваний и
обёрток `WRAPPERS`, из
`CODE1_ANSWERS`, git-подкоманда из `GIT_CODE1_ANSWERS` с нужным флагом, `command -v`/`-V` или `!`;
неразборная строка — не ответ. Ложное срабатывание здесь дороже пропуска: модуль только
подмешивается, агент и без него видит ошибку, а модуль «упала второй раз подряд» на ответе
«не найдено» засоряет контекст и учит агента пропускать модуль. Сомнение решается в пользу ответа
только для команд из списка, остальное — неудача. Сам Claude Code код 1 у `grep`, `rg`,
`git grep`, `git diff --exit-code`, `test`, `[`, `diff`, `find` без обёрток неудачей не отдаёт
(проверено вручную на 2.1.293: результат — вывод, не ошибка); `which`, `command -v`, `type`,
`pgrep`, `pkill`, `cmp`, `git merge-base --is-ancestor` и `grep` под `timeout` или `VAR=…` —
отдаёт. Корпус форм `error` — `tests/fixtures/bash-failure-errors.jsonl`. Отвергнуто: хэш вывода
ошибки (у `grep` без совпадений вывод пуст — хэш одинаков; у тестов вывод меняется временем и
путями — настоящий повтор не ловится) и сброс на новой реплике (пропускает главный случай: автор
вернулся с «всё ещё падает»). С `REPEAT_THRESHOLD` неудач модуль `debugging`
подмешивается не чаще раза за `prompt_id` каждому агенту; нет модуля — предупреждение, отметка о
показе не ставится.

Открытие heredoc `_segments` ищет своим регулярным выражением `HEREDOC` прямо в посимвольном проходе, а не
`depcheck.heredocs`. Арифметика, не закрытая до конца команды (`$((1<<2)` без второй скобки), счётчик
скобок не обнуляет: каждый `<<` после неё — сдвиг, и тело heredoc читается командами.

## Манифесты

Разбор — `manifests.py`, без состояния и ввода-вывода. `manifests.kind(path)` — вид манифеста по имени
файла; `manifests.names(kind, text)` — имена внешних зависимостей, `None` — текст не разобран (JSON —
`json`, TOML — `tomllib`, отсюда Python 3.11+; requirements, `go.mod`, `Gemfile` разбираются построчно и
`None` не дают). Нормализация: PyPI — PEP 503 (`manifests._pep503`), Composer — нижний регистр без
платформенных пакетов (`_COMPOSER_PLATFORM`), crates.io — `_crate`; npm, Go, RubyGems — как записаны.
Транзитивные в `names` не входят: строки `// indirect` в `go.mod` (`_GO_INDIRECT`) и весь сгенерированный
файл требований (`_generated`: заголовок `_GENERATED_HEADER` в начальном блоке комментариев или аннотация
`_VIA` где угодно). `build-system.requires` в `pyproject` без стандартных бэкендов `_BUILD_BACKENDS`
(руководство PyPA «Choosing a build backend»); прочее в `requires` — зависимость. `manifests.known_names(kind, text)`
— старая сторона сравнения: `names` вместе с
транзитивными (`_KNOWN`); ставшая прямой транзитивная зависимость и пакет, уже перечисленный в
сгенерированном файле, не новые. `manifests.own_name(kind, text)` — имя пакета самого манифеста (`_OWN`:
`name`, `project.name`, `tool.poetry.name`, `package.name`, `module`; у requirements и Gemfile `None`): оно
не внешнее. Местные источники `names` отбрасывает: `[tool.uv.sources]` с `workspace` или `path` (список —
если местный хоть один), `gem` с `path:`/`:path =>` и гемы блока `path … do … end` (`_GEM_PATH`,
`_GEM_PATH_BLOCK`), `replace x => ./…` в `go.mod` (`_GO_LOCAL_PATH`). Корпус настоящих манифестов —
`tests/fixtures/manifests/`, ожидания — `CORPUS` в `tests/test_manifests.py`.

Проверка — `judge_tool` и `manifest_watch`, без модели и без лимита отказов:

- Правка файловым инструментом — `judge_tool.judge_manifest_edit` на `EDIT_TOOLS` (`Write`, `Edit`,
  `MultiEdit`). Путь — `guard_memory.target_path`; вид — `manifest_watch.watched_kind`: путь с каталогом из
  `FOREIGN_DIRS` не манифест, каталоги ищутся в пути от проекта (`CLAUDE_PROJECT_DIR`, без неё `cwd`
  входа) — проект сам может лежать под `fixtures/` (`ManifestProjectUnderFixturesTest`).
  `manifest_watch.edit_texts` повторяет инструмент: `Write` — `content`, `Edit` и `MultiEdit` — замены
  `old_string` по очереди с `replace_all`, текст файла для них с CRLF, приведёнными к LF (как Claude Code);
  не найден или неоднозначен — `None`, хук молчит: инструмент откажет сам. Единый путь сравнения —
  `manifest_watch.fresh_names(old, new, head, project)` (для правки его зовёт `edit_names`): новые имена
  против текста до правки, а если они есть или старый текст не разобран, — ещё против версий `_REPO_REFS`
  (`head_names`: объединение `known_names` версий HEAD, MERGE_HEAD, CHERRY_PICK_HEAD, REBASE_HEAD,
  REVERT_HEAD и REVERT_HEAD^ одним `git -C <каталог файла> cat-file --batch` — байты блобов без textconv
  и фильтров; имя файла с переводом строки не ложится в построчный ввод — тогда только HEAD через
  `cat-file blob HEAD:./<имя>`). Конфликт незавершённых merge, pull, cherry-pick, rebase, revert лежит в
  файле, но не в HEAD — версии источника его покрывают; `git merge --squash` `MERGE_HEAD` не пишет,
  `git stash pop`, `git apply`, `git checkout <ref> -- <манифест>` тоже — их источник в `_REPO_REFS` не попадает
  (после команды `Bash` ref сверяет `restored_names`, ниже). Что осталось,
  сверяется с именами других манифестов того же реестра и пакетами самого проекта
  (`manifest_watch.project_names`, зовётся лениво через `judge_tool._project_names`, в пределах
  `SNAPSHOT_BUDGET`). Реестр вида — `manifests.registry` (`_REGISTRY`): package.json — npm, composer.json —
  packagist, pyproject и requirements — pypi, cargo — crates.io, gomod — go, gemfile — rubygems; сравнение
  по реестру, не по виду: имя из `requirements.txt` не новое в `pyproject.toml`. Старый не разобран и версии в
  репозитории нет, новый не разобран, файл больше
  `MAX_MANIFEST_BYTES`, манифесты проекта не перечислить или их больше `MAX_MANIFESTS` (сообщение «не
  сравнён с другими манифестами проекта») — `manifest_watch.Unavailable`: предупреждение
  и `skipped` в журнал. Отказ — `deny_output(MANIFEST_REASON)`, в журнал `deny-dep` хука `MANIFEST_HOOK`
  (`manifest`) с `tool`, `added` и длиной и SHA-256 входа инструмента.
- Команда `Bash`: `judge_bash`, если команда пакет не добавляет, зовёт `snapshot_manifests`; команда с
  маркером (`manifest_watch.has_marker` — по семантике `depcheck`: `PLANKA_DEP_OK=1` ведущим присваиванием
  команды хоть одного сегмента; комментарий и аргумент не маркер, внутри `bash -c "…"` маркер не виден) не
  снимается. `manifest_watch.take` берёт список `list_manifests` — `git ls-files -z -c -o
  --exclude-standard`, вне git обход `_walk` (в обоих режимах без `FOREIGN_DIRS`, куда входит
  `snapshot.IGNORED_DIRS`), не больше `MAX_WALK_FILES` файлов, — не больше `MAX_MANIFESTS` манифестов и на
  каждый пишет `[size, mtime_ns, имена с транзитивными или None, имя пакета манифеста или None]`:
  содержимое не хранится. Если команда возвращает файлы из ref (`manifest_watch.command_refs`), в снимок идёт
  поле `known` — `{реестр: имена}` манифестов ref, созданных до начала сессии (`restored_names`, ниже). Снимок
  с полями `cwd` (каталог команды до неё) и `known` `store` кладёт в
  `state/<session>.manifests.json` под `tool_use_id` и там же удаляет записи старше `ENTRY_TTL`. Сбой
  снимка — `common.warn_once` с ключом `manifest-snapshot` и `skipped` в журнал на каждую команду.
- После команды — `judge_tool.check_command_manifests` на `POST_EVENTS`: `manifest_watch.pop` забирает
  снимок; пути вывода генераторов requirements (`generated_requirements`) `resolve` разрешает от `cwd`
  снимка и от `cwd` после команды (`cd` внутри неё); `compare` — смена режима git/walk — `Unavailable`;
  манифест с тем же размером и mtime пропускается, новый сравнивается с пустым, не разобранный до или
  после — в список предупреждения и `skipped`. Имя, объявленное в любом манифесте того же реестра в снимке, в
  `known` снимка (ref до начала сессии) или пакет самого проекта в снимке и в манифестах после команды, не
  новое: `mv`, `cp`, `git mv`, член workspace, возврат работы автора. Остальное решает `fresh_names` с версиями
  `_REPO_REFS`. Новые имена — `block_output(COMMAND_REASON)`: правка уже в файле, хук не знает, чья она
  (`git apply` патча, ref моложе начала сессии), поэтому текст велит спросить автора, не откатывать вслепую и
  откатывать только свою правку; в журнал `block-dep` с числами `manifests` и `added`.

Имена ref до начала сессии. `manifest_watch.command_refs(command)` по сегментам (`depcheck._segments`,
`depcheck._command`) находит, откуда команда git кладёт файлы в рабочее дерево без коммита: `git stash pop|apply
[N|stash@{N}]` (по умолчанию `stash@{0}`), `git merge --squash <ref>…`, `git checkout <ref> [--] <пути>` (один
операнд без `--` — ветка или путь, не ref), `git restore --source <ref>`, `git cherry-pick -n|--no-commit <ref>…`;
`git apply` (патч), merge, cherry-pick и checkout ветки с коммитом не разбираются — их имена в HEAD или
`_REPO_REFS`; `git -C` не учитывается. Начало сессии — `manifest_watch.mark_start`: на каждом `PreToolUse`
`judge_tool.main` (не на `POST_EVENTS`, не внутри судьи) пишет `state/<session>.start.json` с `int(time.time())`
при первом вызове и дальше файл не меняет; сбой каталога состояния глотается — без начала сессии ref не
сверяются. `manifest_watch.restored_names(root, command, start, deadline)` одним `git cat-file --batch` читает
коммиты ref (`_old_trees`) и берёт деревья тех, чьё время коммиттера меньше `start`, у stash — ещё дерево
третьего родителя (неотслеживаемые файлы `stash -u`); для каждого дерева `git ls-tree -r -z --name-only
--full-tree` — пути манифестов (`watched_kind`, не больше `MAX_MANIFESTS`) и `git cat-file --batch` их блобов (не
больше `MAX_MANIFEST_BYTES`) — `known_names` по реестру. `start` `None`, ref моложе, не найден или срок вышел —
имён нет, сравнение блокирует, как без ref. Время ref — время коммиттера: поддельная дата и ref прошлой сессии
агента проходят как работа автора — `context/deferred/stash-restore-vs-agent-edit.md`.

Генератор requirements (`manifest_watch._is_generator`) узнаётся по словам сегмента `depcheck._command` приватными
помощниками `depcheck`: `_subcommand`, `_after_flags`, `_python_module`, `_PIP`, `_GLOBAL_FLAGS` и наборы флагов;
файлы вывода — цели перенаправлений stdout (`depcheck._split`, `depcheck._REDIRECT`) и значения `_OUTPUT_FLAGS`.
Правка этих помощников `depcheck` — правка `_is_generator`, её держит `GeneratedRequirementsTest` в
`tests/test_judge_tool.py`. Файл, который пишет генератор (`pip freeze`, `uv export`, `poetry export` и т.п.),
перечисляет транзитивные пакеты — их выбрал не агент, поэтому файл в сравнении после команды не проверяется. Дешёвая
ошибка здесь — пропуск: генератор, направленный в рукописный файл, снимает с него проверку. `tee` сразу за генератором
(`pip freeze | tee requirements.txt`) узнаётся (`_tee_outputs`); конвейер с фильтром (`pip freeze | sort >
requirements.txt`) нет — перенаправление в другом сегменте, файл проверяется.

## Состояние и журнал

Каталог данных — `$CLAUDE_PLUGIN_DATA`, без него `.data/` в корне плагина (`common.data_dir`).

- `state/<session>.json` — счётчики отказов; `state/<session>.warned.json` — выданные
  однократные предупреждения (`common.warn_once`); `state/<session>.snap.json` — снимок дерева
  текущей реплики (`snapshot.store`, поле `format` = `snapshot.FORMAT`, 2; блоки режима walk — строкой
  `snapshot._pack_dirs`); `state/<session>.start.json` — начало сессии `{"start": секунды}`
  (`manifest_watch.mark_start`, пишется один раз); `state/<session>.debug.json` — неудачи команд (`counts`) и
  отметки показа модуля по агентам (`shown`); `state/<session>.manifests.json` — снимки манифестов перед
  командами `Bash`, `{tool_use_id: {root, mode, ts, cwd, files: {путь: [size, mtime_ns, имена или
  None, имя пакета или None]}, known?: {реестр: [имена]}}}` (`manifest_watch.store`, `manifest_watch.pop`;
  запись не той формы `pop` отбрасывает).
  Имя — `common.safe_name`. Запись атомарная (`common.atomic_write_json`: временный `.tmp-*` и
  `os.replace`); чтение — `common.read_json` (нет файла, битый JSON или значение не того типа —
  пустое значение); чтение и запись счётчиков, предупреждений, неудач и снимков манифестов — под `fcntl.flock` на
  `state/.lock` (`common.state_lock`). JSON пишется через `common.dumps`: одиночный суррогат в имени
  файла не в UTF-8 — escape `\udcXX`. Файлы `*.json` и брошенные `.tmp-*` старше `STATE_TTL` (7 дней)
  удаляет `common.prune_state`.
- `judge.log` — строка JSON на решение хука, только метаданные: содержимое — длиной и SHA-256
  (`common.log_event`). От `LOG_MAX_BYTES` (1 МиБ) переименовывается в `judge.log.1`; ротация
  и запись под `fcntl.flock` на `judge.log.lock`.

## Снимок дерева

`snapshot.capture(root, deadline)` возвращает снимок одного из двух режимов; `snapshot.store`
добавляет `prompt_id`, `root` и версию формата `format` (`snapshot.FORMAT`, 2), `snapshot.load` отвергает файл
другой версии (снимок прошлого формата — `None`, как нет снимка), без полей своего режима и с повреждёнными
блоками walk.

- `git` — корень в git (`snapshot._git_state`): по каждому репозиторию — корню, инициализированному
  подмодулю (запись `160000` в `ls-files -s` и свой `.git`) и вложенному репозиторию-не-подмодулю
  (неотслеживаемый каталог `dir/` в `git status`) — HEAD и пути из `git status --porcelain=v2
  --untracked-files=all --ignore-submodules=all` (`snapshot._status`). В снимке `repos` —
  `{префикс: HEAD}`, `dirty` — `{путь: [size, mtime_ns] или None}`, `head` и `sub_heads` — базы для
  `comments.extract`. Размер снимка не зависит от размера дерева. `MAX_FILES` (50 000) ограничивает число
  грязных путей: они хранятся словарём, объектом на путь, в памяти и в JSON; больше — `snapshot.TooManyFiles`,
  `capture` его не ловит. Вложенный репозиторий, где git не работает, обходится `_walk_paths` без предела
  числа файлов: его файлы — грязные пути родителя, и предел `MAX_FILES` на них действует уже в `capture`.
- `walk` — вне git: `dirs` — `{каталог от корня через «/», "" — корень: блок}` обхода `snapshot._walk_dirs`.
  Обход `snapshot._walk` — стек каталогов и `os.scandir`, `IGNORED_DIRS` и упрощённый `.gitignore` по
  `snapshot.ignore_rules` (правила родителя плюс свои, на поддерево); символические ссылки не обходятся и в
  снимок не попадают, непрочитанный каталог пропускается. Блок каталога — записи его обычных файлов «имя в
  байтах ФС, NUL, `snapshot._STAT` (size, mtime_ns)», отсортированные: каталог без правок даёт тот же блок при
  любом порядке readdir. Объекта на файл нет — память около размера блоков. Предела числа файлов нет:
  обход ограничен только сроком, срок проверяется на каждом каталоге и раз в `STAT_CHECK_EVERY` файлов.
  В файл снимка блоки идут одной строкой `snapshot._pack_dirs` — base64 от zlib (уровень 1, сжатие по
  каталогу) записей «каталог, NUL, `_LEN` длины блока, блок»; `load` разворачивает её `_unpack_dirs`.

`snapshot.changed_since(root, snap, deadline)` даёт `[(путь, обычный ли файл сейчас)]`. В git
кандидаты — грязные пути на старте и сейчас и, для репозитория, чей HEAD сменился, `git diff
--name-only` против HEAD снимка (без HEAD — все отслеживаемые); из них отбрасываются пути, чей stat
совпал со снимком. В walk — `snapshot.diff` блоков двух обходов: равные блоки каталога пропускаются целиком,
у разных сравниваются записи (`snapshot._records`). `None` с предупреждением — смена режима, пропавший
репозиторий, недоступный коммит; `TimeoutError` — срок.

Снимок связывает `UserPromptSubmit` и `Stop`: `judge_stop.changed_this_turn` сравнивает его с
текущим деревом, только если совпали `prompt_id` и корень проекта. Корень (`common.project_root`)
— вершина git для `CLAUDE_PROJECT_DIR`, без неё — для `cwd` входа хука (путь от git — через
`os.fsdecode`); `cwd` меняется после `cd` агента, `CLAUDE_PROJECT_DIR` — нет.

## Комментарии изменённых файлов

`comments.extract(root, relpaths, base, sub_bases, deadline)` возвращает `(lines, truncated, unknown,
late)`: строки «путь: комментарий», обрезано ли по `MAX_LINES`/`MAX_BYTES`, файлы без известного
синтаксиса, файлы, не разобранные к сроку. Файл разбирается целиком (`comments._comments` по
`comments._SYNTAX`), в вывод идут комментарии строк из `comments._added_lines` — номера по заголовкам
`@@` одного `git diff -U0` против `base` (HEAD на старте реплики) на все отслеживаемые файлы
репозитория (`comments._diff`). `--text` — чтобы файл с атрибутом `-diff` или `binary` (из
`.gitattributes` проекта или `core.attributesFile`) давал строки, а не «Binary files differ»;
`--find-renames` — чтобы переименованный файл сравнивался со старым путём при любом `diff.renames`.
Старый путь git видит, только если он в списке путей: когда среди путей есть новые против `base`, diff
повторяется вместе с путями, удалёнными против `base` (`--diff-filter=D`), если их не больше
`comments.MAX_RENAME_SOURCES` (1000, предел командной строки). Пару git находит по сходству (порог git
по умолчанию — 50 %); файл без пары, в том числе после `mv` без `git add`, — целиком.

Файл подмодуля или вложенного репозитория-не-подмодуля — против базы своего репозитория
(`comments._select`): подмодули — записи `160000` индекса (база из `sub_bases`, без записи — текущий
HEAD подмодуля), вложенные репозитории — ключи `sub_bases`; файл достаётся самому глубокому
репозиторию, чей путь — его префикс. Без `base`, для неотслеживаемого файла и при сбое diff — весь
файл.

Хвосты строки перед местом разбора (`comments._heredoc_ok`, `_docstring_start`, `_after_print`) просматриваются
назад через `comments._back`, не регуляркой по срезу префикса: строка из многих таких мест разбирается за
линейное время.

Срок разбора `_comments` проверяет раз на `_DEADLINE_EVERY` шагов, шаг — строка или позиция внутри
строки: и одна длинная строка (минифицированный бандл) не уходит за срок. BOM в начале файла
снимается. Heredoc Ruby и Perl (`rb`, `pl`, `Rakefile`, `Gemfile`; `comments._RUBY_HEREDOC`: `<<ID`,
`<<-ID`, `<<~ID`, идентификатор и в кавычках) — данные. Сдвиг или добавление от heredoc отличает
`comments._heredoc_ok`: `<<` сразу после слова или закрывающей скобки — сдвиг (`1<<BITS`, `a[0]<<X`,
`print $fh<<EOF`); в Perl исключение (`comments._tight_perl`): вплотную после `print`, `printf`, `say`,
`die`, `warn` (`print<<EOT`) и после дескриптора из заглавных и `_` за `print`, `printf`, `say`
(`print CSS<<EOF`) — heredoc; слово с `$`, `@`, `%`, `&`, `>` впереди (`$fh<<`) не считается.
После пробела за скобкой, кавычкой или переменной (`$a`, `@a`) — сдвиг; в Perl
исключение — дескриптор после `print`, `printf`, `say` (`$fh`, `STDOUT`, `STDERR`, слово ищет
`comments._after_print`; блок `{$fh}`, `{$DB::OUT}`, `{$self->{fh}}`, `{*STDOUT}` — и вплотную,
`print{$fh}<<EOF`, — ищет `comments._print_block` назад не дальше `_BLOCK_LOOKBACK`, 256 знаков, чтобы
разбор строки оставался линейным): `print $fh <<EOF` — heredoc, в Ruby это сдвиг; за прочим словом — heredoc,
только если идентификатор с `-`, `~`, в кавычках или с заглавной буквы (`print <<EOF`; в Perl ведущие `_`
идентификатора не в счёт, `<<_EOUSAGE_`), иначе добавление
(`a <<b`, `puts <<eof`); после другого знака (`=`, `(`, `,`) и в начале строки — heredoc. Дешёвая ошибка —
heredoc, принятый за сдвиг: его тело читается как код и показывает судье лишние строки.

Heredoc Ruby, Perl и Terraform без строки-терминатора до конца файла — не heredoc: `_comments` повторяет
разбор `_parse`, запретив открывать heredoc в этих позициях (`banned`); всего разборов не больше
`comments._MAX_REPARSE` (8) — каждый следующий снимает хотя бы одно открытие; тело такого heredoc читается как
код. Heredoc оболочки (`depcheck.heredocs`) так не повторяется.

Регулярные выражения, slashy-строки и JSX — поля `comments._Syntax` `regex` и `jsx`. `regex="js"` (js, jsx, mjs,
cjs, ts, tsx, mts, cts) и `regex="groovy"` (groovy, gradle): `/` в начале выражения (`comments._expr_start`) —
литерал, `//` и `/*` раньше — комментарии. Начало выражения — по предыдущему токену: назад через пробелы знак из
`_EXPR_AFTER` (не `)`, `]`, кавычка, слово; `++` и `--` — постфикс) или слово из `_EXPR_KEYWORDS` не после `.`;
слово просматривается не дальше `_KEYWORD_MAX` + 1 знаков — время линейное. В начале строки решает `fresh`: в JS —
по концу прошлой строки кода (`a\n/ b` — деление; блочный комментарий в конце строки и строка из одних
комментариев его не меняют), в Groovy всегда начало (конец строки завершает оператор). Литерал — `_JS_REGEX`
(escape, класс `[...]`, флаги) или `_SLASHY` до конца строки, без отката; незакрытый в строке кончается с ней:
многострочная slashy-строка дальше читается как код, мнимая регулярка не прячет следующие строки. Dollar-slashy
Groovy `$/…/$` — многострочный литерал с escape `dollar` (`comments._close`: `$$`, `$/` — escape). `jsx=True`
(js, jsx, tsx; в ts `<T>(x) => x` — обобщение) — `<` в начале выражения, за которым `comments._jsx_opens` видит
тег (`>` фрагмента или имя, за ним `>`, `/`, `{`, атрибут или конец строки; `<T,>`, `<T extends X>`, `<T = X>` —
обобщение), открывает разметку: `comments._markup` ведёт стек `ctx` (`text` — дети элемента, `tag` —
открывающий тег, `close` — закрывающий, `code` — код в `{…}` со счётчиком скобок, его разбирает
`comments._jsx_code`). Текст между тегами — не код; в теге `//` и `/* */` — комментарии, строки атрибутов —
многострочные без escape. Тег или текст, не закрытые к концу файла, `_parse` возвращает в незакрытых, и
`_comments` повторяет разбор, запретив открывать их (`banned`), как heredoc без терминатора. Шаблонные строки JS
— литерал целиком, подстановки `${…}` не разбираются; остаток — `context/deferred/comment-syntaxes.md`.

Прочие поля `comments._Syntax`: `nested` — блоки вкладываются, глубину считает `comments._close_nested`
(`/* */` kt, scala, swift, rs, dart; `{- -}` hs, `#= =#` jl, `(* *)` fs, `#[ ]#` nim, `#| |#` lisp;
`(*)` F# — оператор, `comments._block_opens`); `prefix` — символьный литерал со знаком комментария или
строки — данные (`$%` Erlang, `\;` Clojure, `?;` Emacs Lisp, `#\;` Common Lisp, `?#` Ruby и Elixir — за
ним не буква); `rem` — слово `REM` после указанных знаков открывает комментарий (vb, vbs, bat, cmd,
`comments._rem`); `line_block` — блок из целых строк с первой колонки, идёт в вывод целиком (`=begin`…`=end`
Ruby, POD Perl от `=слово` до `=cut`); `exdoc` — `@doc`, `@moduledoc`, `@typedoc` Elixir со строкой —
документация, в вывод; `heredoc="php"` — тело `<<<ID` до терминатора с отступом, за которым код той же
строки разбирается дальше. Правила маркера `comments._marker_ok`: `start` — только пробелы перед ним (`::`
bat), `vim` — `comments._vim_quote` (`"` в начале строки или после пробела без закрывающей кавычки до конца
строки — комментарий, иначе строка), `vim9` — `#` после пробела, не `#{`. Raw-строки `comments._raw_string`:
ещё `swift` (`#"…"#`, закрытие — кавычки и столько же `#`) и `nim` (`r"…"`, `ident"…"`, удвоенная кавычка —
escape). Docstring Clojure и Emacs Lisp — строка, `#_` Clojure — код: в вывод не идут.

Класс изменённого файла (`common.path_kind`): расширение из `CODE_EXTS` или имя из `CODE_NAMES`
(в том числе стили и манифесты) — код в любом каталоге; затем документация по `common.is_doc_path`;
иначе прочее. Расширения с синтаксисом комментариев в `comments.py` и `CODE_EXTS` совпадают в обе
стороны — у каждого расширения кода есть синтаксис, — а `comments._NAMES` совпадает с `CODE_NAMES` (тест
`PathKindTest.test_code_exts_cover_comment_families` в `tests/test_common.py`). Список «файлы без
известного синтаксиса» (`unknown` из `comments.extract`) поэтому пуст для любого файла кода.

## Платформы

Только POSIX: `common` импортирует `fcntl`, `run_judge` использует `os.killpg` и
`start_new_session`. Смерть судьи вместе с хуком — сторож `common._WATCHDOG` на всех платформах, без
`prctl`; сторож проверен тестами `WatchdogTest` и `JudgeDiesWithHookTest` только на Linux, на других
платформах не запускался. Python 3.11+ (`tomllib` в `manifests`). Зависимостей вне стандартной
библиотеки Python нет.
