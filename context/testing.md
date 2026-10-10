# Проверки

```bash
make test           # python3 -m unittest discover -s tests -t . -v
make test-hostile   # тот же прогон в локали C и с выводом latin-1, с проверкой изоляции
make validate       # claude plugin validate . и plugin: маркетплейс и плагин
make check          # test, test-hostile и validate
# По умолчанию гоняются тесты затронутого функционала: python3 -m unittest tests.test_<модуль> …;
# make test и make check — только по просьбе автора (plugin/rules/verification.md).
make bashdiff       # фаззер детектора против bash 5.3 в песочнице bwrap (в check не входит)
```

`make bashdiff` (`tests/tools/bashdiff.py`) — дифференциальный фаззер: генератор форм с маркерами `touch P<n>`
(единственный источник форм; корпуса и транскрипты фаззер не читает и не исполняет), исполнение — обёрткой инструмента
Bash Claude Code (`eval` после `shopt -u extglob`) внутри `bwrap` (корень только для чтения, сеть выключена; без `bwrap`
или при провале пробы песочницы — выход с кодом 2). Режимы: `--target detector` (
`dependency_add`, `dependency_doubt` с `npm i evil` на месте маркеров: маркер исполнен, а детектор молчит, — потеря, код
1), `--target parser` (исполненные маркеры против `shparse.simple_commands`: имя `touch` с подстановками перед ним,
аргумент — литерал `P<n>` с подстановками после; маркер, исполненный как вывод подстановки в слове команды, — вид
`output`, не потеря).
Фаззер и любой разбор форм генератора — под `prlimit --as=2000000000` и не больше `--jobs 4`: формы, на которых разбор
растёт нелинейно, иначе съедают память машины. `tests/tools/` без `__init__.py`: unittest его не собирает. Разбор shell
— `tests/test_shparse.py`.

CI нет; цели — в `Makefile`. Проверяются два разных свойства:

1. Тесты изолированы от машины. `test-hostile` запускает тот же набор в неудобной локали и кодировке
   (`LC_ALL=C`, `PYTHONIOENCODING=latin-1`); хуки в нём работают в кодировке ascii: `Env.environ`
   ставит им `PYTHONUTF8=0`, иначе Python в локали C сам включает UTF-8 mode. Процесс тестов остаётся
   в UTF-8 mode. Чужие git-настройки — подпись коммитов и игнор `*.py` через `GIT_CONFIG_GLOBAL`,
   `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`, `GIT_CONFIG_PARAMETERS` (его экспортирует
   `git -c`, например при `rebase -x`) и `XDG_CONFIG_HOME/git/ignore` и `attributes` (`* binary`), — и
   `CLAUDE_PROJECT_DIR` на репозиторий, `CLAUDE_CONFIG_DIR=/nonexistent` до кода плагина не доходят:
   `tests/__init__.py` и `Env.environ` их подменяют; их задача — упасть, если изоляция сломана. Что
   `Env.environ` вычищает каждую из этих переменных, заданную снаружи, проверяет `EnvIsolationTest` в
   `tests/test_common.py`: `CLAUDE_CONFIG_DIR=/nonexistent` в `test-hostile` этого не ловит, тесты `guard_memory`
   ставят переменную сами.
2. Плагин работает под чужим git-конфигом пользователя — `tests/test_hostile_git.py`, в обеих целях.

## Устройство тестов

- По файлу `tests/test_<модуль>.py` на модуль `plugin/planka/`, кроме `manifest_watch` — его тесты в
  `tests/test_judge_tool.py` (через хук) и `tests/test_manifests.py` (функции модуля: файлы `_LEGACY`, цели правки,
  подключения, снимок); `tests/test_contract.py` — контракт с настоящими текстами правил, там же `SourceWarningsTest`:
  модули `plugin/planka/` компилируются без предупреждений (`SyntaxWarning` Python пишет в stderr при первой компиляции,
  а хук stderr не пишет; компилируется текст, `__pycache__` предупреждение не прячет); `tests/test_hostile_git.py` —
  git-код нескольких модулей под чужим git-конфигом.
- Модульные тесты импортируют `planka/<модуль>.py` напрямую через `sys.path`.
- Обращение к модели: место и регистр обращения `prompts.ADDRESS` в причине и контексте, без задвоения — `OutputsTest` в
  `tests/test_common.py`; промпты судьи с обращением и на «вы», `common.ADDRESS` — тот же объект, что `prompts.ADDRESS`,
  — `tests/test_prompts.py`; конец причины блока `Stop` — `judge_stop.RETELL_NOTE`, копией текста в
  `tests/test_judge_stop.py` (`StopHookTest`, `DoneHookTest`, `DocsFilterTest`). Судья яруса субагента —
  `SubagentTest` в `tests/test_judge_tool.py`: форк без судьи, нет `model` — отказ без судьи в лимите, вердикты судьи,
  его ошибка, лимит до вызова, в промпте модель, задание и реплика автора, обрезка задания, нет модуля, matcher
  пропускает `Agent`; текст входа — `RenderSubagentTest` в `tests/test_prompts.py`. Ожидаемый ответ хука в тестах хуков
  включает обращение. Однократность обращения — на настоящих текстах `plugin/`:
  `test_prompts.test_address_once_with_real_rubric` (рубрики хуков через `common.rubric`),
  `test_debug_watch.test_real_module_address_once` (настоящий `debugging.md`),
  `test_common.test_rule_texts_without_address` (все модули).
- `tests/__init__.py` изолирует процесс тестов от git-настроек и окружения машины: все переменные `GIT_*` и
  `CLAUDE_PROJECT_DIR` сняты; глобальный и системный конфиг и системные атрибуты git не читаются (`GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_NOSYSTEM`, `GIT_ATTR_NOSYSTEM`); `XDG_CONFIG_HOME` — пустой временный каталог (`ignore` и `attributes` из
  `XDG_CONFIG_HOME/git` git читает и при `GIT_CONFIG_GLOBAL`); `GIT_CEILING_DIRECTORIES` — временный каталог: внешний
  репозиторий не находится.
