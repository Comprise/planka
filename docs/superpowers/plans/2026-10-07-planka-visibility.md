# План: видимые предупреждения, классы файлов, один путь установки, журнал без содержимого

Спека: `docs/superpowers/specs/2026-10-07-planka-visibility-design.md`. Ветка `feat/visibility`.

Граница: только перечисленное в спеке. Python stdlib. Комментарии по-русски, факт, без истории.
Красный тест первым на каждое поведение. Коммит только своих файлов, без трейлеров; не `git add -A`,
не stash/reset/rebase/amend. Проблема — остановиться и предложить варианты, самый правильный первым.

Отвергнуто: код выхода 1 для предупреждений; два пути установки; ротация без смены состава записей.
Долг: показ `systemMessage` вживую не проверен (спека §6).

## Волна 1 — контракт вывода, журнала и классов

### Задача 1: `common.py` — вывод через systemMessage, журнал, path_kind

Файлы: `planka/common.py`, `tests/helpers.py`, `tests/test_common.py`, `tests/test_remind.py`,
`tests/test_judge_stop.py`, `tests/test_judge_tool.py`

- `warn(msg)` добавляет `f"planka: {msg}"` в список модуля; stderr не пишется.
- `deny_output`, `block_output`, `context_output` возвращают словари. `emit(obj)` запоминает ответ.
- `run_hook(main)`: `main()`; исключение → `warn(f"внутренняя ошибка: {e!r}")`; затем один JSON в stdout:
  ответ (если был) плюс `"systemMessage": "\n".join(строки)` (если были); иначе stdout пуст; выход 0.
  Список и ответ сбрасываются в начале `run_hook`.
- `log_event(hook, session_id, *, content=None, **fields)`: при `content` — поля `content_len` и
  `content_sha256` (hex), текста нет. `LOG_MAX_BYTES = 1_048_576`: при размере ≥ порога перед записью
  `os.replace(judge.log, judge.log.1)`.
- `CODE_EXTS` (множество расширений исходного кода, включая все из `comments._C_FAMILY`, `_HASH`,
  `_DASH`, `_HTML` и `php`, `r`, `jl`, `ex`, `exs`, `erl`, `clj`, `fs`, `vb`, `nim`, `zig`, `sol`,
  `proto`, `gradle`, `groovy`, `tf`, `nix`, `el`, `vim`, `bat`, `cmd`), `CODE_NAMES`
  (`Makefile`, `Dockerfile`, `Justfile`, `Rakefile`, `Gemfile`), `path_kind(relpath)` →
  `"doc"|"code"|"other"`. `is_doc_path` остаётся до волны 2.
- `tests/helpers.py`: `messages(r)` — строки `systemMessage` из stdout процесса (пусто, если нет);
  `output(r)` — JSON stdout или `None`.
- Все проверки `stderr` в четырёх файлах тестов переводятся на `messages(r)`; проверки `stdout`
  JSON — на `output(r)`; проверки `log["content"]` — на `content_sha256`/`content_len`.

Проверка: `make test` зелёный. Сообщить: сигнатуры, имена хелперов тестов, число тестов.

## Волна 2 — потребители контракта (параллельно)

### Задача 2: фильтр «документация» на трёх классах

Файлы: `planka/judge_stop.py`, `planka/prompts.py`, `planka/comments.py`, `tests/test_judge_stop.py`,
`tests/test_prompts.py`, `tests/test_comments.py`

- `changed_this_turn` → `(root, [(path, kind, exists)])` по `common.path_kind`; фильтр срабатывает,
  когда есть `kind == "code"`.
- `render_docs_content(message, changed, comments, truncated, no_claude_md, unknown=())`: строки
  «путь — код|документация|прочее», у удалённого «, удалён»; не больше `MAX_LISTED = 100`, дальше
  «… и ещё N».
- `comments.extract` получает только существующие файлы кода; `unknown` — существующие файлы кода
  без семейства комментариев. `Dockerfile`, `Justfile`, `Rakefile`, `Gemfile` — семейство `#`;
  `php`, `groovy`, `gradle`, `proto`, `sol`, `zig` — семейство `//`; `tf`, `nix`, `r`, `jl`, `ex`,
  `exs` — `#`; `erl` — `%` не поддерживается (попадает в `unknown`).
- Тест: каждое расширение с синтаксисом комментариев входит в `common.CODE_EXTS`.
- `log_event` для Stop при фильтре «документация» получает `files` — пути изменённых файлов, не больше
  `MAX_LISTED`.

### Задача 3: предупреждение о языке называет точную команду

Файлы: `planka/remind.py`, `tests/test_remind.py`

- Текст: `задайте comment_lang и doc_lang: claude plugin configure planka@planka --values-stdin`.

## Волна 3 — документация

### Задача 4: README и Makefile

Файлы: `README.md`, `Makefile`

- Удалить цели `install`/`uninstall`. README «Установка»: локальный и удалённый маркетплейс, id
  `planka@planka`, настройка, переход со старой установки (`rm ~/.claude/skills/planka`).
- «Настройки», «Как это работает», «Известные ограничения»: `systemMessage`, три класса, журнал
  метаданных с ротацией. Каждое утверждение сверить с кодом.

## Координатор

После волны 2: удалить `common.is_doc_path`, если вызывающих нет. После каждой волны: `make test`,
`make validate`. В конце: точечное ревью, слияние в `master`.
