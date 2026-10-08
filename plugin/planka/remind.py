"""UserPromptSubmit: подмешивает ядро частью с номером из аргумента, снимает снимок дерева, напоминает об
инициализации документации."""
import re
import sys
import time

import common
import snapshot

# Срок снимка с его упаковкой от старта хука, с: таймаут части 1 в hooks.json — 10 с, напоминание выводится после
# снимка; запись готового файла — в оставшемся запасе.
SNAPSHOT_BUDGET = 7

NO_DOCS_LINE = "Проект без документации: пожалуйста, предложите автору инициализацию по {RULES}/docs.md."

# Предел одной строки additionalContext: длиннее Claude Code сохраняет в файл и отдаёт агенту путь и первые
# 2 000 символов, каждую строку меряя отдельно (code.claude.com/docs/en/hooks.md, «capped at 10,000 characters»).
# Ядро уходит частями, каждая не длиннее CONTEXT_LIMIT и отдельным хуком.
CONTEXT_LIMIT = 10_000
# Число частей ядра; hooks.json зовёт remind.py с номерами 1..PARTS (сверяет tests/test_contract.py).
PARTS = 2
# Заголовок частей после первой: хуки события идут параллельно, и порядок их ответов не гарантирован.
CONTINUATION = ("# Философия работы — продолжение, часть {k} из {n}\n\n"
                "Ядро «Философия работы» приходит {n} частями в любом порядке; вместе они — одно ядро.")

_SECTION = re.compile(r"^(?=## )", re.MULTILINE)


def split_core(text, n):
    """n кусков text по границам разделов «## » подряд: склейка кусков — text, раздел не рвётся, наибольший
    кусок — наименьший из возможных. Разделов меньше n — лишние куски пустые."""
    sections = [s for s in _SECTION.split(text) if s]
    if len(sections) <= n:
        return sections + [""] * (n - len(sections))
    sizes = [len(s) for s in sections]
    m = len(sections)
    prefix = [0]
    for size in sizes:
        prefix.append(prefix[-1] + size)
    # best[j][i] — наименьший наибольший кусок из первых i разделов в j кусках; cut[j][i] — начало j-го куска.
    inf = float("inf")
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    cut = [[0] * (m + 1) for _ in range(n + 1)]
    best[0][0] = 0
    for j in range(1, n + 1):
        for i in range(j, m + 1):
            for s in range(j - 1, i):
                cost = max(best[j - 1][s], prefix[i] - prefix[s])
                if cost < best[j][i]:
                    best[j][i], cut[j][i] = cost, s
    parts, i = [], m
    for j in range(n, 0, -1):
        s = cut[j][i]
        parts.append("".join(sections[s:i]))
        i = s
    return parts[::-1]


def core_parts(text):
    """Тексты частей ядра text по порядку, без обращения common.context_output; пустая строка — части нечего
    отдать. Первая часть начинается заголовком ядра, следующие — заголовком CONTINUATION."""
    out = []
    for k, part in enumerate(split_core(text, PARTS), 1):
        body = part.rstrip()
        if body and k > 1:
            body = CONTINUATION.format(k=k, n=PARTS) + "\n\n" + body
        out.append(body)
    return out


def part_number(args):
    """Номер части ядра из аргументов хука; None с предупреждением — номер вне 1..PARTS."""
    # Без аргумента — первая часть: вызову без номера нужен снимок дерева, а его снимает только она.
    if not args:
        return 1
    arg = args[0]
    if arg.isascii() and arg.isdigit() and 1 <= int(arg) <= PARTS:
        return int(arg)
    common.warn(f"неверный номер части ядра: {arg!r}")
    return None


def take_snapshot(session, prompt_id, root, deadline):
    """Снимок дерева root для реплики prompt_id. Снимок того же корня, который Stop не отметил проверенным (на
    прерванную автором реплику Stop не приходит), остаётся базой и перепривязывается к этой реплике. Снимок
    корня, который в этой сессии уже не удался, не повторяется."""
    if root is None:
        return
    state_dir = common.data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    if snapshot.carry_over(state_dir, session, prompt_id, root) \
            or snapshot.failure(state_dir, session, root) is not None:
        return
    try:
        snap = snapshot.capture(root, deadline)
        # Упаковка блоков walk растёт с деревом и ограничена тем же сроком deadline, что и обход.
        snapshot.store(state_dir, session, prompt_id, root, snap, deadline)
    # TimeoutError — обход, git или упаковка не уложились в deadline.
    except (snapshot.TooManyFiles, TimeoutError) as e:
        common.warn(f"{e}, сверка документации не проверяется")
        snapshot.mark_failed(state_dir, session, root, str(e))
        return
    common.prune_state(state_dir)


def main(args=()):
    if common.barrier_active():
        return
    deadline = time.monotonic() + SNAPSHOT_BUDGET
    part = part_number(args)
    if part is None:
        return
    data = common.read_input()
    if data is None:
        return
    text = common.philosophy_text()
    if text is None:
        return
    text = core_parts(text)[part - 1]
    if part == 1:
        text = remind_project(data, text, deadline)
    if not text:
        return
    out = common.context_output(text)
    # Предел меряет то, что уходит агенту, — вместе с обращением, которое добавляет context_output.
    if len(out["hookSpecificOutput"]["additionalContext"]) > CONTEXT_LIMIT:
        common.warn(f"часть {part} ядра длиннее {CONTEXT_LIMIT} символов: Claude Code отдаст агенту только её начало")
    common.emit(out)


def remind_project(data, text, deadline):
    """text с напоминанием об инициализации документации, если у проекта нет CLAUDE.md; снимок дерева проекта.
    Только в первой части: снимок — один на реплику."""
    session = data.get("session_id")
    session = session if isinstance(session, str) else ""
    cwd = data.get("cwd")
    root = None
    try:
        if isinstance(cwd, str) and cwd:
            root = common.project_root(cwd)
            if not (root / "CLAUDE.md").exists():
                text += "\n\n" + common.substitute(NO_DOCS_LINE)
    except Exception as e:
        common.warn(f"корень проекта не определён: {e!r}")
        root = None
    try:
        take_snapshot(session, data.get("prompt_id", ""), root, deadline)
    except Exception as e:
        common.warn(f"снимок дерева не записан: {e!r}")
    return text


if __name__ == "__main__":
    common.run_hook(lambda: main(sys.argv[1:]))
