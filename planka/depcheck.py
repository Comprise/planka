"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import re
import shlex

DEP_OK_MARKER = "PLANKA_DEP_OK=1"

_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PIP_REQ_FLAGS = {"-r", "--requirement"}

# Флаги, чьё следующее слово — значение, а не пакет; наборы по менеджерам.
_NPM_VALUE_FLAGS = {"--prefix", "-C", "--dir", "--filter", "-w", "--workspace", "--registry", "--tag", "--cache"}
_PNPM_VALUE_FLAGS = {"--prefix", "-C", "--dir", "--filter", "--registry"}
_YARN_VALUE_FLAGS = {"--cwd", "--registry"}
_PIP_VALUE_FLAGS = {
    "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
    "-t", "--target", "--prefix", "--root", "-e", "--editable", "--platform",
    "--python-version", "--implementation", "--abi",
}
_UV_ADD_VALUE_FLAGS = {"-r", "--requirements", "--index", "--default-index", "--python", "--group", "--extra"}
_CARGO_VALUE_FLAGS = {"--path", "--git", "--registry", "--branch", "--tag", "--rev", "--features"}
_POETRY_VALUE_FLAGS = {"--group", "--source", "--extras", "-E"}
_BUNDLE_VALUE_FLAGS = {"--version", "--source", "--group"}
_GEM_VALUE_FLAGS = {"-v", "--version", "--source"}
_COMPOSER_VALUE_FLAGS = {"--with"}
_NO_VALUE_FLAGS = frozenset()
_ADD_FLAGS = {"poetry": _POETRY_VALUE_FLAGS, "cargo": _CARGO_VALUE_FLAGS, "bundle": _BUNDLE_VALUE_FLAGS}


def _words(segment):
    try:
        words = shlex.split(segment, posix=True, comments=True)
    except ValueError:
        # Незакрытая кавычка: комментарий отсекается по первому " #" или ведущему "#".
        if segment.startswith("#"):
            segment = ""
        segment = segment.split(" #", 1)[0]
        words = segment.split()
    while words and (_ENV_ASSIGN.match(words[0]) or words[0] == "sudo"):
        words.pop(0)
    return words


def _positional(args, value_flags=_NO_VALUE_FLAGS):
    """Первое слово-аргумент, похожее на имя пакета: не флаг, не значение флага, не локальный путь."""
    skip = False
    for w in args:
        if skip:
            skip = False
            continue
        if w.startswith("-"):
            skip = w in value_flags
            continue
        if not w or w.startswith((".", "/", "~", "file:")):
            continue
        return w
    return None


def _pip_add(args):
    if _PIP_REQ_FLAGS & set(args):
        return False
    return _positional(args, _PIP_VALUE_FLAGS) is not None


def _is_add(words):
    if not words:
        return False
    w = words
    if w[0] == "go" and len(w) > 1 and w[1] == "get":
        return _positional(w[2:]) is not None
    if w[0] == "npm" and len(w) > 1 and w[1] in ("install", "i", "add"):
        return _positional(w[2:], _NPM_VALUE_FLAGS) is not None
    if w[0] == "pnpm" and len(w) > 1 and w[1] in ("add", "install", "i"):
        return _positional(w[2:], _PNPM_VALUE_FLAGS) is not None
    if w[0] == "yarn" and len(w) > 1 and w[1] == "add":
        return _positional(w[2:], _YARN_VALUE_FLAGS) is not None
    if w[0] in ("pip", "pip3") and len(w) > 1 and w[1] == "install":
        return _pip_add(w[2:])
    if w[0].startswith("python") and len(w) > 3 and w[1] == "-m" and w[2] == "pip" and w[3] == "install":
        return _pip_add(w[4:])
    if w[0] == "uv" and len(w) > 1:
        if w[1] == "add":
            return _positional(w[2:], _UV_ADD_VALUE_FLAGS) is not None
        if w[1] == "pip" and len(w) > 2 and w[2] == "install":
            return _pip_add(w[3:])
    if w[0] in _ADD_FLAGS and len(w) > 1 and w[1] == "add":
        return _positional(w[2:], _ADD_FLAGS[w[0]]) is not None
    if w[0] == "gem" and len(w) > 1 and w[1] == "install":
        return _positional(w[2:], _GEM_VALUE_FLAGS) is not None
    if w[0] == "composer" and len(w) > 1 and w[1] == "require":
        return _positional(w[2:], _COMPOSER_VALUE_FLAGS) is not None
    return False


def _strip_comment(line):
    """Строка без неэкранированного и не взятого в кавычки `#`-хвоста (`#` — в начале или после пробела)."""
    quote = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
        i += 1
    return line


def dependency_add(command):
    """Сегмент команды, добавляющий пакет; None, если такого нет или стоит маркер согласия."""
    if not isinstance(command, str) or DEP_OK_MARKER in command:
        return None
    for line in command.split("\n"):
        for segment in _SPLIT.split(_strip_comment(line)):
            segment = segment.strip()
            if segment and _is_add(_words(segment)):
                return segment
    return None
