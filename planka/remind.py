"""UserPromptSubmit: подмешивает ядро, снимает снимок дерева, напоминает об инициализации документации."""
import os

import common
import snapshot

NO_DOCS_LINE = "Проект без документации: предложи автору инициализацию по rules/docs.md."


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if data is None:
        return
    text = common.philosophy_text()
    if text is None:
        return
    session = data.get("session_id", "")
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        root = common.project_root(cwd)
        if not (root / "CLAUDE.md").exists():
            text += "\n\n" + NO_DOCS_LINE
        test_limit = os.environ.get("PLANKA_TEST_MAX_FILES")
        if test_limit:
            # Тестовый крючок: порог снимка уменьшается, чтобы проверить ветку отказа.
            snapshot.MAX_FILES = int(test_limit)
        files = snapshot.scan(root)
        if files is None:
            common.warn_once(session, "snapshot",
                             f"дерево больше {snapshot.DEFAULT_MAX_FILES} файлов, сверка документации не проверяется")
        else:
            state_dir = common.data_dir() / "state"
            state_dir.mkdir(exist_ok=True)
            snapshot.save(state_dir, session, data.get("prompt_id", ""), root, files)
    s = common.settings()
    if not (s["comment_lang"] and s["doc_lang"]):
        common.warn_once(session, "lang",
                         "задайте comment_lang и doc_lang: claude plugin configure planka --values-stdin")
    common.emit(common.context_output(text))


if __name__ == "__main__":
    common.run_hook(main)
