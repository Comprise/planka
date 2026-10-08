"""PreToolUse: судья записи в постоянную память — автопамять, память субагентов, пользовательские
CLAUDE.md и rules/, CLAUDE.local.md проекта, файлы, которые память подключает импортом @путь, MCP-память;
запись проходит только с явным согласием автора на этот факт."""
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
# Личные инструкции проекта: Claude Code читает их из каталога запуска, каталогов над ним и подкаталогов.
LOCAL_MEMORY = "CLAUDE.local.md"
# Импорты @путь памяти — как у Claude Code: не больше четырёх переходов от файла памяти, файл больше 4 МиБ он не
# читает и его импорты не разбирает.
IMPORT_DEPTH = 4
MAX_IMPORT_FILE = 4 * 1024 * 1024
# Пределы разбора импортов на вызов хука — файлов и байт всего: время разбора до судьи ограничено и при раздутой
# памяти; сверх предела остаток не разбирается, с предупреждением.
MAX_IMPORT_FILES = 200
MAX_IMPORT_BYTES = 16 * 1024 * 1024
# Начало ограждённого блока кода Markdown: импорт в нём Claude Code не разбирает.
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
# Серия обратных кавычек: код в строке идёт от серии до следующей серии той же длины.
BACKTICKS = re.compile(r"`+")
# Импорт: @ и путь до пробела, «\ » — пробел пути. Claude Code ищет @ в начале текста узла Markdown или после
# пробела; узел начинается и после разметки (*@a.md*), поэтому здесь @ годится после любого знака, кроме буквы и
# цифры: почта a@b.md не импорт.
IMPORT = re.compile(r"(?<![^\W_])@((?:[^\s\\]|\\ )+)")
# Хвост разметки и пунктуации, который мог прилипнуть к пути: путь берётся и с ним, и без него.
IMPORT_TAIL = "*_~)]>.,;:!?\"'"


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
    """Абсолютные значения autoMemoryDirectory из всех файлов _settings_files в кодировке файловой системы
    (common.input_path). Claude Code берёт одно по приоритету источников; здесь — все: файл без значения, с
    ошибкой чтения, не с объектом JSON, значение не строкой, относительным путём (~ раскрывается при проверке),
    с NUL или не кодируемое в файловую систему (одиночный суррогат) пропускается."""
    found = []
    for path in _settings_files(config, project):
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f).get("autoMemoryDirectory")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(value, str) or not value or "\0" in value:
            continue
        value = common.input_path(value)
        try:
            os.fsencode(value)
        except UnicodeEncodeError:
            continue
        if os.path.isabs(os.path.expanduser(value)):
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
    (_nested_repository) — не файл рабочего дерева проекта, хотя git проекта отвечает о нём 1. Репозиторий
    проекта — тот, что дал корень common.project_root: git ищет его не выше корня (common.git_env), и
    репозиторий dotfiles в домашнем каталоге над проектом без своего git не отвечает. Корня нет на диске,
    вне репозитория, без git или при ошибке git — False."""
    path, project = os.path.realpath(path), os.path.realpath(project)
    if _nested_repository(path, project):
        return False
    root = common.project_root(project)
    if not os.path.isdir(root):
        return False
    # Каталога проекта может не быть (CLAUDE_PROJECT_DIR удалён), корень — вершина репозитория над ним: git
    # спрашивается из ближайшего существующего предка проекта, он под корнем.
    base = project
    while not os.path.isdir(base) and os.path.dirname(base) != base:
        base = os.path.dirname(base)
    try:
        proc = subprocess.run(["git", "-C", base, "check-ignore", "-q", "--", path],
                              capture_output=True, timeout=CHECK_IGNORE_TIMEOUT, env=common.git_env(root))
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


def _strip_code(text):
    """Текст Markdown без ограждённых блоков кода (незакрытый — до конца текста), кода в строке и комментариев
    HTML: импорты в них Claude Code не разбирает."""
    kept, fence = [], None
    for line in text.splitlines():
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = None
            continue
        match = FENCE.match(line)
        if match:
            fence = match.group(1)
            continue
        kept.append(line)
    return _strip_code_spans(_strip_html_comments("\n".join(kept)))


def _strip_html_comments(text):
    """Текст без комментариев HTML <!-- … -->, каждый заменён пробелом. Незакрытый остаётся текстом, и за ним
    закрытых нет: поиск закрытия от него дошёл до конца текста."""
    parts, pos = [], 0
    while True:
        start = text.find("<!--", pos)
        end = text.find("-->", start + 4) if start >= 0 else -1
        if end < 0:
            parts.append(text[pos:])
            return " ".join(parts)
        parts.append(text[pos:start])
        pos = end + 3


def _strip_code_spans(text):
    """Текст без кода в строке, каждый заменён пробелом: серия обратных кавычек открывает код, следующая серия той
    же длины закрывает; серия без пары остаётся текстом. Пара каждой серии находится одним обходом с конца."""
    runs = [(m.start(), m.end()) for m in BACKTICKS.finditer(text)]
    pair, last = [None] * len(runs), {}
    for i in range(len(runs) - 1, -1, -1):
        length = runs[i][1] - runs[i][0]
        pair[i] = last.get(length)
        last[length] = i
    parts, pos, i = [], 0, 0
    while i < len(runs):
        if pair[i] is None:
            i += 1
            continue
        parts.append(text[pos:runs[i][0]])
        pos = runs[pair[i]][1]
        i = pair[i] + 1
    parts.append(text[pos:])
    return " ".join(parts)


def _accepted_import(spec):
    """Путь импорта, который Claude Code принимает: ./, ~/, абсолютный кроме «/», иначе начало с буквы, цифры, «.»,
    «_» или «-»."""
    return (spec.startswith(("./", "~/")) or (spec.startswith("/") and spec != "/")
            or bool(re.match(r"[A-Za-z0-9._-]", spec)))


def _imports(text, base):
    """Абсолютные нормализованные пути импортов @путь текста файла памяти из каталога base: «#…» отрезан, «\\ » —
    пробел, ~/ — от домашнего каталога, относительный — от base, как разрешает Claude Code."""
    found = set()
    for match in IMPORT.finditer(_strip_code(text)):
        spec = match.group(1).split("#", 1)[0].replace("\\ ", " ")
        for variant in {spec, spec.rstrip(IMPORT_TAIL)}:
            if not variant or not _accepted_import(variant):
                continue
            variant = common.input_path(variant)
            if variant.startswith("~/"):
                variant = os.path.join(os.path.expanduser("~"), variant[2:])
            found.add(os.path.normpath(os.path.join(base, variant)))
    return found


def _read_memory_file(path):
    """Байты обычного файла памяти не длиннее MAX_IMPORT_FILE; нет, не обычный файл (FIFO заблокировал бы хук),
    ошибка чтения или длиннее — None: такой файл Claude Code не читает."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_IMPORT_FILE + 1)
    except OSError:
        return None
    return data if len(data) <= MAX_IMPORT_FILE else None


