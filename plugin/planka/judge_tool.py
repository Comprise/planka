"""PreToolUse: судья вопросов автору (AskUserQuestion), планов (ExitPlanMode), детерминированная проверка
команд Bash и правок манифестов (Write, Edit, MultiEdit). PostToolUse и PostToolUseFailure на Bash: новые
имена зависимостей в манифестах после команды."""
import functools
import os
import time

import common
import depcheck
import guard_memory
import manifest_watch
import planparse
import prompts


BUDGET_MSG = "лимит отказов, пропущено без проверки"


def _skip(hook, session, msg, **fields):
    common.warn(msg)
    common.log_event(hook, session, verdict="skipped", error=msg, **fields)


def _budget_spent(hook, data, content):
    """True с предупреждением и записью журнала, если лимит отказов по реплике и хуку исчерпан."""
    session, prompt_id = data.get("session_id", ""), data.get("prompt_id", "")
    if common.deny_budget_left(session, prompt_id, hook):
        return False
    common.warn(BUDGET_MSG)
    common.log_event(hook, session, verdict="budget", content=content)
    return True


def _deny(hook, data, message, verdict, content, **fields):
    """Отказ PreToolUse с причиной message; счётчик отказов растёт до ответа. Счётчик на пределе к моменту
    отказа — пропуск с предупреждением вместо отказа."""
    session, prompt_id = data.get("session_id", ""), data.get("prompt_id", "")
    if common.deny_budget_exhausted(session, prompt_id, hook):
        common.warn(BUDGET_MSG)
        common.log_event(hook, session, verdict="budget", content=content, **fields)
        return
    # Ответ запоминается до записи журнала: сбой записи не отменяет отказ.
    common.emit(common.deny_output(message))
    common.log_event(hook, session, verdict=verdict, content=content, **fields)


def _judge_and_emit(hook, data, transcript, user_prompt, content):
    """Вызов судьи; лимит отказов проверяет вызывающий до него."""
    session = data.get("session_id", "")
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT, user_prompt, common.judge_model(data, transcript))
    duration_ms = int((time.monotonic() - started) * 1000)
    if verdict.error:
        common.warn(common.skip_message(verdict))
        common.log_event(hook, session, verdict="skipped", error=verdict.error, duration_ms=duration_ms,
                         content=content)
        return
    if verdict.ok:
        common.log_event(hook, session, verdict="ok", duration_ms=duration_ms, content=content)
        return
    violated = f" (нарушено: {', '.join(verdict.violated)})" if verdict.violated else ""
    _deny(hook, data, f"planka: {verdict.reason}{violated}", "deny", content,
          reason=verdict.reason, violated=verdict.violated, duration_ms=duration_ms)


def _author(transcript):
    """Блок <author> судьи: прежние реплики автора, текущая и его ответы — граница задачи. В журнал не идёт:
    content журнала — только проверяемое содержимое."""
    return prompts.author_context(transcript.author_turn, transcript.author_answers, transcript.earlier_turns)


def judge_question(data):
    session = data.get("session_id", "")
    content = prompts.render_questions(data.get("tool_input") or {})
    if not content:
        return
    rubric = common.philosophy_sections("Решения")
    rubric = prompts.without_premises(rubric) if rubric is not None else None
    if rubric is None:
        # Предупреждение уже выдал philosophy_sections.
        common.log_event("question", session, verdict="skipped", error="нет раздела рубрики")
        return
    if _budget_spent("question", data, content):
        return
    transcript = common.read_transcript(data.get("transcript_path"))
    _judge_and_emit("question", data, transcript, prompts.question_prompt(rubric, content, _author(transcript)),
                    content)


