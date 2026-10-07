# Архитектура

planka — плагин Claude Code уровня пользователя: пять хуков из `hooks/hooks.json` подмешивают
правила из `philosophy.md` и `rules/*.md` в контекст агента и отклоняют его действия через
вложенного судью-модель или детерминированные проверки. Пользовательское описание поведения —
`README.md`, разделы «Как это работает» и «Известные ограничения».

Плагин — каталог `plugin/`: маркетплейс отдаёт пользователям только его (`"source": "./plugin"` в
`.claude-plugin/marketplace.json`). Пути ниже, кроме `.claude-plugin/marketplace.json`, — от
`plugin/`, он же `CLAUDE_PLUGIN_ROOT` хуков.

## Компоненты

| Файл | Роль |
| --- | --- |
| `hooks/hooks.json` | регистрация хуков: `UserPromptSubmit` → `remind.py`; `PreToolUse` на `AskUserQuestion\|ExitPlanMode\|Bash` → `judge_tool.py`; `PreToolUse` на файловые инструменты записи и MCP-инструменты с `memor` или `remember` в имени в любом регистре (регулярное выражение — в `hooks/hooks.json`) → `guard_memory.py`; `Stop` → `judge_stop.py`; `PostToolUse` и `PostToolUseFailure` на `Bash` → `debug_watch.py` |
| `planka/remind.py` | `philosophy.md` целиком как `additionalContext`; снимок дерева (`take_snapshot`); строка `NO_DOCS_LINE` об отсутствии `CLAUDE.md` |
| `planka/judge_tool.py` | судья вопроса (`judge_question`), плана (`judge_plan`), отказ на добавление пакета (`judge_bash`, причина `DEP_REASON`) |
| `planka/guard_memory.py` | судья записи в постоянную память: цель — `is_memory_path`, `is_memory_mcp`; содержимое — `render_content` |
| `planka/judge_stop.py` | фильтры «варианты» (`looks_like_options`), «готово» (`claims_done`) по последнему сообщению, «документация» (`docs_check`) по изменениям со снимка; один вызов судьи на сообщения реплики (`turn_messages`, до `prompts.MAX_TURN_CHARS`) |
| `planka/debug_watch.py` | счётчик неудач подряд одной команды `Bash` (`update`); с `REPEAT_THRESHOLD`-й неудачи — модуль `debugging.md` контекстом |
| `planka/common.py` | барьер, чтение входа, тексты правил и рубрика, транскрипт (`read_transcript`), модель и запуск судьи (`judge_model`, `run_judge`), лимит отказов, журнал, классы путей, формат ответа (`run_hook`) |
| `planka/prompts.py` | системный промпт, схема ответа `JUDGE_SCHEMA`, вопросы судье по видам проверки, сборка содержимого |
| `planka/planparse.py` | разбор плана на волны и задачи, владение файлами (`shared_files`) |
| `planka/depcheck.py` | разбор команды Bash: добавляет ли она пакет (`dependency_add`), маркер `DEP_OK_MARKER`, heredoc (`heredocs`) |
| `planka/snapshot.py` | снимок дерева (`capture`, `store`, `load`) и изменения с него (`changed_since`) в режимах git и walk; порог `MAX_FILES` |
| `planka/comments.py` | строки комментариев изменённых файлов для судьи документации (`extract`) |
| `philosophy.md` | ядро правил; индекс «Модули» в конце |
| `rules/*.md` | модули правил, по файлу на область |
| `.claude-plugin/plugin.json` | манифест и `userConfig`: `judge_model`, `comment_lang`, `doc_lang` |
| `.claude-plugin/marketplace.json` | маркетплейс `planka` в корне репозитория, источник плагина — `./plugin` |

## Контракт кода с текстами правил

Код ищет тексты правил по именам; переименование ломает хук без ошибки теста хука — хук пропускает
проверку с предупреждением. Настоящие тексты сверяет `tests/test_contract.py`.