def _import_roots(project):
    """Файлы памяти, импорты которых Claude Code загружает при запуске: CLAUDE.md и rules/**.md каталога настроек,
    CLAUDE.local.md проекта и каталогов над ним. CLAUDE.local.md подкаталогов Claude Code читает по требованию,
    обход всего проекта на каждый вызов хука не делается."""
    config = os.path.abspath(os.path.expanduser(config_dir()))
    roots = [os.path.join(config, "CLAUDE.md")]
    seen = set()
    for base, dirs, names in os.walk(os.path.join(config, "rules"), followlinks=True):
        # Ссылки на каталоги проходятся, цикл ссылок — нет.
        real = os.path.realpath(base)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs.sort()
        roots += [os.path.join(base, name) for name in sorted(names) if name.endswith(".md")]
    base = project
    while True:
        roots.append(os.path.join(base, LOCAL_MEMORY))
        if os.path.dirname(base) == base:
            return roots
        base = os.path.dirname(base)


def _imported_files(project):
    """Файлы, которые импортами @путь подключают файлы памяти _import_roots, прямо или через импортированные, не
    дальше IMPORT_DEPTH переходов; каждый — в формах _forms. Файл не обязан существовать: его создание тоже
    запись в память. Сверх MAX_IMPORT_FILES файлов или MAX_IMPORT_BYTES байт остаток не разбирается, с
    предупреждением."""
    found, seen = set(), set()
    level = [(root, 0) for root in _import_roots(project)]
    files = size = 0
    while level:
        following = []
        for path, depth in level:
            if depth:
                found |= _forms(path)
            real = os.path.realpath(path)
            if depth >= IMPORT_DEPTH or real in seen:
                continue
            seen.add(real)
            data = _read_memory_file(path)
            if data is None:
                continue
            files, size = files + 1, size + len(data)
            if files > MAX_IMPORT_FILES or size > MAX_IMPORT_BYTES:
                common.warn("импорты памяти не разобраны до конца: слишком много файлов, запись в остальные "
                            "импортированные файлы не проверяется")
                return found
            text = data.decode("utf-8", "surrogateescape")
            following += [(child, depth + 1) for child in sorted(_imports(text, os.path.dirname(path)))]
        level = following
    return found


