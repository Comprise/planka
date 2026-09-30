"""PreToolUse: судья вопросов автору (AskUserQuestion) и планов (ExitPlanMode)."""
import json
import pathlib
import time

import common
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
    reason = f"{verdict.reason} (нарушено: {', '.join(verdict.violated)})" if verdict.violated \
        else verdict.reason
    common.log_event(hook, session, verdict="deny", reason=verdict.reason,
                     violated=verdict.violated, duration_ms=duration_ms, content=content)
    common.emit(common.deny_output(reason))


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
            reason = "в одной волне файл принадлежит нескольким задачам:\n" + planparse.format_conflicts(conflicts)
            common.log_event("plan", session, verdict="deny-files", reason=reason, content=plan)
            common.emit(common.deny_output(reason))
            return
    rubric = common.philosophy_sections("Решения", "Планы")
    if rubric is None:
        # Предупреждение уже выдал philosophy_sections.
        common.log_event("plan", session, verdict="skipped", error="нет раздела рубрики")
        return
    _judge_and_emit("plan", session, prompt_id, prompts.plan_prompt(rubric, plan), plan)


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


if __name__ == "__main__":
    common.run_hook(main)