def judge_plan(data):
    session = data.get("session_id", "")
    transcript = common.read_transcript(data.get("transcript_path"))
    if transcript.plan_file is None:
        _skip("plan", session, "план не найден: в транскрипте нет planFilePath")
        return
    try:
        plan = transcript.plan_file.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        _skip("plan", session, f"план не найден: нет файла {transcript.plan_file}")
        return
    except (OSError, ValueError) as e:
        # ValueError — NUL в пути из транскрипта.
        _skip("plan", session, f"план не прочитан: {transcript.plan_file}: {e!r}")
        return
    if _budget_spent("plan", data, plan):
        return
    tasks = planparse.parse_plan(plan)
    conflicts = planparse.shared_files(tasks) if tasks else None
    if conflicts:
        reason = ("planka: в одной волне файл принадлежит нескольким задачам:\n"
                  + planparse.format_conflicts(conflicts))
        _deny("plan", data, reason, "deny-files", plan, reason=reason)
        return
    rubric = common.rubric(("Решения", "Планы"), ("planning", "subagents", "refactoring", "design-patterns", "heuristics"))
    rubric = prompts.without_premises(rubric) if rubric is not None else None
    if rubric is None:
        # Предупреждение уже выдал common.rubric.
        common.log_event("plan", session, verdict="skipped", error="нет раздела рубрики")
        return
    _judge_and_emit("plan", data, transcript, prompts.plan_prompt(rubric, plan, _author(transcript)), plan)


DEP_REASON = ("planka: новая зависимость — вопрос автору (ядро, «Границы»): назови пакет, зачем он "
              "и что из stdlib или уже установленного задачу не закрывает; прочитай {rules}/dependencies.md. "
              "После согласия автора повтори команду, поставив {marker} прямо перед командой установки в её "
              "сегменте, а не в начале всей строки: `cd app && {marker} npm install x`. Команда: {command}")


DEP_DOUBT_REASON = ("planka: команда похожа на добавление пакета, но разбор под сомнением: {why}. Если она добавляет "
                    "пакет, это новая зависимость — вопрос автору (ядро, «Границы»): назови пакет, зачем он и что из "
                    "stdlib или уже установленного задачу не закрывает; прочитай {rules}/dependencies.md и после "
                    "согласия автора повтори команду с {marker} прямо перед командой в её сегменте. Если пакета она не "
                    "добавляет, повтори её так же с {marker}. Команда: {command}")


def judge_bash(data):
    """Добавление пакета (depcheck.dependency_add) и команда установки с пакетом или подкомандой под сомнением
    (depcheck.dependency_doubt) отклоняются детерминированно: без модели и без лимита отказов. Иначе — снимок
    манифестов для сравнения после команды (snapshot_manifests)."""
    command = (data.get("tool_input") or {}).get("command")
    segment = depcheck.dependency_add(command)
    doubt = None if segment is not None else depcheck.dependency_doubt(command)
    if segment is None and doubt is None:
        snapshot_manifests(data)
        return
    session = data.get("session_id", "")
    if doubt is None:
        reason = DEP_REASON.format(rules=common.rules_dir(), marker=depcheck.DEP_OK_MARKER, command=segment)
    else:
        reason = DEP_DOUBT_REASON.format(why=doubt[1], rules=common.rules_dir(), marker=depcheck.DEP_OK_MARKER,
                                         command=doubt[0])
    common.emit(common.deny_output(reason))
    common.log_event("bash", session, verdict="deny-dep", content=command)


MANIFEST_HOOK = "manifest"
EDIT_TOOLS = {"Write", "Edit", "MultiEdit"}
MANIFEST_REASON = ("planka: новая зависимость — вопрос автору (ядро, «Границы»): правка {path} добавляет {names}. "
                   "Назови пакет, зачем он и что из stdlib или уже установленного задачу не закрывает; прочитай "
                   "{rules}/dependencies.md. После согласия автора добавь пакет командой Bash с {marker} прямо перед "
                   "ней в её сегменте — менеджером пакетов (`{marker} npm install x`) или правкой файла "
                   "(`{marker} python3 -c …`): правка файловым инструментом с новым именем отклоняется всегда.")
COMMAND_REASON = ("planka: после команды в манифесте новая зависимость — вопрос автору (ядро, «Границы»): {added}. "
                  "Правка уже в файле, и хук не знает, чья она: команда могла вернуть работу автора (git stash "
                  "pop, merge, apply, checkout файла). Не откатывай манифест вслепую — спроси автора: назови "
                  "пакет, зачем он и что из stdlib или уже установленного задачу не закрывает; прочитай "
                  "{rules}/dependencies.md. Если автор против, откатывай только свою правку. Команду, которая "
                  "по согласию автора добавляет пакет, запускай с {marker} прямо перед ней в её сегменте.")


