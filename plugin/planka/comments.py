"""Извлечение строк комментариев из изменённых файлов для судьи."""
import os
import re
import subprocess

import depcheck

MAX_LINES = 300
MAX_BYTES = 16_384

_C_FAMILY = {"go", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "java", "kt", "kts", "swift",
             "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "dart", "rs", "scala", "m", "mm", "cs",
             "php", "groovy", "gradle", "proto", "sol", "zig"}
_HASH = {"py", "pyi", "sh", "bash", "zsh", "rb", "pl", "toml", "yaml", "yml", "mk", "makefile", "cmake", "cfg",
         "ini", "ps1", "tf", "nix", "r", "jl", "ex", "exs"}
# Файлы с синтаксисом «#», узнаваемые по имени: ключ семейства — имя в нижнем регистре.
_HASH_NAMES = {"Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "Dockerfile", "Justfile", "Rakefile",
               "Gemfile"}
_DASH = {"sql", "lua", "hs"}
_HTML = {"html", "xml", "vue", "svelte"}
# Файлы разметки со скриптами: кроме <!-- -->, комментарии C-семейства.
_SCRIPT_HTML = {"vue", "svelte"}
_DOCSTRING = {"py", "pyi"}
_SHELL = {"sh", "bash", "zsh"}
# «#» начинает комментарий только в начале слова.
_WORD_START_HASH = _SHELL | {"yaml", "yml"}
_DOCSTRING_OPEN = re.compile(r"[rRuU]?(\"\"\"|''')")
_LUA_BLOCK = re.compile(r"--\[(=*)\[")
_HASH_KEYS = _HASH | {n.lower() for n in _HASH_NAMES}
_KNOWN = _C_FAMILY | _HASH_KEYS | _DASH | _HTML
# Строковые литералы: raw-строка Rust r#"…"#, обратные кавычки (шаблон JS, raw-строка Go), "…", '…'.
_STRING = re.compile(r'r(#+)".*?"\1|`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def _strip_strings(line):
    """Содержимое строковых литералов заменяется пробелами: маркеры внутри строковых литералов не считаются."""
    return _STRING.sub(lambda m: " " * len(m.group(0)), line)


def _c_comment(line, probe):
    """Комментарий C-семейства в строке: (текст или None, закрывающий маркер блока, открытого строкой, или None)."""
    i = probe.find("//")
    j = probe.find("/*")
    if 0 <= i and (j < 0 or i < j):
        return line[i:], None
    if j >= 0:
        return line[j:], None if "*/" in probe[j + 2:] else "*/"
    return None, None


def comment_lines(text, ext):
    ext = ext.lower()
    out = []
    # Открытый многострочный блок: (закрывающий маркер, идут ли его строки в вывод).
    in_block = None
    # Терминаторы heredoc в shell: тело heredoc — данные.
    pending = []
    for raw in text.split("\n"):
        if pending:
            term, strip_tabs = pending[0]
            if (raw.lstrip("\t") if strip_tabs else raw) == term:
                pending.pop(0)
            continue
        line = raw.strip()
        if not line:
            continue
        if in_block:
            if in_block[1]:
                out.append(line)
            if in_block[0] in line:
                in_block = None
            continue
        probe = _strip_strings(line)
        if ext in _C_FAMILY or ext in _SCRIPT_HTML and "<!--" not in probe:
            found, close = _c_comment(line, probe)
            if found is not None:
                out.append(found)
            if close:
                in_block = (close, True)
        elif ext in _HASH_KEYS:
            i = _hash_start(probe, ext in _WORD_START_HASH)
            if i >= 0 and not line.startswith("#!"):
                out.append(line[i:])
            if ext in _DOCSTRING:
                # Docstring — строка, которая начинается с тройных кавычек; тройные кавычки дальше
                # в коде строки открывают строковый литерал, его строки пропускаются.
                code = line[:i] if i >= 0 else line
                m = _DOCSTRING_OPEN.match(code)
                for q in ('"""', "'''"):
                    k = code.find(q)
                    if k >= 0:
                        if m:
                            out.append(line)
                        if line.count(q) == 1:
                            in_block = (q, bool(m))
                        break
            if ext in _SHELL:
                pending = depcheck.heredocs(raw)
        elif ext in _DASH:
            i = probe.find("--")
            j = probe.find("{-") if ext == "hs" else -1
            lua = _LUA_BLOCK.match(probe, i) if ext == "lua" and i >= 0 else None
            if j >= 0 and (i < 0 or j < i):
                out.append(line[j:])
                if "-}" not in probe[j + 2:]:
                    in_block = ("-}", True)
            elif lua:
                out.append(line[i:])
                close = "]" + lua.group(1) + "]"
                if close not in probe[lua.end():]:
                    in_block = (close, True)
            elif i >= 0:
                out.append(line[i:])
        elif ext in _HTML:
            i = probe.find("<!--")
            if i >= 0:
                out.append(line[i:])
                if "-->" not in probe[i:]:
                    in_block = ("-->", True)
    return out


def _hash_start(probe, shell):
    """Индекс «#», начинающего комментарий; -1, если его нет.

    «#» сразу после «$» или «{» ($#, ${#a[@]}) — код; в shell «#» начинает комментарий только в начале
    строки или после пробела (${var#prefix} — код).
    """
    i = probe.find("#")
    while i >= 0:
        if i == 0 or (probe[i - 1].isspace() if shell else probe[i - 1] not in "${"):
            return i
        i = probe.find("#", i + 1)
    return -1


def _ext(relpath):
    name = relpath.rsplit("/", 1)[-1]
    if name in _HASH_NAMES:
        return name.lower()
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _git(root, *args):
    try:
        proc = subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", "--literal-pathspecs", *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


_OCTAL = re.compile(r"[0-7]{3}")
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(path):
    """Путь из заголовка diff: снимает C-кавычки git и табуляцию, которую git ставит после имени с пробелом."""
    path = path.rstrip("\t")
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 == len(body):
            out += c.encode("utf-8")
            i += 1
        elif _OCTAL.fullmatch(body[i + 1:i + 4]):
            out.append(int(body[i + 1:i + 4], 8) & 0xFF)
            i += 4
        elif body[i + 1] in _ESCAPES:
            out.append(_ESCAPES[body[i + 1]])
            i += 2
        else:
            # Неизвестный escape берётся буквально, вместе с обратной косой.
            out += body[i:i + 2].encode("utf-8")
            i += 2
    return out.decode("utf-8", errors="replace")


def _added_lines(root, relpaths, base):
    """{путь: строки, добавленные против коммита base} по одному git diff на все пути; None, если diff не
    получен."""
    out = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--relative", "--src-prefix=a/", "--dst-prefix=b/",
               "-U0", base, "--", *relpaths)
    if out is None:
        return None
    added, current, in_header = {}, None, False
    for line in out.split("\n"):
        if line.startswith("diff --git "):
            current, in_header = None, True
        elif in_header and line.startswith("+++ "):
            target = line[4:]
            current = None if target == "/dev/null" else _unquote(target)[2:]
        elif line.startswith("@@"):
            in_header = False
        elif not in_header and current is not None and line.startswith("+"):
            added.setdefault(current, []).append(line[1:])
    return {path: "\n".join(lines) for path, lines in added.items()}


