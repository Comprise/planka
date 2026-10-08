"""Извлечение строк комментариев из изменённых файлов для судьи."""
import codecs
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
    «:» (url(http://…) без кавычек), "start" — только пробелы перед маркером, "make" — в строке рецепта (с
    табуляции) как "word" и сразу за префиксами «@», «-», «+» (_MAKE_PREFIX), в прочих как "code", "vim" и
    "vim9" — по _vim_quote и _marker_ok. blocks —
    (открытие, закрытие) блочного комментария; nested — блоки вкладываются (счётчик глубины). strings —
    (открытие, закрытие, многострочный ли, escape): escape "\\" — обратная косая, "`" — обратная кавычка
    PowerShell, "double" — удвоенная закрывающая кавычка, "nix" — escape строк '' Nix, None — нет; однострочный
    литерал без закрывающей кавычки в той же строке литералом не считается, многострочный без закрытия до конца
    файла — тоже (_comments). prefix — (первый знак, регулярка, нужно ли не-слово перед ним) символьного
    литерала с особым знаком: «$%» Erlang, «?#» Ruby, «\\;» Clojure. rem — знаки, после которых (и пробелов)
    слово REM открывает комментарий, None — REM не комментарий.
    line_block — (начало, конец) блока из целых строк с первой колонки (=begin/=end Ruby, POD Perl).
    exdoc — атрибуты @doc, @moduledoc, @typedoc Elixir со строкой — документация, в вывод.
    regex — «/» в начале выражения (_expr_start) открывает литерал: "js" — регулярное выражение JS с классами
    «[...]» и шаблонная строка «`…`» с подстановками «${…}», "groovy" — slashy-строка «/…/» и dollar-slashy
    «$/…/$» Groovy, "ruby" — регулярное выражение «/…/» (_literal_opens), литералы «%r{…}», «%w(…)» и строки
    «"…"», «`…`» с подстановками «#{…}», "perl" — «/…/» (_literal_opens) и операторы-кавычки «m//», «s///»,
    «qr{}», «tr///», «q()», «qw[]» (_quote_parts); в обоих «$"», «$'», «$`» — переменные, после __END__ — данные.
    jsx — «<» в начале выражения открывает разметку JSX (_jsx_opens): текст между тегами — не код. sigil — сигилы
    Elixir «~r/…/», «~w(…)», с тройными кавычками. block_scalar — блочные скаляры YAML «key: |», «- >-»: строки с
    отступом больше родителя (_yaml_block) — скрипт по _yaml_script_ext, иначе данные.
    """

    def __init__(self, line=(), blocks=(), strings=(), char=False, quote_word=False, docstring=False,
                 heredoc=None, lua=False, raw=None, zig=False, shebang=False, nested=False, prefix=None,
                 rem=None, line_block=None, exdoc=False, regex=None, jsx=False, sigil=False, block_scalar=False):
        self.line, self.blocks, self.char, self.quote_word = line, blocks, char, quote_word
        # Длинное открытие проверяется раньше короткого: «"""» раньше «"».
        self.strings = sorted(strings, key=lambda s: -len(s[0]))
        self.docstring, self.heredoc, self.lua, self.raw, self.zig, self.shebang = (
            docstring, heredoc, lua, raw, zig, shebang)
        self.nested, self.prefix, self.rem, self.line_block, self.exdoc = nested, prefix, rem, line_block, exdoc
        self.regex, self.jsx, self.sigil, self.block_scalar = regex, jsx, sigil, block_scalar
        firsts = {m[0] for m, _ in line} | {o[0] for o, _ in blocks} | {s[0][0] for s in strings}
        firsts |= {"'"} if char else set()
        firsts |= {"<"} if heredoc in ("tf", "ruby", "perl", "php") else set()
        firsts |= {"-", "["} if lua else set()
        firsts |= {"\\"} if zig else set()
        firsts |= {prefix[0]} if prefix else set()
        firsts |= {"r", "R"} if rem is not None else set()
        firsts |= {"@"} if exdoc else set()
        firsts |= {"/"} if regex else set()
        firsts |= {"$"} if regex == "groovy" else set()
        firsts |= {"`"} if regex == "js" else set()
        firsts |= {"%", '"', "`"} if regex == "ruby" else set()
        firsts |= {"<"} if jsx else set()
        firsts |= {"~"} if sigil else set()
        # Операторы-кавычки Perl начинаются с буквы, специальные переменные «$"», «$'», «$`» — с «$»: ищутся своей
        # регуляркой, а не каждая буква и каждый «$».
        extra = "|" + _PERL_QUOTE.pattern if regex == "perl" else ""
        extra += "|" + _SPECIAL_VAR.pattern if regex in ("ruby", "perl") else ""
        self.starts = re.compile("[" + "".join(re.escape(c) for c in sorted(firsts)) + "]" + extra)
        # Код в фигурных скобках разметки JSX и подстановок шаблонной строки: ещё скобки, чтобы найти конец
        # выражения.
        self.code_starts = re.compile("[" + "".join(re.escape(c) for c in sorted(firsts | {"{", "}"})) + "]" + extra)


_DQ = ('"', '"', False, "\\")
_SQ = ("'", "'", False, "\\")
_SQ_RAW = ("'", "'", False, None)
_DQ_RAW = ('"', '"', False, None)
_BT = ("`", "`", False, "\\")
_DQ_ML = ('"', '"', True, "\\")
_SQ_ML = ("'", "'", True, "\\")
_BT_ML = ("`", "`", True, "\\")
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
# Специальные переменные Ruby и Perl с кавычкой в имени.
_SPECIAL_VAR = re.compile(r"\$[\"'`]")
# Комментарий режима /x в многострочном регулярном выражении: «#» в начале строки или после пробела, не «#{»;
# без флага x после закрытия литерала (_FLAGS) снимается с вывода.
_XRE_COMMENT = re.compile(r"(?<!\S)#(?!\{)")
# Операторы-кавычки Perl: не после знака слова, сигила, «->» и «::»; разделитель — вплотную любой знак не слова,
# кроме закрывающих скобок, «;» и «=>» («$h{s}», «s => 1» — слова), или через пробелы «/» и открывающая скобка
# («#» через пробел — комментарий).
_PERL_QUOTE = re.compile(r"(?<![\w$@%&:>])(?:tr|qr|qq|qw|qx|[msqy])(?=[^\w\s=})\];>]|=(?!>)|\s+[/{(\[<])")

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
# Разметка JSX — в jsx, tsx и js (React); в ts «<T>(x) => x» — обобщение, а не тег. Шаблонную строку «`…`»
# разбирает _template.
_JS_STRINGS = (_DQ, _SQ)
_add(("mjs", "cjs", "ts", "mts", "cts"), line=_SLASH, blocks=_C_BLOCK, strings=_JS_STRINGS, regex="js")
_add(("js", "jsx", "tsx"), line=_SLASH, blocks=_C_BLOCK, strings=_JS_STRINGS, regex="js", jsx=True)
_add(("dart",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _T_SQ, _DQ, _SQ), nested=True)
_add(("groovy", "gradle"), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _T_SQ, _DQ, _SQ), regex="groovy")
_add(("php",), line=_SLASH + (("#", "php"),), blocks=_C_BLOCK, strings=(_DQ, _SQ), heredoc="php")
_add(("proto", "sol"), line=_SLASH, blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("zig",), line=_SLASH, strings=(_DQ,), char=True, zig=True)
_add(("py", "pyi"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), docstring=True, shebang=True)
_add(("sh", "bash", "zsh"), line=(("#", "word"),), strings=(_DQ, _SQ_RAW), heredoc="shell", shebang=True)
_RUBY_NAMES = ("rakefile", "gemfile")
# Ruby: «"…"» и «`…`» с подстановками «#{…}» разбирает _template.
_add(("rb",) + _RUBY_NAMES, line=_H, strings=(_SQ_ML,), quote_word=True, shebang=True, heredoc="ruby",
     prefix=("?", _QUESTION_CHAR, True), line_block=(_RUBY_BEGIN, _RUBY_END), regex="ruby")
