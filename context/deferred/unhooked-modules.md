# Модули без хука

**Что не так.** Модули `rules/debugging.md`, `rules/memory.md`, `rules/refactoring.md`,
`rules/design-patterns.md` не входят ни в одну рубрику судьи: их нет среди имён, которые
передают `common.rubric` в `judge_tool.judge_plan` и `judge_stop.main`. Действуют, только если
агент сам открыл модуль по индексу «Модули» в `philosophy.md`.

**Чем доказано.** Чтением: `common.rule_texts` вызывается только с `planning`, `subagents`,
`verification`, `docs`, `comments`.

**Верное решение.** Не записано; ни один хук не видит событие-условие этих модулей («второй
провал подряд», запись в память, рефакторинг, выбор паттерна).

**Почему отложено.** Долг принят автором осознанно (спека
`docs/superpowers/specs/2026-10-07-planka-modules-design.md`, «4. Привязки модулей к судьям»).
