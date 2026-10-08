"""PreToolUse: судья записи в постоянную память — автопамять, память субагентов, пользовательские
CLAUDE.md и rules/, MCP-память; запись проходит только с явным согласием автора на этот факт."""
import glob
import json
import os
import re
import subprocess
import sys
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
# Срок git check-ignore в _repository_file, секунды; входит в сумму срока хука (tests/test_contract.py,
# TimeoutsTest).
CHECK_IGNORE_TIMEOUT = 5


def config_dir():
    """Каталог настроек Claude Code: CLAUDE_CONFIG_DIR, без него ~/.claude."""
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def _forms(path):
    """Путь как написан (абсолютный, нормализованный) и с разрешёнными символьными ссылками."""
    path = os.path.abspath(os.path.expanduser(path))
    return {path, os.path.realpath(path)}


def managed_dir():
    """Каталог managed-настроек Claude Code: macOS — /Library/Application Support/ClaudeCode, Windows —
    C:\\Program Files\\ClaudeCode, иначе /etc/claude-code."""
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    if sys.platform == "win32":
        return "C:\\Program Files\\ClaudeCode"
    return "/etc/claude-code"


def _settings_files(config, project):
    """Файлы настроек, из которых Claude Code берёт autoMemoryDirectory: managed-settings.json и
    managed-settings.d/*.json, .claude/settings.local.json и .claude/settings.json проекта, settings.json
    каталога настроек. Настройки --settings хуку не видны."""
    managed = managed_dir()
    files = [os.path.join(managed, "managed-settings.json")]
    files += sorted(glob.glob(os.path.join(glob.escape(managed), "managed-settings.d", "*.json")))
    files += [os.path.join(project, ".claude", "settings.local.json"),
              os.path.join(project, ".claude", "settings.json"), os.path.join(config, "settings.json")]
    return files


def _auto_memory_overrides(config, project):
    """Абсолютные значения autoMemoryDirectory (~ раскрыта) из всех файлов _settings_files. Claude Code берёт
    одно по приоритету источников; здесь — все: файл без значения, с ошибкой чтения, не строкой или
    относительным путём пропускается."""
    found = []
    for path in _settings_files(config, project):
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f).get("autoMemoryDirectory")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value and os.path.isabs(os.path.expanduser(value)):
            found.append(value)
    return found


def _remote_memory_dir():
    """CLAUDE_CODE_REMOTE_MEMORY_DIR (облачная сессия): каталог, который Claude Code берёт вместо каталога
    настроек для памяти — agent-memory/, projects/<проект>/memory/ и projects/<проект>/agent-memory-local/.
    Не задан или пуст — None."""
    value = os.environ.get("CLAUDE_CODE_REMOTE_MEMORY_DIR")
    return os.path.abspath(os.path.expanduser(value)) if value else None


def _cowork_memory_override():
    """CLAUDE_COWORK_MEMORY_PATH_OVERRIDE целиком заменяет каталог автопамяти, приоритетнее настроек. Claude Code
    принимает только абсолютный путь и ~ не раскрывает: не задан, пуст или относительный — None."""
    value = os.environ.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE")
    return value if value and os.path.isabs(value) else None


def _inside(path, directory):
    return os.path.commonpath([path, directory]) == directory


def _nested_repository(path, project):
    """Между каталогом path и каталогом project (не включая project) есть каталог с .git: path — во
    вложенном репозитории, не в рабочем дереве репозитория проекта. path и project — с разрешёнными
    ссылками; path вне project — False."""
    if not _inside(path, project):
        return False
    base = os.path.dirname(path)
    while base != project and _inside(base, project):
        if os.path.lexists(os.path.join(base, ".git")):
            return True
        base = os.path.dirname(base)
    return False


