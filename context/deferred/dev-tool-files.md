# Файлы инструментов разработки расходятся с правилами проекта

**Что не так.** Файлы настроек инструментов в корне репозитория, которые агент сам не правит
(ядро, «Границы»; поручение аудита запрещает править `.claude/`, `.mcp.json`, `.serena/`, `.gitignore`):

- `.claude/settings.json.bak` — отслеживаемая копия, байт в байт равная `.claude/settings.json`
  (коммит `chore: резервная копия .claude/settings.json`). Резервную копию даёт история git; два
  файла настроек разойдутся при первой правке.
- `.gitignore` не прячет `.claude/settings.local.json`: сейчас его прячет только глобальный игнор
  разработчика (`git check-ignore -v` → `~/.config/git/ignore:1:**/.claude/settings.local.json`). В
  другом клоне личные настройки видны как неотслеживаемые и уходят в коммит по `git add -A`.
- `.claude/settings.json`: хук обновления графа code-review-graph висит на `"matcher": "Edit|Write"` —
  правки через `MultiEdit`, `NotebookEdit` и Bash граф не обновляют, хотя `CLAUDE.md` («MCP Tools:
  code-review-graph») обещает автообновление. Ветка `|| echo 'Not a git repo, skipping'` в
  `SessionStart` печатает «не git» на любой сбой `status`.
- `.claude/skills/*/SKILL.md` и раздел `## MCP Tools: code-review-graph` в `CLAUDE.md` — на
  английском, при правиле `CLAUDE.md` «документация для агента — на русском». Тексты созданы
  установщиком code-review-graph.

**Чем доказано.** `diff .claude/settings.json .claude/settings.json.bak` — пусто; `git check-ignore -v
.claude/settings.local.json` — правило глобального игнора; чтение `.claude/settings.json` и
`.claude/skills/*/SKILL.md`.

**Верное решение и цена.** Удалить `.claude/settings.json.bak` из репозитория; дописать
`.claude/settings.local.json` в `.gitignore`; расширить matcher до `Edit|Write|MultiEdit|NotebookEdit`
и поправить текст ветки `||`; для сгенерированных английских текстов — либо перевести, либо записать в
`CLAUDE.md` исключение «тексты, созданные установщиками инструментов, не переводятся» (перевод
затирается следующей установкой, поэтому рекомендуется исключение). Цена — минуты правки.

**Почему отложено.** Файлы настроек агента и `.gitignore` правит только автор.