def _session(data):
    session = data.get("session_id")
    return session if isinstance(session, str) else ""


def _manifest_skip(session, msg, **fields):
    common.warn(msg)
    common.log_event(MANIFEST_HOOK, session, verdict="skipped", error=msg, **fields)


def _project_names(root, kind):
    """Имена манифестов реестра вида kind и пакетов самого проекта под корнем проекта root() и в его версии HEAD,
    файлов manifest_watch._LEGACY в версии HEAD (manifest_watch.project_names) в пределах SNAPSHOT_BUDGET; root()
    None — пусто."""
    root = root()
    if root is None:
        return frozenset()
    deadline = time.monotonic() + manifest_watch.SNAPSHOT_BUDGET
    try:
        return manifest_watch.project_names(root, kind, deadline)
    except manifest_watch.Unavailable as e:
        raise manifest_watch.Unavailable(f"не сравнён с другими манифестами проекта ({e})") from None


def judge_manifest_edit(data):
    """Write, Edit, MultiEdit манифеста с новым именем зависимости отклоняются без модели и без лимита
    отказов. Правку, которую инструмент сам отклонит, и не манифесты хук пропускает молча."""
    tool, tool_input = data.get("tool_name"), data.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    path = guard_memory.target_path(data)
    if path is None:
        return
    # Каталоги FOREIGN_DIRS ищутся в пути от проекта: проект сам может лежать под fixtures/.
    cwd = data.get("cwd")
    project = os.environ.get("CLAUDE_PROJECT_DIR") or common.input_path(cwd) or ""
    rel = os.path.relpath(path, project) if isinstance(project, str) and os.path.isabs(project) else path
    kind = manifest_watch.watched_kind(path if rel.startswith("..") else rel)
    if kind is None:
        return
    session = _session(data)
    # Корень проекта: проект неизвестен (нет CLAUDE_PROJECT_DIR и абсолютного cwd) — None.
    known_project = isinstance(project, str) and os.path.isabs(project)
    root = functools.cache(lambda: common.project_root(cwd) if known_project else None)
    try:
        names = manifest_watch.check_edit(tool, tool_input, path, kind, lambda: _project_names(root, kind), root)
    except manifest_watch.Unavailable as e:
        _manifest_skip(session, f"манифест {path} {e}: новые зависимости не проверены", tool=tool)
        return
    except TimeoutError:
        _manifest_skip(session, f"манифест {path}: git или обход проекта не уложились в срок, новые зависимости "
                                f"не проверены", tool=tool)
        return
    if not names:
        return
    reason = MANIFEST_REASON.format(path=path, names=", ".join(names), rules=common.rules_dir(),
                                    marker=depcheck.DEP_OK_MARKER)
    # Ответ запоминается до записи журнала: сбой записи не отменяет отказ.
    common.emit(common.deny_output(reason))
    common.log_event(MANIFEST_HOOK, session, verdict="deny-dep", tool=tool, added=len(names),
                     content=tool_input.get("content") if tool == "Write" else common.dumps(tool_input))


def _command_and_id(data):
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    tool_use_id = data.get("tool_use_id")
    if not isinstance(command, str) or not isinstance(tool_use_id, str) or not tool_use_id:
        return None, None
    return command, tool_use_id