# Perl — heredoc Ruby и ещё heredoc в дескриптор: «print $fh <<EOF».
_add(("pl", "pm"), line=_H, strings=(_DQ_ML, _SQ_ML, _BT_ML), quote_word=True, shebang=True, heredoc="perl",
     line_block=(_POD_START, _POD_CUT), regex="perl")
# Make: строка рецепта (с табуляции) уходит в shell без префиксов «@», «-», «+» и пробелов вокруг них — «#» в
# ней комментарий в начале слова и сразу за префиксами. Dockerfile: «#» в начале слова — комментарий строки RUN
# для shell, внутри слова — значение («ARG A=${B#x}»).
_MAKE_PREFIX = re.compile(r"\t[ \t@+-]*")
_MAKE_NAMES = ("mk", "makefile", "gnumakefile")
_add(_MAKE_NAMES, line=(("#", "make"),), strings=(_DQ, _SQ), quote_word=True, shebang=True)
_add(("dockerfile",), line=(("#", "word"),), strings=(_DQ, _SQ), quote_word=True, shebang=True)
_add(("r", "cmake") + tuple(n.lower() for n in _HASH_NAMES
                            if n.lower() not in _RUBY_NAMES + _MAKE_NAMES + ("dockerfile",)),
     line=_H, strings=(_DQ, _SQ), quote_word=True, shebang=True)
_add(("toml",), line=_H, strings=(_T_DQ, _T_SQ_RAW, _DQ, _SQ_RAW))
_add(("yaml", "yml"), line=(("#", "word"),), strings=(_DQ, ("'", "'", False, "double")), quote_word=True,
     block_scalar=True)
_add(("ini", "cfg"), line=_H + ((";", "word"),), strings=(_DQ, _SQ), quote_word=True)
# PowerShell: строки многострочные, escape «"» — обратная кавычка; here-string «@"…"@» и «@'…'@».
_add(("ps1",), line=_H, blocks=(("<#", "#>"),),
     strings=(('@"', '"@', True, None), ("@'", "'@", True, None), ('"', '"', True, "`"), ("'", "'", True, None)),
     quote_word=True)
_add(("tf",), line=_H + _SLASH, blocks=_C_BLOCK, strings=(_DQ,), heredoc="tf")
_add(("nix",), line=_H, blocks=_C_BLOCK, strings=(("''", "''", True, "nix"), ('"', '"', True, "\\")))
_add(("jl",), line=_H, blocks=(("#=", "=#"),), strings=(_T_DQ, _DQ_ML), char=True, shebang=True, nested=True)
_add(("ex", "exs"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), shebang=True, prefix=("?", _QUESTION_CHAR, True),
     exdoc=True, sigil=True)
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
# Heredoc Ruby и Perl: «<<ID», «<<-ID», «<<~ID», идентификатор и в кавычках — там любые знаки, кроме этой
# кавычки («<<'----END----'», «<<""»); перед кавычкой — пробелы («<< "EOT"», только Perl).
_RUBY_HEREDOC = re.compile(r"<<([-~]?)(?:[ \t]+(?=[\"'`]))?([\"'`])?((?(2)(?:(?!\2).)*|[A-Za-z_]\w*))(?(2)\2)")
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
    if rule == "make":
        if not raw.startswith("\t"):
            return _marker_ok(raw, i, "code")
        return i == _MAKE_PREFIX.match(raw).end() or _marker_ok(raw, i, "word")
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
        if esc in ("\\", "`"):
            k = raw.find(esc, i, j)
            if k >= 0:
                i = k + 2
                continue
        elif esc == "double" and raw.startswith(close, j + len(close)):
            i = j + 2 * len(close)
            continue
        elif esc == "dollar":
            # Dollar-slashy Groovy: «$$» и «$/» — escape, «$/$» — escape «$/» и знак «$», а не закрытие.
            k = raw.find("$", i, j)
            if k >= 0:
                i = k + 2 if raw[k + 1] in "$/" else k + 1
                continue
        elif esc == "nix" and raw[j + 2:j + 3] in ("'", "$", "\\") and raw[j + 2:j + 3]:
            i = j + (4 if raw[j + 2] == "\\" else 3)
            continue
        return j + len(close)


def _block_opens(raw, i, opening):
    """Открывает ли opening в i блочный комментарий: «(*)» F# — оператор умножения, а не открытие."""
    return raw.startswith(opening, i) and not (opening == "(*" and raw.startswith(")", i + 2))


