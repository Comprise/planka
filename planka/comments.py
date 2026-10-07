"""Извлечение строк комментариев из изменённых файлов для судьи."""
import os
import re
import subprocess

MAX_LINES = 300
MAX_BYTES = 16_384

_C_FAMILY = {"go", "c", "h", "cc", "cpp", "hpp", "java", "kt", "kts", "swift", "js", "jsx", "ts", "tsx",
             "dart", "rs", "scala", "m", "mm", "cs"}
_HASH = {"py", "sh", "bash", "zsh", "rb", "pl", "toml", "yaml", "yml", "mk", "makefile", "cfg", "ini", "ps1"}
_DASH = {"sql", "lua", "hs"}
_HTML = {"html", "xml", "vue", "svelte"}
_DOCSTRING = {"py"}
_KNOWN = _C_FAMILY | _HASH | _DASH | _HTML


def _strip_strings(line):
    """Содержимое строковых литералов заменяется пробелами: маркеры внутри строковых литералов не считаются."""
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', lambda m: " " * len(m.group(0)), line)


def comment_lines(text, ext):
    ext = ext.lower()
    out = []
    in_block = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if in_block:
            out.append(line)
            if in_block in line:
                in_block = None
            continue
        probe = _strip_strings(line)
        if ext in _C_FAMILY:
            i = probe.find("//")
            j = probe.find("/*")
            if 0 <= i and (j < 0 or i < j):
                out.append(line[i:])
            elif j >= 0:
                out.append(line[j:])
                if "*/" not in probe[j:]:
                    in_block = "*/"
        elif ext in _HASH:
            i = _hash_start(probe)
            if i >= 0 and not line.startswith("#!"):
                out.append(line[i:])
            if ext in _DOCSTRING:
                for q in ('"""', "'''"):
                    k = line.find(q)
                    if k >= 0:
                        out.append(line[k:])
                        if line.count(q) == 1:
                            in_block = q
                        break
        elif ext in _DASH:
            i = probe.find("--")
            if i >= 0:
                out.append(line[i:])
        elif ext in _HTML:
            i = probe.find("<!--")
            if i >= 0:
                out.append(line[i:])
                if "-->" not in probe[i:]:
                    in_block = "-->"
    return out


def _hash_start(probe):
    # Индекс «#», начинающего комментарий; «#» сразу после «$» или «{» ($#, ${#a[@]}) — код.
    i = probe.find("#")
    while i >= 0:
        if i == 0 or probe[i - 1] not in "${":
            return i
        i = probe.find("#", i + 1)
    return -1


def _ext(relpath):
    name = relpath.rsplit("/", 1)[-1]
    if name == "Makefile":
        return "makefile"
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


def _added_lines(root, relpaths):
    """{путь: добавленный текст} по одному git diff на все пути; None, если diff не получен."""
    out = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--relative", "--src-prefix=a/", "--dst-prefix=b/",
               "-U0", "HEAD", "--", *relpaths)
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


def _sources(root, relpaths):
    """{путь: текст к проверке}: добавленные строки отслеживаемого файла; весь файл — для неотслеживаемого,
    вне git и при недоступном diff (в том числе в репозитории без HEAD)."""
    if not relpaths:
        return {}
    listed = _git(root, "ls-files", "-z", "--", *relpaths)
    listed_set = set(listed.split("\0")) if listed else set()
    tracked = [p for p in relpaths if p in listed_set]
    added = _added_lines(root, tracked) if tracked else {}
    if added is None:
        return {p: _read(root, p) for p in relpaths}
    tracked_set = set(tracked)
    return {p: added.get(p, "") if p in tracked_set else _read(root, p) for p in relpaths}


def extract(root, relpaths):
    """(строки комментариев «путь: строка», обрезано ли, отсортированные пути без известного синтаксиса)."""
    unknown = sorted(p for p in relpaths if _ext(p) not in _KNOWN)
    known = [p for p in relpaths if _ext(p) in _KNOWN]
    sources = _sources(root, known)
    lines, size = [], 0
    for rel in known:
        for c in comment_lines(sources[rel], _ext(rel)):
            entry = f"{rel}: {c}"
            if len(lines) >= MAX_LINES or size + len(entry.encode("utf-8")) > MAX_BYTES:
                return lines, True, unknown
            lines.append(entry)
            size += len(entry.encode("utf-8"))
    return lines, False, unknown
