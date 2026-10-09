# `debug_watch._paren_pairs` не вызывается кодом плагина

**Что не так.** `debug_watch._paren_pairs` (`plugin/planka/debug_watch.py`) — пары скобок команды над деревом
`shparse` — код хука не вызывает: `_segments` и `code1_is_answer` обходятся без него. Функцию зовут только тесты:
`ParenPairsTest` и свёртка записанных результатов корпуса в `CorpusResultsTest` (`tests/test_debug_watch.py`,
`tests/fixtures/debug-watch-results.json`).

**Чем доказано.** `grep -rn "_paren_pairs" plugin/ tests/*.py`: вне определения — только `tests/test_debug_watch.py`
(строки с `debug_watch._paren_pairs(…)` в `ParenPairsTest` и в сборке записи корпуса).

**Верное решение и цена.** Удалить функцию из плагина, её тест `ParenPairsTest` и поле пар скобок из записи
`debug-watch-results.json` (перезапись `PLANKA_RECORD_RESULTS=1` со сверкой, что остальные поля не изменились).
Цена — правка трёх файлов и проверка записи корпуса.

**Почему отложено.** Найдено при сведении после коммита: поведение хука не задето, мёртвый код — низкий вес; удаление
трогает записанные результаты корпуса, и его проверяет ревью.