def _close_nested(raw, i, opening, closing, depth, esc=None):
    """(индекс за закрытием внешнего блока, 0) или (-1, глубина к концу строки) для вложенных блоков и литералов в
    скобках: открытие добавляет уровень, закрытие снимает; esc "\\" — обратная косая берёт следующий знак. Время —
    линейное по длине строки."""
    end = len(raw) + 1
    o = c = b = -1
    while True:
        # Найденные o, c и b остаются первыми от i, пока i до них не дошёл.
        if o < i:
            o = raw.find(opening, i)
            o = end if o < 0 else o
        if c < i:
            c = raw.find(closing, i)
            c = end if c < 0 else c
        if esc and b < i:
            b = raw.find(esc, i)
            b = end if b < 0 else b
        if esc and b < min(o, c):
            i = b + 2
        elif o < c:
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


# Слова, после которых начинается выражение: «/» за ними — литерал, «<» — тег.
_EXPR_KEYWORDS = frozenset(("return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
                            "do", "else", "yield", "await"))
_KEYWORD_MAX = max(map(len, _EXPR_KEYWORDS))
# Знаки, после которых начинается выражение; «)», «]», кавычка, «/» и слово — конец значения.
_EXPR_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")


def _is_ident(c):
    return _is_word(c) or c == "$"


def _expr_start(raw, i, fresh=True):
    """Начинается ли в i выражение: перед i через пробелы — знак из _EXPR_AFTER (кроме «++» и «--» постфикса)
    или слово из _EXPR_KEYWORDS, не свойство после «.»; только пробелы — fresh, то же для конца прошлой строки
    кода. Назад — пробелы и не больше _KEYWORD_MAX + 1 знаков слова: время — линейное по длине строки."""
    j = _back(raw, i, str.isspace)
    if not j:
        return fresh
    c = raw[j - 1]
    if _is_ident(c):
        k, lo = j - 1, max(0, j - _KEYWORD_MAX - 1)
        while k > lo and _is_ident(raw[k - 1]):
            k -= 1
        return raw[k:j] in _EXPR_KEYWORDS and not (k and (_is_ident(raw[k - 1]) or raw[k - 1] == "."))
    if c in "+-" and raw[j - 2:j - 1] == c:
        return False
    return c in _EXPR_AFTER


# Регулярное выражение JS: escape, класс «[...]» (в нём «/» не закрывает), флаги. Slashy-строка Groovy: escape.
# Альтернативы начинаются с разных знаков, повторы — без отката: время — линейное по длине строки.
_JS_REGEX = re.compile(r"/(?:[^\\/\[]|\\.|\[(?:[^\\\]]|\\.)*+\])++/\w*")
_SLASHY = re.compile(r"/(?:[^\\/]|\\.)++/")
# Имя тега JSX: «div», «Foo.Bar», «svg:path», «my-elem».
_JSX_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
_JSX_TEXT = re.compile(r"[<{]")
_JSX_TAG = re.compile(r"[{\"'/>]")


def _jsx_opens(raw, i):
    """Открывает ли «<» в i тег JSX: за ним «>» (фрагмент) или имя, а за именем через пробелы — «>», «/», «{»,
    атрибут или конец строки. «<T,>», «<T extends X>», «<T = X>» — параметры обобщения TS, не тег."""
    if raw.startswith(">", i + 1):
        return True
    m = _JSX_NAME.match(raw, i + 1)
    if not m:
        return False
    k = m.end()
    while k < len(raw) and raw[k] in " \t":
        k += 1
    if k == len(raw) or raw[k] in ">/{":
        return True
    if k > m.end() and raw.startswith("extends", k) and not _is_ident(raw[k + 7:k + 8]):
        return False
    return _is_ident(raw[k])


def _markup(raw, i, ctx, n, banned):
    """Разбор разметки JSX с позиции i: вершина ctx — текст или тег. Действие — как у _token; ctx меняется на
    месте. Элементы стека: ["text", позиция открытия] — дети элемента, ["tag", позиция] — открывающий тег,
    ["close"] — закрывающий тег, ["code", глубина] — код в фигурных скобках."""
    top = ctx[-1]
    if top[0] == "text":
        m = _JSX_TEXT.search(raw, i)
        if m is None:
            return "skip", len(raw)
        i = m.start()
        if raw[i] == "{":
            ctx.append(["code", 0])
        elif raw.startswith("</", i):
            ctx.append(["close"])
            return "skip", i + 2
        elif (n, i) not in banned and _jsx_opens(raw, i):
            ctx.append(["tag", (n, i)])
        return "skip", i + 1
    m = _JSX_TAG.search(raw, i)
    if m is None:
        return "skip", len(raw)
    i = m.start()
    c = raw[i]
    if c == "{":
        ctx.append(["code", 0])
        return "skip", i + 1
    if c in "\"'":
        # Строка атрибута — без escape и может занимать несколько строк.
        return "open", c, None, False, i, i + 1
    if c == "/":
        if raw.startswith("//", i):
            return "line", i
        if raw.startswith("/*", i):
            return "open", "*/", None, True, i, i + 2
        if raw.startswith("/>", i) and top[0] == "tag":
            ctx.pop()
            return "skip", i + 2
        return "skip", i + 1
    if top[0] == "tag":
        ctx[-1] = ["text", top[1]]
    else:
        ctx.pop()
        if ctx and ctx[-1][0] == "text":
            ctx.pop()
    return "skip", i + 1


# Строки с подстановками: шаблонная строка JS «`…${…}`», строки Ruby «"…#{…}"» и «`…#{…}`». Ключ — (кавычка,
# знак подстановки); в тексте — escape, закрывающая кавычка, открытие подстановки.
_TEMPLATE = {(q, d): re.compile("[" + re.escape(q) + r"\\]|" + re.escape(d) + r"\{")
             for q, d in (("`", "$"), ('"', "#"), ("`", "#"))}


def _template(raw, i, ctx):
    """Разбор текста строки с подстановками с позиции i, вершина ctx — ["tpl", позиция открытия, кавычка, знак
    подстановки]: кавычка закрывает строку, «${» JS и «#{» Ruby открывают код подстановки ["code", 0]. Действие —
    как у _token; ctx меняется на месте."""
    top = ctx[-1]
    m = _TEMPLATE[top[2], top[3]].search(raw, i)
    if m is None:
        return "skip", len(raw)
    i = m.start()
    if raw[i] == "\\":
        return "skip", i + 2
    if raw[i] == top[2]:
        ctx.pop()
        return "skip", i + 1
    ctx.append(["code", 0])
    return "skip", i + 2


