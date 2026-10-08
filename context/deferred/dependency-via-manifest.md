# Зависимость, добавленная правкой манифеста, проходит без проверки

**Что не так.** Детектор зависимостей (`depcheck.dependency_add`, вызывается из
`judge_tool.judge_bash`) смотрит только команды `Bash`, а установка по манифесту
(`npm install`, `uv sync`, `pip install -r`, `cargo build`, `go mod tidy`) по замыслу не добавление.
Поэтому пакет, вписанный в манифест правкой файла (`Edit`/`Write` `package.json`, `pyproject.toml`,
`requirements*.txt`, `Cargo.toml`, `go.mod`, `Gemfile`) или командой, которая правит манифест без
установки (`echo x >> requirements.txt`, `npm pkg set dependencies.x=…`, `sed -i … package.json`), и
затем установленный по манифесту, проходит без отказа. Агенту, получившему отказ на `npm install x`,
этот путь очевиден.

**Чем доказано.** Вызовом: `dependency_add('echo requests >> requirements.txt && pip install -r
requirements.txt')`, `dependency_add('npm pkg set dependencies.lodash=^4 && npm install')` → `None`;
matcher `judge_tool` в `hooks/hooks.json` — `AskUserQuestion|ExitPlanMode|Bash`, файловые инструменты
судит только `guard_memory` (память). Известное ограничение записано в `README.md`, «Известные
ограничения».

**Верное решение и цена.** Детерминированная проверка правки манифеста: хук `PreToolUse` на файловые
инструменты сравнивает разделы зависимостей манифеста до и после правки (`dependencies`,
`devDependencies`, `[project.dependencies]`, `[tool.poetry.dependencies]`, `[dependencies]` Cargo,
`require` go.mod, строки `requirements*.txt`) и отклоняет новое имя пакета с той же причиной
`DEP_REASON`. Цена — разбор TOML без `tomllib` на Python 3.10 (в stdlib только с 3.11) или
ограничение версией, новый хук или расширение matcher `guard_memory`, корпус манифестов в
`tests/fixtures/`. Команды, правящие манифест (`npm pkg set`, `echo >>`, `sed -i`), — отдельный край
детектора команд.

**Почему отложено.** Новая проверка — функция сверх аудита и выбор автора: какой хук, какие
манифесты и что делать с Python 3.10.
