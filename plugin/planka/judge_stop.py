"""Stop: последнее сообщение со списком вариантов судится по «Решениям», заявка о готовности — по модулю
проверки, правка кода со снимка текущей реплики — по модулям документации, комментариев, паттернов и
рефакторинга; при совпадении нескольких фильтров — один вызов, судья видит все сообщения реплики."""
import re
import time

import comments
import common
import prompts
import snapshot

_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.MULTILINE)
# Целые слова: «подходит», optional, adoption и тип Option<T> не совпадают.
_KEYWORDS = re.compile(r"\b(?:рекоменд\w*|вариант\w*|подход(?:а|е|у|ы|ом|ов|ам|ами|ах)?|"
                       r"recommend(?:s|ed|ation|ations)?|options?(?!<|::)|approach(?:es)?)\b",
                       re.IGNORECASE)


def looks_like_options(text):
    return len(_LIST_ITEM.findall(text or "")) >= 2 and bool(_KEYWORDS.search(text or ""))


# «готов» — только перед знаком препинания или концом строки: «Фикс готов.», но не «готов обсудить».
_DONE = re.compile(r"\b(?:готов[оаы]|готов(?=[ \t]*(?:[.!?,;:)\n]|\Z))|сделан[оаы]?|исправлен[оаы]?|"
                   r"починен[оаы]?|выполнен[оаы]?|заверш[её]н[оаы]?|проход[яи]т|прош[её]л|прошли|зел[её]н\w*|"
                   r"done|fixed|passing|passes|passed|succeeded|succeeds|completed?)\b",
                   re.IGNORECASE)


def claims_done(text):
    return bool(_DONE.search(text or ""))


# Сроки на Stop, с: таймаут хука в hooks.json — 120 с; git rev-parse — до 5 с, сверка со снимком — до
# SNAPSHOT_BUDGET, строки комментариев — до COMMENTS_BUDGET, судья — до 65 с.
SNAPSHOT_BUDGET = 20
COMMENTS_BUDGET = 20


def changed_this_turn(data):
    """Корень проекта, HEAD корня и подмодулей на старте реплики и (путь, класс по common.path_kind, есть ли
    файл сейчас) со снимка текущей реплики.

    None — нет cwd или снимка, снимок другой реплики или другого корня, изменения со снимка не определить
    (snapshot.changed_since) или сверка не уложилась в SNAPSHOT_BUDGET; в двух последних случаях —
    с предупреждением.
    """
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    snap = snapshot.load(common.data_dir() / "state", data.get("session_id", ""))
    if snap is None or snap.get("prompt_id") != data.get("prompt_id", ""):
        return None
    root = common.project_root(cwd)
    if str(root) != snap.get("root"):
        return None
    try:
        found = snapshot.changed_since(root, snap, time.monotonic() + SNAPSHOT_BUDGET)
    except TimeoutError as e:
        common.warn(f"сверка документации не проверена: {e}")
        return None
    if found is None:
        return None
    changed = [(p, common.path_kind(p), exists) for p, exists in found]
    return root, (snap.get("head"), snap.get("sub_heads") or {}), changed


def docs_check(data):
    """Аргументы prompts.render_docs_content после сообщения: (изменённые файлы, строки комментариев, обрезано ли,
    нет ли CLAUDE.md в корне, файлы без известного синтаксиса, файлы, не разобранные к сроку), если со снимка
    текущей реплики изменился файл кода; иначе None. Ошибка — None с предупреждением."""
    try:
        info = changed_this_turn(data)
        if info is None or not any(kind == "code" for _, kind, _ in info[2]):
            return None
        root, (base, sub_bases), changed = info
        lines, truncated, unknown, late = comments.extract(
            root, [p for p, kind, exists in changed if kind == "code" and exists], base, sub_bases,
            time.monotonic() + COMMENTS_BUDGET)
        return changed, lines, truncated, not (root / "CLAUDE.md").exists(), unknown, late
    except Exception as e:
        common.warn(f"сверка документации не проверена: {e!r}")
        return None


def turn_messages(data, transcript, message):
    """Сообщения агента за реплику по порядку, последним — message (last_assistant_message входа: транскрипт к
    Stop может его ещё не содержать). Реплики в транскрипте нет — только message, с предупреждением раз на
    сессию."""
    found = list(transcript.turn_messages)
    if not found and message:
        session = data.get("session_id", "")
        common.warn_once(session if isinstance(session, str) else "", "turn-messages",
                         "сообщения реплики не найдены в транскрипте, судья видит последнее сообщение")
    if message and (not found or found[-1].strip() != message.strip()):
        found.append(message)
    return found


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if not data:
        return
    message = data.get("last_assistant_message") or ""
    # Фильтры «варианты» и «готово» — по последнему сообщению; судья видит всю реплику.
    options = looks_like_options(message)
    done = claims_done(message)
    docs_info = docs_check(data)
    docs = docs_info is not None
    if not (options or done or docs):
        return
    filters = [name for flag, name in ((options, "options"), (done, "done"), (docs, "docs")) if flag]
    meta = {"filters": filters}
    if docs:
        meta["files"] = [p for p, _, _ in docs_info[0][:prompts.MAX_LISTED]]
    session = data.get("session_id", "")
    prompt_id = data.get("prompt_id", "")
    if not common.deny_budget_left(session, prompt_id, "stop"):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event("stop", session, verdict="budget", **meta)
        return
    modules = []
    if done:
        modules.append("verification")
    if docs:
        modules += ["docs", "comments", "design-patterns", "refactoring"]
    rubric = common.rubric(("Решения",) if options else (), tuple(modules))
    if rubric is None:
        # Предупреждение уже выдал common.rubric.
        common.log_event("stop", session, verdict="skipped", error="нет раздела рубрики", **meta)
        return
    transcript = common.read_transcript(data.get("transcript_path"))
    content = prompts.turn_content(turn_messages(data, transcript, message))
    if docs:
        content = prompts.render_docs_content(content, *docs_info)
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT,
                               prompts.stop_prompt(rubric, content, options=options, done=done, docs=docs,
                                                   label=prompts.TURN_LABEL),
                               common.judge_model(data, transcript))
    duration_ms = int((time.monotonic() - started) * 1000)
    if verdict.error:
        common.warn(common.skip_message(verdict))
        common.log_event("stop", session, verdict="skipped", error=verdict.error,
                         duration_ms=duration_ms, content=content, **meta)
        return
    if verdict.ok:
        common.log_event("stop", session, verdict="ok", duration_ms=duration_ms, content=content, **meta)
        return
    log = dict(reason=verdict.reason, violated=verdict.violated, duration_ms=duration_ms, content=content, **meta)
    # Счётчик на пределе к моменту отказа — пропуск с предупреждением вместо отказа.
    if common.deny_budget_exhausted(session, prompt_id, "stop"):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event("stop", session, verdict="budget", **log)
        return
    violated = f" (нарушено: {', '.join(verdict.violated)})" if verdict.violated else ""
    # Ответ запоминается до записи журнала: сбой записи не отменяет отказ.
    common.emit(common.block_output(f"planka: {verdict.reason}{violated}"))
    common.log_event("stop", session, verdict="deny", **log)


if __name__ == "__main__":
    common.run_hook(main)
