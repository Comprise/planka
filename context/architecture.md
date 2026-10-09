# Архитектура

planka — плагин Claude Code уровня пользователя: шесть скриптов хуков из `hooks/hooks.json` подмешивают
правила из `philosophy.md` и `rules/*.md` в контекст агента и отклоняют его действия через
вложенного судью-модель или детерминированные проверки. Пользовательское описание поведения —
`README.md`, разделы «Как это работает» и «Известные ограничения».

Плагин — каталог `plugin/`: маркетплейс отдаёт пользователям только его (`"source": "./plugin"` в
`.claude-plugin/marketplace.json`). Пути ниже, кроме `.claude-plugin/marketplace.json`, — от
`plugin/`, он же `CLAUDE_PLUGIN_ROOT` хуков.

## Компоненты

| Файл | Роль |
| --- | --- |
| `hooks/hooks.json` | регистрация хуков: `SessionStart` и `PostModelSwitch` → `model_watch.py`; `UserPromptSubmit` → `remind.py 1` и `remind.py 2` (по хуку на часть ядра); `PreToolUse` на `^(AskUserQuestion\|ExitPlanMode\|Bash\|Write\|Edit\|MultiEdit)$\|^mcp__` → `judge_tool.py`; `PreToolUse` на файловые инструменты записи и MCP-инструменты с `memor` или `remember` в имени в любом регистре (регулярное выражение — в `hooks/hooks.json`) → `guard_memory.py`; `Stop` → `judge_stop.py`; `PostToolUse` и `PostToolUseFailure` на `Bash` → `debug_watch.py` и `judge_tool.py`, на `^mcp__` → `judge_tool.py` |
| `planka/remind.py` | часть ядра с номером из аргумента (`part_number`) как `additionalContext`: деление по разделам (`split_core`, `core_parts`, `PARTS`, предел `CONTEXT_LIMIT`); в части 1 — снимок дерева (`take_snapshot`) и строка `NO_DOCS_LINE` об отсутствии `CLAUDE.md` (`remind_project`) |
| `planka/judge_tool.py` | судья вопроса (`judge_question`), плана (`judge_plan`), отказ на добавление пакета и на команду установки под сомнением (`judge_bash`, причины `DEP_REASON`, `DEP_DOUBT_REASON`) и снимок манифестов перед командой и вызовом MCP-инструмента (`snapshot_manifests`); отказ правке манифеста (`judge_manifest_edit`, `MANIFEST_REASON`); после команды `Bash` и вызова `mcp__*` — блок на новые имена в манифестах (`check_command_manifests`, `COMMAND_REASON`, `MCP_REASON`) |
| `planka/guard_memory.py` | судья записи в постоянную память: цель — `is_memory_path` (в том числе `CLAUDE.local.md` — `_local_memory`, импорты `@путь` — `_imported_files`), `is_memory_mcp`; содержимое — `render_content` |
| `planka/judge_stop.py` | фильтры «варианты» (`looks_like_options`), «готово» (`claims_done`) по последнему сообщению, «документация» (`docs_check`) по изменениям со снимка; один вызов судьи на шаги реплики (`turn_messages`, `prompts.Step`, до `prompts.MAX_TURN_CHARS`); отметка снимка проверенным после Stop без блока (`release_snapshot`, в `finally` `main`) |
| `planka/debug_watch.py` | счётчик неудач подряд одной команды `Bash` (`update`; хранятся последние `MAX_COUNTS` счётчиков и `MAX_SHOWN` отметок показа); с `REPEAT_THRESHOLD`-й неудачи — модуль `debugging.md` контекстом; команда-ответ с кодом 1 (`code1_is_answer`, сегменты строки — по дереву `shparse`) |
| `planka/model_watch.py` | модель сессии из `model` входа `SessionStart` и `to_model` входа `PostModelSwitch` в состояние (`store`); агенту ничего не выводит |
| `planka/common.py` | барьер, чтение входа, тексты правил и рубрика, транскрипт (`read_transcript`), модель и запуск судьи (`judge_model`, `stored_session_model`, `usable_model`, `run_judge`), лимит отказов, журнал, корень проекта (`project_root`) и окружение git о нём (`git_env`), классы путей, формат ответа (`run_hook`) |
| `planka/prompts.py` | обращение к модели `ADDRESS`, системный промпт, схема ответа `JUDGE_SCHEMA`, вопросы судье по видам проверки, сборка содержимого; шаг реплики `Step` и его текст для судьи (`render_step`, `turn_content`, код меток `step_tag`); код тегов блоков данных (`_fresh_code`) |
| `planka/planparse.py` | разбор плана на волны и задачи, владение файлами (`shared_files`) |
| `planka/manifests.py` | разбор манифестов: вид по имени (`kind`), имена внешних зависимостей с источником не по умолчанию (`names`), они же с транзитивными `go.mod` (`known_names`), имя пакета манифеста (`own_name`), шаблоны членов workspace корня (`workspace_members`) |
| `planka/manifest_watch.py` | манифесты, которые правит файловый инструмент, в том числе через ссылку и жёсткие ссылки (`edit_targets`; перечень манифестов проекта один на правку — `listing`), текст манифеста после правки (`edit_texts`, `check_edit`), новые имена (`fresh_names`, `edit_names`), имена версии HEAD и сторон конфликта (`head_names`), других манифестов проекта и манифестов и файлов `setup.py`, `setup.cfg`, `Pipfile` его версии HEAD (`project_names`, `_tree_names`, `source_kind`), снимок манифестов проекта и сравнение после команды (`take`, `compare`, `store`, `pop`), имена ref до начала сессии, откуда команда git возвращает файлы, и патчей `git apply`, не менявшихся с начала сессии (`command_refs`, `command_patches`, `restored_names`; начало сессии — `mark_start`, `session_start`), члены workspace корня (`_workspace`, `_member`) |
| `planka/depcheck.py` | вопросы детектора зависимостей над деревом `shparse`: добавляет ли команда Bash пакет (`dependency_add`), команда установки под сомнением (`dependency_doubt`), маркер `DEP_OK_MARKER`, heredoc (`heredocs`), сегменты и слова для `manifest_watch` (`_segments`, `_command`, `_split`); добавление по словам (`_is_add`, `_flat_add`), сомнение (`_doubt`, `_segment_doubt`) |
| `planka/shparse.py` | разбор текста команды, как его читает bash 5.3 в обёртке инструмента Bash: дерево команд, слов, подстановок с телом, heredoc и комментариев (`parse`, `walk`, `simple_commands`, `heredocs`); его читают `depcheck`, `debug_watch` и `comments` (файлы sh, bash и shell-скрипты YAML) |
| `planka/pkgmanagers.py` | семантика менеджеров пакетов над списками слов: флаги и их значения (`_takes_value`, наборы `_*_FLAGS`), подкоманда (`_subcommand`), имя программы (`_basename`), менеджеры (`_apt`, `_nix`, `_pip_add`, …), пробный прогон, справка, строки, которые команда исполняет (`_inline_script`, `_git_runs`, `_ssh_command`); текст команды не разбирает |
| `planka/snapshot.py` | снимок дерева (`capture`, `store`, `load`) и изменения с него (`changed_since`) в режимах git и walk; порог `MAX_FILES` грязных путей в git; перепривязка непроверенного снимка (`carry_over`, `mark_checked`), неудачи снимка (`mark_failed`, `failure`) |
| `planka/comments.py` | строки комментариев изменённых файлов для судьи документации (`extract`); sh и bash — по дереву `shparse` (`_shell_comments`) |
| `philosophy.md` | ядро правил; индекс «Модули» в конце |
| `rules/*.md` | модули правил, по файлу на область |
| `.claude-plugin/plugin.json` | манифест и `userConfig`: `judge_model`, `comment_lang`, `doc_lang` |
| `.claude-plugin/marketplace.json` | маркетплейс `planka` в корне репозитория, источник плагина — `./plugin` |

## Контракт кода с текстами правил

Код ищет тексты правил по именам; переименование ломает хук без ошибки теста хука — хук пропускает
проверку с предупреждением. Настоящие тексты сверяет `tests/test_contract.py`.

- Разделы ядра берутся по заголовку `## <имя>` (`common.philosophy_sections`, `common.rubric`): `Решения` — рубрика
  вопроса, плана и фильтра «варианты»; `Планы` — рубрика плана; `Границы` — рубрика записи в память, её же называют
  `judge_tool.DEP_REASON`, `DEP_DOUBT_REASON`, `MANIFEST_REASON`, `COMMAND_REASON` и `MCP_REASON`. Пункт 4 «Решений»
  (самый правильный вариант есть в списке и идёт первым) — вопрос 1 `prompts._CHOICE_CHECKS`, пункт 7 называет там
  вопрос 4. Опоры рекомендации из пункта 4 «Решений» (у опоры назван источник)
  проверяет только фильтр «варианты» (`prompts._MESSAGE_CHECKS`, вопрос 6): судья `Stop` видит шаги реплики с вызовами
  инструментов. Судьи вопроса и плана шагов не видят: `judge_tool` вырезает этот пункт из их рубрики
  (`prompts.without_premises` по строке `prompts.PREMISES_ITEM`). Отвергнуто: исключение словами в вопросах — судья
  плана всё равно требовал источник опоры; проверка опор у судьи вопроса — выбор по предпочтению получал отказ.
- Модули берутся по имени файла (`common.rule_texts`, `common.rubric`): `planning`, `subagents`,
  `refactoring`, `design-patterns`, `heuristics` — план (`judge_tool.judge_plan`); `verification` — фильтр «готово»;
  `docs`, `comments` — фильтр «документация» (`judge_stop.judge`; судья видит сообщения, список файлов и
  строки комментариев, а не код, поэтому `design-patterns` и `refactoring` — только в рубрике плана);
  `memory` — запись в память (`guard_memory.main`); `debugging` — `debug_watch.MODULE`.
  `rules/dependencies.md` называют причины отказа и блока `judge_tool.DEP_REASON`, `DEP_DOUBT_REASON`,
  `MANIFEST_REASON`, `COMMAND_REASON` и `MCP_REASON`.
- Метки `{RULES}`, `{COMMENT_LANG}`, `{DOC_LANG}` заменяет `common.substitute` при каждом чтении: путь к `rules/`
  плагина и значения настроек; незаданный язык — `DEFAULT_LANG` (`ru`). Ссылка на модуль из ядра и модулей пишется как
  `{RULES}/<имя>.md`, строка `remind.NO_DOCS_LINE` — так же; `debug_watch.LINE`, `judge_tool.DEP_REASON`,
  `DEP_DOUBT_REASON`, `MANIFEST_REASON`, `COMMAND_REASON` и `MCP_REASON` подставляют `common.rules_dir()`: агент
  получает абсолютный путь к правилам плагина, а `rules/` проекта с ним не путается.
- Имена, которые код берёт, `tests/test_contract.py` выводит из кода сам (`_code_names`, через `ast`): аргументы вызовов
  `rubric`, `philosophy_sections`, `rule_texts` (через `common.` или после `from common import`) — позиционные,
  распакованные и ключевые; литерал или всё, что присваивают имени из аргумента в той же функции, объемлющей или в
  модуле (`=`, `+=`, `append`, `extend`; `_Scopes`: параметр функции значения не получает), через ветви условного
  выражения, `+` и `tuple()`/`list()`. Аргумент, из которого строк не вывести, — сбой `test_names_taken_by_code_exist`
  (кроме параметров `common.rubric`, переданных дальше). Тест требует каждое имя в `philosophy.md` и `rules/`. Ручные
  `SECTIONS` и `MODULES` — обратное направление: имена, которые код обязан брать; тест требует, чтобы сборщик нашёл их в
  коде, кроме `dependencies` — его код называет только текстом причин отказа. Разбор форм вызова сборщиком держит
  `CodeNamesTest`.
- Ссылку `{RULES}/<имя>.md` в ядре, модулях и коде (`{rules}/…` — поле `format`) сверяет
  `test_module_references_exist`; метки ищутся без учёта регистра. Вопросы судьи и тексты правил,
  которые говорят одно, сверяет `RulesMatchJudgeTest`; он же — правила, которые говорят одно между собой: факт
  спрашивается, только если после поиска осталось несколько кандидатов («Спецификации» 2 и `rules/planning.md`), и
  форму утверждения в «Поведении» («не X, а Y» — отрицание в начале, «X, а не Y» — утверждение с контрастом).
- Модуль начинается заголовком `# `, пустой строкой и строкой условия «Читайте, мой дорогой друг, …» (обращение —
  `prompts.ADDRESS` со строчной буквы); в ядре обращения нет — его ставит `common.context_output` каждой части.
  Ядро, модули, литералы `prompts.py` и тексты хуков — на «вы»: `PoliteFormTest` в `tests/test_contract.py` ищет
  обращение на «ты» и повелительное ед. числа вне кода в обратных кавычках и цитат в «ёлочках» и требует
  «пожалуйста» в каждом предложении с повелительным мн. ч. ядра, модулей и текстов хуков
  (`_sentences_without_please`).

## Ответ хука

Все хуки запускаются через `common.run_hook`: stdout — один JSON (ответ из `common.emit` плюс `systemMessage` из
`common.warn`) или пусто; stderr не пишется; исключение превращается в предупреждение «внутренняя ошибка»; без `fcntl`
(не POSIX, «Платформы») `run_hook` не вызывает хук и отвечает только предупреждением. `remind.py` получает номер части
ядра аргументом командной строки (`run_hook(lambda: main(sys.argv[1:]))`). Ввод и вывод — байтами через
`sys.stdin.buffer` и `sys.stdout.buffer` в UTF-8, независимо от локали, в том числе при кодировке файловой системы ascii
(локаль C без UTF-8 mode); JSON — через `common.dumps`. Путь из входа или транскрипта приводится к кодировке файловой
системы `common.input_path`: Claude Code пишет пути байтами UTF-8, и в локали не UTF-8 строка с кириллицей иначе не
кодируется для `open` и `subprocess`. Через неё идут `cwd` в `common.project_root`, `transcript_path` и `planFilePath` в
`common.read_transcript`, путь цели и `cwd` в `guard_memory.target_path` и `guard_memory._input_cwd`, значение
`autoMemoryDirectory` (файл настроек — текст UTF-8) в `guard_memory._auto_memory_overrides`. Пути из окружения Python
уже декодирует в кодировке файловой системы. Причина отказа начинается с `planka: `. Отказ `PreToolUse` —
`common.deny_output`, отказ `Stop` — `common.block_output` (им же `judge_tool.check_command_manifests` отвечает на
`PostToolUse` и `PostToolUseFailure`: команда уже выполнена, `decision: block` отдаёт причину агенту), контекст
`UserPromptSubmit` — `common.context_output`, у каждой части ядра свой: Claude Code меряет предел `remind.CONTEXT_LIMIT`
(10 000 символов, code.claude.com/docs/en/hooks.md) у строки каждого хука отдельно, длиннее — сохраняет в файл и отдаёт
агенту путь и первые 2 000 символов. `remind.split_core` делит ядро по разделам `## ` на `PARTS` кусков с наименьшим
наибольшим; часть k > 1 начинается заголовком `remind.CONTINUATION`: хуки события идут параллельно, порядок частей не
гарантирован. Часть длиннее предела выдаётся с предупреждением; что части влезают при пути `{RULES}` в 200 символов,
склейка частей — ядро и в `hooks/hooks.json` есть хук на каждый номер, сверяет `CorePartsTest` в
`tests/test_contract.py`; `debug_watch` отвечает через `common.context_output` с событием `PostToolUse` или
`PostToolUseFailure`; `model_watch` — только `systemMessage` предупреждения: plain-text stdout `SessionStart` и
`PostModelSwitch` Claude Code добавляет в контекст агента. Ответ запоминается до записи журнала: сбой записи не отменяет
отказ.

Обращение к модели `prompts.ADDRESS` («Мой дорогой друг») ставят только `deny_output`, `block_output` и
`context_output`; в текстах причин и в `remind.NO_DOCS_LINE`, `debug_watch.LINE` его нет. `deny_output` и `block_output`
вставляют его после префикса `common.REASON_PREFIX` (`planka: `), без префикса — в начало; первое слово причины —
обычное русское слово с заглавной — становится строчным (`common._lower_first_word`: аббревиатура, латиница, текст не с
буквы и имена разделов ядра — заголовки `## …` `philosophy.md`, «Решения 4» — не меняются); пустая причина — одно
обращение, причина со скобки (пустой `reason` судьи с « (нарушено: …)») — без запятой. `context_output` ставит строку
`Мой дорогой друг,` и пустую строку перед текстом. Текст, уже начатый обращением (без учёта регистра, с границей слова:
«Мой дорогой другой» — не обращение; у причины — после ведущих пробелов), не меняется. Обращение из строки условия
модуля (для агента, читающего модуль файлом) `common.rule_texts` снимает («Читайте, пожалуйста, …»): модуль встаёт в
сообщение с обращением — рубрику судьи (`common.rubric`) и контекст `debug_watch`. `remind` сравнивает с `CONTEXT_LIMIT`
текст вместе с обращением. Причина блока `Stop` после причины судьи заканчивается `judge_stop.RETELL_NOTE`: ответ агента
показан автору до хука, после блока агент пишет новое сообщение. Предупреждения `common.warn` — пользователю, без
обращения. `ADDRESS` живёт в `prompts.py`: `common` импортирует `prompts`, обратный импорт дал бы цикл.

