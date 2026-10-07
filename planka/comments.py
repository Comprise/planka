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
_HTML = {"html", "xml", "vue", "svelte", "md"}
_DOCSTRING = {"py"}


def _strip_strings(line):
    """Содержимое строковых литералов заменяется пробелами, чтобы маркер внутри строки не считался комментарием."""
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', lambda m: " " * len(m.group(0)), line)


def comment_lines(text, ext):
    ext = ext.lower()
    out = []
    in_block = None
    for raw in text.splitlines():
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
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _source(root, relpath, in_git):
    """Строки файла, подлежащие проверке: добавленные в git diff для отслеживаемого, иначе все."""
    full = os.path.join(root, relpath)
    if in_git and _git(root, "ls-files", "--error-unmatch", "--", relpath) is not None:
        out = _git(root, "diff", "--no-color", "-U0", "HEAD", "--", relpath)
        if out is None:
            return ""
        return "\n".join(l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++"))
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def extract(root, relpaths):
    in_git = _git(root, "rev-parse", "--show-toplevel") is not None
    lines, size, truncated = [], 0, False
    for rel in relpaths:
        for c in comment_lines(_source(root, rel, in_git), _ext(rel)):
            entry = f"{rel}: {c}"
            if len(lines) >= MAX_LINES or size + len(entry.encode("utf-8")) > MAX_BYTES:
                return lines, True
            lines.append(entry)
            size += len(entry.encode("utf-8"))
    return lines, truncated
