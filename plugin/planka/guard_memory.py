"""PreToolUse: судья записи в постоянную память — автопамять, пользовательские CLAUDE.md и rules/,
MCP-память; запись проходит только с явным согласием автора на этот факт."""
import glob
import json
import os
import re
import time

import common
import prompts

HOOK = "memory"
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Предел каждого поля содержимого судьи в символах.
MAX_FIELD = 8000
# Действия записи в имени MCP-инструмента памяти: создать, изменить, удалить, переименовать запись.
WRITE_VERBS = {"write", "create", "add", "update", "edit", "delete", "remove", "save", "store", "rename",
               "insert", "upsert", "append", "replace", "set", "put", "forget", "clear", "remember",
               "memorize", "memorise", "patch", "merge", "move"}
# Слова памяти в имени сервера или инструмента; слитное имя вроде openmemory — не слово памяти.
MEMORY_WORDS = {"memory", "memories", "memorize", "memorise", "remember"}


def config_dir():
    """Каталог настроек Claude Code: CLAUDE_CONFIG_DIR, без него ~/.claude."""
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def _forms(path):
    """Путь как написан (абсолютный, нормализованный) и с разрешёнными символьными ссылками."""
    path = os.path.abspath(os.path.expanduser(path))
    return {path, os.path.realpath(path)}


def _auto_memory_override(config):
    """autoMemoryDirectory из settings.json каталога настроек; нет файла, ошибка чтения или значение
    не строка — None."""
    try:
        with open(os.path.join(config, "settings.json"), encoding="utf-8") as f:
            value = json.load(f).get("autoMemoryDirectory")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _inside(path, directory):
    return os.path.commonpath([path, directory]) == directory


def _memory_places():
    """Места памяти в каталоге настроек: (файлы, каталоги *.md, каталоги проектов, каталоги памяти).
    Каждое — как написано и с разрешёнными символьными ссылками (_forms); memory/ каждого проекта — и
    с разрешённой ссылкой каталога проекта projects/<проект>, и с разрешённой ссылкой самой memory/."""
    config = os.path.abspath(os.path.expanduser(config_dir()))
    files = _forms(os.path.join(config, "CLAUDE.md"))
    md_dirs = _forms(os.path.join(config, "rules"))
    projects = _forms(os.path.join(config, "projects"))
    memory_dirs = set()
    for project in glob.glob(os.path.join(glob.escape(config), "projects", "*")):
        memory = os.path.join(project, "memory")
        if os.path.islink(project) or os.path.islink(memory):
            memory_dirs |= _forms(memory)
    override = _auto_memory_override(config)
    if override is not None and os.path.isabs(os.path.expanduser(override)):
        memory_dirs |= _forms(override)
    return files, md_dirs, projects, memory_dirs


def is_memory_path(path):
    """Абсолютный нормализованный путь — файл автопамяти, CLAUDE.md или rules/ каталога настроек. Путь и
    места памяти сравниваются в обеих формах (_forms): запись через символьную ссылку в каталоге настроек
    и запись прямо в её цель — запись в память."""
    files, md_dirs, projects, memory_dirs = _memory_places()
    for form in _forms(path):
        if form in files:
            return True
        if any(_inside(form, d) and form != d and form.endswith(".md") for d in md_dirs):
            return True
        for directory in projects:
            if _inside(form, directory):
                parts = os.path.relpath(form, directory).split(os.sep)
                if len(parts) >= 3 and parts[1] == "memory":
                    return True
        if any(_inside(form, d) and form != d for d in memory_dirs):
            return True
    return False


def target_path(data):
    """Абсолютный нормализованный путь цели файлового инструмента: ~ раскрыта, относительный путь — от cwd
    входа хука, символьные ссылки не разрешены; нет пути — None."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("notebook_path" if data.get("tool_name") == "NotebookEdit" else "file_path")
    if not isinstance(raw, str) or not raw:
        return None
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        cwd = data.get("cwd")
        path = os.path.join(cwd if isinstance(cwd, str) and cwd else os.getcwd(), path)
    return os.path.normpath(path)


def _words(name):
    """Слова имени по порядку: разделители не буквы и не цифры, границы camelCase; нижний регистр."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def _writes_memory(words):
    """Действие записи в имени инструмента относится к памяти: глагол записи до слова памяти не дальше чем
    через одно слово (delete_all_memories, save_to_memory) или сразу после него (memory_add); слово из обоих
    множеств (remember) — само себе пара. Слово памяти, за которым идёт другое существительное, а потом
    глагол (memory_usage_set_alert), — не запись в память."""
    return any(word in MEMORY_WORDS and set(words[max(0, i - 2):i + 2]) & WRITE_VERBS
               for i, word in enumerate(words))