- Входные данные тестов — `tests/fixtures/`, корпуса настоящих форм входа (`plugin/rules/heuristics.md`); источник формы
  назван в каждом файле:
  - `plan-*.md` — планы (волны, формат superpowers, канонический формат `planning.md`); `test_planparse` (`CorpusTest`)
    прогоняет каждый `plan-*.md` и сверяет со структурой из `CORPUS` — волна, номер и файлы каждой задачи и конфликты
    владения; ожидание есть у каждого плана корпуса;
  - `transcript-shapes.jsonl` — формы записей транскрипта Claude Code с заменённым содержимым, в том числе `system`
    (`turn_duration`, `stop_hook_summary`, `local_command`), вложения `hook_*`, ответ из одного `thinking`,
    `tool_result` списком блоков и отказы инструмента `user-rejected` (с `userFeedback`), `permission-rule` (hook,
    config/safetyCheck), `automode-blocked`, в начале — две локальные команды до первой записи с `origin` (с выводом
    `<local-command-stdout>` и без него, с записью `system`/`local_command`, как в сессии, начатой с `/plugin`);
    `test_common` (`ReadTranscriptTest`) прогоняет корпус и его префиксы: префикс без `origin` — старый формат,
    локальные команды в нём — реплики; шаги реплики корпуса (`Transcript.turn_steps`: вызовы, их вывод, отклонённые
    вызовы) сверяет `test_corpus_service_entries_are_not_author_turn`;
  - `bash-failure-errors.jsonl` — поле `error` упавшего Bash во входе `PostToolUseFailure`; `test_debug_watch` прогоняет
    хук на каждом образце;
  - `bash-commands.jsonl` — команды `Bash` строкой JSON: `source` — `transcript` (настоящие команды из транскриптов,
    пути и сессии заменены) или `edge` (образец края), `expect` — `add`, `doubt` или `pass`, необязательный `"//"` —
    комментарий образца (известный ложный отказ или неочевидное сомнение — переменная цикла подкомандой `pnpm $a` — и
    обход); `test_depcheck` (`CorpusTest`) сверяет с ожиданием `dependency_add`, затем `dependency_doubt` каждой
    команды; настоящих команд в корпусе больше 1000 — тест держит это число;
  - `bash-transcripts.jsonl` — все команды `Bash` из транскриптов автора (`tests/tools/build_corpus.py`: повторы
    убраны, пути `/home/<имя>`, UUID, адреса почты и имя пользователя заменены, личные слова из `--private слово,…`
    (список передаётся при запуске, в репозиторий не пишется) — на `private`, строки с похожими на секреты значениями
    выброшены — печатается номер и вид шаблона, не значение) с записанным вердиктом `add`/`doubt` версии, на которой
    корпус собран; `test_bash_corpus` сверяет с ним `dependency_add`, затем `dependency_doubt` каждой строки. Строка
    меняет вердикт только по согласию автора;
  - `debug-watch-results.json` — записанный разбор `debug_watch` команд `bash-commands.jsonl` и
    `bash-transcripts.jsonl`: `sha256[:12]` команды → `sha256[:12]` от `_segments`, `_paren_pairs` и `code1_is_answer`;
    `CorpusResultsTest` в `tests/test_debug_watch.py` сверяет с ним каждую команду, `PLANKA_RECORD_RESULTS=1`
    перезаписывает файл — после проверки каждого изменения по bash; классы форм, сверенных с bash, — комментарием
    перед `ParenPairsTest`;
  - `comments-shell-results.json` — записанные комментарии sh и bash (`comments._shell_comments`) образцов
    `comments/` и форм `ShellTreeTest` в `tests/test_comments.py`: вход → `[[номер строки, комментарий]]`; у входа
    `multiline-quote` — результат bash;
  - `manifests/<образец>/<манифест>` — настоящие `package.json`, `composer.json`, `pyproject.toml`, `requirements*.txt`,
    `Cargo.toml`, `go.mod`, `Gemfile` (источник — комментарием, в JSON — ключом `"//"`); `test_manifests` (`CorpusTest`)
    сверяет имена каждого с `CORPUS` (ожидание есть у каждого файла корпуса: файлом корпуса считается файл, который git
    не игнорирует, `CorpusTest.test_every_fixture_has_expectation` берёт список `git ls-files --cached --others
    --exclude-standard`; кэши инструментов, например `.ruff_cache`, — не образцы), имя пакета самого манифеста с
    `OWN`, новые имена без старого текста и с тем же текстом (помощник `_added` — через `manifest_watch.edit_names`), и
    добавление одной строки в настоящий манифест — ровно одно имя. Образцы `legacy-*` — файлы `_LEGACY` без проверки:
    `setup.py` psf/requests v2.31.0, `setup.cfg` pytest 7.4.0, `Pipfile` pipenv v2023.12.1; их известные имена — `LEGACY_CORPUS`
    (`LegacySourcesTest`), они же — `LEGACY_SOURCES` хуков в `tests/test_judge_tool.py`. Образцы местных источников —
    `pyproject-uv-workspace`, `gemfile-path`, `gomod-replace-local`. Образцы `[project.optional-dependencies]` —
    `pyproject-gyp-next`, `pyproject-pandas`; `gomod-tool` — формы go.dev/ref/mod: директива `tool` (строкой и блоком),
    `toolchain`, `godebug`, `exclude`, `retract`, блок `require` со строкой-комментарием и `// indirect`; `tool`
    зависимость не объявляет. Сгенерированные файлы требований (pip-compile, `uv export`) ожидают пустое множество,
    `go.mod` — только прямые зависимости, без `// indirect`. Формы источников и подключений:
    `requirements-cookiecutter-django` (настоящий шаблон: `-r`, строки Jinja, хвостовые комментарии; формы `-c`,
    переноса, `--hash`, `${VAR}` — в `RequirementsTest`), `gomod-replace-versioned` (`replace` с версией и без, на
    каталог `..`), `gemfile-bundler-dsl` (`plugin`, `eval_gemfile`, git_source, ключ строкой), `pyproject-uv-overrides`,
    `pyproject-pdm-overrides`, `pyproject-poetry-multiple`, `npm-pnpm-extensions`, `composer-package-repo`.
  - `comments/<образец>/<файл>` — выдержки публичных проектов по синтаксисам (py, sh, yml/yaml — `run: |` с heredoc,
    `script:` GitLab, `post_build: - |` Read the Docs, `script: |` под `with:`, `description: |` OpenAPI и форма issue,
    rb, pl с вложенными `s{…}{…}se` и heredoc `<< "ID"`, go, rs, js, ts, tsx, jsx, sql, html, php (шаблоны
    `wordpress-twentytwelve`: `header.php`, `custom-header.php`), `pygments-lisp/lisp.py` (Pygments, сырая f-строка с
    `\'`), c, `Makefile` (и рецепт с `@#` — CPython), `Dockerfile`, toml, dart, kts; источник — первой
    строкой-комментарием файла, модули Perl — с расширением `.pl`); `test_comments` (`CorpusTest`) сверяет номера строк
    комментариев каждого файла с `CORPUS` (ожидание есть у каждого файла корпуса), что каждая строка вывода — хвост
    своей строки, и тот же вывод `comments.extract` вне git; однофайловые компоненты — `create-vite-vue/HelloWorld.vue`,
    `sveltekit-sverdle/page.svelte`;