## Судья

`common.run_judge` запускает `claude` с `JUDGE_FLAGS` (`-p`, `--setting-sources ""`,
`--strict-mcp-config`, `--no-session-persistence`, `--output-format json`, `--tools ""`), схемой
`JUDGE_SCHEMA`, системным промптом и моделью, с `cwd` в каталоге данных и окружением
`PLANKA_JUDGE=1`. Каждый хук первым делом проверяет `common.barrier_active` и внутри судьи не
работает.

Результат — `common.Verdict`. Любая ошибка судьи — `Verdict` с `error`: короткое описание без текста модели, оно идёт в
журнал; фрагмент ответа (до `MAX_DETAIL` = 200 символов) — в `detail`, только для пользователя. Предупреждение о
пропуске собирает `common.skip_message`. Ответ не по схеме — `ok` не `bool`, `reason` не строка, `violated` не
список строк — ошибка «ответ судьи не по схеме», не вердикт: приведение типов сделало бы из `"false"` вердикт
`ok`. `reason` обрезается до `MAX_REASON`. `prompts.SYSTEM_PROMPT` и промпт `prompts._wrap` начинаются
обращением `prompts.ADDRESS`; судью просят писать `reason` агенту на «вы», с «пожалуйста» и без обращения — его
добавляет хук («Ответ хука»). Просьбы рубрики («Пожалуйста, …») `SYSTEM_PROMPT` называет обязательными правилами;
первые слова пункта в `violated` берутся после «Пожалуйста». Аргументы после `claude` и stdin судьи —
байты `common._utf8` (claude читает их в UTF-8 при любой локали; суррогат U+DC80..U+DCFF — исходный байт пути, прочие
одиночные суррогаты заменяются «?» поштучно, остальной текст как есть), ответ декодируется из UTF-8 с заменой; ответ
судьи делится на строки только по `\n`: U+2028, U+2029 и U+0085 JSON оставляет символами внутри строки; `ValueError` при
запуске (нулевой байт в аргументе) — пропуск «claude не запущен».

Модель — `common.judge_model(data, transcript)`: `CLAUDE_PLUGIN_OPTION_JUDGE_MODEL`, по умолчанию `SESSION_MODEL`
(`session`) — тогда модель сессии из состояния `state/<session>.model.json` (`common.stored_session_model`), без неё —
из транскрипта. Не нашлась — `None`: судья без `--model`, с предупреждением раз на сессию (`warn_once`, ключ
`session-model`). Состояние пишет `model_watch`: документация хуков даёт поле `model` только во входе `SessionStart` и
не всегда (его нет после `/clear` и при восстановлении сессии) — тогда известная модель не стирается; смену модели
посреди сессии, в том числе восстановление модели при возобновлении, передаёт `to_model` входа `PostModelSwitch`
(Claude Code v2.1.251+). Годная модель — непустая строка не вида `<synthetic>` (`common.usable_model`), так же для
транскрипта; `to_model` не годная — предупреждение, состояние не меняется. При пустом или не строковом `session_id`
модель сессии не пишется и не читается: записи не найти. Агенту `model_watch` ничего не выводит
(«Ответ хука»). Состояние главнее транскрипта: событие смены модели приходит раньше ответа новой модели в транскрипте.
Цена — пропущенный `PostModelSwitch` (хук не уложился в срок, версия Claude Code до 2.1.251) оставляет прежнюю модель
до следующего события. Транскрипт остаётся запасным источником (`context/deferred/transcript-turn-data.md`).

Транскрипт (`transcript_path` входа) читает `common.read_transcript` за один проход и отдаёт `common.Transcript`:
`model` — `message.model` последнего ответа ассистента основной ветки, кроме моделей вида `<synthetic>`
(`common.usable_model`); `plan_file` — последний `attachment.planFilePath`; `turn_messages` — тексты ответов ассистента
после последней реплики автора; `author_turn` — текст этой реплики, сообщений человека посреди хода и ответов автора при
отклонении инструмента; `author_answers` — ответы на `AskUserQuestion` после неё; `message_before_author` — последнее
сообщение ассистента до неё; `earlier_turns` — прежние непустые реплики автора основной ветки от старых к новым, каждая
собрана как `author_turn` и дополнена ответами на `AskUserQuestion` своей реплики (обрезка — в `prompts`); `turn_steps`
— шаги той же реплики по порядку, `prompts.Step`: текст ответа (`text`) или вызов инструмента основной ветки
(`common._tool_step`: имя `call` и аргумент `arg` — команда `Bash`, путь файлового инструмента или вход JSON одной
строкой, переводы строки «⏎», до `STEP_ARG` символов) с меткой результата `mark` и выводом `output` из `tool_result`
(`STEP_OUTPUT` или `STEP_ERROR`, длиннее `STEP_HEAD` + `STEP_TAIL` — начало и конец, `common._step_output`;
`STEP_REJECTED` для отклонённого вызова). Текстом шаг становится только в `prompts.render_step(step, tag)`: метки
«⟦<код> вызов⟧ … ⟦<код> аргумент⟧ …», «⟦<код> вывод|ошибка|отклонено|вывод опущен⟧» и разделитель «--- <код> ---»
(`prompts.turn_separator`) ставятся по полям, данные идут как есть, без экранирования. Код (`prompts.step_tag`, через
`prompts._fresh_code`) — начало SHA-256 всех полей шагов, кратчайшее от 4 hex-знаков, которого нет ни в одном поле
(заняты все 64 знака — код длиннее любого поля); он детерминирован, поэтому `content_sha256` в `judge.log` стабилен.
Агент не знает кода заранее: всё без кода — похожие скобки 〚〛, CR, U+2028, U+0085, черты — для судьи данные. Правило
называет судье `prompts.turn_label(tag, docs)`: `prompts.turn_content(steps, appendix)` возвращает `(content, tag)`,
`judge_stop` передаёт `turn_label(tag, docs=docs)` как `label`. Блок хука фильтра «документация» (`appendix`,
`prompts.render_docs_content`: изменённые файлы и комментарии в них) стоит после шагов за строкой «⟦<код> изменённые
файлы⟧» (`prompts.DOCS_MARK`), вне `MAX_TURN_CHARS`; код считается и по тексту блока (`step_tag(steps, appendix)`),
поэтому такой же список в сообщении агента — данные без кода. Отвергнуто: шаги строками с метками внутри — граница шага
и метка вывода искались по
тексту, который пишет агент. При переполнении `prompts.MAX_TURN_CHARS` `prompts.turn_content` сначала снимает вывод
ранних вызовов с меткой из `prompts.STEP_OUTPUTS` (метка `STEP_DROPPED`, «⟦вывод опущен⟧», `prompts._drop_old_outputs`;
вызов без вывода и «⟦отклонено⟧» не трогаются), затем опускает ранние шаги: тексты сообщений и ближние к последнему
выводы — опоры рекомендации — вытесняются последними. Последний шаг длиннее предела режется по данным до экранирования
(`prompts._clip_last`): у сообщения остаётся конец текста, у вызова — строка вызова и конец вывода. Записи `isSidechain`
пропускаются. Шаги берёт только судья `Stop` (`judge_stop.turn_messages`): по ним он видит проверку, о которой агент
пишет словами; дубль последнего сообщения сверяется с последним текстовым шагом (`call is None`) — после него в
транскрипте бывает вызов инструмента.

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

Проверяемое содержимое идёт судье блоком `<content КОД>…</content КОД>` (`prompts._wrap`). Судьи вопроса, плана и `Stop`
получают перед ним блок `<author КОД>…</author КОД>` — `prompts.author_context(author_turn, author_answers,
earlier_turns)`: прежние реплики автора от старых к новым (все вместе с разделителями — до `prompts.MAX_AUTHOR_FIELD`,
самые старые отбрасываются с пометкой их числа), реплика автора текущего хода и его ответы на `AskUserQuestion` после
неё (каждое поле — до `prompts.MAX_AUTHOR_FIELD`, 8000 символов); передают его `judge_tool._author` и
`judge_stop.judge`. Пояснение `prompts._AUTHOR_NOTE`: явная просьба автора побеждает рубрику, просьба из прежней реплики
— тоже, если более поздняя реплика её не отменила; проверяется только `<content>`, `<author>` — тоже данные. Зачем: без
реплики автора судья не отличает решение агента от решения, которое автор принял сам, и отклоняет сделанное по его явной
просьбе, в том числе по просьбе, сделанной несколько ходов назад; судья записи в память те же поля получает внутри
`<content>` (`guard_memory.render_content`), потому что согласие автора там и есть проверяемое. Блок `<author>`
отдельный, а не часть `<content>`: журнал хранит длину и SHA-256 только проверяемого содержимого, текст автора в
`judge.log` не попадает ни целиком, ни в хэше. Код тегов обоих блоков общий — `prompts._fresh_code` по данным обоих
блоков (как код шагов выше): его нет в данных, правило называет судье `prompts._data_note`. Данные идут как есть:
похожий закрывающий тег (`</content>` без кода, `</content>` с невидимым знаком внутри, `＜/content＞`, другой регистр)
блок не закрывает; `content_sha256` в `judge.log` считается по самому содержимому. Вопросы по видам:
`prompts.question_prompt`, `prompts.plan_prompt`, `prompts.memory_prompt`, `prompts.stop_prompt` (вопросы совпавших
фильтров в порядке options, done, docs; сообщения реплики склеивает `prompts.turn_content` с пояснением
`prompts.turn_label`, не больше `prompts.MAX_TURN_CHARS` символов: ранние шаги сверх предела опущены с пометкой их
числа), блок хука фильтра «документация» — `prompts.render_docs_content`, его ставит `turn_content` за меткой
`DOCS_MARK`.

Судья — группа процессов (`start_new_session`) со сторожем `common._WATCHDOG` (`python3 -I -c`) во главе
(`common._start_judge`): сторож запускает `claude` потомком и раз в `WATCHDOG_POLL` (0,5 с) сверяет своего родителя с
PID хука; хук умер — `SIGKILL` всей группе, `claude` и его потомкам. `claude` завершился — тоже `SIGKILL` группе: его
потомок, отпустивший каналы судьи, не переживает хук; код возврата `claude` сторож не передаёт — ответ уже в каналах.
Таймаут `JUDGE_TIMEOUT` (60 с) убивает группу из хука (`common._kill_group`, ожидание до `common.KILL_WAIT`, 5 с);
сторож, убитый отдельно от группы, оставляет её этому таймауту. Сторож — на всех платформах: `PR_SET_PDEATHSIG` Linux
действует только на сам `claude`, не на его потомков. Цена — лишний запуск интерпретатора на вызов судьи, порядка 20 мс.
Путь к `claude` для сторожа ищется по `PATH` окружения судьи; нет или файл не исполняемый — `FileNotFoundError`, пропуск
«claude не найден в PATH».

## Сроки

Таймауты хуков в `hooks/hooks.json`: `UserPromptSubmit` 10 с у части 1 и 5 с у части 2 (без снимка — только чтение
ядра), `PreToolUse` 90 с у обоих хуков, `Stop` 120 с, `PostToolUse` и `PostToolUseFailure` — 10 с у `debug_watch` и 30 с
у `judge_tool`, `SessionStart` и `PostModelSwitch` — 10 с у `model_watch` (без судьи, git и процессов — только запись
файла под `state_lock`). У `Stop` до судьи ещё `git rev-parse` и, если вершина — домашний каталог или его предок и
`CLAUDE_PROJECT_DIR` ниже неё, `git ls-files` корня проекта (`common.project_root`, вместе до `common.GIT_ROOT_TIMEOUT`,
5 с), сверка со снимком со сроком `judge_stop.SNAPSHOT_BUDGET` (20 с) и извлечение комментариев со сроком
`judge_stop.COMMENTS_BUDGET` (20 с) — вместе с судьёй (до 65 с) не больше 110 с. У части 1 `UserPromptSubmit` снимок
вместе с упаковкой блоков walk (`snapshot._pack_dirs`: срок проверяется на каждом каталоге и через каждые
`snapshot.PACK_CHUNK` байт блока) ограничен сроком `remind.SNAPSHOT_BUDGET` (7 с от старта хука); запись готового файла
и `prune_state` — в оставшемся запасе; не уложился — `TimeoutError`, снимок пропускается с предупреждением, неудача
запоминается для корня (`snapshot.mark_failed`; так же при `TooManyFiles`), и до конца сессии `remind` этот корень не
снимает; напоминание выдаётся. Один вызов git в `snapshot` и `comments` — не дольше `GIT_TIMEOUT` (10 с) и остатка
срока. У хуков `PreToolUse` с судьёй срок — `JUDGE_TIMEOUT` и `KILL_WAIT` (65 с) против 90 с; у `guard_memory` до судьи
ещё корень проекта (`common.project_root`, `common.GIT_ROOT_TIMEOUT`, 5 с) и `git check-ignore`
(`guard_memory._repository_file`, срок `guard_memory.CHECK_IGNORE_TIMEOUT`, 5 с) — для цели под `autoMemoryDirectory`
или cowork, который равен проекту или содержит его; вместе с судьёй 75 с против 90 с. У `judge_tool` на `Bash` и
MCP-инструментах (`mcp__*`) снимок манифестов — `git rev-parse` корня (`common.GIT_ROOT_TIMEOUT`) и затем, отсчитанный
после него, срок `manifest_watch.SNAPSHOT_BUDGET` (5 с) на `git ls-files` и разбор, а у команды с ref (`command_refs`) —
и на `git cat-file --batch` и `git ls-tree` ref до начала сессии, у `git apply` — и на чтение файлов патчей
(`command_patches`; оба — `restored_names`; срок вышел — снимок не снят), у правки манифеста — корень проекта
(`GIT_ROOT_TIMEOUT`, один раз), с ним один `git cat-file --batch` версий `_REPO_REFS` и сторон `_INDEX_STAGES`
(`manifest_watch.HEAD_TIMEOUT`, 5 с), обход манифестов проекта и дерево HEAD (`judge_tool._project_names`, ещё
`SNAPSHOT_BUDGET`), а у файла с несколькими жёсткими ссылками и у правки с подключением до них — `list_manifests` в
срок `HEAD_TIMEOUT`, один на правку (`manifest_watch.listing`): 2 · `HEAD_TIMEOUT` + `GIT_ROOT_TIMEOUT` +
`SNAPSHOT_BUDGET` на первый манифест. Второй и следующий манифест одного файла проверяется, только если с его
`HEAD_TIMEOUT` + `SNAPSHOT_BUDGET` укладывается в `judge_tool.LINKED_BUDGET` (60 с) от `started` — отметки до корня
проекта и перечня; иначе пропуск с предупреждением. Вся правка — не дольше большего из двух сроков, 60 с против 90 с;
на `PostToolUse` сравнение со снимком — срок `manifest_watch.CHECK_BUDGET` (5 с) против 30 с, в него входят и `git
cat-file --batch` изменённых манифестов, и дерево HEAD. Вышел срок — пропуск с предупреждением.

Эти суммы проверяет `tests/test_contract.py`, `TimeoutsTest`, против таймаутов из `hooks/hooks.json` константами модулей
(`common.JUDGE_TIMEOUT`, `common.KILL_WAIT`, `common.GIT_ROOT_TIMEOUT`, `judge_stop.SNAPSHOT_BUDGET`,
`judge_stop.COMMENTS_BUDGET`, `remind.SNAPSHOT_BUDGET`, `guard_memory.CHECK_IGNORE_TIMEOUT`,
`manifest_watch.SNAPSHOT_BUDGET`, `CHECK_BUDGET`, `HEAD_TIMEOUT`, `judge_tool.LINKED_BUDGET`; что `started` снят до
`listing` и `edit_targets`, — `test_linked_budget_counts_from_hook_work` по коду `judge_manifest_edit`); там же
`test_post_tool_use_has_no_judge`: на `PostToolUse` ни `debug_watch`, ни `manifest_watch`, ни
`judge_tool.check_command_manifests` судью не зовут;
`test_model_watch_fits` — у `model_watch` нет вызовов судьи, `subprocess` и git, таймаут обоих событий 10 с. Что код
передаёт именно эти константы, проверяют `KillGroupTest` и `ProjectRootTest` в `tests/test_common.py` и
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
`context/testing.md`). Формы, которые разбор понимает, — `README.md`, «Известные ограничения». Строку файлов
`planparse._split` делит на пути и остаток после серии путей в кавычках (связки серии — `planparse._CONN`); повторная
строка файлов задачи с остатком-предложением (`planparse._continues_description`) — описание, её пути владением не
считаются. Пункт списка файлов, который не владение (`- **Не трогать:**`), `parse_plan` запоминает отступом
`skip_indent`: пункты глубже него принадлежат ему и пропускаются, даже если они целиком пути; пункт на его уровне или
мельче, заголовок, конец списка и новая строка файлов снимают отметку; пункт глубже отметки, который сам не путь (`- всё
в legacy/`), её не снимает. Пункты, вложенные под саму строку `Файлы:`, — владение. Корпус —
`tests/fixtures/plan-readonly-nested.md` и `plan-readonly-prose-nested.md`. Значение строки файлов без кавычек, чьё
первое слово — заглушка из `planparse._NO_FILES` (`нет`, `none`, `empty`, `n/a`, `tbd`, `ничего`, `etc`, тире, `…`), —
задача без файлов; последний элемент перечня без кавычек `etc` или `etc.` (`a.py, etc.`) — не путь. Отказ по общему
файлу (`deny-files`) кончается подсказкой `judge_tool.FILES_HINT` (пометить файл чтением или
отдать одной задаче), в журнал идёт число конфликтов `conflicts` (пар «волна, файл»), без причины: пути файлов из плана
— содержимое.

