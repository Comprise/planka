"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import re
import shlex

DEP_OK_MARKER = "PLANKA_DEP_OK=1"

_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PIP_REQ_FLAGS = {"-r", "--requirement"}

# Флаги, чьё следующее слово — значение, а не пакет.
_NODE_VALUE_FLAGS = {"--prefix", "-C", "--dir", "--filter", "-w", "--workspace", "--registry", "--tag", "--cache"}
_PIP_VALUE_FLAGS = {
    "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
    "-t", "--target", "--prefix", "--root", "-e", "--editable", "--platform",
    "--python-version", "--implementation", "--abi",
}
_UV_ADD_VALUE_FLAGS = {"-r", "--requirements", "--index", "--default-index", "--python", "--group", "--extra"}
_OTHER_VALUE_FLAGS = {"--path", "--git", "--registry", "--source", "--version", "-v"}
_NO_VALUE_FLAGS = frozenset()


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
        return _positional(w[2:], _NODE_VALUE_FLAGS) is not None
    if w[0] == "pnpm" and len(w) > 1 and w[1] in ("add", "install", "i"):
        return _positional(w[2:], _NODE_VALUE_FLAGS) is not None
    if w[0] == "yarn" and len(w) > 1 and w[1] == "add":
        return _positional(w[2:], _NODE_VALUE_FLAGS) is not None
    if w[0] in ("pip", "pip3") and len(w) > 1 and w[1] == "install":
        return _pip_add(w[2:])
    if w[0].startswith("python") and len(w) > 3 and w[1] == "-m" and w[2] == "pip" and w[3] == "install":
        return _pip_add(w[4:])
    if w[0] == "uv" and len(w) > 1:
        if w[1] == "add":
            return _positional(w[2:], _UV_ADD_VALUE_FLAGS) is not None
        if w[1] == "pip" and len(w) > 2 and w[2] == "install":
            return _pip_add(w[3:])
    if w[0] in ("poetry", "cargo", "bundle") and len(w) > 1 and w[1] == "add":
        return _positional(w[2:], _OTHER_VALUE_FLAGS) is not None
    if w[0] == "gem" and len(w) > 1 and w[1] == "install":
        return _positional(w[2:], _OTHER_VALUE_FLAGS) is not None
    if w[0] == "composer" and len(w) > 1 and w[1] == "require":
        return _positional(w[2:], _OTHER_VALUE_FLAGS) is not None
    return False


def dependency_add(command):
    """Сегмент команды, добавляющий пакет; None, если такого нет или стоит маркер согласия."""
    if not isinstance(command, str) or DEP_OK_MARKER in command:
        return None
    for segment in _SPLIT.split(command):
        segment = segment.strip()
        if segment and _is_add(_words(segment)):
            return segment
    return None
