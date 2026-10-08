# Порог снимка вне git

**Что не так.** Вне git-репозитория снимок дерева — обход каталогов со stat каждого файла
(`snapshot.capture`, режим `walk`). Когда имён файлов в обходе больше `snapshot.MAX_FILES` (50 000) —
счёт идёт уже в `snapshot._walk_paths` и включает символические ссылки и не обычные файлы, —
обход обрывается с `snapshot.TooManyFiles`: снимок не пишется, `remind.take_snapshot` один раз за
сессию предупреждает, `snapshot.changed_since` отвечает `None`, и фильтр «документация» в `judge_stop`
в таком проекте не срабатывает. В git-режиме порог ограничивает число изменённых и неотслеживаемых
путей и обход вложенного репозитория, где git не работает.

**Чем доказано.** Чтением `snapshot._walk_paths`, `snapshot.capture` и `remind.take_snapshot`; тесты
`ChangedSinceWalkTest.test_walk_too_many` и `test_walk_truncated_is_undetermined`,
`WalkCaptureTest.test_truncated_walk_is_too_many_even_if_few_regular_files`,
`RemindTest.test_too_many_files_warns`.

**Верное решение и цена.** Узнать изменения без stat всех файлов можно только по индексу или
журналу событий ФС: git-индекс есть только в git, а inotify/fsevents требуют демона вне stdlib.
Поднять порог — компактная запись снимка; цена — формат снимка и время обхода в бюджете 7 с.

**Почему отложено.** Решения на stdlib без демона нет; в git-проектах долг закрыт.
