# Файлы разработки: игнор `.superpowers/`, хук графа без `MultiEdit`, версия `code-review-graph`

**Что не так.**

- `.gitignore` не содержит `.superpowers/`: игнор держится только на `.superpowers/sdd/.gitignore` (`*`),
  поэтому файл или каталог superpowers вне `sdd/` появляется в `git status` неотслеживаемым и может уйти в
  коммит.
- `.claude/settings.json`: хук обновления графа зарегистрирован на `"matcher": "Edit|Write"` — правка через
  `MultiEdit` граф не обновляет, хотя раздел `## MCP Tools: code-review-graph` в `CLAUDE.md` обещает
  обновление при изменении файлов.
- Тот же хук зовёт `code-review-graph` из `PATH` без версии, а `.mcp.json` закрепляет MCP-сервер на
  `uvx code-review-graph@2.3.9`: после обновления одного из них `graph.db` пишут разные версии.

**Чем доказано.** Исполнением 2026-10-08: `git check-ignore -v .superpowers/foo.md .superpowers/sdd/x` —
первый путь не игнорируется, второй — `.superpowers/sdd/.gitignore:1:*`; matcher прочитан в
`.claude/settings.json`; `code-review-graph --version` — `2.3.9` из `~/.local/bin`, версия в `.mcp.json` —
`2.3.9`, сейчас совпадают.

**Верное решение и цена.** Строка `.superpowers/` в `.gitignore`; matcher `Edit|Write|MultiEdit`; в хуке
`uvx code-review-graph@2.3.9`, та же версия, что в `.mcp.json`. Цена — три строки.

**Почему отложено.** `.gitignore`, `.claude/` и `.mcp.json` правит только автор (`plugin/philosophy.md`,
«Границы»; ограничение ночного аудита 2026-10-08).
