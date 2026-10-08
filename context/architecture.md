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
| `planka/judge_stop.py` | фильтры «варианты» (`looks_like_options`), «готово» (`claims_done`) по последнему сообщению, «документация» (`docs_check`) по изменениям со снимка; один вызов судьи на шаги реплики (`turn_messages`, до `prompts.MAX_TURN_CHARS`); отметка снимка проверенным после Stop без блока (`release_snapshot`) |
| `planka/debug_watch.py` | счётчик неудач подряд одной команды `Bash` (`update`); с `REPEAT_THRESHOLD`-й неудачи — модуль `debugging.md` контекстом |
| `planka/common.py` | барьер, чтение входа, тексты правил и рубрика, транскрипт (`read_transcript`), модель и запуск судьи (`judge_model`, `run_judge`), лимит отказов, журнал, корень проекта (`project_root`) и окружение git о нём (`git_env`), классы путей, формат ответа (`run_hook`) |
| `planka/prompts.py` | системный промпт, схема ответа `JUDGE_SCHEMA`, вопросы судье по видам проверки, сборка содержимого |
| `planka/planparse.py` | разбор плана на волны и задачи, владение файлами (`shared_files`) |
| `planka/manifests.py` | разбор манифестов: вид по имени (`kind`), имена внешних зависимостей (`names`), они же с транзитивными (`known_names`), имя пакета манифеста (`own_name`) |
| `planka/manifest_watch.py` | текст манифеста после правки файловым инструментом (`edit_texts`, `check_edit`), новые имена (`fresh_names`, `edit_names`), имена версии HEAD и сторон конфликта (`head_names`), других манифестов проекта и манифестов и файлов `setup.py`, `setup.cfg`, `Pipfile` его версии HEAD (`project_names`, `_tree_names`, `source_kind`), снимок манифестов проекта и сравнение после команды (`take`, `compare`, `store`, `pop`), имена ref до начала сессии, откуда команда git возвращает файлы (`command_refs`, `restored_names`; начало сессии — `mark_start`, `session_start`), файлы вывода генераторов requirements (`generated_requirements`) |
| `planka/depcheck.py` | разбор команды Bash: добавляет ли она пакет (`dependency_add`), маркер `DEP_OK_MARKER`, heredoc (`heredocs`), автомат кавычек `_scan` |
| `planka/snapshot.py` | снимок дерева (`capture`, `store`, `load`) и изменения с него (`changed_since`) в режимах git и walk; порог `MAX_FILES` грязных путей в git; перепривязка непроверенного снимка (`carry_over`, `mark_checked`), неудачи снимка (`mark_failed`, `failure`) |
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
  вопросы `prompts._CHOICE_CHECKS`. Опоры рекомендации из пункта 4 «Решений» (у опоры назван источник) проверяет
  только фильтр «варианты» (`prompts._MESSAGE_CHECKS`, вопрос 6): судья `Stop` видит шаги реплики с вызовами
  инструментов. Судьи вопроса и плана шагов не видят: `judge_tool` вырезает этот пункт из их рубрики
  (`prompts.without_premises` по строке `prompts.PREMISES_ITEM`). Отвергнуто: исключение словами в вопросах — судья
  плана всё равно требовал источник опоры; проверка опор у судьи вопроса — выбор по предпочтению получал отказ.
- Модули берутся по имени файла (`common.rule_texts`, `common.rubric`): `planning`, `subagents`,
  `refactoring`, `design-patterns`, `heuristics` — план (`judge_tool.judge_plan`); `verification` — фильтр «готово»;
  `docs`, `comments` — фильтр «документация» (`judge_stop.judge`; судья видит сообщения, список файлов и
  строки комментариев, а не код, поэтому `design-patterns` и `refactoring` — только в рубрике плана);
  `memory` — запись в память (`guard_memory.main`); `debugging` — `debug_watch.MODULE`.
  `rules/dependencies.md` называют причины отказа `judge_tool.DEP_REASON`, `judge_tool.MANIFEST_REASON`
  и `judge_tool.COMMAND_REASON`.
- Метки `{RULES}`, `{COMMENT_LANG}`, `{DOC_LANG}` заменяет `common.substitute` при каждом чтении:
  путь к `rules/` плагина и значения настроек; незаданный язык — `DEFAULT_LANG` (`ru`). Ссылка на
  модуль из ядра и модулей пишется как `{RULES}/<имя>.md`, строка `remind.NO_DOCS_LINE` — так же;
  `debug_watch.LINE`, `judge_tool.DEP_REASON`, `MANIFEST_REASON` и `COMMAND_REASON` подставляют
  `common.rules_dir()`: агент получает абсолютный путь к правилам плагина, а `rules/` проекта с ним не
  путается.
- Имена, которые код берёт, `tests/test_contract.py` выводит из кода сам (`_code_names`, через `ast`):
  аргументы вызовов `rubric`, `philosophy_sections`, `rule_texts` (через `common.` или после
  `from common import`) — позиционные, распакованные и ключевые; литерал или всё, что в том же файле
  присваивают имени из аргумента (`=`, `+=`, `append`, `extend`), через ветви условного выражения, `+`
  и `tuple()`/`list()`. Аргумент, из которого строк не вывести, — сбой `test_names_taken_by_code_exist`
  (кроме параметров `common.rubric`, переданных дальше). Тест требует каждое имя в `philosophy.md` и
  `rules/`. Ручные `SECTIONS` и `MODULES` — обратное направление: имена, которые код обязан брать; тест
  требует, чтобы сборщик нашёл их в коде, кроме `dependencies` — его код называет только текстом причин
  отказа. Разбор форм вызова сборщиком держит `CodeNamesTest`.
- Ссылку `{RULES}/<имя>.md` в ядре, модулях и коде (`{rules}/…` — поле `format`) сверяет
  `test_module_references_exist`; метки ищутся без учёта регистра. Вопросы судьи и тексты правил,
  которые говорят одно, сверяет `RulesMatchJudgeTest`.

## Ответ хука

Все хуки запускаются через `common.run_hook`: stdout — один JSON (ответ из `common.emit` плюс
`systemMessage` из `common.warn`) или пусто; stderr не пишется; исключение превращается в
предупреждение «внутренняя ошибка». Ввод и вывод — байтами через `sys.stdin.buffer` и
`sys.stdout.buffer` в UTF-8, независимо от локали, в том числе при кодировке файловой системы ascii
(локаль C без UTF-8 mode); JSON — через `common.dumps`. Путь из входа или транскрипта приводится к
кодировке файловой системы `common.input_path`: Claude Code пишет пути байтами UTF-8, и в локали не
UTF-8 строка с кириллицей иначе не кодируется для `open` и `subprocess`. Через неё идут `cwd` в
`common.project_root`, `transcript_path` и `planFilePath` в `common.read_transcript`, путь цели и `cwd`
в `guard_memory.target_path` и `guard_memory._input_cwd`, значение `autoMemoryDirectory` (файл настроек —
текст UTF-8) в `guard_memory._auto_memory_overrides`. Пути из окружения Python уже декодирует в кодировке
файловой системы. Причина отказа
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

Результат — `common.Verdict`. Любая ошибка судьи — `Verdict` с `error`: короткое описание без текста модели, оно идёт в
журнал; фрагмент ответа (до `MAX_DETAIL` = 200 символов) — в `detail`, только для пользователя. Предупреждение о
пропуске собирает `common.skip_message`. `reason` обрезается до `MAX_REASON`. Аргументы после `claude` и stdin судьи —
байты `common._utf8` (claude читает их в UTF-8 при любой локали; суррогат U+DC80..U+DCFF — исходный байт пути, прочие
одиночные суррогаты заменяются «?» поштучно, остальной текст как есть), ответ декодируется из UTF-8 с заменой; ответ
судьи делится на строки только по `\n`: U+2028, U+2029 и U+0085 JSON оставляет символами внутри строки; `ValueError` при
запуске (нулевой байт в аргументе) — пропуск «claude не запущен».

Модель — `common.judge_model(data, transcript)`: `CLAUDE_PLUGIN_OPTION_JUDGE_MODEL`, по умолчанию
`SESSION_MODEL` (`session`) — тогда модель из транскрипта. Не нашлась — `None`: судья без `--model`,
с предупреждением раз на сессию (`warn_once`, ключ `session-model`). Модель сессии хуки иначе не
получают: поле `model` во входе есть только у `SessionStart` (`context/deferred/session-model-from-transcript.md`).

