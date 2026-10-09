# `depcheck.heredocs` не вызывается кодом плагина

**Что не так.** `depcheck.heredocs` (`plugin/planka/depcheck.py`) только передаёт строку в `shparse.heredocs`; модули
плагина зовут `shparse.heredocs` напрямую (`comments.py`), `judge_tool.py` и `manifest_watch.py` импортируют `depcheck`
ради других функций. Обёртку зовут только тесты `tests/test_depcheck.py`.

**Чем доказано.** `grep -rn "heredocs(" plugin/planka/`: вызовы — `shparse.heredocs` в `comments.py`, `depcheck.py`
(внутри самой обёртки) и `shparse.py`; `depcheck.heredocs(` — только в `tests/test_depcheck.py`.

**Верное решение и цена.** Удалить обёртку и перевести её проверки в `tests/test_depcheck.py` на `shparse.heredocs`
(тот же вход и то же ожидание; место им — `tests/test_shparse.py`, если там нет таких же). Цена — правка двух файлов
тестов и одной функции.

**Почему отложено.** Найдено при сведении после коммита: поведение не задето, мёртвый код — низкий вес; перенос
проверок между файлами тестов проверяет ревью.