- Разделы ядра берутся по заголовку `## <имя>` (`common.philosophy_sections`, `common.rubric`):
  `Решения` — рубрика вопроса, плана и фильтра «варианты»; `Планы` — рубрика плана; `Границы` —
  рубрика записи в память, её же называет `judge_tool.DEP_REASON`. Пункт 7 «Решений» называют
  вопросы `prompts._QUESTION_CHECKS`.
- Модули берутся по имени файла (`common.rule_texts`, `common.rubric`): `planning`, `subagents`,
  `refactoring`, `design-patterns`, `heuristics` — план (`judge_tool.judge_plan`); `verification` — фильтр «готово»;
  `docs`, `comments`, `design-patterns`, `refactoring` — фильтр «документация» (`judge_stop.main`);
  `memory` — запись в память (`guard_memory.main`); `debugging` — `debug_watch.MODULE`.
  `rules/dependencies.md` называет причина отказа `judge_tool.DEP_REASON`.
- Метки `{RULES}`, `{COMMENT_LANG}`, `{DOC_LANG}` заменяет `common.substitute` при каждом чтении:
  путь к `rules/` плагина и значения настроек; незаданный язык — `DEFAULT_LANG` (`ru`). Ссылка на
  модуль из ядра и модулей пишется как `{RULES}/<имя>.md`, строка `remind.NO_DOCS_LINE` — так же;
  `debug_watch.LINE` и `judge_tool.DEP_REASON` подставляют `common.rules_dir()`: агент получает
  абсолютный путь к правилам плагина, а `rules/` проекта с ним не путается.

## Ответ хука

Все хуки запускаются через `common.run_hook`: stdout — один JSON (ответ из `common.emit` плюс
`systemMessage` из `common.warn`) или пусто; stderr не пишется; исключение превращается в
предупреждение «внутренняя ошибка». Ввод и вывод — байтами через `sys.stdin.buffer` и
`sys.stdout.buffer` в UTF-8, независимо от локали; JSON — через `common.dumps`. Причина отказа
начинается с `planka: `. Отказ `PreToolUse` — `common.deny_output`, отказ `Stop` —
`common.block_output`, контекст `UserPromptSubmit` — `common.context_output`; `debug_watch` отвечает
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
`MAX_REASON`.

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
корпус `tests/fixtures/transcript-shapes.jsonl`. Нет файла или ошибка чтения — пустой `Transcript`.
Хук читает транскрипт один раз и передаёт его в `judge_model`.

Проверяемое содержимое идёт судье блоком `<content>…</content>` (`prompts._wrap`); закрывающий тег
внутри содержимого — в любом регистре и с пробелами (`prompts._CLOSING_TAG`) — экранируется.
Вопросы по видам: `prompts.question_prompt`, `prompts.plan_prompt`, `prompts.memory_prompt`,
`prompts.stop_prompt` (вопросы совпавших фильтров в порядке options, done, docs; сообщения реплики
склеивает `prompts.turn_content` с пояснением `prompts.TURN_LABEL`, не больше `prompts.MAX_TURN_CHARS`
символов: ранние сообщения сверх предела опущены с пометкой их числа), содержимое фильтра
«документация» — `prompts.render_docs_content`.

Судья — лидер своей группы процессов (`start_new_session`): таймаут `JUDGE_TIMEOUT` (60 с) убивает
группу (`common._kill_group`, ожидание до 5 с). Группа умирает и вместе с хуком (`common._start_judge`):
на Linux — `PR_SET_PDEATHSIG` в `preexec_fn` (`common._die_with_hook`); на других платформах судья
запускается потомком сторожа `common._WATCHDOG` (`python3 -I -c`), который раз в `WATCHDOG_POLL`
(0,5 с) сверяет своего родителя с PID хука и при расхождении убивает группу. Путь к `claude` для
сторожа ищется по `PATH` окружения судьи; нет — `FileNotFoundError`, пропуск «claude не найден в PATH».

## Сроки

