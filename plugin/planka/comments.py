"""Извлечение строк комментариев из изменённых файлов для судьи."""
import os
import re
import subprocess
import time

import common
import depcheck

MAX_LINES = 300
MAX_BYTES = 16_384
# Предел одного вызова git, с; общий срок задаёт deadline в extract.
GIT_TIMEOUT = 10

# Файлы, узнаваемые по имени: ключ синтаксиса — имя в нижнем регистре. JSON-манифесты — без комментариев,
# JSONC-манифесты — с «//» и «/* */».
_HASH_NAMES = {"Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "Dockerfile", "Justfile", "Rakefile",
               "Gemfile"}
_JSON_NAMES = {"package.json", "composer.json"}
_JSONC_NAMES = {"tsconfig.json", "jsconfig.json", "deno.json"}
_GOMOD_NAMES = {"go.mod"}
_NAMES = _HASH_NAMES | _JSON_NAMES | _JSONC_NAMES | _GOMOD_NAMES


class _Syntax:
    """Синтаксис комментариев и строковых литералов одного языка.

    line — (маркер, правило) строчного комментария: правило "any" — маркер везде, "word" — в начале строки
    или после пробела, "code" — не сразу после «$» и «{», "php" — «#» не перед «[», "css" — не сразу после
    «:» (url(http://…) без кавычек). blocks — (открытие,
    закрытие) блочного комментария. strings — (открытие, закрытие, многострочный ли, escape): escape "\\" —
    обратная косая, "double" — удвоенная закрывающая кавычка, "nix" — escape строк '' Nix, None — нет;
    однострочный литерал без закрывающей кавычки в той же строке литералом не считается.
    """

    def __init__(self, line=(), blocks=(), strings=(), char=False, quote_word=False, docstring=False,
                 heredoc=None, lua=False, raw=None, zig=False, shebang=False):
        self.line, self.blocks, self.char, self.quote_word = line, blocks, char, quote_word
        # Длинное открытие проверяется раньше короткого: «"""» раньше «"».
        self.strings = sorted(strings, key=lambda s: -len(s[0]))
        self.docstring, self.heredoc, self.lua, self.raw, self.zig, self.shebang = (
            docstring, heredoc, lua, raw, zig, shebang)
        firsts = {m[0] for m, _ in line} | {o[0] for o, _ in blocks} | {s[0][0] for s in strings}
        firsts |= {"'"} if char else set()
        firsts |= {"<"} if heredoc == "tf" else set()
        firsts |= {"-", "["} if lua else set()
        firsts |= {"\\"} if zig else set()
        self.starts = re.compile("[" + "".join(re.escape(c) for c in sorted(firsts)) + "]")


_DQ = ('"', '"', False, "\\")
_SQ = ("'", "'", False, "\\")
_SQ_RAW = ("'", "'", False, None)
_DQ_RAW = ('"', '"', False, None)
_BT = ("`", "`", False, "\\")
_T_DQ = ('"""', '"""', True, "\\")
_T_SQ = ("'''", "'''", True, "\\")
_T_DQ_RAW = ('"""', '"""', True, None)
_T_SQ_RAW = ("'''", "'''", True, None)
_SLASH = (("//", "any"),)
_C_BLOCK = (("/*", "*/"),)
_H = (("#", "code"),)

_SYNTAX = {}


def _add(exts, **kw):
    for ext in exts:
        _SYNTAX[ext] = _Syntax(**kw)


_add(("c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm"),
     line=_SLASH, blocks=_C_BLOCK, strings=(_DQ,), char=True, raw="cpp")
_add(("java",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _DQ), char=True)
_add(("kt", "kts", "scala"), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ_RAW, _DQ), char=True)
_add(("swift",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _DQ))
_add(("cs",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ_RAW, _DQ), char=True, raw="cs")
_add(("rs",), line=_SLASH, blocks=_C_BLOCK, strings=(('"', '"', True, "\\"),), char=True, raw="rust")
_add(("go",), line=_SLASH, blocks=_C_BLOCK, strings=(("`", "`", True, None), _DQ), char=True)
_add(("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"),
     line=_SLASH, blocks=_C_BLOCK, strings=(("`", "`", True, "\\"), _DQ, _SQ))
