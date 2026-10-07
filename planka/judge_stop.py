"""Stop: сообщение со списком вариантов судится по «Решениям», заявка о готовности — по модулю проверки;
при совпадении обоих фильтров — один вызов."""
import re
import time

import common
import prompts

_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.MULTILINE)
_KEYWORDS = re.compile(r"рекоменд|вариант|подход|recommended|option|approach", re.IGNORECASE)


def looks_like_options(text):
    return len(_LIST_ITEM.findall(text or "")) >= 2 and bool(_KEYWORDS.search(text or ""))


_DONE = re.compile(r"\b(?:готов[оаы]|сделан[оаы]?|исправлен[оаы]?|починен[оаы]?|проход[яи]т|"
                   r"прош[её]л|прошли|зел[её]н\w*|done|fixed|passing|passes|completed?)\b",
                   re.IGNORECASE)


def claims_done(text):
    return bool(_DONE.search(text or ""))


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if not data:
        return
    message = data.get("last_assistant_message") or ""
    options = looks_like_options(message)
    done = claims_done(message)
    if not options and not done:
        return
    filters = [name for flag, name in ((options, "options"), (done, "done")) if flag]
    session = data.get("session_id", "")
    prompt_id = data.get("prompt_id", "")
    rubric = common.rubric(("Решения",) if options else (), ("verification",) if done else ())
    if rubric is None:
        # Предупреждение уже выдал common.rubric.
        common.log_event("stop", session, verdict="skipped", error="нет раздела рубрики", filters=filters)
        return
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT,
                               prompts.stop_prompt(rubric, message, options=options, done=done))
    duration_ms = int((time.monotonic() - started) * 1000)
    if verdict.error:
        common.warn(f"судья пропущен: {verdict.error}")
        common.log_event("stop", session, verdict="skipped", error=verdict.error,
                         duration_ms=duration_ms, content=message, filters=filters)
        return
    if verdict.ok:
        common.log_event("stop", session, verdict="ok", duration_ms=duration_ms, content=message, filters=filters)
        return
    if common.deny_budget_exhausted(session, prompt_id, "stop"):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event("stop", session, verdict="budget", reason=verdict.reason,
                         violated=verdict.violated, duration_ms=duration_ms, content=message, filters=filters)
        return
    violated = f" (нарушено: {', '.join(verdict.violated)})" if verdict.violated else ""
    reason = f"planka: {verdict.reason}{violated}"
    common.log_event("stop", session, verdict="deny", reason=verdict.reason,
                     violated=verdict.violated, duration_ms=duration_ms, content=message, filters=filters)
    common.emit(common.block_output(reason))


if __name__ == "__main__":
    common.run_hook(main)