Таймауты хуков в `hooks/hooks.json`: `UserPromptSubmit` 10 с, `PreToolUse` 90 с у обоих хуков,
`Stop` 120 с, `PostToolUse` и `PostToolUseFailure` 10 с. У `Stop` до судьи ещё `git rev-parse`
(до 5 с), сверка со снимком со сроком `judge_stop.SNAPSHOT_BUDGET` (20 с) и извлечение комментариев
со сроком `judge_stop.COMMENTS_BUDGET` (20 с) — вместе с судьёй (до 65 с) не больше 110 с. У
`UserPromptSubmit` снимок ограничен сроком `remind.SNAPSHOT_BUDGET` (7 с от старта хука); не
уложился — `TimeoutError`, снимок пропускается с предупреждением, напоминание выдаётся. Один вызов
git в `snapshot` и `comments` — не дольше `GIT_TIMEOUT` (10 с) и остатка срока.

## Лимит отказов

`MAX_DENIES` = 2 на ключ `<prompt_id>:<hook>`, хук — `question`, `plan`, `memory`, `stop`. До вызова
судьи `common.deny_budget_left` проверяет счётчик, не меняя его (сбой чтения — проверка идёт); лимит
исчерпан — пропуск с предупреждением и записью `budget`. При отказе `common.deny_budget_exhausted`
увеличивает счётчик; если к этому моменту он уже на пределе (параллельный вызов), отказ заменяется
пропуском. Сбой записи счётчика — исключение, `run_hook` выдаёт «внутреннюю ошибку» без отказа: без
счётчика нечем остановить цикл отказов. Отказ `judge_bash` лимитом не ограничен.

## Запись в память

`guard_memory` срабатывает на файловые инструменты (`FILE_TOOLS`) и MCP-инструменты. Каталог
настроек — `guard_memory.config_dir` (`CLAUDE_CONFIG_DIR`, без него `~/.claude`). Путь цели —
`guard_memory.target_path` (`~` раскрыта, относительный — от `cwd` входа, ссылки не разрешены); память —
`guard_memory.is_memory_path`: `CLAUDE.md` и `rules/**.md` каталога настроек,
`projects/<проект>/memory/**`, абсолютный `autoMemoryDirectory` из его `settings.json`. Путь цели и
места памяти (`_memory_places`) сравниваются в двух формах — как написаны и через `realpath`
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
(`guard_memory.write_text`), каждое поле — до `MAX_FIELD` символов.

## Повторная неудача команды

`debug_watch` считает по ключу `debug_watch.command_key` (SHA-256 команды с нормализованными
пробелами) в `state/<session>.debug.json`: `counts` — неудачи подряд по ключам, `shown_prompt` —
реплика, в которой модуль уже показан. Неудача — событие `PostToolUseFailure` без `is_interrupt`,
кроме кода 1 команды-ответа; `PostToolUse` той же команды и код 1 команды-ответа удаляют её ключ.
Код выхода — первая строка `error` вида `Exit code N` (`debug_watch.exit_code`; документация хуков
велит опираться только на неё, остальное — вывод без стабильного формата). Команда-ответ
(`debug_watch.code1_is_answer`) — последняя команда строки (`_segments`: комментарий от `#` в начале слова, в том числе после `;`, `|`; разделители вне кавычек `|`,
`|&`, `||`, `&&`, `;`, перевод строки), а если она после `&&` и из `PASS_THROUGH` без
перенаправлений (`<`, `>` вне кавычек и экранирования, `UNQUOTED`) — команда перед ней (`_last_pipeline_command`); присваивание одной
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
подмешивается не чаще раза за `prompt_id`; нет модуля — предупреждение, отметка о показе не ставится.

## Состояние и журнал

Каталог данных — `$CLAUDE_PLUGIN_DATA`, без него `.data/` в корне плагина (`common.data_dir`).