def _repository_file(path, project):
    """Путь в рабочем дереве git проекта project и не исключён git проекта (.gitignore, info/exclude,
    core.excludesFile): файл репозитория, а не память — память вне репозитория (rules/memory.md).
    Отслеживаемый файл git check-ignore не считает исключённым. Путь во вложенном репозитории
    (_nested_repository) — не файл рабочего дерева проекта, хотя git проекта отвечает о нём 1. Вне
    репозитория, без git или при ошибке git — False."""
    path, project = os.path.realpath(path), os.path.realpath(project)
    if _nested_repository(path, project):
        return False
    # Каталог проекта может не существовать (CLAUDE_PROJECT_DIR удалён): git спрашивается из ближайшего
    # существующего предка.
    base = project
    while not os.path.isdir(base) and os.path.dirname(base) != base:
        base = os.path.dirname(base)
    try:
        proc = subprocess.run(["git", "-C", base, "check-ignore", "-q", "--", path],
                              capture_output=True, timeout=CHECK_IGNORE_TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    # 0 — исключён, 1 — не исключён, 128 — не репозиторий, путь вне репозитория или ошибка.
    return proc.returncode == 1


def _memory_places(project):
    """Места памяти: (файлы, каталоги *.md, каталоги проектов, каталоги памяти, каталоги autoMemoryDirectory).
    Каталоги памяти — memory/ проектов, agent-memory/ каталога настроек и .claude/agent-memory-local/ проекта
    project; при CLAUDE_CODE_REMOTE_MEMORY_DIR — его agent-memory/ (projects/ — среди каталогов проектов);
    каталоги autoMemoryDirectory — из _auto_memory_overrides и CLAUDE_COWORK_MEMORY_PATH_OVERRIDE. Каждое —
    как написано и с разрешёнными символьными ссылками (_forms); memory/ каждого проекта — и с разрешённой
    ссылкой каталога проекта projects/<проект>, и с разрешённой ссылкой самой memory/."""
    config = os.path.abspath(os.path.expanduser(config_dir()))
    files = _forms(os.path.join(config, "CLAUDE.md"))
    md_dirs = _forms(os.path.join(config, "rules"))
    projects = _forms(os.path.join(config, "projects"))
    memory_dirs, overrides = set(), set()
    for stored in glob.glob(os.path.join(glob.escape(config), "projects", "*")):
        memory = os.path.join(stored, "memory")
        if os.path.islink(stored) or os.path.islink(memory):
            memory_dirs |= _forms(memory)
    for override in _auto_memory_overrides(config, project) + [_cowork_memory_override()]:
        if override:
            overrides |= _forms(override)
    memory_dirs |= _forms(os.path.join(config, "agent-memory"))
    remote = _remote_memory_dir()
    if remote:
        memory_dirs |= _forms(os.path.join(remote, "agent-memory"))
        projects |= _forms(os.path.join(remote, "projects"))
        for stored in glob.glob(os.path.join(glob.escape(remote), "projects", "*")):
            if os.path.islink(stored):
                for name in ("memory", "agent-memory-local"):
                    memory_dirs |= _forms(os.path.join(stored, name))
    memory_dirs |= _forms(os.path.join(project, ".claude", "agent-memory-local"))
    return files, md_dirs, projects, memory_dirs, overrides


def is_memory_path(path, project):
    """Абсолютный нормализованный путь — файл автопамяти, памяти субагента, CLAUDE.md или rules/ каталога
    настроек; project — каталог проекта сессии (_project_dir). path и project — строки в кодировке файловой
    системы (target_path, _project_dir приводят пути входа через common.input_path). Путь и места памяти
    сравниваются в обеих формах (_forms): запись через символьную ссылку в каталоге настроек и запись прямо
    в её цель — запись в память. В каталоге autoMemoryDirectory или cowork, который содержит проект project
    или равен ему, файл репозитория проекта (_repository_file) — не память; в каталоге строго внутри проекта
    и вне проекта git не спрашивается, запись — память."""
    files, md_dirs, projects, memory_dirs, overrides = _memory_places(project)
    for form in _forms(path):
        if form in files:
            return True
        if any(_inside(form, d) and form != d and form.endswith(".md") for d in md_dirs):
            return True
        for directory in projects:
            if _inside(form, directory):
                parts = os.path.relpath(form, directory).split(os.sep)
                if len(parts) >= 3 and parts[1] in ("memory", "agent-memory-local"):
                    return True
        if any(_inside(form, d) and form != d for d in memory_dirs):
            return True
    forms, projects = _forms(path), _forms(project)
    hits = {d for form in forms for d in overrides if _inside(form, d) and form != d}
    if not hits:
        return False
    # Каталог памяти, содержащий проект или равный ему: файл репозитория проекта — не память. Любой другой
    # каталог памяти — память, git не спрашивается. Каталог содержит проект, если любая его форма содержит
    # любую форму проекта: ссылка на проект и сам проект — один каталог.
    if not all(any(_inside(p, f) for f in _forms(d) for p in projects) for d in hits):
        return True
    in_project = any(_inside(form, p) for form in forms for p in projects)
    return not (in_project and _repository_file(path, project))


def _input_cwd(data):
    """cwd входа хука в кодировке файловой системы (common.input_path); не строка, пусто или NUL в пути — None."""
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd or "\0" in cwd:
        return None
    return common.input_path(cwd)


def _project_dir(data):
    """Каталог проекта сессии, от которого Claude Code читает .claude/ проекта: CLAUDE_PROJECT_DIR, без
    него cwd входа хука (_input_cwd), без него — текущий каталог."""
    return os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or _input_cwd(data) or os.getcwd())


def target_path(data):
    """Абсолютный нормализованный путь цели файлового инструмента в кодировке файловой системы
    (common.input_path): ~ раскрыта, относительный путь — от cwd входа хука, символьные ссылки не разрешены.
    Нет пути — None; путь с NUL — None: такой файл инструмент не откроет, записи нет."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("notebook_path" if data.get("tool_name") == "NotebookEdit" else "file_path")
    if not isinstance(raw, str) or not raw or "\0" in raw:
        return None
    path = os.path.expanduser(common.input_path(raw))
    if not os.path.isabs(path):
        path = os.path.join(_input_cwd(data) or os.getcwd(), path)
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
    """Поле агента и текст записи не длиннее MAX_FIELD; keep_tail — обрезка с начала: конец сообщения агента
    ближе к записи. Поля автора обрезает prompts.author_context."""
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
    """Содержимое судьи: реплика автора и ответы на AskUserQuestion (prompts.author_context), последние
    сообщения агента перед репликой автора и перед записью, цель и текст записи."""
    before = transcript.message_before_author
    last = transcript.turn_messages[-1] if transcript.turn_messages else ""
    none = "(нет)"
    parts = [prompts.author_context(transcript.author_turn, transcript.author_answers), "",
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
        if path is None or not is_memory_path(path, _project_dir(data)):
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
