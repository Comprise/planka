# Разработка

## Запуск

Плагин ставится из маркетплейса рабочей копии (`README.md`, «Установка»):

```bash
claude plugin marketplace add ~/Projects/planka
claude plugin install planka@planka
```

Правки подхватываются в следующей сессии или по `/reload-plugins`. Хуки запускаются как
`python3 "${CLAUDE_PLUGIN_ROOT}/planka/<скрипт>.py"`; модули `planka/` импортируют друг друга
по имени (`import common`), каталог скрипта — первый в `sys.path`.

## Код

- Только стандартная библиотека Python; новый пакет — вопрос автору.
- Новая точка входа хука — регистрация в `plugin/hooks/hooks.json`, тело `main()` через
  `common.run_hook(main)`, первой строкой `main` — `common.barrier_active()`.
- Сообщение пользователю — только `common.warn` / `common.warn_once`; `print` и stderr в хуках
  не используются.
- Новая настройка — `userConfig` в `plugin/.claude-plugin/plugin.json`; хук читает её из
  `CLAUDE_PLUGIN_OPTION_<ИМЯ>`.
- Комментарии в коде — на русском, по `plugin/rules/comments.md`.

## Документация

- Пользовательская документация — `README.md`: настройки, хуки, рубрики, списки слов и
  менеджеров пакетов, журнал, известные ограничения. Правка поведения хука сверяется с ним.
- Документация для агента — `CLAUDE.md`, `context/`, `context/deferred/` — на русском.
- Спеки и планы — временное состояние работы, в репозиторий не попадают; решение автора, на
  которое опирается запись `context/deferred/`, переносится в неё цитатой.

## Git

Сообщение коммита — `тип(область): описание` на русском; типы `feat`, `fix`, `docs`, `merge`,
область — имя модуля (`fix(comments): …`, `docs(readme): …`).