Транскрипт (`transcript_path` входа) читает `common.read_transcript` за один проход и отдаёт
`common.Transcript`: `model` — `message.model` последнего ответа ассистента основной ветки, кроме
моделей вида `<synthetic>`; `plan_file` — последний `attachment.planFilePath`; `turn_messages` —
тексты ответов ассистента после последней реплики автора; `author_turn` — текст этой реплики,
сообщений человека посреди хода и ответов автора при отклонении инструмента; `author_answers` — ответы на
`AskUserQuestion` после неё; `message_before_author` — последнее сообщение ассистента до неё; `earlier_turns` —
прежние непустые реплики автора основной ветки от старых к новым, каждая собрана как `author_turn` и
дополнена ответами на `AskUserQuestion` своей реплики (обрезка — в `prompts`); `turn_steps` — шаги той же
реплики по порядку: тексты ответов и вызовы инструментов основной ветки (`common._tool_step`: «⟦вызов <имя>⟧» и
команда `Bash`, путь файлового инструмента или вход JSON одной строкой — переводы строки «⏎», до `STEP_ARG`
символов) с выводом из `tool_result`
(«⟦вывод⟧» или «⟦ошибка⟧», длиннее `STEP_HEAD` + `STEP_TAIL` — начало и конец; `common._step_output`) или
«⟦отклонено⟧» для отклонённого вызова. При переполнении `prompts.MAX_TURN_CHARS` `prompts.turn_content` сначала
снимает вывод ранних вызовов («⟦вывод опущен⟧», `prompts._drop_old_outputs`), затем опускает ранние шаги: тексты
сообщений и ближние к последнему выводы — опоры рекомендации — вытесняются последними. Записи `isSidechain`
пропускаются. Шаги берёт только судья `Stop` (`judge_stop.turn_messages`): по ним он видит проверку, о которой
агент пишет словами; дубль последнего сообщения сверяется с последним текстовым шагом — после него в транскрипте
бывает вызов инструмента.

Реплика автора (`common._is_author_turn`) — запись `user` основной ветки без `isMeta`, которую написал человек: с полем
`origin` — только `origin.kind == "human"` (`task-notification` — уведомление о фоновой задаче, `peer` — сообщение
другого агента, оно же `isMeta`). Без `origin` в транскрипте, где `origin` есть хоть у одной записи, — локальная
команда (`<command-name>`, `<local-command-stdout>`), не реплика, в том числе записанная раньше первой записи с
`origin` (сессия, начатая с `/plugin`, `/model`, `/mcp`); в транскрипте старого формата, без `origin`, — запись с
текстом без `tool_result`. Формат транскрипта до разбора узнаёт `common._transcript_has_origin`: проход по файлу до
первой записи с `origin` (строки без подстроки `"origin"` не разбираются), в транскрипте старого формата — до конца
файла. Классификация по ходу чтения («`origin` уже встречался») отвергнута: локальные команды до первой реплики
попадали в `earlier_turns` каждого судьи сессии. Цена — транскрипт, начатый версией Claude Code без `origin` и
продолженный версией с ним: прежние реплики старой части теряются из `earlier_turns`; пропуск ответа автора
дешевле, чем чужой текст за просьбу автора. Сообщение человека посреди хода — вложение `attachment.type ==
"queued_command"` с `origin.kind == "human"`: его текст дописывается к `author_turn`, ход не сбрасывается. Ответ автора
при отклонении инструмента (`common._rejection_feedback`) — запись `user` с `toolDenialKind == "user-rejected"` и
`permissionDecision.source == "user_reject"`: её `userFeedback` дописывается к `author_turn`, tool_result такой записи
не ответ `AskUserQuestion`. Отказы `permission-rule` (хук, правило, safetyCheck) и `automode-blocked` (классификатор) —
не автор: из записей с `toolDenialKind` берётся только `user-rejected`. Дешевле пропустить ответ автора (ложный отказ,
его ограничивает `MAX_DENIES`), чем принять за просьбу автора чужой текст. Формы записей Claude Code 2.1.293 без текста
— корпус `tests/fixtures/transcript-shapes.jsonl`. Нет файла, путь с нулевым байтом или ошибка чтения — пустой
`Transcript`. Хук читает транскрипт один раз и передаёт его в `judge_model`.

Проверяемое содержимое идёт судье блоком `<content>…</content>` (`prompts._wrap`). Судьи вопроса,
плана и `Stop` получают перед ним блок `<author>` — `prompts.author_context(author_turn,
author_answers, earlier_turns)`: прежние реплики автора от старых к новым (все вместе с разделителями —
до `prompts.MAX_AUTHOR_FIELD`, самые старые отбрасываются с пометкой их числа), реплика автора текущего
хода и его ответы на `AskUserQuestion` после неё (каждое поле — до `prompts.MAX_AUTHOR_FIELD`, 8000
символов); передают его `judge_tool._author` и `judge_stop.judge`. Пояснение `prompts._AUTHOR_NOTE`:
явная просьба автора побеждает рубрику, просьба из прежней реплики — тоже, если более поздняя реплика её
не отменила; проверяется только `<content>`, `<author>` — тоже данные. Зачем: без реплики автора судья не
отличает решение агента от решения, которое автор принял сам, и отклоняет сделанное по его явной просьбе, в
том числе по просьбе, сделанной несколько ходов назад;
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
`git rev-parse` и, если вершина — домашний каталог или его предок и `CLAUDE_PROJECT_DIR` ниже неё, `git ls-files`
корня проекта (`common.project_root`, вместе до `common.GIT_ROOT_TIMEOUT`, 5 с), сверка со снимком со сроком
`judge_stop.SNAPSHOT_BUDGET` (20 с) и извлечение комментариев со сроком `judge_stop.COMMENTS_BUDGET` (20 с) — вместе с
судьёй (до 65 с) не больше 110 с. У `UserPromptSubmit` снимок ограничен сроком `remind.SNAPSHOT_BUDGET` (7 с от старта
хука); не уложился — `TimeoutError`, снимок пропускается с предупреждением, неудача запоминается для корня
(`snapshot.mark_failed`; так же при `TooManyFiles`), и до конца сессии `remind` этот корень не снимает;
напоминание выдаётся. Один вызов git в
`snapshot` и `comments` — не дольше `GIT_TIMEOUT` (10 с) и остатка срока. У хуков `PreToolUse` с судьёй срок —
`JUDGE_TIMEOUT` и `KILL_WAIT` (65 с) против 90 с; у `guard_memory` до судьи ещё корень проекта
(`common.project_root`, `common.GIT_ROOT_TIMEOUT`, 5 с) и `git check-ignore` (`guard_memory._repository_file`, срок
`guard_memory.CHECK_IGNORE_TIMEOUT`, 5 с) — для цели под `autoMemoryDirectory` или cowork, который равен проекту
или содержит его; вместе с судьёй 75 с против 90 с. У
`judge_tool` на `Bash` снимок манифестов — `git rev-parse` корня (`common.GIT_ROOT_TIMEOUT`) и срок
`manifest_watch.SNAPSHOT_BUDGET` (5 с) на `git ls-files` и разбор, а у команды с ref (`command_refs`) — и на
`git cat-file --batch` и `git ls-tree` ref до начала сессии (`restored_names`; срок вышел — снимок не снят), у
правки манифеста — корень проекта (`GIT_ROOT_TIMEOUT`, один раз), с ним один `git cat-file --batch` версий
`_REPO_REFS` и сторон `_INDEX_STAGES` (`manifest_watch.HEAD_TIMEOUT`, 5 с), обход манифестов проекта и дерево HEAD
(`judge_tool._project_names`, ещё `SNAPSHOT_BUDGET`): `HEAD_TIMEOUT` + `GIT_ROOT_TIMEOUT` + `SNAPSHOT_BUDGET`; на
`PostToolUse` сравнение со снимком — срок `manifest_watch.CHECK_BUDGET` (5 с) против 30 с, в него входят и `git
cat-file --batch` изменённых манифестов, и дерево HEAD. Вышел срок — пропуск с предупреждением.

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
`planparse._is_whole_path`, текст заголовка ATX без закрывающих `#` — `planparse._atx_text`, пометка в скобках в конце
строки файлов — `planparse._trailing_note`; регулярные выражения в них — без вложенных повторов. Вырезка `<!-- -->`
(`planparse._structure_lines`) — один проход по строке. В `_FILES_HEAD`, `_PREFIX` и хвосте `_READ_NOTE` нет двух
соседних повторов, которые могут съесть один и тот же пробел (`\s*\**\s*`, `[\s*_]*\s+`): на строке из десятков тысяч
пробелов такие пары перебирают квадратично (в версии 0.5.0 пометка `(read` с 30 000 пробелов — 20 с, 1 МБ `<!---->` в
строке — 5–12 с). Регулярные выражения с вложенными повторами на тех же местах уходят в перебор с возвратами: в версии
0.3.0 пункт вида `- a.a.a.…` в 40 000 символов разбирался 29 с, `- a/a/…` в 16 000 — 4,5 с, заголовок `# a` с 30 000
пробелов — 6,6 с (замер), при таймауте `PreToolUse` 90 с и плане, который пишет агент. Линейность держит
`RobustnessTest.test_adversarial_input_is_linear` (отношение процессорного времени входов при множителе 1 и 4,
`context/testing.md`). Формы, которые разбор понимает, —
`README.md`, «Известные ограничения». Строку файлов `planparse._split` делит на пути и остаток после серии путей в
кавычках (связки серии — `planparse._CONN`); повторная строка файлов задачи с остатком-предложением
(`planparse._continues_description`) — описание, её пути владением не считаются.

