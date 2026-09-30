"""Stop: если последнее сообщение агента похоже на список вариантов, судья проверяет его по «Решениям»."""
import re
import time

import common
import prompts

_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.MULTILINE)
_KEYWORDS = re.compile(r"рекоменд|вариант|подход|recommended|option|approach", re.IGNORECASE)


def looks_like_options(text):
    return len(_LIST_ITEM.findall(text or "")) >= 2 and bool(_KEYWORDS.search(text or ""))


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if not data:
        return
    message = data.get("last_assistant_message") or ""
    if not looks_like_options(message):
        return
    session = data.get("session_id", "")
    prompt_id = data.get("prompt_id", "")
    rubric = common.philosophy_sections("Решения")
    if rubric is None:
        return
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT, prompts.message_prompt(rubric, message))
    duration_ms = int((time.monotonic() - started) * 1000)
    if verdict.error:
        common.warn(f"судья пропущен: {verdict.error}")
        common.log_event("stop", session, verdict="skipped", error=verdict.error,
                         duration_ms=duration_ms, content=message)
        return
    if verdict.ok:
        common.log_event("stop", session, verdict="ok", duration_ms=duration_ms, content=message)
        return
    if common.deny_budget_exhausted(session, prompt_id, "stop"):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event("stop", session, verdict="budget", reason=verdict.reason,
                         violated=verdict.violated, duration_ms=duration_ms, content=message)
        return
    reason = f"{verdict.reason} (нарушено: {', '.join(verdict.violated)})" if verdict.violated \
        else verdict.reason
    common.log_event("stop", session, verdict="deny", reason=verdict.reason,
                     violated=verdict.violated, duration_ms=duration_ms, content=message)
    common.emit(common.block_output(reason))


if __name__ == "__main__":
    main()