## Запись в память

`guard_memory` срабатывает на файловые инструменты (`FILE_TOOLS`) и MCP-инструменты. Каталог настроек —
`guard_memory.config_dir` (`CLAUDE_CONFIG_DIR`, без него `~/.claude`). Путь цели — `guard_memory.target_path`
(`common.input_path`, `~` раскрыта, относительный — от `cwd` входа `guard_memory._input_cwd`, ссылки не разрешены; путь
с NUL — `None`, такой файл инструмент не откроет); память — `guard_memory.is_memory_path(path, project)`, `project` —
`guard_memory._project_dir` (`CLAUDE_PROJECT_DIR`, без неё `cwd` входа, без него текущий каталог): `CLAUDE.md` и
`rules/**.md` каталога настроек, `projects/<проект>/memory/**` и `projects/<проект>/agent-memory-local/**`, память
субагентов — `agent-memory/` каталога настроек (`memory: user`) и `.claude/agent-memory-local/` проекта (`memory:
local`), — при `CLAUDE_CODE_REMOTE_MEMORY_DIR` (`guard_memory._remote_memory_dir`, облачная сессия) ещё его
`agent-memory/` и `projects/`, — и каждое абсолютное значение `autoMemoryDirectory`
(`guard_memory._auto_memory_overrides`) и `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` (`guard_memory._cowork_memory_override`:
только абсолютный путь, `~` не раскрывается, как у Claude Code). Значение `autoMemoryDirectory` ищется во всех файлах
`guard_memory._settings_files`: `managed-settings.json` и `managed-settings.d/*.json` каталога
`guard_memory.managed_dir`, `.claude/settings.local.json` и `.claude/settings.json` проекта, `settings.json` каталога
настроек. Claude Code берёт одно значение по приоритету источников, хук — объединение всех: настройки `--settings` и
серверные managed-настройки хуку не видны, порядок приоритета им не воспроизвести, а лишнее место памяти стоит только
лишнего вызова судьи (дешёвая ошибка ниже). Значение приводится `common.input_path`; относительное значение, значение с
NUL и значение, не кодируемое в файловую систему (одиночный суррогат), пропускаются
(`test_unusable_auto_memory_directory_skipped`), как и файл настроек не с объектом JSON и не обычный файл (FIFO повесил
бы хук; Claude Code такой файл настроек не читает). `.claude/agent-memory/` проекта (`memory: project`) памятью не
считается: это отслеживаемый файл репозитория (`MemoryHookTest.test_agent_memory`). По той же причине под каталогом
`autoMemoryDirectory` или cowork, который равен проекту или содержит его (`guard_memory.is_memory_path`: каждый
совпавший каталог содержит проект; сравниваются все формы `_forms` каталога и проекта), файл репозитория проекта — не
память (`guard_memory._repository_file`, спрашивается только для пути внутри проекта). Под каталогом строго внутри
проекта (`test_auto_memory_directory_in_repository`, `test_auto_memory_directory_in_home_project`) или вне него
(`test_auto_memory_directory_in_foreign_repository`) git не спрашивается, запись судится всегда: автор назначил его
местом памяти. Родитель проекта, он же корень репозитория, — первый случай (`test_auto_memory_directory_above_project`;
путь вне проекта судится). Путь во вложенном репозитории между путём и проектом (`guard_memory._nested_repository`:
каталог с `.git`) — не файл рабочего дерева проекта, память даже при ответе 1
(`test_auto_memory_directory_nested_repository`). Вопрос git проекта: корень — `common.project_root(<каталог проекта>)`,
его нет на диске — не файл репозитория, git не спрашивается; иначе `git -C <ближайший существующий каталог проекта>
check-ignore -q` с окружением `common.git_env(<корень>)` (репозиторий выше корня — dotfiles над проектом без своего git
— не отвечает, `test_auto_memory_directory_project_under_dotfiles_home`; подкаталог монорепозитория, новый или
удалённый, — файл монорепозитория, `test_auto_memory_directory_monorepo_subproject`) отвечает 1 — путь в рабочем дереве
и не исключён `.gitignore`, `info/exclude`, `core.excludesFile` (отслеживаемый файл исключённым не считается). Иначе
значение из `.claude/settings.json` проекта, равное самому проекту, отдавало бы судье памяти каждую запись в проекте.
Код 0 (исключён), 128 (не репозиторий, `test_auto_memory_directory_in_project_without_git`), нет `git`, ошибка или
таймаут — память (код 1 — файл репозитория): лишний вызов судьи дешевле пропуска. Места каталога настроек и
`CLAUDE_CODE_REMOTE_MEMORY_DIR` git не проверяются. Путь цели и места памяти (`_memory_places`) сравниваются в двух
формах — как написаны и через `realpath` (`_forms`), а `memory/` проекта, если ссылка она или `projects/<проект>`, — и
разрешённой целью: каталог настроек под dotfiles судится и при записи через ссылку, и при записи прямо в её цель. Запись
через ссылку вне каталога настроек ловит `realpath` пути цели. Ссылка на файл внутри `rules/` или `memory/` ловится
только записью через неё. MCP — `guard_memory.is_memory_mcp`: слово из `WRITE_VERBS` в имени инструмента при слове из
`MEMORY_WORDS` в имени сервера; иначе `guard_memory._writes_memory` — в имени инструмента глагол записи перед словом
памяти не дальше чем через одно слово или сразу после него (слова по порядку — `guard_memory._words`). Корпус настоящих
и ложных имён — `MemoryMcpNameTest.CORPUS` в `tests/test_guard_memory.py`. Дешёвая ошибка — ложное срабатывание: лишний
вызов судьи (не больше `common.MAX_DENIES` отказов на реплику) дешевле записи в память без согласия автора; поэтому
`set_memory_limit` и `clear_memory_cache` судятся как запись. `guard_memory._words` делит и конец аббревиатуры перед
словом (`saveLTMemory`), и границу буквы и цифры (`storeMemory2`). Matcher хука в `hooks/hooks.json` пропускает `mcp__`
с `memor` или `remember` в любом регистре — надмножество `MEMORY_WORDS`. Содержимое судьи —
`guard_memory.render_content` из полей `Transcript` и текста записи (`guard_memory.write_text`): прежние реплики автора,
реплика текущего хода и его ответы — тем же текстом `prompts.author_context`, что блок `<author>` других судей (пределы
— `prompts.MAX_AUTHOR_FIELD`), сообщения агента и текст записи — до `guard_memory.MAX_FIELD` символов
(`guard_memory._clip`).

Личные инструкции и импорты. Память — ещё `CLAUDE.local.md` (`guard_memory.LOCAL_MEMORY`) в каталоге проекта, над ним и
в его подкаталогах (`guard_memory._local_memory`: Claude Code читает их из каталога запуска, каталогов над ним и
подкаталогов) и файлы, которые подключают импортом `@путь` файлы `_import_roots` — `CLAUDE.md` и `rules/**.md` каталога
настроек (ссылки на каталоги проходятся, цикл — нет) и `CLAUDE.local.md` проекта и каталогов над ним
(`guard_memory._imported_files`: обход по уровням, не дальше `IMPORT_DEPTH` переходов, каждый файл — в формах `_forms`,
файл не обязан существовать: его создание тоже запись в память). Импорты файла — `guard_memory._imports`: `IMPORT` — `@`
не после буквы и цифры (почта — не импорт), путь до пробела, `\ ` — пробел, `#…` отрезан, путь берётся и с хвостом
разметки `IMPORT_TAIL`, и без него; `_accepted_import` — формы, которые принимает Claude Code; относительный — от
каталога файла, `~/` — от дома; `_strip_code` вырезает ограждённые блоки (`FENCE`: три обратные кавычки, за которыми в
строке есть ещё обратная кавычка, — код в строке, не блок), код в строке и HTML-комментарии (`_strip_html_comments`:
`<!-->` и `<!--->` — закрытые пустые комментарии). Файл читает `_read_memory_file`: только обычный файл (FIFO не вешает
хук), не больше `MAX_IMPORT_FILE` (4 МиБ — больше Claude Code не читает). Импорты `CLAUDE.local.md` подкаталогов не
разбираются: Claude Code читает их по требованию, а обход проекта на каждый вызов хука дорог. Пределы на вызов —
`MAX_IMPORT_FILES` файлов и `MAX_IMPORT_BYTES` байт: сверх них остаток не разбирается, с предупреждением. Проектный
`CLAUDE.md` в корни не входит: это документация, его импорты — не память. `CLAUDE.local.md` или импортированный файл,
оказавшийся файлом репозитория проекта (`_repository_file`), — не память, как проектный `CLAUDE.md`; git спрашивается не
больше одного раза на вызов (`repository_file` в `is_memory_path`): срок `CHECK_IGNORE_TIMEOUT` входит в сумму
`TimeoutsTest` один раз. Корпус форм импорта — `ImportParseTest.CORPUS` в `tests/test_guard_memory.py` (примеры
документации Claude Code о памяти).

Регистр и `..`. На файловой системе без учёта регистра (`common.case_insensitive`: путь или его предок
находится по имени с обращённым регистром, и `os.path.samefile` — тот же файл; проверяется каждый уровень) путь и
места памяти сравниваются после `str.casefold`. Отдельного вызова о регистре в стандартной библиотеке нет; цена —
соседняя ссылка с тем же именем в другом регистре даёт лишний вызов судьи. `target_path` схлопывает `..`
лексически (`os.path.normpath`) до разрешения ссылок: Claude Code сам нормализует путь инструмента так же перед
хуком и пишет в нормализованный (проверено на бинарнике 2.1.294; закреплено тестом).

## Повторная неудача команды

`debug_watch` считает по ключу `debug_watch.command_key(command, agent_id)` (SHA-256 команды с
нормализованными пробелами; у субагента — `agent_id` входа, `\0` и команда, у основного агента — одна
команда) в `state/<session>.debug.json`: `counts` — неудачи подряд по ключам, `shown` —
`{agent_id: prompt_id}`, реплика, в которой модуль уже показан этому агенту (у основного ключ `""`).
Субагенты и основной агент считают и получают модуль порознь: неудача субагента не делает «второй раз»
у другого. `debug_watch.update` пишет файл заново только из `counts` и `shown`: поля прежних форматов
(`shown_prompt`) не читаются и при записи не переносятся. Файл ограничен: последние `MAX_COUNTS` (500) ключей
`counts` и `MAX_SHOWN` (100) записей `shown` (`debug_watch._recent`); свежесть — порядок вставки словаря, тронутая
запись переставляется в конец. Без предела каждая новая команда сессии росла бы файл, который хук читает и пишет
целиком под `state_lock`.

Неудача — событие `PostToolUseFailure` без `is_interrupt`, кроме кода 1 команды-ответа; `PostToolUse` той же команды и
код 1 команды-ответа удаляют её ключ. Код выхода — первая строка `error` вида `Exit code N` (`debug_watch.exit_code`;
документация хуков велит опираться только на неё, остальное — вывод без стабильного формата). Команда-ответ
(`debug_watch.code1_is_answer`) — последняя команда строки (`_segments`, ниже), а если она после `&&` и из
`PASS_THROUGH` без перенаправлений (`<`, `>` вне кавычек и экранирования, `UNQUOTED`) — команда перед ней
(`_last_pipeline_command`); присваивание одной подстановки `x=$(…)` (`SUBSTITUTION_ASSIGNMENT`) — по команде
подстановки. Затем — после присваиваний и обёрток `WRAPPERS`, из `CODE1_ANSWERS`, git-подкоманда из `GIT_CODE1_ANSWERS`
с нужным флагом, `command -v`/`-V` или `!`; неразборная строка — не ответ. Слова команды берёт `debug_watch._head_words`
по первым `_HEAD_LIMIT` (4096) символам (`#` внутри слова, `VAR=a#b`, — не комментарий): слово, разрезанное пределом,
отбрасывается, кавычка, не закрытая в пределах обрезанной строки, — не ошибка; флаг ответа дальше предела не виден —
неудача. Ложное срабатывание здесь дороже пропуска: модуль только подмешивается, агент и без него видит ошибку, а модуль
«упала второй раз подряд» на ответе «не найдено» засоряет контекст и учит агента пропускать модуль. Сомнение решается в
пользу ответа только для команд из списка, остальное — неудача. Сам Claude Code код 1 у `grep`, `rg`, `git grep`, `git
diff --exit-code`, `test`, `[`, `diff`, `find` без обёрток неудачей не отдаёт (проверено вручную на 2.1.293: результат —
вывод, не ошибка); `which`, `command -v`, `type`, `pgrep`, `pkill`, `cmp`, `git merge-base --is-ancestor` и `grep` под
`timeout` или `VAR=…` — отдаёт. Корпус форм `error` — `tests/fixtures/bash-failure-errors.jsonl`. Отвергнуто: хэш вывода
ошибки (у `grep` без совпадений вывод пуст — хэш одинаков; у тестов вывод меняется временем и путями — настоящий повтор
не ловится) и сброс на новой реплике (пропускает главный случай: автор вернулся с «всё ещё падает»). С
`REPEAT_THRESHOLD` неудач модуль `debugging` подмешивается не чаще раза за `prompt_id` каждому агенту; нет модуля —
предупреждение, отметка о показе не ставится.

Сегменты строки `debug_watch._segments` берёт из дерева `shparse.parse`: разделитель — оператор `_SEPARATOR` (`|`, `|&`,
`||`, `&&`, `;`, перевод строки; `\` с переводом строки — продолжение, одиночный `&` и скобки не делят) вне узлов
`_NO_SPLIT` (слова — с кавычками, подстановками и скобками массива, перенаправления — `>|` не конвейер, `((…))`,
арифметика, `[[ … ]]`), образцов `case`, строк, отброшенных ошибкой скобок массива (`Script.dropped`), комментариев и
тел heredoc. Комментарии и тела heredoc вместе со строкой терминатора (`_skip_heredocs`; терминатор со знаком конца
подстановки в остатке строки, `E)`, — до знака; heredoc в `` `…` ``, тело которого bash не читает, диапазона не даёт) из
текста сегментов вырезаны. Поэтому `<<` в арифметике — сдвиг, `((cd a && make); false)` — подоболочка в подоболочке, и
её разделители делят команды, а разделитель в теле подстановки (`diff <(a; b) f`, `f=$(a | b); echo $f`) команды строки
не делит. Фатальная синтаксическая ошибка: от конца последней прочитанной команды строка — одна команда без разделителей
(bash её не исполняет). `debug_watch._paren_pairs` (пары `(` и `)` вне кавычек, комментариев и тел heredoc по тому же
дереву) хук не вызывает: её результат входит в записанную сверку корпусов (`CorpusResultsTest` в
`tests/test_debug_watch.py`).

## Разбор shell

`shparse.py` повторяет parse.y bash 5.3 и отдаёт одно дерево; устройство — в докстроке модуля. Его опоры:

- Среда исполнения — обёртка инструмента Bash Claude Code: `bash -c 'source <снимок> … && shopt -u extglob … && eval
  '<команда>''` (проверено `ps` и `$BASH_EXECUTION_STRING` в сессии, Linux); снимок ставит `shopt -u expand_aliases`.
  Поэтому алиасы не раскрываются, `extglob` выключен, а сверка с bash идёт через ту же обёртку (`make bashdiff`).
- Синтаксическая ошибка: фатальная — команды до неё исполнены, после — нет; ошибка скобок присваивания массива
  отбрасывает свою строку, чтение продолжается со следующей (проверено прогоном bash с `touch`).
- Тело подстановки, которое bash разбирает при раскрытии (`` `…` ``, `$((…) …)`, подстановки в теле heredoc и в `'…'`
  арифметики), разбирается по напечатанному тексту, как у bash (print_comsub). Подстановка, не закрытая в `'…'`
  арифметики, продолжается за кавычкой: заново разбирается текст только от первой такой кавычки до конца выражения —
  он содержит следующие кавычки, а bash читает выражение один раз слева направо; разбор от каждой кавычки
  разбирал бы каждую подстановку до n раз (квадратичное время).