_add(("dart", "groovy", "gradle"), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _T_SQ, _DQ, _SQ))
_add(("php",), line=_SLASH + (("#", "php"),), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("proto", "sol"), line=_SLASH, blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("zig",), line=_SLASH, strings=(_DQ,), char=True, zig=True)
_add(("py", "pyi"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), docstring=True, shebang=True)
_add(("sh", "bash", "zsh"), line=(("#", "word"),), strings=(_DQ, _SQ_RAW), heredoc="shell", shebang=True)
_add(("rb", "pl", "r", "mk", "makefile", "cmake") + tuple(n.lower() for n in _HASH_NAMES),
     line=_H, strings=(_DQ, _SQ), quote_word=True, shebang=True)
_add(("toml",), line=_H, strings=(_T_DQ, _T_SQ_RAW, _DQ, _SQ_RAW))
_add(("yaml", "yml"), line=(("#", "word"),), strings=(_DQ, ("'", "'", False, "double")), quote_word=True)
_add(("ini", "cfg"), line=_H + ((";", "word"),), strings=(_DQ, _SQ), quote_word=True)
_add(("ps1",), line=_H, blocks=(("<#", "#>"),), strings=(_DQ_RAW, _SQ_RAW), quote_word=True)
_add(("tf",), line=_H + _SLASH, blocks=_C_BLOCK, strings=(_DQ,), heredoc="tf")
_add(("nix",), line=_H, blocks=_C_BLOCK, strings=(("''", "''", True, "nix"), ('"', '"', True, "\\")))
_add(("jl",), line=_H, blocks=(("#=", "=#"),), strings=(_T_DQ, _DQ), char=True, shebang=True)
_add(("ex", "exs"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), shebang=True)
_add(("sql",), line=(("--", "any"),), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("lua",), line=(("--", "any"),), strings=(_DQ, _SQ), lua=True)
_add(("hs",), line=(("--", "any"),), blocks=(("{-", "-}"),), strings=(_DQ,), char=True)
_add(("html", "xml"), blocks=(("<!--", "-->"),))
_add(("css",), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("scss", "sass", "less"), line=(("//", "css"),), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(_JSON_NAMES, strings=(_DQ,))
_add(_JSONC_NAMES, line=_SLASH, blocks=_C_BLOCK, strings=(_DQ,))
_add(_GOMOD_NAMES, line=_SLASH, strings=(_DQ, ("`", "`", False, None)))
_add(("vue", "svelte"), line=_SLASH, blocks=(("<!--", "-->"),) + _C_BLOCK, strings=(_DQ, _SQ, _BT),
     quote_word=True)

_KNOWN = set(_SYNTAX)
_LUA_BLOCK = re.compile(r"--\[(=*)\[")
_LUA_LONG = re.compile(r"\[(=*)\[")
_CPP_RAW = re.compile(r'"([^()\\\s]{0,16})\(')
_TF_HEREDOC = re.compile(r"<<(-?)([A-Za-z_][\w-]*)")
# Символьный литерал: один символ или escape; «'a» без закрывающей кавычки — время жизни Rust, штрих Haskell.
_CHAR = re.compile(r"'(?:[^'\\]|\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|[0-7]{1,3}|.))'")


def _is_word(c):
    return c.isalnum() or c == "_"


def _marker_ok(raw, i, rule):
    if rule == "word":
        return i == 0 or raw[i - 1].isspace()
    if rule == "code":
        return i == 0 or raw[i - 1] not in "${"
    if rule == "php":
        return not raw.startswith("[", i + 1)
    if rule == "css":
        return i == 0 or raw[i - 1] != ":"
    return True


def _close(raw, i, close, esc):
    """Индекс за маркером close, первым от i с учётом escape; -1, если в строке его нет. Время — линейное
    по длине строки."""
    j = -1
    while True:
        # Найденный j остаётся первым маркером от i, пока i до него не дошёл.
        if j < i:
            j = raw.find(close, i)
            if j < 0:
                return -1
        if esc == "\\":
            k = raw.find("\\", i, j)
            if k >= 0:
                i = k + 2
                continue
        elif esc == "double" and raw.startswith(close, j + len(close)):
            i = j + 2 * len(close)
            continue
        elif esc == "nix" and raw[j + 2:j + 3] in ("'", "$", "\\") and raw[j + 2:j + 3]:
            i = j + (4 if raw[j + 2] == "\\" else 3)
            continue
        return j + len(close)


def _raw_string(mode, raw, i):
    """(закрытие, escape, конец открытия) raw-строки, чья кавычка стоит в i; None, если это не raw-строка."""
    if mode == "rust":
        k = i
        while k and raw[k - 1] == "#":
            k -= 1
        if k and raw[k - 1] == "r" and (k < 2 or not _is_word(raw[k - 2])
                                        or raw[k - 2] == "b" and (k < 3 or not _is_word(raw[k - 3]))):
            return '"' + "#" * (i - k), None, i + 1
    elif mode == "cpp":
        if i and raw[i - 1] == "R" and (i < 2 or not _is_word(raw[i - 2]) or raw[i - 2] in "uUL8"):
            m = _CPP_RAW.match(raw, i)
            if m:
                return ")" + m.group(1) + '"', None, m.end()
    elif mode == "cs":
        if raw[i - 1:i] == "@" or raw[max(0, i - 2):i] == "@$":
            return '"', "double", i + 1
    return None


_LEAD_SPACE = re.compile(r"\s*")


def _docstring_start(raw, i):
    """Начало docstring, чьи тройные кавычки стоят в i: строка начинается с них (префикс r или u); иначе None."""
    lead = _LEAD_SPACE.match(raw, 0, i).end()
    return lead if lead == i or (lead == i - 1 and raw[lead] in "rRuU") else None


def _token(syn, raw, i, pending, unclosed):
    """Разбор с позиции i вне литерала и комментария.

    ("line",) — строчный комментарий до конца строки; ("skip", j) — литерал до j; ("open", закрытие,
    escape, в вывод ли, начало вывода, конец открытия) — многострочный блок или литерал; None — обычный символ.
    unclosed — открытия однострочных литералов, не закрытых в этой строке; _token дополняет его.
    """
    c = raw[i]
    if syn.heredoc == "tf" and raw.startswith("<<", i):
        m = _TF_HEREDOC.match(raw, i)
        if m:
            pending.append((m.group(2), "strip"))
            return "skip", m.end()
    if syn.lua and raw.startswith("--", i):
        m = _LUA_BLOCK.match(raw, i)
        if m:
            return "open", "]" + m.group(1) + "]", None, True, i, m.end()
    for opening, closing in syn.blocks:
        if raw.startswith(opening, i):
            return "open", closing, None, True, i, i + len(opening)
    for marker, rule in syn.line:
        if raw.startswith(marker, i) and _marker_ok(raw, i, rule):
            return ("line",)
    if syn.lua and c == "[":
        m = _LUA_LONG.match(raw, i)
        if m:
            return "open", "]" + m.group(1) + "]", None, False, i, m.end()
    if syn.zig and raw.startswith("\\\\", i):
        return "skip", len(raw)
    if c == '"' and syn.raw:
        found = _raw_string(syn.raw, raw, i)
        if found:
            return "open", found[0], found[1], False, i, found[2]
    if c == "'" and syn.char:
        m = _CHAR.match(raw, i)
        return ("skip", m.end()) if m else None
    if c == "'" and syn.quote_word and i and _is_word(raw[i - 1]):
        return None
    for opening, closing, multiline, esc in syn.strings:
        if raw.startswith(opening, i):
            if multiline:
                doc = _docstring_start(raw, i) if syn.docstring else None
                return "open", closing, esc, doc is not None, i if doc is None else doc, i + len(opening)
            # Однострочный литерал, не закрытый от кавычки, не закрывается и от следующих таких же кавычек строки;
            # исключение — пустой литерал из пары «''» при escape "double", комментария в нём нет.
            if opening in unclosed:
                return None
            j = _close(raw, i + len(opening), closing, esc)
            if j < 0:
                unclosed.add(opening)
                return None
            return "skip", j
    return None


# Срок разбора проверяется раз на столько строк.
_DEADLINE_EVERY = 1000


def _comments(text, ext, deadline=None):
    """[(номер строки с 1, строка комментария)]; номер считается по «\\n», как в git diff. TimeoutError, если
    срок deadline (time.monotonic) прошёл до конца разбора."""
    syn = _SYNTAX.get(ext.lower())
    if syn is None:
        return []
    out = []
    # Открытый многострочный блок или литерал: (закрытие, escape, идут ли его строки в вывод).
    state = None
    # Открытые heredoc: (терминатор, как сравнивать строку): тело heredoc — данные.
    pending = []
    for n, raw in enumerate(text.split("\n"), 1):
        if deadline is not None and n % _DEADLINE_EVERY == 1 and time.monotonic() >= deadline:
            raise TimeoutError
        if raw.endswith("\r"):
            raw = raw[:-1]
        if pending:
            term, mode = pending[0]
            if (raw.strip() if mode == "strip" else raw.lstrip("\t") if mode == "tabs" else raw) == term:
                pending.pop(0)
            continue
        if n == 1 and syn.shebang and raw.startswith("#!"):
            continue
        start = 0 if state and state[2] else None
        i = 0
        unclosed = set()
        while i < len(raw):
            if state:
                j = _close(raw, i, state[0], state[1])
                if j < 0:
                    break
                i, state = j, None
                continue
            m = syn.starts.search(raw, i)
            if m is None:
                break
            i = m.start()
            act = _token(syn, raw, i, pending, unclosed)
            if act is None:
                i += 1
            elif act[0] == "line":
                start = i if start is None else start
                break
            elif act[0] == "skip":
                i = act[1]
            else:
                _, closing, esc, emit, at, end = act
                if emit and start is None:
                    start = at
                state, i = (closing, esc, emit), end
        if start is not None and raw[start:].strip():
            out.append((n, raw[start:].strip()))
        if syn.heredoc == "shell":
            pending = [(term, "tabs" if tabs else "exact") for term, tabs in depcheck.heredocs(raw)]
    return out


def comment_lines(text, ext):
    """Строки комментариев текста по синтаксису расширения ext; строка блока — целиком."""
    return [c for _, c in _comments(text, ext)]


def _ext(relpath):
    name = relpath.rsplit("/", 1)[-1]
    if name in _NAMES:
        return name.lower()
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _git(root, deadline, *args):
    """stdout `git -C root <args>` байтами; None при ошибке git; TimeoutError, если вызов не уложился в
    GIT_TIMEOUT или срок deadline (time.monotonic) прошёл."""
    timeout = GIT_TIMEOUT if deadline is None else min(GIT_TIMEOUT, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError
    try:
        proc = subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", "--literal-pathspecs", *args],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


_OCTAL = re.compile(r"[0-7]{3}")
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(path):
    """Путь из заголовка diff, декодированный os.fsdecode: снимает C-кавычки git и табуляцию, которую git ставит
    после имени с пробелом; байты не в UTF-8 — суррогаты, как в os.fsdecode."""
    path = path.rstrip("\t")
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 == len(body):
            out += os.fsencode(c)
            i += 1
        elif _OCTAL.fullmatch(body[i + 1:i + 4]):
            out.append(int(body[i + 1:i + 4], 8) & 0xFF)
            i += 4
        elif body[i + 1] in _ESCAPES:
            out.append(_ESCAPES[body[i + 1]])
            i += 2
        else:
            # Неизвестный escape берётся буквально, вместе с обратной косой.
            out += os.fsencode(body[i:i + 2])
            i += 2
    return os.fsdecode(bytes(out))


_HUNK = re.compile(rb"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _added_lines(root, relpaths, base, deadline):
    """{путь: номера строк рабочей версии, добавленных против коммита base} по одному git diff на все пути;
    None, если diff не получен."""
    out = _git(root, deadline, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--relative",
               "--src-prefix=a/", "--dst-prefix=b/", "-U0", "--inter-hunk-context=0", base, "--", *relpaths)
    if out is None:
        return None
    added, current, in_header = {}, None, False
    for line in out.split(b"\n"):
        if line.startswith(b"diff --git "):
            current, in_header = None, True
        elif in_header and line.startswith(b"+++ "):
            target = os.fsdecode(line[4:])
            current = None if target == "/dev/null" else _unquote(target)[2:]
        elif line.startswith(b"@@"):
            in_header = False
            m = _HUNK.match(line)
            if current is not None and m:
                first, count = int(m.group(1)), 1 if m.group(2) is None else int(m.group(2))
                added.setdefault(current, set()).update(range(first, first + count))
    return added


def _select(root, relpaths, base, sub_bases, deadline, prefix, out):
    """Заполняет out {prefix + путь: номера строк к проверке или None — весь файл}: для отслеживаемого файла —
    строки, добавленные против коммита base; весь файл — для неотслеживаемого, без base и при недоступном diff.
    Файл подмодуля — против sub_bases[путь подмодуля] (HEAD подмодуля на старте реплики), без записи — против
    текущего HEAD подмодуля. TimeoutError — по сроку deadline; уже заполненное остаётся в out."""
    if base is None:
        out.update({prefix + p: None for p in relpaths})
        return
    # Индекс: режим и путь каждой записи; режим 160000 — подмодуль.
    listing = _git(root, deadline, "ls-files", "-s", "-z") or b""
    stage = [os.fsdecode(e).partition("\t") for e in listing.split(b"\0") if e]
    subs = [path for meta, _, path in stage if meta.startswith("160000 ")]
    listed = {path for meta, _, path in stage if not meta.startswith("160000 ")}
    in_subs = set()
    for sub in subs:
        inside = [p for p in relpaths if p.startswith(sub + "/")]
        if inside:
            in_subs.update(inside)
            nested = {k[len(sub) + 1:]: v for k, v in sub_bases.items() if k.startswith(sub + "/")}
            _select(os.path.join(root, sub), [p[len(sub) + 1:] for p in inside], sub_bases.get(sub, "HEAD"),
                    nested, deadline, f"{prefix}{sub}/", out)
    rest = [p for p in relpaths if p not in in_subs]
    tracked = [p for p in rest if p in listed]
    added = _added_lines(root, tracked, base, deadline) if tracked else {}
    for p in rest:
        out[prefix + p] = added.get(p, set()) if added is not None and p in listed else None


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def extract(root, relpaths, base="HEAD", sub_bases=None, deadline=None):
    """(строки комментариев «путь: строка», обрезано ли, файлы без известного синтаксиса, файлы, не
    разобранные к сроку deadline (time.monotonic)); списки путей отсортированы.

    Файл разбирается целиком, в вывод идут комментарии на строках, добавленных против коммита base (HEAD на
    старте реплики: коммит в ходе реплики их не прячет); None — репозиторий без коммитов, файлы берутся
    целиком. sub_bases — то же для подмодулей: {путь подмодуля: коммит}. Непустой список не разобранных к
    сроку — с предупреждением.
    """
    unknown = [p for p in relpaths if _ext(p) not in _KNOWN]
    known = [p for p in relpaths if _ext(p) in _KNOWN]
    selected = {}
    try:
        if known:
            _select(root, known, base, sub_bases or {}, deadline, "", selected)
    except TimeoutError:
        pass
    # Не прочитанные или не разобранные к сроку.
    late = [p for p in known if p not in selected]
    lines, size, truncated = [], 0, False
    for rel in known:
        if rel not in selected:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            late.append(rel)
            continue
        if truncated:
            continue
        try:
            found = _comments(_read(os.path.join(root, rel)), _ext(rel), deadline)
        except TimeoutError:
            late.append(rel)
            continue
        wanted = selected[rel]
        for n, c in found:
            if wanted is not None and n not in wanted:
                continue
            entry = f"{rel}: {c}"
            # Путь не в UTF-8 несёт суррогаты: размер — в байтах имени на диске.
            entry_size = len(entry.encode("utf-8", "surrogateescape"))
            if len(lines) >= MAX_LINES or size + entry_size > MAX_BYTES:
                truncated = True
                break
            lines.append(entry)
            size += entry_size
    if late:
        common.warn(f"строки комментариев не извлечены в срок, файлов кода без проверки: {len(late)}")
    return lines, truncated, sorted(unknown), sorted(late)
