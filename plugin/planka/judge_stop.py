"""Stop: сообщение со списком вариантов судится по «Решениям», заявка о готовности — по модулю проверки,
правка кода со снимка текущей реплики — по модулям документации и комментариев;
при совпадении нескольких фильтров — один вызов."""
import re
import time

import comments
import common
import prompts
import snapshot

_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.MULTILINE)
_KEYWORDS = re.compile(r"рекоменд|вариант|подход|recommended|option|approach", re.IGNORECASE)


def looks_like_options(text):
    return len(_LIST_ITEM.findall(text or "")) >= 2 and bool(_KEYWORDS.search(text or ""))


_DONE = re.compile(r"\b(?:готов[оаы]|сделан[оаы]?|исправлен[оаы]?|починен[оаы]?|проход[яи]т|"
                   r"прош[её]л|прошли|зел[её]н\w*|done|fixed|passing|passes|completed?)\b",
                   re.IGNORECASE)


def claims_done(text):
    return bool(_DONE.search(text or ""))


def changed_this_turn(data):
    """Корень проекта и (путь, класс по common.path_kind, есть ли файл сейчас) со снимка текущей реплики;
    None без снимка."""
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    snap = snapshot.load(common.data_dir() / "state", data.get("session_id", ""))
    if snap is None or snap.get("prompt_id") != data.get("prompt_id", ""):
        return None
    root = common.project_root(cwd)
    if str(root) != snap.get("root"):
        return None
    now = snapshot.scan(root)
    if now is None:
        return None
    return root, [(p, common.path_kind(p), (root / p).is_file()) for p in snapshot.diff(snap["files"], now)]


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if not data:
        return
    message = data.get("last_assistant_message") or ""
    options = looks_like_options(message)
    done = claims_done(message)
    docs_info = changed_this_turn(data)
    docs = bool(docs_info and any(kind == "code" for _, kind, _ in docs_info[1]))
    if not (options or done or docs):
        return
    filters = [name for flag, name in ((options, "options"), (done, "done"), (docs, "docs")) if flag]
    meta = {"filters": filters}
    if docs:
        meta["files"] = [p for p, _, _ in docs_info[1][:prompts.MAX_LISTED]]
    session = data.get("session_id", "")
    prompt_id = data.get("prompt_id", "")
    modules = []
    if done:
        modules.append("verification")
    if docs:
        modules += ["docs", "comments"]
    rubric = common.rubric(("Решения",) if options else (), tuple(modules))
    if rubric is None:
        # Предупреждение уже выдал common.rubric.
        common.log_event("stop", session, verdict="skipped", error="нет раздела рубрики", **meta)
        return
    content = message
    if docs:
        root, changed = docs_info
        lines, truncated, unknown = comments.extract(
            root, [p for p, kind, exists in changed if kind == "code" and exists])
        content = prompts.render_docs_content(message, changed, lines, truncated,
                                              not (root / "CLAUDE.md").exists(), unknown)
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT,
                               prompts.stop_prompt(rubric, content, options=options, done=done, docs=docs))
    duration_ms = int((time.monotonic() - started) * 1000)
    if verdict.error:
        common.warn(f"судья пропущен: {verdict.error}")
        common.log_event("stop", session, verdict="skipped", error=verdict.error,
                         duration_ms=duration_ms, content=content, **meta)
        return
    if verdict.ok:
        common.log_event("stop", session, verdict="ok", duration_ms=duration_ms, content=content, **meta)
        return
    if common.deny_budget_exhausted(session, prompt_id, "stop"):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event("stop", session, verdict="budget", reason=verdict.reason,
                         violated=verdict.violated, duration_ms=duration_ms, content=content, **meta)
        return
    violated = f" (нарушено: {', '.join(verdict.violated)})" if verdict.violated else ""
    reason = f"planka: {verdict.reason}{violated}"
    # Ответ запоминается до записи журнала: сбой записи не отменяет отказ.
    common.emit(common.block_output(reason))
    common.log_event("stop", session, verdict="deny", reason=verdict.reason,
                     violated=verdict.violated, duration_ms=duration_ms, content=content, **meta)


if __name__ == "__main__":
    common.run_hook(main)
