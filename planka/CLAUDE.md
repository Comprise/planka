# planka/

Код хуков: точки входа `remind.py`, `judge_tool.py`, `judge_stop.py`, остальное — их модули.

## Инварианты

- Модули импортируют друг друга по имени (`import common`), без пакета.
- Точка входа: `common.run_hook(main)`; `main` начинается с `common.barrier_active()`.
- Вывод — только через `common.emit` и `common.warn`; `print` и stderr не используются.
- Запись файлов состояния — атомарная, через временный файл и `os.replace`.

## Читать перед правкой

- `../context/architecture.md`
- `../context/testing.md`
- `../context/development.md`, «Код»