def snapshot_manifests(data):
    """Снимок имён манифестов проекта перед командой Bash с именами ref до начала сессии, откуда команда
    возвращает файлы (manifest_watch.restored_names); команда с маркером согласия не снимается. Сбой снимка или
    чтения имён ref — предупреждение раз на сессию и пропуск в журнале на каждую команду, снимок не сохраняется,
    команда идёт без проверки манифестов."""
    command, tool_use_id = _command_and_id(data)
    cwd = data.get("cwd")
    if command is None or manifest_watch.has_marker(command) or not isinstance(cwd, str) or not cwd:
        return
    deadline = time.monotonic() + manifest_watch.SNAPSHOT_BUDGET
    session = _session(data)
    try:
        root = common.project_root(cwd)
        entry = manifest_watch.take(root, deadline)
        # Каталог команды до неё: от него разрешаются пути вывода генераторов requirements и патчей git apply.
        entry["cwd"] = common.input_path(cwd)
        # Имена ref до начала сессии, откуда команда git возвращает файлы, и старых патчей — работа автора.
        known = manifest_watch.restored_names(root, command, manifest_watch.session_start(session), deadline,
                                              cwd=entry["cwd"])
        if known:
            entry["known"] = known
        manifest_watch.store(session, tool_use_id, entry)
    except (manifest_watch.Unavailable, OSError) as e:
        # TimeoutError — подкласс OSError.
        msg = f"снимок манифестов не снят ({e}): новые зависимости от команд Bash не проверяются"
        try:
            common.warn_once(session, "manifest-snapshot", msg)
        except OSError:
            # Каталог состояния недоступен: отметку раз на сессию не записать.
            common.warn(msg)
        try:
            common.log_event(MANIFEST_HOOK, session, verdict="skipped", error=msg)
        except OSError:
            # Каталог данных недоступен: предупреждение уже выдано.
            pass


def check_command_manifests(data):
    """После команды Bash: новые имена в манифестах против снимка — блок PostToolUse с причиной агенту."""
    if data.get("tool_name") != "Bash":
        return
    command, tool_use_id = _command_and_id(data)
    if command is None:
        return
    session = _session(data)
    deadline = time.monotonic() + manifest_watch.CHECK_BUDGET
    try:
        entry = manifest_watch.pop(session, tool_use_id)
    except OSError as e:
        msg = f"снимок манифестов не прочитан, новые зависимости после команды не проверены: {e!r}"
        common.warn(msg)
        try:
            common.log_event(MANIFEST_HOOK, session, verdict="skipped", error=msg)
        except OSError:
            # Каталог данных недоступен: предупреждение уже выдано.
            pass
        return
    if entry is None or manifest_watch.has_marker(command):
        return
    try:
        # Файл requirements, который пишет генератор (pip freeze, poetry export и др.), не проверяется: пути
        # вывода — от каталога до команды и после неё (cd внутри команды).
        cwd = data.get("cwd")
        skip = manifest_watch.resolve(manifest_watch.generated_requirements(command),
                                      [entry.get("cwd"), common.input_path(cwd) if isinstance(cwd, str) else None])
        added, unknown = manifest_watch.compare(entry, deadline, skip)
    except (manifest_watch.Unavailable, OSError) as e:
        _manifest_skip(session, f"манифесты после команды не проверены: {e}")
        return
    if unknown:
        _manifest_skip(session, f"манифест не разобран до или после команды, новые зависимости не проверены: "
                                f"{', '.join(unknown)}")
    if not added:
        return
    listed = "; ".join(f"{rel}: {', '.join(names)}" for rel, names in sorted(added.items()))
    reason = COMMAND_REASON.format(added=listed, rules=common.rules_dir(), marker=depcheck.DEP_OK_MARKER)
    common.emit(common.block_output(reason))
    common.log_event(MANIFEST_HOOK, session, verdict="block-dep", manifests=len(added),
                     added=sum(map(len, added.values())), content=command)


POST_EVENTS = ("PostToolUse", "PostToolUseFailure")


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if not data:
        return
    if data.get("hook_event_name") in POST_EVENTS:
        check_command_manifests(data)
        return
    try:
        manifest_watch.mark_start(_session(data))
    except OSError:
        # Каталог состояния недоступен: без начала сессии ref не сверяются, блок после команды остаётся;
        # снимок манифестов предупредит сам.
        pass
    tool = data.get("tool_name")
    if tool == "AskUserQuestion":
        judge_question(data)
    elif tool == "ExitPlanMode":
        judge_plan(data)
    elif tool == "Bash":
        judge_bash(data)
    elif tool in EDIT_TOOLS:
        judge_manifest_edit(data)


if __name__ == "__main__":
    common.run_hook(main)
