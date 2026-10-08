# Возврат файлов патчем и ref с поддельной или прошлой датой в проверке манифестов

**Что не так.** `manifest_watch.restored_names` считает не новыми имена из манифестов ref, откуда команда git
возвращает файлы (`manifest_watch.command_refs`: `git stash pop|apply`, `git merge --squash`,
`git checkout <ref> -- <пути>`, `git restore --source`, `git cherry-pick -n`), если ref создан до начала сессии
(`manifest_watch.session_start`). «Создан до» — время коммиттера коммита ref (`manifest_watch._old_trees`) меньше
начала сессии. Остаток:

- `git apply` патча и `npm create` с новыми пакетами ref не называют: имя, которое они вписали в манифест, —
  блок `judge_tool.check_command_manifests` (`COMMAND_REASON`), хотя пакет мог добавить автор;
- коммит или stash с поддельной датой коммиттера (`GIT_COMMITTER_DATE`) проходит как старый: агент может сам
  создать ref «до сессии» и вернуть из него имя без блока;
- ref, созданный агентом в прошлой сессии, — тоже до начала текущей: его имена проходят как работа автора;
- `state/<session>.start.json` пишется один раз (`manifest_watch.mark_start`) и не обновляется, а
  `common.prune_state` удаляет файлы состояния старше `STATE_TTL` (7 дней) по mtime: в сессии дольше недели
  начало пишется заново, и ref первой недели сессии проходят как старые.

**Чем доказано.** Чтением `manifest_watch.command_refs`, `_old_trees`, `restored_names` (docstring называет
поддельную дату), `mark_start` и `common.prune_state`. Исполнением — `tests/test_judge_tool.py`,
`ManifestBashGitTest`: `test_stash_after_session_start_blocked_with_safe_reason` (stash моложе начала сессии —
блок), `test_author_stash_before_session_passes`, `test_restore_from_old_ref_passes`; `CommandRefsTest.test_refs` —
`git apply` ref не даёт.

**Верное решение и цена.** Различать по журналу ref (reflog): ref, чья запись reflog появилась в сессии, — новый,
независимо от даты коммиттера; ref прошлой сессии агента так не отличить без журнала ref по сессиям. Цена —
разбор reflog (`git reflog`/`.git/logs`) на каждую команду с ref в пределах `manifest_watch.SNAPSHOT_BUDGET`, а
у stash reflog — единственная история и при `stash drop` теряется. `git apply` патча детерминированно не
различить: патч не несёт ни ref, ни даты.

**Почему отложено.** Обход требует от агента намеренной подделки даты или ref прошлой сессии; ложный блок
`git apply` стоит одного вопроса автору: текст блока велит не откатывать вслепую. Вес низкий.
