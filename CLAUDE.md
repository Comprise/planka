# planka

Плагин Claude Code: хуки подмешивают правила `plugin/philosophy.md` и `plugin/rules/*.md` в контекст агента и
отклоняют его действия через судью-модель или детерминированные проверки. Пользовательское
описание — `README.md`.

## Правила проекта

- Только стандартная библиотека Python; новый пакет — вопрос автору.
- Хук отвечает одним JSON на stdout через `common.run_hook`; stderr не пишет; сообщения
  пользователю — `common.warn`.
- Каждый хук в начале проверяет `common.barrier_active`: внутри вложенного судьи хуки не работают.
- Любая ошибка судьи или хука — пропуск проверки с предупреждением, не блокировка.
- Комментарии в коде и документация для агента — на русском. Исключение — тексты, которые пишут установщики
  инструментов (`.claude/skills/*` и раздел `## MCP Tools: code-review-graph` ниже): их не переводят,
  перевод затёрла бы следующая установка.
- Правка поведения хука сверяется с `README.md`.

## Критические инварианты

- Пользователям уходит только `plugin/`: файлы разработки — тесты, документация, настройки
  инструментов — лежат вне него.
- Имена разделов `## Решения`, `## Планы`, `## Границы` в `plugin/philosophy.md` и имена файлов
  `plugin/rules/*.md` — контракт с кодом; его проверяет `tests/test_contract.py` по настоящим
  текстам. См. `context/architecture.md`, «Контракт кода с текстами правил».
- Метки `{RULES}`, `{COMMENT_LANG}`, `{DOC_LANG}` в правилах подставляет `common.substitute`.
- `judge.log` хранит только метаданные: содержимое — длиной и SHA-256.

## Структура

| Путь | Что |
| --- | --- |
| `plugin/` | плагин целиком — только он уходит пользователям (`"source": "./plugin"`) |
| `plugin/planka/` | код хуков |
| `plugin/rules/` | модули правил |
| `plugin/philosophy.md` | ядро правил |
| `plugin/hooks/hooks.json` | регистрация хуков |
| `plugin/.claude-plugin/plugin.json` | манифест плагина |
| `.claude-plugin/marketplace.json` | манифест маркетплейса |
| `tests/` | unittest и заглушка `claude` |
| `context/` | документация для агента |

## Документы `context/`

| Документ | О чём |
| --- | --- |
| `context/architecture.md` | компоненты, контракт с правилами, ответ хука, судья, сроки, лимит отказов, разбор плана, память, неудачи команд, состояние, снимок, комментарии, платформы |
| `context/development.md` | запуск, точки входа, правила кода, новый хук, документация, git |
| `context/testing.md` | цели `make`, устройство тестов, изоляция окружения, заглушка судьи |
| `context/deferred/INDEX.md` | отложенное |

Минимальные наборы:

- Правка хука или `plugin/planka/*.py` — `context/architecture.md`, `context/testing.md`.
- Правка тестов — `context/testing.md`, `tests/CLAUDE.md`.
- Правка `plugin/philosophy.md` или `plugin/rules/` — `context/architecture.md`, «Контракт кода с текстами правил».
- Новая настройка, хук или пакет — `context/development.md`.

## `plugin/planka/`

Код хуков: точки входа `remind.py`, `judge_tool.py` (`PreToolUse`, а на `Bash` ещё `PostToolUse` и
`PostToolUseFailure` — проверка манифестов), `guard_memory.py`, `judge_stop.py`, `debug_watch.py`; остальное —
их модули, среди них `manifests.py` (разбор манифестов) и `manifest_watch.py` (правка манифеста, снимок и
сравнение после команды, имена ref до начала сессии, откуда команда git возвращает файлы).

Инварианты:

- Модули импортируют друг друга по имени (`import common`), без пакета.
- Точка входа: `common.run_hook(main)`; `main` начинается с `common.barrier_active()`.
- Вывод — только через `common.emit` и `common.warn`; `print` и stderr не используются.
- Отказ запоминается через `common.emit` до записи журнала: сбой записи не отменяет отказ.
- Запись файлов состояния — атомарная, через `common.atomic_write_json` (временный файл и
  `os.replace`); чтение и запись счётчиков, предупреждений, неудач команд и снимков манифестов — под
  `common.state_lock`.
- Хук с судьёй проверяет лимит отказов до вызова судьи (`common.deny_budget_left`); ошибка судьи
  — пропуск с `common.skip_message`, в журнал — `Verdict.error` без текста модели.
- JSON наружу — через `common.dumps`: имя файла не в UTF-8 не роняет запись.

Читать перед правкой: `context/architecture.md`, `context/testing.md`,
`context/development.md` («Код»).

## `plugin/rules/`

Модули правил: по файлу на область, индекс — раздел «Модули» в `plugin/philosophy.md`.

Инварианты:

- Модуль начинается с заголовка и строки условия «Читай …».
- Имя файла — контракт с кодом: переименование — правка вызовов `common.rubric` и
  `common.rule_texts` в `judge_tool.py`, `judge_stop.py`, `guard_memory.py`, `debug_watch.py`
  (`MODULE`), ссылок `judge_tool.DEP_REASON`, `MANIFEST_REASON`, `COMMAND_REASON`, `MODULES` в
  `tests/test_contract.py`, индекса в `plugin/philosophy.md` и `README.md`. Имена из вызовов тест выводит
  из кода сам (`test_names_taken_by_code_exist`); `dependencies` код называет только текстом причин отказа.
- Метки `{RULES}`, `{COMMENT_LANG}`, `{DOC_LANG}` подставляются при чтении; других меток нет.

Читать перед правкой: `context/architecture.md`, «Контракт кода с текстами правил».

## Ведение документации

По `plugin/rules/docs.md`: каждая правка кода сверяет `context/`, этот файл (для `plugin/` — его
разделы выше) и локальный `CLAUDE.md` правленного каталога, `README.md` и комментарии; остаток —
запись в `context/deferred/`.

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**This project has a knowledge graph. Start with the code-review-graph
MCP tools to narrow scope, then read the source.** The graph is cheaper than scanning files and
gives you structural context (callers, dependents, test coverage) that file search cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes_tool` or `query_graph_tool` instead of Grep
- **Understanding impact**: `get_impact_radius_tool` instead of manually tracing imports
- **Code review**: `detect_changes_tool` + `get_review_context_tool` instead of reading entire files
- **Finding relationships**: `query_graph_tool` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview_tool` + `list_communities_tool`

### Verify in the source

- Narrow scope with the graph, then read the source. Do not change code from graph output alone.
- For any non-trivial change, read the implementation and the relevant tests before concluding.
- Verify the exact source when touching behavior, database logic, migrations, retries, fallbacks,
  recovery, or compatibility code.
- When the graph and the source disagree, the source wins. The graph may be stale or may not
  model that relationship.
- An empty graph result can mean "not indexed" or "not statically visible", not "does not exist".

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes_tool` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context_tool` | Need source snippets for review — token-efficient |
| `get_impact_radius_tool` | Understanding blast radius of a change |
| `get_affected_flows_tool` | Finding which execution paths are impacted |
| `query_graph_tool` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes_tool` | Finding functions/classes by name or keyword |
| `get_architecture_overview_tool` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes_tool` for code review.
3. Use `get_affected_flows_tool` to understand impact.
4. Use `query_graph_tool` pattern="tests_for" to check coverage.
<!-- /code-review-graph MCP tools -->