## Запись в память

`guard_memory` срабатывает на файловые инструменты (`FILE_TOOLS`) и MCP-инструменты. Каталог настроек —
`guard_memory.config_dir` (`CLAUDE_CONFIG_DIR`, без него `~/.claude`). Путь цели — `guard_memory.target_path`
(`common.input_path`, `~` раскрыта, относительный — от `cwd` входа `guard_memory._input_cwd`, ссылки не разрешены; путь
с NUL — `None`, такой файл инструмент не откроет); память — `guard_memory.is_memory_path(path, project)`, `project` —
`guard_memory._project_dir` (`CLAUDE_PROJECT_DIR`, без неё `cwd` входа, без него текущий каталог): `CLAUDE.md` и
`rules/**.md` каталога настроек, `projects/<проект>/memory/**` и `projects/<проект>/agent-memory-local/**`, память
субагентов — `agent-memory/` каталога настроек (`memory: user`) и `.claude/agent-memory-local/` проекта
(`memory: local`), — при `CLAUDE_CODE_REMOTE_MEMORY_DIR` (`guard_memory._remote_memory_dir`, облачная сессия) ещё его
`agent-memory/` и `projects/`, — и каждое абсолютное значение `autoMemoryDirectory`
(`guard_memory._auto_memory_overrides`) и `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` (`guard_memory._cowork_memory_override`:
только абсолютный путь, `~` не раскрывается, как у Claude Code). Значение `autoMemoryDirectory` ищется во всех файлах
`guard_memory._settings_files`: `managed-settings.json` и `managed-settings.d/*.json` каталога
`guard_memory.managed_dir`, `.claude/settings.local.json` и `.claude/settings.json` проекта, `settings.json` каталога
настроек. Claude Code берёт одно значение по приоритету источников, хук — объединение всех: настройки `--settings` и
серверные managed-настройки хуку не видны, порядок приоритета им не воспроизвести, а лишнее место памяти стоит только
лишнего вызова судьи (дешёвая ошибка ниже). Значение приводится `common.input_path`; относительное значение, значение с
NUL и значение, не кодируемое в файловую систему (одиночный суррогат), пропускаются
(`test_unusable_auto_memory_directory_skipped`), как и файл настроек не с объектом JSON. `.claude/agent-memory/` проекта
(`memory: project`) памятью не считается: это отслеживаемый файл репозитория (`MemoryHookTest.test_agent_memory`). По
той же причине под каталогом `autoMemoryDirectory` или cowork, который равен проекту или содержит его
(`guard_memory.is_memory_path`: каждый совпавший каталог содержит проект; сравниваются все формы `_forms` каталога и
проекта), файл репозитория проекта — не память (`guard_memory._repository_file`, спрашивается только для пути внутри
проекта). Под каталогом строго внутри проекта (`test_auto_memory_directory_in_repository`,
`test_auto_memory_directory_in_home_project`) или вне него (`test_auto_memory_directory_in_foreign_repository`) git не
спрашивается, запись судится всегда: автор назначил его местом памяти. Родитель проекта, он же корень репозитория, —
первый случай (`test_auto_memory_directory_above_project`; путь вне проекта судится). Путь во вложенном репозитории
между путём и проектом (`guard_memory._nested_repository`: каталог с `.git`) — не файл рабочего дерева проекта, память
даже при ответе 1 (`test_auto_memory_directory_nested_repository`). Вопрос git проекта: корень —
`common.project_root(<каталог проекта>)`, его нет на диске — не файл репозитория, git не спрашивается; иначе
`git -C <ближайший существующий каталог проекта> check-ignore -q` с окружением `common.git_env(<корень>)`
(репозиторий выше корня — dotfiles над проектом без своего git — не отвечает,
`test_auto_memory_directory_project_under_dotfiles_home`; подкаталог монорепозитория, новый или удалённый, — файл
монорепозитория, `test_auto_memory_directory_monorepo_subproject`) отвечает 1 — путь в рабочем дереве и не исключён
`.gitignore`, `info/exclude`, `core.excludesFile` (отслеживаемый файл исключённым не считается). Иначе значение из
`.claude/settings.json` проекта, равное самому проекту, отдавало бы судье памяти каждую запись в проекте. Код 0
(исключён), 128 (не репозиторий, `test_auto_memory_directory_in_project_without_git`), нет `git`, ошибка или таймаут —
память (код 1 — файл репозитория): лишний вызов судьи дешевле пропуска. Места каталога настроек и
`CLAUDE_CODE_REMOTE_MEMORY_DIR` git не проверяются. Путь цели и места памяти (`_memory_places`) сравниваются в двух
формах — как написаны и через `realpath` (`_forms`), а `memory/` проекта, если ссылка она или `projects/<проект>`, — и
разрешённой целью: каталог настроек под dotfiles судится и при записи через ссылку, и при записи прямо в её цель. Запись
через ссылку вне каталога настроек ловит `realpath` пути цели. Ссылка на файл внутри `rules/` или `memory/` ловится
только записью через неё. MCP — `guard_memory.is_memory_mcp`: слово из `WRITE_VERBS` в имени инструмента при слове из
`MEMORY_WORDS` в имени сервера; иначе `guard_memory._writes_memory` — в имени инструмента глагол записи перед словом
памяти не дальше чем через одно слово или сразу после него (слова по порядку — `guard_memory._words`). Корпус настоящих
и ложных имён — `MemoryMcpNameTest.CORPUS` в `tests/test_guard_memory.py`. Дешёвая ошибка — ложное срабатывание: лишний
вызов судьи (не больше `common.MAX_DENIES` отказов на реплику) дешевле записи в память без согласия автора; поэтому
`set_memory_limit` и `clear_memory_cache` судятся как запись. Matcher хука в `hooks/hooks.json` пропускает `mcp__` с
`memor` или `remember` в любом регистре — надмножество `MEMORY_WORDS`. Содержимое судьи — `guard_memory.render_content`
из полей `Transcript` и текста записи (`guard_memory.write_text`): прежние реплики автора, реплика текущего хода и его
ответы — тем же текстом `prompts.author_context`, что блок `<author>` других судей (пределы —
`prompts.MAX_AUTHOR_FIELD`), сообщения агента и текст записи — до `guard_memory.MAX_FIELD` символов
(`guard_memory._clip`).

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

## Детектор зависимостей