def is_memory_mcp(tool):
    """mcp__<server>__<tool>: слово из MEMORY_WORDS в имени сервера и действие записи в имени инструмента,
    либо действие записи над словом памяти в имени инструмента (_writes_memory)."""
    parts = tool.split("__")
    if len(parts) < 3 or parts[0] != "mcp":
        return False
    words = _words(parts[-1])
    if set(_words("__".join(parts[1:-1]))) & MEMORY_WORDS:
        return bool(set(words) & WRITE_VERBS)
    return _writes_memory(words)


def _clip(text, keep_tail=False):
    if len(text) <= MAX_FIELD:
        return text
    return "… обрезано\n" + text[-MAX_FIELD:] if keep_tail else text[:MAX_FIELD] + "\n… обрезано"


def write_text(data):
    """Записываемый текст: содержимое Write, новые строки правок Edit, MultiEdit и NotebookEdit,
    аргументы MCP-инструмента JSON."""
    tool = data.get("tool_name")
    ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if tool == "Write":
        parts = [ti.get("content")]
    elif tool == "Edit":
        parts = [ti.get("new_string")]
    elif tool == "MultiEdit":
        edits = ti.get("edits")
        edits = edits if isinstance(edits, list) else []
        parts = [e.get("new_string") for e in edits if isinstance(e, dict)]
    elif tool == "NotebookEdit":
        parts = ["(удаление ячейки)" if ti.get("edit_mode") == "delete" else ti.get("new_source")]
    else:
        return common.dumps(ti)
    return "\n---\n".join(p for p in parts if isinstance(p, str))


def render_content(data, target, transcript):
    author, answers = transcript.author_turn, transcript.author_answers
    before = transcript.message_before_author
    last = transcript.turn_messages[-1] if transcript.turn_messages else ""
    none = "(нет)"
    parts = ["Реплика автора текущего хода:", _clip(author) or none, "",
             "Ответы автора на AskUserQuestion после неё:",
             "\n---\n".join(_clip(a) for a in answers) or none, "",
             "Последнее сообщение агента перед репликой автора:", _clip(before, keep_tail=True) or none, "",
             "Последнее сообщение агента перед записью:", _clip(last, keep_tail=True) or none, "",
             f"Цель записи: {target}", "Текст записи:", _clip(write_text(data)) or none]
    return "\n".join(parts)


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if not data:
        return
    tool = data.get("tool_name")
    if tool in FILE_TOOLS:
        path = target_path(data)
        if path is None or not is_memory_path(path):
            return
        target = f"файл {path} ({tool})"
    elif isinstance(tool, str) and is_memory_mcp(tool):
        target = f"MCP-инструмент {tool}"
    else:
        return
    session, prompt_id = data.get("session_id", ""), data.get("prompt_id", "")
    if not common.deny_budget_left(session, prompt_id, HOOK):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event(HOOK, session, verdict="budget", tool=tool)
        return
    rubric = common.rubric(("Границы",), ("memory",))
    if rubric is None:
        # Предупреждение уже выдал common.rubric.
        common.log_event(HOOK, session, verdict="skipped", error="нет раздела рубрики", tool=tool)
        return
    transcript = common.read_transcript(data.get("transcript_path"))
    content = render_content(data, target, transcript)
    started = time.monotonic()
    verdict = common.run_judge(prompts.SYSTEM_PROMPT, prompts.memory_prompt(rubric, content),
                              common.judge_model(data, transcript))
    duration_ms = int((time.monotonic() - started) * 1000)
    if verdict.error:
        common.warn(common.skip_message(verdict))
        common.log_event(HOOK, session, verdict="skipped", error=verdict.error, tool=tool,
                         duration_ms=duration_ms, content=content)
        return
    if verdict.ok:
        common.log_event(HOOK, session, verdict="ok", tool=tool, duration_ms=duration_ms, content=content)
        return
    if common.deny_budget_exhausted(session, prompt_id, HOOK):
        common.warn("лимит отказов, пропущено без проверки")
        common.log_event(HOOK, session, verdict="budget", reason=verdict.reason, violated=verdict.violated,
                         tool=tool, duration_ms=duration_ms, content=content)
        return
    violated = f" (нарушено: {', '.join(verdict.violated)})" if verdict.violated else ""
    reason = f"planka: {verdict.reason}{violated}"
    # Ответ запоминается до записи журнала: сбой записи не отменяет отказ.
    common.emit(common.deny_output(reason))
    common.log_event(HOOK, session, verdict="deny", reason=verdict.reason, violated=verdict.violated,
                     tool=tool, duration_ms=duration_ms, content=content)


if __name__ == "__main__":
    common.run_hook(main)
