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
    или после пробела, "code" — не сразу после «$», «{» и «\\», "php" — «#» не перед «[», "css" — не сразу после
    «:» (url(http://…) без кавычек), "start" — только пробелы перед маркером, "vim" и "vim9" — по _vim_quote и
    _marker_ok. blocks — (открытие, закрытие) блочного комментария; nested — блоки вкладываются (счётчик
    глубины). strings — (открытие, закрытие, многострочный ли, escape): escape "\\" — обратная косая,
    "double" — удвоенная закрывающая кавычка, "nix" — escape строк '' Nix, None — нет; однострочный литерал
    без закрывающей кавычки в той же строке литералом не считается. prefix — (первый знак, регулярка, нужно ли
    не-слово перед ним) символьного литерала с особым знаком: «$%» Erlang, «?#» Ruby, «\\;» Clojure. rem —
    знаки, после которых (и пробелов) слово REM открывает комментарий, None — REM не комментарий.
    line_block — (начало, конец) блока из целых строк с первой колонки (=begin/=end Ruby, POD Perl).
    exdoc — атрибуты @doc, @moduledoc, @typedoc Elixir со строкой — документация, в вывод.
    """

    def __init__(self, line=(), blocks=(), strings=(), char=False, quote_word=False, docstring=False,
                 heredoc=None, lua=False, raw=None, zig=False, shebang=False, nested=False, prefix=None,
                 rem=None, line_block=None, exdoc=False):
        self.line, self.blocks, self.char, self.quote_word = line, blocks, char, quote_word
        # Длинное открытие проверяется раньше короткого: «"""» раньше «"».
        self.strings = sorted(strings, key=lambda s: -len(s[0]))
        self.docstring, self.heredoc, self.lua, self.raw, self.zig, self.shebang = (
            docstring, heredoc, lua, raw, zig, shebang)
        self.nested, self.prefix, self.rem, self.line_block, self.exdoc = nested, prefix, rem, line_block, exdoc
        firsts = {m[0] for m, _ in line} | {o[0] for o, _ in blocks} | {s[0][0] for s in strings}
        firsts |= {"'"} if char else set()
        firsts |= {"<"} if heredoc in ("tf", "ruby", "perl", "php") else set()
        firsts |= {"-", "["} if lua else set()
        firsts |= {"\\"} if zig else set()
        firsts |= {prefix[0]} if prefix else set()
        firsts |= {"r", "R"} if rem is not None else set()
        firsts |= {"@"} if exdoc else set()
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

# Символьные литералы: «$c» Erlang, «\c» Clojure, «?c» Emacs Lisp, «#\c» Common Lisp, «?c» Ruby и Elixir
# (за ним не буква: «?a b» — литерал, «c ?abc» — тернарный оператор). Escape — обратная косая и следующий знак.
_DOLLAR_CHAR = re.compile(r"\$(?:\\.|.)")
_BACKSLASH_CHAR = re.compile(r"\\.")
_ELISP_CHAR = re.compile(r"\?(?:\\.|.)")
_HASH_CHAR = re.compile(r"#\\.")
_QUESTION_CHAR = re.compile(r"\?(?:\\.|[^\s\\])(?!\w)")
# Блоки из целых строк с первой колонки: =begin … =end Ruby, POD Perl от «=слово» до «=cut».
_RUBY_BEGIN = re.compile(r"=begin(?:\s|$)")
_RUBY_END = re.compile(r"=end(?:\s|$)")
_POD_START = re.compile(r"=[A-Za-z]")
_POD_CUT = re.compile(r"=cut(?:\s|$)")

_SYNTAX = {}


def _add(exts, **kw):
    for ext in exts:
        _SYNTAX[ext] = _Syntax(**kw)


_add(("c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm"),
     line=_SLASH, blocks=_C_BLOCK, strings=(_DQ,), char=True, raw="cpp")
_add(("java",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _DQ), char=True)
# Блоки «/* */» Kotlin, Scala, Swift, Rust, Dart вкладываются; в C, Java, JS, Go, C#, Groovy — нет.
_add(("kt", "kts", "scala"), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ_RAW, _DQ), char=True, nested=True)
_add(("swift",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _DQ), raw="swift", nested=True)
_add(("cs",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ_RAW, _DQ), char=True, raw="cs")
_add(("rs",), line=_SLASH, blocks=_C_BLOCK, strings=(('"', '"', True, "\\"),), char=True, raw="rust",
     nested=True)
_add(("go",), line=_SLASH, blocks=_C_BLOCK, strings=(("`", "`", True, None), _DQ), char=True)
_add(("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"),
     line=_SLASH, blocks=_C_BLOCK, strings=(("`", "`", True, "\\"), _DQ, _SQ))
_add(("dart",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _T_SQ, _DQ, _SQ), nested=True)
_add(("groovy", "gradle"), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _T_SQ, _DQ, _SQ))
_add(("php",), line=_SLASH + (("#", "php"),), blocks=_C_BLOCK, strings=(_DQ, _SQ), heredoc="php")
_add(("proto", "sol"), line=_SLASH, blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("zig",), line=_SLASH, strings=(_DQ,), char=True, zig=True)
_add(("py", "pyi"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), docstring=True, shebang=True)
_add(("sh", "bash", "zsh"), line=(("#", "word"),), strings=(_DQ, _SQ_RAW), heredoc="shell", shebang=True)
_RUBY_NAMES = ("rakefile", "gemfile")
_add(("rb",) + _RUBY_NAMES, line=_H, strings=(_DQ, _SQ), quote_word=True, shebang=True, heredoc="ruby",
     prefix=("?", _QUESTION_CHAR, True), line_block=(_RUBY_BEGIN, _RUBY_END))
# Perl — heredoc Ruby и ещё heredoc в дескриптор: «print $fh <<EOF».
_add(("pl",), line=_H, strings=(_DQ, _SQ), quote_word=True, shebang=True, heredoc="perl",
     line_block=(_POD_START, _POD_CUT))
_add(("r", "mk", "makefile", "cmake") + tuple(n.lower() for n in _HASH_NAMES if n.lower() not in _RUBY_NAMES),
     line=_H, strings=(_DQ, _SQ), quote_word=True, shebang=True)
_add(("toml",), line=_H, strings=(_T_DQ, _T_SQ_RAW, _DQ, _SQ_RAW))
_add(("yaml", "yml"), line=(("#", "word"),), strings=(_DQ, ("'", "'", False, "double")), quote_word=True)
_add(("ini", "cfg"), line=_H + ((";", "word"),), strings=(_DQ, _SQ), quote_word=True)
_add(("ps1",), line=_H, blocks=(("<#", "#>"),), strings=(_DQ_RAW, _SQ_RAW), quote_word=True)
_add(("tf",), line=_H + _SLASH, blocks=_C_BLOCK, strings=(_DQ,), heredoc="tf")
_add(("nix",), line=_H, blocks=_C_BLOCK, strings=(("''", "''", True, "nix"), ('"', '"', True, "\\")))
_add(("jl",), line=_H, blocks=(("#=", "=#"),), strings=(_T_DQ, _DQ), char=True, shebang=True, nested=True)
_add(("ex", "exs"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), shebang=True, prefix=("?", _QUESTION_CHAR, True),
     exdoc=True)
_add(("sql",), line=(("--", "any"),), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("lua",), line=(("--", "any"),), strings=(_DQ, _SQ), lua=True)
_add(("hs",), line=(("--", "any"),), blocks=(("{-", "-}"),), strings=(_DQ,), char=True, nested=True)
_add(("html", "xml"), blocks=(("<!--", "-->"),))
_add(("css",), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("scss", "sass", "less"), line=(("//", "css"),), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(_JSON_NAMES, strings=(_DQ,))
_add(_JSONC_NAMES, line=_SLASH, blocks=_C_BLOCK, strings=(_DQ,))
_add(_GOMOD_NAMES, line=_SLASH, strings=(_DQ, ("`", "`", False, None)))
_add(("vue", "svelte"), line=_SLASH, blocks=(("<!--", "-->"),) + _C_BLOCK, strings=(_DQ, _SQ, _BT),
     quote_word=True)
# Erlang: «$%» и «$"» — символьные литералы; тройная строка OTP 27 — без escape.
_add(("erl",), line=(("%", "any"),), strings=(_T_DQ_RAW, _DQ, _SQ), prefix=("$", _DOLLAR_CHAR, False))
# Clojure и Emacs Lisp: строки многострочные (docstring — тоже строка, не в вывод); «\;» Clojure и «?;» Emacs
# Lisp — символьные литералы. «#_» Clojure убирает форму из чтения — это код, не текст.
_LISP_STR = ('"', '"', True, "\\")
_add(("clj", "cljs", "edn"), line=((";", "any"),), strings=(_LISP_STR,), prefix=("\\", _BACKSLASH_CHAR, False))
_add(("el",), line=((";", "any"),), strings=(_LISP_STR,), prefix=("?", _ELISP_CHAR, True))
# Common Lisp: «#\;» — символьный литерал, «#| |#» вкладываются.
_add(("lisp",), line=((";", "any"),), blocks=(("#|", "|#"),), strings=(_LISP_STR,),
     prefix=("#", _HASH_CHAR, False), nested=True)
# F#: «(* *)» вкладываются, «(*)» — оператор; @"…" — verbatim, как в C#.
_add(("fs", "fsx", "fsi"), line=_SLASH, blocks=(("(*", "*)"),), strings=(_T_DQ_RAW, ('"', '"', True, "\\")), char=True,
     raw="cs", nested=True)
# Visual Basic: «'» — комментарий везде вне строки, REM — в начале оператора.
_add(("vb", "vbs"), line=(("'", "any"),), strings=(('"', '"', False, "double"),), rem=":")
# Nim: «#[ ]#» и «##[ ]##» вкладываются; «r"…"» и «ident"…"» — сырые строки с удвоенной кавычкой.
_add(("nim",), line=(("#", "any"),), blocks=(("##[", "]##"), ("#[", "]#")), strings=(_T_DQ_RAW, _DQ), char=True,
     raw="nim", nested=True)
# Vim: «"» — и комментарий, и строка (_vim_quote); «#» vim9script — после пробела, не «#{».
_add(("vim",), line=(('"', "vim"), ("#", "vim9")), strings=(_DQ, ("'", "'", False, "double")))
# Batch: REM — в начале команды, после «&», «|», «(» и «@»; «::» — только в начале строки.
_add(("bat", "cmd"), line=(("::", "start"),), strings=(_DQ_RAW,), rem="&|(@")

_KNOWN = set(_SYNTAX)
_LUA_BLOCK = re.compile(r"--\[(=*)\[")
_LUA_LONG = re.compile(r"\[(=*)\[")
_CPP_RAW = re.compile(r'"([^()\\\s]{0,16})\(')
_TF_HEREDOC = re.compile(r"<<(-?)([A-Za-z_][\w-]*)")
# Heredoc Ruby и Perl: «<<ID», «<<-ID», «<<~ID», идентификатор и в кавычках.
_RUBY_HEREDOC = re.compile(r"<<([-~]?)([\"'`]?)([A-Za-z_]\w*)\2")
# Heredoc и nowdoc PHP: «<<<ID», «<<<"ID"», «<<<'ID'», за ними конец строки.
_PHP_HEREDOC = re.compile(r"<<<[ \t]*([\"']?)([A-Za-z_]\w*)\1[ \t]*$")
# Документация Elixir: @doc, @moduledoc, @typedoc со строкой, тройной или обычной, и с сигилом ~s, ~S.
_EX_DOC = re.compile(r'@(?:module|type)?doc[ \t]+(~([sS]))?("""|\'\'\'|")')
# Символьный литерал: один символ или escape; «'a» без закрывающей кавычки — время жизни Rust, штрих Haskell.
_CHAR = re.compile(r"'(?:[^'\\]|\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|[0-7]{1,3}|.))'")


def _is_word(c):
    return c.isalnum() or c == "_"


def _marker_ok(raw, i, rule):
    if rule == "word":
        return i == 0 or raw[i - 1].isspace()
    if rule == "code":
        return i == 0 or raw[i - 1] not in "${\\"
    if rule == "php":
        return not raw.startswith("[", i + 1)
    if rule == "css":
        return i == 0 or raw[i - 1] != ":"
    if rule == "start":
        return _back(raw, i, str.isspace) == 0
    if rule == "vim":
        return _vim_quote(raw, i)
    if rule == "vim9":
        return (i == 0 or raw[i - 1].isspace()) and not raw.startswith("{", i + 1)
    return True


def _vim_quote(raw, i):
    """Открывает ли «"» в i комментарий Vim: в начале строки (после пробелов и «:») — да; после пробела — да,
    если до конца строки нет закрывающей кавычки; иначе это строка. Время — линейное по хвосту строки: «"»
    с закрывающей кавычкой разбор пропускает до неё как строку."""
    if _back(raw, i, lambda c: c.isspace() or c == ":") == 0:
        return True
    return raw[i - 1].isspace() and _close(raw, i + 1, '"', "\\") < 0


def _rem(raw, i, seps):
    """Открывает ли слово REM в i комментарий: за ним пробел или конец строки, перед ним — начало строки или
    знак из seps, через пробелы."""
    if raw[i:i + 3].lower() != "rem" or raw[i + 3:i + 4] not in ("", " ", "\t"):
        return False
    k = _back(raw, i, str.isspace)
    return k == 0 or raw[k - 1] in seps


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


def _block_opens(raw, i, opening):
    """Открывает ли opening в i блочный комментарий: «(*)» F# — оператор умножения, а не открытие."""
    return raw.startswith(opening, i) and not (opening == "(*" and raw.startswith(")", i + 2))


def _close_nested(raw, i, opening, closing, depth):
    """(индекс за закрытием внешнего блока, 0) или (-1, глубина к концу строки) для вложенных блоков: открытие
    добавляет уровень, закрытие снимает. Время — линейное по длине строки."""
    end = len(raw) + 1
    o = c = -1
    while True:
        # Найденные o и c остаются первыми от i, пока i до них не дошёл.
        if o < i:
            o = raw.find(opening, i)
            o = end if o < 0 else o
        if c < i:
            c = raw.find(closing, i)
            c = end if c < 0 else c
        if o < c:
            if _block_opens(raw, o, opening):
                i, depth = o + len(opening), depth + 1
            else:
                # «(*)» F#: оператор, глубину не меняет.
                i = o + 3
        elif c < end:
            i, depth = c + len(closing), depth - 1
            if not depth:
                return i, 0
        else:
            return -1, depth


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
    elif mode == "swift":
        # «#"…"#», «##"…"##», «#"""…"""#»: закрытие — кавычки и столько же «#».
        k = _back(raw, i, lambda c: c == "#")
        if k < i:
            quote = '"""' if raw.startswith('"""', i) else '"'
            return quote + "#" * (i - k), None, i + len(quote)
    elif mode == "nim":
        # «r"…"» и «ident"…"» — сырые однострочные, удвоенная кавычка — escape; тройные — обычные строки.
        if i and _is_word(raw[i - 1]) and not raw.startswith('"""', i):
            return '"', "double", i + 1
    return None


def _back(raw, j, pred):
    """Начало серии символов с pred, кончающейся перед j."""
    while j and pred(raw[j - 1]):
        j -= 1
    return j


# Хвосты строки перед позицией разбора просматриваются назад; время — линейное по длине строки.


def _docstring_start(raw, i):
    """Начало docstring, чьи тройные кавычки стоят в i: строка начинается с них (префикс r или u); иначе None."""
    k = i - 1 if i and raw[i - 1] in "rRuU" else i
    return k if not _back(raw, k, str.isspace) else None


_PRINT = frozenset(("print", "printf", "say"))
# Функции Perl, после которых «<<ID» вплотную — heredoc.
_PERL_TIGHT = _PRINT | {"die", "warn"}
_PERL_HANDLE = re.compile(r"[A-Z_][A-Z0-9_]*")


def _print_word(raw, k):
    """Кончается ли перед k отдельное слово print, printf или say Perl."""
    # Слово длиннее «printf» — не из _PRINT: дальше назад не идём.
    start = k
    while start and _is_word(raw[start - 1]) and k - start <= 6:
        start -= 1
    return raw[start:k] in _PRINT


def _after_print(raw, j):
    """Стоит ли перед j через пробел отдельное слово print, printf или say Perl."""
    k = _back(raw, j, str.isspace)
    return k < j and _print_word(raw, k)


# Блок-дескриптор Perl ищется назад не дальше стольких знаков: разбор строки остаётся линейным.
_BLOCK_LOOKBACK = 256


def _print_block(raw, j):
    """Стоит ли перед j, на «}», блок-дескриптор Perl после print, printf, say: «{$fh}», «{$DB::OUT}»,
    «{$self->{fh}}», «{*STDOUT}», и вплотную к слову («print{$fh}»). Блок со вложенными скобками — не длиннее
    _BLOCK_LOOKBACK знаков."""
    k = _back(raw, j - 1, lambda c: _is_word(c) or c == ":")
    if k < j - 1 and raw[k - 2:k] == "{$":
        return _print_word(raw, _back(raw, k - 2, str.isspace))
    depth, b, lo = 0, j, max(0, j - _BLOCK_LOOKBACK)
    while b > lo:
        b -= 1
        if raw[b] == "}":
            depth += 1
        elif raw[b] == "{":
            depth -= 1
            if not depth:
                break
    else:
        return False
    return raw[b + 1:b + 2] in ("$", "*") and _print_word(raw, _back(raw, b, str.isspace))


def _tight_perl(raw, i):
    """Стоит ли перед «<<» в i вплотную слово Perl, после которого идёт heredoc: print, printf, say, die,
    warn («print<<EOT») или дескриптор из заглавных и «_» после print, printf, say («print CSS<<EOF»)."""
    k = _back(raw, i, _is_word)
    if k == i or k and raw[k - 1] in "$@%&>":
        return False
    word = raw[k:i]
    return word in _PERL_TIGHT or bool(_PERL_HANDLE.fullmatch(word)) and _after_print(raw, k)


def _heredoc_ok(raw, i, m, perl=False):
    """Открывает ли «<<» с совпавшим _RUBY_HEREDOC m в позиции i heredoc, а не сдвиг или добавление.

    Сразу после слова или закрывающей скобки — сдвиг («1<<BITS», «a[0]<<X»). После пробела: за скобкой или
    кавычкой — терм, сдвиг; за переменной Perl («$a», «@a») — сдвиг; за прочим словом — heredoc, если
    идентификатор с «-», «~», в кавычках или с заглавной буквы («print <<EOF»), иначе добавление
    («a <<b»). За любым другим знаком («=», «(», «,») и в начале строки — heredoc.

    perl — ещё heredoc в дескриптор после print, printf, say: «$fh», STDOUT, STDERR через пробел, блок
    «{$fh}», «{$self->{fh}}», «{*STDOUT}» через пробел и вплотную (_print_block); «<<» вплотную после print,
    printf, say, die, warn и после дескриптора из заглавных и «_» за print, printf, say (_tight_perl); ведущие
    «_» идентификатора не мешают заглавной букве («<<_EOUSAGE_»). В Ruby «print $fh <<EOF» — сдвиг глобальной
    переменной, там правило не действует.
    """
    if not i:
        return True
    if _is_word(raw[i - 1]) or raw[i - 1] in ")]}":
        return perl and (_tight_perl(raw, i) or raw[i - 1] == "}" and _print_block(raw, i))
    j = _back(raw, i, str.isspace)
    if j == i or not j:
        return True
    if perl and raw[j - 1] == "}":
        return _print_block(raw, j)
    if raw[j - 1] in ")]}\"'`":
        return False
    k = _back(raw, j, _is_word)
    if k == j:
        return True
    if k and raw[k - 1] in "$@":
        return perl and raw[k - 1] == "$" and _after_print(raw, k - 1)
    if perl and raw[k:j] in ("STDOUT", "STDERR") and _after_print(raw, k):
        return True
    ident = m.group(3).lstrip("_") if perl else m.group(3)
    return bool(m.group(1) or m.group(2) or ident[:1].isupper())


def _token(syn, raw, i, pending, unclosed, n=0, banned=frozenset()):
    """Разбор с позиции i вне литерала и комментария.

    ("line",) — строчный комментарий до конца строки; ("skip", j) — литерал до j; ("open", закрытие,
    escape, в вывод ли, начало вывода, конец открытия[, открытие вложенного блока]) — многострочный блок или
    литерал; None — обычный символ.
    unclosed — открытия однострочных литералов, не закрытых в этой строке; _token дополняет его. n — номер
    строки; heredoc с позицией (n, i) из banned не открывается.
    """
    c = raw[i]
    if syn.prefix and c == syn.prefix[0] and not (
            syn.prefix[2] and i and (_is_word(raw[i - 1]) or raw[i - 1] in ")]}")):
        m = syn.prefix[1].match(raw, i)
        if m:
            return "skip", m.end()
    if syn.heredoc == "php" and raw.startswith("<<<", i) and (n, i) not in banned:
        m = _PHP_HEREDOC.match(raw, i)
        if m:
            pending.append((m.group(2), "php", (n, i)))
            return "skip", m.end()
    if syn.exdoc and c == "@":
        m = _EX_DOC.match(raw, i)
        if m:
            return "open", m.group(3), None if m.group(2) == "S" else "\\", True, i, m.end()
    if syn.rem is not None and c in "rR" and _rem(raw, i, syn.rem):
        return ("line",)
    if syn.heredoc == "tf" and raw.startswith("<<", i):
        m = _TF_HEREDOC.match(raw, i)
        if m:
            if (n, i) not in banned:
                pending.append((m.group(2), "strip", (n, i)))
                return "skip", m.end()
    if syn.heredoc in ("ruby", "perl") and raw.startswith("<<", i) and (n, i) not in banned:
        m = _RUBY_HEREDOC.match(raw, i)
        if m and _heredoc_ok(raw, i, m, syn.heredoc == "perl"):
            pending.append((m.group(3), "exact" if not m.group(1) else "strip", (n, i)))
            return "skip", m.end()
    if syn.lua and raw.startswith("--", i):
        m = _LUA_BLOCK.match(raw, i)
        if m:
            return "open", "]" + m.group(1) + "]", None, True, i, m.end()
    for opening, closing in syn.blocks:
        if _block_opens(raw, i, opening):
            return "open", closing, None, True, i, i + len(opening), opening if syn.nested else None
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
        if found and syn.raw == "nim":
            j = _close(raw, found[2], found[0], found[1])
            return ("skip", j) if j >= 0 else None
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


# Срок разбора проверяется раз на столько шагов; шаг — строка или позиция разбора внутри строки.
_DEADLINE_EVERY = 1000
# Разборов файла не больше стольких; каждый следующий отбрасывает хотя бы один незакрытый heredoc.
_MAX_REPARSE = 8


def _comments(text, ext, deadline=None):
    """[(номер строки с 1, строка комментария)]; номер считается по «\\n», как в git diff. TimeoutError, если
    срок deadline (time.monotonic) прошёл до конца разбора. Heredoc Ruby, Perl и Terraform без терминатора
    до конца файла — не heredoc: разбор повторяется без него, и тело читается как код."""
    syn = _SYNTAX.get(ext.lower())
    if syn is None:
        return []
    banned = frozenset()
    out, open_ = _parse(text, syn, deadline, banned)
    for _ in range(_MAX_REPARSE - 1):
        if not open_ or syn.heredoc == "shell":
            break
        banned |= {where for _, _, where in open_}
        out, open_ = _parse(text, syn, deadline, banned)
    return out


def _parse(text, syn, deadline, banned):
    """(комментарии, heredoc без терминатора к концу файла); banned — позиции heredoc, не открывающихся."""
    out = []
    # Открытый многострочный блок или литерал: (закрытие, escape, идут ли его строки в вывод, открытие
    # вложенного блока или None, глубина вложенности).
    state = None
    # Открытые heredoc: (терминатор, как сравнивать строку, позиция «<<» или None): тело heredoc — данные.
    pending = []
    # Конец открытого блока из целых строк (line_block) или None.
    line_block = None
    steps = 0

    def tick():
        nonlocal steps
        steps += 1
        if deadline is not None and steps % _DEADLINE_EVERY == 1 and time.monotonic() >= deadline:
            raise TimeoutError

    for n, raw in enumerate(text.split("\n"), 1):
        tick()
        if raw.endswith("\r"):
            raw = raw[:-1]
        # BOM UTF-8 в начале файла — не текст: иначе «#!» первой строки не узнаётся.
        if n == 1 and raw.startswith("\ufeff"):
            raw = raw[1:]
        i = 0
        if pending:
            term, mode, _ = pending[0]
            if mode == "php":
                # Терминатор PHP — с отступом, за ним код той же строки («EOT;», «EOT . 'x'; // c»).
                body = raw.lstrip()
                if not body.startswith(term) or _is_word(body[len(term):len(term) + 1]):
                    continue
                pending.pop(0)
                i = len(raw) - len(body) + len(term)
            else:
                if (raw.strip() if mode == "strip" else raw.lstrip("\t") if mode == "tabs" else raw) == term:
                    pending.pop(0)
                continue
        if n == 1 and syn.shebang and raw.startswith("#!"):
            continue
        if line_block:
            if raw.strip():
                out.append((n, raw.strip()))
            if line_block.match(raw):
                line_block = None
            continue
        if state is None and syn.line_block and syn.line_block[0].match(raw):
            out.append((n, raw.strip()))
            line_block = None if syn.line_block[1].match(raw) else syn.line_block[1]
            continue
        start = 0 if state and state[2] else None
        unclosed = set()
        while i < len(raw):
            tick()
            if state and state[3]:
                j, depth = _close_nested(raw, i, state[3], state[0], state[4])
                if j < 0:
                    state = state[:4] + (depth,)
                    break
                i, state = j, None
                continue
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
            act = _token(syn, raw, i, pending, unclosed, n, banned)
            if act is None:
                i += 1
            elif act[0] == "line":
                start = i if start is None else start
                break
            elif act[0] == "skip":
                i = act[1]
            else:
                _, closing, esc, emit, at, end, *nest = act
                if emit and start is None:
                    start = at
                state, i = (closing, esc, emit, nest[0] if nest else None, 1), end
        if start is not None and raw[start:].strip():
            out.append((n, raw[start:].strip()))
        if syn.heredoc == "shell":
            pending = [(term, "tabs" if tabs else "exact", None) for term, tabs in depcheck.heredocs(raw)]
    return out, pending


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
# Больше удалённых путей в пару переименованию не подбирается: командная строка git ограничена.
MAX_RENAME_SOURCES = 1000


def _diff(root, paths, base, deadline):
    """({путь: номера добавленных строк}, пути, новые против base без пары-переименования) git diff путей
    paths против коммита base; None, если diff не получен. --text — и для файлов с атрибутом -diff или
    binary; --find-renames — переименованный файл сравнивается со старым путём, если тот есть в paths."""
    out = _git(root, deadline, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "--find-renames",
               "--relative", "--src-prefix=a/", "--dst-prefix=b/", "-U0", "--inter-hunk-context=0", base, "--",
               *paths)
    if out is None:
        return None
    added, new, current, in_header, from_null = {}, set(), None, False, False
    for line in out.split(b"\n"):
        if line.startswith(b"diff --git "):
            current, in_header, from_null = None, True, False
        elif in_header and line.startswith(b"--- "):
            from_null = line == b"--- /dev/null"
        elif in_header and line.startswith(b"+++ "):
            target = os.fsdecode(line[4:])
            current = None if target == "/dev/null" else _unquote(target)[2:]
            if current is not None and from_null:
                new.add(current)
        elif line.startswith(b"@@"):
            in_header = False
            m = _HUNK.match(line)
            if current is not None and m:
                first, count = int(m.group(1)), 1 if m.group(2) is None else int(m.group(2))
                added.setdefault(current, set()).update(range(first, first + count))
    return added, new


def _added_lines(root, relpaths, base, deadline):
    """{путь: номера строк рабочей версии, добавленных против коммита base}; None, если diff не получен.

    Один git diff на все пути; если среди них есть новые против base, diff повторяется вместе с путями,
    удалёнными против base: переименованный файл (git mv) — против своего старого пути, а не целиком.
    """
    found = _diff(root, relpaths, base, deadline)
    if found is None or not found[1]:
        return None if found is None else found[0]
    gone = _git(root, deadline, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=D", "--relative",
                base, "--")
    sources = [os.fsdecode(e) for e in (gone or b"").split(b"\0") if e]
    if not sources or len(sources) > MAX_RENAME_SOURCES:
        return found[0]
    paired = _diff(root, [*relpaths, *sources], base, deadline)
    return found[0] if paired is None else paired[0]


def _select(root, relpaths, base, sub_bases, deadline, prefix, out):
    """Заполняет out {prefix + путь: номера строк к проверке или None — весь файл}: для отслеживаемого файла —
    строки, добавленные против коммита base; весь файл — для неотслеживаемого, без base и при недоступном diff.
    Файл подмодуля — против sub_bases[путь подмодуля] (HEAD подмодуля на старте реплики), без записи — против
    текущего HEAD подмодуля; файл вложенного репозитория не подмодуля — против sub_bases[его путь].
    Ключи sub_bases — пути от root. TimeoutError — по сроку deadline; уже заполненное остаётся в out."""
    if base is None:
        out.update({prefix + p: None for p in relpaths})
        return
    # Индекс: режим и путь каждой записи; режим 160000 — подмодуль.
    listing = _git(root, deadline, "ls-files", "-s", "-z") or b""
    stage = [os.fsdecode(e).partition("\t") for e in listing.split(b"\0") if e]
    subs = [path for meta, _, path in stage if meta.startswith("160000 ")]
    listed = {path for meta, _, path in stage if not meta.startswith("160000 ")}
    # Вложенный репозиторий не подмодуль — ключ sub_bases без записи 160000 в индексе; его файлы — против
    # своей базы, как файлы подмодуля. Файл достаётся самому глубокому репозиторию, чей путь — его префикс.
    repos = {**{sub: "HEAD" for sub in subs}, **sub_bases}
    in_subs = set()
    for sub in sorted(repos, key=len, reverse=True):
        inside = [p for p in relpaths if p.startswith(sub + "/") and p not in in_subs]
        if inside:
            in_subs.update(inside)
            # Базы глубже sub уже применены: их репозитории обработаны раньше.
            _select(os.path.join(root, sub), [p[len(sub) + 1:] for p in inside], repos[sub], {}, deadline,
                    f"{prefix}{sub}/", out)
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
    целиком. sub_bases — то же для подмодулей и вложенных репозиториев: {путь репозитория от root: коммит или
    None}. Непустой список не разобранных к сроку — с предупреждением.
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
