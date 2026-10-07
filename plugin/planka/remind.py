"""UserPromptSubmit: подмешивает ядро, снимает снимок дерева, напоминает об инициализации документации."""
import time

import common
import snapshot

# Срок снимка от старта хука, с: таймаут хука в hooks.json — 10 с, напоминание выводится после снимка.
SNAPSHOT_BUDGET = 7

NO_DOCS_LINE = "Проект без документации: предложи автору инициализацию по {RULES}/docs.md."


def take_snapshot(session, prompt_id, root, deadline):
    if root is None:
        return
    try:
        snap = snapshot.capture(root, deadline)
    except snapshot.TooManyFiles as e:
        common.warn_once(session, "snapshot", f"{e}, сверка документации не проверяется")
        return
    state_dir = common.data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    snapshot.store(state_dir, session, prompt_id, root, snap)
    common.prune_state(state_dir)


def main():
    if common.barrier_active():
        return
    deadline = time.monotonic() + SNAPSHOT_BUDGET
    data = common.read_input()
    if data is None:
        return
    text = common.philosophy_text()
    if text is None:
        return
    session = data.get("session_id", "")
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
    common.emit(common.context_output(text))


if __name__ == "__main__":
    common.run_hook(main)