Разбор — `depcheck.py`, без состояния. Кавычки и скобки отслеживает один автомат `depcheck._scan` со стеком
(`'`, `"`, `$'`, `(` — подстановка или подоболочка, `` ` `` — подстановка `` `…` `` в `"…"`, `A`/`a` — арифметика и
скобка в ней). Его используют
`heredocs` (однострочный, с пустым стеком; его же зовёт `comments`), `_segments` (внутри кавычки) и
`_close_paren` (конец `$(…)`, построчно, с пропуском тел heredoc до терминатора), через `_close_paren` — `_split`
(подстановка в `"…"` — часть слова) и `_quoted_substitutions`. Внутри `"$(…)"` кавычки вложены, как в bash.
Подстановка `` `…` `` внутри `"…"` кончается на первой неэкранированной обратной кавычке, кавычки в её теле границу не
меняют — так в bash 5.3 (`` "a `echo '`'` b" `` — ошибка: тело кончается на второй обратной кавычке;
`` "a `echo "b; c"` d" `` — одно слово). `_split` берёт такую подстановку в слово текстом (`_close_backtick`),
`_quoted_substitutions` — её тело командой. Отвергнуто: вкладывать кавычки в `` `…` ``, как в `$(…)`, — bash кончает
тело на неэкранированной обратной кавычке независимо от кавычек; держать `` ` `` в `"…"` обычным символом —
внутренняя `"` закрывает внешнюю, текст строки идёт командой (ложный отказ на
`` git commit -m "fix `echo "a; npm install x"` text" ``) или команда после строки уходит в кавычку (пропуск
`` echo "a `echo 'it"s'` c"; npm install x ``). Тело
heredoc, открытого в незакрытой кавычке (в том числе на строке продолжения подстановки), `_segments` оставляет
в сегменте текстом без разбора; его разбирает `_quoted_substitutions` → `_find`: тело `cat` — данные, тело
оболочки без скрипта — команды. Отвергнуто: держать один плоский уровень кавычек — внутренняя `"` в `"$(…)"`
закрывает внешнюю, текст строки идёт командой (ложный отказ на `gh pr create --body "$(printf "…")"`);
выбрасывать тело heredoc из сегмента — `_close_paren` тогда не находит терминатор или закрывает подстановку на
`)` из тела. Дешёвая ошибка детектора — пропуск: ложный отказ останавливает обычную работу агента; на
незакрытой конструкции (bash сам падает с синтаксической ошибкой) детектор может молчать.

Не добавление: пробный прогон (`depcheck._dry_run`: у npm — `_npm_dry_run`, у apt и apt-get — `_apt_parse`, у
aptitude — `_aptitude_simulates`, у brew — ещё `-n` и склейка с ним (`_brew_dry_run`; короткие флаги `brew install`
значения не берут, Library/Homebrew/cmd/install.rb, флаги со значением — `_BREW_VALUE_FLAGS`), у прочих — слово
`_DRY_RUN` среди слов команды; у запускателей — `npx`,
`corepack`, `python -m`, `… run` — проверяется вложенная команда), пакеты своего workspace (`_LOCAL_PROTOCOLS`:
`workspace:`, `link:`, `portal:`, `file:`; `pnpm --workspace`) и `--path` у cargo и bundle. `corepack` и
`npx`/`bunx`/`pnpx` (`_NPX`) разбираются как запуск следующей команды (версия `@…` у имени снимается). `bash +c '…'`
исполняет строку, как `-c` (проверено на bash 5.3).

Значение флага пробного прогона разбирается у npm и apt — там, где менеджер принимает его словом. npm (nopt) берёт
`true`/`false` следом за булевым флагом или после `=` (иное после `=` — отдельное позиционное слово, флаг — истина),
`--no-` в любом регистре обращает значение при каждом повторе, имя сокращается до `--dr`, действует последний флаг
(проверено `npm config get dry-run …` на npm 11.16). apt и apt-get (CommandLine apt, apt-pkg/contrib/cmndline.cc):
имя без учёта регистра, значение — после `=`, остаток склейки, следующее слово без `-` или слово перед `-` имени
(`--no-simulate`, `--yes-simulate`); булево — StringToBool (`_apt_bool`: `no|false|without|off|disable`,
`yes|true|with|on|enable`, целое C 0 или 1, пустая строка — ложь); не булево после `=` или перед `-` — ошибка
разбора, apt не выполняется — не добавление; `--no-act` — собственное имя пробного прогона, не отрицание. Флаг
уровня apt (IntLevel: `-q`, `--quiet`, `--silent`) съедает значение, если оно целиком целое strtol по основанию 10
(`depcheck._apt_level`: после `=`, остаток склейки `-q2` или следующее слово без `-`, `apt-get -q 2 install jq`);
иначе уровень растёт без значения, слово остаётся операндом (`-q 2x` — `2x` подкоманда); не целое после `=` — ошибка
разбора, не добавление. `_apt_parse` возвращает слова команды без съеденных значений булевых флагов и флагов уровня,
подкоманду и пакеты ищут по ним (`apt-get -s no install jq` — установка). Значения флагов из `_APT_VALUE_FLAGS` (в том
числе `--planner`, `--comment`, `-S`/`--snapshot`, `--with-source`, `--cli-version` apt 3,
apt-private/private-cmndline.cc) в словах остаются: их пропускает `_subcommand`. Восьмеричное целое в `_apt_bool`
читается по основанию 10: у значений 0 и 1 результат тот же, прочие не булевы. У прочих
менеджеров флаг без значения: `false` следом — позиционное слово (`pip install --dry-run false x` — пробный прогон),
`--dry-run=false` и `--no-dry-run` — ошибка разбора (optparse у pip, clap у cargo и uv, cleo у poetry, symfony
console у composer; у `pnpm add` и `pnpm install <пакет>` 12.10 `--dry-run` нет — ошибка в любой форме). Дешёвая
ошибка — пропуск: команда с `--dry-run` у неизвестного разбора считается пробной. Отвергнуто: один разбор
`--dry-run false` для всех менеджеров — у pip, cargo, bun это пробный прогон с пакетом `false`, вышел бы ложный
отказ.

## Манифесты

Разбор — `manifests.py`, без состояния и ввода-вывода. `manifests.kind(path)` — вид манифеста по имени файла,
сравниваемому до конца строки (`\Z`): `requirements.txt` с переводом строки в конце имени — не манифест, поэтому имя
манифеста перевода строки не содержит (возврат каретки — может, версию HEAD такого файла `head_names` читает отдельным
`git cat-file blob`);
`manifests.names(kind, text)` — имена внешних зависимостей, `None` — текст не разобран (JSON — `json`, TOML — `tomllib`,
отсюда Python 3.11+; requirements, `go.mod`, `Gemfile` разбираются построчно и `None` не дают). Нормализация: PyPI — PEP
503 (`manifests._pep503`), Composer — нижний регистр без платформенных пакетов (`_COMPOSER_PLATFORM`), crates.io —
`_crate`; npm, Go, RubyGems — как записаны. Транзитивные в `names` не входят: строки `// indirect` в `go.mod`
(`_GO_INDIRECT`) и весь сгенерированный файл требований (`_generated`: заголовок `_GENERATED_HEADER` в начальном блоке
комментариев или аннотация `_VIA` где угодно). `build-system.requires` в `pyproject` без стандартных бэкендов
`_BUILD_BACKENDS` (руководство PyPA «Choosing a build backend»); прочее в `requires` — зависимость.
`manifests.known_names(kind, text)` — старая сторона сравнения: `names` вместе с транзитивными (`_KNOWN`); ставшая
прямой транзитивная зависимость и пакет, уже перечисленный в сгенерированном файле, не новые.
`manifests.own_name(kind, text)` — имя пакета самого манифеста (`_OWN`: `name`, `project.name`, `tool.poetry.name`,
`package.name`, `module`; у requirements и Gemfile `None`): оно не внешнее. Местные источники `names` отбрасывает:
`[tool.uv.sources]` с `workspace` или `path` (список — если местный хоть один), `gem` с `path:`/`:path =>` и гемы блока
`path … do … end` (`_GEM_PATH`, `_GEM_PATH_BLOCK`; вложенность в блоке — по `do` в конце строки, ключевому слову блока в
начале строки (`_RUBY_KEYWORD_OPEN`: `if`, `unless`, `case`, `begin`, `while`, `until`, `for`, `def`, `class`,
`module`), кроме блока в одну строку с `end` в конце (`_RUBY_LINE_END`), и `end` в начале строки; лишнее открытие
дешевле недосчёта: первое — пропуск гемов после блока, второе — ложный отказ на местный гем), `replace x => ./…` в
`go.mod` (`_GO_LOCAL_PATH`). Корпус настоящих манифестов — `tests/fixtures/manifests/`, ожидания — `CORPUS` в
`tests/test_manifests.py`.

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
  файле, но не в HEAD — версии источника его покрывают. К версиям `_REPO_REFS` тот же вызов добавляет стороны
  конфликта индекса `_INDEX_STAGES` (`:1:./<имя>`, `:2:`, `:3:` — база, наша, их). git пишет их при конфликте
  любой операции, в том числе `git stash pop|apply`, `git merge --squash`, `git cherry-pick -n`, которые ни
  `MERGE_HEAD`, ни `CHERRY_PICK_HEAD` не оставляют, и держит до `git add`/`git rm` файла;
  `git checkout --ours|--theirs` их не снимает. Стороны берутся из индекса, не из маркеров конфликта в файле:
  маркеры пишет любой, кто правит файл, в том числе агент (обход в два шага: `Write` с маркерами пропускается
  как «не разобран», затем разрешение с новым именем), а стадии пишет механизм слияния git, одним способом для
  любой операции; `stash@{0}`, `ORIG_HEAD`, `SQUASH_MSG` отвергнуты — у каждой операции свой след, у
  `cherry-pick -n` следа нет. Сторонам доверяется, как источнику незавершённого merge: конфликт `git stash pop`
  stash, созданного в сессии, тоже проходит — возраст ref сверяет только `restored_names` для команды; стадии,
  как и `MERGE_HEAD`, агент может записать сам (`git update-index --index-info`). Такой пропуск дешевле ложного
  отказа на WIP автора. Чистые `git stash pop`, `git apply`, `git checkout <ref> -- <манифест>` в `_REPO_REFS`
  не попадают (после команды `Bash` ref сверяет `restored_names`, ниже). Что осталось,
  сверяется с именами других манифестов того же реестра и пакетами самого проекта
  (`manifest_watch.project_names`, зовётся лениво через `judge_tool._project_names`, в пределах
  `SNAPSHOT_BUDGET`). Файлы `_LEGACY` (`setup.py`, `setup.cfg`, `Pipfile`, реестр PyPI;
  `manifest_watch.source_kind`, `_known`) дают имена только из деревьев git — версии HEAD (`_tree_names`, ниже) и
  ref до начала сессии (`restored_names`); файлы `_LEGACY` рабочего дерева имён не дают ни правке, ни снимку перед
  командой. Их правку не проверяет никто, поэтому имя из рабочего дерева открывало бы обход в два шага: агент
  пишет пакет в любой `setup.py` (неотслеживаемый, в подкаталоге), затем тот же пакет проходит в `pyproject.toml`.
  Отвергнуто: стадия 0 индекса (её пишет `git add` агента), стороны конфликта и `_REPO_REFS` для `_LEGACY` (лишние
  вызовы git ради редкого случая). Цена — ложный отказ на перенос из незакоммиченного `setup.py` и из любого
  `setup.py` вне git (`README.md`, «Известные ограничения»). `_setup_py` — `ast`, строковые литералы `_SETUP_KEYS`
  (`install_requires`, `setup_requires`, `tests_require`, `extras_require`) аргументов и ключей словаря с
  подстановкой присваиваний уровня модуля и `+` (`_literals`: обход стеком, каждый узел и каждое имя — один раз, время
  линейно по размеру дерева при любом ветвлении ссылок имён и циклах присваиваний; глубина не ограничена),
  `SyntaxWarning` разбора подавлен (хук stderr не пишет);
  `_setup_cfg` — `configparser`, `[options]` и `[options.extras_require]` без `file:`; `_pipfile` — ключи таблиц,
  кроме `_PIPFILE_NOT_PACKAGES`; строки требований разбирает `manifests.known_names("requirements", …)`. Как
  манифест эти файлы не проверяются: их имена только известные. Новые виды манифестов — работа
  `manifests.py`; `setup.py` — код, точный разбор невозможен: литералы через `ast` переоценивают известные
  (дешёвая ошибка — пропуск), требования, собранные кодом, не видны — остаётся ложный отказ. В git добавляются
  имена манифестов и файлов `_LEGACY` версии HEAD — `_tree_names(root, "HEAD", deadline)`:
  `git ls-tree -r -z --name-only --full-tree` и `git cat-file --batch`, общий код с `restored_names`; манифестов
  в дереве больше `MAX_MANIFESTS` — `Unavailable`, нет HEAD — имён нет. Член npm workspace, ссылающийся на
  соседа, которого ещё нет, — новое имя: точного знания нет ни в одном файле, а эвристика «тот же scope и `*`»
  пропускала бы явное добавление. Реестр вида — `manifests.registry` (`_REGISTRY`): package.json — npm, composer.json —
  packagist, pyproject и requirements — pypi, cargo — crates.io, gomod — go, gemfile — rubygems; сравнение
  по реестру, не по виду: имя из `requirements.txt` не новое в `pyproject.toml`. Старый не разобран и версии в
  репозитории нет, новый не разобран, файл больше
  `MAX_MANIFEST_BYTES`, манифесты проекта не перечислить или их больше `MAX_MANIFESTS` (сообщение «не
  сравнён с другими манифестами проекта») — `manifest_watch.Unavailable`: предупреждение
  и `skipped` в журнал. Отказ — `deny_output(MANIFEST_REASON)`, в журнал `deny-dep` хука `MANIFEST_HOOK`
  (`manifest`) с `tool`, `added` и длиной и SHA-256 входа инструмента.
- Команда `Bash`: `judge_bash`, если команда пакет не добавляет, зовёт `snapshot_manifests`; команда с маркером
  (`manifest_watch.has_marker` — по семантике `depcheck`: `PLANKA_DEP_OK=1` ведущим присваиванием команды хоть одного
  сегмента; комментарий и аргумент не маркер, внутри `bash -c "…"` маркер не виден) не снимается. `manifest_watch.take`
  берёт список `list_manifests` — `git ls-files -z -c -o --exclude-standard`, вне git обход `_walk` (в обоих режимах без
  `FOREIGN_DIRS`, куда входит `snapshot.IGNORED_DIRS`), не больше `MAX_WALK_FILES` файлов, — не больше `MAX_MANIFESTS`
  манифестов и на каждый пишет `[size, mtime_ns, имена с транзитивными или None, имя пакета манифеста или None]`:
  содержимое не хранится. Если команда возвращает файлы из ref (`manifest_watch.command_refs`), в снимок идёт поле
  `known` — `{реестр: имена}` манифестов и файлов `_LEGACY` ref, созданных до начала сессии (`restored_names`,
  ниже). Файлы `_LEGACY` рабочего дерева в снимок не входят: их имена после команды берутся только из версии HEAD
  (`project_of` в `compare`). Снимок с полями `cwd` (каталог команды до неё) и `known` `store` кладёт
  в `state/<session>.manifests.json` под `tool_use_id` и там же удаляет записи старше `ENTRY_TTL`. Сбой снимка —
  `common.warn_once` с ключом `manifest-snapshot` и `skipped` в журнал на каждую команду.
- После команды — `judge_tool.check_command_manifests` на `POST_EVENTS`: `manifest_watch.pop` забирает снимок; пути
  вывода генераторов requirements (`generated_requirements`) `resolve` разрешает от `cwd` снимка и от `cwd` после
  команды (`cd` внутри неё); `compare` — смена режима git/walk — `Unavailable`; манифест с тем же размером и mtime
  пропускается, новый сравнивается с пустым, не разобранный до или после — в список предупреждения и `skipped`. Имя,
  объявленное в любом манифесте того же реестра в снимке, в `known` снимка (ref до начала сессии) или пакет самого
  проекта в снимке и в манифестах после команды, не новое: `mv`, `cp`, `git mv`, член workspace, возврат работы автора.
  Остальное решает `fresh_names` с версиями `_REPO_REFS` и сторонами `_INDEX_STAGES`; в git остаток сверяется ещё с
  именами манифестов и `_LEGACY` версии HEAD (`project_of` в `compare`: дерево читается один раз и лениво, только если
  что-то осталось). Новые имена — `block_output(COMMAND_REASON)`: правка уже в файле, хук не знает, чья она (`git apply`
  патча, ref моложе начала сессии), поэтому текст велит спросить автора, не откатывать вслепую и откатывать только свою
  правку; в журнал `block-dep` с числами `manifests` и `added`. Сбой `manifest_watch.pop` (`OSError`) — предупреждение и
  `skipped` в журнал; сбой записи журнала глотается.

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
--full-tree` — пути манифестов и файлов `_LEGACY` (`source_kind`, не больше `MAX_MANIFESTS`) и `git cat-file --batch`
их блобов (не больше `MAX_MANIFEST_BYTES`) — `known_names` по реестру (`_tree_names`). `start` `None`, ref моложе
или не найден — имён нет, сравнение блокирует, как без ref. Срок вышел, манифестов в дереве ref больше `MAX_MANIFESTS`
или git не прочитал дерево — `Unavailable`: `snapshot_manifests` снимок не сохраняет, предупреждение раз на сессию
(`manifest-snapshot`) и `skipped` в журнал, команда идёт без проверки. Сомнение решается пропуском
(`plugin/rules/heuristics.md`) по правилу «ошибка хука — пропуск, не блокировка»: блок после `git stash pop` автора без
имён ref был бы ложным. `cat-file` ref вне git-корня (`_old_trees`, ответа нет) — «ref нет», не сбой. Время ref — время
коммиттера: поддельная дата и ref прошлой сессии агента проходят как работа автора —
`context/deferred/stash-restore-vs-agent-edit.md`.

Генератор requirements (`manifest_watch._is_generator`) узнаётся по словам сегмента `depcheck._command` приватными
помощниками `depcheck`: `_subcommand`, `_after_flags`, `_python_module`, `_PIP`, `_GLOBAL_FLAGS` и наборы флагов;
файлы вывода — цели перенаправлений stdout (`depcheck._split`, `depcheck._REDIRECT`) и значения `_OUTPUT_FLAGS`.
Правка этих помощников `depcheck` — правка `_is_generator`, её держит `GeneratedRequirementsTest` в
`tests/test_judge_tool.py`. `depcheck._basename` приводит имя с суффиксом Windows к нижнему регистру
(`PIP.EXE` — `pip`). Файл, который пишет генератор (`pip freeze`, `uv export`, `poetry export` и т.п.),
перечисляет транзитивные пакеты — их выбрал не агент, поэтому файл в сравнении после команды не проверяется. Дешёвая
ошибка здесь — пропуск: генератор, направленный в рукописный файл, снимает с него проверку. `tee` сразу за генератором
(`pip freeze | tee requirements.txt`) узнаётся (`_tee_outputs`); конвейер с фильтром (`pip freeze | sort >
requirements.txt`) нет — перенаправление в другом сегменте, файл проверяется.

## Состояние и журнал

Каталог данных — `$CLAUDE_PLUGIN_DATA`, без него `.data/` в корне плагина (`common.data_dir`).

- `state/<session>.json` — счётчики отказов; `state/<session>.warned.json` — выданные
  однократные предупреждения (`common.warn_once`); `state/<session>.snap.json` — снимок дерева
  текущей реплики (`snapshot.store`, поле `format` = `snapshot.FORMAT`, 2; блоки режима walk — строкой
  `snapshot._pack_dirs`; поле `checked` — отметил ли `Stop` снимок проверенным, `snapshot.mark_checked`; снимок
  без поля считается проверенным); `state/<session>.start.json` — начало сессии `{"start": секунды}`
  (`manifest_watch.mark_start`, пишется один раз); `state/<session>.debug.json` — неудачи команд (`counts`) и
  отметки показа модуля по агентам (`shown`); `state/<session>.manifests.json` — снимки манифестов перед
  командами `Bash`, `{tool_use_id: {root, mode, ts, cwd, files: {путь: [size, mtime_ns, имена или
  None, имя пакета или None]}, known?: {реестр: [имена]}}}` (`manifest_watch.store`, `manifest_watch.pop`;
  запись не той формы `pop` отбрасывает; `known` — имена манифестов и файлов `_LEGACY` ref до начала сессии);
  `state/<session>.snapfail.json` — неудачи снимка дерева `{корень: причина}` (`snapshot.mark_failed`,
  `snapshot.failure`).
  Имя — `common.safe_name`. Запись атомарная (`common.atomic_write_json`: временный `.tmp-*` и
  `os.replace`); чтение — `common.read_json` (нет файла, битый JSON или значение не того типа —
  пустое значение); чтение и запись счётчиков, предупреждений, неудач команд, снимков манифестов, неудач снимка дерева,
  перепривязка и отметка снимка дерева (`snapshot.carry_over`, `snapshot.mark_checked`) — под `fcntl.flock` на
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
`os.fsdecode`); `cwd` меняется после `cd` агента, `CLAUDE_PROJECT_DIR` — нет. Каталога нет (удалён) — git
спрашивается из ближайшего существующего предка (`common._existing_dir`). Корень отделяется от вершины, только если
сходятся три условия: вершина — домашний каталог пользователя (`os.path.expanduser("~")`) или его предок (сравнение
через `realpath`, `common._home_or_above`), `CLAUDE_PROJECT_DIR` ниже вершины и под ним репозиторий не отслеживает ни
одного файла (`git --literal-pathspecs ls-files -z --cached -- <путь>` пуст). Тогда корень — сам `CLAUDE_PROJECT_DIR`:
репозиторий выше — не репозиторий проекта (домашний каталог-репозиторий dotfiles, часто с
`status.showUntrackedFiles=no`, над проектом без своего git). Под любой другой вершиной — монорепозиторий: новый
подкаталог (`mkdir packages/new; cd packages/new; claude`) и подкаталог из `.gitignore` получают корнем вершину,
иначе соседи workspace не видны проверке манифестов, а `remind` и `judge_stop` не находят `CLAUDE.md` корня. Сбой или
таймаут `ls-files`, ошибка сравнения вершины с каталогом (`common._same_dir`) — вершина. Для `cwd` правило не
действует: `cwd` после `cd` агента в новый неотслеживаемый подкаталог сдвинул бы корень между `UserPromptSubmit` и
`Stop`. Дом, совпадающий с вершиной рабочего репозитория (контейнер, `HOME=/app`), неотличим от дома-репозитория
dotfiles: новый подкаталог такого репозитория получает корнем себя (README, «Известные ограничения»). Оба вызова
git — в одном сроке `GIT_ROOT_TIMEOUT`. Отвергнуто: признак «под каталогом не отслеживается ни
одного файла» без условия о доме (новый и исключённый подкаталог монорепозитория становился корнем) и строгий признак
по `status.showUntrackedFiles` (dotfiles без этой настройки остались бы с корнем `~`).

Окружение `common.git_env(root)` — `GIT_CEILING_DIRECTORIES` с родителем `realpath(root)` первым (унаследованное
значение — после): git ищет репозиторий в корне и ниже, но не выше, и корень, отделённый от репозитория выше, остаётся
вне git. Для корня-вершины окружение ничего не меняет: `.git` — в самом корне. С ним идёт каждый вызов git о проекте:
`snapshot._git`, `comments._git`, `guard_memory._repository_file` и `manifest_watch._git` (`root` — обязательный
именованный аргумент). Без него — только `common.project_root`, который вершину и ищет, и `manifest_watch.head_names`
для файла вне корня (`manifest_watch._inside`, символические ссылки разрешены) или при `root` None (проект неизвестен):
такой файл читает версии своего репозитория. `judge_tool.judge_manifest_edit` считает корень один раз
(`functools.cache`) и отдаёт его и `head_names`, и `_project_names`.

`remind.take_snapshot` не переснимает снимок того же корня и формата `FORMAT` с `checked: false`: `snapshot.carry_over`
перепривязывает его `prompt_id` к новой реплике, база и HEAD остаются со старта первой реплики без `Stop` (на прерванную
реплику `Stop` не приходит). Корень, снимок которого в сессии не удался (`snapshot.failure`), не снимается.
`judge_stop.main` после `Stop` без блока зовёт `release_snapshot` → `snapshot.mark_checked`; блок (`judge` вернул
`True`) и исключение в `judge` снимок не отмечают. Ошибка судьи, лимит отказов, нет рубрики, неопределимые изменения —
отмечают: иначе при постоянной ошибке судьи база растёт без конца, а «не определить» предупреждало бы на каждом `Stop`.
Блок не отмечает: `Stop` после блока приходит снова, когда агент закончит, а если автор прервёт реплику после блока,
правки проверит следующая реплика. Формат меняется аддитивно, поэтому `FORMAT` не поднят: снимок без поля `checked`
перезаписывается, как прежде. Неудача снимка запоминается для корня, а не гасит только предупреждение: снимок (`git
status --untracked-files=all` или обход) иначе повторялся бы на каждом `UserPromptSubmit` и съедал до срока каждую
реплику; повтор через N реплик отвергнут — для него нет ясного условия.

## Комментарии изменённых файлов

`comments.extract(root, relpaths, base, sub_bases, deadline)` возвращает `(lines, truncated, unknown,
late)`: строки «путь: комментарий», обрезано ли по `MAX_LINES`/`MAX_BYTES`, файлы без известного
синтаксиса, файлы, не разобранные к сроку. Файл разбирается целиком (`comments._comments` по
`comments._SYNTAX`), в вывод идут комментарии строк из `comments._added_lines` — номера по заголовкам
`@@` `git diff -U0` против `base` (HEAD на старте реплики) на отслеживаемые файлы (`comments._diff`); пути идут
аргументами частями не длиннее `comments.MAX_ARG_BYTES` (24 000 байт: ARG_MAX и 32 767 знаков командной строки
Windows), чтобы E2BIG не превращал все файлы в «целиком». `--text` — чтобы файл с атрибутом `-diff` или
`binary` (из `.gitattributes` проекта или `core.attributesFile`) давал строки, а не «Binary files differ»;
`--find-renames` — чтобы переименованный файл сравнивался со старым путём при любом `diff.renames`.
Когда среди путей есть новые против `base`, `_added_lines` делает ещё один `git diff --diff-filter=R` всего
дерева, без путей в командной строке, и берёт из него пары для новых путей; переименование без правок — пустое
множество строк. Пару git находит по сходству (порог git по умолчанию — 50 %); файл без пары, в том числе после
`mv` без `git add`, — целиком.

Файл подмодуля или вложенного репозитория-не-подмодуля — против базы своего репозитория
(`comments._select`): подмодули — записи `160000` индекса (база из `sub_bases`, без записи — текущий
HEAD подмодуля), вложенные репозитории — ключи `sub_bases`; файл достаётся самому глубокому
репозиторию, чей путь — его префикс. Без `base`, для неотслеживаемого файла и при сбое diff — весь
файл.

Хвосты строки перед местом разбора (`comments._heredoc_ok`, `_docstring_start`, `_after_print`) просматриваются
назад через `comments._back`, не регуляркой по срезу префикса: строка из многих таких мест разбирается за
линейное время.

Срок разбора `_comments` проверяет раз на `_DEADLINE_EVERY` шагов, шаг — строка или позиция внутри строки: и одна
длинная строка (минифицированный бандл) не уходит за срок. BOM UTF-8 в начале файла снимается; файл с BOM UTF-16 или
UTF-32 декодируется по нему (`comments._read`), номера строк пересчитываются по байтам 0x0A, как их считает git (в
UTF-16 этот байт входит и в другие знаки). Heredoc Ruby и Perl (`rb`, `pl`, `pm`, `Rakefile`, `Gemfile`;
`comments._RUBY_HEREDOC`: `<<ID`, `<<-ID`, `<<~ID`, идентификатор и в кавычках — там любые знаки, кроме этой кавычки,
`<<'----END----'`) — данные. Сдвиг или добавление от heredoc отличает `comments._heredoc_ok`: `<<` сразу после слова или
закрывающей скобки — сдвиг (`1<<BITS`, `a[0]<<X`, `print $fh<<EOF`); в Perl исключение (`comments._tight_perl`):
вплотную после `print`, `printf`, `say`, `die`, `warn` (`print<<EOT`) и после дескриптора из заглавных и `_` за `print`,
`printf`, `say` (`print CSS<<EOF`) — heredoc; слово с `$`, `@`, `%`, `&`, `>` впереди (`$fh<<`) не считается. После
пробела за скобкой, кавычкой или переменной (`$a`, `@a`) — сдвиг; в Perl исключение — дескриптор после `print`,
`printf`, `say` (`$fh`, `STDOUT`, `STDERR`, слово ищет `comments._after_print`; блок `{$fh}`, `{$DB::OUT}`,
`{$self->{fh}}`, `{*STDOUT}` — и вплотную, `print{$fh}<<EOF`, — ищет `comments._print_block` назад не дальше
`_BLOCK_LOOKBACK`, 256 знаков, чтобы разбор строки оставался линейным): `print $fh <<EOF` — heredoc, в Ruby это сдвиг;
за прочим словом — heredoc, только если идентификатор с `-`, `~`, в кавычках или с заглавной буквы (`print <<EOF`; в
Perl ведущие `_` идентификатора не в счёт, `<<_EOUSAGE_`), иначе добавление (`a <<b`, `puts <<eof`); после другого знака
(`=`, `(`, `,`) и в начале строки — heredoc. Дешёвая ошибка — heredoc, принятый за сдвиг: его тело читается как код и
показывает судье лишние строки.

Heredoc Ruby, Perl и Terraform без строки-терминатора до конца файла — не heredoc: `_comments` повторяет
разбор `_parse`, запретив открывать heredoc в этих позициях (`banned`); всего разборов не больше
`comments._MAX_REPARSE` (8) — каждый следующий снимает хотя бы одно открытие; тело такого heredoc читается как
код. Heredoc оболочки (`depcheck.heredocs`) так не повторяется. Так же откатываются строка с подстановками и
многострочный литерал без закрытия до конца файла (строка из `_Syntax.strings`, оператор-кавычка Perl, литерал
`%` Ruby, многострочная регулярка Ruby и Perl, сигил Elixir): позиция открытия идёт в `banned`, литерал
читается однострочным.

Регулярные выражения, slashy-строки и JSX — поля `comments._Syntax` `regex` и `jsx`. `regex="js"` (js, jsx, mjs, cjs,
ts, tsx, mts, cts) и `regex="groovy"` (groovy, gradle): `/` в начале выражения (`comments._expr_start`) — литерал, `//`
и `/*` раньше — комментарии. Начало выражения — по предыдущему токену: назад через пробелы знак из `_EXPR_AFTER` (не
`)`, `]`, кавычка, слово; `++` и `--` — постфикс) или слово из `_EXPR_KEYWORDS` не после `.`; слово просматривается не
дальше `_KEYWORD_MAX` + 1 знаков — время линейное. В начале строки решает `fresh`: в JS и Perl — по концу прошлой строки
кода (`a\n/ b` — деление; блочный комментарий в конце строки и строка из одних комментариев его не меняют), в Groovy и
Ruby всегда начало (конец строки завершает оператор). Литерал — `_JS_REGEX` (escape, класс `[...]`, флаги) или `_SLASHY`
до конца строки, без отката; незакрытый в строке кончается с ней: многострочная slashy-строка дальше читается как код,
мнимая регулярка не прячет следующие строки. Dollar-slashy Groovy `$/…/$` — многострочный литерал с escape `dollar`
(`comments._close`: `$$`, `$/` — escape). `jsx=True` (js, jsx, tsx; в ts `<T>(x) => x` — обобщение) — `<` в начале
выражения, за которым `comments._jsx_opens` видит тег (`>` фрагмента или имя, за ним `>`, `/`, `{`, атрибут или конец
строки; `<T,>`, `<T extends X>`, `<T = X>` — обобщение), открывает разметку: `comments._markup` ведёт стек `ctx` (`text`
— дети элемента, `tag` — открывающий тег, `close` — закрывающий, `code` — код в `{…}` со счётчиком скобок, его разбирает
`comments._code`). Текст между тегами — не код; в теге `//` и `/* */` — комментарии, строки атрибутов — многострочные
без escape. Тег или текст, не закрытые к концу файла, `_parse` возвращает в незакрытых, и `_comments` повторяет разбор,
запретив открывать их (`banned`), как heredoc без терминатора. Шаблонная строка JS и строки Ruby `"…"`, `` `…` `` —
элемент `tpl` того же стека (`comments._template`): кавычка закрывает строку, `${` и `#{` открывают `code` подстановки;
комментарий в подстановке — настоящий, кавычки в ней строку не закрывают. `<<` в коде JSX — сдвиг, не тег. Остаток —
`context/deferred/comment-syntaxes.md`.

`regex="ruby"` и `regex="perl"`: `/` и `%` (Ruby) — литерал по `comments._literal_opens`: в начале выражения (не
после `}` — это элемент хеша), после слова из `comments._TERM_WORDS` (`if`, `unless`, `and`, `or`, `when`; в Perl ещё
`split`, `grep`, `map`, `join`, `push`, `eq`, `cmp` и подобные — и вплотную, и перед пробелом: `split/\s+/`,
`split / /`) или аргументом вызова — после слова через пробел и вплотную к следующему знаку, кроме `=`: в Ruby `/=`
после слова — всегда присваивание с делением (так лексер Ruby: `/=` не в начале выражения — `tOP_ASGN`; `foo /=#/` —
`foo /=` и комментарий), в Perl — только `=` с пробелом за ним (`print /=#/` — регулярка, `$a /= 2` — присваивание);
`puts %w(a)` — литерал; после числа, переменной (`$a /2`) и локальной переменной Ruby
(`names` из `comments._ruby_locals`: присваивание `a = …`, `a ||= …`, `a, b = …`, параметры метода в скобках и
блока после `do` и `{`, списки параметров не длиннее 256 знаков; не после `.`) — деление; `:/` и `:%` Ruby —
символы. Буквы за закрывающим разделителем литерала — флаги (`comments._FLAGS`), не оператор-кавычка: `/a/s, @x` —
не `s,…,`. Регулярка-аргумент, не закрытая в
строке, кончается с ней; в начале выражения — многострочная. Операторы-кавычки Perl (`comments._PERL_QUOTE`: `m`,
`s`, `qr`, `tr`, `y`, `q`, `qq`, `qw`, `qx` не после знака слова, сигила, `->`, `::`; разделитель — вплотную любой
знак не слова, кроме закрывающих скобок, `;` и `=>`, через пробелы — `/` и открывающая скобка) и литералы `%`
Ruby разбирает `comments._quote_parts`: скобки вкладываются, escape — обратная косая; у `s`, `tr`, `y` две части,
вторая — тем же разделителем или своими скобками через пробелы (`comments._second_part`); многострочный литерал
— состояние разбора с вложенностью (`comments._close_nested` с escape). Многострочная регулярка (`m`, `qr`, `s`,
`%r`, `/…/`, сигил `~r`): `#` после пробела в ней — комментарий режима /x (`comments._XRE_COMMENT`); номера таких
строк вывода `_parse` копит в `xre` и снимает их, если за закрытием литерала (у `s`, `tr`, `y` — за второй частью)
нет флага `x`; флаги не видны (вторая часть `s{…}` на следующей строке) — строки остаются. Вторая часть `s{…}{…}` в
`{}`, не закрытая в строке, — литерал со вложенностью (`then="code"`), его текст копится в `body`; при закрытии с
флагом `e` текст разбирается как код Perl рекурсивным `_parse` (`level`, не глубже `comments._MAX_CODE_DEPTH` = 4:
каждая строка разбирается не больше пяти раз, разбор линеен), без `e` — строка. Heredoc Perl — и с пробелами
перед идентификатором в кавычках (`<< "DONT"`, `comments._RUBY_HEREDOC`; в Ruby `x << "a"` — добавление). `$"`, `$'`, ``
$` `` — переменные; строка `__END__` или `__DATA__` — дальше данные, кроме POD. Строки Ruby, Perl, Julia, PowerShell
многострочные (PowerShell: escape `` ` ``, here-string `@"…"@` и `@'…'@`). `sigil` — сигилы Elixir (`~r/…/`, `~w(…)`, с
тройными кавычками), многострочные, без вложенности. `block_scalar` (yaml, yml) — `comments._yaml_block`: строка, код
которой кончается индикатором `|` или `>` после `key:` или `-` (тег и якорь перед ним не мешают), открывает блочный
скаляр; его строки — с отступом больше родителя и пустые. Индикатор один на строке (`run:` и `  |` строкой ниже) —
значение ключа прошлых строк: родитель — последний ключ стека `keys` левее индикатора, без него — `-1` (скаляр всего
документа). Ключ скаляра — на его строке (`comments._YAML_KEY`), у `- |` и у индикатора на своей строке — по стеку
открытых ключей `keys` (`comments._yaml_key`, `comments._yaml_parent`: последний ключ с колонкой не больше колонки `-`,
список без отступа — в колонке ключа); пространство имён снимается (`ansible.builtin.shell` → `shell`). Синтаксис
скрипта выбирает `comments._yaml_script_ext`: вход `script` под `with:` шага, чей `uses` — `actions/github-script`
(`comments._GITHUB_SCRIPT`; значения `uses` открытых отображений по колонкам — словарь `uses` в `comments._yaml_key`,
новый элемент `- ` снимает запись своей колонки), — JavaScript; скаляр под ключом из `comments._YAML_SCRIPT_KEYS`
(GitHub Actions `run`, GitLab `script`/`before_script`/`after_script`, Travis, Ansible `shell`/`command`/`cmd`, Compose
`command`/`entrypoint`, CircleCI, Buildkite, Drone, Azure `bash`/`pwsh`/`powershell`/`script`, Read the Docs
`build.jobs`) — shell; его строки без общего отступа разбирает этот синтаксис (`comments._yaml_script`, heredoc shell
— данные); под прочим ключом — данные.
Дешёвая ошибка — однострочный литерал: многострочный, принятый по ошибке, прячет комментарии до своего закрытия, поэтому
многострочными считаются только литералы в однозначном месте, а прочие кончаются со строкой.

Флаги стоят после литерала, поэтому решение о `#` в многострочной регулярке откладывается до закрытия: вывод строк
до него копится и снимается задним числом, а не держится второй проход по файлу. Отвергнуто: всегда считать `#`
комментарием (ложные строки у регулярок без /x) и никогда (теряет комментарии /x). Снятие строк при ошибочно
открытой регулярке теряет настоящие комментарии на всём её протяжении — поэтому вместе с ним закрыты найденные на
исходниках Perl core и Ruby 3.4 источники таких ошибок: `/=/` после слова, `split/…/`, `split / /`, флаги `/a/s,`
как оператор `s`, символ `:/`, heredoc `<< "ID"`. Замена `s{…}{…}e` разбирается кодом, только когда флаг `e` виден
при закрытии, — так решает и сам Perl (разделители ищутся до разбора кода).

Блочный скаляр YAML под ключом скрипта — shell, его комментарии пишет агент, и правила `rules/comments.md` к ним
применимы; содержимое прочих скаляров — текст (Markdown в `value: |` формы issue GitHub, `description: |` OpenAPI,
`documentation: |` анализатора Dart), где `# Заголовок` — не комментарий. Дешевле пропуск: скрипт под ключом не из
списка теряет комментарии у судьи, а ложный «комментарий» из Markdown может отклонить остановку агента. Отвергнуто:
разбор всех скаляров как YAML (ложные заголовки) и всех как данных (теряет скрипты CI); различение по содержимому
(неоднозначно: `# Errors` — и заголовок, и комментарий shell). Вход `script` под `with:` — данные действия, а не скрипт
шага: у `actions/github-script` это JavaScript (шаблонная строка с `# Заголовок` внутри — не комментарий), у прочих
действий — по их выбору; на корпусе (mldsa-native, `./.github/actions/setup-shell`) вход `script` — shell. Поэтому
JavaScript — только при `uses: actions/github-script` того же шага, прочие `script` под `with:` — shell. Отвергнуто:
все скаляры под `with:` — данные (теряет комментарии shell-входов локальных действий) и все `script` — shell (ложные
строки из JS flutter `release-tracker.yml`).

Прочие поля `comments._Syntax`: `nested` — блоки вкладываются, глубину считает `comments._close_nested`
(`/* */` kt, scala, swift, rs, dart; `{- -}` hs, `#= =#` jl, `(* *)` fs, `#[ ]#` nim, `#| |#` lisp;
`(*)` F# — оператор, `comments._block_opens`); `prefix` — символьный литерал со знаком комментария или
строки — данные (`$%` Erlang, `\;` Clojure, `?;` Emacs Lisp, `#\;` Common Lisp, `?#` Ruby и Elixir — за
ним не буква); `rem` — слово `REM` после указанных знаков открывает комментарий (vb, vbs, bat, cmd,
`comments._rem`); `line_block` — блок из целых строк с первой колонки, идёт в вывод целиком (`=begin`…`=end`
Ruby, POD Perl от `=слово` до `=cut`); `exdoc` — `@doc`, `@moduledoc`, `@typedoc` Elixir со строкой —
документация, в вывод; `heredoc="php"` — тело `<<<ID` до терминатора с отступом, за которым код той же
строки разбирается дальше. Правила маркера `comments._marker_ok`: `start` — только пробелы перед ним (`::`
bat), `word` — только в начале слова (sh, yaml, `Dockerfile`: и в строке `RUN` для shell, внутри слова — значение,
`ARG A=${B#x}`), `code` — не после `$`, `{`, `\`, `make` — в строке рецепта (с табуляции) как `word` и сразу за
префиксами `@`, `-`, `+` и пробелами вокруг них (`comments._MAKE_PREFIX`: make снимает их и отдаёт shell `# текст`,
`@# note` — комментарий), в прочих как `code` (mk, `Makefile`, `GNUmakefile`: рецепт уходит в shell,
`echo '#define X'` — значение; `CFLAGS = -O2#opt` вне рецепта — комментарий), `vim` —
`comments._vim_quote` (`"` в начале строки или после пробела без закрывающей кавычки до конца строки — комментарий,
иначе строка), `vim9` — `#` после пробела, не `#{`. Raw-строки `comments._raw_string`: ещё `swift` (`#"…"#`, закрытие —
кавычки и столько же `#`) и `nim` (`r"…"`, `ident"…"`, удвоенная кавычка — escape). Docstring Clojure и Emacs Lisp —
строка, `#_` Clojure — код: в вывод не идут.

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
