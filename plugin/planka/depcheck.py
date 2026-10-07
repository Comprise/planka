"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import re
import shlex

DEP_OK_MARKER = "PLANKA_DEP_OK=1"

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


# shlex.split квадратичен по длине токена; _is_add смотрит только на ведущие слова сегмента.
_WORDS_LIMIT = 4096


def _words(segment):
    segment = segment[:_WORDS_LIMIT]
    try:
        words = shlex.split(segment, posix=True, comments=True)
    except ValueError:
        # Кавычка не закрыта или обрезана лимитом: комментарий отсекается по первому " #" или ведущему "#".
        if segment.startswith("#"):
            segment = ""
        segment = segment.split(" #", 1)[0]
        words = segment.split()
    while words and (_ENV_ASSIGN.match(words[0]) or words[0] == "sudo"):
        words.pop(0)
    return words


def _positionals(args, value_flags=_NO_VALUE_FLAGS):
    """Слова-аргументы, похожие на имя пакета: не флаг, не значение флага, не локальный путь или архив."""
    skip = False
    for w in args:
        if skip:
            skip = False
            continue
        if w.startswith("-"):
            skip = w in value_flags
            continue
        if not w or w.startswith((".", "/", "~", "file:")) or w.endswith((".whl", ".tar.gz")):
            continue
        yield w


def _positional(args, value_flags=_NO_VALUE_FLAGS):
    return next(_positionals(args, value_flags), None)


# Установщик и его спутники в venv пакетов в проект не добавляют.
_PIP_BOOTSTRAP = {"pip", "setuptools", "wheel"}
# Имя проекта в требовании pip кончается на первом спецификаторе версии, extras, маркере или URL.
_PIP_NAME_END = re.compile(r"[<>=!~\[;@]")


def _pip_add(args):
    if _PIP_REQ_FLAGS & set(args):
        return False
    names = {_PIP_NAME_END.split(w, 1)[0].lower() for w in _positionals(args, _PIP_VALUE_FLAGS)}
    return bool(names - _PIP_BOOTSTRAP)


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


_WORD_END = " \t;&|<>()"


def _heredoc_word(line, i):
    """Терминатор heredoc, начинающийся с позиции i, после снятия кавычек, и позиция за ним."""
    word = []
    while i < len(line) and line[i] not in _WORD_END:
        c = line[i]
        if c in "'\"":
            end = line.find(c, i + 1)
            end = len(line) if end < 0 else end
            word.append(line[i + 1:end])
            i = end + 1
        elif c == "\\":
            word.append(line[i + 1:i + 2])
            i += 2
        else:
            word.append(c)
            i += 1
    return "".join(word), i


def _heredocs(line):
    """Heredoc, открытые строкой, по порядку: (терминатор, снимать ли ведущие табы).

    `<<` в кавычках, here-string `<<<`, сдвиг в арифметике `((…))` и комментарий не считаются;
    подстановка `$(…)` внутри двойных кавычек — снова команда.
    """
    found = []
    stack = []  # "'", '"', "(" — подстановка или подоболочка, "A" — арифметика, "a" — скобка в ней
    i = 0
    while i < len(line):
        top = stack[-1] if stack else None
        c = line[i]
        if top == "'":
            if c == "'":
                stack.pop()
            i += 1
        elif c == "\\":
            i += 2
        elif top == '"':
            if c == '"':
                stack.pop()
                i += 1
            elif line.startswith("$((", i):
                stack.append("A")
                i += 3
            elif line.startswith("$(", i):
                stack.append("(")
                i += 2
            else:
                i += 1
        elif c in "'\"":
            stack.append(c)
            i += 1
        elif top in ("A", "a"):
            if top == "A" and line.startswith("))", i):
                stack.pop()
                i += 2
            else:
                if c == "(":
                    stack.append("a")
                elif c == ")" and top == "a":
                    stack.pop()
                i += 1
        elif line.startswith("$((", i) or line.startswith("((", i):
            stack.append("A")
            i += 3 if c == "$" else 2
        elif line.startswith("$(", i) or c == "(":
            stack.append("(")
            i += 2 if c == "$" else 1
        elif c == ")":
            if top == "(":
                stack.pop()
            i += 1
        elif c == "#" and (i == 0 or line[i - 1].isspace()):
            break
        elif line.startswith("<<<", i):
            i += 3
        elif line.startswith("<<", i):
            i += 2
            strip_tabs = line.startswith("-", i)
            i += strip_tabs
            while i < len(line) and line[i] in " \t":
                i += 1
            word, i = _heredoc_word(line, i)
            if word:
                found.append((word, strip_tabs))
        else:
            i += 1
    return found


def _segments(command):
    """Сегменты команды: границы — `&&`, `||`, `;`, `|` и перевод строки вне кавычек.

    Кавычка, открытая на одной строке, продолжается на следующих; `\\` в конце строки продолжает
    сегмент. Тело heredoc — данные, а не команды: строки до терминатора не входят ни в один
    сегмент. Комментарий — `#` в начале строки или после пробела вне кавычек — до конца строки.
    """
    segments, current = [], []
    quote = None
    pending = []

    def flush():
        segments.append("".join(current))
        current.clear()

    for line in command.split("\n"):
        if pending:
            term, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == term:
                pending.pop(0)
            continue
        # Позиция, с которой строка идёт вне кавычки, перенесённой с прошлых строк.
        outside = 0 if quote is None else None
        continued = False
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if quote == "'":
                current.append(c)
                if c == "'":
                    quote = None
            elif quote == '"':
                if c == "\\" and i + 1 < n:
                    current.append(line[i:i + 2])
                    i += 2
                    continue
                current.append(c)
                if c == '"':
                    quote = None
            elif c == "\\":
                if i + 1 == n:
                    continued = True
                    break
                current.append(line[i:i + 2])
                i += 2
                continue
            elif c in "'\"":
                quote = c
                current.append(c)
            elif c == "#" and (i == 0 or line[i - 1].isspace()):
                break
            elif line.startswith(("&&", "||"), i):
                flush()
                i += 2
                continue
            elif c in ";|":
                flush()
            else:
                current.append(c)
            if outside is None and quote is None:
                outside = i + 1
            i += 1
        if quote is not None:
            current.append("\n")
        elif continued:
            current.append(" ")
        else:
            flush()
        pending = _heredocs(line[outside:]) if outside is not None else []
    flush()
    return segments


def dependency_add(command):
    """Сегмент команды, добавляющий пакет; None, если такого нет или стоит маркер согласия."""
    if not isinstance(command, str) or DEP_OK_MARKER in command:
        return None
    for segment in _segments(command):
        segment = segment.strip()
        if segment and _is_add(_words(segment)):
            return segment
    return None
