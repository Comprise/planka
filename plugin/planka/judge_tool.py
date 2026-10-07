"""PreToolUse: судья вопросов автору (AskUserQuestion), планов (ExitPlanMode) и
детерминированная проверка команд Bash."""
import json
import pathlib
import time

import common
import depcheck
import planparse
import prompts


def plan_file_from_transcript(path):
    """Последняя запись транскрипта с attachment.planFilePath; None, если её нет."""
    if not isinstance(path, str) or not path:
        return None
    found = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                att = entry.get("attachment") if isinstance(entry, dict) else None
                p = att.get("planFilePath") if isinstance(att, dict) else None
                if isinstance(p, str) and p:
                    found = p
    except OSError:
        return None
    return pathlib.Path(found) if found else None


def _skip(hook, session, msg, **fields):
    common.warn(msg)
    common.log_event(hook, session, verdict="skipped", error=msg, **fields)


def _judge_and_emit(hook, session, prompt_id, user_prompt, content):
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT, user_prompt)
    duration_ms = int((time.monotonic() - started) * 1000)
    if verdict.error:
        _skip(hook, session, f"судья пропущен: {verdict.error}", duration_ms=duration_ms, content=content)
        return
    if verdict.ok:
        common.log_event(hook, session, verdict="ok", duration_ms=duration_ms, content=content)
        return
    if common.deny_budget_exhausted(session, prompt_id, hook):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event(hook, session, verdict="budget", reason=verdict.reason,
                         violated=verdict.violated, duration_ms=duration_ms, content=content)
        return
    violated = f" (нарушено: {', '.join(verdict.violated)})" if verdict.violated else ""
    reason = f"planka: {verdict.reason}{violated}"
    # Ответ запоминается до записи журнала: сбой записи не отменяет отказ.
    common.emit(common.deny_output(reason))
    common.log_event(hook, session, verdict="deny", reason=verdict.reason,
                     violated=verdict.violated, duration_ms=duration_ms, content=content)


def judge_question(data):
    session, prompt_id = data.get("session_id", ""), data.get("prompt_id", "")
    content = prompts.render_questions(data.get("tool_input") or {})
    if not content:
        return
    rubric = common.philosophy_sections("Решения")
    if rubric is None:
        # Предупреждение уже выдал philosophy_sections.
        common.log_event("question", session, verdict="skipped", error="нет раздела рубрики")
        return
    _judge_and_emit("question", session, prompt_id, prompts.question_prompt(rubric, content), content)


def judge_plan(data):
    session, prompt_id = data.get("session_id", ""), data.get("prompt_id", "")
    plan_path = plan_file_from_transcript(data.get("transcript_path", ""))
    if plan_path is None:
        _skip("plan", session, "план не найден: в транскрипте нет planFilePath")
        return
    try:
        plan = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _skip("plan", session, f"план не найден: нет файла {plan_path}")
        return
    tasks = planparse.parse_plan(plan)
    if tasks:
        conflicts = planparse.shared_files(tasks)
        if conflicts:
            if common.deny_budget_exhausted(session, prompt_id, "plan"):
                common.warn("лимит отказов, пропущено без проверки")
                common.log_event("plan", session, verdict="budget", reason=planparse.format_conflicts(conflicts))
                return
            reason = ("planka: в одной волне файл принадлежит нескольким задачам:\n"
                      + planparse.format_conflicts(conflicts))
            common.emit(common.deny_output(reason))
            common.log_event("plan", session, verdict="deny-files", reason=reason, content=plan)
            return
    rubric = common.rubric(("Решения", "Планы"), ("planning", "subagents"))
    if rubric is None:
        # Предупреждение уже выдал common.rubric.
        common.log_event("plan", session, verdict="skipped", error="нет раздела рубрики")
        return
    _judge_and_emit("plan", session, prompt_id, prompts.plan_prompt(rubric, plan), plan)


DEP_REASON = ("planka: новая зависимость — вопрос автору (ядро, «Границы»): назови пакет, зачем он "
              "и что из stdlib или уже установленного задачу не закрывает; прочитай {rules}/dependencies.md. "
              "После согласия автора повтори команду с префиксом {marker}. Команда: {command}")


def judge_bash(data):
    """Добавление пакета отклоняется детерминированно: без модели и без лимита отказов."""
    command = (data.get("tool_input") or {}).get("command")
    segment = depcheck.dependency_add(command)
    if segment is None:
        return
    session = data.get("session_id", "")
    reason = DEP_REASON.format(rules=common.rules_dir(), marker=depcheck.DEP_OK_MARKER, command=segment)
    common.emit(common.deny_output(reason))
    common.log_event("bash", session, verdict="deny-dep", content=command)


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if not data:
        return
    tool = data.get("tool_name")
    if tool == "AskUserQuestion":
        judge_question(data)
    elif tool == "ExitPlanMode":
        judge_plan(data)
    elif tool == "Bash":
        judge_bash(data)


if __name__ == "__main__":
    common.run_hook(main)