- Тесты в git-репозитории (`GitCaptureTest`, `ChangedSinceGitTest`, git-тесты `test_comments`, `test_judge_stop`,
  `test_remind`, `test_common`, `test_hostile_git`, `ManifestEditGitTest`, `ManifestBashGitTest`,
  `ManifestConflictTest`, `ManifestTransferEditTest`, `MemoryHookTest.test_auto_memory_directory_in_repository`,
  `…_above_project`, `…_in_home_project`, `…_nested_repository`, `…_in_foreign_repository`) создают временный
  репозиторий и пропускаются без `git`.
- Проверка манифестов: разбор — `tests/test_manifests.py` (корпус, по классу на вид манифеста, `AddedContractTest`,
  `LegacySourcesTest` — разбор `_LEGACY` на корпусе `legacy-*`; строки и опции требований, как их читает pip, —
  `RequirementsTest.test_lines_as_pip_reads_them`, линейность разбора опций — `test_linear_on_options`; подключение
  `manifests.Include` и его снятие по перечню — `IncludeTest`, имя подключения — путь цели от корня проекта во всех
  источниках имён — `test_include_named_by_target_from_project_root`; манифесты правки по регистру и жёсткой ссылке —
  `EditTargetTest`, регистр компонентов пути — `test_other_case_components_on_case_insensitive_fs` (подмена
  `common.case_insensitive` и `os.path.realpath`; запись в другом регистре — ссылка, по одной за раз: двух записей,
  различных только регистром, на такой файловой системе не бывает); регистр у разрешённого пути (ссылка с непохожим
  именем в `Package.json`, ветка real в `_named_target`) — `test_other_case_through_link_on_case_insensitive_fs`; пути
  снимка при смене режима и переполнении перечня — `CompareLostListTest`; края: цикл присваиваний, цепочка из 30 имён,
  `test_setup_py_references_linear`; источник не по умолчанию и `index <url>` — `SourceTest`, операторы строки Gemfile —
  `StatementsTest`, имя пакета только из HEAD или члена workspace корня — `OwnNameTrustTest`, FIFO под именем манифеста
  — `FifoManifestTest`; патчи `git apply` до начала сессии — `OldPatchTest` и `OldPatchGitTest`, mtime `start.json` в
  долгой сессии — `SessionStartTtlTest`); хук — `tests/test_judge_tool.py`: `ManifestEditTest` (правка `Write`, `Edit`,
  `MultiEdit`), `ManifestEditGitTest` (версии `_REPO_REFS` — база), `ManifestBashTest` и его наследник
  `ManifestBashGitTest` (снимок и сравнение вне git и в git: генератор requirements и заголовок генератора — блок, с
  маркером — нет, маркер, `PostToolUseFailure`, параллельные команды по `tool_use_id`, `mv`, `cp`, `git mv`; ref до
  начала сессии: stash и ref `checkout`, `restore --source`, `merge --squash`, `cherry-pick -n` старше начала — пропуск
  (`test_author_stash_before_session_passes`, `test_author_stash_untracked_manifest_passes`,
  `test_restore_from_old_ref_passes`), моложе, в ту же секунду или без записи начала — блок с безопасным текстом
  (`test_stash_after_session_start_blocked_with_safe_reason`, `test_restore_from_young_ref_blocked`,
  `test_ref_in_session_start_second_not_old`, `test_old_ref_without_session_start_blocked`); начало сессии подменяется
  записью `start.json`; время ref — по коммиттеру, не автору, имена HEAD убранного манифеста, `Unavailable`
  `restored_names` по сроку, пределу и нечитаемому дереву ref (`test_restored_names_unreadable_tree_unavailable`) и
  пропуск снимка с предупреждением; перенос из незакоммиченного `_LEGACY` вне git и в git блокируется —
  `test_uncommitted_legacy_source_names_new`, из закоммиченного проходит —
  `test_committed_legacy_source_moved_to_pyproject_not_new`), `ManifestConflictTest` (стороны конфликта индекса после
  `git stash pop`, `git merge --squash`, `git cherry-pick -n`: `Write`, `Edit` разрешения и `git checkout --theirs`
  проходят, имя ни одной стороны отклоняется; источник `git rebase` — `REBASE_HEAD` после `git add` нашей стороны;
  стадии `:1:`, `:2:`, `:3:`, записанные `git update-index --index-info` в форме `git ls-files -s`, у каждой своё имя —
  `test_each_index_stage_read`), `ManifestTransferEditTest` (имена версии HEAD убранного манифеста и файлов `_LEGACY`
  версии HEAD проходят; имена `_LEGACY` рабочего дерева — неотслеживаемого, в подкаталоге `scratch/`, изменённого после
  коммита — новые: обход в два шага, `test_legacy_source_in_tree_only_names_new`; `SyntaxWarning` закоммиченного
  setup.py не в stderr), `ManifestFailureBranchesTest` (сбои `pop`, `compare`, срок правки — точный `systemMessage` и
  `skipped`), `CommandRefsTest` (формы команд git, из которых `command_refs` берёт ref, и формы без ref — `git apply`),
  `ManifestProjectUnderFixturesTest` (`FOREIGN_DIRS` — от проекта), `ManifestProjectUnderHomeRepoTest` (проект без
  своего git под `HOME`-репозиторием с `status.showUntrackedFiles=no` и `.gitignore` `*`: `list_manifests` — режим
  `walk`, правка и команда с новым именем — отказ и блок, имена версии HEAD и stash `~` не известны; файл вне корня
  читает версии своего репозитория), `ManifestBypassTest` (имя свежего манифеста вне workspace, правка через ссылку на
  файл и каталог, FIFO, `FOREIGN_DIRS` выше репозитория, возвращённые размер и mtime, запись в файл генератора до и
  после него), `ManifestWatchStateTest` (файл состояния снимков: срок записей, удаление пустого файла, предел обхода вне
  git, повторно не читаются файлы с тем же размером и mtime, та же длина с новым mtime перечитывается, смена режима
  git/walk — причина третьим элементом `compare`, `test_mode_change_lost`; `start.json` пишет первый `PreToolUse` один
  раз, `PostToolUse` и хук внутри судьи — нет); старый патч по относительному пути из подкаталога —
  `test_old_patch_by_relative_path_from_subdir_passes` (`ManifestBashGitTest`).
