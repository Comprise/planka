# Архитектура

planka — плагин Claude Code уровня пользователя: три хука из `hooks/hooks.json` подмешивают
правила из `philosophy.md` и `rules/*.md` в контекст агента и отклоняют его действия через
вложенного судью-модель или детерминированные проверки. Пользовательское описание поведения —
`README.md`, разделы «Как это работает» и «Известные ограничения».

## Компоненты

| Файл | Роль |
| --- | --- |
| `hooks/hooks.json` | регистрация хуков: `UserPromptSubmit` → `remind.py`, `PreToolUse` на `AskUserQuestion\|ExitPlanMode\|Bash` → `judge_tool.py`, `Stop` → `judge_stop.py` |
| `planka/remind.py` | `philosophy.md` целиком как `additionalContext`; снимок дерева; строка об отсутствии `CLAUDE.md`; предупреждение о незаданных языках |
| `planka/judge_tool.py` | судья вопроса (`judge_question`), плана (`judge_plan`), отказ на добавление пакета (`judge_bash`) |
| `planka/judge_stop.py` | фильтры «варианты», «готово», «документация» по последнему сообщению и изменениям со снимка; один вызов судьи |
| `planka/common.py` | барьер, чтение входа, рубрика, `run_judge`, лимит отказов, журнал, классы путей, формат ответа (`run_hook`) |
| `planka/prompts.py` | системный промпт, схема ответа `JUDGE_SCHEMA`, вопросы судье по видам проверки, сборка содержимого |
| `planka/planparse.py` | разбор плана на волны и задачи, владение файлами (`shared_files`) |
| `planka/depcheck.py` | разбор команды Bash: добавляет ли она пакет (`dependency_add`), маркер `DEP_OK_MARKER` |
| `planka/snapshot.py` | снимок дерева `{путь: [size, mtime_ns]}`, `.gitignore` упрощённо, порог `MAX_FILES` |
| `planka/comments.py` | строки комментариев изменённых файлов для судьи документации (`extract`) |
| `philosophy.md` | ядро правил; индекс «Модули» в конце |
| `rules/*.md` | модули правил, по файлу на область |
| `.claude-plugin/plugin.json` | манифест и `userConfig`: `judge_model`, `comment_lang`, `doc_lang` |
| `.claude-plugin/marketplace.json` | маркетплейс `planka`, источник плагина — корень репозитория |

## Контракт кода с текстами правил

Код ищет тексты правил по именам; переименование ломает хук без ошибки теста — хук пропускает
проверку с предупреждением.

- Разделы ядра берутся по заголовку `## <имя>` (`common.philosophy_sections`): `Решения` —
  рубрика вопроса, плана и фильтра «варианты»; `Планы` — рубрика плана.
- Модули берутся по имени файла (`common.rule_texts`): `planning`, `subagents` — план;
  `verification` — фильтр «готово»; `docs`, `comments` — фильтр «документация».
  `rules/dependencies.md` называет причина отказа `judge_tool.DEP_REASON`.
- Метки `{RULES}`, `{COMMENT_LANG}`, `{DOC_LANG}` заменяет `common.substitute` при каждом чтении:
  путь к `rules/` плагина и значения настроек; незаданный язык — `UNSET_LANG`.

## Ответ хука

Все хуки запускаются через `common.run_hook`: stdout — один JSON (ответ из `common.emit` плюс
`systemMessage` из `common.warn`) или пусто; stderr не пишется; исключение превращается в
предупреждение «внутренняя ошибка». Причина отказа начинается с `planka: `. Отказ `PreToolUse` —
`common.deny_output`, отказ `Stop` — `common.block_output`.

## Судья

`common.run_judge` запускает `claude` с `JUDGE_FLAGS` (`-p`, `--setting-sources ""`,
`--tools ""` и др.), схемой `JUDGE_SCHEMA` и моделью из `CLAUDE_PLUGIN_OPTION_JUDGE_MODEL`
(по умолчанию `sonnet`), в отдельной группе процессов, с `cwd` в каталоге данных и окружением
`PLANKA_JUDGE=1`. Каждый хук первым делом проверяет `common.barrier_active` и внутри судьи не
работает. Любая ошибка судьи — `Verdict` с `error`, хук пропускает проверку с предупреждением.
Таймаут — `JUDGE_TIMEOUT` (60 с) при таймауте хука 90 с в `hooks/hooks.json`.

Лимит отказов: `MAX_DENIES` = 2 на ключ `<prompt_id>:<hook>` (`common.deny_budget_exhausted`);
отказ `judge_bash` лимитом не ограничен.

## Состояние и журнал

Каталог данных — `$CLAUDE_PLUGIN_DATA`, без него `.data/` в корне плагина (`common.data_dir`).

- `state/<session>.json` — счётчики отказов; `state/<session>.warned.json` — выданные
  однократные предупреждения; `state/<session>.snap.json` — снимок дерева текущей реплики.
  Имя — `common._safe_name`. Запись атомарная (`common._atomic_write_json`, `snapshot.save`).
  Файлы старше `STATE_TTL` (7 дней) удаляет `common.prune_state`.
- `judge.log` — строка JSON на решение хука, только метаданные: содержимое — длиной и SHA-256
  (`common.log_event`). От `LOG_MAX_BYTES` (1 МиБ) переименовывается в `judge.log.1`; ротация
  и запись под `fcntl.flock` на `judge.log.lock`.

Снимок связывает `UserPromptSubmit` и `Stop`: `judge_stop.changed_this_turn` сравнивает его с
текущим деревом, только если совпали `prompt_id` и корень проекта (`common.project_root`).

## Платформы

Только POSIX: `common` импортирует `fcntl`, `run_judge` использует `os.killpg` и
`start_new_session`. Зависимостей вне стандартной библиотеки Python нет.
