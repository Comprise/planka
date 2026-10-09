# Разработка

## Запуск

Плагин ставится из маркетплейса рабочей копии (`README.md`, «Установка»):

```bash
claude plugin marketplace add ~/Projects/planka
claude plugin install planka@planka
```

Установленный плагин — копия в `~/.claude/plugins/cache/planka/planka/<version>/`; правки рабочей
копии попадают в неё через `claude plugin marketplace update planka` и `claude plugin update
planka@planka` с перезапуском сессии (`README.md`, «Установка»). Хуки запускаются как
`python3 "${CLAUDE_PLUGIN_ROOT}/planka/<скрипт>.py"`; модули `planka/` импортируют друг друга
по имени (`import common`), каталог скрипта — первый в `sys.path`.

Точки входа: `remind.py` (`UserPromptSubmit`; хук на каждую часть ядра, `python3 …/remind.py <номер>`), `judge_tool.py`
(`PreToolUse`, в том числе MCP-инструменты `mcp__*`; на `Bash` и `mcp__*` ещё `PostToolUse`, `PostToolUseFailure`),
`guard_memory.py` (`PreToolUse`), `judge_stop.py` (`Stop`), `debug_watch.py` (`PostToolUse`, `PostToolUseFailure`),
`model_watch.py` (`SessionStart`, `PostModelSwitch`). Один скрипт на нескольких событиях различает их по
`hook_event_name` входа (`judge_tool.POST_EVENTS`).

## Код

- Только стандартная библиотека Python 3.11+ (`tomllib` в `manifests.py`); новый пакет — вопрос автору.
- Скрипты разработки — в `tests/tools/` (`context/testing.md`); фаззеру `make bashdiff` нужны bash 5.3 и `bwrap`
  (bubblewrap): без песочницы он формы не исполняет.
- Новая точка входа хука — регистрация в `plugin/hooks/hooks.json` с таймаутом, тело `main()` через
  `common.run_hook(main)`, первой строкой `main` — `common.barrier_active()`; тесты — подпроцессом
  через `tests.helpers.Env` (`context/testing.md`); строка в таблицах хуков `README.md`, «Как это
  работает», и `context/architecture.md`, «Компоненты».
- Хук, который берёт раздел ядра или модуль правил по имени, добавляет это имя в
  `tests/test_contract.py` (`SECTIONS`, `MODULES`) и в `context/architecture.md`, «Контракт кода с
  текстами правил». Имя из вызова `common.rubric`, `common.philosophy_sections`, `common.rule_texts`
  или константы `MODULE` тест находит в коде и сам (`test_names_taken_by_code_exist`); ручной список —
  обратная проверка, что код имя ещё берёт. Имя, переданное иначе (переменной из другого места,
  текстом причины, как `dependencies` в причинах отказа `judge_tool`: `DEP_REASON`, `DEP_DOUBT_REASON`,
  `MANIFEST_REASON`, `COMMAND_REASON`, `MCP_REASON`), тест в коде не видит.
- Новый срок внутри хука (бюджет, таймаут вызова) — в сумму `TimeoutsTest` в `tests/test_contract.py`
  и в `context/architecture.md`, «Сроки»: сумма сроков хука меньше его `timeout` в `hooks/hooks.json`.
- Хук с судьёй: лимит отказов до вызова судьи (`common.deny_budget_left`) и при отказе
  (`common.deny_budget_exhausted`); ошибка судьи — `common.skip_message` пользователю,
  `Verdict.error` в журнал.
- Сообщение пользователю — только `common.warn` / `common.warn_once`; `print` и stderr в хуках
  не используются.
- Файл состояния — в `state/` через `common.atomic_write_json` и `common.read_json` под
  `common.state_lock`, имя от `common.safe_name(session_id)`; `common.prune_state` удаляет старые.
- Новая настройка — `userConfig` в `plugin/.claude-plugin/plugin.json`; хук читает её из
  `CLAUDE_PLUGIN_OPTION_<ИМЯ>`; тесты её вычищают в `Env.environ`.
- Комментарии в коде — на русском, по `plugin/rules/comments.md`.

## Документация

- Пользовательская документация — `README.md`: установка, настройки, хуки, рубрики, списки слов и
  менеджеров пакетов, журнал, известные ограничения, проверка. Правка поведения хука сверяется с
  ним.
- Документация для агента — `CLAUDE.md`, `context/`, `context/deferred/` — на русском.
- Спеки и планы — временное состояние работы, в репозиторий не попадают; решение автора, на
  которое опирается запись `context/deferred/`, переносится в неё цитатой.

## Git

Сообщение коммита — `тип(область): описание` на русском; типы `feat`, `fix`, `refactor`, `docs`,
`chore`, `merge`. Область — имя модуля (`fix(comments): …`, `docs(readme): …`); правка нескольких
модулей пишется без области (`fix: …`).