- `tests/test_hostile_git.py` передаёт враждебный конфиг явно — `GIT_CONFIG_GLOBAL` на временный файл — только вызовам
  плагина (`snapshot.capture`, `snapshot.changed_since`, `comments.extract`, `common.project_root` (`git rev-parse`,
  `git ls-files` под домашним каталогом-репозиторием — `HostileRootTest`), `guard_memory.is_memory_path`,
  `manifest_watch.list_manifests`, `manifest_watch.head_names`, `manifest_watch.restored_names`,
  `manifest_watch.project_names`, `manifest_watch.take` и `manifest_watch.compare` через `mock.patch.dict(os.environ)`,
  хукам `remind.py` и `judge_stop.py` через `Env.run`); git подготовки репозитория идёт под изолированным конфигом.
  Настройки `HOSTILE` не должны менять результат: каждая — отдельный `subTest` против ожидаемого результата под
  изолированным конфигом, все вместе — отдельный тест и тест хуков. Внешний diff, пейджер, монитор файловой системы и
  textconv-драйвер `diff.junk.textconv` — скрипт, печатающий строки вида diff и завершающийся с ошибкой;
  `core.attributesFile` — файл `ATTRIBUTES` (`*.py -diff`, у `a.py` драйвер `junk`). Репозиторий сценария держит свой
  `.gitattributes` (`b.py binary`, `ü.py -diff`), а `old.py` переименован в `moved.py` коммитом с дописанной строкой:
  комментарии `moved.py` — только новые, против старого пути. `NO_RENAMES` (`diff.renames=false`) и `INTER_HUNK` —
  отдельные тесты того же сценария. `core.excludesFile` законно меняет результат: `ExcludesFileTest` проверяет, что
  игнорируемый пользователем файл не попадает в изменения. Новый вызов git в плагине — его флаги проверяются здесь;
  новая настройка, которую вызов должен отключать, — в `HOSTILE`.
- Тесты хуков запускают скрипт подпроцессом: `tests.helpers.Env` создаёт временные `CLAUDE_PLUGIN_ROOT` (свои
  `philosophy.md` и `rules/` из `helpers.PHILOSOPHY` и `helpers.RULES`), `CLAUDE_PLUGIN_DATA`, каталог проекта и
  транскрипт `Env.transcript` с ответом ассистента модели `claude-test-model`; `Env.run` подаёт вход хука в stdin. Вход
  собирает `Env.hook_input(event, **fields)`: `session_id`, `prompt_id`, `cwd` — каталог проекта, `transcript_path` —
  `Env.transcript`. Ответ разбирают `helpers.output` и `helpers.messages` (строки `systemMessage`), журнал —
  `Env.log_lines`. `MemoryHookTest.run_tool` принимает `proc_cwd` — текущий каталог процесса хука: им проверяется, что
  относительный cowork не разрешается от каталога процесса.
- Гонка счётчика неудач команд — `StateLockRaceTest` в `tests/test_debug_watch.py`: `PARALLEL` хуков подпроцессом через
  обёртку `WRAPPER`, рандеву перед первым `common.data_dir` (до `state_lock`) и задержка `SLOW` после
  `common.read_json`; без блокировки записи затирают друг друга.