- Слово с подстановкой, где `\` в скобках присваивания массива делит слова при чтении иначе, чем при раскрытии
  (bash при чтении держит разделитель `(` или `"`, при раскрытии `xparse_dolparen` — пустой стек), раскрывается
  заново по напечатанному тексту (`_reexpand_word`).
- Присваивание `имя[…]=` узнаёт `_assignment` (assignment bash); конец индекса ищет `_skip_subscript`, как skipsubscript
  bash: `]` во вложенных `[…]`, `'…'`, `"…"`, `` `…` ``, `$(…)` и `${…}` индекс не кончает (`a[${x:-]}]=1` —
  присваивание), незакрытая конструкция — слово не присваивание. Подстановка `$(…)`, `<(…)` прямо в индексе для поиска
  конца — заглушка `$( )`; bash просматривает её текст символами, и `)` метки `case` или `]` в `<(…)` там читаются
  иначе.
- Без рекурсии Python по глубине входа: разбор написан генераторами со своим стеком; разобранные подстановки
  переиспользуются при повторном разборе, разбор линеен, кроме цепочки повторного раскрытия
  (`context/deferred/shparse-depth3-tail.md`).

Дерево читают `depcheck` (детектор зависимостей, ниже), `debug_watch` (сегменты строки, «Повторная неудача команды»)
и `comments` (комментарии sh и bash, «Комментарии изменённых файлов»).

## Детектор зависимостей

Текст команды разбирает `shparse.parse` (грамматика bash 5.3, «Разбор shell»); вопросы детектора задаёт `depcheck.py`
над деревом, без состояния. `depcheck._analysis(text)` обходит дерево и отдаёт три списка: команды — `_Segment` каждой
простой команды (`shparse.Simple`) по порядку текста, в том числе в телах подстановок `$(…)`, `` `…` ``, `${ …; }`,
`${| …; }`, `<(…)`, `>(…)`, в функциях, арифметике (`'…'` в ней — не кавычки: `echo $(( '$(npm i x)' ))`, индекс
присваивания и `${a[…]}`, смещение `${x:…}`) и в подстановках тела heredoc с терминатором без кавычек; связи «вывод →
вход»; тела heredoc, которые исполняет оболочка. Сегмент — текст простой команды без пробелов по краям: его возвращает
`dependency_add` (`echo $(npm i x)` — сегмент `npm i x`). Подстановки уже лежат в дереве, их команды проверяются на той
же глубине, что и команда вокруг; глубина `_MAX_DEPTH` (4) считает только тексты, которые разбираются заново: строки
`sh -c`/`eval`, here-string, вход оболочки, тела heredoc оболочки, команды `_launched`. В одном вызове
`dependency_add` или `dependency_doubt` (`_within_call`) каждый текст разбирается один раз: модульное `_call` держит
остаток `_BRACE_BUDGET`, разборы по тексту и остаток `_NAME_BUDGET`; вне вызова у каждого текста свои пределы.

Синтаксическая ошибка скобок присваивания массива (`fatal=False`) уже учтена деревом: bash отбрасывает строку, и
следующие строки — команды (`x=(<<E` ⏎ `npm i x` — добавление). После фатальной ошибки bash не исполняет ни команду с
ошибкой, ни остаток, но ошибка разбора, которой у bash нет, не должна прятать команды: ложный отказ дешевле пропуска.
Поэтому `_parses` разбирает заново остаток со следующей за ошибкой строки (не больше `_RECOVERIES`, 16, раз), а начало
строки с ошибкой до её лексемы — с командой `:` на следующей строке (`&&`, `|` в конце начала — не ошибка), укорачивая
его до новой ошибки не больше `_PREFIX_TRIES` (4) раз. Остаток самой строки за ошибкой не проверяется: `echo ) ; npm i
x` — пропуск, как в bash; команда на следующих строках проверяется: `echo )` ⏎ `npm i x` — ложный отказ.

Связи `_analysis`: соседние команды конвейера — источник-простая команда и каждая простая команда приёмника вне тел
подстановок (`_stdin_heads`; у составного источника вывод неизвестен); `<(…)` словом или целью `<` — источник —
единственная простая команда тела (`_sub_output`), иначе вывод неизвестен; `>(…)` целью `>`, `>>`, `>|`, `&>`, `&>>`
(`_OUT_REDIRECTS`) получает вывод самой команды, словом — неизвестный, приёмник — простые команды тела
(`_procsub_feeds`). Тела heredoc — команды, если в той же полной команде (элемент `Script.commands`; у тела подстановки
свои) есть оболочка без маркера, читающая stdin (`pkgmanagers._heredoc_runs`: `sh`, `bash`, `zsh`, `dash`, `ksh`, `fish`
без скрипта, без `-c` или с `-s`, `ssh` без удалённой команды, `source` и `.` со скриптом `-` или `/dev/stdin`): `bash
<<E`, `cat <<E | bash`, `cat <<'EOF' |` ⏎ тело ⏎ `EOF` ⏎ `bash`. Текст тела — `_heredoc_text`: у терминатора в кавычках
— тело как есть, иначе части тела, раскрытия — своим текстом (`\$(…)` тела — уже `$(…)`). Маркер у оболочки покрывает её
тело heredoc, как строку `-c` и вход по конвейеру. `_stdin_scripts` по связям отдаёт текст, который исполняет со stdin
приёмник без маркера — `_heredoc_runs` или `source`/`.` без скрипта (`source <(…)`): вывод `echo`/`printf`
(`_echo_text`: `\n` — перевод строки, у printf каждый аргумент — с новой строки; `printf -v` — вывод неизвестен) и тела
heredoc `cat` (`_heredoc_cat`: без флагов, без операндов или с операндом stdin `_STDIN_OPERAND` — `-`, `/dev/stdin`,
`/dev/fd/N`, `/proc/<процесс>/fd/N`). Вывод файлов (`_file_output`: `cat f…` без флагов и операндов stdin, `git show
<ревизия>:<путь>`) без входа из другой команды — пропуск: скрипт из файла не виден, как у `bash f`. Неизвестный вывод
источника, вывод `echo`/`printf` с вычисляемым именем команды (`_computed`) или со словом с внешним раскрытием, тело
heredoc `cat` с внешним раскрытием — текст неизвестен до исполнения (сомнение `_WHY_COMPUTED`, ниже). `_find` проверяет
команды текста, затем тела heredoc оболочки, затем вход из связей — на уровень глубже.

Строки, которые исполняет сама команда, собирает `_scripts`: `sh -c`/`eval` (`pkgmanagers._inline_script`; `eval --`
отбрасывает `--`, как встроенная bash), here-string оболочки без скрипта (`_Segment.herestrings`) и запуск из аргументов
(`_launched`: ssh — `pkgmanagers._ssh_command`, флаги и после хоста, слова склеены пробелом; `trap`; `find -exec` —
`_find_runs`, `{}` в команде — путь от точки старта, не пакет; `$HOME`, `$PWD`, `$OLDPWD`, `$TMPDIR` в начале точки —
абсолютный путь, `_ABSOLUTE_VAR`; `cmd /c`, `pwsh -Command`, `nix develop|shell -c`, `nix-shell --run`, `mise exec --`,
git — `pkgmanagers._git_runs`: `submodule foreach`, `bisect run`, `rebase -x|--exec` (и сокращения `_GIT_REBASE_EXEC`:
`--ex`, `--exe`; `--e` git отвергает как неоднозначный) и псевдоним `-c alias.<имя>='!…'`, вызванный подкомандой; слова
за ним — аргументы строки псевдонима, `"$@"`, а не её текст). Маркер команды покрывает её строки `_scripts`, но не
подстановки в её словах: они — свои команды дерева и исполняются до неё. Флаги оболочки читаются до первого
позиционного слова или `--`; с `-n`, `-o noexec` или fish `--no-execute`, не снятыми дальше `+n` или `+o noexec`
(`pkgmanagers._noexec(flag, following, noexec)`: режим — последнее значение по порядку флагов), оболочка команды только
разбирает: ни строка `-c`, ни stdin не исполняются.

Слова команды даёт `_pairs` по дереву: ведущие присваивания (их узнал разбор при любом индексе: `a["]"]=1`, `a[b[1]]=1`,
`a[${x:-]}]=1`; маркер — присваивание, чей текст — ровно `PLANKA_DEP_OK=1`, без кавычек и `\`), затем слова;
перенаправления не входят, слово из одной процесс-подстановки — тоже (её вход и выход — связи). Значение слова —
`_word_text` (`_Word`, строка с узлом слова): кавычки и `\` сняты, `$'…'` раскрыта (символ с кодом 0 обрывает строку,
bash 5.3: `touch $'P1\0x'y` создаёт `P1y`), раскрытие (параметр, подстановка, арифметика) — своим текстом в команде, с
кавычками; раскрытие длиннее `_EXPANSION_MAX` (256) — заглушка `_PLACEHOLDER` (`$_`): значение неизвестно, а текст
повторял бы вложенный текст на каждом уровне. Начало слова до первой кавычки или `\` (`_word_lead`) решает, присваивание
ли слово за обёрткой и ключевое ли оно (`'A=1' npm i x` — команда `A=1`, `\if` — не ключевое слово). Фигурные скобки
слов вне присваиваний раскрываются, как в словах команды bash (`_brace_marks`: `{`, `,`, `}` литералов без кавычек и
`\`; `_brace_words`); слова раскрытия — с пустым началом: не присваивания и не ключевые слова. Слова `for … in`, `case`
и других составных команд — не слова простой команды: их скобки не раскрываются. `_BRACE_BUDGET` (8192) — один предел
на вызов `dependency_add` или `dependency_doubt`, общий для всех команд и вложенных строк: каждое слово раскрытия, в
том числе промежуточное, тратит свою длину и 1; слова с места, где предел кончился, не видны — `_strip_command` отдаёт
довод `cut` `_WHY_BRACES` (за `_WORDS_LIMIT` — `_WHY_CUT`), `_doubt` берёт `_WHY_BRACES` сомнением у любой команды
(ложный отказ, `touch f{1..5000}.txt`, снимает маркер), `_WHY_CUT` — если видимое имя — менеджер или запускатель
(`pkgmanagers._installer`: `_INSTALLERS`, `_PIP`, `_PYTHON`) или за пределом может стоять строка `eval`/`sh -c`
(`pkgmanagers._cut_script`). Отвергнуто: перечень хвостов, с которыми видимые слова ставят пакет (глагол установки с
пакетом, `npm install` за запускателем), — неполон: флаг со значением съедает глагол (`yarn --cwd {d,} add x`),
подкоманды из нескольких слов (`dotnet add package`, `dart pub add`, `swift package add-dependency`) и флаги-подкоманды
(`pacman -S`, `nix-env -i`, `python -m pip`) — пропуск; в настоящих командах (корпус `bash-commands.jsonl` и около 8,8
тыс. команд транскриптов) ни один сегмент не длиннее `_WORDS_LIMIT`. Отвергнуто: предел на одно слово — 15 КБ слов
`{1..99}{1..99}` только раскрытием `_brace_words` занимают 1,7 с; предел на текст — время растёт с числом сегментов и
проходов (`{1..9999};` подряд — 0,43 с на КБ, хук на 60 КБ — 26 с); с пределом на вызов — около 6 мс на КБ на оба
прохода. Ведущие слова снимает `_strip_command`: присваивания `_ENV_ASSIGN` (и `+=`, `a[i]=`) по началу слова,
`_KEYWORDS`, имя после `function` и после `coproc` перед составной командой (`_COMPOUND`), обёртки `_WRAPPERS` (`su`,
`runuser` — `_C_WRAPPERS`, `pkgmanagers._c_wrapper` разбирает флаги как getopt с перестановкой: строка
`-c`/`--command`/`--session-command`, у `runuser` без неё — слова после `-u пользователь`, без `-c` и `-u` — `<программа
-s или sh> [слова за пользователем]`, без слов — оболочка, читающая stdin (`_stdin_scripts`); строка `env -S` — снова
команда, её слова даёт разбор `shparse` (`_env_split`); с команды снимается не больше `_MAX_WRAPPERS` (16) обёрток, за
пределом — сомнение `_WHY_WRAPPERS`; `flock файл -c`; `watch` без `-x` — строка `sh -c` из склеенных слов,
`_WATCH_EXEC`), слова команды — до `_WORDS_LIMIT` символов от её первого слова. Имя команды — `pkgmanagers._basename`:
без каталога и суффикса Windows, в нижнем регистре (`NPM.CMD` — `npm`: macOS и Windows регистр не различают). Флаг,
склеенный со значением (`-uroot`), следующего слова не берёт (`pkgmanagers._takes_value`). Каждая из этих форм названа
в коде поимённо; неназванные формы частично ловят сомнения общего вида (`_WHY_COMPUTED`, `_WHY_LAUNCHER`, `_WHY_NAME`,
ниже), остальные проходят — `context/deferred/depcheck-enumerated-forms.md`. Корпус настоящих команд —
`tests/fixtures/bash-commands.jsonl` (`CorpusTest` в `tests/test_depcheck.py`).

`manifest_watch` берёт у детектора `_segments` (тексты простых команд, `_analysis`), `_command` (слова первой по тексту
простой команды и маркер) и `_split` (пары «слово, начало» первой простой команды с перенаправлениями по порядку текста:
перенаправление — пара «оператор с номером и целью слитно, оператор с номером», его узнаёт `_REDIRECT`; цель heredoc —
терминатор). `depcheck.heredocs` — вызов `shparse.heredocs`.

Не добавление: пробный прогон (`pkgmanagers._dry_run`: у npm — `_npm_dry_run`, у apt и apt-get — `_apt_parse`, у
aptitude — `_aptitude_simulates`, у brew — ещё `-n` и склейка с ним (`_brew_dry_run`; короткие флаги `brew install`
значения не берут, Library/Homebrew/cmd/install.rb, флаги со значением — `_BREW_VALUE_FLAGS`), у pip и
`_DRY_RUN_MANAGERS` — слово `_DRY_RUN` среди слов команды, у прочих — не пробный прогон, ниже; слово после `--` —
операнд, не флаг; у запускателей — `npx`, `corepack`, `python -m`, `… run` — проверяется вложенная команда), пакеты
своего workspace (`_LOCAL_PROTOCOLS`: `workspace:`, `link:`, `portal:`, `file:`; `pnpm --workspace`) и `--path` у cargo
и bundle. `corepack` и `npx`/`bunx`/`pnpx` (`_NPX`) разбираются как запуск следующей команды (версия `@…` у имени
снимается). `bash +c '…'` исполняет строку, как `-c` (проверено на bash 5.3).

Значение флага пробного прогона разбирается у npm и apt — там, где менеджер принимает его словом. npm (nopt) берёт
`true`/`false` следом за булевым флагом или после `=` (иное после `=` — отдельное позиционное слово, флаг — истина),
`--no-` в любом регистре обращает значение при каждом повторе, имя сокращается до `--dr`, действует последний флаг
(проверено `npm config get dry-run …` на npm 11.16). apt и apt-get (CommandLine apt, apt-pkg/contrib/cmndline.cc):
имя без учёта регистра, значение — после `=`, остаток склейки, следующее слово без `-` или слово перед `-` имени
(`--no-simulate`, `--yes-simulate`); булево — StringToBool (`_apt_bool`: `no|false|without|off|disable`,
`yes|true|with|on|enable`, целое C 0 или 1, пустая строка — ложь); не булево после `=` или перед `-` — ошибка
разбора, apt не выполняется — не добавление; `--no-act` — собственное имя пробного прогона, не отрицание. Флаг
уровня apt (IntLevel: `-q`, `--quiet`, `--silent`) съедает значение, если оно целиком целое strtol по основанию 10
(`pkgmanagers._apt_level`: после `=`, остаток склейки `-q2` или следующее слово без `-`, `apt-get -q 2 install jq`);
иначе уровень растёт без значения, слово остаётся операндом (`-q 2x` — `2x` подкоманда); не целое после `=` — ошибка
разбора, не добавление. `_apt_parse` возвращает слова команды без съеденных значений булевых флагов и флагов уровня,
подкоманду и пакеты ищут по ним (`apt-get -s no install jq` — установка). Значения флагов из `_APT_VALUE_FLAGS` (в том
числе `--planner`, `--comment`, `-S`/`--snapshot`, `--with-source`, `--cli-version` apt 3,
apt-private/private-cmndline.cc) в словах остаются: их пропускает `_subcommand`. Восьмеричное целое в `_apt_bool`
читается по основанию 10: у значений 0 и 1 результат тот же, прочие не булевы. У прочих
менеджеров флаг без значения: `false` следом — позиционное слово (`pip install --dry-run false x` — пробный прогон),
`--dry-run=false` и `--no-dry-run` — ошибка разбора (optparse у pip, clap у cargo и uv, cleo у poetry, symfony
console у composer; у `pnpm add` и `pnpm install <пакет>` 12.10 `--dry-run` нет — ошибка в любой форме). Команда с
`--dry-run` считается пробной только у менеджеров `_DRY_RUN_MANAGERS`, где флаг — пробный прогон или ошибка
разбора, и у pip; у прочих (yarn 1 незнакомый флаг пропускает и ставит пакет, проверено на 1.22; mix, choco, nuget,
winget, cpan, cpanm, port, scoop) — установка: пропуск открыл бы обход дописанным `--dry-run`. `--dry-run` сразу за
флагом со значением этого менеджера — сомнение (ниже). Отвергнуто: один разбор
`--dry-run false` для всех менеджеров — у pip, cargo, bun это пробный прогон с пакетом `false`, вышел бы ложный
отказ.

Сомнение — `depcheck.dependency_doubt`, второй проход тем же обходом (`_find_doubt`, `_segment_doubt`), когда
`dependency_add` добавления не нашёл: сегмент, где менеджер и команда установки распознаны, а пакет или подкоманда под
сомнением, или за `_MAX_WRAPPERS` снятыми обёртками стоит ещё обёртка (`wrapped`, любая команда, `_WHY_WRAPPERS`; маркер
— в начале сегмента), — отказ `judge_tool.DEP_DOUBT_REASON` с доводом (`_WHY_*`) и обходом маркером, в журнал
`deny-dep`. По правилу о краях эвристик (`CLAUDE.md`, «Правила проекта») это то же добавление пакета другой записью:
пропуск открыл бы обход проверки, ложный отказ снимает маркер. `_doubt` проверяет слова команды (`_strip_command`: ещё
снята ли обёртка `xargs`, довод `cut` — слова за пределом раскрытия скобок (`_WHY_BRACES`) или слово-не-флаг, начатое за
`_WORDS_LIMIT` (`_WHY_CUT`), и стоит ли обёртка за пределом) по очереди: обёртка за пределом (`_WHY_WRAPPERS`), заглушка
пакета `_STDIN_PACKAGE` в конце у xargs (`_WHY_XARGS`), слова за пределом (довод `cut`: `_WHY_BRACES` — всегда,
`_WHY_CUT` — у `_installer` и у `eval` или оболочки, чья строка `-c` или скрипт за пределом, `_cut_script`; у `python`
со скриптом, `-c` или `-` в видимых словах, `_python_target`, — не сомнение), `_is_add` в режимах `_MASKED` (`--dry-run`
сразу за флагом со значением этого менеджера, `_DRY_VALUE_FLAGS`, — значение флага), `_LOOSE` (`_loosened`: первые
`_LOOSE_FLAGS` пар «флаг без `=`, слово-не-флаг» перед подкомандой склеиваются в `--flag=value`, по варианту на k; не у
`_NO_SUBCOMMAND`) и `_DEEP` (за `_MAX_DEPTH` у запускателей — `_flat_add`), затем имя команды — подстановка или
переменная, за ним глагол `_INSTALL_VERBS` и слово-не-флаг по наборам `_EXPANDED_VALUE_FLAGS` (`_expanded_install`, `$M
install x`) или имя с пробелом в `${…}`, чьи слова значения по умолчанию со словами за именем — установка
(`_expanded_name_install`, `${x:-npm i x}`): менеджер известен только при исполнении (`_WHY_NAME`); затем `_is_add` в
режиме `_COMPUTED`: подкоманда менеджера с подкомандой, conda или `gem` — подстановка или переменная (`_expanded`), а во
вложенной команде и имя по `_expanded_install` — сомнение `_WHY_SUBCOMMAND`, кроме справки до `--` (`_asks_help(name,
args)`: `--help`, `-h`, флаги справки по менеджерам — `_HELP_FLAGS`). Режим переходит во вложенные `npx`, `python -m`,
`… run`. На глубине `_MAX_DEPTH` вложенные тексты (строки `_scripts`, тела heredoc оболочки, вход оболочки) проверяются
`_soup_add` (довод `_WHY_DEPTH`): кавычки и `\` сняты, команды по `;&|()`, обратной кавычке и переводу строки, в каждой
`_flat_add` берёт первое слово после присваиваний, флагов, обёрток, `eval`, оболочек, запускателей и `<менеджер> run`.
Отвергнуто: пропуск с предупреждением — обход проверки; спуск глубже `_MAX_DEPTH` без предела и перебор всех пар флагов
— время квадратично; общий набор флагов со значением всех менеджеров для `--dry-run` — ложный отказ на `pip install -q
--dry-run x` (`-q` со значением у winget). У npm `true`/`false` за флагом без `=` перед подкомандой — значение флага
(`_npm_subcommand`): nopt берёт их и у булева, и у незнакомого флага, — точное решение, не сомнение.

Последним `_doubt` проверяет неизвестную программу: `_launches_install` — программа не из `_KNOWN_PROGRAMS` и не из
`_DATA_PROGRAMS` получает словами менеджер `_PAIR_MANAGERS` и за ним глагол `_LAUNCH_VERBS` или флаг, и слова с
менеджера по его правилам — добавление или сомнение (`_is_add`, `_doubt`; `direnv exec . npm install x`, `docker exec c
npm --weird v i x`, `$PY -m pip install x`; `docker compose exec web npm install` и пробный прогон — нет): довод
`_WHY_LAUNCHER`. Каждая пара разбирает хвост слов целиком; больше `_MAX_LAUNCH_PAIRS` (16) пар — сомнение без разбора,
проход линеен.

`_segment_doubt` после `_doubt` проверяет ещё два сомнения общего вида. Имя команды — вывод подстановки: при доводе
`_doubt` `None`, `_WHY_SUBCOMMAND` или `_WHY_LAUNCHER` `_name_doubt` смотрит слово-имя с подстановкой (`$(…)`, `` `…`
``, `${ …; }` — и внутри `"…"` и операторов `${…}`) или с `${…}` с пробелом (`_name_expands`): bash исполняет вывод
командой. Слова текста слова-имени и тел heredoc, открытых в нём (кавычки сняты, `${…`-начала и `}` — пробелы,
разделители команд — границы), со словами за именем ставят пакет с какого-то места (`_is_add`,
`pkgmanagers._expanded_install`) — довод `_WHY_NAME` (`$(echo npm) i x`, `$(echo npm i x)`, `${ $(cat <<E` ⏎ `npm i x` ⏎
`E` ⏎ `); }`). Текст длиннее `_WORDS_LIMIT`, больше `_MAX_LAUNCH_PAIRS` мест с менеджером или текст сверх остатка
`_NAME_BUDGET` (4 × `_WORDS_LIMIT` на вызов) — сомнение без разбора: проход линеен.

Вычисляемая строка — `_computed_script`, довод `_WHY_COMPUTED`: строка, которую команда отдаёт оболочке (`eval`, `sh -c`
и прочие `pkgmanagers._C_SHELLS`, here-string оболочки, строки `_launched`), содержит внешнее раскрытие (в слове строки,
у `eval` — в любом слове за ним) или имя хотя бы одной её команды вычисляется (`_computed`: первое слово команды
начинается с `$` или `` ` ``; `$'…'` раскрыта и вычисляемой не считается). Внешнюю подстановку внешняя оболочка
вставляет в текст, а внутренняя разбирает его заново: bash 5.3 — `t='a $(touch P1)'; bash -c "x=( $t )"` создаёт P1,
`D='.; touch P2'; bash -c "cd $D && true"` — P2; пропуск открыл бы обход проверки. `_find_doubt` после команд текста так
же судит тела heredoc оболочки и вход из связей (`_stdin_scripts`): текст неизвестен (`curl … | bash`, `bash <(curl …)`,
`source <(curl …)`, `echo $X | bash`, `echo "$t" | bash`) или тело с внешним раскрытием (`bash <<E` ⏎ `$t` ⏎ `E`, `cat
<<E | bash` с `$t` в теле) — `_WHY_COMPUTED`; тело с терминатором в кавычках — текст как есть, его команды
проверяются.

Внешнее раскрытие (`depcheck._expands`) — в частях слова вне `'…'` и `$'…'`, в том числе внутри `"…"`: параметр, кроме
`_NUMERIC_PARAM` (`$$`, `$?`, `$#`, `$!` — число или пусто), подстановка `$(…)`, `` `…` ``, `${ …; }`, `${| …; }`; в
слове команды — ещё шаблон имён в литерале без кавычек (`_globs`: `*`, `?`, `[` с `]` за ним без `\` перед ними — имя
файла становится текстом, bash 5.3: `touch 'touch Q13'; bash -c *` создаёт Q13; here-string и тело heredoc bash шаблоном
имён не раскрывает). Не внешние: арифметика `$((…))`, `$[…]` (число: `bash -c "echo $((n+1)) $$"` печатает числа),
процесс-подстановка `<(…)`, `>(…)` (путь `/dev/fd/N`), тильда (домашний каталог). Признак несёт `_Word.expands`;
`_Word.shell` (`_shell_text`) — запись слова для повторного разбора, где раскрытия остаются раскрытиями: литерал — в
кавычках shlex (символы шаблона — без кавычек), `'…'` и `$'…'` — значением в кавычках shlex, `"…"` — в двойных кавычках
со `\` перед `\`, `"`, `$`, `` ` `` литералов. Строки из слов-аргументов (`find -exec`, `nix develop -c`, `mise exec
--`, `git submodule foreach`, `git bisect run`, аргументы псевдонима git) собирает `pkgmanagers._shell_join`: слово с
внешним раскрытием — его записью `shell` (повторный разбор видит раскрытие на месте: `find . -exec sh -c "x $1" _ {} \;`
— строка `sh -c` с раскрытием), без записи (слово раскрытия скобок, `_derived`) — в кавычках с заглушкой `"$_"` за ним,
прочие — в кавычках shlex. Строка, склеенная из слов пробелом (`ssh host …`, `cmd /c`, `pwsh -Command`, `watch` без
`-x`), или часть слова (`su -c"…"`, `--command=…`, `git -c alias.<имя>='!…'`, `rebase --exec=…`) несёт признак своих
слов (`pkgmanagers._joined`, `_part_of`; `_replaced` — подстановка `{}` у `find -exec` в слове и в его записи).

На корпусе `tests/fixtures/bash-commands.jsonl` (1405 настоящих команд) сомнения общего вида дают четыре ложных
отказа, все `_WHY_COMPUTED`: `sh -c "$(jq …)"` (образец с ключом `"//"`) и три `timeout … bash -c "until [ -s $F ];
…"`. Отвергнуто: широкий вариант `_launches_install` (любая неизвестная программа с менеджером среди аргументов, без
глагола установки и разбора слов менеджера) — вместе с любой вычисляемой строкой 13 ложных отказов на том же корпусе
(замер при правке). Формы и их ложные отказы — `README.md`, «Известные ограничения».

## Манифесты

Разбор — `manifests.py`, без состояния и ввода-вывода. `manifests.kind(path, fold)` — вид манифеста по имени файла
(`fold` — без учёта регистра, только базовое имя и только имена `_KINDS` — `_FOLDED_KINDS`; имена requirements и
каталоги — как написаны), сравниваемому до конца строки (`\Z`): `requirements.txt` с переводом
строки в конце имени — не манифест, поэтому имя манифеста перевода строки не содержит (возврат каретки — может, версию
HEAD такого файла `head_names` читает отдельным `git cat-file blob`); `manifests.names(kind, text)` — имена внешних
зависимостей, `None` — текст не разобран (JSON — `json`, TOML — `tomllib`, отсюда Python 3.11+; requirements, `go.mod`,
`Gemfile` разбираются построчно и `None` не дают). Файл требований читается, как его читает pip: логические строки —
`manifests._requirement_lines` (склейка по `\`, комментарий `(^|\s+)#`), строка — `_requirement_line` (до первого слова
на `-` — требование, остальное — опции: слова `_shell_words`, как у `shlex.split` posix, и разбор `_parse_options`, как
у optparse pip, — сокращения длинных опций, склейка коротких, `--`; оба — один проход, время линейно; опции, которые pip
не разберёт, — пусто). Строка с `${VAR}` (`_REQ_ENV`) — имя сама строка (`_env_line`): pip подставит значение при
чтении; у слова-URL (`_env_word`) снимаются ссылка `@…` и фрагмент `#…` (`_url_source`), если в снимаемой части нет
`${`; у архива — его пакет: колесо — `имя @ каталог`, как без переменной, другой архив — URL с именем файла без запроса
и фрагмента (имя пакета sdist из имени файла точно не выделить: новая версия sdist — новое имя).
Подключение файла `-r`, `-c` и `eval_gemfile "путь"` Gemfile — `manifests.Include`, строка `include <путь>` отдельного
типа: равна только подключению того же пути, не имени пакета с тем же текстом (`include @ git+…` — пакет); содержимое
подключённого файла ставится, а разбор его не видит. Имя подключения — путь цели от корня проекта
(`manifest_watch._rooted`): путь из манифеста разрешается от его каталога, как у pip и Bundler, и нормализуется
`os.path.normpath` без разрешения ссылок; у манифеста вне корня — абсолютный, путь со схемой URL — как записан. Так
нормализуются все источники имён: текст до и после правки (`edit_names(…, rel)`, `rel` — `_project_rel`: от корня как
написан или от его `realpath`), версии `head_names`, манифесты `project_names`, снимок `take` (в состоянии
`{"include": путь от корня}`), имена после команды в `compare`, деревья `_tree_names` (путь в дереве — от вершины git,
она же корень: `common.git_env` не пускает git выше корня). Одинаковый текст `-r base.txt` в `requirements/dev.txt` и в
корне — разные имена (`requirements/base.txt` и `base.txt`), `-r ../extra.list` из `requirements/` и `-r extra.list` из
корня — одно. Подключение снимает `manifest_watch._unchecked(names, listed)`, только если `realpath` цели от корня
проекта — манифест из перечня манифестов проекта: при правке (`check_edit`) — перечень `listing`, после команды
(`compare`) — перечень `list_manifests` после неё; такой файл проверяется сам. Файл, исключённый git, в перечень не
входит: его подключение — новое имя. Нормализация: PyPI — PEP 503 (`manifests._pep503`), Composer — нижний регистр без
платформенных пакетов
(`_COMPOSER_PLATFORM`), crates.io — `_crate`; npm, Go, RubyGems — как записаны. Транзитивные в `names` не входят: строки
`// indirect` в `go.mod` (`_GO_INDIRECT`). Файл требований с заголовком генератора или аннотацией `# via` разбирается,
как любой: заголовок впишет и агент вместе с пакетом. `build-system.requires` в `pyproject` без стандартных бэкендов
`_BUILD_BACKENDS` (руководство PyPA «Choosing a build backend»); прочее в `requires` — зависимость.
`manifests.known_names(kind, text)` — старая сторона сравнения: `names` вместе с транзитивными `go.mod` (`_KNOWN`);
ставшая прямой транзитивная зависимость не новая. Пакет не из реестра по умолчанию `names` пишет именем с источником
`имя @ источник` (`manifests._sourced`; git, URL, другой реестр: спецификатор npm не из реестра `_NPM_SOURCE`, PEP 508
`name @ url`, таблицы uv и poetry `_table_source`, Cargo `_cargo_source`, опции и блоки `git`, `github`, `source`
Gemfile `_gem_source`), источник для всех пакетов — `index <url>` (`manifests._index`: индексы uv, poetry, pdm и опции
requirements `_pypi_index` кроме PyPI, `repositories` composer кроме `path`, `source` Gemfile кроме RubyGems). У VCS
ссылка `@…` и фрагмент `#…` снимаются, у архива (`_ARCHIVE`: `.whl`, `.zip`, `.tar`, `.tgz`, `.tbz2`, `.txz`,
`.tar.gz|bz2|xz`) источник — каталог URL без имени файла, запроса и фрагмента (`_url_source`; у npm — для спецификатора
`http(s)://`): смена источника — новое имя, другой коммит, тег, ветка или файл архива в том же каталоге — нет. Ещё имена
дают overrides npm (`_npm_overrides`: `overrides`, `resolutions`, `pnpm.overrides`, только ключ, который ставит другой
пакет или источник — версия лишь закрепляет пакет), `patch.*` и `replace` Cargo, модуль-замена из другого пути в
`replace` `go.mod` (`_go_replace`), `tool.hatch.envs.*`, `pnpm.packageExtensions`, требования с URL в
`tool.uv.override-dependencies`, `constraint-dependencies` и `tool.pdm.resolution.overrides` (только источник:
ограничение без URL пакета не добавляет), репозиторий `package` composer (`dist.url`, `source.url` — источник его
пакета), список ограничений зависимости poetry (у элемента свой источник), гемы `plugin` и `(gem …)`, источники гема
`gist`, `bitbucket`, `gitlab` и ключ-строка `"git" =>` (`_GEM_SOURCE_KEYS`). Операторы строки Gemfile делит
`_ruby_statements` (`;` и `#` вне кавычек). Источник вне манифеста (`.npmrc`, `pip.conf`, `.cargo/config.toml`,
`go.work`, `GOPROXY`, `UV_INDEX_URL`) не виден. `manifests.own_name(kind, text)` — имя пакета самого манифеста (`_OWN`:
`name`, `project.name`, `tool.poetry.name`, `package.name`, `module`; у requirements и Gemfile `None`): оно не внешнее,
только если манифест в версии HEAD или ref до начала сессии (`_tree_names` добавляет имена пакетов манифестов дерева)
или член workspace корня (`manifest_watch._member` по `manifests.workspace_members`: `workspaces` корневого
`package.json`, `[tool.uv.workspace]` `members` и `exclude` корневого `pyproject.toml`, последний совпавший шаблон
решает) — такого члена менеджер ставит из каталога по имени. Имя свежего манифеста вне workspace пишет кто угодно, и
обход в два шага (новый `package.json` с `"name": "x"`, затем зависимость `x`) прошёл бы. Цена — ложный отказ на соседа
вне workspace корня: член `pnpm-workspace.yaml` (файл не читается), Cargo-член без `path`, модуль `go.work`, сосед вне
git. Местные источники `names` отбрасывает: `[tool.uv.sources]` с `workspace` или `path` (список — если местный хоть
один), `gem` с `path:`/`:path =>` и гемы блока `path … do … end` на любой глубине (`_GEM_PATH`, `_GEM_BLOCK`,
`_GEM_LOCAL`; источник гема — ближайший блок `path`, `git`, `github`, `source` в стеке блоков, прочий блок наследует
внешний; `source "url"` без блока (в каждом операторе `_ruby_statements`) и гем с опцией источника внутри `path` — с
источником; вложенность в блоке — по `do` в конце строки, ключевому слову блока в начале строки (`_RUBY_KEYWORD_OPEN`:
`if`, `unless`, `case`, `begin`, `while`, `until`, `for`, `def`, `class`, `module`), кроме блока в одну строку с `end` в
конце (`_RUBY_LINE_END`), и `end` в начале строки; лишнее открытие дешевле недосчёта: первое — пропуск гемов после
блока, второе — ложный отказ на местный гем), `replace x => ./…` в `go.mod` (каталог — `manifests._go_directory`, как
`modfile.IsDirectoryPath`: `.`, `..`, `./`, `../`, `/`, `\`, буква диска; замена с версией левой части `x vN => ./x`
местная, только если `vN` — версия `x` в `require`). Корпус настоящих манифестов — `tests/fixtures/manifests/`, ожидания
— `CORPUS` в `tests/test_manifests.py`.

Проверка — `judge_tool` и `manifest_watch`, без модели и без лимита отказов:

- Правка файловым инструментом — `judge_tool.judge_manifest_edit` на `EDIT_TOOLS` (`Write`, `Edit`, `MultiEdit`). Путь —
  `guard_memory.target_path`; пути и виды — `manifest_watch.edit_targets(path, root, listed)` → `([(путь, вид)],
  причина или None)`. Манифест по имени: `watched_kind` пути с разрешёнными ссылками от `realpath` корня проекта и пути
  как написан от корня; манифест хоть по одному из них — манифест, и проверяется путь, по которому он манифест (ссылка
  `deps.json` → `package.json`, каталог-ссылка `build` → `.`). На файловой системе без учёта регистра
  (`common.case_insensitive`; пробуется, только если путь в нижнем регистре — манифест) путь, путь с разрешёнными
  ссылками и корень приводятся к записям каталогов (`_on_disk`: каждый компонент — запись своего каталога с тем же
  именем без учёта регистра и тем же файлом, `os.listdir` и `os.path.samefile`; компонента нет на диске — он и
  остальные как написаны): `realpath` регистр не правит. Вид — по этим путям (`_named_target(cased=True)`), без учёта
  регистра только базового имени `_KINDS` (нового `PACKAGE.JSON` в каталоге ещё нет, а npm прочтёт его как
  `package.json`); имена requirements и каталоги — по записям: `REQUIREMENTS/BASE.TXT` — манифест
  `requirements/base.txt`, `requirements/x.txt` в настоящем каталоге `Docs/Requirements` — нет. Файл с несколькими
  жёсткими ссылками (`st_nlink` больше 1, обычный файл) — ещё `_linked_targets`: все манифесты из перечня проекта с теми
  же `st_dev` и `st_ino`, каждый по своему виду, каким бы ни было имя и каталог правленого пути и `FOREIGN_DIRS` в нём.
  Перечень — `listing(root)`: `list_manifests` в срок `HEAD_TIMEOUT`, один на правку (результат и ошибка запоминаются;
  его же берёт `_unchecked`). Не перечислить (срок, больше `MAX_MANIFESTS`) — причина: `common.warn_once` с ключом
  `manifest-hardlink` и `skipped` в журнал на каждую правку, манифест по имени проверяется. Второй и следующий манифест
  одного файла — в пределах `judge_tool.LINKED_BUDGET` («Сроки»). Путь с каталогом из `FOREIGN_DIRS` не
  манифест, каталоги ищутся в пути от корня — проект сам может лежать под `fixtures/`
  (`ManifestProjectUnderFixturesTest`); проект неизвестен (нет `CLAUDE_PROJECT_DIR` и абсолютного `cwd`) — путь как
  есть. Файл читает `manifest_watch._read` с `O_NONBLOCK`: FIFO не вешает хук, не обычный файл — `Unavailable`.
  `manifest_watch.edit_texts` повторяет инструмент: `Write` — `content`, `Edit` и `MultiEdit` — замены `old_string` по
  очереди с `replace_all`, текст файла для них с CRLF, приведёнными к LF (как Claude Code); не найден или неоднозначен —
  `None`, хук молчит: инструмент откажет сам. Единый путь сравнения — `manifest_watch.fresh_names(old, new, head,
  project)` (для правки его зовёт `edit_names`): новые имена против текста до правки, а если они есть или старый текст
  не разобран, — ещё против версий `_REPO_REFS`; старый текст не разобран и версий нет — сравнение с пустым: иначе битый
  манифест и затем зависимость прошли бы в два шага (`Write` с `{`, затем `Write` с пакетом). Версии `_REPO_REFS` читает
  `head_names` — объединение `known_names` версий HEAD, MERGE_HEAD, CHERRY_PICK_HEAD, REBASE_HEAD, REVERT_HEAD и
  REVERT_HEAD^ одним `git -C <каталог файла> cat-file --batch` — байты блобов без textconv и фильтров; имя файла с
  переводом строки не ложится в построчный ввод — тогда только HEAD через `cat-file blob HEAD:./<имя>`. Конфликт
  незавершённых merge, pull, cherry-pick, rebase, revert лежит в файле, но не в HEAD — версии источника его покрывают. К
  версиям `_REPO_REFS` тот же вызов добавляет стороны конфликта индекса `_INDEX_STAGES` (`:1:./<имя>`, `:2:`, `:3:` —
  база, наша, их). git пишет их при конфликте любой операции, в том числе `git stash pop|apply`, `git merge --squash`,
  `git cherry-pick -n`, которые ни `MERGE_HEAD`, ни `CHERRY_PICK_HEAD` не оставляют, и держит до `git add`/`git rm`
  файла; `git checkout --ours|--theirs` их не снимает. Стороны берутся из индекса, не из маркеров конфликта в файле:
  маркеры пишет любой, кто правит файл, в том числе агент (обход в два шага: `Write` с маркерами пропускается как «не
  разобран», затем разрешение с новым именем), а стадии пишет механизм слияния git, одним способом для любой операции;
  `stash@{0}`, `ORIG_HEAD`, `SQUASH_MSG` отвергнуты — у каждой операции свой след, у `cherry-pick -n` следа нет.
  Сторонам доверяется, как источнику незавершённого merge: конфликт `git stash pop` stash, созданного в сессии, тоже
  проходит — возраст ref сверяет только `restored_names` для команды; стадии, как и `MERGE_HEAD`, агент может записать
  сам (`git update-index --index-info`). Такой пропуск дешевле ложного отказа на WIP автора. Чистые `git stash pop`,
  `git apply`, `git checkout <ref> -- <манифест>` в `_REPO_REFS` не попадают (после команды `Bash` ref сверяет
  `restored_names`, ниже). Что осталось, сверяется с именами других манифестов того же реестра и пакетами самого проекта
  (`manifest_watch.project_names`, зовётся лениво через `judge_tool._project_names`, в пределах `SNAPSHOT_BUDGET`).
  Файлы `_LEGACY` (`setup.py`, `setup.cfg`, `Pipfile`, реестр PyPI; `manifest_watch.source_kind`, `_known`) дают имена
  только из деревьев git — версии HEAD (`_tree_names`, ниже) и ref до начала сессии (`restored_names`); файлы `_LEGACY`
  рабочего дерева имён не дают ни правке, ни снимку перед командой. Их правку не проверяет никто, поэтому имя из
  рабочего дерева открывало бы обход в два шага: агент пишет пакет в любой `setup.py` (неотслеживаемый, в подкаталоге),
  затем тот же пакет проходит в `pyproject.toml`. Отвергнуто: стадия 0 индекса (её пишет `git add` агента), стороны
  конфликта и `_REPO_REFS` для `_LEGACY` (лишние вызовы git ради редкого случая). Цена — ложный отказ на перенос из
  незакоммиченного `setup.py` и из любого `setup.py` вне git (`README.md`, «Известные ограничения»). `_setup_py` —
  `ast`, строковые литералы `_SETUP_KEYS` (`install_requires`, `setup_requires`, `tests_require`, `extras_require`)
  аргументов и ключей словаря с подстановкой присваиваний уровня модуля и `+` (`_literals`: обход стеком, каждый узел и
  каждое имя — один раз, время линейно по размеру дерева при любом ветвлении ссылок имён и циклах присваиваний; глубина
  не ограничена), `SyntaxWarning` разбора подавлен (хук stderr не пишет); `_setup_cfg` — `configparser`, `[options]` и
  `[options.extras_require]` без `file:`; `_pipfile` — ключи таблиц, кроме `_PIPFILE_NOT_PACKAGES`; строки требований
  разбирает `manifests.known_names("requirements", …)`. Как манифест эти файлы не проверяются: их имена только
  известные. Новые виды манифестов — работа `manifests.py`; `setup.py` — код, точный разбор невозможен: литералы через
  `ast` переоценивают известные (дешёвая ошибка — пропуск), требования, собранные кодом, не видны — остаётся ложный
  отказ. В git добавляются имена манифестов и файлов `_LEGACY` версии HEAD — `_tree_names(root, "HEAD", deadline)`: `git
  ls-tree -r -z --name-only --full-tree` и `git cat-file --batch`, общий код с `restored_names`; манифестов в дереве
  больше `MAX_MANIFESTS` — `Unavailable`, нет HEAD — имён нет. Член npm workspace, ссылающийся на соседа, которого ещё
  нет, — новое имя: точного знания нет ни в одном файле, а эвристика «тот же scope и `*`» пропускала бы явное
  добавление. Реестр вида — `manifests.registry` (`_REGISTRY`): package.json — npm, composer.json — packagist, pyproject
  и requirements — pypi, cargo — crates.io, gomod — go, gemfile — rubygems; сравнение по реестру, не по виду: имя из
  `requirements.txt` не новое в `pyproject.toml`. Новый не разобран, файл больше `MAX_MANIFEST_BYTES`, манифесты проекта
  не перечислить или их больше `MAX_MANIFESTS` (сообщение «не сравнён с другими манифестами проекта») —
  `manifest_watch.Unavailable`: предупреждение и `skipped` в журнал. Отказ — `deny_output(MANIFEST_REASON)`, в журнал
  `deny-dep` хука `MANIFEST_HOOK` (`manifest`) с `tool`, `added` и длиной и SHA-256 входа инструмента.
- Команда `Bash`: `judge_bash`, если команда пакет не добавляет, зовёт `snapshot_manifests`; команда с маркером
  (`manifest_watch.has_marker` — по семантике `depcheck`: `PLANKA_DEP_OK=1` ведущим присваиванием команды хоть одного
  сегмента; комментарий и аргумент не маркер, внутри `bash -c "…"` маркер не виден) не снимается. `manifest_watch.take`
  берёт список `list_manifests` — `git ls-files -z -c -o --exclude-standard`, вне git обход `_walk` (в обоих режимах без
  `FOREIGN_DIRS`, куда входит `snapshot.IGNORED_DIRS`), не больше `MAX_WALK_FILES` файлов, — не больше `MAX_MANIFESTS`
  манифестов и на каждый пишет `[size, mtime_ns, ctime_ns, имена с транзитивными или None, имя пакета манифеста или
  None]` (ctime ставит ядро при любой записи, `touch -r` возвращает размер и mtime, но не ctime):
  содержимое не хранится. Если команда возвращает файлы из ref (`manifest_watch.command_refs`), в снимок идёт поле
  `known` — `{реестр: имена}` манифестов и файлов `_LEGACY` ref, созданных до начала сессии (`restored_names`,
  ниже), и слова патчей `git apply`, не менявшихся с начала сессии (`command_patches`, ниже). Файлы `_LEGACY` рабочего
  дерева в снимок не входят: их имена после команды берутся только из версии HEAD (`project_of` в `compare`). Снимок с
  полем `known` `store` кладёт в `state/<session>.manifests.json` под `tool_use_id` и
  там же удаляет записи старше `ENTRY_TTL`. Сбой снимка и вызов без `tool_use_id` (снимок не с чем сопоставить после
  вызова) — `common.warn_once` с ключом `manifest-snapshot` и `skipped` в журнал на каждую команду.
- После команды — `judge_tool.check_command_manifests` на `POST_EVENTS`: `manifest_watch.pop` забирает снимок; `compare`
  — смена режима git/walk за команду (`git init`) или манифестов не перечислить (больше `MAX_MANIFESTS`, вне git файлов
  больше `MAX_WALK_FILES`) — сверяются только пути снимка, а причина — третий элемент `(added, unknown, lost)`:
  предупреждение «новые манифесты после команды не проверены» и `skipped` в журнал, блок по новым именам в манифестах
  снимка остаётся. Манифест с тем же размером, mtime и ctime пропускается, новый и не разобранный в снимке без
  версии git сравниваются с пустым (`fresh_names`), не разобранный после — в список предупреждения и `skipped`. Имя,
  объявленное в любом манифесте того же реестра в снимке, в `known` снимка (ref до начала сессии, с именами пакетов их
  манифестов) или пакет члена workspace корня (`_member`) в снимке и в манифестах после команды, не новое: `mv`, `cp`,
  `git mv`, член workspace, возврат работы автора. Остальное решает `fresh_names` с версиями `_REPO_REFS` и сторонами
  `_INDEX_STAGES`; в git остаток сверяется ещё с именами манифестов и `_LEGACY` версии HEAD и именами пакетов её
  манифестов (`project_of` в `compare`: дерево читается один раз и лениво, только если что-то осталось). Новые имена —
  `block_output(COMMAND_REASON)`: правка уже в файле, хук не знает, чья она (`git apply` патча из stdin или изменённого
  в сессии, ref моложе начала сессии), поэтому текст велит спросить автора, не откатывать вслепую и откатывать только
  свою правку; в журнал `block-dep` с числами `manifests` и `added`. Сбой `manifest_watch.pop` (`OSError`) —
  предупреждение и `skipped` в журнал; сбой записи журнала глотается.
- Вызов MCP-инструмента (`judge_tool.MCP_PREFIX`, `mcp__*`): MCP-инструменты правят файлы своими путями, и что правит
  вызов, хук не знает. Поэтому `judge_tool.main` на `PreToolUse` снимает тот же снимок `snapshot_manifests` (команды
  нет — `_command_and_id` отдаёт пустую строку: ни маркера, ни ref, ни патчей), а `check_command_manifests` на
  `POST_EVENTS` сравнивает его так же, как после команды `Bash`; новые имена — `block_output(MCP_REASON)`, в журнал
  `block-dep`. Цена — снимок манифестов на каждый вызов MCP-инструмента, в том числе на чтение.

Имена ref до начала сессии. `manifest_watch.command_refs(command)` по сегментам (`depcheck._segments`,
`depcheck._command`) находит, откуда команда git кладёт файлы в рабочее дерево без коммита: `git stash pop|apply
[N|stash@{N}]` (по умолчанию `stash@{0}`), `git merge --squash <ref>…`, `git checkout <ref> [--] <пути>` (один
операнд без `--` — ветка или путь, не ref), `git restore --source <ref>`, `git cherry-pick -n|--no-commit <ref>…`;
merge, cherry-pick и checkout ветки с коммитом не разбираются — их имена в HEAD или `_REPO_REFS` (`git apply` —
ниже, «Патчи»); `git -C` не учитывается. Начало сессии — `manifest_watch.mark_start`: на каждом `PreToolUse`
`judge_tool.main` (не на `POST_EVENTS`, не внутри судьи) пишет `state/<session>.start.json` с `int(time.time())`
при первом вызове, дальше содержимое не меняет, а mtime обновляет (`os.utime`): `common.prune_state` удаляет
файлы состояния старше `STATE_TTL` по mtime, и начало сессии, где `judge_tool` вызывался в пределах `STATE_TTL`,
остаётся; сбой каталога состояния глотается — без начала сессии ref не сверяются.
`manifest_watch.restored_names(root, command, start, deadline, cwd)` одним `git cat-file --batch` читает коммиты ref
(`_old_trees`) и берёт деревья тех, чьё время коммиттера меньше `start`, у stash — ещё дерево третьего родителя
(неотслеживаемые файлы `stash -u`); для каждого дерева `git ls-tree -r -z --name-only --full-tree` — пути манифестов и
файлов `_LEGACY` (`source_kind`, не больше `MAX_MANIFESTS`) и `git cat-file --batch` их блобов (не больше
`MAX_MANIFEST_BYTES`) — `known_names` по реестру (`_tree_names`). `start` `None`, ref моложе или не найден — имён нет,
сравнение блокирует, как без ref. Срок вышел, манифестов в дереве ref больше `MAX_MANIFESTS` или git не прочитал дерево
— `Unavailable`: `snapshot_manifests` снимок не сохраняет, предупреждение раз на сессию (`manifest-snapshot`) и
`skipped` в журнал, команда идёт без проверки. Сомнение решается пропуском (`plugin/rules/heuristics.md`) по правилу
«ошибка хука — пропуск, не блокировка»: блок после `git stash pop` автора без имён ref был бы ложным. `cat-file` ref вне
git-корня (`_old_trees`, ответа нет) — «ref нет», не сбой.

Патчи. `manifest_watch.command_patches(command)` — файлы патчей `git apply` по сегментам: операнды (флаги со значением —
`_APPLY_VALUE_FLAGS`, после `--` — все) и, если операндов нет или операнд `-`, цель перенаправления `<`; heredoc,
конвейер и `git -C` не разбираются. `_old_patch_names` открывает файл с `O_NONBLOCK` (FIFO не вешает хук), читает
обычный файл не больше `MAX_MANIFEST_BYTES` и сверяет ctime после чтения: меньше `start` — патч не менялся с начала
сессии. ctime ставит ядро при любой записи и смене атрибутов, `touch` назад его не ставит (POSIX, «Платформы»).
Относительный путь — от `cwd` аргумента `restored_names`, без него — от корня; `snapshot_manifests` передаёт каталог
команды до неё (`cwd` входа `PreToolUse`), `cd` внутри команды не учитывается. `_patch_names` берёт блоки, чей файл по
заголовку `---`/`+++` — манифест или `_LEGACY` (`source_kind`), считает строки блока по числам `@@` (строка `--- x`
внутри блока — содержимое) и из строк `+` и `-` берёт слова `_TOKEN`, нормализованные по реестру (`_NORMALIZE`):
надмножество имён, лишнее слово — пропуск, дешёвая ошибка.

Предел точности. Время ref — время коммиттера; запись reflog берёт ту же `GIT_COMMITTER_DATE`
(`GIT_COMMITTER_DATE=… git stash` пишет дату и в коммит, и в `logs/refs/stash`), mtime объекта ставит `touch`. Не
подделать только ctime файла объекта, а он отвечает «записан в сессии», не «кем»: коммит, полученный `git fetch` в
сессии (до `transfer.unpackLimit`, 100 объектов, — отдельным файлом объекта), и stash автора, упакованный в сессии
`git gc` или `git maintenance`, записаны в сессии так же, как коммит агента с поддельной датой, и отказ по ctime
блокировал бы `git fetch && git merge --squash`; признаки «пришло с remote» (reflog `fetch:`, `refs/remotes`,
`FETCH_HEAD`) агент пишет сам. Кто создал ref или патч в прошлой сессии, не хранят ни git, ни состояние planka (по
сессиям, `STATE_TTL`). Поэтому поддельная дата, ref и патч прошлой сессии агента проходят как работа автора
(`README.md`, «Известные ограничения»): проверка ловит невнимательность агента, а намеренный обход и так проще —
маркером `PLANKA_DEP_OK=1` или записью стадий индекса (выше). Патч из stdin, heredoc или конвейера не несёт ни ref,
ни файла — блок.

Генератор requirements. Файл, который пишет генератор (`pip freeze > requirements.txt`, `uv export -o …`), и файл с
заголовком генератора или аннотацией `# via` проверяются после команды по содержимому, как прочие манифесты: хук не
знает, выполнился ли генератор и писал ли он последним (`echo x >> requirements.txt; false && pip freeze >
requirements.txt`), а заголовок впишет и агент. Новые имена — блок, обход — маркер `PLANKA_DEP_OK=1` перед командой
генератора по согласию автора. Отвергнуто: узнавать генератор по словам команды и снимать проверку с файлов его вывода
— запись в тот же файл другим сегментом той же команды проходила бы без проверки.

## Состояние и журнал

Каталог данных — `$CLAUDE_PLUGIN_DATA`, без него `.data/` в корне плагина (`common.data_dir`).

- `state/<session>.json` — счётчики отказов; `state/<session>.warned.json` — выданные однократные предупреждения
  (`common.warn_once`; сбой чтения или записи файла — `OSError` глотается, предупреждение выдаётся как обычное и может
  повториться); `state/<session>.snap.json` — снимок дерева текущей реплики (`snapshot.store`, поле `format` =
  `snapshot.FORMAT`, 2; блоки режима walk — строкой `snapshot._pack_dirs`; поле `checked` — отметил ли `Stop` снимок
  проверенным, `snapshot.mark_checked`; снимок без поля считается проверенным); `state/<session>.start.json` — начало
  сессии `{"start": секунды}` (`manifest_watch.mark_start`: содержимое пишется один раз, mtime обновляется на каждом
  `PreToolUse`); `state/<session>.debug.json` — неудачи команд (`counts`, последние `debug_watch.MAX_COUNTS`) и отметки
  показа модуля по агентам (`shown`, последние `MAX_SHOWN`); `state/<session>.manifests.json` — снимки манифестов перед
  командами `Bash`, `{tool_use_id: {root, mode, ts, files: {путь: [size, mtime_ns, ctime_ns, имена или None, имя пакета
  или None]}, known?: {реестр: [имена]}}}`, имя — строка, подключение `manifests.Include` — `{"include": путь}`
  (`manifest_watch._dump_names`, `_load_names`, `_valid_names`; `manifest_watch.store`, `manifest_watch.pop`; запись не
  той формы `pop` отбрасывает — снимок прошлой версии плагина, команда идёт без сравнения; `known` — имена манифестов и
  файлов `_LEGACY`
  ref до начала сессии и имена пакетов их манифестов); `state/<session>.snapfail.json` — неудачи снимка дерева `{корень:
  причина}` (`snapshot.mark_failed`, `snapshot.failure`); `state/<session>.model.json` — модель сессии `{"model": имя}`
  (`model_watch.store`, `common.session_model_path`; запись под `state_lock`, чтение `common.stored_session_model` — без
  него, файл заменяется целиком). Имя — `common.safe_name`. Запись атомарная (`common.atomic_write_json`: временный
  `.tmp-*` и `os.replace`); чтение — `common.read_json` (нет файла, битый JSON или значение не того типа — пустое
  значение); чтение и запись счётчиков, предупреждений, неудач команд, снимков манифестов, неудач снимка дерева, запись
  модели сессии, начала сессии (`manifest_watch.mark_start`) и снимка дерева (`snapshot.store`), перепривязка и отметка
  снимка дерева (`snapshot.carry_over`, `snapshot.mark_checked`) — под `fcntl.flock` на `state/.lock`
  (`common.state_lock`). JSON пишется через `common.dumps`: одиночный суррогат в имени файла не в UTF-8 — escape
  `\udcXX`. Файлы `*.json` и брошенные `.tmp-*` старше `STATE_TTL` (7 дней) удаляет `common.prune_state`.
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
  --untracked-files=all --ignore-submodules=all` (`snapshot._status`; ещё режим отслеживаемого пути в HEAD — поле
  `"modes"` записи репозитория `_git_state`, в файл снимка не идёт: `mH` записи `1`, `mH` исходного пути
  переименования `2` (нового пути в HEAD нет), `m2` записи конфликта `u` — сторона «ours», HEAD; `m1` — база
  слияния; новый путь переименования, заменённый ссылкой, не сообщается, сообщается исчезнувший исходный путь). В
  снимке `repos` —
  `{префикс: HEAD}`, `dirty` — `{путь: [size, mtime_ns] или None}`, `head` и `sub_heads` — базы для
  `comments.extract`. Размер снимка не зависит от размера дерева. `MAX_FILES` (50 000) ограничивает число
  грязных путей: они хранятся словарём, объектом на путь, в памяти и в JSON; больше — `snapshot.TooManyFiles`,
  `capture` его не ловит. Вложенный репозиторий, где git не работает, обходится `_walk_paths` без предела
  числа файлов: его файлы — грязные пути родителя, и предел `MAX_FILES` на них действует уже в `capture`.
- `walk` — вне git: `dirs` — `{каталог от корня через «/», "" — корень: блок}` обхода `snapshot._walk_dirs`.
  Обход `snapshot._walk` — стек каталогов и `os.scandir`, `IGNORED_DIRS` и упрощённый `.gitignore` по
  `snapshot.ignore_rules` (правила родителя плюс свои, на поддерево; `.gitignore` читается, только если это обычный
  файл — не ссылка и не FIFO, как у git); символические ссылки не обходятся и в
  снимок не попадают, непрочитанный каталог пропускается. Блок каталога — записи его обычных файлов «имя в
  байтах ФС, NUL, `snapshot._STAT` (size, mtime_ns)», отсортированные: каталог без правок даёт тот же блок при
  любом порядке readdir. Объекта на файл нет — память около размера блоков. Предела числа файлов нет:
  обход ограничен только сроком, срок проверяется на каждом каталоге и раз в `STAT_CHECK_EVERY` файлов.
  В файл снимка блоки идут одной строкой `snapshot._pack_dirs` — base64 от zlib (уровень 1, сжатие по
  каталогу) записей «каталог, NUL, `_LEN` длины блока, блок»; `load` разворачивает её `_unpack_dirs`. Упаковка
  растёт с деревом и входит в срок снимка: `store(…, deadline)` проверяет его на каждом каталоге и через каждые
  `PACK_CHUNK` байт блока, вышел — `TimeoutError`, файл не пишется.

`snapshot.changed_since(root, snap, deadline)` даёт `[(путь, обычный ли файл сейчас)]`. В git кандидаты — грязные пути
на старте и сейчас и, для репозитория, чей HEAD сменился, пути `git diff --raw` против HEAD снимка с режимом в нём
(`snapshot._since_commit`; без HEAD — все отслеживаемые, `ls-files -s`); из них отбрасываются пути, чей stat совпал со
снимком. Ссылка, подмодуль или каталог на месте пути, который не был обычным файлом (новый путь, ссылка и подмодуль в
git), пропускается; обычный файл git (режим `_REGULAR_MODES` в HEAD сейчас или в HEAD снимка), заменённый ссылкой, —
`(путь, False)`: файла больше нет. В walk — `snapshot.diff` блоков двух обходов: равные блоки каталога пропускаются
целиком, у разных сравниваются записи (`snapshot._records`). `None` с предупреждением — смена режима, пропавший
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
`judge_stop.main` после `Stop` без блока зовёт в `finally` `release_snapshot` → `snapshot.mark_checked`; блок снимок не
отмечает — признак блока `judge` кладёт в список `blocked` сразу после `common.emit`, до записи журнала, так что и
исключение после блока его не теряет; исключение без блока (в том числе после вердикта ok или пропуска) снимок отмечает.
Ошибка судьи, лимит отказов, нет рубрики, неопределимые изменения — отмечают: иначе при постоянной ошибке судьи база
растёт без конца, а «не определить» предупреждало бы на каждом `Stop`. Блок не отмечает: `Stop` после блока приходит
снова, когда агент закончит, а если автор прервёт реплику после блока, правки проверит следующая реплика. Формат
меняется аддитивно, поэтому `FORMAT` не поднят: снимок без поля `checked` перезаписывается, как прежде. Неудача снимка
запоминается для корня, а не гасит только предупреждение: снимок (`git status --untracked-files=all` или обход) иначе
повторялся бы на каждом `UserPromptSubmit` и съедал до срока каждую реплику; повтор через N реплик отвергнут — для него
нет ясного условия.

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
UTF-16 этот байт входит и в другие знаки): строка текста k занимает git-строки `[номера[k - 1], номера[k])`, и `extract`
берёт её комментарий, если добавлена любая из них. Heredoc Ruby и Perl (`rb`, `pl`, `pm`, `Rakefile`, `Gemfile`;
`comments._RUBY_HEREDOC`: `<<ID`, `<<-ID`, `<<~ID`, идентификатор и в кавычках — там любые знаки, кроме этой кавычки,
`<<'----END----'`) — данные. Сдвиг или добавление от heredoc отличает `comments._heredoc_ok`: `<<` сразу после слова или
закрывающей скобки — сдвиг (`1<<BITS`, `a[0]<<X`, `print $fh<<EOF`), кроме слова из `comments._tight_heredoc`: в Ruby —
`comments._RUBY_BEG` (`return<<EOS`; за ними лексер Ruby в состоянии EXPR_BEG/EXPR_MID), в Perl — `comments._PERL_TIGHT`
(встроенные функции, ждущие аргумент, `comments._PERL_TERM`, и say: `print<<EOT`, `lc<<EOS`) и дескриптор из заглавных и
`_` за `print`, `printf`, `say` (`print CSS<<EOF`); слово с сигилом, после `.`, `->`, `::` (`comments._member`) — не
встроенное. После пробела за скобкой, кавычкой или переменной (`$a`, `@a`) — сдвиг; в Perl исключение — дескриптор после
`print`, `printf`, `say` (`$fh`, `STDOUT`, `STDERR`, слово ищет `comments._after_print`; блок `{$fh}`, `{$DB::OUT}`,
`{$self->{fh}}`, `{*STDOUT}` — и вплотную, `print{$fh}<<EOF`, — узнаёт `comments._print_block`, парную `{` берёт
`comments._brace_pairs`: скобки строки считаются один раз, разбор линеен при любой длине блока): `print $fh <<EOF` —
heredoc, в Ruby это сдвиг. За словом Ruby — как лексер Ruby: за числом, локальной переменной (`names`) и
`comments._RUBY_VALUE` (`self`, `nil`, `true`, `false`, `end`…) — сдвиг и добавление, за методом (и после `.`, `::`, с
`?`, `!` на конце) и константой — heredoc при любом идентификаторе (`puts <<eof`); `foo?<<x` — сдвиг. За словом Perl: из
`_PERL_TERM` — heredoc, из `comments._PERL_VALUE` (функции без аргументов, `__LINE__`, `__FILE__`, `__PACKAGE__`) —
сдвиг, за прочим словом — heredoc, только если идентификатор с `-`, `~`, в кавычках или с заглавной буквы (ведущие `_`
не в счёт, `<<_EOUSAGE_`), иначе сдвиг. Таблицы встроенных слов Perl сверены с `perl -MO=Deparse` (Perl 5.42), правила
Ruby — с Prism. После другого знака (`=`, `(`, `,`) и в начале строки — heredoc. Дешёвая ошибка — heredoc, принятый за
сдвиг: его тело читается как код и показывает судье лишние строки.

Heredoc Ruby, Perl, PHP и Terraform без строки-терминатора до конца файла — не heredoc: `_comments` повторяет
разбор `_parse`, запретив открывать heredoc в этих позициях (`banned`); всего разборов не больше
`comments._MAX_REPARSE` (8) — каждый следующий снимает хотя бы одно открытие; тело такого heredoc читается как
код. Heredoc оболочки в общем движке (`heredoc="shell"`, zsh; открытия — `shparse.heredocs`) так не повторяется. Так
же откатываются строка с подстановками и
многострочный литерал без закрытия до конца файла (строка из `_Syntax.strings`, оператор-кавычка Perl, литерал
`%` Ruby, многострочная регулярка Ruby и Perl, сигил Elixir, slashy-строка и dollar-slashy Groovy): позиция открытия
идёт в `banned`, литерал читается однострочным. Тройная кавычка без закрытия — пустая строка и кавычка, как у лексера с
длиннейшим совпадением (Groovy).

Файлы `sh` и `bash` (`comments._TREE_SHELL`) и shell-скрипт блочного скаляра YAML (`comments._yaml_script` с `sh`)
разбирает `shparse` (`comments._shell_comments`): комментарии — `Script.comments` дерева, BOM в начале и `\r` перед
`\n` сняты, «#!» первой строки — не комментарий. bash прекращает чтение на фатальной синтаксической ошибке, но
комментарии после неё нужны судье (отказ без них дороже лишней строки): остаток разбирается заново со строки после
ошибки, не больше `_SHELL_RECOVERIES` (64) раз, — работа линейна по длине текста. Комментарий на строке ошибки после неё
(`echo ) # d`) и комментарии за 65-й ошибкой в вывод не попадают. Исключение в `shparse` — комментарии даёт общий движок
(`comments._SYNTAX`). zsh читает общий движок: грамматика zsh — не bash.

Регулярные выражения, slashy-строки и JSX — поля `comments._Syntax` `regex` и `jsx`. `regex="js"` (js, jsx, mjs, cjs,
ts, tsx, mts, cts): `/` в начале выражения — литерал `_JS_REGEX` (escape, класс `[...]`, флаги), незакрытый в строке
кончается с ней; `//` и `/*` раньше — комментарии. Начало выражения решает `comments._js_expr_start` по предыдущему
токену: назад через пробелы знак из `_EXPR_AFTER` или `/` деления, слово из `_EXPR_KEYWORDS` не после `.`
(`comments._expr_start`); `++` и `--` не меняют ответ токена перед собой (серию `+` лексер делит на `++` слева,
`comments._postfix`), в начале строки — префикс. Токены, которых не видно по знаку, `_parse` пишет в `known` строки
{конец: начинается ли выражение}: `)` заголовка `if`, `while`, `for`, `with` — да (стек скобок `comments._brackets`,
как acorn), конец регулярки — нет, блочный комментарий — ответ токена перед ним (`before`, и для многострочного). Слово
просматривается не дальше `_KEYWORD_MAX` + 1 знаков — время линейное. В начале строки решает `fresh`: в JS и Perl — по
концу прошлой строки кода, в Ruby всегда начало. `regex="groovy"` (groovy, gradle) — как лексер Groovy 4
(`GroovyLexer.isRegexAllowed`, ожидания тестов сверены с `GroovyLangLexer`): `/` — slashy-строка, если перед ней
(`comments._groovy_slashy_ok`) не значение: имя, число, `this`, `null`, `true`, `false`, `)`, `]`, `}`, кавычка, `++`,
`--`, конец slashy-строки (`known`); любое ключевое слово `comments._GROOVY_KEYWORDS`, и после `.`, — не значение. В
начале строки — начало выражения, кроме строк внутри `(` (не `try (`) и `[`: там перевод строки лексер пропускает
(`comments._newline_hidden`), решает конец прошлой строки кода. Slashy-строка — элемент `tpl` стека `ctx`
(`comments._template`): многострочная, `${` открывает код подстановки, обратная косая — escape только перед `/`; без
закрытия до конца файла — деление (откат `banned`). Dollar-slashy `$/…/$` — в любом месте выражения, кроме `a$/` (имя
`a$`), многострочный литерал с escape `dollar` (`comments._close`: `$$`, `$/` — escape), без закрытия — имя `$`.
Строка Groovy в кавычках — `strings` с многострочностью "cont": на следующую строку только за нечётной серией `\` в
конце строки. `jsx=True` (js, jsx, tsx; в ts `<T>(x) => x` — обобщение) — `<` в начале выражения, за которым
`comments._jsx_opens` видит тег (`>` фрагмента или имя, за ним `>`, `/`, `{`, атрибут или конец строки; `<T,>`,
`<T extends X>`, `<T = X>` — обобщение), открывает разметку: `comments._markup` ведёт стек `ctx` (`text` — дети
элемента, `tag` — открывающий тег, `close` — закрывающий, `code` — код в `{…}` со счётчиком скобок, его разбирает
`comments._code`). Текст между тегами — не код; в теге `//` и `/* */` — комментарии, строки атрибутов — многострочные
без escape. Тег или текст, не закрытые к концу файла, `_parse` возвращает в незакрытых, и `_comments` повторяет разбор,
запретив открывать их (`banned`), как heredoc без терминатора. Шаблонная строка JS, строки Ruby `"…"`, `` `…` `` и
slashy-строка Groovy — элемент `tpl` того же стека: кавычка закрывает строку, `${` и `#{` открывают `code`
подстановки; комментарий в подстановке — настоящий, кавычки в ней строку не закрывают. `<<` в коде JSX — сдвиг, не
тег.

Однофайловые компоненты Vue и Svelte — поле `sfc` (`"vue"`, `"svelte"`), как их компиляторы: `comments._sfc` вырезает
блоки `<script>` и `<style>` (`_SFC_BLOCK`, атрибуты — до `>` вне кавычек `_SFC_ATTRS`, конец — `_SFC_END`, сырой текст
HTML) и разбирает их содержимое синтаксисом по атрибуту `lang` (`_SFC_LANGS`: без `lang` — `mjs` и `css`; неизвестный
`lang`, `coffee`, `pug`, — без комментариев), номера строк — по всему файлу. Остаток — разметка (`_reparse` с вершиной
стека `ctx` `["text", None]`, `comments._markup` с `sfc`; тег открывают `<` и буква ASCII, `comments._html_tag`): текст
— данные, `<!-- -->` — комментарий, вложенность элементов не ведётся (пустые элементы HTML без закрытия), в теге `//` и
`/*` — текст; код — `{{…}}` Vue и `{…}` Svelte (у Svelte и в теге, `_SFC_TAG`; `{/if}`, `{:else}` — не код) разбирается
как JavaScript. Отвергнуто: разбирать весь файл одним синтаксисом с `//`, `/* */` и `<!-- -->` — URL в тексте шаблона
давал ложный «комментарий». Значение директивы Vue (`comments._vue_directive`: имя с `v-`, `:`, `@`, `#`, `.`) — литерал
до первой такой же кавычки; при закрытии его текст с раскрытыми сущностями HTML разбирается как `mjs`
(`comments._vue_expr`). Строка атрибута Svelte в кавычках — элемент `["tpl", позиция, кавычка, ""]`: текст с кодом
`{…}`.

`php=True` (php): файл — HTML с блоками PHP. Вершина стека `ctx` — `["html"]`; `comments._php_html` ищет `<?php` (перед
пробелом или концом строки), `<?=`, `<?` (не `<?xml`) — кадр `["php"]`, код до `?>` вне строк и блочных комментариев
(`_code`); `?>` кончает и строчный комментарий (вывод — хвост строки); `<!-- -->` — комментарий; `<script>`, `<style>` —
кадры `stag` и `raw`: содержимое разбирается по `css` или `mjs` (`<script>` с `type` не JavaScript, `_JS_TYPES`, —
данные), блок PHP в нём — код PHP, а для разбора JS и CSS — имя `_` (`comments._php_raw_done`: `/<?= $p ?>/g` — не
`//g`); в тексте комментария имени нет, хвост из одних блоков PHP — не комментарий; без закрытия — до конца файла. Блок
`<?php` внутри `<!-- -->` и внутри значения атрибута тега `<script>`, `<style>` в кавычках — текст. Вывод `_parse` для
php сортируется по номеру строки. Строки PHP `'…'`, `"…"`, `` `…` `` многострочны: `?>` и `<?php` в них — текст.

Строки с подстановками кода — поле `interp` и `comments._Interp`: закрытие строки ищет `comments._close_interp` в
пределах строки файла — стек кадров строк и кода подстановок со счётом скобок; строку в коде подстановки узнаёт
`_string_at`, подстановку — `_interp_at`. Языки: kt, kts, groovy, gradle `"`, `"""` — `${`; swift — `\(`; cs — `$"`,
`$@"`, `@$"` — `{`, `{{` — текст; py — f- и t-строки, `{{` — текст; у сырых (с `r`) `\` берёт следующий знак, только
если это первый знак закрытия или `\`, и `\{` открывает подстановку; `:` на нулевой глубине `{}`, `()`, `[]` кода
подстановки открывает спецификацию формата (`_Interp.spec`, `_PY_SPEC`) — текст до `}` с вложенными `{…}`: `'` в
`f"{x:'>10}"` — не строка; dart — кроме `r'…'`; ex — `#{`; sh `"…"` — `$(`, `${`, `` ` ``, `\` в коде подстановки
берёт следующий знак (`code_esc`); tf — `${`, `%{`, `$${` и `%%{` — текст. Строка внутри подстановки внешнюю не
закрывает;
комментарий внутри подстановки не выводится; подстановка, не закрытая в строке файла, оставляет строку незакрытой.
`$'…'` sh — строка с escape `\`.

`regex="ruby"` и `regex="perl"`: `/` и `%` (Ruby) — литерал по `comments._literal_opens`: в начале выражения (не
после `}` — это элемент хеша), после слова из `comments._TERM_WORDS` (Ruby — `comments._RUBY_BEG`: `if`, `unless`,
`and`, `or`, `when`, `return`, `else` и подобные; Perl — `comments._PERL_TERM` и `when`: встроенные функции, ждущие
аргумент, и операторы `eq`, `cmp`, `xor`… — и вплотную, и перед пробелом: `split/\s+/`, `split / /`, `shift / 2` —
регулярка, многострочная) или аргументом вызова — после слова через пробел и вплотную к следующему знаку, кроме `=`: в
Ruby `/=` после слова — всегда присваивание с делением (так лексер Ruby: `/=` не в начале выражения — `tOP_ASGN`; `foo
/=#/` — `foo /=` и комментарий), в Perl — только `=` с пробелом за ним (`print /=#/` — регулярка, `$a /= 2` —
присваивание); `puts %w(a)` — литерал; после слова из `comments._VALUE_WORDS` (Ruby `self`, `nil`, `true`, `false`,
`end`…; Perl `time`, `wantarray`, `__LINE__`…) — деление; имя метода Ruby с `?`, `!` на конце (`foo? /`) — слово; после
числа, переменной (`$a /2`) и локальной переменной Ruby (`names` из `comments._ruby_locals`: присваивание `a = …`, `a
||= …`, `a, b = …`, параметры метода в скобках и блока после `do` и `{`, списки параметров не длиннее 256 знаков; не
после `.`) — деление; `:/` и `:%` Ruby — символы. Буквы за закрывающим разделителем литерала — флаги
(`comments._FLAGS`), не оператор-кавычка: `/a/s, @x` — не `s,…,`. Регулярка-аргумент, не закрытая в строке, кончается с
ней; в начале выражения — многострочная. Операторы-кавычки Perl (`comments._PERL_QUOTE`: `m`, `s`, `qr`, `tr`, `y`, `q`,
`qq`, `qw`, `qx` не после знака слова, сигила, `->`, `::`; `s` за `-` и перед знаком не слова — файловый тест `-s($f)`,
как читает toke.c, а `--s` — декремент и `s`; разделитель — вплотную любой знак не слова, кроме закрывающих скобок, `;`
и `=>`, через пробелы — `/` и открывающая скобка) и литералы `%` Ruby разбирает `comments._quote_parts`: скобки
вкладываются, escape — обратная косая; у `s`, `tr`, `y` две части, вторая — тем же разделителем или своими скобками
через пробелы или на следующих строках (`comments._second_part`); многострочный литерал — состояние разбора с
вложенностью (`comments._close_nested` с escape). Многострочная регулярка (`m`, `qr`, `s`, `%r`, `/…/`, сигил `~r`): `#`
после пробела в ней — комментарий режима /x (`comments._XRE_COMMENT`); номера таких строк вывода `_parse` копит в `xre`
и снимает их, если за закрытием литерала (у `s`, `tr`, `y` — за второй частью) нет флага `x`; вторая часть в скобках — и
на следующих строках за пробелами и комментариями (perlop): закрыв первую часть, `_parse` ждёт её в состоянии `"await"`
(`comments._line_rest_empty`; из закрытой в строке первой части — действие `("await", …)` `_quote_parts`),
строки-комментарии между частями — настоящие; флаги неизвестны, только если второй части нет. Вторая часть `s{…}{…}` в
`{}`, не закрытая в строке, — литерал со вложенностью (`then="code"`), его текст копится в `body`; при закрытии с флагом
`e` текст разбирается как код Perl рекурсивным `_parse` (`level`; повторный разбор всех вторых частей файла — не больше
`comments._CODE_BUDGET` · `len(text)` знаков, бюджет на весь `_parse`, глубина — не больше `comments._MAX_CODE_DEPTH` =
64, предел рекурсии Python: разбор линеен), без `e` — строка. Heredoc Perl — и с пробелами перед идентификатором в
кавычках (`<< "DONT"`, `comments._RUBY_HEREDOC`; в Ruby `x << "a"` — добавление). `$"`, `$'`, `` $` `` — переменные;
строка `__END__` или `__DATA__` — дальше данные, кроме POD. Строки Ruby, Perl, Julia, PowerShell многострочные
(PowerShell: escape `` ` ``, here-string `@"…"@` и `@'…'@`). `sigil` — сигилы Elixir (`~r/…/`, `~w(…)`, с тройными
кавычками), многострочные, без вложенности. `block_scalar` (yaml, yml) — `comments._yaml_block`: строка, код которой
кончается индикатором `|` или `>` после `key:` или `-` (тег и якорь перед ним не мешают), открывает блочный скаляр; его
строки — с отступом больше родителя и пустые. Индикатор один на строке (`run:` и `  |` строкой ниже) — значение ключа
прошлых строк: родитель — последний ключ стека `keys` левее индикатора, без него — `-1` (скаляр всего документа). Ключ
скаляра — на его строке (`comments._YAML_KEY`), у `- |` и у индикатора на своей строке — по стеку открытых ключей `keys`
(`comments._yaml_key`, `comments._yaml_parent`: последний ключ с колонкой не больше колонки `-`, список без отступа — в
колонке ключа); пространство имён снимается (`ansible.builtin.shell` → `shell`). Синтаксис скрипта выбирает
`comments._yaml_script_ext`: вход `script` под `with:` шага, чей `uses` — `actions/github-script`
(`comments._GITHUB_SCRIPT`; значения `uses` открытых отображений по колонкам — словарь `uses` в `comments._yaml_key`,
новый элемент `- ` снимает запись своей колонки), — JavaScript; ключи шага идут в любом порядке: вход шага, чей `uses`
ещё не встречен, ждёт в `waiting` (`comments._yaml_done`), `uses` той же колонки решает js или sh, конец отображения
шага и конец файла — sh; вывод `_parse` YAML сортируется по номерам строк; скаляр под ключом из
`comments._YAML_SCRIPT_KEYS` (GitHub Actions `run`, GitLab `script`/`before_script`/`after_script`, Travis, Ansible
`shell`/`command`/`cmd`, Compose `command`/`entrypoint`, CircleCI, Buildkite, Drone, Azure
`bash`/`pwsh`/`powershell`/`script`, Read the Docs `build.jobs`) — shell; его строки без общего отступа разбирает этот
синтаксис (`comments._yaml_script`, heredoc shell — данные); под прочим ключом — данные. Дешёвая ошибка — однострочный
литерал: многострочный, принятый по ошибке, прячет комментарии до своего закрытия, поэтому многострочными считаются
только литералы в однозначном месте, а прочие кончаются со строкой.

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

Прочие поля `comments._Syntax`: `nested` — блоки вкладываются, глубину считает `comments._close_nested` (`/* */` kt,
scala, swift, rs, dart; `{- -}` hs, `#= =#` jl, `(* *)` fs, `#[ ]#` nim, `#| |#` lisp; `(*)` F# — оператор,
`comments._block_opens`); `prefix` — символьный литерал со знаком комментария или строки — данные (`$%` Erlang, `\;`
Clojure, `?;` Emacs Lisp, `#\;` Common Lisp, `?#` Ruby и Elixir — за ним не буква); `rem` — слово `REM` после указанных
знаков открывает комментарий (vb, vbs, bat, cmd, `comments._rem`); `line_block` — блок из целых строк с первой колонки,
идёт в вывод целиком (`=begin`…`=end` Ruby, POD Perl от `=слово` до `=cut`); не внутри строки с подстановками (`=begin`
в `"…"` Ruby — текст, в коде подстановки `#{…}` — комментарий, как у ruby 3.4); POD — только где лексер Perl ждёт новый
оператор (toke.c, `PL_expect == XSTATE`): флаг `stmt` `_parse` ставит по концу прошлой строки кода
(`comments._statement_end`: `;`, `{`, `}` или метка `_PERL_LABEL`), посреди оператора `\n=shift` — присваивание; `}`
анонимного хеша без разбора языка от блока не отличить — после него POD (дешёвая ошибка: лишние строки у судьи); `exdoc`
— `@doc`, `@moduledoc`, `@typedoc` Elixir со строкой — документация, в вывод; `heredoc="php"` — тело `<<<ID` до
терминатора с отступом, за которым код той же строки разбирается дальше. Правила маркера `comments._marker_ok`: `start`
— только пробелы перед ним (`::` bat), `word` — только в начале слова (sh, yaml, `Dockerfile`: и в строке `RUN` для
shell, внутри слова — значение, `ARG A=${B#x}`), `code` — не после `$`, `${`, `\` (`{# c` в py, rb, pl и др. —
комментарий), `make` — в строке рецепта (с табуляции) как `word` и сразу за префиксами `@`, `-`, `+` и пробелами вокруг
них (`comments._MAKE_PREFIX`: make снимает их и отдаёт shell `# текст`, `@# note` — комментарий), в прочих как `code`
(mk, `Makefile`, `GNUmakefile`: рецепт уходит в shell, `echo '#define X'` — значение; `CFLAGS = -O2#opt` вне рецепта —
комментарий; вне рецепта не после `$`, `{`, `\`: `$(shell echo $${#A})` — `#` в вызове функции GNU make буквален), `vim`
— `comments._vim_quote` (`"` в начале строки или после пробела без закрывающей кавычки до конца строки — комментарий,
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

Только POSIX: блокировкам состояния нужен `fcntl`, `run_judge` использует `os.killpg` и `start_new_session`. Без `fcntl`
(Windows без WSL) `common` импортируется, а `run_hook` не вызывает хук и отвечает предупреждением «хуки работают только
на POSIX» (`NoPosixTest` в `tests/test_common.py`): упавший импорт дал бы Claude Code трассировку в stderr вместо
ответа. Смерть судьи вместе с хуком — сторож `common._WATCHDOG` на всех платформах, без `prctl`; сторож проверен тестами
`WatchdogTest` и `JudgeDiesWithHookTest` только на Linux, на других платформах не запускался. Python 3.11+ (`tomllib` в
`manifests`). Зависимостей вне стандартной библиотеки Python нет.
