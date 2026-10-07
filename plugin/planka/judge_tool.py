"""PreToolUse: судья вопросов автору (AskUserQuestion), планов (ExitPlanMode) и
детерминированная проверка команд Bash."""
import time

import common
import depcheck
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


def judge_question(data):
    session = data.get("session_id", "")
    content = prompts.render_questions(data.get("tool_input") or {})
    if not content:
        return
    rubric = common.philosophy_sections("Решения")
    if rubric is None:
        # Предупреждение уже выдал philosophy_sections.
        common.log_event("question", session, verdict="skipped", error="нет раздела рубрики")
        return
    if _budget_spent("question", data, content):
        return
    transcript = common.read_transcript(data.get("transcript_path"))
    _judge_and_emit("question", data, transcript, prompts.question_prompt(rubric, content), content)


def judge_plan(data):
    session = data.get("session_id", "")
    transcript = common.read_transcript(data.get("transcript_path"))
    if transcript.plan_file is None:
        _skip("plan", session, "план не найден: в транскрипте нет planFilePath")
        return
    try:
        plan = transcript.plan_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _skip("plan", session, f"план не найден: нет файла {transcript.plan_file}")
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
    if rubric is None:
        # Предупреждение уже выдал common.rubric.
        common.log_event("plan", session, verdict="skipped", error="нет раздела рубрики")
        return
    _judge_and_emit("plan", data, transcript, prompts.plan_prompt(rubric, plan), plan)


DEP_REASON = ("planka: новая зависимость — вопрос автору (ядро, «Границы»): назови пакет, зачем он "
              "и что из stdlib или уже установленного задачу не закрывает; прочитай {rules}/dependencies.md. "
              "После согласия автора повтори команду, поставив {marker} прямо перед командой установки в её "
              "сегменте, а не в начале всей строки: `cd app && {marker} npm install x`. Команда: {command}")


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