- Детектор зависимостей над деревом `shparse` — `tests/test_depcheck.py`: формы, которые разбор читает как bash
  (`GrammarFormsTest`; проверка остатка после фатальной ошибки — `test_syntax_error_recovery`), внешнее раскрытие в
  строке оболочки (`OuterExpansionScriptTest`: сомнение и его исключения, вход оболочки, маркер у оболочки покрывает
  тело heredoc — `test_marker_covers_heredoc`), имя команды — вывод подстановки (`NameOutputTest`); края разбора, на
  которых детектор падал или молчал (`ParserLimitsTest`: позиции слова, раскрытого по напечатанному тексту, и узлы в
  пределах каждого разобранного детектором текста — команды, строки `eval`, начала строк с ошибкой — на корпусах и
  формах фаззера (`test_parsed_texts_in_bounds`); вложенность
  10⁵ уровней — подпроцессом, сомнение `_WHY_NESTING`; сбой детектора и разбора — сомнение `_WHY_UNPARSED`, маркер в
  начале команды; конструкция, не закрытая до конца ввода, — начало строки проверяется; линейность `_name_expands`);
  вердикты корпусов — `CorpusTest` и `tests/test_bash_corpus.py`. Разбор shell сам по себе — `tests/test_shparse.py`:
  `PositionsTest` — `0 ≤ start ≤ end ≤ len(text)` у каждого узла на корпусах `tests/fixtures/bash-*.jsonl` и формах
  фаззера (зёрна 7 и 13, по 4000 форм обеих разновидностей; генератор `tests/tools/bashdiff.py` загружается по пути,
  формы только разбираются); `DepthLimitTest` — предел стека разбора `shparse._STACK_MAX`: 10⁵ уровней каждого вида
  вложенности подпроцессом (segfault не роняет тест-раннер) — ошибка `DEPTH`, корпуса и формы фаззера не доходят и до
  100 уровней (`_STACK_MAX` подменён на 100), дерево до предела — не глубже двух пределов. Тесты линейности, чей вход
  глубже предела, и `LinearityTest.test_deep_nesting` (глубина 10⁴ без `RecursionError`) снимают предел подменой
  `_STACK_MAX`: они проверяют сам разбор, а на пределе вложенный вход кончался бы ошибкой `DEPTH` сразу, и проверка
  прошла бы впустую. Такие тесты: `LinearityTest.test_nesting`, `test_heredocs`, `ReparseLinearityTest.test_nesting`,
  `PrintedTextTest.test_linear` (`tests/test_shparse.py`); `test_linear` классов `ArithmeticOutsideQuotesTest`,
  `ArithmeticQuotesTest`, `ArithmeticSubstitutionTest`, `ExpandedNameTest`, `FunctionSubstitutionTest`,
  `GrammarLinearTest`, `HeredocInSubstitutionEndTest`, `ParameterExpansionSpaceTest` (`tests/test_depcheck.py`);
  `LinearParseTest.test_string_substitutions_are_linear` (`tests/test_comments.py`),
  `SegmentsTest.test_shift_after_open_parens_linear` (`tests/test_debug_watch.py`). Новый тест линейности с
  вложенностью глубже предела подменяет его так же.
- Разделители команд `debug_watch._segments` (по дереву `shparse`) — `SegmentsTest` в `tests/test_debug_watch.py`:
  разделитель в подстановке, образце `case`, скобках массива и перенаправлении `>|` команды не делит
  (`test_operators_outside_words_only`), строка с фатальной синтаксической ошибкой — одна команда
  (`test_syntax_error_line_is_one_command`); арифметика без разделителей
  и `<<` в ней — сдвиг; `((` без закрытия `))` — подоболочка в подоболочке, её разделители делят команды
  (`test_nested_subshell_is_not_arithmetic`); тело heredoc с апострофом не прячет пары скобок `((…))` после терминатора
  (`test_heredoc_body_quote_does_not_hide_arithmetic_pairs`); не прячут и не сдвигают пары скобок ни тело heredoc рядом
  с арифметикой — в подоболочке `(` и после закрытой `((…))`
  (`test_heredoc_beside_arithmetic_does_not_hide_arithmetic_pairs`), ни `<<` внутри `((…))` и `$[…]` — сдвиг
  (`test_shift_does_not_hide_later_arithmetic_pairs`), ни here-string `<<<`
  (`test_here_string_does_not_hide_arithmetic_pairs`), ни `<<-` с табуляцией перед терминатором
  (`test_tab_stripped_heredoc_does_not_hide_arithmetic_pairs`).
