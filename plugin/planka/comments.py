"""Извлечение строк комментариев из изменённых файлов для судьи."""
import codecs
import functools
import html
import os
import re
import subprocess
import time

import common
import shparse

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
    или после пробела, "code" — не сразу после «$», «${» и «\\», "php" — «#» не перед «[», "css" — не сразу после
    «:» (url(http://…) без кавычек), "start" — только пробелы перед маркером, "make" — в строке рецепта (с
    табуляции) как "word" и сразу за префиксами «@», «-», «+» (_MAKE_PREFIX), в прочих — не сразу после «$», «{»
    и «\\», "vim" и "vim9" — по _vim_quote и _marker_ok. blocks — (открытие, закрытие) блочного комментария;
    nested — блоки вкладываются (счётчик глубины). strings —
    (открытие, закрытие, многострочный ли, escape): многострочный — True, False или "cont" (на следующую строку —
    только за нечётной серией «\\» в конце строки, Groovy); escape "\\" — обратная косая, "`" — обратная кавычка
    PowerShell, "double" — удвоенная закрывающая кавычка, "nix" — escape строк '' Nix, None — нет; однострочный
    литерал без закрывающей кавычки в той же строке литералом не считается, многострочный без закрытия до конца
    файла — тоже (_comments), тройная кавычка тогда — пустая строка и кавычка. prefix — (первый знак, регулярка,
    нужно ли не-слово перед ним) символьного литерала с особым знаком: «$%» Erlang, «?#» Ruby, «\\;» Clojure. rem —
    знаки, после которых (и пробелов) слово REM открывает комментарий, None — REM не комментарий.
    line_block — (начало, конец) блока из целых строк с первой колонки (=begin/=end Ruby, POD Perl).
    exdoc — атрибуты @doc, @moduledoc, @typedoc Elixir со строкой — документация, в вывод.
    regex — «/» в начале выражения (_expr_start) открывает литерал: "js" — регулярное выражение JS с классами
    «[...]» и шаблонная строка «`…`» с подстановками «${…}», "groovy" — многострочная slashy-строка «/…/» с
    подстановками «${…}» (_groovy_slashy_ok) и dollar-slashy «$/…/$» Groovy, "ruby" — регулярное выражение «/…/»
    (_literal_opens), литералы «%r{…}», «%w(…)» и строки «"…"», «`…`» с подстановками «#{…}», "perl" — «/…/»
    (_literal_opens) и операторы-кавычки «m//», «s///», «qr{}», «tr///», «q()», «qw[]» (_quote_parts); в обоих
    «$"», «$'», «$`» — переменные, после __END__ — данные.
    jsx — «<» в начале выражения открывает разметку JSX (_jsx_opens): текст между тегами — не код. sfc — "vue" или
    "svelte": файл — разметка однофайлового компонента (_sfc, _markup), код — JavaScript в «{{…}}» Vue и «{…}»
    Svelte. sigil — сигилы Elixir «~r/…/», «~w(…)», с тройными кавычками. block_scalar — блочные скаляры YAML
    «key: |», «- >-»: строки с отступом больше родителя (_yaml_block) — скрипт по _yaml_script_ext, иначе данные.
    interp — ({открытие строки: _Interp}, правило префикса) строк с подстановками кода (_interp_at): escape такой
    строки — _Interp, её закрытие ищет _close_interp. php — файл PHP: вне «<?php … ?>», «<?= … ?>», «<? … ?>» —
    HTML (_php_html), «?>» кончает и строчный комментарий.
    """

    def __init__(self, line=(), blocks=(), strings=(), char=False, quote_word=False, docstring=False,
                 heredoc=None, lua=False, raw=None, zig=False, shebang=False, nested=False, prefix=None,
                 rem=None, line_block=None, exdoc=False, regex=None, jsx=False, sigil=False, block_scalar=False,
                 sfc=None, interp=None, php=False):
        self.line, self.blocks, self.char, self.quote_word = line, blocks, char, quote_word
        # Длинное открытие проверяется раньше короткого: «"""» раньше «"».
        self.strings = sorted(strings, key=lambda s: -len(s[0]))
        self.docstring, self.heredoc, self.lua, self.raw, self.zig, self.shebang = (
            docstring, heredoc, lua, raw, zig, shebang)
        self.nested, self.prefix, self.rem, self.line_block, self.exdoc = nested, prefix, rem, line_block, exdoc
        self.regex, self.jsx, self.sigil, self.block_scalar, self.sfc = regex, jsx, sigil, block_scalar, sfc
        self.interp, self.php = interp, php
        for it in (interp[0].values() if interp else ()):
            for one in (it if isinstance(it, tuple) else (it,)):
                one.syn = self
                if one.spec:
                    one.spec.syn = self
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
        # Скобки ведут стек _brackets: «)» условия JS, перевод строки в «(» и «[» Groovy.
        firsts |= set(_BRACKETS[regex]) if regex in _BRACKETS else set()
        firsts |= {"`"} if regex == "js" else set()
        firsts |= {"%", '"', "`"} if regex == "ruby" else set()
        firsts |= {"<"} if jsx else set()
        firsts |= {"~"} if sigil else set()
        firsts |= {"?"} if php else set()
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

# Скобки, которые ведёт стек _parse: в JS — «(» и «)» (заголовок if, while, for, with), в Groovy — все три пары
# (перевод строки внутри «(» и «[» лексер Groovy пропускает).
_BRACKETS = {"js": "()", "groovy": "()[]{}"}

class _Interp:
    """Escape строки с подстановками кода («"${f("x")}"» Kotlin, «"$(cmd)"» shell): закрывающую кавычку ищет
    _close_interp, строки в коде подстановки — свои. esc — escape текста строки ("\\", "double", "raw", None; "raw" —
    сырая f-строка Python: «\\» берёт следующий знак, только если это первый знак закрытия или «\\»); subst —
    (открытие подстановки, открывающая скобка или None, закрывающая) — скобки считаются, закрывающая на нулевой
    глубине кончает подстановку; literal — знаки текста, которые подстановку не открывают («{{», «$${»). code_esc —
    «\\» в коде подстановки берёт следующий знак (shell). spec — _Interp текста спецификации формата Python: «:» на
    нулевой глубине скобок кода подстановки открывает этот текст до закрывающей скобки подстановки. syn — синтаксис
    языка (_Syntax ставит сам): по нему узнаются строки в коде подстановки."""

    def __init__(self, esc, subst, literal=(), code_esc=False, spec=None):
        self.esc, self.subst, self.literal, self.syn = esc, subst, literal, None
        self.code_esc, self.spec = code_esc, spec
        self._text, self._code = {}, {}

    def text_re(self, close):
        """Регулярка текста строки с закрытием close: escape-знаки литерала, открытия подстановок, закрытие,
        обратная косая; длинное раньше короткого («\\(» Swift раньше «\\»)."""
        if close not in self._text:
            toks = self.literal + tuple(o for o, _, _ in self.subst) + (close,)
            toks += ("\\",) if self.esc in ("\\", "raw") else ()
            self._text[close] = re.compile("|".join(map(re.escape, toks)))
        return self._text[close]

    def code_re(self, opening, closing):
        """Регулярка кода подстановки: её скобки, первые знаки строк и символьных литералов языка; «\\» при code_esc;
        «:» и скобки «()[]» при spec."""
        key = opening, closing
        if key not in self._code:
            syn = self.syn
            chars = {opening, closing} - {None} | {s[0][0] for s in syn.strings}
            chars |= {"'"} if syn.char else set()
            chars |= {'"'} if syn.raw else set()
            chars |= {"\\"} if self.code_esc else set()
            chars |= set(":()[]") if self.spec else set()
            self._code[key] = re.compile("[" + "".join(re.escape(c) for c in sorted(chars)) + "]")
        return self._code[key]


_SYNTAX = {}


def _add(exts, **kw):
    for ext in exts:
        _SYNTAX[ext] = _Syntax(**kw)


_add(("c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm"),
     line=_SLASH, blocks=_C_BLOCK, strings=(_DQ,), char=True, raw="cpp")
_add(("java",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _DQ), char=True)
# Подстановки строк: «${…}» Kotlin, Groovy, Dart, Terraform; «\\(…)» Swift; «{…}» C# «$"…"» и f-строк Python;
# «#{…}» Elixir; «$(…)», «${…}», «`…`» shell.
_DOLLAR_BRACE = (("${", "{", "}"),)
_KT_INTERP = {'"': _Interp("\\", _DOLLAR_BRACE), '"""': _Interp(None, _DOLLAR_BRACE)}
_SWIFT_INTERP = _Interp("\\", (("\\(", "(", ")"),))
_CS_SUBST = (("{", "{", "}"),)
_CS_INTERP = _Interp("\\", _CS_SUBST, ("{{",))
# Verbatim «$@"…"», «@$"…"»: escape — удвоенная кавычка (_raw_string); в таблице interp C# — под ключом "verbatim",
# не открытием строки.
_CS_VERBATIM_INTERP = _Interp("double", _CS_SUBST, ("{{",))
# f-строки и t-строки Python: (обычная, сырая с «r» в префиксе). Спецификация формата «{x:'>{w}}» — текст без
# escape с подстановками «{…}», у которых тоже бывает спецификация.
_PY_SPEC = _Interp(None, _CS_SUBST)
_PY_SPEC.spec = _PY_SPEC
_PY_INTERP = {q: (_Interp("\\", _CS_SUBST, ("{{",), spec=_PY_SPEC), _Interp("raw", _CS_SUBST, ("{{",), spec=_PY_SPEC))
              for q in ('"', "'", '"""', "'''")}
_PY_PREFIXES = frozenset(("f", "rf", "fr", "t", "rt", "tr"))
_DART_INTERP = _Interp("\\", _DOLLAR_BRACE)
_EX_INTERP = _Interp("\\", (("#{", "{", "}"),))
_SH_INTERP = _Interp("\\", (("$(", "(", ")"), ("${", "{", "}"), ("`", None, "`")), code_esc=True)
_TF_INTERP = _Interp("\\", (("${", "{", "}"), ("%{", "{", "}")), ("$${", "%%{"))
# Блоки «/* */» Kotlin, Scala, Swift, Rust, Dart вкладываются; в C, Java, JS, Go, C#, Groovy — нет. Строка Scala
# без интерполятора («s"…"») подстановок не знает.
_add(("kt", "kts"), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ_RAW, _DQ), char=True, nested=True,
     interp=(_KT_INTERP, None))
_add(("scala",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ_RAW, _DQ), char=True, nested=True)
_add(("swift",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _DQ), raw="swift", nested=True,
     interp=({'"': _SWIFT_INTERP, '"""': _SWIFT_INTERP}, None))
_add(("cs",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ_RAW, _DQ), char=True, raw="cs",
     interp=({'"': _CS_INTERP, "verbatim": _CS_VERBATIM_INTERP}, "cs"))
_add(("rs",), line=_SLASH, blocks=_C_BLOCK, strings=(('"', '"', True, "\\"),), char=True, raw="rust",
     nested=True)
_add(("go",), line=_SLASH, blocks=_C_BLOCK, strings=(("`", "`", True, None), _DQ), char=True)
# Разметка JSX — в jsx, tsx и js (React); в ts «<T>(x) => x» — обобщение, а не тег. Шаблонную строку «`…`»
# разбирает _template.
_JS_STRINGS = (_DQ, _SQ)
_add(("mjs", "cjs", "ts", "mts", "cts"), line=_SLASH, blocks=_C_BLOCK, strings=_JS_STRINGS, regex="js")
_add(("js", "jsx", "tsx"), line=_SLASH, blocks=_C_BLOCK, strings=_JS_STRINGS, regex="js", jsx=True)
_add(("dart",), line=_SLASH, blocks=_C_BLOCK, strings=(_T_DQ, _T_SQ, _DQ, _SQ), nested=True,
     interp=(dict.fromkeys(('"', "'", '"""', "'''"), _DART_INTERP), "dart"))
# Groovy: строка в кавычках продолжается на следующей строке за «\\» в конце строки (multiline "cont").
_add(("groovy", "gradle"), line=_SLASH, blocks=_C_BLOCK,
     strings=(_T_DQ, _T_SQ, ('"', '"', "cont", "\\"), ("'", "'", "cont", "\\")), regex="groovy",
     interp=(dict.fromkeys(('"', '"""'), _Interp("\\", _DOLLAR_BRACE)), None))
# PHP: строки «'…'», «"…"», «`…`» многострочны, «?>» в них — текст.
_add(("php",), line=_SLASH + (("#", "php"),), blocks=_C_BLOCK, strings=(_DQ_ML, _SQ_ML, _BT_ML), heredoc="php",
     shebang=True, php=True)
_add(("proto", "sol"), line=_SLASH, blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("zig",), line=_SLASH, strings=(_DQ,), char=True, zig=True)
_add(("py", "pyi"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), docstring=True, shebang=True,
     interp=(_PY_INTERP, "py"))
# Shell: «$'…'» — строка с escape обратной косой (ANSI-C quoting bash и zsh), «'…'» — без escape.
_add(("sh", "bash", "zsh"), line=(("#", "word"),), strings=(("$'", "'", False, "\\"), _DQ, _SQ_RAW), heredoc="shell",
     shebang=True, interp=({'"': _SH_INTERP}, None))
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
     quote_word=True, shebang=True)
_add(("tf",), line=_H + _SLASH, blocks=_C_BLOCK, strings=(_DQ,), heredoc="tf", interp=({'"': _TF_INTERP}, None))
_add(("nix",), line=_H, blocks=_C_BLOCK, strings=(("''", "''", True, "nix"), ('"', '"', True, "\\")), shebang=True)
_add(("jl",), line=_H, blocks=(("#=", "=#"),), strings=(_T_DQ, _DQ_ML), char=True, shebang=True, nested=True)
_add(("ex", "exs"), line=_H, strings=(_T_DQ, _T_SQ, _DQ, _SQ), shebang=True, prefix=("?", _QUESTION_CHAR, True),
     exdoc=True, sigil=True, interp=(dict.fromkeys(('"', "'", '"""', "'''"), _EX_INTERP), None))
_add(("sql",), line=(("--", "any"),), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("lua",), line=(("--", "any"),), strings=(_DQ, _SQ), lua=True)
_add(("hs",), line=(("--", "any"),), blocks=(("{-", "-}"),), strings=(_DQ,), char=True, nested=True)
_add(("html", "xml"), blocks=(("<!--", "-->"),))
_add(("css",), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(("scss", "sass", "less"), line=(("//", "css"),), blocks=_C_BLOCK, strings=(_DQ, _SQ))
_add(_JSON_NAMES, strings=(_DQ,))
_add(_JSONC_NAMES, line=_SLASH, blocks=_C_BLOCK, strings=(_DQ,))
_add(_GOMOD_NAMES, line=_SLASH, strings=(_DQ, ("`", "`", False, None)))
# Однофайловые компоненты Vue и Svelte: разметка и код выражений в ней; «<script>» и «<style>» разбирает _sfc.
for _name in ("vue", "svelte"):
    _add((_name,), line=_SLASH, blocks=_C_BLOCK, strings=_JS_STRINGS, regex="js", sfc=_name)
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
     raw="nim", nested=True, shebang=True)
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
        # «${#x}» — переменная PowerShell с именем «#x», «{#» без «$» — скобка и комментарий.
        return i == 0 or raw[i - 1] not in "$\\" and raw[i - 2:i] != "${"
    if rule == "php":
        return not raw.startswith("[", i + 1)
    if rule == "css":
        return i == 0 or raw[i - 1] != ":"
    if rule == "start":
        return _back(raw, i, str.isspace) == 0
    if rule == "make":
        if not raw.startswith("\t"):
            # «#» в вызове функции и ссылке на переменную GNU make буквален: «$(shell echo $${#A})».
            return i == 0 or raw[i - 1] not in "${\\"
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
    по длине строки. esc _Interp — строка с подстановками (_close_interp)."""
    if isinstance(esc, _Interp):
        return _close_interp(raw, i, close, esc)
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


def _close_interp(raw, i, close, it):
    """_close для строки с подстановками it (_Interp): стек кадров — строк (закрытие, _Interp) и кода подстановок
    [открывающая скобка, закрывающая, глубина, глубина «()[]» при spec, _Interp строки]; строка в коде подстановки —
    свой кадр или литерал до закрытия (_string_at); «:» на нулевых глубинах при spec меняет кадр кода на кадр текста
    спецификации формата. -1 — строка или подстановка не закрыта в строке файла. Время — линейное по длине строки:
    каждый шаг сдвигает i вперёд."""
    stack = [(close, it)]
    while True:
        top = stack[-1]
        if len(top) == 2:
            closing, cur = top
            m = cur.text_re(closing).search(raw, i)
            if m is None:
                return -1
            tok, i = m.group(), m.end()
            if tok == closing:
                if cur.esc == "double" and raw.startswith(closing, i):
                    i += len(closing)
                    continue
                stack.pop()
                if not stack:
                    return i
            elif tok == "\\":
                if cur.esc == "\\" or raw.startswith((closing[0], "\\"), i):
                    i += 1
            elif tok not in cur.literal:
                stack.append([*next((o, c) for t, o, c in cur.subst if t == tok), 0, 0, cur])
            continue
        cur = top[4]
        m = cur.code_re(top[0], top[1]).search(raw, i)
        if m is None:
            return -1
        k = m.start()
        ch, i = raw[k], k + 1
        if ch == top[1]:
            if not top[2]:
                stack.pop()
            else:
                top[2] -= 1
        elif ch == top[0]:
            top[2] += 1
        elif ch == "\\" and cur.code_esc:
            i = k + 2
        elif cur.spec and ch in "()[]":
            top[3] += 1 if ch in "([" else -1
        elif cur.spec and ch == ":":
            if not top[2] and not top[3]:
                stack[-1] = (top[1], cur.spec)
        else:
            found = _string_at(cur.syn, raw, k)
            if found is None:
                continue
            if found[0] == "skip":
                i = found[1]
                continue
            start, closing, esc = found
            if isinstance(esc, _Interp):
                stack.append((closing, esc))
                i = start
            else:
                i = _close(raw, start, closing, esc)
                if i < 0:
                    return -1


def _string_at(syn, raw, k):
    """Строка, открытая в k кода подстановки, как у _token: (начало текста, закрытие, escape); ("skip", конец) —
    символьный литерал; None — не строка."""
    c = raw[k]
    if c == "'" and syn.char:
        m = _CHAR.match(raw, k)
        return ("skip", m.end()) if m else None
    if c == '"' and syn.raw:
        found = _raw_string(syn.raw, raw, k)
        if found:
            return found[2], found[0], found[1]
    for opening, closing, _, esc in syn.strings:
        if raw.startswith(opening, k):
            return k + len(opening), closing, _interp_at(syn, raw, k, opening) or esc
    return None


def _interp_at(syn, raw, i, opening):
    """_Interp строки с открытием opening в i или None — строка без подстановок. Правило префикса syn.interp: "py" —
    префикс f или t (с r — сырая), "cs" — «$» перед кавычкой, "dart" — не сырая «r'…'»; None — любая такая строка."""
    if syn.interp is None:
        return None
    table, rule = syn.interp
    it = table.get(opening)
    if it is None:
        return None
    if rule == "py":
        k = _back(raw, i, str.isalpha)
        prefix = raw[k:i].lower()
        if prefix not in _PY_PREFIXES or k and _is_word(raw[k - 1]):
            return None
        return it[1] if "r" in prefix else it[0]
    if rule == "cs":
        return it if raw[i - 1:i] == "$" else None
    if rule == "dart" and raw[i - 1:i] == "r" and not (i > 1 and _is_word(raw[i - 2])):
        return None
    return it


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
            # «$@"…"» и «@$"…"» — verbatim с подстановками «{…}».
            return '"', _CS_VERBATIM_INTERP if "$" in raw[max(0, i - 2):i] else "double", i + 1
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
# Встроенные функции и операторы Perl, за которыми лексер Perl ждёт терм: «/» — регулярка, «<<» — heredoc, и через
# пробел, и вплотную (perl -MO=Deparse, Perl 5.42). say, fc, evalbytes — функции только с feature, а feature включают
# и модули: они тут не перечислены.
_PERL_TERM = frozenset((
    "abs", "accept", "alarm", "and", "atan2", "bind", "binmode", "bless", "caller", "chdir", "chmod", "chomp", "chop",
    "chown", "chr", "chroot", "close", "closedir", "cmp", "connect", "cos", "crypt", "dbmclose", "dbmopen", "defined",
    "delete", "die", "do", "each", "elsif", "eof", "eq", "eval", "exec", "exists", "exit", "exp", "fcntl", "fileno",
    "flock", "formline", "ge", "getc", "getgrgid", "getgrnam", "gethostbyaddr", "gethostbyname", "getnetbyaddr",
    "getnetbyname", "getpeername", "getpgrp", "getpriority", "getprotobyname", "getprotobynumber", "getpwnam",
    "getpwuid", "getservbyname", "getservbyport", "getsockname", "getsockopt", "glob", "gmtime", "goto", "grep", "gt",
    "hex", "if", "index", "int", "ioctl", "join", "keys", "kill", "last", "lc", "lcfirst", "le", "length", "link",
    "listen", "localtime", "lock", "log", "lstat", "lt", "map", "mkdir", "msgctl", "msgget", "msgrcv", "msgsnd", "ne",
    "next", "not", "oct", "open", "opendir", "or", "ord", "pack", "pipe", "pop", "pos", "print", "printf", "prototype",
    "push", "quotemeta", "rand", "read", "readdir", "readline", "readlink", "readpipe", "recv", "redo", "ref", "rename",
    "require", "reset", "return", "reverse", "rewinddir", "rindex", "rmdir", "scalar", "seek", "seekdir", "select",
    "semctl", "semget", "semop", "send", "sethostent", "setnetent", "setpgrp", "setpriority", "setprotoent",
    "setservent", "setsockopt", "shift", "shmctl", "shmget", "shmread", "shmwrite", "shutdown", "sin", "sleep",
    "socket", "socketpair", "sort", "splice", "split", "sprintf", "sqrt", "srand", "stat", "study", "substr",
    "symlink", "syscall", "sysopen", "sysread", "sysseek", "system", "syswrite", "tell", "telldir", "tie", "tied",
    "truncate", "uc", "ucfirst", "umask", "undef", "unless", "unlink", "unpack", "unshift", "untie", "until", "utime",
    "values", "vec", "waitpid", "warn", "while", "write", "xor"))
# Встроенные функции Perl без аргументов и литералы __FILE__, __LINE__, __PACKAGE__: за ними лексер Perl ждёт
# оператор — «/» деление, «<<» сдвиг.
_PERL_VALUE = frozenset((
    "endgrent", "endhostent", "endnetent", "endprotoent", "endpwent", "endservent", "fork", "getgrent", "gethostent",
    "getlogin", "getnetent", "getppid", "getprotoent", "getpwent", "getservent", "setgrent", "setpwent", "time",
    "times", "wait", "wantarray", "__FILE__", "__LINE__", "__PACKAGE__"))
# Слова Perl, после которых «<<ID» вплотную — heredoc: _PERL_TERM и say (с feature say — функция вывода).
_PERL_TIGHT = _PERL_TERM | {"say"}
_PERL_HANDLE = re.compile(r"[A-Z_][A-Z0-9_]*")
# Ключевые слова Ruby, за которыми лексер Ruby (EXPR_BEG, EXPR_MID) начинает выражение: «/» — регулярка, «<<» —
# heredoc и вплотную. За _RUBY_VALUE (EXPR_END) — оператор: деление, сдвиг и добавление.
_RUBY_BEG = frozenset(("if", "unless", "while", "until", "and", "or", "when", "in", "elsif", "then", "else", "return",
                       "break", "next", "do", "case", "begin", "rescue", "ensure"))
_RUBY_VALUE = frozenset(("self", "nil", "true", "false", "__FILE__", "__LINE__", "__ENCODING__", "end", "redo",
                         "retry"))


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


@functools.lru_cache(maxsize=1)
def _brace_pairs(raw):
    """{позиция «}»: позиция парной «{»} строки raw: скобки считаются подряд, без учёта строк и литералов. Строка
    разбирается один раз за все «<<» в ней: решение про блок-дескриптор остаётся линейным."""
    pairs, opened = {}, []
    for m in _BRACES.finditer(raw):
        if m.group() == "{":
            opened.append(m.start())
        elif opened:
            pairs[m.start()] = opened.pop()
    return pairs


_BRACES = re.compile(r"[{}]")


def _print_block(raw, j):
    """Стоит ли перед j, на «}», блок-дескриптор Perl после print, printf, say: «{$fh}», «{$DB::OUT}»,
    «{$self->{fh}}», «{*STDOUT}», и вплотную к слову («print{$fh}»); парная «{» — по _brace_pairs."""
    k = _back(raw, j - 1, lambda c: _is_word(c) or c == ":")
    if k < j - 1 and raw[k - 2:k] == "{$":
        return _print_word(raw, _back(raw, k - 2, str.isspace))
    b = _brace_pairs(raw).get(j - 1)
    return b is not None and raw[b + 1:b + 2] in ("$", "*") and _print_word(raw, _back(raw, b, str.isspace))


def _member(raw, k):
    """Стоит ли перед словом, начатым в k, сигил «$», «@», «%», «&», «.», «->» или «::»: это переменная, метод или
    имя пакета, а не встроенное слово языка."""
    return bool(k) and (raw[k - 1] in "$@%&." or raw[k - 2:k] in ("->", "::"))


def _tight_heredoc(raw, i, perl):
    """Стоит ли перед «<<» в i вплотную слово, после которого идёт heredoc: в Perl — из _PERL_TIGHT («print<<EOT»,
    «lc<<EOS») или дескриптор из заглавных и «_» после print, printf, say («print CSS<<EOF»); в Ruby — из
    _RUBY_BEG («return<<EOS»)."""
    k = _back(raw, i, _is_word)
    if k == i or _member(raw, k) or k and raw[k - 1] == ">":
        return False
    word = raw[k:i]
    if not perl:
        return word in _RUBY_BEG
    return word in _PERL_TIGHT or bool(_PERL_HANDLE.fullmatch(word)) and _after_print(raw, k)


def _heredoc_ok(raw, i, m, perl=False, names=frozenset()):
    """Открывает ли «<<» с совпавшим _RUBY_HEREDOC m в позиции i heredoc, а не сдвиг или добавление.

    Сразу после слова или закрывающей скобки — сдвиг («1<<BITS», «a[0]<<X»), кроме слов _tight_heredoc. После
    пробела: за скобкой или кавычкой — терм, сдвиг; за переменной Perl («$a», «@a») — сдвиг. За словом Ruby — как
    лексер Ruby: за числом, локальной переменной (names из _ruby_locals, не после «.») и словом из _RUBY_VALUE —
    сдвиг и добавление, за прочим словом (метод, константа) — heredoc. За словом Perl: из _PERL_TERM — heredoc, из
    _PERL_VALUE — сдвиг; за прочим (своя функция, константа: решают объявления и импорт) — heredoc, если
    идентификатор с «-», «~», в кавычках или с заглавной буквы («foo <<EOF»), иначе сдвиг («a <<b»). За любым
    другим знаком («=», «(», «,») и в начале строки — heredoc.

    perl — ещё heredoc в дескриптор после print, printf, say: «$fh», STDOUT, STDERR через пробел, блок
    «{$fh}», «{$self->{fh}}», «{*STDOUT}» через пробел и вплотную (_print_block); ведущие «_» идентификатора не
    мешают заглавной букве («<<_EOUSAGE_»). В Ruby «print $fh <<EOF» — сдвиг глобальной переменной, там правило не
    действует.
    """
    if not i:
        return True
    if _is_word(raw[i - 1]) or raw[i - 1] in ")]}" or not perl and raw[i - 1] in "?!" and _is_word(raw[i - 2:i - 1]):
        # «foo?<<x» Ruby — сдвиг за методом.
        return _is_word(raw[i - 1]) and _tight_heredoc(raw, i, perl) or perl and raw[i - 1] == "}" and _print_block(
            raw, i)
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
    word = raw[k:j]
    if not perl:
        if word[0].isdigit():
            return False
        return _member(raw, k) or word not in _RUBY_VALUE and word not in names
    if not _member(raw, k) and (word in _PERL_TERM or word in _PERL_VALUE):
        return word in _PERL_TERM
    ident = m.group(3).lstrip("_")
    return bool(m.group(1) or m.group(2) or ident[:1].isupper())


# Слова, после которых начинается выражение: «/» за ними — литерал, «<» — тег.
_EXPR_KEYWORDS = frozenset(("return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
                            "do", "else", "yield", "await"))
_KEYWORD_MAX = max(map(len, _EXPR_KEYWORDS))
# Знаки, после которых начинается выражение; «)», «]», кавычка, «/» и слово — конец значения.
_EXPR_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")


def _is_ident(c):
    return _is_word(c) or c == "$"


def _expr_start(raw, i, fresh=True, known=None):
    """Начинается ли в i выражение: перед i через пробелы — знак из _EXPR_AFTER (кроме «++» и «--» постфикса)
    или слово из _EXPR_KEYWORDS, не свойство после «.»; только пробелы — fresh, то же для конца прошлой строки
    кода. known — {конец токена: начинается ли за ним выражение} строки для токенов, которых не видно по знаку:
    «)» заголовка оператора, блочный комментарий (_parse). Назад — пробелы и не больше _KEYWORD_MAX + 1 знаков
    слова: время — линейное по длине строки."""
    j = _back(raw, i, str.isspace)
    if not j:
        return fresh
    if known and j in known:
        return known[j]
    c = raw[j - 1]
    if _is_ident(c):
        k, lo = j - 1, max(0, j - _KEYWORD_MAX - 1)
        while k > lo and _is_ident(raw[k - 1]):
            k -= 1
        return raw[k:j] in _EXPR_KEYWORDS and not (k and (_is_ident(raw[k - 1]) or raw[k - 1] == "."))
    if c in "+-" and _postfix(raw, j):
        return False
    return c in _EXPR_AFTER


def _postfix(raw, j):
    """Кончается ли перед j «++» или «--»: серия «+» лексер делит на «++» слева направо, «+++» — «++» и «+»."""
    return (j - _back(raw, j, lambda c: c == raw[j - 1])) % 2 == 0


def _js_expr_start(raw, i, fresh=True, known=None):
    """_expr_start для JS: «/» перед i — деление, за ним выражение (конец регулярки и блочного комментария —
    в known); «++» и «--» не меняют ответ токена перед собой (префикс за началом выражения, постфикс за значением),
    в начале строки — префикс (постфикс перед переводом строки запрещён). Назад — серии «+», «-» и пробелы до
    другого токена: время — линейное по длине строки."""
    while True:
        j = _back(raw, i, str.isspace)
        if not j or known and j in known or raw[j - 1] not in "/+-":
            return _expr_start(raw, i, fresh, known)
        if raw[j - 1] == "/" or not _postfix(raw, j):
            return True
        i = _back(raw, j, lambda c: c == raw[j - 1])
        if not _back(raw, i, str.isspace):
            return True


# Ключевые слова Groovy: лексер (GroovyLexer.g4) выдаёт их токенами и не после «.», и «/» за любым из них —
# slashy-строка; this, null, true, false — значения.
_GROOVY_KEYWORDS = frozenset((
    "as", "assert", "boolean", "break", "byte", "case", "catch", "char", "class", "const", "continue", "def", "default",
    "do", "double", "else", "enum", "extends", "final", "finally", "float", "for", "goto", "if", "implements", "import",
    "in", "instanceof", "int", "interface", "long", "native", "new", "package", "permits", "private", "protected",
    "public", "record", "return", "sealed", "short", "static", "strictfp", "super", "switch", "synchronized",
    "threadsafe", "throw", "throws", "trait", "transient", "try", "var", "void", "volatile", "while", "yield"))
_GROOVY_KEYWORD_MAX = max(map(len, _GROOVY_KEYWORDS))


def _groovy_slashy_ok(raw, i, fresh, known):
    """Открывает ли «/» в i slashy-строку Groovy (GroovyLexer.isRegexAllowed): перед i через пробелы — не
    значение: не имя, число, this, null, true, false, не «)», «]», «}», кавычка, «++», «--» и не конец slashy-строки
    (known); только пробелы — fresh. Слово просматривается не дальше _GROOVY_KEYWORD_MAX + 1 знаков."""
    j = _back(raw, i, str.isspace)
    if not j:
        return fresh
    if j in known:
        return known[j]
    c = raw[j - 1]
    if _is_ident(c):
        k, lo = j - 1, max(0, j - _GROOVY_KEYWORD_MAX - 1)
        while k > lo and _is_ident(raw[k - 1]):
            k -= 1
        return raw[k:j] in _GROOVY_KEYWORDS and not (k and _is_ident(raw[k - 1]))
    if c in "+-" and _postfix(raw, j):
        return False
    return c not in ")]}\"'"


_STATEMENT_HEADS = frozenset(("if", "while", "for", "with"))


def _word_before(raw, i, words):
    """Слово из words перед i через пробелы, не свойство после «.»; иначе None. Назад — пробелы и не больше
    длины самого длинного слова + 1 знаков."""
    j = _back(raw, i, str.isspace)
    k, lo = j, max(0, j - max(map(len, words)) - 1)
    while k > lo and _is_ident(raw[k - 1]):
        k -= 1
    word = raw[k:j]
    return word if word in words and not (k and (_is_ident(raw[k - 1]) or raw[k - 1] == ".")) else None


def _brackets(syn, raw, i, stack, known):
    """Скобка в i кода JS или Groovy: «(», «[», «{» кладут на stack, закрывающая снимает. «(» за if, while, for,
    with JS — заголовок оператора: «)» его конец, за ней начинается выражение (known; так acorn). «(» за try
    Groovy — не скобка для перевода строки (GroovyLexer.isInsideParens)."""
    c = raw[i]
    if c in "([{":
        kind = None
        if c == "(":
            kind = _word_before(raw, i, _STATEMENT_HEADS if syn.regex == "js" else _TRY)
        stack.append((c, kind))
    elif stack:
        _, kind = stack.pop()
        if kind in _STATEMENT_HEADS:
            known[i + 1] = True


_TRY = frozenset(("try",))


def _newline_hidden(stack):
    """Пропускает ли лексер Groovy перевод строки: внутри «(» (не try) или «[» — да, внутри «{» и вне скобок —
    нет."""
    return bool(stack) and stack[-1][0] in "([" and stack[-1][1] != "try"


# Регулярное выражение JS: escape, класс «[...]» (в нём «/» не закрывает), флаги. Slashy-строка Groovy: escape.
# Альтернативы начинаются с разных знаков, повторы — без отката: время — линейное по длине строки.
_JS_REGEX = re.compile(r"/(?:[^\\/\[]|\\.|\[(?:[^\\\]]|\\.)*+\])++/\w*")
_SLASHY = re.compile(r"/(?:[^\\/]|\\.)++/")
# Имя тега JSX: «div», «Foo.Bar», «svg:path», «my-elem».
_JSX_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
_JSX_TEXT = re.compile(r"[<{]")
_JSX_TAG = re.compile(r"[{\"'/>]")
_SFC_TEXT = re.compile(r"<|\{")
# В теге Vue «{» — текст: код Vue в атрибутах — в кавычках.
_SFC_TAG = {"vue": re.compile(r"[\"'/>]"), "svelte": _JSX_TAG}


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


def _html_tag(raw, i):
    """Открывает ли «<» в i тег разметки компонента Vue или Svelte: за ним буква ASCII (токенизатор HTML,
    «tag open state»; парсеры Vue и Svelte так же)."""
    c = raw[i + 1:i + 2]
    return c.isascii() and c.isalpha()


# Директивы Vue: «v-if», «v-on:click», «:x», «@click», «#slot», «.prop» — их значение выражение JavaScript.
_VUE_DIRECTIVE = ("v-", ":", "@", "#", ".")


def _vue_directive(raw, i):
    """Стоит ли перед кавычкой в i через пробелы «имя=» директивы Vue (_VUE_DIRECTIVE). Назад — пробелы и имя
    атрибута: время на кавычку — по длине имени."""
    j = _back(raw, i, str.isspace)
    if not j or raw[j - 1] != "=":
        return False
    k = _back(raw, j - 1, str.isspace)
    start = _back(raw, k, lambda c: not c.isspace() and c not in "\"'>/=")
    return raw[start:k].startswith(_VUE_DIRECTIVE)


def _markup(raw, i, ctx, n, banned, sfc=None):
    """Разбор разметки JSX с позиции i: вершина ctx — текст или тег. Действие — как у _token; ctx меняется на
    месте. Элементы стека: ["text", позиция открытия] — дети элемента, ["tag", позиция] — открывающий тег,
    ["close"] — закрывающий тег, ["code", глубина] — код в фигурных скобках.

    sfc — разметка однофайлового компонента Vue или Svelte (как их компиляторы): корень стека — текст всего файла
    (позиция None), тег открывает «<» и буква ASCII (_html_tag), вложенность элементов не ведётся (пустые элементы
    HTML «<br>» без закрытия); «<!-- -->» — комментарий; код — «{{…}}» Vue и «{…}» Svelte (в Svelte и в теге, и в
    строке атрибута — вершина ["tpl", позиция, кавычка, ""] для _template), «{/if}», «{:else}» Svelte — не код;
    значение директивы Vue (_vue_directive) — литерал со второй частью "expr": _parse разбирает его текст как
    JavaScript (_vue_expr); в теге «//» и «/*» — текст."""
    top = ctx[-1]
    if top[0] == "text":
        m = (_SFC_TEXT if sfc else _JSX_TEXT).search(raw, i)
        if m is None:
            return "skip", len(raw)
        i = m.start()
        if sfc and raw.startswith("<!--", i):
            return "open", "-->", None, True, i, i + 4
        if sfc == "vue" and raw[i] == "{":
            if not raw.startswith("{{", i):
                return "skip", i + 1
            ctx.append(["code", 1])
            return "skip", i + 2
        if sfc == "svelte" and raw.startswith(("{/", "{:"), i):
            # Закрытие и продолжение блока Svelte: «{/if}», «{:else}» — до «}».
            j = raw.find("}", i)
            return "skip", len(raw) if j < 0 else j + 1
        if raw[i] == "{":
            ctx.append(["code", 0])
        elif raw.startswith("</", i):
            ctx.append(["close"])
            return "skip", i + 2
        elif (n, i) not in banned and (_html_tag(raw, i) if sfc else _jsx_opens(raw, i)):
            ctx.append(["tag", (n, i)])
        return "skip", i + 1
    m = (_SFC_TAG[sfc] if sfc else _JSX_TAG).search(raw, i)
    if m is None:
        return "skip", len(raw)
    i = m.start()
    c = raw[i]
    if c == "{":
        ctx.append(["code", 0])
        return "skip", i + 1
    if c in "\"'":
        # Строка атрибута — без escape, до первой такой же кавычки, и может занимать несколько строк. Svelte читает код
        # «{…}» и в ней; значение директивы Vue — выражение JavaScript.
        if sfc == "svelte" and (n, i) not in banned:
            ctx.append(["tpl", (n, i), c, ""])
            return "skip", i + 1
        if sfc == "vue" and _vue_directive(raw, i):
            return "open", c, None, False, i, i + 1, None, None, "expr", False
        return "open", c, None, False, i, i + 1
    if c == "/":
        if raw.startswith("//", i) and not sfc:
            return "line", i
        if raw.startswith("/*", i) and not sfc:
            return "open", "*/", None, True, i, i + 2
        if raw.startswith("/>", i) and top[0] == "tag":
            ctx.pop()
            return "skip", i + 2
        return "skip", i + 1
    if sfc:
        ctx.pop()
    elif top[0] == "tag":
        ctx[-1] = ["text", top[1]]
    else:
        ctx.pop()
        if ctx and ctx[-1][0] == "text":
            ctx.pop()
    return "skip", i + 1


# Строки с подстановками: шаблонная строка JS «`…${…}`», строки Ruby «"…#{…}"» и «`…#{…}`», slashy-строка Groovy
# «/…${…}/». Ключ — (кавычка, знак подстановки); в тексте — escape, закрывающая кавычка, открытие подстановки.
_TEMPLATE = {(q, d): re.compile("[" + re.escape(q) + r"\\]|" + re.escape(d) + r"\{")
             for q, d in (("`", "$"), ('"', "#"), ("`", "#"), ("/", "$"))}
# Строка атрибута Svelte в кавычках: текст без escape с кодом «{…}».
_TEMPLATE.update({(q, ""): re.compile(re.escape(q) + r"|\{") for q in "\"'"})


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
        # В slashy-строке Groovy обратная косая — escape только перед «/».
        return "skip", i + 2 if top[2] != "/" or raw.startswith("/", i + 1) else i + 1
    if raw[i] == top[2]:
        ctx.pop()
        return "skip", i + 1
    ctx.append(["code", 0])
    return "skip", i + 1 + len(top[3])


# Открытие блока PHP: «<?php» перед пробелом или концом строки, «<?=», короткое «<?» (не «<?xml»).
_PHP_OPEN = r"<\?(?:php(?=\s|$)|=|(?!xml))"
_PHP_HTML = re.compile(_PHP_OPEN + r"|<!--|<(script|style)(?![\w-])", re.I)
_PHP_STAG = re.compile(_PHP_OPEN + r"|[\"'>]", re.I)
_PHP_RAW = {kind: re.compile(_PHP_OPEN + "|</" + kind + r"(?=[\s/>]|$)", re.I) for kind in ("script", "style")}
# Тип «<script>» с JavaScript; прочий («text/html», «text/template») — данные.
_SCRIPT_TYPE = re.compile(r"""(?<![\w:-])type\s*=\s*["']?([^"'\s>]*)""", re.I)
_JS_TYPES = frozenset(("", "module", "text/javascript", "application/javascript", "application/x-javascript",
                       "text/ecmascript", "application/ecmascript"))


def _php_html(raw, i, ctx, n, out, deadline):
    """Разбор HTML файла PHP с позиции i: вершина ctx — ["html"] — текст, ["stag", тег, [куски атрибутов]] —
    открывающий тег «<script» или «<style» до «>», ["raw", тег, синтаксис или None, первая строка, {номер: [куски
    текста]}] — их содержимое до «</script», «</style». Действие — как у _token; ctx меняется на месте. Открытие
    блока PHP (_PHP_OPEN) кладёт ["php"] — код PHP до «?>» (_code); «<!-- -->» — комментарий. Содержимое «<script>»
    и «<style>», где блок PHP заменён именем «_», разбирается по своему синтаксису (_php_raw_done), как блоки _sfc."""
    top = ctx[-1]
    if top[0] == "html":
        m = _PHP_HTML.search(raw, i)
        if m is None:
            return "skip", len(raw)
        if m.group() == "<!--":
            return "open", "-->", None, True, m.start(), m.end()
        ctx.append(["stag", m.group(1).lower(), []] if m.group(1) else ["php"])
        return "skip", m.end()
    if top[0] == "stag":
        m = _PHP_STAG.search(raw, i)
        if m is None:
            top[2].append(raw[i:])
            return "skip", len(raw)
        k = m.start()
        top[2].append(raw[i:k])
        if raw[k] in "\"'":
            # Значение атрибута — до такой же кавычки в строке, «>» и «<?» в нём не считаются.
            j = raw.find(raw[k], k + 1)
            j = len(raw) if j < 0 else j + 1
            top[2].append(raw[k:j])
            return "skip", j
        if raw[k] == ">":
            kind = top[1]
            ext = "css"
            if kind == "script":
                t = _SCRIPT_TYPE.search("".join(top[2]))
                ext = "mjs" if (t.group(1).lower() if t else "") in _JS_TYPES else None
            ctx[-1] = ["raw", kind, ext, n, {}]
            return "skip", k + 1
        ctx.append(["php"])
        return "skip", m.end()
    body = top[4]
    m = _PHP_RAW[top[1]].search(raw, i)
    body.setdefault(n, []).append(raw[i:len(raw) if m is None else m.start()])
    if m is None:
        return "skip", len(raw)
    if m.group().startswith("<?"):
        # Блок PHP выводит значение: для разбора JS и CSS он — имя (_php_raw_done), а не пустое место: «/<?= $p ?>/g»
        # — не «//g».
        body[n].append(None)
        ctx.append(["php"])
        return "skip", m.end()
    ctx.pop()
    out.extend(_php_raw_done(top, n, deadline))
    return "skip", m.start() + 2


def _php_raw_done(top, last, deadline):
    """Комментарии содержимого «<script>» или «<style>» файла PHP — кадра top (_php_html) — до строки last: строки
    без блоков PHP, разбор — по синтаксису кадра, где на месте блока (None в кусках) стоит имя «_». Текст
    комментария — тот же хвост строки без имени; хвост из одних блоков PHP — не комментарий."""
    _, _, ext, first, body = top
    if ext is None:
        return []
    lines = [body.get(k, ()) for k in range(first, last + 1)]
    text = "\n".join("".join(p if p is not None else "_" for p in pieces) for pieces in lines)
    out = []
    for k, c in _comments(text, ext, deadline):
        # c — хвост строки разбора без пробелов по краям: его начало, сдвинутое на имена до него, — начало хвоста
        # в строке без имён.
        pieces = lines[k - 1]
        start = len("".join(p if p is not None else "_" for p in pieces).rstrip()) - len(c)
        plain, pos = [], 0
        for p in pieces:
            if p is None:
                pos += 1
                continue
            plain.append(p[max(0, start - pos):])
            pos += len(p)
        tail = "".join(plain).strip()
        if tail:
            out.append((first + k - 1, tail))
    return out


def _code(syn, raw, i, ctx, pending, unclosed, n, banned, fresh, names, known):
    """Действие в коде: вне разметки и строк с подстановками, в фигурных скобках внутри них (вершина ctx —
    ["code", глубина]) или в блоке PHP (вершина ["php"]: «?>» снимает её). «`» в JS, «"» и «`» в Ruby, «/» в начале
    выражения Groovy (_groovy_slashy_ok) открывают строку с подстановками, «<» в начале выражения — тег JSX («<<» —
    сдвиг); прочее — _token. known — как у _expr_start."""
    c = raw[i]
    if syn.php and raw.startswith("?>", i) and ctx and ctx[-1][0] == "php":
        # Конец блока PHP: дальше HTML.
        ctx.pop()
        return "skip", i + 2
    if ctx and ctx[-1][0] == "code" and c in "{}":
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
    if (c == "/" and syn.regex == "groovy" and raw[i + 1:i + 2] not in ("/", "*")
            and _groovy_slashy_ok(raw, i, fresh, known)):
        # Без закрытия до конца файла лексер Groovy читает «/» как деление.
        if (n, i) in banned:
            return None
        ctx.append(["tpl", (n, i), "/", "$"])
        return "skip", i + 1
    if syn.jsx and c == "<":
        if raw.startswith("<", i + 1):
            return "skip", i + 2
        if (n, i) not in banned and _js_expr_start(raw, i, fresh, known) and _jsx_opens(raw, i):
            ctx.append(["tag", (n, i)])
            return "skip", i + 1
    return _token(syn, raw, i, pending, unclosed, n, banned, fresh, names, known)


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
# («split/\\s+/», «split / /», «x if /a/»); when Perl — с feature switch.
_TERM_WORDS = {"ruby": _RUBY_BEG, "perl": _PERL_TERM | {"when"}}
# Слова, после которых «/» — деление и вплотную к следующему знаку («time /2», «self /a»).
_VALUE_WORDS = {"ruby": _RUBY_VALUE, "perl": _PERL_VALUE}


def _literal_opens(raw, i, fresh, lang, names=frozenset()):
    """Открывает ли «/» или «%» в i литерал Ruby или Perl (lang): "expr" — в начале выражения (_expr_start; после
    «}» — конец элемента хеша, деление) и после слова из _TERM_WORDS, "arg" — аргументом вызова: после слова через
    пробел и вплотную к следующему знаку («puts /#/», «puts %w(a)»); None — деление и остаток: после слова из
    _VALUE_WORDS («time /2», «self /a»), числа, переменной («$a /2», «@n /2») и локальной переменной Ruby из names не
    после «.» («a = 4» раньше, «a /b»), перед пробелом и перед «=»: в Ruby всегда («a /=2»), в Perl — с пробелом за
    ним («$a /= 2»); символ Ruby «:/», «:%». Слово из _TERM_WORDS и _VALUE_WORDS после «.», «->», «::» и сигила —
    метод или переменная.
    """
    if lang == "ruby" and i and raw[i - 1] == ":" and raw[i - 2:i - 1] != ":":
        return None
    j = _back(raw, i, str.isspace)
    # Имя метода Ruby с «?» или «!» на конце («foo? /a/») — слово, а не тернарный оператор.
    method = lang == "ruby" and j > 1 and raw[j - 1] in "?!" and _is_word(raw[j - 2])
    if not method and (not j or raw[j - 1] != "}") and _expr_start(raw, i, fresh):
        return "expr"
    if not j or not (_is_word(raw[j - 1]) or method):
        return None
    k = _back(raw, j - method, _is_word)
    member = _member(raw, k)
    if raw[k:j] in _TERM_WORDS[lang] and not member:
        return "expr"
    if raw[k:j] in _VALUE_WORDS[lang] and not member:
        return None
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
    строке, у оператора с двумя частями (two) — обе части; ("await", where, конец первой части) — первая часть в
    скобках закрыта, вторая — на следующих строках (_line_rest_empty); ("open", …, then, regex) — продолжается на
    следующих строках, then у первой части оператора с двумя частями — где вторая: "same" — до того же разделителя
    ещё раз, "pair" — со своим разделителем через пробелы (_second_part), "code" — вторая часть в «{}» (_second_part);
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
        if _line_rest_empty(raw, end):
            return "await", where, end
        return _second_part(raw, end, n, banned, where) or ("skip", _FLAGS.match(raw, end).end())
    j = _close(raw, end, opening, "\\")
    if j >= 0:
        return "skip", _FLAGS.match(raw, j).end()
    if where in banned:
        return "skip", len(raw)
    return "open", opening, "\\", False, k, end, None, where


def _line_rest_empty(raw, j):
    """Пусты ли строка с j или в ней за пробелами только комментарий «#»: вторая часть оператора s, tr, y Perl в
    скобках тогда — на следующих строках (perlop: между частями — пробелы и комментарии)."""
    return _REST_EMPTY.match(raw, j) is not None


_REST_EMPTY = re.compile(r"\s*(?:#|\Z)")


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


def _token(syn, raw, i, pending, unclosed, n=0, banned=frozenset(), fresh=True, names=frozenset(), known=None):
    """Разбор с позиции i вне литерала и комментария.

    ("line",) — строчный комментарий до конца строки (у _markup — ("line", начало комментария)); ("skip", j) —
    литерал до j; ("open", закрытие, escape, в вывод ли, начало вывода, конец открытия[, открытие вложенного
    блока или None[, позиция открытия литерала (n, i)[, вторая часть, регулярка ли — _quote_parts]]]) —
    многострочный блок или литерал; None — обычный символ.
    unclosed — открытия однострочных литералов, не закрытых в этой строке; _token дополняет его. n — номер
    строки; heredoc с позицией (n, i) из banned не открывается, многострочный литерал с ней — однострочный.
    fresh — начинается ли выражение в начале строки (_expr_start); known — как у _expr_start.
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
        # «-s» и знак не слова за ним — файловый тест размера («-s($f)»), не замена: так toke.c читает «-» с буквой
        # файлового теста; «--s» — декремент и «s». Прочие однобуквенные операторы-кавычки файловыми тестами не бывают.
        if m and m.group() == "s" and raw[i - 1:i] == "-" and not _postfix(raw, i):
            m = None
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
        if m and not (spaced and syn.heredoc == "ruby") and _heredoc_ok(raw, i, m, syn.heredoc == "perl", names):
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
    if syn.regex == "js" and c == "/" and _js_expr_start(raw, i, fresh, known):
        # «//» и «/*» выше — комментарии. Незакрытая в строке регулярка кончается с ней: мнимая регулярка не прячет
        # следующие строки.
        m = _JS_REGEX.match(raw, i)
        return "skip", m.end() if m else len(raw)
    if syn.regex == "groovy" and raw.startswith("$/", i) and not (i and _is_ident(raw[i - 1])) and (n, i) not in banned:
        # Dollar-slashy лексер Groovy открывает в любом месте выражения, «a$/» — имя «a$» и деление; без закрытия до
        # конца файла «$» — имя.
        return "open", "/$", "dollar", False, i, i + 2, None, (n, i)
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
            esc = _interp_at(syn, raw, i, opening) or esc
            # Однострочный литерал, не закрытый от кавычки, не закрывается и от следующих таких же кавычек строки:
            # каждая из них экранирована, и хвост строки не просматривается заново. Исключение — пустой литерал из пары
            # «''» при escape "double", комментария в нём нет.
            if multiline is not True and opening in unclosed:
                return None
            if multiline == "cont":
                j = _close(raw, i + len(opening), closing, esc)
                if j >= 0:
                    return "skip", j
                # Нечётная серия «\» в конце строки — продолжение строки, чётная — escape самих косых.
                multiline = (len(raw) - _back(raw, len(raw), lambda c: c == "\\")) % 2
            if multiline and (n, i) in banned and len(opening) > 1:
                # Тройная кавычка без закрытия до конца файла — пустая строка и кавычка, как у лексера с
                # длиннейшим совпадением (Groovy: «"""» — «""» и «"»).
                continue
            if multiline and (n, i) not in banned:
                doc = _docstring_start(raw, i) if syn.docstring else None
                return ("open", closing, esc, doc is not None, i if doc is None else doc, i + len(opening), None,
                        (n, i))
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


def _yaml_key(code, keys, uses, waiting=None, ready=None):
    """Ключ отображения в начале кода строки code YAML или None; keys — стек (колонка ключа, ключ) открытых
    отображений: ключ снимает со стека ключи с колонкой не меньше своей и ложится на него. uses — {колонка:
    значение ключа uses} открытых отображений: ключ снимает записи глубже себя, элемент последовательности «- » —
    и запись своей колонки. waiting — {колонка: [строки скрипта]} входов «script» под «with:», чей шаг ещё без
    uses (_yaml_script_ext); ключ uses их колонки и конец их отображения переносят их в ready [(строки, синтаксис)]."""
    m = _YAML_KEY.match(code)
    if m is None:
        return None
    col, key = len(m.group(1)), next(g for g in m.groups()[1:] if g is not None)
    while keys and keys[-1][0] >= col:
        keys.pop()
    keys.append((col, key))
    for c in [c for c in uses if c > col or c == col and "-" in m.group(1)]:
        del uses[c]
    for c in [c for c in waiting or () if c > col or c == col and "-" in m.group(1)]:
        ready.extend((lines, "sh") for lines in waiting.pop(c))
    if key == "uses":
        uses[col] = code[m.end():].strip().strip("\"'")
        if waiting and col in waiting:
            ext = "js" if _GITHUB_SCRIPT.match(uses[col]) else "sh"
            ready.extend((lines, ext) for lines in waiting.pop(col))
    return key


def _yaml_parent(keys, col):
    """Ключ, которому принадлежит элемент последовательности с «-» в колонке col: последний ключ стека keys с
    колонкой не больше col (список без отступа — в колонке ключа); ключи глубже снимаются."""
    while keys and keys[-1][0] > col:
        keys.pop()
    return keys[-1][1] if keys else None


def _yaml_script_ext(keys, uses):
    """Синтаксис скрипта в блочном скаляре ключа на вершине стека keys (_yaml_key): "js" — вход «script» под
    «with:» шага с actions/github-script (uses — _yaml_key), ("with", колонка «with») — такой вход шага, чей uses
    ещё не встречен (ключи шага идут в любом порядке), "sh" — ключ из _YAML_SCRIPT_KEYS (у ключа с пространством
    имён — последняя часть), None — данные."""
    if not keys:
        return None
    key = keys[-1][1].rsplit(".", 1)[-1]
    if key == "script" and len(keys) > 1 and keys[-2][1] == "with":
        col = keys[-2][0]
        if col not in uses:
            return "with", col
        if _GITHUB_SCRIPT.match(uses[col]):
            return "js"
    return "sh" if key in _YAML_SCRIPT_KEYS else None


def _yaml_script(lines, ext, deadline):
    """Комментарии скрипта блочного скаляра YAML lines [(номер строки, строка)] по синтаксису ext, без общего
    отступа строк: [(номер строки, комментарий)]."""
    body = [raw for _, raw in lines]
    indent = min((len(r) - len(r.lstrip(" ")) for r in body if r.strip()), default=0)
    script = "\n".join(r[indent:] for r in body)
    found = _shell_comments(script, deadline) if ext == "sh" else None
    if found is None:
        found = _parse(script, _SYNTAX[ext], deadline, frozenset())[0]
    return [(lines[k - 1][0], c) for k, c in found]


def _yaml_done(scalar, waiting, out, deadline):
    """Закончен блочный скаляр-скрипт scalar (_parse): комментарии — в out, вход «script» шага без uses — в
    waiting до его uses (_yaml_key)."""
    if isinstance(scalar[2], tuple):
        waiting.setdefault(scalar[2][1], []).append(scalar[1])
    else:
        out.extend(_yaml_script(scalar[1], scalar[2], deadline))


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
# Повторный разбор вторых частей s{…}{…}e Perl как кода: всех вместе — не больше _CODE_BUDGET знаков на знак файла
# (разбор линеен при любой вложенности), вложенность — не глубже _MAX_CODE_DEPTH (предел рекурсии Python).
_CODE_BUDGET = 4
_MAX_CODE_DEPTH = 64


# Блоки однофайлового компонента: комментарий разметки и открывающие теги «<script>», «<style>»; атрибуты тега — до
# «>» вне кавычек («generic="T extends Record<K, V>"»); содержимое — до «</script», «</style» (сырой текст HTML, и
# внутри строк JS).
_SFC_BLOCK = re.compile(r"<!--|<(script|style)(?![\w-])", re.I)
_SFC_ATTRS = re.compile(r"""(?:[^>"']|"[^"]*"|'[^']*')*+>""")
_SFC_LANG = re.compile(r"""(?<![\w:-])lang[ \t\n]*=[ \t\n]*["']?([\w-]*)""", re.I)
_SFC_END = {"script": re.compile(r"</script(?=[\s/>])", re.I), "style": re.compile(r"</style(?=[\s/>])", re.I)}
# Синтаксис содержимого блока по атрибуту lang; без lang — JavaScript-модуль без JSX и CSS. Неизвестный lang
# (coffee, pug) — блок без комментариев.
_SFC_LANGS = {"script": {"": "mjs", "js": "mjs", "javascript": "mjs", "ts": "ts", "typescript": "ts", "jsx": "jsx",
                         "tsx": "tsx"},
              "style": {"": "css", "css": "css", "postcss": "css", "scss": "scss", "sass": "sass", "less": "less",
                        "stylus": "scss", "styl": "scss"}}


def _sfc(text, syn, deadline):
    """Комментарии однофайлового компонента Vue или Svelte: вне «<script>» и «<style>» — разметка (комментарии
    «<!-- -->», «//» в тексте — текст), их содержимое — по синтаксису из lang (_SFC_LANGS). Номера строк — по
    всему файлу."""
    # pos — начало ещё не взятой в разметку части текста, scan — конец разобранной (комментарий разметки остаётся в
    # ней и не открывает блоков), line — номер строки перед pos с нуля.
    markup, out, pos, scan, line = [], [], 0, 0, 0
    for m in _SFC_BLOCK.finditer(text):
        if m.start() < scan:
            continue
        if m.group(1) is None:
            end = text.find("-->", m.end())
            if end < 0:
                break
            scan = end + 3
            continue
        attrs = _SFC_ATTRS.match(text, m.end())
        if attrs is None:
            # Тег без «>» до конца файла — не тег.
            break
        kind = m.group(1).lower()
        lang = _SFC_LANG.search(text, m.end(), attrs.end() - 1)
        sub = _SFC_LANGS[kind].get(lang.group(1).lower() if lang else "")
        close = _SFC_END[kind].search(text, attrs.end())
        end = close.start() if close else len(text)
        line += text.count("\n", pos, attrs.end())
        markup.append(text[pos:attrs.end()])
        body = text[attrs.end():end]
        if sub is not None:
            out.extend((line + n, c) for n, c in _comments(body, sub, deadline))
        # Строки содержимого остаются в разметке пустыми: номера строк разметки не сдвигаются.
        markup.append("\n" * body.count("\n"))
        line += body.count("\n")
        pos = scan = end
    markup.append(text[pos:])
    out.extend(_reparse("".join(markup), syn, deadline))
    return sorted(out, key=lambda e: e[0])


# Метка Perl одна на строке: «FOO:» («FOO::» — имя пакета).
_PERL_LABEL = re.compile(r"[ \t]*[A-Za-z_]\w*[ \t]*:")


def _statement_end(raw, j):
    """Кончается ли перед j оператор Perl: «;», «{», «}» или метка — за ними лексер Perl ждёт новый оператор. «{» и
    «}» анонимного хеша и анонимной функции тоже приняты за блок: их без разбора языка не отличить."""
    return raw[j - 1] in ";{}" or bool(_PERL_LABEL.fullmatch(raw, 0, j))


def _comments(text, ext, deadline=None):
    """[(номер строки с 1, строка комментария)]; номер считается по «\\n», как в git diff. TimeoutError, если
    срок deadline (time.monotonic) прошёл до конца разбора. Heredoc Ruby, Perl, PHP и Terraform без терминатора
    до конца файла — не heredoc: разбор повторяется без него, и тело читается как код. Так же тег JSX без
    закрытия до конца файла — не тег, а строка с подстановками и многострочный литерал (строка, оператор-кавычка
    Perl, литерал «%» Ruby, сигил Elixir) — однострочные: разбор повторяется без всех незакрытых открытий."""
    syn = _SYNTAX.get(ext.lower())
    if syn is None:
        return []
    if ext.lower() in _TREE_SHELL:
        found = _shell_comments(text, deadline)
        if found is not None:
            return found
    if syn.sfc:
        return _sfc(text, syn, deadline)
    return _reparse(text, syn, deadline)


# Расширения файлов, чьи комментарии даёт разбор bash (shparse); zsh разбирает прежний движок.
_TREE_SHELL = frozenset(("sh", "bash"))


def _shell_comments(text, deadline=None):
    """Комментарии скрипта bash по разбору shparse: [(номер строки с 1, строка комментария)], как у _parse; None —
    разбор не годится и комментарии даёт прежний движок: bash прекращает чтение на фатальной синтаксической
    ошибке, и комментарии после неё выпали бы из вывода (отказ судьи без них дороже лишней строки), либо shparse
    упал. Перевод строки «\\r\\n» снимается, как у _parse (файл с CRLF), BOM в начале — тоже; «#!» первой строки —
    не комментарий. Срок deadline проверяется до и после разбора (shparse линеен по длине текста)."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n")
    try:
        script = shparse.parse(text)
    except Exception:  # noqa: BLE001
        return None
    if script.error is not None and script.error.fatal:
        return None
    out, line, at = [], 1, 0
    for start, end in script.comments:
        if start == 0 and text.startswith("#!"):
            continue
        line += text.count("\n", at, start)
        at = start
        out.append((line, text[start:end].strip()))
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError
    return out


def _reparse(text, syn, deadline):
    """Комментарии text по синтаксису syn (_parse), с повторами разбора без незакрытых открытий (_comments)."""
    banned = frozenset()
    out, open_ = _parse(text, syn, deadline, banned)
    for _ in range(_MAX_REPARSE - 1):
        if not open_ or syn.heredoc == "shell":
            break
        banned |= {where for _, _, where in open_}
        out, open_ = _parse(text, syn, deadline, banned)
    return out


def _parse(text, syn, deadline, banned, level=0, budget=None):
    """(комментарии, открытия без закрытия к концу файла: heredoc, теги JSX, строки с подстановками, многострочные
    литералы); banned — позиции открытий, не открывающихся. level — вложенность text во вторые части s{…}{…}e Perl,
    budget — [остаток знаков их повторного разбора] на весь файл, _CODE_BUDGET·len(text) у внешнего разбора: часть
    длиннее остатка и глубже _MAX_CODE_DEPTH не разбирается кодом, и разбор остаётся линейным."""
    out = []
    if budget is None:
        budget = [_CODE_BUDGET * len(text)]
    # Открытый многострочный блок или литерал: (закрытие, escape, идут ли его строки в вывод, открытие
    # вложенного блока или None, глубина вложенности, позиция открытия литерала для отката или None, где вторая
    # часть оператора Perl s, tr, y — _quote_parts — или None, регулярное выражение ли с комментариями /x). Вторая
    # часть "await" — первая часть в скобках закрыта, вторая — на следующих строках (_line_rest_empty).
    state = None
    # Открытые heredoc: (терминатор, как сравнивать строку, позиция «<<» или None): тело heredoc — данные.
    pending = []
    # Конец открытого блока из целых строк (line_block) или None.
    line_block = None
    # Стек разметки JSX (_markup), строк с подстановками (_template) и HTML файла PHP (_php_html); пуст — код вне них.
    ctx = [["text", None]] if syn.sfc else [["html"]] if syn.php else []
    # Открытый блочный скаляр YAML: (отступ родителя, его строки [(номер, строка)] у скрипта или None у данных,
    # синтаксис скрипта — _yaml_script_ext) или None; стек ключей открытых отображений YAML, значения их ключей
    # uses, входы «script» шагов без uses и решённые из них (_yaml_key).
    scalar, keys, uses, waiting, ready = None, [], {}, {}, []
    # Прошла ли строка __END__ или __DATA__ Ruby и Perl: дальше данные, кроме блоков line_block.
    data = False
    # Начинается ли выражение в начале строки: в JS и Perl — по концу прошлой строки кода («a\n/ b» — деление);
    # в Groovy — так же внутри «(» и «[» (_newline_hidden), иначе конец строки завершает оператор, как в Ruby.
    fresh = True
    # Стек открытых скобок кода JS и Groovy (_brackets); начинается ли выражение за открытым блочным комментарием —
    # по токену перед ним (_expr_start, _groovy_slashy_ok).
    stack, before = [], None
    starts_expr = {"groovy": _groovy_slashy_ok, "js": _js_expr_start}.get(syn.regex, _expr_start)
    # Номера в out строк, чей вывод начат комментарием /x открытой многострочной регулярки (_XRE_COMMENT): без
    # флага x после закрытия литерала они снимаются; None — регулярки нет или решение принято. Строки второй части
    # s{…}{…} Perl, открытой в прошлых строках, — [(номер, текст)] или None.
    xre, body = None, None
    # Локальные переменные Ruby, введённые строками до текущей и ею (_ruby_locals): «/» и «%» после них — деление.
    names = set()
    # Ждёт ли Perl в начале строки новый оператор (toke.c, PL_expect == XSTATE): только там «=слово» открывает POD,
    # посреди оператора «\n=shift» — присваивание. Решает конец прошлой строки кода (_statement_end); в Ruby
    # «=begin» с первой колонки — комментарий в любом месте кода.
    stmt = True
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
                _yaml_done(scalar, waiting, out, deadline)
            scalar = None
        if line_block:
            if raw.strip():
                out.append((n, raw.strip()))
            if line_block.match(raw):
                line_block = None
            continue
        # Внутри строки с подстановками «=begin» — текст строки, в коде подстановки «#{…}» — комментарий (ruby 3.4).
        if (state is None and syn.line_block and not (ctx and ctx[-1][0] != "code") and syn.line_block[0].match(raw)
                and (stmt or data)):
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
        # Конец кода строки: начало строчного комментария или конец строки; {конец токена: начинается ли за ним
        # выражение} для токенов строки, которых не видно по знаку (_expr_start).
        cut, known = len(raw), {}
        while i < len(raw):
            tick()
            # Закрыт ли в этом шаге литерал или найдена вторая часть оператора s, tr, y Perl: then и act решают
            # про флаги и вторую часть.
            closed = False
            if state and state[6] == "await":
                # Вторая часть s{…}{…}, tr{…}{…} Perl — первый знак не пробел; «#» перед ней — комментарий.
                j = len(raw) - len(raw[i:].lstrip())
                if j == len(raw):
                    break
                if raw[j] == "#":
                    i, act = j, ("line",)
                else:
                    i, then, where, state, closed = j, "pair", state[5], None, True
                    act = _second_part(raw, j, n, banned, where)
            elif state:
                if state[3]:
                    j, depth = _close_nested(raw, i, state[3], state[0], state[4], state[1])
                else:
                    j, depth = _close(raw, i, state[0], state[1]), 0
                if state[7] and start is None:
                    m = _XRE_COMMENT.search(raw, i, j - len(state[0]) if j >= 0 else len(raw))
                    if m:
                        start, xre_here = m.start(), True
                if state[6] in ("code", "expr"):
                    body.append((n, raw[i:j - len(state[0]) if j >= 0 else len(raw)]))
                if j < 0:
                    state = state[:4] + (depth,) + state[5:] if state[3] else state
                    break
                closing, esc, emit, _, _, where, then, _ = state
                i, state, closed = j, None, True
                if emit and syn.regex in _BRACKETS:
                    # Блочный комментарий прозрачен: за ним решает токен перед ним.
                    known[j] = before
                if syn.regex in ("ruby", "perl") and then in (None, "code"):
                    i = _FLAGS.match(raw, j).end()
                if then == "same":
                    # Вторая часть оператора Perl «s/…/…/» — до того же разделителя.
                    state = (closing, esc, False, None, 1, where, None, False)
                    continue
                if then == "pair" and _line_rest_empty(raw, j):
                    state = (None, None, False, None, 1, where, "await", False)
                    continue
                act = _second_part(raw, j, n, banned, where) if then == "pair" else None
            else:
                top = ctx[-1][0] if ctx else None
                if top == "tpl":
                    depth, slashy = len(ctx), ctx[-1][2] == "/"
                    act = _template(raw, i, ctx)
                    if slashy and len(ctx) < depth:
                        # Закрытая slashy-строка — значение, хотя кончается знаком «/».
                        known[act[1]] = False
                elif top in ("html", "stag", "raw"):
                    act = _php_html(raw, i, ctx, n, out, deadline)
                elif top not in (None, "code", "php"):
                    act = _markup(raw, i, ctx, n, banned, syn.sfc)
                else:
                    m = (syn.code_starts if top == "code" else syn.starts).search(raw, i)
                    if m is None:
                        break
                    i = m.start()
                    if syn.regex in _BRACKETS and raw[i] in _BRACKETS[syn.regex] and not (ctx and raw[i] in "{}"):
                        _brackets(syn, raw, i, stack, known)
                        act = None
                    else:
                        act = _code(syn, raw, i, ctx, pending, unclosed, n, banned, fresh, names, known)
                        if syn.regex == "js" and raw[i] == "/" and act is not None and act[0] == "skip":
                            # Регулярка — значение, хотя кончается знаком «/».
                            known[act[1]] = False
            if closed:
                # Флаги за последним разделителем литерала; None — вторая часть оператора не закрыта в этой строке
                # или не найдена в ней.
                flags = None
                if then in (None, "code"):
                    flags = _FLAGS.match(raw, j).group()
                elif act is not None and act[0] == "skip":
                    flags = raw[_back(raw, act[1], str.isalpha):act[1]]
                if then == "expr":
                    out.extend(_vue_expr(body, deadline))
                    body = None
                if then == "code":
                    code = "\n".join(t for _, t in body)
                    if "e" in flags and level < _MAX_CODE_DEPTH and budget[0] >= len(code):
                        budget[0] -= len(code)
                        found = _parse(code, syn, deadline, frozenset(), level + 1, budget)[0]
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
            if act is None:
                i += 1
            elif act[0] == "line":
                cut = act[1] if len(act) > 1 else i
                if start is None:
                    start = cut
                k = raw.find("?>", cut) if syn.php and ctx[-1][0] == "php" else -1
                if k < 0:
                    break
                # «?>» кончает строчный комментарий PHP: вывод — хвост строки, разбор идёт дальше в HTML.
                cut, i = len(raw), k
            elif act[0] == "skip":
                i = act[1]
            elif act[0] == "await":
                state, i = (None, None, False, None, 1, act[1], "await", False), act[2]
            else:
                _, closing, esc, emit, at, end, *rest = act
                if emit and start is None:
                    start = at
                if emit and syn.regex in _BRACKETS:
                    before = starts_expr(raw, at, fresh, known)
                rest += [None] * (4 - len(rest))
                state, i = (closing, esc, emit, rest[0], 1, *rest[1:]), end
                if rest[3]:
                    xre = []
                if rest[2] in ("code", "expr"):
                    body = []
        if start is not None and raw[start:].strip():
            out.append((n, raw[start:].strip()))
            if xre_here and xre is not None:
                xre.append(len(out) - 1)
        if syn.block_scalar and state is None:
            key = _yaml_key(raw[:cut], keys, uses, waiting, ready)
            for lines, ext in ready:
                out.extend(_yaml_script(lines, ext, deadline))
            ready.clear()
            parent = _yaml_block(raw[:cut], keys)
            if parent is not None:
                if key is None:
                    _yaml_parent(keys, parent)
                ext = _yaml_script_ext(keys, uses)
                scalar = (parent, [] if ext else None, ext)
        if syn.regex in ("js", "perl", "groovy") and state is None and not (ctx and ctx[-1][0] != "code"):
            # Блочный комментарий в конце строки прозрачен (known); строка из одних комментариев и пробелов конец кода
            # не меняет.
            j = _back(raw, cut, str.isspace)
            end = starts_expr(raw, j, fresh, known) if j else fresh
            fresh = end if syn.regex != "groovy" or _newline_hidden(stack) else True
        if syn.regex == "perl" and state is None:
            j = _back(raw, cut, str.isspace)
            if j:
                stmt = _statement_end(raw, j)
        if syn.heredoc == "shell":
            pending = [(term, "tabs" if tabs else "exact", None) for term, tabs in shparse.heredocs(raw)]
    if scalar is not None and scalar[1]:
        _yaml_done(scalar, waiting, out, deadline)
    if syn.block_scalar:
        # Входы «script» шагов без uses до конца файла — shell; их строки — раньше уже выведенных.
        for lines in waiting.values():
            for part in lines:
                out.extend(_yaml_script(part, "sh", deadline))
        out.sort(key=lambda e: e[0])
    if syn.php:
        # «<script>» и «<style>» без закрытия — до конца файла; вывод содержимого и строк PHP — по номерам строк.
        for e in ctx:
            if e[0] == "raw":
                out.extend(_php_raw_done(e, n, deadline))
        out.sort(key=lambda e: e[0])
    open_ = [(None, "ctx", e[1]) for e in ctx if e[0] in ("tag", "text", "tpl") and e[1] is not None]
    if state and state[5] and state[6] != "await":
        open_.append((None, "literal", state[5]))
    return out, pending + open_


def _vue_expr(body, deadline):
    """Комментарии значения директивы Vue: строки body [(номер, текст)], разбор — модуль JavaScript. Сущности HTML
    раскрыты, как у компилятора Vue; строка, где раскрытие дало бы перевод строки («&#10;»), — без раскрытия, чтобы
    номера строк не сдвинулись."""
    lines = []
    for _, t in body:
        u = html.unescape(t)
        lines.append(t if "\n" in u or "\r" in u else u)
    found = _parse("\n".join(lines), _SYNTAX["mjs"], deadline, frozenset())[0]
    return [(body[k - 1][0], c) for k, c in found]


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
    """(текст файла, первые номера строк git по порядку строк текста и номер за последней или None — совпадают с
    ними): UTF-16 и UTF-32 — по BOM, прочее — UTF-8; не прочитан — пустой текст. git считает строки по байту «\\n»,
    а в UTF-16 и UTF-32 этот байт входит и в другие знаки: строка текста k занимает git-строки
    [номера[k - 1], номера[k])."""
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
            lines.append(lines[-1] + body.count(b"\n", pos) + 1)
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
            if wanted is not None and wanted.isdisjoint(range(numbers[n - 1], numbers[n]) if numbers else (n,)):
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