- `state/<session>.json` — счётчики отказов; `state/<session>.warned.json` — выданные
  однократные предупреждения (`common.warn_once`); `state/<session>.snap.json` — снимок дерева
  текущей реплики (`snapshot.store`); `state/<session>.debug.json` — счётчики неудач команд. Имя —
  `common.safe_name`. Запись атомарная (`common.atomic_write_json`: временный `.tmp-*` и
  `os.replace`); чтение — `common.read_json` (нет файла, битый JSON или значение не того типа —
  пустое значение); чтение и запись счётчиков, предупреждений и неудач — под `fcntl.flock` на
  `state/.lock` (`common.state_lock`). JSON пишется через `common.dumps`: одиночный суррогат в имени
  файла не в UTF-8 — escape `\udcXX`. Файлы `*.json` и брошенные `.tmp-*` старше `STATE_TTL` (7 дней)
  удаляет `common.prune_state`.
- `judge.log` — строка JSON на решение хука, только метаданные: содержимое — длиной и SHA-256
  (`common.log_event`). От `LOG_MAX_BYTES` (1 МиБ) переименовывается в `judge.log.1`; ротация
  и запись под `fcntl.flock` на `judge.log.lock`.

## Снимок дерева

`snapshot.capture(root, deadline)` возвращает снимок одного из двух режимов; `snapshot.store`
добавляет `prompt_id` и `root`, `snapshot.load` отвергает файл без полей своего режима.

- `git` — корень в git (`snapshot._git_state`): по каждому репозиторию — корню, инициализированному
  подмодулю (запись `160000` в `ls-files -s` и свой `.git`) и вложенному репозиторию-не-подмодулю
  (неотслеживаемый каталог `dir/` в `git status`) — HEAD и пути из `git status --porcelain=v2
  --untracked-files=all --ignore-submodules=all` (`snapshot._status`). В снимке `repos` —
  `{префикс: HEAD}`, `dirty` — `{путь: [size, mtime_ns] или None}`, `head` и `sub_heads` — базы для
  `comments.extract`. Размер снимка не зависит от размера дерева; `MAX_FILES` ограничивает число
  грязных путей. Вложенный репозиторий, где git не работает, обходится как вне git.
- `walk` — вне git: `files` — `{путь: [size, mtime_ns]}` обхода `snapshot._walk_paths`
  (`IGNORED_DIRS` и упрощённый `.gitignore` по `snapshot.ignore_rules`). Больше `MAX_FILES` —
  `snapshot.TooManyFiles`, `remind.take_snapshot` предупреждает раз на сессию
  (`context/deferred/snapshot-threshold.md`).

`snapshot.changed_since(root, snap, deadline)` даёт `[(путь, обычный ли файл сейчас)]`. В git
кандидаты — грязные пути на старте и сейчас и, для репозитория, чей HEAD сменился, `git diff
--name-only` против HEAD снимка (без HEAD — все отслеживаемые); из них отбрасываются пути, чей stat
совпал со снимком. В walk — разница двух обходов (`snapshot.diff`). `None` с предупреждением — смена
режима, пропавший репозиторий, недоступный коммит, вне git больше `MAX_FILES`; `TimeoutError` — срок.

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
репозитория; файл подмодуля — против `sub_bases` своего подмодуля (`comments._select`). Без `base`,
для неотслеживаемого файла и при сбое diff — весь файл.

Класс изменённого файла (`common.path_kind`): расширение из `CODE_EXTS` или имя из `CODE_NAMES`
(в том числе стили и манифесты) — код в любом каталоге; затем документация по `common.is_doc_path`;
иначе прочее. Каждое расширение с синтаксисом комментариев в `comments.py` входит в `CODE_EXTS`, а
`comments._NAMES` совпадает с `CODE_NAMES` (тест `test_code_exts_cover_comment_families`).

## Платформы

Только POSIX: `common` импортирует `fcntl`, `run_judge` использует `os.killpg` и
`start_new_session`. Смерть судьи вместе с хуком на Linux — `prctl` через `ctypes`, на остальных
POSIX — сторож; сторож проверен тестом `WatchdogTest` подменой `sys.platform` на Linux, на других
платформах не запускался. Зависимостей вне стандартной библиотеки Python нет.