- Линейность разбора проверяется отношением процессорного времени, а не абсолютным порогом: `helpers.cpu_seconds` —
  наименьшее `time.process_time` из трёх запусков, `helpers.assert_linear(test, small, large)` — запуск на входе
  вчетверо больше не дольше 8 запусков малого и 5 мс (линейный — около 4, квадратичный — около 16):
  `AddedContractTest.test_large_file_linear` (манифесты около 1 МБ), `LegacySourcesTest.test_setup_py_references_linear`
  (setup.py с шестью уровнями имён по 4 и 16 ссылок), `LinearParseTest` (комментарии),
  `FilterTest.test_list_items_linear_in_blank_lines`, `BacktickInDoubleQuotesTest.test_linear` в
  `tests/test_depcheck.py` (подстановка `` `…` `` в `"…"`: закрытая, незакрытая до конца команды, на многих строках),
  `GrammarLinearTest.test_linear` там же (формы грамматики над деревом),
  `BacktickOutsideQuotesTest.test_linear` там же (`` `…` `` вне кавычек и в `$(…)`), `ArithmeticQuotesTest.test_linear`
  там же (кавычки в арифметике `$((…))` и `$[…]`), `DoubtTest.test_linear` там же (формы сомнения детектора),
  `ArithmeticOutsideQuotesTest.test_linear` там же (арифметика, индексы и смещения вне кавычек),
  `ParameterExpansionSpaceTest.test_linear` там же (пробелы внутри `${…}` вне кавычек),
  `SubscriptWordQuotesTest`, `DoubleDollarTest`, `FunctionSubstitutionTest`, `HeredocInSubstitutionEndTest`,
  `EscapedBlankCommentTest` — их `test_linear` там же (кавычки в слове с `[`, `$$`, подстановка функции `${ …; }`,
  конец heredoc внутри подстановки, `\` перед пробелом и `#`),
  `RobustnessTest.test_adversarial_input_is_linear` (`tests/test_planparse.py`: каждый враждебный пункт и строка при
  множителе 1 и 4), `SegmentsTest.test_unclosed_test_brackets_linear` (`tests/test_debug_watch.py`),
  `SegmentsTest.test_shift_after_open_parens_linear` там же (`<` после тысяч открытых `(`),
  `Code1IsAnswerTest.test_long_word_linear` там же (слово длиннее `_HEAD_LIMIT`),
  `ShellTreeTest.test_many_fatal_errors_linear` в `tests/test_comments.py` (повторный разбор после каждой фатальной
  ошибки), `ParserLimitsTest.test_nested_parameter_name_linear` в `tests/test_depcheck.py` (имя команды из вложенных
  `${a:-…}`), `UserAndWatchWrappersTest.test_linear`
  в `tests/test_depcheck.py` (цепочки `su`/`runuser`/`watch`), `GemfileTest.test_linear_on_long_line` в
  `tests/test_manifests.py` (строки Gemfile из кавычек и пробелов), `ImportParseTest.test_strip_code_linear` в
  `tests/test_guard_memory.py` (незакрытые `<!--`, серии обратных кавычек);
  `WalkCaptureTest.test_walk_cost_independent_of_ext_count` — обход при 1 и 2000 расширениях `.gitignore` через
  `helpers.cpu_seconds`, порог 4. Часы стены и абсолютный порог в секундах под нагрузкой машины флакуют.
- Окружение `Env.environ` вычищает `PLANKA_*`, `CLAUDE_PLUGIN_OPTION_*`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR`,
  `CLAUDE_CODE_REMOTE_MEMORY_DIR` и `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`, ставит `HOME` во временный каталог,
  `PYTHONUTF8=0` (хук — в кодировке локали, как `python3` пользователя без UTF-8 mode) и `tests/stub` первым в `PATH`;
  `Env.run` обменивается с хуком в UTF-8, как Claude Code, при любой локали процесса тестов. Вместо `claude` отвечает
  заглушка `tests/stub/claude`. `judge_model` не задан — действует `session`, модель берётся из `Env.transcript` (файла
  модели сессии `Env` не пишет; модель из состояния — `tests/test_model_watch.py`). Новая переменная окружения, которую
  читает код, вычищается там же.

## Заглушка судьи

`tests/stub/claude` ведёт себя по `PLANKA_STUB`: `ok`, `deny` (причина из `PLANKA_STUB_REASON`), `violated` (`violated`
из `PLANKA_STUB_VIOLATED`, сырой JSON), `hang` (через `exec sleep`: висит сам процесс заглушки), `garbage` (не JSON),
`nostructured` (JSON без `structured_output`), `noOk` (`structured_output` без `ok`), `raw` (`structured_output`
целиком из `PLANKA_STUB_SO`, сырой JSON: ответы не по схеме), `unparsed` (строка с `{`, но не
JSON), `notlogged` (`is_error`). При `PLANKA_STUB_RECORD=<файл>` пишет туда аргументы, stdin, `PLANKA_JUDGE`, `cwd` и
PID процесса. Ответ с текстом собирает `python3` с `PYTHONUTF8=1` и `PYTHONIOENCODING=utf-8`: аргументы и вывод в UTF-8
в любой локали и кодировке вывода; не-ASCII не экранируется (`ensure_ascii=False`), как у `JSON.stringify` claude. Новый
вид ответа судьи — новая ветка заглушки.

## Тесты в том же процессе

Тестовых крючков в рабочем коде нет. Где нужно подменить модуль или платформу, тест зовёт код в
своём процессе с `mock.patch`:

- общий помощник — `helpers.run_in_process(env, main, hook_input, **environ)`: `main` хука через
  `common.run_hook` с окружением `Env.environ` и подменой stdin и stdout, ответ — словарём; подмены
  модулей ставит вызывающий;
- порог `snapshot.MAX_FILES` грязных путей в git, его отсутствие вне git, срок снимка (неудача запоминается: повтора для
  того же корня нет, другой корень снимается — `test_failed_snapshot_*`), срок git — `test_slow_git_keeps_reminder`
  (подложный `git status` спит 5 с и затем оставляет метку, срок снимка подменён на 1 с; метки нет — git прерван по
  сроку, время не замеряется) и сбой снимка — `remind.main` через `RemindTest.run_in_process` (обёртка над
  `helpers.run_in_process`); предел части вместе с обращением — `RemindTest.test_part_limit_counts_address`
  (`CONTEXT_LIMIT` подменён длиной ушедшего текста без единицы и ею самой); снимок walk без предела числа файлов,
  независимый от порядка readdir, проверка срока в `_walk_dirs`, имена не в UTF-8 и отказ `load` прошлому формату и
  повреждённым блокам — `WalkCaptureTest`, `ChangedSinceWalkTest`, `StoreLoadDiffTest` в `tests/test_snapshot.py`;
- перепривязка и отметка снимка (снимок другой версии формата или без поля `format` не перепривязывается —
  `test_other_format_not_carried_over`), неудачи снимка — `SnapshotStateTest` в `tests/test_snapshot.py`; прерванная
  реплика, блок, блок другим фильтром и ошибка судьи —
  `DocsFilterTest.test_interrupted_turn_edits_checked_on_next_stop`, `test_blocked_then_interrupted_turn_rechecked`,
  `test_block_on_other_filter_keeps_snapshot`, `test_checked_turn_not_rechecked`, `test_judge_error_marks_turn_checked`
  в `tests/test_judge_stop.py`;
- гонка лимита отказов (параллельный вызов исчерпал лимит между проверкой и отказом) —
  `helpers.run_in_process` в `tests/test_judge_tool.py` и `tests/test_judge_stop.py`; счётчик на
  пределе ставит `helpers.fill_budget`;
- `judge_stop.changed_this_turn` с подменой `snapshot` — `DocsFilterTest.in_process`; сроки сверки и
  разбора комментариев, переданные в `snapshot.changed_since` и `comments.extract`, —
  `DocsFilterTest.test_deadlines_passed_to_changed_since_and_extract`;
- разбор импортов `@путь` памяти — `ImportParseTest` в `tests/test_guard_memory.py`: корпус `CORPUS` — строки из
  примеров документации Claude Code о памяти с ожидаемыми путями; файл больше `MAX_IMPORT_FILE`, предел
  `MAX_IMPORT_FILES` с предупреждением, циклы импортов и ссылок `rules/`, FIFO, проба регистра
  `common.case_insensitive`;
- `autoMemoryDirectory` из managed-настроек — `guard_memory.is_memory_path` с подменой
  `guard_memory.managed_dir` на временный каталог (`ManagedSettingsTest`): системный каталог тест не пишет;
- `git check-ignore` в `guard_memory._repository_file` под чужим git-конфигом — `HostileMemoryTest` в
  `tests/test_hostile_git.py` (каталог cowork равен проекту: git проекта решает, что файл репозитория,
  что память): результат не меняется ни от одной настройки `HOSTILE`; законное влияние
  `core.excludesFile` — `ExcludesFileTest.test_user_excluded_file_is_memory`; срок вызова —
  `RepositoryFileTest` в `tests/test_guard_memory.py`; ответ 128 (проект не репозиторий) —
  `MemoryHookTest.test_auto_memory_directory_in_project_without_git`;
- корень проекта: монорепозиторий (новый, исключённый `.gitignore` и удалённый подкаталог) — вершина
  (`ProjectRootTest.test_untracked_subdir_of_monorepo_is_toplevel`); дом-репозиторий — `DotfilesHomeTest` в
  `tests/test_common.py` берёт «дом» `HOME` из `Env.environ`: вершина в предке дома, `HOME` через ссылку, имя
  каталога с `[ ]`, отслеживаемый `~/.config/nvim` — вершина, репозиторий внутри дома — вершина; тест, которому нужно
  отделение корня под `Env.project`, ставит `HOME` на `Env.project`;
- сторож судьи — `WatchdogTest` в `tests/test_common.py`: `common.run_judge` в процессе тестов (ответ,
  таймаут, нет `claude`); `test_judge_group_killed_with_hook` — хук подпроцессом в своей сессии (`start_new_session`:
  сбойный сторож бьёт `killpg(0)` по группе хука, а не раннера тестов), `claude` — скрипт с
  потомком: после `SIGKILL` хука оба умирают за `WATCHDOG_POLL` + 3 с; хук `Stop` целиком —
  `JudgeDiesWithHookTest` в `tests/test_judge_stop.py`, только на Linux (живость процесса — по `/proc`);
- сроки проверки манифестов — `ManifestDeadlineTest` в `tests/test_judge_tool.py`: `manifest_watch.take` и
  `compare` подменены, проверяется срок, который им передаёт `judge_tool.main`, и `HEAD_TIMEOUT` вызова
  `manifest_watch._git`; сумма сроков правки манифеста 2 · `HEAD_TIMEOUT` + `GIT_ROOT_TIMEOUT` +
  `SNAPSHOT_BUDGET` и `judge_tool.LINKED_BUDGET` против таймаута хука — `TimeoutsTest` (что `started` в
  `judge_manifest_edit` снят до перечня и `edit_targets`, — `test_linked_budget_counts_from_hook_work`); сбой снимка
  (больше `MAX_MANIFESTS`, недоступный
  каталог данных; предупреждение раз, `skipped` на каждую команду) — `ManifestSnapshotFailureTest`; имена ref
  в сроке снимка `SNAPSHOT_BUDGET` — `ManifestDeadlineTest.test_restored_ref_within_snapshot_budget`;
- манифесты под враждебным git-конфигом — `HostileManifestTest` в `tests/test_hostile_git.py`: `git ls-files`
  (отслеживаемые, неотслеживаемые, исключённые `.gitignore`, пути с пробелом и кириллицей, `FOREIGN_DIRS`) и `git
  cat-file --batch` версий `_REPO_REFS` при textconv-драйвере на манифесте и незавершённом слиянии (`MERGE_HEAD`),
  стороны конфликта индекса `:1:`, `:2:`, `:3:` (`cf/requirements.txt` под `diff=junk`, стадии записаны `git
  update-index --index-info`), `git cat-file blob` для манифеста с возвратом каретки в имени (`nl/requirements\r.txt`
  под `diff=junk`; имя с переводом строки не манифест), `git ls-files` жёсткой ссылки на манифест в `edit_targets`; `git
  cat-file --batch` и `git ls-tree` в `restored_names` — по ветке и `stash -u` с отслеживаемым и неотслеживаемым
  манифестом; в `project_names` и `compare` — дерево HEAD с `setup.py` под `diff=junk` (имя `legacydep` известно,
  `evilpkg` — новое).

Локаль с кодировкой ascii проверяет `NonUtf8LocaleTest` своим скриптом `_LOCALE_HOOK` подпроцессом
(`LC_ALL=C`, `PYTHONUTF8=0`): судья с русским промптом через сторож, корень проекта и
транскрипт по путям с кириллицей. Если кодировка файловой системы там не ascii, тест пропускается. Цель
записи в память, `cwd`, `autoMemoryDirectory` и пути окружения (`CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`,
`CLAUDE_CODE_REMOTE_MEMORY_DIR`, `CLAUDE_CONFIG_DIR`) с кириллицей в той же локали —
`MemoryHookTest.test_non_utf8_locale_cyrillic_paths` в `tests/test_guard_memory.py`, хуком подпроцессом, с тем же
пропуском.

Журнал без содержимого проверяет `helpers.assert_not_logged`: ни одна строка `judge.log` не содержит переданных текстов
(реплики автора, плана, сообщений); блок промпта судьи по тегам с кодом достаёт `helpers.prompt_block`, блок `<author>`
из записи заглушки — `helpers.author_block`. Помощники, нужные нескольким файлам тестов, лежат в `tests/helpers.py`:
тест не импортирует другой тест.

## Что тестами не покрыто

- Тесты хуков берут свои тексты правил (`helpers.PHILOSOPHY`, `helpers.RULES`); настоящие `plugin/philosophy.md` и
  `plugin/rules/*.md` проверяет только `tests/test_contract.py`: разделы и модули, которые код берёт по имени (ручные
  `SECTIONS`, `MODULES` и имена, выведенные из кода, — `test_names_taken_by_code_exist`), заголовок и строка условия
  «Читайте, мой дорогой друг,» каждого модуля, только известные метки (без учёта регистра), ссылки `{RULES}/<имя>.md` в
  правилах и коде, согласие правил с вопросами судьи и между собой (`RulesMatchJudgeTest`: самый правильный вариант
  в списке и первым — «Решения» 4 и вопрос 1 `prompts._CHOICE_CHECKS`, `test_correct_option_first_checked`; факт
  спрашивается, только если после поиска осталось несколько кандидатов, — «Спецификации» 2 и `planning.md`,
  `test_fact_question_rule_matches_planning`; форма «X, а не Y» в «Поведении» — утверждение с контрастом,
  `test_positive_statement_rule_names_its_form`), разбор форм вызова сборщиком
  (`CodeNamesTest`; имя ищется в своей функции, объемлющих и модуле, параметр функции не разрешается — `_Scopes`),
  списки кода, скопированные в `README.md` рядом с именем константы (`ReadmeCopiesTest`), форма «вы» (`PoliteFormTest`:
  нет «ты» и повелительного ед. числа в ядре, модулях, литералах `prompts.py` и текстах хуков, которые сборщик
  `_hook_texts` выводит из кода по аргументам `deny_output`, `block_output`, `context_output`; повелительное ед. числа
  ловится по форме слова на месте указания (`IMPERATIVE_PLACE`, не-глаголы на этом месте — `IMPERATIVE_EXCEPT`) и
  списком `SINGULAR_IMPERATIVES` в любом месте, в середине предложения вне списка — нет; «пожалуйста» в ядре, каждом
  модуле и каждом предложении ядра, модулей и текстов хуков с повелительным мн. числа (`_sentences_without_please`);
  баланс «» в блоке ядра и модулей; промпты судьи, которые зовут хуки, начинаются обращением; ответ хука собирается
  только в `common.py`), части ядра (`CorePartsTest`: каждая с путём `{RULES}` в 200 символов, строкой `NO_DOCS_LINE` и
  обращением `context_output` не длиннее `remind.CONTEXT_LIMIT`, склейка частей — ядро, хук на каждый номер части в
  `hooks/hooks.json`), пункты «Решения» 7 и «Планы» 9, полнота индекса «Модули», метки языков. Там же `TimeoutsTest`:
  сроки внутри хуков против таймаутов `hooks/hooks.json` (`context/architecture.md`, «Сроки»), в сумме —
  `common.GIT_ROOT_TIMEOUT`, `guard_memory.CHECK_IGNORE_TIMEOUT`, сроки `manifest_watch` и `judge_tool.LINKED_BUDGET`,
  и умолчание `timeout` у
  `common.run_judge` — `common.JUDGE_TIMEOUT`.
- Живой `claude`: вызов судьи, показ `systemMessage`, хуки `guard_memory`, `debug_watch` и `model_watch`, проверка
  манифестов (отказ правке и блок `PostToolUse`) в настоящей сессии проверяются только вручную.
- Сторож судьи на платформе не Linux не запускался.
- Вложения с `attachment.planFilePath` в корпусе `transcript-shapes.jsonl` нет: тесты собирают его
  сами (`type: plan_mode`), с настоящего транскрипта форма не снята.
- Набор целиком без UTF-8 mode не гоняется: в `test-hostile` в кодировке ascii работают только хуки
  подпроцессом (`Env.environ`) и `NonUtf8LocaleTest`; модульные тесты в процессе тестов идут в UTF-8 mode.
- Настройки `HOSTILE`, на которые вызовы плагина не реагируют по устройству git (`color.status`,
  `diff.mnemonicPrefix`, `diff.relative`, `diff.ignoreSubmodules`, `core.quotePath`, `core.pager`,
  `status.*` кроме `showUntrackedFiles`, `commit.gpgsign`, `log.showSignature`), не краснеют при снятии
  флага из вызова: тест держит их от будущих вызовов без флагов. `core.attributesFile` и
  `diff.junk.textconv` по отдельности тоже не краснеют при снятии `--no-textconv`: драйвер действует
  только вместе с файлом атрибутов, снятие ловят тесты всех настроек вместе. `core.fsmonitor` проверен
  только со сбойным хуком; монитор, который отвечает успехом и врёт, обманывает и сам git; встроенный демон не
  запускается.