def _code(syn, raw, i, ctx, pending, unclosed, n, banned, fresh, names):
    """Действие в коде: вне разметки и строк с подстановками или в фигурных скобках внутри них (вершина ctx —
    ["code", глубина]). «`» в JS, «"» и «`» в Ruby открывают строку с подстановками, «<» в начале выражения — тег
    JSX («<<» — сдвиг); прочее — _token."""
    c = raw[i]
    if ctx and c in "{}":
        if c == "{":
            ctx[-1][1] += 1
        elif ctx[-1][1]:
            ctx[-1][1] -= 1
        else:
            ctx.pop()
        return "skip", i + 1
    if c == "`" and syn.regex == "js" or c in "\"`" and syn.regex == "ruby":
        if (n, i) in banned:
            # Без закрытия до конца файла — однострочный литерал без подстановок.
            j = _close(raw, i + 1, c, "\\")
            return ("skip", j) if j >= 0 else None
        ctx.append(["tpl", (n, i), c, "$" if syn.regex == "js" else "#"])
        return "skip", i + 1
    if syn.jsx and c == "<":
        if raw.startswith("<", i + 1):
            return "skip", i + 2
        if (n, i) not in banned and _expr_start(raw, i, fresh) and _jsx_opens(raw, i):
            ctx.append(["tag", (n, i)])
            return "skip", i + 1
    return _token(syn, raw, i, pending, unclosed, n, banned, fresh, names)


# Флаги после закрытия литерала Ruby и Perl («/a/s», «s{a}{b}gex», «%r{a}x»): «x» — режим /x, «e» — замена Perl —
# код; буквы за закрывающим разделителем — флаги, а не оператор-кавычка («/a/s, @x» — не «s,…,»).
_FLAGS = re.compile(r"[A-Za-z]*")
# Парные разделители литералов Ruby, Perl, Elixir: скобки вкладываются (кроме Elixir).
_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_BRACKET_TOKENS = {o: re.compile(r"\\.|[" + re.escape(o + c) + "]") for o, c in _PAIRS.items()}
# Литералы Ruby с «%»: «%r{…}», «%w(…)», «%q[…]», «%(…)»; «%=» — присваивание.
_PERCENT = re.compile(r"%[qQwWiIrsx]?[^\w\s=]")
# Сигил Elixir: строчная буква или заглавные, разделитель; тройные кавычки — многострочный сигил.
_SIGIL = re.compile(r"~(?:[a-z]|[A-Z][A-Z0-9]*)(\"{3}|'{3}|[/|\"'(\[{<])")


def _delimited(raw, i):
    """Индекс за закрывающим разделителем литерала, чей открывающий разделитель стоит в i; -1, если в строке его
    нет. Скобки вкладываются, escape — обратная косая и следующий знак."""
    opening = raw[i]
    closing = _PAIRS.get(opening)
    if closing is None:
        return _close(raw, i + 1, opening, "\\")
    depth = 0
    for m in _BRACKET_TOKENS[opening].finditer(raw, i + 1):
        if m.group() == closing:
            if not depth:
                return m.end()
            depth -= 1
        elif m.group() == opening:
            depth += 1
    return -1


# Слова Ruby и Perl, после которых начинается выражение: «/» за ними — регулярка и вплотную, и перед пробелом
# («split/\\s+/», «split / /», «x if /a/»).
_TERM_WORDS = {"ruby": frozenset(("if", "unless", "while", "until", "and", "or", "not", "when", "elsif", "then")),
               "perl": frozenset(("if", "unless", "while", "until", "and", "or", "not", "when", "elsif", "split",
                                  "grep", "map", "join", "push", "unshift", "eq", "ne", "lt", "gt", "le", "ge",
                                  "cmp"))}


def _literal_opens(raw, i, fresh, lang, names=frozenset()):
    """Открывает ли «/» или «%» в i литерал Ruby или Perl (lang): "expr" — в начале выражения (_expr_start; после
    «}» — конец элемента хеша, деление) и после слова из _TERM_WORDS, "arg" — аргументом вызова: после слова через
    пробел и вплотную к следующему знаку («puts /#/», «puts %w(a)»); None — деление и остаток: после числа,
    переменной («$a /2», «@n /2») и локальной переменной Ruby из names не после «.» («a = 4» раньше, «a /b»), перед
    пробелом и перед «=»: в Ruby всегда («a /=2»), в Perl — с пробелом за ним («$a /= 2»); символ Ruby «:/», «:%».
    """
    if lang == "ruby" and i and raw[i - 1] == ":" and raw[i - 2:i - 1] != ":":
        return None
    j = _back(raw, i, str.isspace)
    if (not j or raw[j - 1] != "}") and _expr_start(raw, i, fresh):
        return "expr"
    if not j or not _is_word(raw[j - 1]):
        return None
    k = _back(raw, j, _is_word)
    member = k and (raw[k - 1] in "$@%&." or raw[k - 2:k] in ("->", "::"))
    if raw[k:j] in _TERM_WORDS[lang] and not member:
        return "expr"
    nxt = raw[i + 1:i + 2]
    if j == i or nxt in ("", " ", "\t") or nxt == "=" and (lang == "ruby" or raw[i + 2:i + 3] in ("", " ", "\t")):
        return None
    if raw[k].isdigit() or k and raw[k - 1] in "$@%":
        return None
    return None if raw[k:j] in names and not (k and raw[k - 1] == ".") else "arg"


# Локальные переменные Ruby: присваивание («a = 1», «a ||= 1», «a += 1», «a, b = …»), параметры метода в скобках
# и параметры блока после «do» и «{». Списки параметров — не длиннее 256 знаков: разбор строки остаётся линейным.
_RUBY_ASSIGN = re.compile(r"(?<![\w$@:.])([a-z_]\w*)[ \t]*(?:\|\||&&|\*\*|<<|>>|[-+*/%|&^])?=(?![=~>])")
_RUBY_MULTI = re.compile(r"[ \t]*([a-z_]\w*(?:[ \t]*,[ \t]*\*?[a-z_]\w*)+)[ \t]*=(?![=~>])")
_RUBY_PARAMS = re.compile(r"\bdef[ \t]+[\w.]+[?!=]?[ \t]*\(([^()]{0,256})\)|(?:\bdo|\{)[ \t]*\|([^|]{0,256})\|")
_RUBY_PARAM = re.compile(r"(?:^|[,(])[ \t]*[*&]{0,2}([a-z_]\w*)")