def _local_memory(form, projects, fold):
    """form — CLAUDE.local.md в каталоге проекта, над ним или в его подкаталоге (любая форма проекта projects);
    fold — свёртка регистра, которой свёрнуты form и projects (is_memory_path)."""
    if os.path.basename(form) != fold(LOCAL_MEMORY):
        return False
    directory = os.path.dirname(form)
    return any(_inside(p, directory) or _inside(form, p) for p in projects)


def _case_insensitive(path):
    """Файловая система пути не различает регистр: путь или его предок находится и по имени с обращённым регистром,
    и это тот же файл (os.path.samefile). Проверяется каждый уровень: том без учёта регистра монтируется и внутрь
    чувствительного к нему."""
    base = path
    while True:
        name = os.path.basename(base)
        swapped = name.swapcase()
        if swapped != name:
            try:
                if os.path.samefile(base, os.path.join(os.path.dirname(base), swapped)):
                    return True
            except (OSError, ValueError):
                pass
        parent = os.path.dirname(base)
        if parent == base:
            return False
        base = parent


def is_memory_path(path, project):
    """Абсолютный нормализованный путь — файл автопамяти, памяти субагента, CLAUDE.md или rules/ каталога
    настроек, CLAUDE.local.md проекта или файл, который память подключает импортом (_imported_files); project —
    каталог проекта сессии (_project_dir). path и project — строки в кодировке файловой системы (target_path,
    _project_dir приводят пути входа через common.input_path). Путь и места памяти сравниваются в обеих формах
    (_forms): запись через символьную ссылку в каталоге настроек и запись прямо в её цель — запись в память. На
    файловой системе без учёта регистра (_case_insensitive) сравнение — без учёта регистра. CLAUDE.local.md и
    импортированный файл, которые оказались файлом репозитория проекта (_repository_file), — не память, как
    проектный CLAUDE.md. В каталоге autoMemoryDirectory или cowork, который содержит проект project или равен ему,
    файл репозитория проекта — не память; в каталоге строго внутри проекта и вне проекта git не спрашивается,
    запись — память."""
    fold = str.casefold if _case_insensitive(path) else str

    def folded(paths):
        return {fold(p) for p in paths}

    files, md_dirs, projects, memory_dirs, overrides = (folded(s) for s in _memory_places(project))
    forms = folded(_forms(path))
    for form in forms:
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
    projects = folded(_forms(project))
    # git спрашивается не больше одного раза: его срок входит в срок хука один раз (TimeoutsTest).
    answers = []

    def repository_file():
        if not answers:
            answers.append(_repository_file(path, project))
        return answers[0]

    if ((any(_local_memory(form, projects, fold) for form in forms) or forms & folded(_imported_files(project)))
            and not repository_file()):
        return True
    hits = {d for form in forms for d in overrides if _inside(form, d) and form != d}
    if not hits:
        return False
    # Каталог памяти, содержащий проект или равный ему: файл репозитория проекта — не память. Любой другой
    # каталог памяти — память, git не спрашивается. Каталог содержит проект, если любая его форма содержит
    # любую форму проекта: ссылка на проект и сам проект — один каталог.
    if not all(any(_inside(p, f) for f in folded(_forms(d)) for p in projects) for d in hits):
        return True
    in_project = any(_inside(form, p) for form in forms for p in projects)
    return not (in_project and repository_file())


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
    # «..» схлопывается до разрешения ссылок, как у Claude Code: он сам нормализует путь инструмента лексически
    # (path.resolve) и пишет в нормализованный, а не туда, куда «..» привело бы ядро после ссылки.
    return os.path.normpath(path)


def _words(name):
    """Слова имени по порядку: разделители не буквы и не цифры, границы camelCase, конец аббревиатуры перед
    словом (saveLTMemory), граница буквы и цифры (storeMemory2); нижний регистр."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    spaced = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", spaced)
    spaced = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", spaced)
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
    """Содержимое судьи: прежние реплики автора, текущая и ответы на AskUserQuestion (prompts.author_context),
    последние сообщения агента перед репликой автора и перед записью, цель и текст записи."""
    before = transcript.message_before_author
    last = transcript.turn_messages[-1] if transcript.turn_messages else ""
    none = "(нет)"
    parts = [prompts.author_context(transcript.author_turn, transcript.author_answers, transcript.earlier_turns), "",
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