def _read(root, relpath):
    try:
        with open(os.path.join(root, relpath), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _sources(root, relpaths, base, sub_bases):
    """{путь: текст к проверке}: строки отслеживаемого файла, добавленные против коммита base; весь файл —
    для неотслеживаемого, вне git, без base и при недоступном diff. Файл подмодуля — против sub_bases[путь
    подмодуля] (HEAD подмодуля на старте реплики), без записи — против текущего HEAD подмодуля."""
    if not relpaths:
        return {}
    if base is None:
        return {p: _read(root, p) for p in relpaths}
    # Индекс: режим и путь каждой записи; режим 160000 — подмодуль.
    stage = [e.partition("\t") for e in (_git(root, "ls-files", "-s", "-z") or "").split("\0") if e]
    subs = [path for meta, _, path in stage if meta.startswith("160000 ")]
    listed_set = {path for meta, _, path in stage if not meta.startswith("160000 ")}
    # Файл подмодуля берётся из диффа самого подмодуля.
    result = {}
    for sub in subs:
        inside = [p for p in relpaths if p.startswith(sub + "/")]
        if inside:
            nested = {k[len(sub) + 1:]: v for k, v in sub_bases.items() if k.startswith(sub + "/")}
            got = _sources(os.path.join(root, sub), [p[len(sub) + 1:] for p in inside],
                           sub_bases.get(sub, "HEAD"), nested)
            result.update({f"{sub}/{p}": text for p, text in got.items()})
    relpaths = [p for p in relpaths if p not in result]
    if not relpaths:
        return result
    tracked = [p for p in relpaths if p in listed_set]
    added = _added_lines(root, tracked, base) if tracked else {}
    if added is None:
        result.update({p: _read(root, p) for p in relpaths})
        return result
    tracked_set = set(tracked)
    result.update({p: added.get(p, "") if p in tracked_set else _read(root, p) for p in relpaths})
    return result


def extract(root, relpaths, base="HEAD", sub_bases=None):
    """(строки комментариев «путь: строка», обрезано ли, отсортированные пути без известного синтаксиса).

    base — коммит, против которого берутся добавленные строки (HEAD на старте реплики: коммит в ходе
    реплики их не прячет); None — репозиторий без коммитов, файлы берутся целиком. sub_bases — то же для
    подмодулей: {путь подмодуля: коммит}.
    """
    unknown = sorted(p for p in relpaths if _ext(p) not in _KNOWN)
    known = [p for p in relpaths if _ext(p) in _KNOWN]
    sources = _sources(root, known, base, sub_bases or {})
    lines, size = [], 0
    for rel in known:
        for c in comment_lines(sources[rel], _ext(rel)):
            entry = f"{rel}: {c}"
            if len(lines) >= MAX_LINES or size + len(entry.encode("utf-8")) > MAX_BYTES:
                return lines, True, unknown
            lines.append(entry)
            size += len(entry.encode("utf-8"))
    return lines, False, unknown