def _ruby_locals(raw):
    """Имена локальных переменных Ruby, которые вводит строка raw (_RUBY_ASSIGN, _RUBY_MULTI, _RUBY_PARAMS)."""
    found = {m.group(1) for m in _RUBY_ASSIGN.finditer(raw)}
    m = _RUBY_MULTI.match(raw)
    if m:
        found.update(_RUBY_PARAM.findall("," + m.group(1)))
    for m in _RUBY_PARAMS.finditer(raw):
        found.update(_RUBY_PARAM.findall("," + (m.group(1) if m.group(1) is not None else m.group(2))))
    return found


def _quote_parts(raw, k, two, n, banned, where, regex=False):
    """Действие для литерала Ruby или Perl с открывающим разделителем в k: ("skip", конец за флагами) — закрыт в
    строке, у оператора с двумя частями (two) — обе части; ("open", …, then, regex) — продолжается на следующих
    строках, then у первой части оператора с двумя частями — где вторая: "same" — до того же разделителя ещё раз,
    "pair" — со своим разделителем через пробелы (_second_part), "code" — вторая часть в «{}» (_second_part);
    regex — первая часть — регулярное выражение, в многострочном «#» — комментарий режима /x (_XRE_COMMENT).
    Незакрытый литерал с позицией открытия where из banned кончается с концом строки."""
    end = _delimited(raw, k)
    opening = raw[k]
    if end < 0:
        if where in banned:
            return "skip", len(raw)
        nest = opening if opening in _PAIRS else None
        then = None if not two else "pair" if nest else "same"
        return "open", _PAIRS.get(opening, opening), "\\", False, k, k + 1, nest, where, then, regex
    if not two:
        return "skip", _FLAGS.match(raw, end).end()
    if opening in _PAIRS:
        return _second_part(raw, end, n, banned, where) or ("skip", _FLAGS.match(raw, end).end())
    j = _close(raw, end, opening, "\\")
    if j >= 0:
        return "skip", _FLAGS.match(raw, j).end()
    if where in banned:
        return "skip", len(raw)
    return "open", opening, "\\", False, k, end, None, where


def _second_part(raw, j, n, banned, where):
    """Действие для второй части оператора s, tr, y Perl, чья первая часть в скобках кончилась в j: разделитель —
    через пробелы в той же строке; None — его там нет. Вторая часть в «{}», не закрытая в строке, — литерал со
    второй частью "code": после закрытия с флагом e её текст разбирается как код Perl (_parse)."""
    while j < len(raw) and raw[j].isspace():
        j += 1
    if j == len(raw) or _is_word(raw[j]) or raw[j] == "#":
        return None
    act = _quote_parts(raw, j, False, n, banned, where)
    return act[:8] + ("code", False) if act[0] == "open" and raw[j] == "{" else act


def _token(syn, raw, i, pending, unclosed, n=0, banned=frozenset(), fresh=True, names=frozenset()):
    """Разбор с позиции i вне литерала и комментария.

    ("line",) — строчный комментарий до конца строки (у _markup — ("line", начало комментария)); ("skip", j) —
    литерал до j; ("open", закрытие, escape, в вывод ли, начало вывода, конец открытия[, открытие вложенного
    блока или None[, позиция открытия литерала (n, i)[, вторая часть, регулярка ли — _quote_parts]]]) —
    многострочный блок или литерал; None — обычный символ.
    unclosed — открытия однострочных литералов, не закрытых в этой строке; _token дополняет его. n — номер
    строки; heredoc с позицией (n, i) из banned не открывается, многострочный литерал с ней — однострочный.
    fresh — начинается ли выражение в начале строки (_expr_start).
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
    if syn.sigil and c == "~":
        m = _SIGIL.match(raw, i)
        if m:
            closing = _PAIRS.get(m.group(1), m.group(1))
            if (n, i) not in banned:
                return "open", closing, "\\", False, i, m.end(), None, (n, i), None, raw[i + 1] in "rR"
            j = _close(raw, m.end(), closing, "\\")
            return ("skip", j) if j >= 0 else None
    if syn.regex in ("ruby", "perl") and c == "$" and raw[i + 1:i + 2] in ("\"", "'", "`"):
        # Специальные переменные «$"», «$'», «$`» — не строки.
        return "skip", i + 2
    if syn.regex == "perl" and c in "mqsty":
        m = _PERL_QUOTE.match(raw, i)
        if m:
            k = m.end()
            while raw[k].isspace():
                k += 1
            return _quote_parts(raw, k, m.group() in ("s", "tr", "y"), n, banned, (n, i), m.group() in ("m", "qr", "s"))
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
        # Пробел перед идентификатором в кавычках — только в Perl: в Ruby «x << "a"» — добавление.
        spaced = m and raw[i + 2 + len(m.group(1))] in " \t"
        if m and not (spaced and syn.heredoc == "ruby") and _heredoc_ok(raw, i, m, syn.heredoc == "perl"):
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
    if syn.regex in ("js", "groovy") and c in "/$" and _expr_start(raw, i, fresh):
        # «//» и «/*» выше — комментарии. Незакрытый в строке литерал кончается с ней: многострочная
        # slashy-строка Groovy дальше читается как код, а мнимая регулярка не прячет следующие строки.
        if c == "/":
            m = (_JS_REGEX if syn.regex == "js" else _SLASHY).match(raw, i)
            return "skip", m.end() if m else len(raw)
        if syn.regex == "groovy" and raw.startswith("$/", i):
            return "open", "/$", "dollar", False, i, i + 2
    opens = syn.regex in ("ruby", "perl") and c in "/%" and _literal_opens(raw, i, fresh, syn.regex, names)
    if opens:
        if c == "/":
            # Регулярка в начале выражения продолжается на следующих строках; аргументом вызова — кончается с концом
            # строки, как регулярка JS.
            m = _SLASHY.match(raw, i)
            if m:
                return "skip", _FLAGS.match(raw, m.end()).end()
            if opens == "expr" and (n, i) not in banned:
                return "open", "/", "\\", False, i, i + 1, None, (n, i), None, True
            return "skip", len(raw)
        if syn.regex == "ruby" and _PERCENT.match(raw, i):
            return _quote_parts(raw, i + 2 if raw[i + 1].isalpha() else i + 1, False, n, banned, (n, i),
                                raw[i + 1] == "r")
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
            if multiline and (n, i) not in banned:
                doc = _docstring_start(raw, i) if syn.docstring else None
                return ("open", closing, esc, doc is not None, i if doc is None else doc, i + len(opening), None,
                        (n, i))
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


# Индикатор блочного скаляра YAML в конце кода строки: «|» или «>», за ним индикаторы отступа и обрезки.
_YAML_INDICATOR = re.compile(r"(?:^|(?<=\s))[|>][-+1-9]{0,2}$")
# Отступ и маркеры элементов последовательности перед ключом: «  - - key:».
_YAML_LEAD = re.compile(r" *(?:- +)*")
# Ключ отображения в начале кода строки: простой или в кавычках, за двоеточием пробел или конец строки.
_YAML_KEY = re.compile(r"( *(?:- +)*)(?:\"([^\"]*)\"|'([^']*)'|([\w$][\w.$/-]*))[ \t]*:(?:[ \t]|$)")
# Ключи, чей блочный скаляр — скрипт shell: GitHub Actions, GitLab CI, Travis CI, Ansible (и с пространством
# имён, «ansible.builtin.shell»), Compose, CircleCI, Buildkite, Drone, Azure Pipelines, Tekton, Read the Docs
# (build.jobs). Скаляр под прочим ключом — данные.
_YAML_SCRIPT_KEYS = frozenset((
    "run", "script", "before_script", "after_script", "install", "before_install", "after_success",
    "after_failure", "before_deploy", "after_deploy", "shell", "command", "commands", "cmd", "entrypoint", "bash",
    "pwsh", "powershell", "post_checkout", "pre_system_dependencies", "post_system_dependencies",
    "pre_create_environment", "post_create_environment", "pre_install", "post_install", "pre_build", "post_build"))


# Действие GitHub Actions, чей вход «script» — JavaScript.
_GITHUB_SCRIPT = re.compile(r"actions/github-script(?:@|$)")


def _yaml_key(code, keys, uses):
    """Ключ отображения в начале кода строки code YAML или None; keys — стек (колонка ключа, ключ) открытых
    отображений: ключ снимает со стека ключи с колонкой не меньше своей и ложится на него. uses — {колонка:
    значение ключа uses} открытых отображений: ключ снимает записи глубже себя, элемент последовательности «- » —
    и запись своей колонки."""
    m = _YAML_KEY.match(code)
    if m is None:
        return None
    col, key = len(m.group(1)), next(g for g in m.groups()[1:] if g is not None)
    while keys and keys[-1][0] >= col:
        keys.pop()
    keys.append((col, key))
    for c in [c for c in uses if c > col or c == col and "-" in m.group(1)]:
        del uses[c]
    if key == "uses":
        uses[col] = code[m.end():].strip().strip("\"'")
    return key


def _yaml_parent(keys, col):
    """Ключ, которому принадлежит элемент последовательности с «-» в колонке col: последний ключ стека keys с
    колонкой не больше col (список без отступа — в колонке ключа); ключи глубже снимаются."""
    while keys and keys[-1][0] > col:
        keys.pop()
    return keys[-1][1] if keys else None


def _yaml_script_ext(keys, uses):
    """Синтаксис скрипта в блочном скаляре ключа на вершине стека keys (_yaml_key): "js" — вход «script» под
    «with:» шага с actions/github-script (uses — _yaml_key), "sh" — ключ из _YAML_SCRIPT_KEYS (у ключа с
    пространством имён — последняя часть), None — данные."""
    if not keys:
        return None
    key = keys[-1][1].rsplit(".", 1)[-1]
    if key == "script" and len(keys) > 1 and keys[-2][1] == "with" and _GITHUB_SCRIPT.match(
            uses.get(keys[-2][0], "")):
        return "js"
    return "sh" if key in _YAML_SCRIPT_KEYS else None


def _yaml_script(lines, ext, deadline):
    """Комментарии скрипта блочного скаляра YAML lines [(номер строки, строка)] по синтаксису ext, без общего
    отступа строк: [(номер строки, комментарий)]."""
    body = [raw for _, raw in lines]
    indent = min((len(r) - len(r.lstrip(" ")) for r in body if r.strip()), default=0)
    found = _parse("\n".join(r[indent:] for r in body), _SYNTAX[ext], deadline, frozenset())[0]
    return [(lines[k - 1][0], c) for k, c in found]


def _yaml_block(code, keys):
    """Отступ родителя блочного скаляра YAML, который открывает код строки code («key: |», «- key: >-», «- |»;
    тег и якорь перед индикатором — «key: !!str &a |»): строки скаляра — с отступом больше него. Индикатор один на
    строке — значение ключа прошлых строк: родитель — последний ключ стека keys (_yaml_key) левее индикатора, без
    него — -1. None — строка скаляр не открывает."""
    code = code.rstrip()
    m = _YAML_INDICATOR.search(code)
    if m is None:
        return None
    head = code[:m.start()].rstrip()
    for _ in range(2):
        parts = head.rsplit(None, 1)
        if len(parts) < 2 or parts[1][0] not in "!&":
            break
        head = parts[0]
    if not head.strip():
        col = len(code) - len(code.lstrip(" "))
        return next((c for c, _ in reversed(keys) if c < col), -1)
    if head.endswith(":"):
        return _YAML_LEAD.match(head).end()
    if head.endswith("-") and _YAML_LEAD.fullmatch(head + " "):
        return len(head) - 1
    return None


# Срок разбора проверяется раз на столько шагов; шаг — строка или позиция разбора внутри строки.
_DEADLINE_EVERY = 1000
# Разборов файла не больше стольких; каждый следующий отбрасывает хотя бы одно незакрытое к концу файла открытие:
# heredoc, тег JSX, строку с подстановками, многострочный литерал.
_MAX_REPARSE = 8
# Вложенность вторых частей s{…}{…}e Perl, разбираемых как код: каждая строка разбирается не больше
# _MAX_CODE_DEPTH + 1 раз.
_MAX_CODE_DEPTH = 4


def _comments(text, ext, deadline=None):
    """[(номер строки с 1, строка комментария)]; номер считается по «\\n», как в git diff. TimeoutError, если
    срок deadline (time.monotonic) прошёл до конца разбора. Heredoc Ruby, Perl и Terraform без терминатора
    до конца файла — не heredoc: разбор повторяется без него, и тело читается как код. Так же тег JSX без
    закрытия до конца файла — не тег, а строка с подстановками и многострочный литерал (строка, оператор-кавычка
    Perl, литерал «%» Ruby, сигил Elixir) — однострочные: разбор повторяется без всех незакрытых открытий."""
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


def _parse(text, syn, deadline, banned, level=0):
    """(комментарии, открытия без закрытия к концу файла: heredoc, теги JSX, строки с подстановками, многострочные
    литералы); banned — позиции открытий, не открывающихся. level — вложенность text во вторые части s{…}{…}e Perl:
    глубже _MAX_CODE_DEPTH они не разбираются, и разбор остаётся линейным."""
    out = []
    # Открытый многострочный блок или литерал: (закрытие, escape, идут ли его строки в вывод, открытие
    # вложенного блока или None, глубина вложенности, позиция открытия литерала для отката или None, где вторая
    # часть оператора Perl s, tr, y — _quote_parts — или None, регулярное выражение ли с комментариями /x).
    state = None
    # Открытые heredoc: (терминатор, как сравнивать строку, позиция «<<» или None): тело heredoc — данные.
    pending = []
    # Конец открытого блока из целых строк (line_block) или None.
    line_block = None
    # Стек разметки JSX (_markup) и строк с подстановками (_template); пуст — код вне них.
    ctx = []
    # Открытый блочный скаляр YAML: (отступ родителя, его строки [(номер, строка)] у скрипта или None у данных,
    # синтаксис скрипта — _yaml_script_ext) или None; стек ключей открытых отображений YAML и значения их ключей
    # uses (_yaml_key).
    scalar, keys, uses = None, [], {}
    # Прошла ли строка __END__ или __DATA__ Ruby и Perl: дальше данные, кроме блоков line_block.
    data = False
    # Начинается ли выражение в начале строки: в JS и Perl — по концу прошлой строки кода («a\n/ b» — деление);
    # в Groovy и Ruby конец строки завершает оператор.
    fresh = True
    # Номера в out строк, чей вывод начат комментарием /x открытой многострочной регулярки (_XRE_COMMENT): без
    # флага x после закрытия литерала они снимаются; None — регулярки нет или решение принято. Строки второй части
    # s{…}{…} Perl, открытой в прошлых строках, — [(номер, текст)] или None.
    xre, body = None, None
    # Локальные переменные Ruby, введённые строками до текущей и ею (_ruby_locals): «/» и «%» после них — деление.
    names = set()
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
        if scalar is not None:
            if not raw.strip() or len(raw) - len(raw.lstrip(" ")) > scalar[0]:
                if scalar[1] is not None:
                    scalar[1].append((n, raw))
                continue
            if scalar[1]:
                out.extend(_yaml_script(scalar[1], scalar[2], deadline))
            scalar = None
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
        if data:
            continue
        if state is None and not ctx and syn.regex in ("ruby", "perl") and raw.rstrip() in ("__END__", "__DATA__"):
            data = True
            continue
        start = 0 if state and state[2] else None
        # Начат ли вывод строки комментарием /x.
        xre_here = False
        unclosed = set()
        if syn.regex == "ruby":
            names.update(_ruby_locals(raw))
        # Конец кода строки: начало строчного комментария или конец строки; начало и конец последнего блочного
        # комментария строки (-1 — открыт в прошлых строках или нет).
        cut, opened_at, closed_at = len(raw), -1, -1
        while i < len(raw):
            tick()
            if state:
                if state[3]:
                    j, depth = _close_nested(raw, i, state[3], state[0], state[4], state[1])
                else:
                    j, depth = _close(raw, i, state[0], state[1]), 0
                if state[7] and start is None:
                    m = _XRE_COMMENT.search(raw, i, j - len(state[0]) if j >= 0 else len(raw))
                    if m:
                        start, xre_here = m.start(), True
                if state[6] == "code":
                    body.append((n, raw[i:j - len(state[0]) if j >= 0 else len(raw)]))
                if j < 0:
                    state = state[:4] + (depth,) + state[5:] if state[3] else state
                    break
                closing, esc, emit, _, _, where, then, _ = state
                i, state, closed_at = j, None, j if emit else closed_at
                if syn.regex in ("ruby", "perl") and then in (None, "code"):
                    i = _FLAGS.match(raw, j).end()
                if then == "same":
                    # Вторая часть оператора Perl «s/…/…/» — до того же разделителя.
                    state = (closing, esc, False, None, 1, where, None, False)
                    continue
                act = _second_part(raw, j, n, banned, where) if then == "pair" else None
                # Флаги за последним разделителем литерала; None — вторая часть оператора не закрыта в этой строке
                # или не найдена в ней.
                flags = None
                if then in (None, "code"):
                    flags = _FLAGS.match(raw, j).group()
                elif act is not None and act[0] == "skip":
                    flags = raw[_back(raw, act[1], str.isalpha):act[1]]
                if then == "code":
                    if "e" in flags and level < _MAX_CODE_DEPTH:
                        found = _parse("\n".join(t for _, t in body), syn, deadline, frozenset(), level + 1)[0]
                        out.extend((body[k - 1][0], c) for k, c in found)
                    body = None
                if xre is not None and (flags is not None or act is None):
                    # Флаги известны — литерал закрыт; второй части нет в строке — флаги неизвестны, вывод остаётся.
                    if flags is not None and "x" not in flags:
                        for k in reversed(xre):
                            del out[k]
                        if xre_here:
                            start = None
                    xre, xre_here = None, False
                if act is None:
                    continue
            else:
                top = ctx[-1][0] if ctx else None
                if top == "tpl":
                    act = _template(raw, i, ctx)
                elif top and top != "code":
                    act = _markup(raw, i, ctx, n, banned)
                else:
                    m = (syn.code_starts if ctx else syn.starts).search(raw, i)
                    if m is None:
                        break
                    i = m.start()
                    act = _code(syn, raw, i, ctx, pending, unclosed, n, banned, fresh, names)
            if act is None:
                i += 1
            elif act[0] == "line":
                cut = act[1] if len(act) > 1 else i
                if start is None:
                    start = cut
                break
            elif act[0] == "skip":
                i = act[1]
            else:
                _, closing, esc, emit, at, end, *rest = act
                if emit and start is None:
                    start = at
                opened_at = at if emit else opened_at
                rest += [None] * (4 - len(rest))
                state, i = (closing, esc, emit, rest[0], 1, *rest[1:]), end
                if rest[3]:
                    xre = []
                if rest[2] == "code":
                    body = []
        if start is not None and raw[start:].strip():
            out.append((n, raw[start:].strip()))
            if xre_here and xre is not None:
                xre.append(len(out) - 1)
        if syn.block_scalar and state is None:
            key = _yaml_key(raw[:cut], keys, uses)
            parent = _yaml_block(raw[:cut], keys)
            if parent is not None:
                if key is None:
                    _yaml_parent(keys, parent)
                ext = _yaml_script_ext(keys, uses)
                scalar = (parent, [] if ext else None, ext)
        if syn.regex in ("js", "perl") and state is None and not (ctx and ctx[-1][0] != "code"):
            # Блочный комментарий в конце строки — не код: конец кода перед ним. Строка из одних комментариев и
            # пробелов, как и блок из прошлых строк, конец кода не меняет.
            j = _back(raw, cut, str.isspace)
            if j and j == closed_at:
                j = _back(raw, opened_at, str.isspace) if opened_at >= 0 else 0
            if j:
                fresh = _expr_start(raw, j, fresh)
        if syn.heredoc == "shell":
            pending = [(term, "tabs" if tabs else "exact", None) for term, tabs in depcheck.heredocs(raw)]
    if scalar is not None and scalar[1]:
        out.extend(_yaml_script(scalar[1], scalar[2], deadline))
    open_ = [(None, "ctx", e[1]) for e in ctx if e[0] in ("tag", "text", "tpl")]
    if state and state[5]:
        open_.append((None, "literal", state[5]))
    return out, pending + open_


def comment_lines(text, ext):
    """Строки комментариев текста по синтаксису расширения ext; строка блока — целиком."""
    return [c for _, c in _comments(text, ext)]


def _ext(relpath):
    name = relpath.rsplit("/", 1)[-1]
    if name in _NAMES:
        return name.lower()
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _git(root, deadline, *args):
    """stdout `git -C root <args>` байтами, с окружением common.git_env: репозиторий выше root не берётся; None при
    ошибке git; TimeoutError, если вызов не уложился в GIT_TIMEOUT или срок deadline (time.monotonic) прошёл."""
    timeout = GIT_TIMEOUT if deadline is None else min(GIT_TIMEOUT, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError
    try:
        proc = subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", "--literal-pathspecs", *args],
                              capture_output=True, timeout=timeout, env=common.git_env(root))
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
# Сумма длин путей-аргументов одного вызова git, байт: командная строка ограничена (ARG_MAX; 32 767 знаков всей
# строки в Windows). Больше путей — несколько вызовов.
MAX_ARG_BYTES = 24_000
_DIFF = ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "--find-renames", "--relative",
         "--src-prefix=a/", "--dst-prefix=b/", "-U0", "--inter-hunk-context=0")


def _batches(paths):
    """Пути paths частями, сумма длин путей части в байтах — не больше MAX_ARG_BYTES; путь длиннее — частью из
    него одного."""
    batch, size = [], 0
    for p in paths:
        n = len(os.fsencode(p)) + 1
        if batch and size + n > MAX_ARG_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(p)
        size += n
    if batch:
        yield batch


def _parse_diff(out):
    """({путь: номера добавленных строк}, пути, новые против базы) из вывода git diff -U0; путь без ханков
    (переименование без правок) — с пустым множеством."""
    added, new, current, in_header, from_null = {}, set(), None, False, False
    for line in out.split(b"\n"):
        if line.startswith(b"diff --git "):
            current, in_header, from_null = None, True, False
        elif in_header and line.startswith(b"rename to "):
            current = _unquote(os.fsdecode(line[10:]))
            added.setdefault(current, set())
        elif in_header and line.startswith(b"--- "):
            from_null = line == b"--- /dev/null"
        elif in_header and line.startswith(b"+++ "):
            target = os.fsdecode(line[4:])
            current = None if target == "/dev/null" else _unquote(target)[2:]
            if current is not None:
                added.setdefault(current, set())
                if from_null:
                    new.add(current)
        elif line.startswith(b"@@"):
            in_header = False
            m = _HUNK.match(line)
            if current is not None and m:
                first, count = int(m.group(1)), 1 if m.group(2) is None else int(m.group(2))
                added[current].update(range(first, first + count))
    return added, new


def _diff(root, paths, base, deadline):
    """({путь: номера добавленных строк}, пути, новые против base) git diff путей paths против коммита base —
    вызовами не длиннее MAX_ARG_BYTES путей; None, если diff не получен. --text — и для файлов с атрибутом -diff
    или binary."""
    added, new = {}, set()
    for batch in _batches(paths):
        out = _git(root, deadline, *_DIFF, base, "--", *batch)
        if out is None:
            return None
        part = _parse_diff(out)
        added.update(part[0])
        new |= part[1]
    return added, new


def _added_lines(root, relpaths, base, deadline):
    """{путь: номера строк рабочей версии, добавленных против коммита base}; None, если diff не получен.

    git diff путей relpaths; если среди них есть новые против base, ещё один git diff всего дерева только
    переименований (--diff-filter=R): переименованный файл (git mv) — против своего старого пути, а не целиком.
    """
    found = _diff(root, relpaths, base, deadline)
    if found is None or not found[1]:
        return None if found is None else found[0]
    added, new = found
    out = _git(root, deadline, *_DIFF, "--diff-filter=R", base, "--")
    if out is not None:
        renamed = _parse_diff(out)[0]
        added.update({p: lines for p, lines in renamed.items() if p in new})
    return added


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


# Кодировки по BOM; BOM UTF-32 LE начинается с BOM UTF-16 LE и проверяется раньше.
_BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF16_LE, "utf-16-le"),
         (codecs.BOM_UTF16_BE, "utf-16-be"))


def _read(path):
    """(текст файла, номера строк git по порядку строк текста или None — совпадают с ними): UTF-16 и UTF-32 —
    по BOM, прочее — UTF-8; не прочитан — пустой текст. git считает строки по байту «\\n», а в UTF-16 и UTF-32
    этот байт входит и в другие знаки."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return "", None
    for bom, codec in _BOMS:
        if data.startswith(bom):
            body = data[len(bom):]
            unit = "\n".encode(codec)
            lines, pos = [1], 0
            while True:
                k = body.find(unit, pos)
                while k >= 0 and k % len(unit):
                    k = body.find(unit, k + 1)
                if k < 0:
                    break
                lines.append(lines[-1] + body.count(b"\n", pos, k + len(unit)))
                pos = k + len(unit)
            return body.decode(codec, errors="replace"), lines
    return data.decode("utf-8", errors="replace"), None


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
        text, numbers = _read(os.path.join(root, rel))
        try:
            found = _comments(text, _ext(rel), deadline)
        except TimeoutError:
            late.append(rel)
            continue
        wanted = selected[rel]
        for n, c in found:
            if wanted is not None and (numbers[n - 1] if numbers else n) not in wanted:
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
