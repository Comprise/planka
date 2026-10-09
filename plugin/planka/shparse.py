"""Разбор текста команды так, как его читает bash 5.3 в обёртке инструмента Bash Claude Code.

Обёртка исполняет команду через `eval` после `shopt -u extglob`, алиасы не раскрываются (снимок оболочки ставит
`shopt -u expand_aliases`). Разбор повторяет parse.y bash 5.3 и отдаёт одно дерево: команды, слова, подстановки с
телом, heredoc, комментарии. bash не вызывается.

Устройство повторяет bash:

- Ввод читается построчно (shell_getc): `\\` с переводом строки снимается там, где его снимает bash; тела heredoc
  берутся с первой непрочитанной строки ввода (read_secondary_line через yy_getc), а не из текущей строки: текущая
  строка дочитывается после них. Остаток строки терминатора, вернувшийся во ввод (make_here_document, правило
  shell_eof_token), и текст `((…)`, который оказался подоболочкой (parse_dparen, push_string), читаются раньше
  остатка текущей строки — стек кусков ввода `_R.segs`.
- Лексер (read_token, read_token_word, parse_matched_pair, parse_comsub) зависит от двух последних лексем
  (last_read_token, token_before_that) и флагов parser_state; грамматика — нисходящий разбор правил parse.y с той же
  очередностью чтения лексем, что у bison (лексема-предпросмотр читается, когда правилу она нужна).
- Тело `$(…)`, `${ …; }`, `${| …; }`, `<(…)`, `>(…)` разбирается сразу тем же разбором с другим знаком конца
  (shell_eof_token, parse_comsub); синтаксическая ошибка в нём — ошибка всей команды. Тело `` `…` ``, `$((…) …)`,
  подстановки в теле heredoc с терминатором без кавычек и в `'…'` арифметики bash разбирает при раскрытии
  (command_substitute, xparse_dolparen) — здесь отдельным разбором той же строки; их ошибка остаётся в их теле.
- `$(…)` bash хранит напечатанной (print_comsub) и при раскрытии слова разбирает заново без стека разделителей.
  Расходится с чтением только `\\` в словах скобок массива внутри подстановки: при чтении разделитель — `(`, `{`
  подстановки или `"` вокруг неё, и `\\` не экранирует (read_token_word); при раскрытии — экранирует. Такое слово
  раскрывается заново по своему тексту (`_reexpand_word`); его границу задаёт чтение.
- Рекурсии Python по глубине входа нет: разбор написан генераторами, вложенный вызов — `yield генератор`, стек
  вызовов ведёт `_run`. Разбор, `walk` и `simple_commands` линейны по длине текста; исключения, как в bash, —
  цепочки heredoc в подстановке в теле heredoc (строки тела внутреннего heredoc перечитываются на каждом уровне) и
  вложенные подстановки, чьи слова скобок массива с `\\` при чтении и при раскрытии делятся по-разному (`\\'`
  открывает кавычку только при чтении): каждый уровень раскрытия читает весь вложенный текст заново.

Синтаксическая ошибка (`Script.error`): `fatal=True` — bash прекращает чтение (команды до неё исполнены, её команда и
следующие — нет); `fatal=False` — ошибка скобок присваивания массива (parse_compound_assignment): bash отбрасывает
команду и остаток физической строки (reset_parser) и читает со следующей строки (`Script.dropped`). Тела heredoc,
уже прочитанные с той строки, остаются прочитанными; heredoc после ошибки не открыт, его тело читается командами.
"""

import bisect
import re

# Символы, по parse.y и syntax.h bash 5.3.
_BLANK = " \t"
_META = frozenset("()<>;&|")
_BREAK = frozenset("()<>;&| \t\n")
_QUOTE = frozenset("'\"`")
_FUNSUB = frozenset(" \t\n|")
_DIGITS = frozenset("0123456789")
_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME_CHAR = _NAME_START | _DIGITS
_SPECIAL_PARAM = frozenset("@*#?-!$0123456789")
# Символы, перед которыми `\\` снимается в "…" (slashify_in_quotes) и в теле heredoc (slashify_in_here_document).
_DQ_ESC = frozenset("\\`$\"\n")
_HD_ESC = frozenset("\\`$")
_PATTERN_CHAR = frozenset("*?+@!")

# parser_state (parser.h).
_CASEPAT = 0x1
_ALEXPNEXT = 0x2
_ALLOWOPNBRC = 0x4
_SUBSHELL = 0x20
_CMDSUBST = 0x40
_CASESTMT = 0x80
_CONDCMD = 0x100
_CONDEXPR = 0x200
_EXTPAT = 0x1000
_COMPASSIGN = 0x2000
_ASSIGNOK = 0x4000
_EOFTOKEN = 0x8000
_REGEXP = 0x10000
_REDIRLIST = 0x80000
_NOEXPAND = 0x400000
_FUNSUBST = 0x1000000
_CMDBLTIN = 0x2000000

# Флаги parse_matched_pair.
_P_FIRSTCLOSE = 0x1
_P_ALLOWESC = 0x2
_P_DQUOTE = 0x4
_P_COMMAND = 0x8
_P_ARRAYSUB = 0x20
_P_DOLBRACE = 0x40
_P_ARITH = 0x80
# Своё: текст тела heredoc без кавычек у терминатора (`"` не особый, `\\` снимается перед slashify_in_here_document).
_P_HEREDOC = 0x100
# Своё: строка, которую bash раскрывает целиком при исполнении (тело heredoc, '…' арифметики): ошибка разбора
# подстановки в ней кончает раскрытие, а не разбор команды.
_P_TOP = 0x200
# Своё: `` `…` `` прямо в "…": bash снимает в её теле и `\\` перед `"` (string_extract_double_quoted); в `${…}`
# внутри "…" и в теле heredoc — нет (de_backslash).
_P_BQDQ = 0x400
# Своё: `${…}` внутри образца `${x#…}`, `${x/…}` в "…": его слово раскрывается не как в "…" (процесс-подстановка в
# значении исполняется).
_P_UNQ = 0x800
# Своё: конструкция внутри "…" того же слова (раскрытие как в "…").
_P_INDQ = 0x1000

# dolbrace_state (parser.h).
_DB_PARAM = 1
_DB_OP = 2
_DB_WORD = 4
_DB_QUOTE = 0x40
_DB_QUOTE2 = 0x80

_RESERVED = {"if", "then", "else", "elif", "fi", "case", "esac", "for", "select", "while", "until", "do", "done",
             "in", "function", "time", "{", "}", "!", "[[", "]]", "coproc"}
# reserved_word_acceptable: после этих лексем слово в позиции команды.
_RESWORD_OK = frozenset({"\n", ";", "(", ")", "|", "&", "{", "}", "&&", "ARITH_CMD", "!", "|&", "]]", "do", "done",
                         "elif", "else", "esac", "fi", "if", "||", ";;", ";&", ";;&", "then", "time", "TIMEOPT",
                         "TIMEIGN", "coproc", "until", "while", None, "DOLPAREN", "DOLBRACE"})
# time_command_acceptable.
_TIME_OK = frozenset({None, ";", "\n", "&&", "||", "&", "while", "do", "until", "if", "then", "elif", "else", "{",
                      "(", ")", "!", "time", "TIMEOPT", "TIMEIGN", "DOLPAREN", "DOLBRACE"})
_REDIR_OPS = frozenset({">", "<", ">>", ">|", "<>", "<<", "<<-", "<<<", "<&", ">&", "&>", "&>>"})
_NUM_REDIR_OPS = _REDIR_OPS - {"&>", "&>>"}
_SHELL_START = frozenset({"(", "{", "if", "while", "until", "for", "select", "case", "[[", "ARITH_CMD"})
_CMD_START = _SHELL_START | _REDIR_OPS | {"WORD", "ASSIGN", "NUMBER", "REDIR_WORD", "function", "coproc", "!",
                                          "time"}
_ASSIGN_BUILTINS = frozenset({"alias", "declare", "export", "local", "readonly", "typeset", "eval", "let"})
# Встроенные команды, чей аргумент `имя[…]=…` bash исполняет присваиванием с индексом (проверено на 5.3; у
# export, readonly, alias индекс не вычисляется).
_SUBSCRIPT_BUILTINS = frozenset({"declare", "typeset", "local", "eval", "let"})
_TEST_UNOP = frozenset("abcdefghknoprstuvwxzGLNORS")
_TEST_BINOP = frozenset({"=", "==", "!=", "<", ">", "=~", "-nt", "-ot", "-ef", "-eq", "-ne", "-lt", "-le", "-gt",
                         "-ge"})
# HEREDOC_MAX (shell.h): больше heredoc в одной команде bash не принимает и выходит.
_HEREDOC_MAX = 16


# ---------------------------------------------------------------------------------------------------------------
# Узлы дерева. Позиции start, end — в исходном тексте parse(text): у тел, которые bash разбирает из другой строки
# (`` `…` `` без экранирования, тело heredoc), позиции переведены обратно в исходный текст.


class Node:
    """Узел дерева: start, end — позиции в исходном тексте; _fields — поля с дочерними узлами."""
    __slots__ = ("start", "end")
    _fields = ()

    def __repr__(self):
        inner = ", ".join(f"{name}={getattr(self, name)!r}" for name in self._fields)
        return f"{type(self).__name__}({inner})"

    def children(self):
        """Дочерние узлы по порядку полей (списки и пары — по элементам)."""
        out = []
        for name in self._fields:
            _collect(getattr(self, name), out)
        return out


def _collect(value, out):
    """Узлы из значения поля (узел, список, пара) — в out по порядку."""
    if isinstance(value, Node):
        out.append(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, Node):
                out.append(item)
            elif isinstance(item, (list, tuple)):
                _collect(item, out)


def _node(cls, start, end, **fields):
    """Узел класса cls с позициями и полями (не заданные — None)."""
    node = cls.__new__(cls)
    node.start = start
    node.end = end
    for name in cls.__slots__:
        setattr(node, name, fields.get(name))
    return node


class SyntaxIssue:
    """Синтаксическая ошибка: pos — начало лексемы, на которой bash её нашёл; fatal — прекращает ли bash чтение."""
    __slots__ = ("pos", "fatal")

    def __init__(self, pos, fatal):
        self.pos = pos
        self.fatal = fatal

    def __repr__(self):
        return f"SyntaxIssue({self.pos}, fatal={self.fatal})"

    def __eq__(self, other):
        return isinstance(other, SyntaxIssue) and (self.pos, self.fatal) == (other.pos, other.fatal)

    __hash__ = None


class Script(Node):
    """Разобранный текст: commands — полные команды в порядке чтения bash (по переводу строки верхнего уровня, с
    телами heredoc); error — None или SyntaxIssue; dropped — диапазоны (start, end), отброшенные после ошибки скобок
    массива; comments — диапазоны (start, end) комментариев `#…` (без перевода строки)."""
    __slots__ = ("commands", "error", "dropped", "comments")
    _fields = ("commands",)


class Simple(Node):
    """Простая команда: assigns — присваивания перед именем, words — слова, redirs — перенаправления."""
    __slots__ = ("assigns", "words", "redirs")
    _fields = ("assigns", "words", "redirs")


class Pipeline(Node):
    """Конвейер: commands; bang — `!` (нечётное число); time — None, "time" или "time -p"."""
    __slots__ = ("commands", "bang", "time")
    _fields = ("commands",)


class AndOr(Node):
    """`a && b || c`: items и ops ("&&", "||") между ними."""
    __slots__ = ("items", "ops")
    _fields = ("items",)


class Sequence(Node):
    """Список: items и seps — разделитель после каждого (";", "&", "\\n"), у последнего его может не быть."""
    __slots__ = ("items", "seps")
    _fields = ("items",)


class Subshell(Node):
    """`( … )`: body — список команд, redirs — перенаправления за скобкой."""
    __slots__ = ("body", "redirs")
    _fields = ("body", "redirs")


class Group(Node):
    """`{ …; }`: body — список команд, redirs — перенаправления за скобкой."""
    __slots__ = ("body", "redirs")
    _fields = ("body", "redirs")


class If(Node):
    """clauses — пары (условие, тело) для `if` и каждого `elif`; orelse — тело `else` или None."""
    __slots__ = ("clauses", "orelse", "redirs")
    _fields = ("clauses", "orelse", "redirs")


class While(Node):
    """`while cond; do body; done`."""
    __slots__ = ("cond", "body", "redirs")
    _fields = ("cond", "body", "redirs")


class Until(Node):
    """`until cond; do body; done`."""
    __slots__ = ("cond", "body", "redirs")
    _fields = ("cond", "body", "redirs")


class For(Node):
    """name — Word имени или None у `for ((…))`; words — слова после `in` или None (`"$@"`); arith — Arith
    выражений `for ((…))` или None."""
    __slots__ = ("name", "words", "arith", "body", "redirs")
    _fields = ("name", "words", "arith", "body", "redirs")


class Select(Node):
    """`select name in words; do body; done`; words — None без `in` (`"$@"`)."""
    __slots__ = ("name", "words", "body", "redirs")
    _fields = ("name", "words", "body", "redirs")


class Case(Node):
    """word — проверяемое слово; items — тройки (образцы: список Word, тело или None, завершитель ";;"/";&"/";;&"
    или None)."""
    __slots__ = ("word", "items", "redirs")
    _fields = ("word", "items", "redirs")


class Function(Node):
    """Определение функции: name — Word имени, body — составная команда (её перенаправления — в ней)."""
    __slots__ = ("name", "body")
    _fields = ("name", "body")


class Coproc(Node):
    """name — Word имени или None, body — команда."""
    __slots__ = ("name", "body")
    _fields = ("name", "body")


class ArithCmd(Node):
    """`((…))`: expr — Arith."""
    __slots__ = ("expr", "redirs")
    _fields = ("expr", "redirs")


class Cond(Node):
    """`[[ … ]]`: words — слова выражения по порядку (операнды и операторы)."""
    __slots__ = ("words", "redirs", "_toks")
    _fields = ("words", "redirs")


class Redir(Node):
    """Перенаправление: op — оператор (">", "<<", "<&", …); fd — текст номера или `{имя}` перед ним или None;
    target — Word или Heredoc."""
    __slots__ = ("op", "fd", "target")
    _fields = ("target",)


class Heredoc(Node):
    """Here-document: term — терминатор после снятия кавычек; strip_tabs — `<<-`; quoted — были ли кавычки в
    терминаторе; body_start, body_end — тело в тексте (без строки терминатора); parts — части тела (у терминатора в
    кавычках — один Lit); body_text — текст тела, как его читает bash (`\\` с переводом строки снят у терминатора
    без кавычек, табы `<<-` сняты); word — Word терминатора. start, end — слово терминатора."""
    __slots__ = ("term", "strip_tabs", "quoted", "body_start", "body_end", "parts", "word", "body_text", "_ranges",
                 "memo", "skel")
    _fields = ("parts",)


class Word(Node):
    """Слово: parts — его части по порядку; _tok — текст лексемы, как его хранит bash (word->word: `$'…'` уже в
    '…', подстановки — узлами, их текст — печатью print_comsub)."""
    __slots__ = ("parts", "_tok")
    _fields = ("parts",)

    def literal(self):
        """Значение после снятия кавычек или None, если в слове есть раскрытие (параметр, подстановка,
        арифметика)."""
        return _literal(self.parts)


class Lit(Node):
    """Текст без кавычек (с `\\` уже снятым)."""
    __slots__ = ("text",)


class SQ(Node):
    """'…': text — содержимое."""
    __slots__ = ("text",)


class DQ(Node):
    """"…": parts — части."""
    __slots__ = ("parts",)
    _fields = ("parts",)


class AnsiC(Node):
    """$'…': value — значение после раскрытия экранирования."""
    __slots__ = ("value",)


class Param(Node):
    """Раскрытие параметра: `$x`, `$$`, `${…}`; text — его текст; parts — части слов операторов `${…}`."""
    __slots__ = ("text", "parts")
    _fields = ("parts",)


class Sub(Node):
    """Подстановка, которую bash исполняет: kind — "$(", "`", "${ ", "${|", "<(", ">("; body — Script тела."""
    __slots__ = ("kind", "body")
    _fields = ("body",)


class Arith(Node):
    """Арифметика `$((…))`, `$[…]`, `((…))`, `for ((…))`: parts — части выражения (подстановки в нём и в его `'…'`
    — Sub)."""
    __slots__ = ("parts", "_tok")
    _fields = ("parts",)


def _literal(parts):
    """Значение частей без раскрытий или None (Word.literal)."""
    out = []
    for part in parts:
        kind = type(part)
        if kind is Lit or kind is SQ:
            out.append(part.text)
        elif kind is AnsiC:
            out.append(part.value)
        elif kind is DQ:
            inner = _literal(part.parts)
            if inner is None:
                return None
            out.append(inner)
        else:
            return None
    return "".join(out)


# ---------------------------------------------------------------------------------------------------------------
# Исключения разбора: только внутри модуля.


class _Fatal(Exception):
    """Синтаксическая ошибка, после которой bash прекращает чтение (yyerror, YYABORT)."""
    def __init__(self, pos):
        super().__init__(pos)
        self.pos = pos


class _Discard(Exception):
    """Ошибка скобок присваивания массива: jump_to_top_level (DISCARD) — команда и остаток строки отброшены."""
    def __init__(self, pos):
        super().__init__(pos)
        self.pos = pos


class _ScanStop(Exception):
    """Раскрытие строки (тело heredoc, слово `=~`) кончилось ошибкой разбора подстановки: parts — найденное до неё
    на уровне, откуда брошено."""

    def __init__(self, parts, pos):
        super().__init__(pos)
        self.parts = parts
        self.pos = pos


def _run(gen):
    """Исполняет генератор разбора: `yield g` — вызов вложенного генератора g, его возврат — значение `yield`.
    Стек вызовов — список, рекурсии Python нет. Исключение вложенного генератора бросается в вызвавший."""
    stack = [gen]
    value = None
    error = None
    while True:
        top = stack[-1]
        try:
            if error is not None:
                exc, error = error, None
                sub = top.throw(exc)
            else:
                sub = top.send(value)
        except StopIteration as stop:
            stack.pop()
            value = stop.value
            if not stack:
                return value
            continue
        except Exception as exc:  # передаётся вызвавшему генератору
            stack.pop()
            if not stack:
                raise
            error = exc
            continue
        stack.append(sub)
        value = None


# ---------------------------------------------------------------------------------------------------------------
# Лексемы и части слова.


class _Tok:
    """Лексема: kind — вид ("WORD", "ASSIGN", "NUMBER", "REDIR_WORD", "ARITH_CMD", "ARITH_FOR", "EOF", оператор или
    зарезервированное слово), value — Word, текст числа или Arith, start, end — позиции, text — сырой текст слова."""
    __slots__ = ("kind", "value", "start", "end", "text")

    def __init__(self, kind, value, start, end, text=None):
        self.kind = kind
        self.value = value
        self.start = start
        self.end = end
        self.text = text

    def __repr__(self):
        return f"_Tok({self.kind!r}, {self.text!r}, {self.start})"


class _Parts:
    """Сборщик частей: подряд идущие символы — один Lit; `$имя`, `$1`, `$@`, `$$` вне экранирования — Param."""
    __slots__ = ("parts", "buf", "bstart", "bend", "dollar", "pname", "pstart", "pend")

    def __init__(self):
        self.parts = []
        self.buf = []
        self.bstart = self.bend = 0
        self.dollar = None  # позиция неэкранированного `$` в конце buf
        self.pname = None  # имя параметра `$имя`, которое ещё читается
        self.pstart = self.pend = 0

    def char(self, c, pos, esc=False):
        if self.pname is not None:
            if not esc and c in _NAME_CHAR:
                self.pname.append(c)
                self.pend = pos + 1
                return
            self._end_param()
        if self.dollar is not None:
            dpos = self.dollar
            self.dollar = None
            if not esc and (c in _NAME_START or c in _SPECIAL_PARAM):
                self.buf.pop()
                if c in _NAME_START:
                    self._flush(dpos)
                    self.pname = ["$", c]
                    self.pstart, self.pend = dpos, pos + 1
                else:
                    self._flush(dpos)
                    self.parts.append(_node(Param, dpos, pos + 1, text="$" + c, parts=[]))
                return
        if not self.buf:
            self.bstart = pos
        self.buf.append(c)
        self.bend = pos + 1
        if c == "$" and not esc:
            self.dollar = pos

    def text(self, s, start, end):
        """Готовый кусок литерального текста."""
        self._settle()
        if not s:
            return
        if not self.buf:
            self.bstart = start
        self.buf.append(s)
        self.bend = end

    def drop_last(self):
        """Снять последний символ буфера (`$`, `<`, `>` перед подстановкой)."""
        self._settle()
        if self.buf:
            last = self.buf.pop()
            if len(last) > 1:
                self.buf.append(last[:-1])
            self.bend -= 1

    def part(self, node):
        if (type(node) is SQ and not node.text) or (type(node) is DQ and not node.parts):
            # Пустые '' и "" значения не меняют: слово `P1''` — один Lit.
            return
        self._settle()
        self._flush(node.start)
        self.parts.append(node)

    def extend(self, nodes):
        for node in nodes:
            if type(node) is Lit:
                self.text(node.text, node.start, node.end)
            else:
                self.part(node)

    def _settle(self):
        if self.pname is not None:
            self._end_param()
        self.dollar = None

    def _end_param(self):
        name = "".join(self.pname)
        self.pname = None
        self.parts.append(_node(Param, self.pstart, self.pend, text=name, parts=[]))

    def _flush(self, end):
        if self.buf:
            self.parts.append(_node(Lit, self.bstart, max(self.bend, self.bstart), text="".join(self.buf)))
            self.buf = []

    def done(self):
        self._settle()
        self._flush(self.bend)
        return self.parts

    def mark(self):
        """Начало участка: всё собранное до него — отдельными частями."""
        self._settle()
        self._flush(self.bend)
        return len(self.parts)

    def wrap(self, mark, start, end):
        """Части с mark — одним Arith (индекс, смещение, которые bash раскрывает арифметикой)."""
        self._settle()
        self._flush(self.bend)
        inner = self.parts[mark:]
        del self.parts[mark:]
        self.parts.append(_node(Arith, start, max(start, end), parts=inner))


def _ansi_c(s):
    """Значение $'…' (ansicstr, lib/sh/strtrans.c): `\\n`, `\\t`, `\\xHH`, `\\uHHHH`, `\\cX`, восьмеричные коды."""
    out = []
    i, n = 0, len(s)
    simple = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
              "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        c = s[i + 1]
        i += 2
        if c in simple:
            out.append(simple[c])
        elif c in "01234567":
            j = i - 1
            k = j
            while k < n and k < j + 3 and s[k] in "01234567":
                k += 1
            out.append(chr(int(s[j:k], 8) & 0xFF))
            i = k
        elif c in "xuU":
            limit = {"x": 2, "u": 4, "U": 8}[c]
            k = i
            while k < n and k < i + limit and s[k] in "0123456789abcdefABCDEF":
                k += 1
            if k == i:
                out.append("\\" + c)
            else:
                code = int(s[i:k], 16)
                out.append(chr(code) if code < 0x110000 else "\ufffd")
                i = k
        elif c == "c" and i < n:
            out.append(chr(ord(s[i]) & 0x1F))
            i += 1
        else:
            out.append("\\" + c)
    return "".join(out)


def _quote_removal(s):
    """string_quote_removal (subst.c): снятие `\\`, '…', "…" с сырого текста слова (терминатор heredoc)."""
    out = []
    i, n = 0, len(s)
    dquote = False
    while i < n:
        c = s[i]
        if c == "\\":
            i += 1
            if i >= n:
                out.append("\\")
                break
            c = s[i]
            if dquote and c not in _DQ_ESC:
                out.append("\\")
            out.append(c)
            i += 1
        elif c == "'" and not dquote:
            j = s.find("'", i + 1)
            if j < 0:
                out.append(s[i + 1:])
                break
            out.append(s[i + 1:j])
            i = j + 1
        elif c == '"':
            dquote = not dquote
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _sq(s):
    """sh_single_quote: текст в '…'."""
    return "'" + s.replace("'", "'\\''") + "'"


def _chk_arithsub(s):
    """chk_arithsub (subst.c): скобки выражения `$((…))` сбалансированы с учётом `\\`, '…' и "…" (skip_double_quoted:
    в "…" пропускаются и `$(…)`, `${…}`, `` `…` `` целиком); `` `…` `` вне "…" не пропускается."""
    count = 0
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "(":
            count += 1
        elif c == ")":
            count -= 1
            if count < 0:
                return False
        if c == "\\":
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == '"':
            i = _skip_double_quoted(s, i + 1)
        else:
            i += 1
    return count == 0


def _skip_double_quoted(s, i):
    """skip_double_quoted: позиция за закрывающей `"` (с позиции после открывающей); `$(…)`, `${…}`, `` `…` `` и
    кавычки в них пропускаются целиком (стек, без рекурсии)."""
    n = len(s)
    stack = ['"']
    while i < n and stack:
        c = s[i]
        top = stack[-1]
        if c == "\\":
            i += 2
            continue
        if top == '"':
            if c == '"':
                stack.pop()
            elif c == "`":
                stack.append("`")
            elif c == "$" and s[i + 1:i + 2] in ("(", "{"):
                stack.append(s[i + 1])
                i += 1
        elif top == "`":
            if c == "`":
                stack.pop()
        else:
            close = ")" if top == "(" else "}"
            if c == close:
                stack.pop()
            elif c == "'":
                j = s.find("'", i + 1)
                i = n if j < 0 else j
            elif c == '"':
                stack.append('"')
            elif c == "`":
                stack.append("`")
            elif c == "$" and s[i + 1:i + 2] in ("(", "{"):
                stack.append(s[i + 1])
                i += 1
            elif c == "(" and top == "(":
                stack.append("(")
        i += 1
    return i


def _arith_neutral(s):
    """Текст для chk_arithsub, который можно заменить на `()` в любом месте счёта: скобки сбалансированы, нет кавычек,
    `\\`, `` ` `` и `${` — состояние счёта до текста (например, незакрытая '…' из тела heredoc раньше) текст
    тогда проходит так же, как `()`."""
    return not any(c in s for c in "'\"\\`") and "${" not in s and _chk_arithsub(s)


def _open_subscript(s):
    """Начало слова — `имя[` с незакрытой скобкой (индекс присваивания)."""
    k = s.find("[")
    return k > 0 and _valid_ident(s[:k]) and s.count("[") > s.count("]")


def _valid_ident(s):
    """valid_identifier: имя переменной."""
    return bool(s) and s[0] in _NAME_START and all(c in _NAME_CHAR for c in s)


def _skip_subscript(s, i):
    """Конец `[…]` с позиции `[` (skipsubscript, упрощённо: вложенные скобки, кавычки, `\\`): индекс `]` или len."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _assignment(s, compassign=False):
    """assignment (general.c): позиция `=` присваивания `имя=`, `имя+=`, `имя[…]=` или 0."""
    if not s:
        return 0
    c = s[0]
    if compassign and c != "[":
        return 0
    if not compassign and c not in _NAME_START:
        return 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "=":
            return i
        if c == "[":
            j = _skip_subscript(s, i)
            if j >= n or s[j] != "]":
                return 0
            j += 1
            if s[j:j + 2] == "+=":
                return j + 1
            return j if s[j:j + 1] == "=" else 0
        if c == "+" and s[i + 1:i + 2] == "=":
            return i + 1
        if c not in _NAME_CHAR:
            return 0
        i += 1
    return 0


# ---------------------------------------------------------------------------------------------------------------
# Чтение и разбор одной строки ввода.


class _R:
    """Ввод, лексер и грамматика над одной строкой: исходным текстом или строкой, которую bash разбирает при
    раскрытии (тело `` `…` ``, тело heredoc). pieces — перевод позиций строки в позиции исходного текста; tail —
    дописывать ли перевод строки в конец (так читает ввод shell_getc; раскрытие строки его не дописывает)."""

    def __init__(self, text, pieces=None, comments=None, tail=True, limit=None, foreign=None, shared=None):
        n0 = len(text)
        add = ""
        if tail and text and text[-1] != "\n":
            # shell_getc: последней строке без перевода строки он дописывается; строке, кончающейся нечётным
            # числом `\\`, вместо него — ещё `\\` (снятие `\\` с переводом строки не съедает последний `\\`).
            k = n0
            while k > 0 and text[k - 1] == "\\":
                k -= 1
            add = "\\" if (n0 - k) % 2 else "\n"
        self.s = text + add
        self.n0 = n0
        self.n = len(self.s)
        if pieces is not None and not pieces:
            pieces = [(0, 0)]
        self.pieces = pieces  # [(локальное начало, исходное начало)] по возрастанию или None
        self.limit = limit  # наибольшая исходная позиция (строка напечатана bash: позиции — в пределах подстановки)
        self.piece_starts = [p[0] for p in pieces] if pieces else None
        # Ввод: текущий кусок [pos, lim], стек отложенных кусков, первая непрочитанная строка.
        self.pos = 0
        self.lim = -1
        self.segs = []
        self.frontier = 0
        self.cpos = 0
        self.cprev = 0
        self.last_i = -1
        self.jumps = []
        # Лексер: история лексем (yylex), parser_state и счётчики.
        self.current = None
        self.last = None
        self.before = None
        self.ago = None
        self.token_to_read = None
        self.state = 0
        self.open_brace_count = 0
        self.esacs_needed = 0
        self.expecting_in = 0
        self.expecting_cmd = None
        self.dstack = []
        self.eof_tok = None
        self.extglob = False
        self.pending = []
        self.gathered = []
        self.opened = []
        self.comments = comments if comments is not None else []
        # Подстановки, разобранные этим разбором: позиция `$` → (узел, конец, куски текста, обстановка, чистая
        # ли — см. parse_comsub). foreign — такие же записи, разобранные раньше другим разбором над тем же текстом
        # (печать подстановки, тело heredoc, группа `=~`): повторный разбор берёт их готовыми, а не разбирает
        # вложенные подстановки заново на каждом уровне вложенности (иначе время — экспонента по глубине).
        self.sub_memo = {}
        self.foreign = foreign if foreign is not None else {}
        self.memo_keys = None
        self.foreign_keys = None
        # Общие для всех разборов одного текста: (позиция в исходном тексте, обстановка) → [(текст, узел, куски)]
        # подстановок, чей текст лёг в исходный подряд (позиции узлов — те же): другой разбор того же места с тем же
        # текстом берёт её готовой (раскрытие '…' в арифметике и остатка за ней перечитывает одно место в разных
        # разборах, не вложенных друг в друга).
        self.shared = shared if shared is not None else {}
        self.paren_memo = {}
        self.paren_next = {}
        # Скобки арифметики за `<`, `>`, прочитанные parse_matched_pair этого разбора или разбора той же строки, из
        # которого он создан (view): `(` → (`)`, текст между ними, qc, scanning) — см. pmp.
        self.paren_tpl = {}
        # Группы `(…)` образца `=~` и extglob: `(` → (`)`, разделитель) — пары, которые нашёл parse_matched_pair
        # группы (без переходов ввода внутри); group_views — такие пары разборов, читавших тот же текст раньше
        # (словарь, сдвиг позиций). Вложенная группа в подстановке в группе не перечитывается на каждом уровне.
        self.group_pairs = {}
        self.group_views = []
        self.group_rec = None
        self.unit_mark = None
        self.cond_token = None
        self.tpl_out = []
        self.cond_toks = []
        self.sub_quotes = []
        self.assign_builtin = None
        self.scanning = False
        self.scan_failed = False
        self.quote_memo = None
        # `\\` в слове скобок массива внутри подстановки, не экранировавший следующий символ (разделитель — `(`,
        # `{` или `"`), который экранирует разбор при раскрытии (разделителей нет): read_token_word.
        self.esc_split = False
        # В текущем слове есть подстановка с таким `\\`: слово раскрывается заново по его тексту (read_token_word).
        self.reexpand = False

    def view(self, start):
        """Новый разбор той же строки с позиции start (без копии текста): bash разбирает её ещё раз при раскрытии."""
        sub = _R("", None, self.comments, shared=self.shared, foreign=dict(self.sub_memo))
        sub.s, sub.n0, sub.n, sub.limit = self.s, self.n0, self.n, self.limit
        sub.pieces, sub.piece_starts = self.pieces, self.piece_starts
        sub.frontier = start
        # Пары скобок, которые нашёл parse_matched_pair этой строки: `((` в теле `<(((…) …)` — подоболочка без
        # нового прохода (arith_cmd), `<((` — без нового parse_matched_pair (parse_comsub).
        sub.paren_tpl, sub.paren_memo, sub.paren_next = self.paren_tpl, self.paren_memo, self.paren_next
        return sub

    # -- позиции

    def P(self, i):
        """Позиция строки → позиция исходного текста."""
        i = min(i, self.n0)
        if self.pieces is None:
            return i
        k = max(0, bisect.bisect_right(self.piece_starts, i) - 1)
        local, orig = self.pieces[k]
        if self.limit is not None:
            return min(orig + (i - local), self.limit)
        return orig + (i - local)

    def PE(self, i):
        """Конец диапазона: позиция после последнего символа."""
        i = min(i, self.n0)
        if self.pieces is None or i <= 0:
            return self.P(i)
        return self.P(i - 1) + 1

    def orig_pieces(self, ranges):
        """Куски строки [(начало, конец)) → куски производной строки [(локальное начало, исходное начало)]."""
        out = []
        local = 0
        for a, b in ranges:
            if b <= a:
                continue
            if self.pieces is None:
                out.append((local, a))
            else:
                k = max(0, bisect.bisect_right(self.piece_starts, a) - 1)
                i = a
                while i < b:
                    pl, po = self.pieces[k]
                    nxt = self.pieces[k + 1][0] if k + 1 < len(self.pieces) else b
                    stop = min(b, nxt)
                    out.append((local + (i - a), po + (i - pl)))
                    i = stop
                    k += 1
            local += b - a
        return out

    # -- ввод (shell_getc, shell_ungetc)

    def getc(self, rm):
        """Следующий символ ввода или None в конце. rm — снимать ли `\\` с переводом строки."""
        s = self.s
        while True:
            i = self.pos
            if i > self.lim:
                if self.segs:
                    self.pos, self.lim = self.segs.pop()
                    continue
                f = self.frontier
                if f >= self.n:
                    self.cpos = self.n0
                    return None
                e = s.find("\n", f)
                if e < 0:
                    e = self.n - 1
                self.pos, self.lim, self.frontier = f, e, e + 1
                continue
            c = s[i]
            if c == "\\" and rm and i < self.lim and s[i + 1] == "\n":
                self.pos = i + 2
                continue
            self.pos = i + 1
            self.cprev = self.cpos
            self.cpos = i
            if i != self.last_i + 1:
                self.jumps.append((self.last_i + 1, i))
            self.last_i = i
            return c

    def ungetc(self, c):
        """shell_ungetc: символ c, только что прочитанный, читается снова; два возврата подряд — два символа."""
        if c is None:
            return
        self.pos = self.cpos
        self.last_i = self.cpos - 1
        self.cpos = self.cprev

    def push_input(self, pieces):
        """Куски [(начало, конец включительно)] читаются раньше остатка текущего куска (push_string, shell_ungets)."""
        pieces = [p for p in pieces if p[0] <= p[1]]
        if not pieces:
            return
        self.segs.append((self.pos, self.lim))
        for piece in reversed(pieces[1:]):
            self.segs.append(piece)
        self.pos, self.lim = pieces[0]

    def advance_to(self, e):
        """Перейти к позиции e, отбросив прочитанное до неё (повтор уже разобранной подстановки)."""
        guard = len(self.segs) + 1
        while not (self.pos <= e <= self.lim + 1) and guard > 0:
            if not self.segs:
                break
            self.pos, self.lim = self.segs.pop()
            guard -= 1
        if self.pos <= e <= self.lim + 1:
            self.pos = e
            self.last_i = e - 1
            return True
        return False

    def skip_to(self, e):
        """Перейти вперёд к позиции e за подстановкой, разобранной другим разбором (запись foreign): в текущем
        куске или, если отложенных кусков нет, в непрочитанных строках. Иначе ввод не меняется, False."""
        if self.pos <= e <= self.lim + 1:
            self.pos = e
        elif not self.segs and self.frontier <= e <= self.n:
            self.pos, self.lim, self.frontier = e, e - 1, e
        else:
            return False
        self.last_i = e - 1
        return True

    def memo_in(self, ranges):
        """Записи sub_memo и foreign внутри кусков ranges [(начало, конец)) этой строки — в позициях строки,
        склеенной из этих кусков: для разбора, который её читает (тело heredoc, группа `=~`). Только чистые и
        только внешние: вложенные в другую запись ему не понадобятся — он её не читает."""
        out = {}
        if not ranges or not (self.sub_memo or self.foreign):
            return out
        if self.sub_memo and self.memo_keys is None:
            self.memo_keys = sorted(self.sub_memo)
        if self.foreign and self.foreign_keys is None:
            self.foreign_keys = sorted(self.foreign)
        local = 0
        for a, b in ranges:
            for memo, keys in ((self.sub_memo, self.memo_keys), (self.foreign, self.foreign_keys)):
                if not memo:
                    continue
                k = bisect.bisect_left(keys, a)
                while k < len(keys) and keys[k] < b:
                    key = keys[k]
                    node, end, tpl, ctx, clean = memo[key]
                    if clean and end <= b:
                        out.setdefault(local + key - a, (node, local + end - a, tpl, ctx, True))
                        k = bisect.bisect_left(keys, end, k + 1)
                    else:
                        k += 1
            local += b - a
        return out

    def drop_line(self):
        """reset_parser: остаток текущей строки и отложенные куски отбрасываются, чтение — со следующей строки."""
        self.segs = []
        self.pos = self.lim + 1

    # -- heredoc (gather_here_documents, make_here_document, read_secondary_line)

    def gather(self):
        """gather_here_documents: тела ожидающих heredoc — по порядку открытия, с первой непрочитанной строки."""
        while self.pending:
            doc = self.pending.pop(0)
            self.read_body(doc)
            self.gathered.append(doc)
            if not doc.quoted:
                doc.parts = yield self.body_parts(doc)

    def read_line(self, f, join):
        """Строка тела с позиции f (read_a_line): (обработанный текст с переводом строки, куски [(начало,
        конец)) исходных позиций, позиция следующей строки) или None в конце ввода. join — снимать `\\` с переводом
        строки (терминатор без кавычек)."""
        s, n = self.s, self.n0
        if f >= n:
            return None
        if not join:
            e = s.find("\n", f, n)
            if e < 0:
                return s[f:n] + "\n", [(f, n)], n
            return s[f:e + 1], [(f, e + 1)], e + 1
        text = []
        ranges = []
        i = f
        while True:
            e = s.find("\n", i, n)
            stop = n if e < 0 else e
            k = stop
            while k > i and s[k - 1] == "\\":
                k -= 1
            odd = (stop - k) % 2 == 1
            if e >= 0 and odd:
                text.append(s[i:stop - 1])
                ranges.append((i, stop - 1))
                i = e + 1
                if i >= n:
                    return "".join(text), ranges, n
                continue
            if e < 0:
                if odd:
                    # `\\` в конце ввода: read_a_line дописывает ещё `\\` и кончает строку без перевода строки.
                    text.append(s[i:n] + "\\")
                    ranges.append((i, n))
                    return "".join(text), ranges, n
                text.append(s[i:n] + "\n")
                ranges.append((i, n))
                return "".join(text), ranges, n
            text.append(s[i:e + 1])
            ranges.append((i, e + 1))
            return "".join(text), ranges, e + 1

    def read_body(self, doc):
        """make_here_document: тело heredoc doc до строки терминатора (табы `<<-` сняты до сравнения; у
        терминатора без кавычек строки склеены по `\\` с переводом строки); в подстановке строка, начатая
        терминатором, со знаком конца подстановки в остатке тоже кончает тело."""
        term = doc.term
        tlen = len(term)
        strip = doc.strip_tabs
        join = not doc.quoted
        eof = self.eof_tok if (self.state & _EOFTOKEN) else None
        f = self.frontier
        body_ranges = []
        body_text = []
        doc_start = f
        end = None
        fast = self._fast_body(f, term, strip, join, eof)
        if fast is not None:
            end, f, push, body_text, body_ranges = fast
            if push is not None:
                self.push_input([push])
        while fast is None:
            got = self.read_line(f, join)
            if got is None:
                end = f
                break
            line, ranges, nxt = got
            line_start = f
            skip = 0
            if strip and line:
                if line.startswith(term) and line[tlen:tlen + 1] == "\n":
                    end = line_start
                    f = nxt
                    break
                while skip < len(line) and line[skip] == "\t":
                    skip += 1
            rest = line[skip:]
            if rest.startswith(term) and rest[tlen:tlen + 1] == "\n":
                end = line_start
                f = nxt
                break
            if eof and rest.startswith(term) and eof in rest[tlen:]:
                # make_here_document: строка, начатая терминатором, со знаком конца подстановки в остатке кончает
                # тело; остаток возвращается во ввод (shell_ungets) и читается раньше остатка текущей строки.
                end = line_start
                f = nxt
                self.push_input(self._line_slice(ranges, skip + tlen))
                break
            body_text.append(rest)
            body_ranges.extend(self._ranges_from(ranges, skip))
            f = nxt
        self.frontier = max(self.frontier, f) if end is not None else self.frontier
        if end is None:
            end = self.n0
        doc.body_start = self.P(doc_start)
        doc.body_end = self.PE(end)
        text = "".join(body_text)
        doc.body_text = text
        doc._ranges = body_ranges
        if doc.quoted:
            doc.parts = [_node(Lit, doc.body_start, doc.body_end, text=text)] if text else []

    def _fast_body(self, f, term, strip, join, eof):
        """Тело heredoc поиском строки терминатора регулярным выражением (тот же результат, что построчное чтение
        read_body, без прохода Python по строкам тела: тело внутреннего heredoc в подстановке в теле внешнего не
        перечитывается построчно на каждом уровне). None — в теле есть склейка `\\` с переводом строки, нужен
        построчный разбор. Иначе (начало строки терминатора, начало следующей строки, остаток строки для shell_ungets
        или None, [текст тела], [куски тела])."""
        s, n = self.s, self.n0
        tabs = r"\t*" if strip else ""
        ends = r"(?:\n|\Z" + (r"|[^\n]*" + re.escape(eof) if eof else "") + ")"
        m = re.compile(r"^" + tabs + re.escape(term) + ends, re.MULTILINE).search(s, f, n)
        line_start = m.start() if m else n
        line_end = s.find("\n", line_start, n) if m else n
        if line_end < 0:
            line_end = n
        if join and (s.find("\\\n", f, line_end + 1) >= 0 or (line_end == n and n > f and s[n - 1] == "\\")):
            return None
        push = None
        if m:
            k = line_start
            while k < line_end and s[k] == "\t" and strip:
                k += 1
            if s[k + len(term):k + len(term) + 1] not in ("\n", ""):
                # Правило знака конца подстановки: остаток строки — во ввод.
                last = line_end if line_end < n else (n if self.n > n else n - 1)
                push = (k + len(term), last)
            nxt = line_end + 1 if line_end < n else n
        else:
            nxt = n
        body = s[f:line_start]
        if m is None and body and body[-1] != "\n":
            body += "\n"
        if not strip:
            return line_start, nxt, push, [body], [(f, line_start)] if line_start > f else []
        texts, ranges = [], []
        pos = f
        while pos < line_start:
            e = s.find("\n", pos, line_start)
            e = line_start if e < 0 else e + 1
            k = pos
            while k < e and s[k] == "\t":
                k += 1
            texts.append(s[k:e])
            if e > k:
                ranges.append((k, e))
            pos = e
        if m is None and texts and not texts[-1].endswith("\n"):
            texts[-1] += "\n"
        return line_start, nxt, push, texts, ranges

    @staticmethod
    def _ranges_from(ranges, skip):
        """Куски строки без первых skip символов."""
        out = []
        for a, b in ranges:
            if skip >= b - a:
                skip -= b - a
                continue
            out.append((a + skip, b))
            skip = 0
        return out

    def _line_slice(self, ranges, skip):
        """Остаток строки терминатора с символа skip — куски ввода [(начало, конец включительно)]."""
        out = []
        for a, b in self._ranges_from(ranges, skip):
            out.append((a, min(b, self.n) - 1))
        # Последний символ строки — перевод строки (или дописанный в конце ввода).
        if out and out[-1][1] < self.n0 and self.s[out[-1][1]] != "\n" and out[-1][1] + 1 < self.n:
            a, b = out[-1]
            out[-1] = (a, b + 1)
        return out

    def body_parts(self, doc):
        """Части тела heredoc без кавычек у терминатора: раскрытие как в "…" без особой `"` (expand_string с
        Q_HERE_DOCUMENT); подстановки разбираются из строки тела."""
        text = doc.body_text
        if not text:
            return []
        sub = _R(text, self.orig_pieces(doc._ranges) or [(0, doc.body_start)], self.comments, shared=self.shared,
                 tail=False, foreign=self.memo_in(doc._ranges))
        sub.pos, sub.lim, sub.frontier = 0, -1, 0
        parts = yield sub.scan_text(_P_HEREDOC)
        doc.memo = sub.sub_memo
        return parts

    def scan_text(self, flags):
        """Строка целиком в режиме "…" или тела heredoc, до конца ввода (expand_string)."""
        self.scanning = True
        parts = yield self.pmp('"', '"', None, flags | _P_TOP, self.cpos)
        return parts[0]

    # -- лексемы (yylex, read_token)

    def yylex(self):
        """yylex: следующая лексема с историей (two_tokens_ago, token_before_that, last_read_token, current_token)."""
        self.ago = self.before
        self.before = self.last
        self.last = self.current
        tok = yield self.read_token()
        self.current = tok.kind
        return tok

    def push_history(self, kind):
        """Сдвиг истории лексем, как у yylex, с лексемой kind, которую грамматика получила без read_token."""
        self.ago = self.before
        self.before = self.last
        self.last = self.current
        self.current = kind

    def resword_ok(self, last):
        """reserved_word_acceptable: после last слово — в позиции команды (и после имени за `function`,
        `coproc`: проверка — по последним двум лексемам истории, как в bash)."""
        return last in _RESWORD_OK or (self.last == "WORD" and self.before in ("coproc", "function"))

    def command_position(self, last):
        """command_token_position: слово после last — в позиции команды (присваивание, алиас)."""
        if last == "ASSIGN":
            return True
        if (self.state & _REDIRLIST) and last not in _REDIR_OPS:
            return True
        return last not in (";;", ";&", ";;&") and self.resword_ok(last)

    def assignment_ok(self, last):
        """assignment_acceptable: после last слово может быть присваиванием."""
        return self.command_position(last) and not (self.state & _CASEPAT)

    def read_token(self):
        """read_token: лексема с текущей позиции — пробелы и комментарий пропущены, перевод строки собирает тела
        heredoc, метасимволы — операторы, остальное — слово (read_token_word)."""
        if self.token_to_read is not None:
            tok, self.token_to_read = self.token_to_read, None
            return tok
        while True:
            c = self.getc(True)
            if c is None or c not in _BLANK:
                break
        if c is None:
            return _Tok("EOF", None, self.n0, self.n0)
        start = self.cpos
        if self.unit_mark is None:
            self.unit_mark = start
        if c == "#":
            # Комментарий: до перевода строки (discard_until), сам перевод строки — лексема.
            while True:
                c = self.getc(False)
                if c is None:
                    self.comments.append((self.P(start), self.PE(self.n0)))
                    return _Tok("EOF", None, self.n0, self.n0)
                if c == "\n":
                    break
            self.comments.append((self.P(start), self.PE(self.cpos)))
            start = self.cpos
        if c == "\n":
            if self.pending:
                yield self.gather()
            self.state &= ~(_ALEXPNEXT | _ASSIGNOK | _CMDBLTIN)
            return _Tok("\n", None, self.P(start), self.PE(start + 1))
        if self.state & _REGEXP:
            tok = yield self.read_token_word(c, start)
            return tok
        if c in _META:
            self.state &= ~(_ASSIGNOK | _CMDBLTIN)
            if (self.state & _CMDSUBST) and c == self.eof_tok:
                peek = self.getc(False)
            else:
                peek = self.getc(True)
            kind = None
            if c == peek:
                if c == "<":
                    p2 = self.getc(True)
                    if p2 == "-":
                        kind = "<<-"
                    elif p2 == "<":
                        kind = "<<<"
                    else:
                        self.ungetc(p2)
                        kind = "<<"
                elif c == ">":
                    kind = ">>"
                elif c == ";":
                    self.state |= _CASEPAT
                    p2 = self.getc(True)
                    if p2 == "&":
                        kind = ";;&"
                    else:
                        self.ungetc(p2)
                        kind = ";;"
                elif c == "&":
                    kind = "&&"
                elif c == "|":
                    kind = "||"
                elif c == "(":
                    result = yield self.parse_dparen(start)
                    if result is not None:
                        return result
            elif c == "<" and peek == "&":
                kind = "<&"
            elif c == ">" and peek == "&":
                kind = ">&"
            elif c == "<" and peek == ">":
                kind = "<>"
            elif c == ">" and peek == "|":
                kind = ">|"
            elif c == "&" and peek == ">":
                p2 = self.getc(True)
                if p2 == ">":
                    kind = "&>>"
                else:
                    self.ungetc(p2)
                    kind = "&>"
            elif c == "|" and peek == "&":
                kind = "|&"
            elif c == ";" and peek == "&":
                self.state |= _CASEPAT
                kind = ";&"
            if kind is not None:
                return _Tok(kind, None, self.P(start), self.PE(self.pos))
            self.ungetc(peek)
            if c == ")" and self.last == "(" and self.before == "WORD":
                self.state |= _ALLOWOPNBRC
            if c == "(" and not (self.state & (_CASEPAT | _CONDCMD)) and self.last != "WORD":
                self.state |= _SUBSHELL
            elif (self.state & _CASEPAT) and c == ")":
                self.state &= ~_CASEPAT
            elif (self.state & _SUBSHELL) and c == ")":
                self.state &= ~_SUBSHELL
            if c not in "<>" or peek != "(":
                return _Tok(c, None, self.P(start), self.PE(start + 1))
        if c == "-" and self.last in ("<&", ">&"):
            self.state &= ~_CMDBLTIN
            return _Tok("-", None, self.P(start), self.PE(start + 1))
        tok = yield self.read_token_word(c, start)
        return tok

    def parse_dparen(self, start):
        """`((` (parse_dparen, parse_arith_cmd): после `for` — выражения `for ((…))`; в позиции команды —
        арифметика, если скобки кончаются `))`, иначе подоболочка: прочитанный текст возвращается во ввод с второй
        `(` (push_string). None — обычная `(`."""
        if self.last == "for":
            res = yield self.arith_cmd()
            if res is None:
                raise _Fatal(self.P(start))
            if res[0] == 1:
                return _Tok("ARITH_FOR", res[1], self.P(start), self.PE(self.pos))
            raise _Fatal(self.P(start))
        if self.resword_ok(self.last):
            res = yield self.arith_cmd()
            if res[0] == 1:
                return _Tok("ARITH_CMD", res[1], self.P(start), self.PE(self.pos))
            if res[1] is not None:
                self.push_input(res[1])
            if not (self.state & (_CASEPAT | _CONDCMD)):
                self.state |= _SUBSHELL
            return _Tok("(", None, self.P(start), self.PE(start + 1))
        return None

    def arith_cmd(self):
        """(1, Arith) — `((…))`; (0, куски ввода или None) — подоболочка: прочитанное со второй `(` bash
        возвращает во ввод. Скобка, уже сосчитанная внешним `((`, читается повторно из возвращённого текста: её
        пара и символ за парой известны (paren_memo, paren_next), и возврат во ввод — просто чтение дальше со
        второй `(` (без повторного прохода: вложенные `((…) …)` линейны)."""
        open_pos = self.cpos  # вторая `(`
        if open_pos in self.paren_memo and self.paren_next.get(open_pos, ")") != ")":
            self.ungetc("(")
            return 0, None
        k0 = len(self.jumps)
        parts, close_pos = yield self.pmp(None, "(", ")", _P_ARITH, open_pos)
        expr_tok = [_Tpl(self.tpl_out[:-1])]
        k1 = len(self.jumps)
        c = self.getc(False)
        self.paren_next[open_pos] = c
        if c == ")":
            node = _node(Arith, self.P(open_pos - 1), self.PE(self.pos), parts=parts)
            node._tok = expr_tok
            return 1, node
        self.ungetc(c)
        pieces = [(a, b - 1) for a, b in self.ranges_between(open_pos, close_pos + 1, k0, k1)]
        return 0, pieces

    # -- слово (read_token_word)

    def read_token_word(self, c, start, whole=False, parts=None):
        """read_token_word: слово с символа c. whole — вся строка одним словом (раскрытие слова при исполнении:
        `(…)` образца `=~` и шаблона extglob) — возврат части, а не лексема."""
        tok = []
        sub_quotes = []
        if parts is None:
            parts = _Parts()
        outer, self.reexpand = self.reexpand, False
        all_digit = c in _DIGITS
        dollar_present = quoted = pass_next = compound = False
        P = self.P
        end_char = None
        while True:
            if c is None:
                break
            if pass_next:
                pass_next = False
                tok.append(c)
                parts.char(c, P(self.cpos), True)
                all_digit = all_digit and c in _DIGITS
                c = self.getc(self._cd() != "'")
                continue
            cd = self._cd()
            if c == "\\":
                if self.state & _NOEXPAND:
                    pass_next = True
                    quoted = True
                    tok.append(c)
                    c = self.getc(False)
                    continue
                peek = self.getc(False)
                if peek == "\n":
                    c = self.getc(cd != "'")
                    continue
                self.ungetc(peek)
                if cd is None or cd == "`" or (cd == '"' and peek in _DQ_ESC):
                    pass_next = True
                elif peek is not None and (peek in _BREAK or peek in _QUOTE or peek in "$\\"):
                    # Сюда доходят только слова скобок массива в подстановке (parse_compound_assignment снимает
                    # PST_NOEXPAND, разделитель — `(`, `{` подстановки или `"` вокруг неё): `\\` не экранирует. При
                    # раскрытии bash разбирает напечатанный текст подстановки заново без разделителей
                    # (xparse_dolparen), и там этот `\\` экранирует: `x=$(a=(\\'y) ; touch P ; b=(\\'))` исполняет
                    # `touch P`. Перед прочими символами `\\` меняет только значение слова, не границы.
                    self.esc_split = True
                quoted = True
                tok.append(c)
                if not pass_next:
                    parts.char(c, P(self.cpos), True)
                all_digit = False
                c = self.getc(False if pass_next else cd != "'")
                continue
            if (not whole and (self.state & _FUNSUBST) and not tok and not quoted and c == "}"
                    and self.resword_ok(self.last)):
                tok.append(c)
                parts.char(c, P(self.cpos))
                all_digit = dollar_present = False
                end_char = None
                self.reexpand = outer
                return self._got_token(tok, parts, start, all_digit, dollar_present, quoted, compound, end_char,
                                       self.pos)
            if c in _QUOTE:
                qpos = self.cpos
                self._dpush(c)
                res = yield self.pmp(c, c, c, _P_COMMAND if c == "`" else 0, qpos)
                self._dpop()
                node, raw = res
                if (c == "'" and (self.state & _ASSIGNOK) and self.assign_builtin in _SUBSCRIPT_BUILTINS
                        and _open_subscript(_flat(tok))):
                    # `declare a['…']=…`: индекс присваивания встроенной команды bash раскрывает арифметикой.
                    sub_quotes.append((qpos, self.cpos))
                tok.append(c)
                tok.append(_Tpl(raw))
                parts.part(node)
                all_digit = False
                if c != "`":
                    quoted = True
                if c == '"' and "$" in _flat(raw):
                    dollar_present = True
                c = self.getc(self._cd() != "'")
                continue
            if not whole and (self.state & _REGEXP) and c in "(|":
                if c == "|":
                    tok.append(c)
                    parts.char(c, P(self.cpos))
                    all_digit = False
                    c = self.getc(self._cd() != "'")
                    continue
                gpos = self.cpos
                node = yield self.group_word(cd, gpos, gpos)
                tok.append(self._group_tok(self.s[gpos:self.cpos + 1], gpos))
                parts.extend(node)
                dollar_present = all_digit = False
                c = self.getc(self._cd() != "'")
                continue
            if not whole and self.extglob and c in _PATTERN_CHAR:
                ppos = self.cpos
                peek = self.getc(True)
                if peek == "(":
                    gpos = self.cpos
                    node = yield self.group_word(cd, gpos, ppos)
                    tok.append(c)
                    tok.append(self._group_tok(self.s[gpos:self.cpos + 1], gpos))
                    parts.extend(node)
                    dollar_present = all_digit = False
                    c = self.getc(self._cd() != "'")
                    continue
                self.ungetc(peek)
            if c in "$<>":
                cpos = self.cpos
                peek = self.getc(True)
                if peek == "(" or (peek in ("{", "[") and c == "$"):
                    ppos = self.cpos
                    if peek == "{":
                        npeek = self.getc(True)
                        self.ungetc(npeek)
                        if npeek in _FUNSUB:
                            self._dpush(peek)
                            node = yield self.parse_comsub(cd, "{", "}", _P_COMMAND, cpos)
                            self._dpop()
                            pieces = self.tpl_out
                        else:
                            node = yield self.pmp(cd, "{", "}", _P_FIRSTCLOSE | _P_DOLBRACE, ppos)
                            pieces = ["{", _Tpl(self.tpl_out)]
                            node = self._param(node, cpos)
                    elif peek == "(":
                        self._dpush(peek)
                        node = yield self.parse_comsub(cd, "(", ")", _P_COMMAND, cpos)
                        self._dpop()
                        pieces = self.tpl_out
                    else:
                        inner, close_pos = yield self.pmp(cd, "[", "]", _P_ARITH, ppos)
                        pieces = ["[", _Tpl(self.tpl_out)]
                        node = _node(Arith, P(cpos), _close_end(self, close_pos), parts=inner)
                    tok.append(c)
                    tok.append(_Tpl(pieces))
                    parts.part(node)
                    dollar_present = True
                    all_digit = False
                    c = self.getc(self._cd() != "'")
                    continue
                if c == "$" and peek in ("'", '"'):
                    qpos = self.cpos
                    self._dpush(peek)
                    res = yield self.pmp(peek, peek, peek, _P_ALLOWESC if peek == "'" else 0, qpos)
                    self._dpop()
                    node, raw = res
                    if peek == "'":
                        value = node.value
                        tok.append(_sq(value))
                        node.start = P(cpos)
                    else:
                        tok.append('"')
                        tok.append(_Tpl(raw))
                        node.start = P(cpos)
                    parts.part(node)
                    quoted = True
                    all_digit = False
                    c = self.getc(self._cd() != "'")
                    continue
                if c == "$" and peek == "$":
                    tok.append("$$")
                    parts.part(_node(Param, P(cpos), P(self.cpos) + 1, text="$$", parts=[]))
                    dollar_present = True
                    all_digit = False
                    c = self.getc(self._cd() != "'")
                    continue
                self.ungetc(peek)
            elif not whole and c == "[" and ((tok and self.assignment_ok(self.last) and _valid_ident(_flat(tok)))
                               or (not tok and (self.state & _COMPASSIGN))):
                bpos = self.cpos
                saved_quotes, self.sub_quotes = self.sub_quotes, sub_quotes
                inner, close_pos = yield self.pmp(cd, "[", "]", _P_ARRAYSUB, bpos)
                self.sub_quotes = saved_quotes
                tok.append("[")
                tok.append(_Tpl(self.tpl_out))
                parts.text("[", P(bpos), P(bpos) + 1)
                # Индекс присваивания (за `]` — `=` или `+=`) bash раскрывает арифметикой.
                c2 = self.getc(True)
                c3 = self.getc(True) if c2 == "+" else None
                self.ungetc(c3)
                self.ungetc(c2)
                if c2 == "=" or (c2 == "+" and c3 == "="):
                    index = _node(Arith, P(bpos) + 1, P(close_pos), parts=list(inner))
                    parts.part(index)
                else:
                    parts.extend(inner)
                parts.text("]", P(close_pos), _close_end(self, close_pos))
                all_digit = False
                c = self.getc(self._cd() != "'")
                continue
            elif (not whole and c == "=" and tok and (self.assignment_ok(self.last) or (self.state & _ASSIGNOK))
                  and self._token_is_assignment(_flat(tok))):
                epos = self.cpos
                peek = self.getc(True)
                if peek == "(":
                    words = yield self.compound_assignment()
                    tok.append("=(")
                    for k, w in enumerate(words):
                        if k:
                            tok.append(" ")
                        tok.append(_Tpl(w.value._tok or [w.text]))
                    tok.append(")")
                    parts.char("=", P(epos))
                    parts.text("(", P(epos + 1), P(epos + 1) + 1)
                    for k, w in enumerate(words):
                        if k:
                            parts.text(" ", w.value.start, w.value.start)
                        parts.extend(w.value.parts)
                    parts.text(")", P(self.cpos), P(self.cpos) + 1)
                    all_digit = False
                    compound = True
                    c = self.getc(self._cd() != "'")
                    continue
                self.ungetc(peek)
            if c in _BREAK and not whole:
                self.ungetc(c)
                end_char = c
                break
            tok.append(c)
            parts.char(c, P(self.cpos))
            all_digit = all_digit and c in _DIGITS
            if c == "$":
                dollar_present = True
            c = self.getc(self._cd() != "'" and not pass_next)
        if self.reexpand and not whole:
            parts = yield self._reexpand_word(tok, start, self.pos if c is not None else self.n0)
        self.reexpand = outer
        if whole:
            return parts.done()
        if sub_quotes:
            # '…' в индексе присваивания (`a['…']=`, `[…]=` в скобках массива, аргумент declare): индекс bash
            # раскрывает арифметикой — подстановки в '…' исполняются; у слова без `=` это обычные кавычки.
            text = _flat(tok)
            if _assignment(text, bool(self.state & _COMPASSIGN)) > 0:
                index = next((p for p in parts.parts if type(p) is Arith), None)
                for a, b in sub_quotes:
                    sub, _ = yield self.arith_quote(self.s[a + 1:b], [(0, P(a + 1))], a + 1)
                    for node in sub:
                        if type(node) is not Lit:
                            if index is not None:
                                index.parts.append(node)
                            else:
                                parts.part(node)
        return self._got_token(tok, parts, start, all_digit, dollar_present, quoted, compound, end_char,
                               self.pos if c is not None else self.n0)

    def _reexpand_word(self, tok, start, end):
        """Части слова, как его раскрывает bash, когда разбор подстановки в нём при раскрытии расходится с разбором
        при чтении (esc_split): текст слова — подстановки напечатанными (print_comsub) — разбирается заново без
        разделителей (expand_word_internal, string_extract_double_quoted → xparse_dolparen). Конец такой подстановки
        при раскрытии бывает дальше или ближе, чем при чтении: `"$(a=(\\)) ; touch P)"` исполняет `touch P`."""
        anchors = []
        text = _render(tok, anchors)
        key = ("reexpand_word", self.P(start), text)
        done = self.shared.get(key)
        if done is None:
            sub = _R(text, [(0, self.P(start))], self.comments, shared=self.shared, tail=False,
                     limit=self.PE(end), foreign=_seeds(anchors))
            done = yield sub.scan_word()
            self.shared[key] = done
        parts = _Parts()
        parts.extend(done)
        return parts

    def group_word(self, cd, gpos, wpos):
        """Группа `(…)` образца `=~` или шаблона extglob с позиции gpos: parse_matched_pair находит её конец без
        разбора подстановок внутри, раскрывает её bash при исполнении, как слово (wpos — начало, с `@` и т. п.)."""
        k0 = len(self.jumps)
        self._dpush("(")
        qc = "'" if cd == "'" else '"' if cd == '"' else None
        close = self._group_pair(gpos, qc)
        if close is not None and self.skip_to(close):
            # Конец группы уже нашёл parse_matched_pair внешней группы (вложенная `=~` в подстановке в группе).
            self.getc(qc != "'")
        else:
            saved, self.group_rec = self.group_rec, (gpos, qc)
            yield self.pmp(cd, "(", ")", 0, gpos)
            self.group_rec = saved
        self._dpop()
        close_pos = self.cpos
        ranges = self.raw_ranges(wpos, close_pos + 1, k0, len(self.jumps))
        text = "".join(self.s[a:b] for a, b in ranges)
        sub = _R(text, self.orig_pieces(ranges), self.comments, shared=self.shared, tail=False,
                 foreign=self.memo_in(ranges))
        if len(ranges) == 1:
            a = ranges[0][0]
            if self.group_pairs:
                sub.group_views.append((self.group_pairs, a))
            sub.group_views.extend((d, off + a) for d, off in self.group_views)
        parts = yield sub.scan_word()
        return parts

    def _group_pair(self, gpos, qc):
        """Позиция `)` группы с `(` на gpos, найденная parse_matched_pair внешней группы (group_pairs этого разбора
        или разбора, читавшего тот же текст, — group_views) с тем же разделителем; None — неизвестна."""
        hit = self.group_pairs.get(gpos)
        if hit is None:
            for pairs, off in self.group_views:
                hit = pairs.get(gpos + off)
                if hit is not None:
                    hit = (hit[0] - off, hit[1])
                    break
        if hit is None or hit[1] != qc or not gpos < hit[0] < self.n0:
            return None
        return hit[0]

    def _group_tok(self, text, gpos):
        """Кусок лексемы — текст группы `(…)` с позиции gpos, как в исходном тексте; с ним — подстановки, разобранные
        в ней (memo): повторный разбор напечатанного текста берёт их готовыми."""
        piece = _Tpl([text])
        piece.memo = self.memo_in([(gpos, gpos + len(text))]) or None
        return piece

    def scan_word(self):
        """Строка целиком одним словом, как её раскрывает bash при исполнении (группа `=~`, extglob)."""
        c = self.getc(True)
        if c is None:
            return []
        parts = _Parts()
        self.scanning = True
        try:
            yield self.read_token_word(c, self.cpos, whole=True, parts=parts)
        except _ScanStop as stop:
            parts.extend(x for x in stop.parts if type(x) is not Lit)
        except (_Fatal, _Discard) as exc:
            # Ошибка разбора подстановки при раскрытии слова: раскрытие кончается на ней.
            parts.part(_failed_sub(self, exc.pos))
        return parts.done()

    def raw_ranges(self, a, b, k0, k1):
        """Текст конструкции от a до b (не включая), который bash разберёт ещё раз при раскрытии: исходный текст
        подряд — вместе с телами heredoc вложенных подстановок, как их печатает print_comsub. Если ввод возвращался
        назад (остаток строки терминатора перед остатком текущей строки), — прочитанные куски по порядку."""
        for frm, to in self.jumps[k0:k1]:
            if to < frm:
                return self.ranges_between(a, b, k0, k1)
        return [(a, b)] if b > a else []

    def ranges_between(self, a, b, k0, k1):
        """Прочитанный текст от позиции a до b (не включая) — куски [(начало, конец)) без пропусков ввода (строк
        тел heredoc, снятых `\\` с переводом строки), по записям jumps[k0:k1]."""
        out = []
        cur = a
        for frm, to in self.jumps[k0:k1]:
            if frm > b:
                break
            if frm > cur:
                out.append((cur, frm))
            cur = max(cur, to)
        if b > cur:
            out.append((cur, b))
        return out

    def _cd(self):
        """current_delimiter: верхний разделитель стека (None — вне кавычек и подстановок)."""
        for c in reversed(self.dstack):
            if c == "":
                return None
            if c is not None:
                return c
        return None

    def _dpush(self, c):
        """push_delimiter: при раскрытии строки (тело heredoc, слово `=~`) стек разделителей разбора пуст —
        подстановку там находит раскрытие, а не read_token_word."""
        self.dstack.append(None if self.scanning else c)

    def _dpop(self):
        if self.dstack:
            self.dstack.pop()

    def _param(self, res, dpos):
        """Param `${…}` из результата pmp и позиции `$`."""
        inner, close_pos, text = res
        return _node(Param, self.P(dpos), _close_end(self, close_pos), text="${" + text, parts=inner)

    def _token_is_assignment(self, t):
        """token_is_assignment: текст слова с `=` за ним — начало присваивания (`x`, `x+`, `a[1]`)."""
        r = _assignment(t + "=", bool(self.state & _COMPASSIGN))
        return r > 0 and r == len(t)

    def _got_token(self, tok, parts, start, all_digit, dollar_present, quoted, compound, end_char, end):
        """Конец read_token_word: вид лексемы по тексту — NUMBER, особые слова (special_case_tokens),
        зарезервированные (CHECK_FOR_RESERVED_WORD), REDIR_WORD, присваивание или слово; флаги parser_state."""
        text = _flat(tok)
        last = self.last
        word = _node(Word, self.P(start), self.PE(end), parts=parts.done())
        word._tok = tok
        tstart, tend = word.start, word.end
        if all_digit and (end_char in ("<", ">") or last in ("<&", ">&")):
            try:
                value = int(text)
            except ValueError:
                value = None
            if value is not None and value < 2 ** 31:
                return _Tok("NUMBER", text, tstart, tend, text)
        kind = self._special_case(text)
        if kind is not None:
            return _Tok(kind, word, tstart, tend, text)
        if not dollar_present and not quoted and self.resword_ok(last) and text in _RESERVED:
            kind = self._reserved(text)
            if kind is not None:
                return _Tok(kind, word, tstart, tend, text)
        compassign = bool(self.state & _COMPASSIGN)
        is_assign = _assignment(text, compassign) > 0
        assign_word = is_assign and (self.assignment_ok(last) or compassign)
        if self.command_position(last) or (self.state & _CMDBLTIN):
            if text in _ASSIGN_BUILTINS:
                self.state |= _ASSIGNOK
                self.assign_builtin = text
            else:
                self.state &= ~_CMDBLTIN
        else:
            self.state &= ~_CMDBLTIN
        if text[:1] == "{" and text[-1:] == "}" and end_char in ("<", ">") and len(text) > 2:
            inner = text[1:-1]
            if _valid_ident(inner) or _valid_array_ref(inner):
                return _Tok("REDIR_WORD", text, tstart, tend, text)
        kind = "ASSIGN" if assign_word else "WORD"
        if last == "function":
            self.state |= _ALLOWOPNBRC
        elif last in ("case", "for"):
            self.expecting_cmd = last
            self.expecting_in += 1
        elif last == "select":
            self.expecting_in += 1
        return _Tok(kind, word, tstart, tend, text)

    def _reserved(self, text):
        """CHECK_FOR_RESERVED_WORD."""
        kind = text
        if self.state & _CASEPAT and kind != "esac":
            return None
        if kind == "time" and self.last not in _TIME_OK:
            return None
        if kind == "time" and self.last in (None, ";", "\n") and self.before == "|":
            return None
        if self.state & _CASEPAT and self.last in ("|", "(") and kind == "esac":
            return None
        if kind == "esac":
            self.state &= ~(_CASEPAT | _CASESTMT)
            self.esacs_needed -= 1
        elif kind == "case":
            self.state |= _CASESTMT
            self.expecting_cmd = "case"
        elif kind == "]]":
            self.state &= ~(_CONDCMD | _CONDEXPR)
        elif kind == "[[":
            self.state |= _CONDCMD
        elif kind == "for":
            self.expecting_cmd = "for"
        elif kind == "{":
            self.open_brace_count += 1
        elif kind == "}" and self.open_brace_count:
            self.open_brace_count -= 1
        return kind

    def _special_case(self, t):
        """special_case_tokens."""
        last, before = self.last, self.before
        if last == "WORD" and before in ("for", "case", "select") and t == "in":
            if self.expecting_cmd == "case":
                self.state |= _CASEPAT
                self.esacs_needed += 1
            if self.expecting_in:
                self.expecting_in -= 1
            self.expecting_cmd = None
            return "in"
        if self.expecting_in and last in ("WORD", "\n") and t == "in":
            if self.expecting_cmd == "case" and (self.state & _CASESTMT):
                self.state |= _CASEPAT
                self.esacs_needed += 1
            self.expecting_in -= 1
            self.expecting_cmd = None
            return "in"
        if self.expecting_in and last in ("\n", ";") and t == "do":
            self.expecting_in -= 1
            self.expecting_cmd = None
            return "do"
        if last == "WORD" and before in ("for", "select") and t == "do":
            if self.expecting_in:
                self.expecting_in -= 1
            self.expecting_cmd = None
            return "do"
        if self.esacs_needed and last == "in" and t == "esac":
            self.esacs_needed -= 1
            self.state &= ~_CASEPAT
            return "esac"
        if self.state & _ALLOWOPNBRC:
            self.state &= ~_ALLOWOPNBRC
            if t == "{":
                self.open_brace_count += 1
                return "{"
        if last == "ARITH_FOR" and t == "do":
            return "do"
        if last == "ARITH_FOR" and t == "{":
            self.open_brace_count += 1
            return "{"
        if self.open_brace_count and self.resword_ok(last) and t == "}":
            self.open_brace_count -= 1
            return "}"
        if last == "time" and t == "-p":
            return "TIMEOPT"
        if last == "time" and t == "--":
            return "TIMEIGN"
        if last == "TIMEOPT" and t == "--":
            return "TIMEIGN"
        if (self.state & _CONDEXPR) and t == "]]":
            return "]]"
        return None

    # -- parse_matched_pair

    def pmp(self, qc, open_, close, flags, opos):
        """parse_matched_pair: конструкция от символа open_ на позиции opos до парного close. Возврат зависит от
        вида: '…' → (SQ, сырой текст), $'…' → (AnsiC, сырой текст), "…" → (DQ, сырой текст), `…` → (Sub, сырой
        текст), `{` → (части, позиция `}`, текст), `(`, `[` → (части, позиция закрывающей)."""
        P = self.P
        count = 1
        passnext = wasdol = gtlt = False
        dbstate = _DB_PARAM if flags & _P_DOLBRACE else 0
        rflags = _P_DQUOTE if qc == '"' else (flags & _P_DQUOTE)
        # P_DQUOTE parse_matched_pair берёт и из стека разделителей (qc — текущий разделитель), в том числе в теле
        # `$(…)` внутри "…"; раскрытие же как в "…" — только внутри "…" того же слова (_P_INDQ).
        if open_ == '"' or (flags & _P_INDQ):
            rflags |= _P_INDQ
        heredoc = bool(flags & _P_HEREDOC)
        esc_set = _HD_ESC if heredoc else _DQ_ESC
        sq = open_ == "'"
        dq = open_ == '"'
        bq = open_ == "`"
        raw = []
        rawpos = [] if bq else None
        parts = _Parts()
        dq_ctx = bool(flags & _P_INDQ) or dq
        close_pos = None
        scanning = self.scanning
        # Скобки арифметики: открытые на этом уровне и последняя закрытая (её пара и следующий символ — для
        # повторного чтения `((…) …)`, arith_cmd).
        parens = [] if open_ == "(" and (flags & _P_ARITH) else None
        # По скобке parens: начало её текста в raw, число переходов ввода, стоит ли она за `<`, `>`. Текст такой
        # скобки (она может начать `<((…) …)`, которую разбор при раскрытии прочтёт сам) сворачивается в один кусок
        # raw и запоминается (paren_tpl): parse_matched_pair этой подстановки (тот же qc, только P_ARITH) возьмёт
        # конец и текст готовыми, без нового прохода. Прочие скобки не сворачиваются: кусок на каждой вложенной
        # скобке хранил бы свой текст (_flat) — память по квадрату глубины.
        pidx = [] if parens is not None else None
        precord = parens is not None and flags == _P_ARITH
        grec = self.group_rec
        gstack = [] if grec is not None and grec[0] == opos and open_ == "(" and not flags else None
        closed = None
        colon = substr = index_open = False
        sub_depth = 0
        # Начала участков `${a[…]}` и `${x:…}` в частях — для узла Arith.
        index_mark = arith_mark = None
        arith = bool(flags & _P_ARITH)
        # В арифметике `${…}` разбирает не parse_matched_pair, а раскрытие (как в "…"): процесс-подстановку в
        # образце `#`, `%`, `/`, `^`, `,` оно исполняет. Области `${` и такие подстановки — для разбора после.
        dbr = []
        procs = []
        quotes = []
        while count:
            ch = self.getc(qc != "'" and not passnext)
            if ch is None:
                if close is None:
                    break
                raise _Fatal(P(opos))
            cp = self.cpos
            if closed is not None:
                self.paren_next[closed] = ch
                closed = None
            if passnext:
                passnext = False
                if qc != "'" and ch == "\n":
                    if raw:
                        raw.pop()
                        if rawpos is not None:
                            rawpos.pop()
                    continue
                raw.append(ch)
                if rawpos is not None:
                    rawpos.append(cp)
                if sq:
                    continue
                if dq or heredoc:
                    if ch in esc_set and not (heredoc and ch == "\n"):
                        parts.char(ch, P(cp), True)
                    else:
                        parts.char("\\", P(cp) - 1, True)
                        parts.char(ch, P(cp), True)
                else:
                    parts.char("\\", P(cp) - 1, True)
                    parts.char(ch, P(cp), True)
                continue
            if ch == close:
                count -= 1
            elif open_ != close and wasdol and open_ == "{" and ch == open_:
                count += 1
            elif not (flags & _P_FIRSTCLOSE) and ch == open_:
                count += 1
                if parens is not None:
                    parens.append(cp)
                    pidx.append((len(raw) + 1, len(self.jumps), gtlt))
                if gstack is not None:
                    gstack.append((cp, len(self.jumps)))
            if gstack is not None and ch == close and count > 0 and gstack:
                gopen, gjumps = gstack.pop()
                if len(self.jumps) == gjumps:
                    self.group_pairs[gopen] = (cp, grec[1])
            if count == 0:
                close_pos = cp
                if parens is not None:
                    self.paren_memo[opos] = cp
                break
            raw.append(ch)
            if rawpos is not None:
                rawpos.append(cp)
            if parens is not None and ch == ")" and parens:
                closed = parens.pop()
                self.paren_memo[closed] = cp
                i, pjumps, after_gtlt = pidx.pop()
                j = len(raw) - 1
                if after_gtlt and (not quotes or quotes[-1][0] < i) and (not procs or procs[-1][1] < i):
                    # Свёрнутый кусок печатается и сравнивается так же, как символы по одному, но внешние
                    # скобки уже не перебирают его символы.
                    piece = _Tpl(raw[i:j])
                    raw[i:j] = [piece]
                    if precord and pjumps == len(self.jumps):
                        self.paren_tpl[closed] = (cp, piece, qc, scanning)
            if sq:
                if (flags & _P_ALLOWESC) and ch == "\\":
                    passnext = True
                continue
            if ch == "\\":
                passnext = True
                wasdol = False
                gtlt = False
                continue
            if flags & _P_DOLBRACE:
                prev_state = dbstate
                dbstate = _dolbrace_state(dbstate, ch, len(raw))  # raw здесь — по символу на элемент до вложений
                # Индекс `${a[…]}` и смещение `${x:…}` bash раскрывает арифметикой: '…' в них — текст "…".
                if colon:
                    colon = False
                    substr = ch not in "-=?+"
                    if substr:
                        arith_mark = (parts.mark(), P(cp))
                if prev_state == _DB_PARAM and dbstate == _DB_OP and ch == ":":
                    colon = True
                if dbstate == _DB_PARAM and ch == "[":
                    sub_depth += 1
                    if sub_depth == 1:
                        index_open = True
                elif sub_depth and ch == "]":
                    sub_depth -= 1
                    if sub_depth == 0 and index_mark is not None:
                        parts.wrap(index_mark[0], index_mark[1], P(cp))
                        index_mark = None
            handled = False
            try:
                if open_ != close and close is not None:
                    if ch in _QUOTE:
                        self._dpush(ch)
                        qidx = len(raw) - (2 if wasdol else 1)  # начало кавычки в тексте лексемы
                        if wasdol and ch == "'":
                            res = yield self.pmp(ch, ch, ch, _P_ALLOWESC | rflags, cp)
                        else:
                            res = yield self.pmp(ch, ch, ch, rflags, cp)
                        self._dpop()
                        node, inner_raw = res
                        if wasdol and ch == "'":
                            # $'…' в группе bash заменяет значением: в '…' вне "…" и в образце, иначе — как есть.
                            del raw[-2:]
                            if not (rflags & _P_DQUOTE) or dbstate in (_DB_QUOTE, _DB_QUOTE2):
                                raw.append(_sq(node.value))
                            else:
                                raw.append(node.value)
                        else:
                            if wasdol and ch == '"':
                                del raw[-2]
                            raw.append(_Tpl(inner_raw))
                        if wasdol and ch in "'\"":
                            parts.drop_last()
                            node.start = P(cp) - 1
                        arith_sq = (flags & _P_ARITH) or sub_depth or substr
                        # В слове `${x:-…}` (не в образце) внутри "…" и тела heredoc '…' — не кавычки: bash
                        # раскрывает слово, как текст в "…", подстановки в '…' исполняются.
                        if ((flags & _P_DOLBRACE) and dq_ctx and not (flags & _P_UNQ)
                                and dbstate not in (_DB_QUOTE, _DB_QUOTE2)):
                            arith_sq = True
                        if ch == "'" and (flags & _P_ARRAYSUB) and not wasdol:
                            self.sub_quotes.append((cp, self.cpos))
                        if ch == "'" and arith_sq and not wasdol:
                            # В арифметике '…' — не кавычки: выражение раскрывается, как текст в "…".
                            sub, failed = yield self.arith_quote(self.s[cp + 1:self.cpos], [(0, P(cp + 1))], cp + 1)
                            if type(raw[-1]) is _Tpl:
                                raw[-1].memo = self.quote_memo
                            if failed:
                                quotes.append((qidx, P(cp)))
                            else:
                                parts.text("'", P(cp), P(cp) + 1)
                                parts.extend(sub)
                                parts.text("'", P(self.cpos), P(self.cpos) + 1)
                        elif ch == "'" and (flags & _P_ARITH) and ("$" in node.value or "`" in node.value):
                            # $'…' в арифметике bash заменяет её значением в '…' (ansiexpand, sh_single_quote), а
                            # '…' арифметика раскрывает: подстановки в значении исполняются.
                            parts.part(node)
                            sub, failed = yield self.arith_quote(node.value, [(0, node.start)])
                            if failed:
                                quotes.append((qidx, node.start))
                            else:
                                parts.extend(x for x in sub if type(x) is not Lit)
                        else:
                            parts.part(node)
                        handled = True
                    elif (flags & (_P_ARRAYSUB | _P_DOLBRACE)) and wasdol and ch in "({[":
                        handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate, arith_region=bool(sub_depth or substr))
                        if open_ == ch:
                            count -= 1
                    elif (flags & _P_ARITH) and wasdol and ch == "(":
                        handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate, arith_region=bool(sub_depth or substr))
                        if open_ == ch:
                            count -= 1
                            if parens and parens[-1] == cp:
                                parens.pop()
                                pidx.pop()
                    elif (flags & _P_ARITH) and wasdol and ch == "{":
                        npeek = self.getc(True)
                        self.ungetc(npeek)
                        if npeek in _FUNSUB:
                            handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate, arith_region=bool(sub_depth or substr))
                    elif (flags & (_P_ARRAYSUB | _P_DOLBRACE)) and gtlt and ch == "(":
                        executed = not (dq_ctx and not (flags & _P_UNQ) and (flags & _P_DOLBRACE)
                                        and dbstate not in (_DB_QUOTE, _DB_QUOTE2))
                        handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate, arith_region=bool(sub_depth or substr),
                                                          procsub=True, executed=executed)
                        if open_ == ch:
                            count -= 1
                elif open_ == '"' and ch == "`":
                    bqflags = rflags | (_P_BQDQ if close is not None else 0)
                    res = yield self.pmp(None, "`", "`", bqflags, cp)
                    node, inner_raw = res
                    raw.append(_Tpl(inner_raw))
                    parts.part(node)
                    handled = True
                elif open_ != "`" and wasdol and ch in "({[":
                    handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate, arith_region=bool(sub_depth or substr))
                    if open_ == ch:
                        count -= 1
            except _ScanStop as stop:
                # Раскрытие кончилось ошибкой глубже: найденное до неё остаётся.
                parts.extend(x for x in stop.parts if type(x) is not Lit)
                if flags & _P_TOP:
                    self.scan_failed = True
                    break
                raise _ScanStop(parts.done(), stop.pos) from None
            except (_Fatal, _Discard) as exc:
                if not scanning:
                    raise
                # Ошибка разбора подстановки при раскрытии: bash прекращает раскрытие строки (и команду), уже
                # раскрытое исполнено. Подстановка — с ошибкой в теле, остаток строки не раскрывается.
                if ch in "({" and wasdol:
                    parts.drop_last()
                    parts.part(_failed_sub(self, exc.pos, P(cp) - 1, "$(" if ch == "(" else "${ "))
                if flags & _P_TOP:
                    self.scan_failed = True
                    break
                raise _ScanStop(parts.done(), exc.pos) from None
            if not handled:
                if not bq:
                    parts.char(ch, P(cp))
            if index_open:
                index_open = False
                index_mark = (parts.mark(), P(cp) + 1)
            if arith:
                if wasdol and ch == "{" and not handled:
                    # [состояние, символов, раскрывается ли не как в "…" (внутри образца внешней `${…}`), глубина
                    # скобок `<(…)`].
                    outer = dbr[-1] if dbr else None
                    unq = bool(outer) and (outer[2] or outer[0] in (_DB_QUOTE, _DB_QUOTE2))
                    dbr.append([_DB_PARAM, 0, unq, 0])
                elif dbr:
                    region = dbr[-1]
                    if region[3]:
                        # Внутри `<(…)` области: её конец находит разбор при раскрытии, `}` в ней — не конец `${`.
                        if ch == "(" and not handled:
                            region[3] += 1
                        elif ch == ")":
                            region[3] -= 1
                    elif ch == "}":
                        dbr.pop()
                    else:
                        region[1] += 1
                        region[0] = _dolbrace_state(region[0], ch, region[1])
                        if gtlt and ch == "(" and not handled:
                            region[3] = 1
                            if region[2] or region[0] in (_DB_QUOTE, _DB_QUOTE2):
                                procs.append((cp, len(raw)))
            if ch in "<>" and not gtlt:
                gtlt = True
            else:
                gtlt = False
            if ch == "$" and not wasdol:
                wasdol = True
            else:
                wasdol = False
        # Итог по виду конструкции.
        end_pos = close_pos if close_pos is not None else self.cpos
        for idx, qstart in quotes:
            # Подстановка, не закрытая в '…' арифметики, при раскрытии продолжается за кавычкой: раскрывается
            # текст от кавычки (она — простой символ) до конца выражения, как его хранит bash.
            anchors = []
            text = _render(raw[idx:], anchors)
            sub = _R(text, [(0, qstart)], self.comments, shared=self.shared, tail=False, limit=P(end_pos),
                     foreign=_seeds(anchors))
            inner = yield sub.scan_text(_P_HEREDOC)
            parts.extend(x for x in inner if type(x) is not Lit)
        if procs:
            for cp, idx in procs:
                anchors = []
                text = _render(raw[idx:], anchors)
                sub = _R(text, [(0, P(cp) + 1)], self.comments, shared=self.shared, limit=P(end_pos),
                         foreign=_seeds(anchors))
                body = yield sub.toplevel(")")
                end = body.end + 1 if body.commands else P(cp) + 1
                parts.part(_node(Sub, P(cp) - 1, end, kind=self.s[cp - 1] + "(", body=body))
        if sq:
            text = "".join(raw)
            if flags & _P_ALLOWESC:
                return _node(AnsiC, P(opos), _close_end(self, end_pos), value=_ansi_c(text)), [text, "'"]
            return _node(SQ, P(opos), _close_end(self, end_pos), text=text), [text, "'"]
        if bq:
            text = "".join(raw)
            body = yield self.backquote_body(text, rawpos, bool(flags & _P_BQDQ))
            return _node(Sub, P(opos), _close_end(self, end_pos), kind="`", body=body), [text, "`"]
        if dq and close is not None:
            return _node(DQ, P(opos), _close_end(self, end_pos), parts=parts.done()), raw + ['"']
        if close is None:
            return parts.done(), end_pos
        self.tpl_out = raw + [close]
        if flags & _P_DOLBRACE:
            if arith_mark is not None:
                parts.wrap(arith_mark[0], arith_mark[1], P(end_pos))
            return parts.done(), end_pos, _flat(raw) + "}"
        return parts.done(), end_pos

    def _dollar_word(self, ch, cp, open_, flags, rflags, parts, raw, procsub=False, executed=True, dbstate=0,
                     arith_region=False):
        """parse_dollar_word: `$(`, `${`, `$[` (и `<(`, `>(` в `${…}` и индексе) внутри конструкции."""
        P = self.P
        start = cp - 1
        k0 = len(self.jumps)
        if ch == "(":
            node = yield self.parse_comsub(None, "(", ")", (rflags | _P_COMMAND) & ~(_P_DQUOTE | _P_INDQ), start)
            pieces = self.tpl_out
        elif ch == "{":
            npeek = self.getc(True)
            self.ungetc(npeek)
            if npeek in _FUNSUB:
                node = yield self.parse_comsub(None, "{", "}", rflags | _P_COMMAND, start)
                pieces = self.tpl_out
            else:
                unq = flags & _P_UNQ
                if (flags & _P_DOLBRACE) and dbstate in (_DB_QUOTE, _DB_QUOTE2):
                    unq = _P_UNQ
                if arith_region:
                    # Индекс `${a[…]}`, смещение `${x:…}`: bash раскрывает их арифметикой, как текст в "…".
                    unq, rflags = 0, rflags | _P_INDQ
                res = yield self.pmp(None, "{", "}", _P_FIRSTCLOSE | _P_DOLBRACE | rflags | unq, cp)
                pieces = ["{", _Tpl(self.tpl_out)]
                node = self._param(res, start)
        else:
            inner, close_pos = yield self.pmp(None, "[", "]", rflags | _P_ARITH, cp)
            pieces = ["[", _Tpl(self.tpl_out)]
            node = _node(Arith, P(start), _close_end(self, close_pos), parts=inner)
        if raw and raw[-1] == ch:
            raw.pop()  # открывающий символ — в начале pieces
        piece = _Tpl(pieces)
        piece.balanced = type(node) is Arith and ch == "("
        raw.append(piece)
        parts.drop_last()
        if procsub and isinstance(node, Sub):
            node.kind = self.s[start] + "("
            if not executed:
                # Не процесс-подстановка при раскрытии: её текст раскрывается, как текст в "…" (подстановки
                # `$(…)`, `` `…` `` в нём исполняются), тело, разобранное parse_comsub, — нет.
                # Раскрытие зависит только от узла (его печать, позиции): узел, взятый готовым в другом разборе,
                # раскрывается один раз — иначе каждый уровень вложенности раскрывал бы все более глубокие заново.
                key = ("scan", id(node), self.s[start])
                done = self.shared.get(key)
                if done is None:
                    anchors = []
                    text = self.s[start] + _render([("sub", node)], anchors)
                    sub = _R(text, [(0, node.start)], self.comments, shared=self.shared, tail=False,
                             limit=node.end, foreign=_seeds(anchors, 1))
                    inner = yield sub.scan_text(0)
                    done = self.shared[key] = (node, inner)
                parts.extend(done[1])
                return True
        parts.part(node)
        return True

    def arith_quote(self, text, pieces, src=None):
        """Текст '…' в арифметике (или значение $'…'): bash раскрывает его, как текст в "…" (Q_ARITH): подстановки
        в нём исполняются; разбор — отдельной строкой при раскрытии. (части, не закрыта ли в нём подстановка)."""
        self.quote_memo = None
        if "$" not in text and "`" not in text:
            return ([_node(Lit, pieces[0][1], pieces[0][1] + len(text), text=text)] if text else []), False
        foreign = self.memo_in([(src, src + len(text))]) if src is not None else None
        sub = _R(text, pieces, self.comments, shared=self.shared, tail=False, foreign=foreign)
        parts = yield sub.scan_text(_P_HEREDOC)
        self.quote_memo = sub.sub_memo
        return parts, sub.scan_failed

    def backquote_body(self, text, rawpos, dq):
        """Тело `` `…` ``: bash снимает `\\` перед `\\`, `` ` ``, `$` (в "…" — и перед `"`) и разбирает его при
        раскрытии отдельной строкой (command_substitute)."""
        out = []
        pieces = []
        i, n = 0, len(text)
        escapable = "\\`$\"" if dq else "\\`$"
        local = 0
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n and text[i + 1] in escapable:
                c = text[i + 1]
                i += 1
            out.append(c)
            orig = rawpos[i] if i < len(rawpos) else (rawpos[-1] + 1 if rawpos else 0)
            pieces.append((local, self.P(orig)))
            local += 1
            i += 1
        body_text = "".join(out)
        # Куски по одному символу сливаются в непрерывные.
        merged = []
        for lp, op in pieces:
            if merged and op - merged[-1][1] == lp - merged[-1][0]:
                continue
            merged.append((lp, op))
        sub = _R(body_text, merged or [(0, self.P(rawpos[0]) if rawpos else 0)], self.comments, shared=self.shared)
        script = yield sub.toplevel()
        return script

    # -- parse_comsub

    def parse_comsub(self, qc, open_, close, flags, dpos):
        """Подстановка с позиции dpos (`$`, `<`, `>`): `$(…)`, `$((…))`, `${ …; }`, `${| …; }`, `<(…)`, `>(…)` —
        Sub или Arith; тело разбирается тем же разбором со знаком конца close (parse_comsub). Подстановка, которую
        разбор при раскрытии прочтёт иначе (esc_split в её теле), отмечает слово вокруг (reexpand)."""
        node = yield self._parse_comsub(qc, open_, close, flags, dpos)
        if self.shared.get(("reexpand", id(node))) is node:
            self.reexpand = True
        return node

    def _parse_comsub(self, qc, open_, close, flags, dpos):
        P = self.P
        ctx = self._sub_ctx(open_)
        memo = self.sub_memo.get(dpos)
        if memo is not None:
            node, end, tpl, mctx, _ = memo
            if mctx == ctx and self.advance_to(end):
                self.tpl_out = tpl
                return node
        origin = None
        hit = self._shared_get(dpos, ctx)
        if hit is not None:
            node, tpl = hit
            self._memo(dpos, node, tpl, ctx)
            self.tpl_out = tpl
            return node
        memo = self.foreign.pop(dpos, None)
        if memo is not None:
            self.foreign_keys = None
            node, end, tpl, mctx, _ = memo
            if mctx == ctx and self.skip_to(end):
                self._memo(dpos, node, tpl, ctx)
                self.tpl_out = tpl
                return node
            origin = tpl[0]
            # Разбор в этой обстановке годится, только если текст с dpos тот же: печать (print_comsub) и исходный
            # текст расходятся пробелами, и длина из другого текста сдвинула бы чтение.
            for alt_node, alt_text, alt_tpl in (origin.alts or {}).get(ctx, ()):
                if self.s.startswith(alt_text, dpos) and self.skip_to(dpos + len(alt_text)):
                    self._memo(dpos, alt_node, alt_tpl, ctx)
                    self.tpl_out = alt_tpl
                    return alt_node
            # Подстановку разбирать заново (иная обстановка): готовыми остаются внешние подстановки в её тексте —
            # если он здесь в печатном виде (позиции записей — позиции печати).
            anchors = []
            printed = _render(list(origin), anchors)
            if self.s.startswith(printed, dpos + 1):
                for key, entry in _seeds(anchors, dpos + 1).items():
                    self.foreign.setdefault(key, entry)
            self.foreign_keys = None
        kind_char = self.s[dpos]
        if open_ == "(":
            peek = self.getc(True)
            ppos = self.cpos
            self.ungetc(peek)
            if peek == "(":
                k0 = len(self.jumps)
                hit = self.paren_tpl.get(ppos - 1) if kind_char in "<>" else None
                if hit is not None and hit[2:] == (qc, self.scanning) and self.skip_to(hit[0]):
                    # Конец и текст уже нашёл parse_matched_pair внешней `<((…) …)` (или `$((`, `((`), которая
                    # читала эту строку: тело всё равно разбирается при раскрытии, части арифметики не нужны.
                    self.getc(qc != "'")
                    close_pos, content, inner = hit[0], hit[1], []
                else:
                    inner, close_pos = yield self.pmp(qc, "(", ")", _P_ARITH, ppos - 1)
                    content = _Tpl(self.tpl_out[:-1])
                k1 = len(self.jumps)
                node = yield self._arith_or_comsub(kind_char, dpos, ppos, close_pos, inner, k0, k1, content)
                self.tpl_out = self._memo(dpos, node, ["(", content, ")"], ctx, origin)
                return node
        spec = None
        if open_ == "{":
            peek = self.getc(True)
            spec = peek
            if peek == "\n":
                self.ungetc(peek)
        saved = self._save()
        self.state &= ~(_REGEXP | _EXTPAT | _CONDCMD | _CONDEXPR | _COMPASSIGN | _CASEPAT | _ALEXPNEXT | _SUBSHELL
                        | _REDIRLIST)
        self.state |= _CMDSUBST | _EOFTOKEN | _NOEXPAND
        if open_ == "{":
            self.state |= _FUNSUBST
        self.eof_tok = close
        self.pending = []
        self.esacs_needed = self.expecting_in = 0
        self.expecting_cmd = None
        self.extglob = False
        self.scanning = False
        self.current = "\n"
        self.push_history("DOLPAREN" if open_ == "(" else "DOLBRACE")
        ncomments = len(self.comments)
        split, self.esc_split = self.esc_split, False
        t = yield self.yylex()
        body, t = yield self.compound_list(t, True)
        if t.kind != close:
            raise _Fatal(t.start)
        if self.pending:
            yield self.gather()
        self._restore(saved)
        split, self.esc_split = self.esc_split, split
        if open_ == "(":
            kind = kind_char + "("
        else:
            kind = "${|" if spec == "|" else "${ "
        script = self._script(body, None, P(dpos), t.end, ncomments)
        node = _node(Sub, P(dpos), t.end, kind=kind, body=script)
        if split:
            self.shared[("reexpand", id(node))] = node
        self.tpl_out = self._memo(dpos, node, [("sub", node)], ctx, origin)
        return node

    def _linear(self, a, b):
        """Позиция a в исходном тексте, если строка [a, b) легла в него подряд (один кусок, без предела limit)."""
        if self.pieces is None:
            return a if b <= self.n0 else None
        if b > self.n0:
            return None
        k = bisect.bisect_right(self.piece_starts, a) - 1
        if k < 0 or (k + 1 < len(self.pieces) and self.pieces[k + 1][0] < b):
            return None
        local, orig = self.pieces[k]
        if self.limit is not None and orig + (b - 1 - local) > self.limit:
            return None
        return orig + (a - local)

    def _shared_get(self, dpos, ctx):
        """Подстановка с позиции dpos из self.shared: (узел, куски) или None."""
        if not self.shared:
            return None
        base = self._linear(dpos, dpos + 1)
        if base is None:
            return None
        for text, node, tpl in self.shared.get((base, ctx), ()):
            end = dpos + len(text)
            if self.s.startswith(text, dpos) and self._linear(dpos, end) == base and self.skip_to(end):
                return node, tpl
        return None

    def _sub_ctx(self, open_):
        """Обстановка, от которой зависит разбор подстановки (ключ записей sub_memo, foreign). `$((`: чтение
        parse_matched_pair в текущем режиме — режим раскрытия строки, текущий разделитель, parser_state. Иначе
        разбор тела: разделитель (`\\` в словах тела) и parser_state, каким его ставит parse_comsub."""
        cd = self._cd()
        cd = 0 if cd is None or cd == "`" else cd if cd in "'\"" else 1
        if open_ == "(":
            peek = self.getc(True)
            self.ungetc(peek)
            if peek == "(":
                return (self.scanning, cd, self.state)
        state = self.state & ~(_REGEXP | _EXTPAT | _CONDCMD | _CONDEXPR | _COMPASSIGN | _CASEPAT | _ALEXPNEXT
                               | _SUBSHELL | _REDIRLIST)
        return (open_, cd, state | _CMDSUBST | _EOFTOKEN | _NOEXPAND | (_FUNSUBST if open_ == "{" else 0))

    def _memo(self, dpos, node, pieces, ctx, origin=None):
        """Запись подстановки с позиции dpos, разобранной до self.pos: куски её текста — одним _Tpl с seed (печать
        отмечает его для повторного разбора). Чистая — ввод после неё идёт по строкам подряд (нет отложенных кусков,
        тела heredoc не прочитаны дальше её строки): другой разбор того же текста может перешагнуть её целиком."""
        if type(pieces[0]) is _Tpl and len(pieces) == 1 and pieces[0].seed is not None:
            tpl = pieces
        else:
            piece = _Tpl(pieces)
            piece.seed = (node, ctx)
            tpl = [piece]
        clean = not self.segs and self.frontier == self.lim + 1
        if clean:
            base = self._linear(dpos, self.pos)
            if base is not None:
                self.shared.setdefault((base, ctx), []).append((self.s[dpos:self.pos], node, tpl))
        if origin is not None and clean:
            # Тот же текст, разобранный в иной обстановке, чем при чтении (origin): следующий разбор в этой
            # обстановке возьмёт результат готовым.
            if origin.alts is None:
                origin.alts = {}
            origin.alts.setdefault(ctx, []).append((node, self.s[dpos:self.pos], tpl))
        if dpos not in self.sub_memo:
            self.memo_keys = None
        self.sub_memo[dpos] = (node, self.pos, tpl, ctx, clean)
        return tpl

    def _arith_or_comsub(self, kind_char, dpos, ppos, close_pos, inner, k0, k1, content):
        """`$((…)…)`: арифметика, если текст после `$(` — `(…)` со сбалансированными скобками (subst.c,
        chk_arithsub), иначе подстановка команды, которую bash разбирает при раскрытии. `<((…`, `>((…`:
        процесс-подстановку при раскрытии находит разбор (extract_process_subst → xparse_dolparen), и её конец —
        конец разбора с `)`, а не счёт скобок: в `${x/<((…) …)/y}` он бывает дальше конца, найденного при чтении."""
        P = self.P
        if kind_char in "<>":
            sub = self.view(ppos)
            script = yield sub.toplevel(")")
            return _node(Sub, P(dpos), _close_end(self, close_pos), kind=kind_char + "(", body=script)
        # Текст после `$(`, как его хранит bash: вложенные подстановки — напечатанными (print_comsub).
        anchors = []
        read = _render([content], anchors)
        if read[:1] == "(" and read[-1:] == ")":
            skel = _skel([content])
            arith = skel[:1] == "(" and skel[-1:] == ")" and _chk_arithsub(skel[1:-1])
        else:
            arith = False
        if arith:
            return _node(Arith, P(dpos), _close_end(self, close_pos), parts=_strip_parens(inner))
        # bash разбирает при раскрытии этот текст; позиции его узлов — в пределах подстановки.
        sub = _R(read, [(0, P(ppos))], self.comments, shared=self.shared, limit=P(close_pos),
                 foreign=_seeds(anchors))
        script = yield sub.toplevel()
        if script.error is not None and script.error.fatal and not script.commands:
            # bash разбирает при раскрытии напечатанный текст (print_comsub), а не исходный; разбор исходного не
            # сошёлся — подстановки, разобранные при чтении, остаются (ложный отказ дешевле пропуска).
            return _node(Arith, P(dpos), _close_end(self, close_pos), parts=_strip_parens(inner))
        return _node(Sub, P(dpos), _close_end(self, close_pos), kind=kind_char + "(", body=script)

    def _save(self):
        """Состояние лексера и разбора, которое save_parser_state сохраняет вокруг подстановки."""
        return (self.state, self.current, self.last, self.before, self.ago, self.token_to_read, self.eof_tok,
                self.pending, self.esacs_needed, self.expecting_in, self.expecting_cmd, self.extglob, self.scanning)

    def _restore(self, saved):
        (self.state, self.current, self.last, self.before, self.ago, self.token_to_read, self.eof_tok,
         self.pending, self.esacs_needed, self.expecting_in, self.expecting_cmd, self.extglob,
         self.scanning) = saved

    def compound_assignment(self):
        """parse_compound_assignment: слова `x=(…)` до `)`. Иная лексема — ошибка, после которой bash отбрасывает
        команду и остаток строки (jump_to_top_level DISCARD)."""
        saved = self._save()
        self.last = "WORD"
        assignok = self.state & _ASSIGNOK
        self.state &= ~(_NOEXPAND | _CONDCMD | _CONDEXPR | _REGEXP | _EXTPAT)
        self.state |= _COMPASSIGN
        self.esacs_needed = self.expecting_in = 0
        self.expecting_cmd = None
        words = []
        while True:
            t = yield self.read_token()
            if t.kind == ")":
                break
            if t.kind == "\n":
                continue
            if t.kind not in ("WORD", "ASSIGN"):
                raise _Discard(t.start)
            words.append(t)
        pending = self.pending
        self._restore(saved)
        self.pending = pending
        if assignok:
            self.state |= _ASSIGNOK
        return words

    # -- грамматика

    def can_start(self, t):
        """Лексема t начинает команду (pipeline_command)."""
        return t.kind in _CMD_START

    def compound_list(self, t, empty_ok=False):
        """compound_list: newline_list list0|list1. Возврат (узел или None, следующая лексема)."""
        while t.kind == "\n":
            t = yield self.yylex()
        if not self.can_start(t):
            if empty_ok:
                return None, t
            raise _Fatal(t.start)
        items, seps = [], []
        while True:
            node, t = yield self.and_or(t)
            items.append(node)
            if t.kind in (";", "&", "\n"):
                seps.append(t.kind)
                t = yield self.yylex()
                while t.kind == "\n":
                    t = yield self.yylex()
                if self.can_start(t):
                    continue
            break
        return _seq(items, seps), t

    def and_or(self, t):
        """list1 без `;`, `&`, перевода строки: конвейеры через `&&`, `||` (перевод строки за ними пропускается)."""
        items, ops = [], []
        while True:
            node, t = yield self.pipeline_command(t)
            items.append(node)
            if t.kind in ("&&", "||"):
                ops.append(t.kind)
                t = yield self.yylex()
                while t.kind == "\n":
                    t = yield self.yylex()
                continue
            break
        if len(items) == 1:
            return items[0], t
        return _node(AndOr, items[0].start, items[-1].end, items=items, ops=ops), t

    def pipeline_command(self, t):
        """pipeline_command: `!` и `time` перед конвейером (или пустой командой, nullcmd_terminator)."""
        bang = False
        timed = None
        start = t.start
        while True:
            if t.kind == "!":
                bang = not bang
                t = yield self.yylex()
                continue
            if t.kind == "time":
                timed = "time"
                t = yield self.yylex()
                if t.kind == "TIMEOPT":
                    timed = "time -p"
                    t = yield self.yylex()
                if t.kind == "TIMEIGN":
                    t = yield self.yylex()
                continue
            break
        if (bang or timed) and t.kind in (";", "\n", "&", "EOF"):
            # `!` и `time` без команды: пустая команда; разделитель bash читает ещё раз (token_to_read).
            self.token_to_read = t
            t = yield self.yylex()
            return _node(Pipeline, start, start, commands=[], bang=bang, time=timed), t
        cmds = []
        while True:
            node, t = yield self.command(t)
            cmds.append(node)
            if t.kind in ("|", "|&"):
                t = yield self.yylex()
                while t.kind == "\n":
                    t = yield self.yylex()
                continue
            break
        if len(cmds) == 1 and not bang and not timed:
            return cmds[0], t
        return _node(Pipeline, start, cmds[-1].end, commands=cmds, bang=bang, time=timed), t

    def command(self, t):
        """command: простая команда, составная с перенаправлениями, определение функции, coproc."""
        k = t.kind
        if k == "WORD":
            t2 = yield self.yylex()
            if t2.kind == "(":
                node, t = yield self.function_def(t, t2)
                return node, t
            node, t = yield self.simple_command(t, t2)
            return node, t
        if k in ("ASSIGN", "NUMBER", "REDIR_WORD") or k in _REDIR_OPS:
            node, t = yield self.simple_command(None, t)
            return node, t
        if k == "function":
            node, t = yield self.function_kw(t)
            return node, t
        if k == "coproc":
            node, t = yield self.coproc(t)
            return node, t
        if k in _SHELL_START:
            node, t = yield self.shell_command(t)
            node, t = yield self.redirect_list(node, t)
            return node, t
        raise _Fatal(t.start)

    def redirect_list(self, node, t):
        """redirection_list после составной команды — в node.redirs."""
        redirs = node.redirs if node.redirs is not None else []
        while t.kind in _REDIR_OPS or t.kind in ("NUMBER", "REDIR_WORD"):
            r, t = yield self.redirection(t)
            redirs.append(r)
            node.end = max(node.end, r.end)
        node.redirs = redirs
        return node, t

    def simple_command(self, first, t):
        """simple_command: слова, присваивания и перенаправления до первой иной лексемы; PST_REDIRLIST —
        пока в команде одни перенаправления (make_simple_command)."""
        assigns, words, redirs = [], [], []
        start = first.start if first is not None else t.start
        end = first.end if first is not None else t.start
        if first is not None:
            words.append(first.value)
        while True:
            k = t.kind
            if k == "ASSIGN":
                (words if words else assigns).append(t.value)
                end = t.end
            elif k == "WORD":
                words.append(t.value)
                end = t.end
            elif k in _REDIR_OPS or k in ("NUMBER", "REDIR_WORD"):
                if not words and not assigns:
                    self.state |= _REDIRLIST
                r, t = yield self.redirection(t)
                redirs.append(r)
                end = r.end
                if not words and not assigns:
                    self.state |= _REDIRLIST
                continue
            else:
                break
            self.state &= ~_REDIRLIST
            t = yield self.yylex()
        self.state &= ~_REDIRLIST
        return _node(Simple, start, end, assigns=assigns, words=words, redirs=redirs), t

    def redirection(self, t):
        """redirection: [NUMBER|REDIR_WORD] оператор цель; `<<`, `<<-` открывают heredoc (push_heredoc)."""
        fd = None
        start = t.start
        if t.kind in ("NUMBER", "REDIR_WORD"):
            fd = t.text
            t = yield self.yylex()
            if t.kind not in _NUM_REDIR_OPS:
                raise _Fatal(t.start)
        op = t.kind
        target = yield self.yylex()
        if op in ("<&", ">&"):
            if target.kind == "NUMBER":
                word = _node(Word, target.start, target.end, parts=[_node(Lit, target.start, target.end,
                                                                           text=target.text)])
            elif target.kind == "-":
                word = _node(Word, target.start, target.end, parts=[_node(Lit, target.start, target.end,
                                                                           text="-")])
            elif target.kind == "WORD":
                word = target.value
            else:
                raise _Fatal(target.start)
        elif target.kind == "WORD":
            word = target.value
        else:
            raise _Fatal(target.start)
        if op in ("<<", "<<-"):
            text = target.text
            quoted = any(ch in text for ch in "'\"\\")
            doc = _node(Heredoc, word.start, word.end, parts=[])
            doc.term = _quote_removal(text) if quoted else text
            doc.strip_tabs = op == "<<-"
            doc.quoted = quoted
            doc.word = word
            doc.body_start = doc.body_end = word.end
            doc.body_text = ""
            doc._ranges = []
            if len(self.pending) >= _HEREDOC_MAX:
                raise _Fatal(target.start)
            self.pending.append(doc)
            self.opened.append(doc)
            node = _node(Redir, start, word.end, target=doc)
        else:
            node = _node(Redir, start, word.end, target=word)
        node.op = op
        node.fd = fd
        t = yield self.yylex()
        return node, t

    def function_def(self, name, t):
        """WORD '(' ')' newline_list function_body."""
        t = yield self.yylex()
        if t.kind != ")":
            raise _Fatal(t.start)
        t = yield self.yylex()
        while t.kind == "\n":
            t = yield self.yylex()
        body, t = yield self.function_body(t)
        return _node(Function, name.start, body.end, name=name.value, body=body), t

    def function_body(self, t):
        """function_body: составная команда и перенаправления за ней."""
        if t.kind not in _SHELL_START:
            raise _Fatal(t.start)
        body, t = yield self.shell_command(t)
        body, t = yield self.redirect_list(body, t)
        return body, t

    def function_kw(self, t):
        """`function имя [()] тело`."""
        start = t.start
        name = yield self.yylex()
        if name.kind != "WORD":
            raise _Fatal(name.start)
        t = yield self.yylex()
        if t.kind == "(":
            t = yield self.yylex()
            if t.kind != ")":
                raise _Fatal(t.start)
            t = yield self.yylex()
            while t.kind == "\n":
                t = yield self.yylex()
        elif t.kind == "\n":
            while t.kind == "\n":
                t = yield self.yylex()
        body, t = yield self.function_body(t)
        return _node(Function, start, body.end, name=name.value, body=body), t

    def coproc(self, t):
        """`coproc` составная команда, `coproc ИМЯ` составная команда или `coproc` простая команда."""
        start = t.start
        t = yield self.yylex()
        if t.kind in _SHELL_START:
            body, t = yield self.shell_command(t)
            body, t = yield self.redirect_list(body, t)
            return _node(Coproc, start, body.end, name=None, body=body), t
        if t.kind == "WORD":
            t2 = yield self.yylex()
            if t2.kind in _SHELL_START:
                body, t3 = yield self.shell_command(t2)
                body, t3 = yield self.redirect_list(body, t3)
                return _node(Coproc, start, body.end, name=t.value, body=body), t3
            body, t = yield self.simple_command(t, t2)
            return _node(Coproc, start, body.end, name=None, body=body), t
        if t.kind in ("ASSIGN", "NUMBER", "REDIR_WORD") or t.kind in _REDIR_OPS:
            body, t = yield self.simple_command(None, t)
            return _node(Coproc, start, body.end, name=None, body=body), t
        raise _Fatal(t.start)

    def expect(self, t, kind):
        """Лексема t — вида kind, иначе синтаксическая ошибка."""
        if t.kind != kind:
            raise _Fatal(t.start)

    def shell_command(self, t):
        """shell_command: `( )`, `{ }`, if, while, until, for, select, case, `[[ ]]`, `(( ))`."""
        k = t.kind
        start = t.start
        if k == "(":
            body, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, ")")
            end = t.end
            t = yield self.yylex()
            return _node(Subshell, start, end, body=body, redirs=None), t
        if k == "{":
            body, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, "}")
            end = t.end
            t = yield self.yylex()
            return _node(Group, start, end, body=body, redirs=None), t
        if k == "if":
            clauses = []
            cond, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, "then")
            body, t = yield self.compound_list((yield self.yylex()))
            clauses.append((cond, body))
            orelse = None
            while t.kind == "elif":
                cond, t = yield self.compound_list((yield self.yylex()))
                self.expect(t, "then")
                body, t = yield self.compound_list((yield self.yylex()))
                clauses.append((cond, body))
            if t.kind == "else":
                orelse, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, "fi")
            end = t.end
            t = yield self.yylex()
            return _node(If, start, end, clauses=clauses, orelse=orelse, redirs=None), t
        if k in ("while", "until"):
            cond, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, "do")
            body, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, "done")
            end = t.end
            t = yield self.yylex()
            cls = While if k == "while" else Until
            return _node(cls, start, end, cond=cond, body=body, redirs=None), t
        if k in ("for", "select"):
            node, t = yield self.for_command(t)
            return node, t
        if k == "case":
            node, t = yield self.case_command(t)
            return node, t
        if k == "[[":
            node, t = yield self.cond_command(t)
            return node, t
        if k == "ARITH_CMD":
            end = t.end
            node = _node(ArithCmd, start, end, expr=t.value, redirs=None)
            t = yield self.yylex()
            return node, t
        raise _Fatal(t.start)

    def for_command(self, t):
        """for_command, arith_for_command, select_command (все формы parse.y)."""
        k = t.kind
        start = t.start
        t = yield self.yylex()
        name = None
        words = None
        arith = None
        if t.kind == "ARITH_FOR" and k == "for":
            arith = t.value
            t = yield self.yylex()
            if t.kind in ("\n", ";", "EOF"):
                t = yield self.yylex()
                while t.kind == "\n":
                    t = yield self.yylex()
        elif t.kind == "WORD":
            name = t.value
            t = yield self.yylex()
            if t.kind == ";":
                t = yield self.yylex()
                while t.kind == "\n":
                    t = yield self.yylex()
            else:
                while t.kind == "\n":
                    t = yield self.yylex()
                if t.kind == "in":
                    words = []
                    t = yield self.yylex()
                    while t.kind == "WORD":
                        words.append(t.value)
                        t = yield self.yylex()
                    if t.kind not in ("\n", ";", "EOF"):
                        raise _Fatal(t.start)
                    t = yield self.yylex()
                    while t.kind == "\n":
                        t = yield self.yylex()
        else:
            raise _Fatal(t.start)
        if t.kind == "do":
            body, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, "done")
        elif t.kind == "{":
            body, t = yield self.compound_list((yield self.yylex()))
            self.expect(t, "}")
        else:
            raise _Fatal(t.start)
        end = t.end
        t = yield self.yylex()
        if k == "for":
            node = _node(For, start, end, name=name, words=words, arith=arith, body=body, redirs=None)
        else:
            node = _node(Select, start, end, name=name, words=words, body=body, redirs=None)
        return node, t

    def case_command(self, t):
        """case_command: образцы `[(] слово [| слово…] )`, тело, `;;`, `;&`, `;;&`; последний — без них."""
        start = t.start
        t = yield self.yylex()
        if t.kind != "WORD":
            raise _Fatal(t.start)
        word = t.value
        t = yield self.yylex()
        while t.kind == "\n":
            t = yield self.yylex()
        self.expect(t, "in")
        t = yield self.yylex()
        items = []
        while True:
            while t.kind == "\n":
                t = yield self.yylex()
            if t.kind == "esac":
                break
            if t.kind == "(":
                t = yield self.yylex()
            if t.kind != "WORD":
                raise _Fatal(t.start)
            patterns = [t.value]
            t = yield self.yylex()
            while t.kind == "|":
                t = yield self.yylex()
                if t.kind != "WORD":
                    raise _Fatal(t.start)
                patterns.append(t.value)
                t = yield self.yylex()
            self.expect(t, ")")
            t = yield self.yylex()
            body, t = yield self.compound_list(t, True)
            if t.kind in (";;", ";&", ";;&"):
                items.append((patterns, body, t.kind))
                t = yield self.yylex()
                continue
            if t.kind == "esac":
                items.append((patterns, body, None))
                break
            raise _Fatal(t.start)
        end = t.end
        t = yield self.yylex()
        return _node(Case, start, end, word=word, items=items, redirs=None), t

    def cond_command(self, t):
        """`[[ … ]]` (parse_cond_command): лексемы читает read_token без истории yylex; знак `]]` — COND_END."""
        start = t.start
        # yylex, который вернёт COND_CMD: last_read_token — `[[` на всё время разбора выражения.
        self.push_history("COND_CMD")
        self.state |= _CONDEXPR
        words = []
        # Лексемы для печати — свои у каждого `[[ ]]`: `[[` в подстановке внутри выражения не продолжает список
        # внешнего (иначе лексемы внешнего попали бы в печать внутреннего — цикл: внутренний печатает внешнее слово
        # со своей же подстановкой).
        outer_toks = self.cond_toks
        self.cond_toks = []
        yield self.cond_expr(words)
        ct = self.cond_token
        if ct.kind != "]]":
            raise _Fatal(ct.start)
        self.state &= ~(_CONDEXPR | _CONDCMD)
        self.push_history("]]")
        end = ct.end
        toks = self.cond_toks
        self.cond_toks = outer_toks
        t = yield self.yylex()
        node = _node(Cond, start, end, words=words, redirs=None)
        node._toks = toks
        return node, t

    def cond_read(self):
        """Лексема выражения `[[ ]]` (read_token); для печати — в cond_toks."""
        t = yield self.read_token()
        if t.kind not in ("\n", "]]", "EOF"):
            self.cond_toks.append(t.value if t.kind == "WORD" else t.kind)
        return t

    def cond_skip(self):
        """cond_skip_newlines: лексема выражения `[[ ]]` без переводов строки."""
        while True:
            t = yield self.cond_read()
            if t.kind != "\n":
                self.cond_token = t
                return t

    def cond_expr(self, words):
        """cond_or / cond_and: `||`, `&&` между термами — циклом, без рекурсии."""
        while True:
            yield self.cond_term(words)
            if self.cond_token.kind in ("&&", "||"):
                continue
            return

    def cond_term(self, words):
        """cond_term: `( выражение )`, `!` (циклом), унарный оператор с операндом, бинарный или одно слово."""
        while True:
            t = yield self.cond_skip()
            if t.kind == "]]":
                raise _Fatal(t.start)
            if t.kind == "!" or (t.kind == "WORD" and t.text == "!"):
                continue
            break
        if t.kind == "(":
            yield self.cond_expr(words)
            if self.cond_token.kind != ")":
                raise _Fatal(self.cond_token.start)
            yield self.cond_skip()
            return
        if t.kind == "WORD" and len(t.text) == 2 and t.text[0] == "-" and t.text[1] in _TEST_UNOP:
            words.append(t.value)
            t2 = yield self.cond_read()
            if t2.kind != "WORD":
                raise _Fatal(t2.start)
            words.append(t2.value)
            yield self.cond_skip()
            return
        if t.kind == "WORD":
            words.append(t.value)
            t2 = yield self.cond_read()
            if t2.kind == "WORD" and t2.text in _TEST_BINOP and t2.text != "=~":
                if t2.text in ("=", "==", "!="):
                    self.state |= _EXTPAT
                words.append(t2.value)
            elif t2.kind == "WORD" and t2.text == "=~":
                self.state |= _REGEXP
                words.append(t2.value)
            elif t2.kind in ("<", ">"):
                words.append(_node(Word, t2.start, t2.end, parts=[_node(Lit, t2.start, t2.end, text=t2.kind)]))
            elif t2.kind in ("]]", "&&", "||", ")"):
                self.cond_token = t2
                return
            else:
                raise _Fatal(t2.start)
            self.extglob = bool(self.state & _EXTPAT)
            t3 = yield self.cond_read()
            self.extglob = False
            self.state &= ~(_REGEXP | _EXTPAT)
            if t3.kind != "WORD":
                raise _Fatal(t3.start)
            words.append(t3.value)
            yield self.cond_skip()
            return
        raise _Fatal(t.start)

    # -- верхний уровень (parse_and_execute: команда за командой до ошибки)

    def toplevel(self, eof=None):
        """parse_and_execute: команды (inputunit) до конца ввода; фатальная ошибка кончает разбор, ошибка скобок
        массива — отбрасывает команду и строку. С eof — тело подстановки: compound_list до знака конца."""
        start_comments = len(self.comments)
        commands = []
        error = None
        dropped = []
        if eof:
            self.state |= _CMDSUBST | _EOFTOKEN | _NOEXPAND
            if eof == "}":
                self.state |= _FUNSUBST
            self.eof_tok = eof
            try:
                t = yield self.yylex()
                body, t = yield self.compound_list(t, True)
                if t.kind not in ("EOF", eof):
                    raise _Fatal(t.start)
                if self.pending:
                    yield self.gather()
                if body is not None:
                    commands = _split_lines(body)
            except _Fatal as exc:
                error = SyntaxIssue(exc.pos, True)
            except _Discard as exc:
                error = SyntaxIssue(exc.pos, False)
            script = _node(Script, 0, self.P(self.n0), commands=commands)
            script.error = error
            script.dropped = dropped
            script.comments = sorted(set(self.comments[start_comments:]))
            return script
        while True:
            self.unit_mark = None
            try:
                t = yield self.yylex()
                if t.kind == "EOF":
                    break
                if t.kind == "\n":
                    continue
                node, t = yield self.simple_list(t)
                commands.append(node)
                if t.kind == "EOF":
                    break
            except _Fatal as exc:
                if error is None or not error.fatal:
                    error = SyntaxIssue(exc.pos, True)
                break
            except _Discard as exc:
                if error is None:
                    error = SyntaxIssue(exc.pos, False)
                self._reset()
                begin = self.P(self.unit_mark) if self.unit_mark is not None else exc.pos
                dropped.append((begin, max(begin, self.PE(min(self.frontier, self.n0)))))
        script = _node(Script, 0, self.P(self.n0), commands=commands)
        script.error = error
        script.dropped = dropped
        script.comments = sorted(set(self.comments[start_comments:]))
        return script

    def _reset(self):
        """reset_parser после ошибки скобок массива."""
        self.drop_line()
        self.state = 0
        self.open_brace_count = 0
        self.pending = []
        self.esacs_needed = self.expecting_in = 0
        self.expecting_cmd = None
        self.dstack = []
        self.eof_tok = None
        self.extglob = False
        self.current = self.last = "\n"
        self.token_to_read = _Tok("\n", None, self.P(min(self.pos, self.n0)), self.P(min(self.pos, self.n0)))

    def simple_list(self, t):
        """simple_list верхнего уровня: до перевода строки или конца ввода (без переводов строки внутри)."""
        items, seps = [], []
        while True:
            node, t = yield self.and_or(t)
            items.append(node)
            if t.kind in (";", "&"):
                seps.append(t.kind)
                t = yield self.yylex()
                if t.kind in ("\n", "EOF"):
                    break
                continue
            break
        if t.kind not in ("\n", "EOF"):
            raise _Fatal(t.start)
        if self.pending:
            yield self.gather()
        return _seq(items, seps), t

    def _script(self, body, error, start, end, ncomments):
        """Script тела подстановки из списка команд body."""
        script = _node(Script, start, end, commands=_split_lines(body) if body is not None else [])
        script.error = error
        script.dropped = []
        script.comments = sorted(set(self.comments[ncomments:]))
        return script


class _Tpl(list):
    """Кусок текста лексемы (вложенная конструкция): элементы — строки, ("sub", Sub), _Tpl; flat, text —
    готовый текст (_flat, _render), чтобы вложенные куски не собирались заново на каждом уровне."""
    __slots__ = ("flat", "text", "skel", "balanced", "seed", "alts", "memo")

    def __init__(self, items=()):
        super().__init__(items)
        self.flat = None
        self.text = None
        self.skel = None
        self.balanced = False
        # Подстановка, разобранная parse_comsub (её текст — этот кусок): (узел, обстановка разбора). Повторный
        # разбор напечатанного текста берёт узел готовым (_render с anchors, _R.foreign).
        self.seed = None
        # Разборы того же текста в иной обстановке: обстановка → [(узел, прочитанный текст, куски текста)].
        self.alts = None
        # Текст '…' в арифметике: записи sub_memo разбора, раскрывшего его (arith_quote), в позициях текста.
        self.memo = None


def _flat(pieces):
    """Текст лексемы для проверок вида (зарезервированное слово, присваивание): подстановка — заглушкой."""
    out = []
    stack = [(pieces, 0, len(out))]
    while stack:
        seq, i, mark = stack.pop()
        while i < len(seq):
            p = seq[i]
            i += 1
            if type(p) is str:
                out.append(p)
            elif type(p) is _Tpl:
                if p.flat is not None:
                    out.append(p.flat)
                else:
                    stack.append((seq, i, mark))
                    stack.append((p, 0, len(out)))
                    break
            else:
                out.append("$( )")
        else:
            if type(seq) is _Tpl:
                seq.flat = "".join(out[mark:])
                del out[mark:]
                out.append(seq.flat)
    return "".join(out)


def _skel(pieces):
    """Текст для chk_arithsub: вложенная арифметика, уже признанная сбалансированной, — `()` (её скобки и кавычки
    не меняют счёт), остальное — как печатает bash; без этого счёт на каждом уровне вложенности перечитывал бы все
    более глубокие."""
    out = []
    stack = [(pieces, 0, 0)]
    while stack:
        seq, i, mark = stack.pop()
        while i < len(seq):
            p = seq[i]
            i += 1
            if type(p) is str:
                out.append(p)
            elif type(p) is _Tpl:
                if p.balanced:
                    out.append("()")
                elif p.skel is not None:
                    out.append(p.skel)
                elif p.seed is not None and type(p.seed[0]) is Sub:
                    out.append(_sub_skel(p))
                else:
                    stack.append((seq, i, mark))
                    stack.append((p, 0, len(out)))
                    break
            else:
                out.append(_render([p]))
        else:
            if type(seq) is _Tpl:
                seq.skel = "".join(out[mark:])
                del out[mark:]
                out.append(seq.skel)
    return "".join(out)


def _sub_skel(piece):
    """skel подстановки (кусок с seed, узел Sub) для chk_arithsub: сбалансированная — `()` (её скобки и кавычки не
    меняют счёт), иначе её текст со skel вложенных. Снизу вверх, стеком: каждая считается один раз, иначе каждый
    уровень вложенности пересчитывал бы все более глубокие."""
    stack = [piece]
    while stack:
        cur = stack[-1]
        if cur.skel is not None:
            stack.pop()
            continue
        need = []
        text = _render(list(cur), skel=True, need=need)
        if need:
            stack.extend(need)
            continue
        cur.skel = "()" if _arith_neutral(text) else text
        stack.pop()
    return piece.skel


def _doc_skel(doc, need):
    """skel тела heredoc (_sub_skel): подстановки, разобранные при раскрытии тела (doc.memo), — их skel, если их
    текст в теле — тот же, что печать. None — у них ещё нет skel (они — в need)."""
    if getattr(doc, "skel", None) is not None:
        return doc.skel
    text = doc.body_text
    memo = getattr(doc, "memo", None)
    spans = []
    covered = -1
    for key in sorted(memo or ()):
        if key < covered:
            continue
        node, end, tpl, ctx, clean = memo[key]
        piece = tpl[0]
        if clean and type(node) is Sub and piece.seed is not None:
            covered = end
            if piece.skel is None:
                if need is None:
                    return None
                need.append(piece)
            spans.append((key + 1, end, piece))
    if need:
        return None
    out = []
    prev = 0
    for a, b, piece in spans:
        printed = piece.text if piece.text is not None else _render([piece])
        if text[a:b] == printed:
            out.append(text[prev:a])
            out.append(piece.skel)
            prev = b
    out.append(text[prev:])
    doc.skel = "".join(out)
    return doc.skel


def _render(pieces, anchors=None, skel=False, need=None):
    """Текст, как его хранит и печатает bash (make_command_string, print_comsub в print_cmd.c): слова — текстом
    лексем, подстановки `$(…)`, `${ …; }`, `<(…)` — печатью их тела, heredoc — заголовок в строке команды, тела —
    за разделителем после неё. Пробелы и отступы bash не повторяются — только то, что меняет повторный разбор.
    Без рекурсии: стек элементов; время — по длине результата.

    anchors — список, куда добавляются (начало, конец, кусок) подстановок, уже разобранных parse_comsub (кусок с
    seed), внешних в тексте: повторный разбор текста берёт их готовыми (_seeds). skel — текст для chk_arithsub
    (_skel): вложенные подстановки — их skel; need — список, куда добавляются подстановки без skel (_sub_skel)."""
    out = []
    size = 0
    tail = ""
    pending = [[]]
    stack = [("flush",)] + list(reversed(pieces))
    while stack:
        item = stack.pop()
        kind = type(item)
        if kind is str:
            if item:
                out.append(item)
                size += len(item)
                tail = (tail + item)[-2:]
            continue
        if kind is _Tpl:
            if skel:
                if item.balanced:
                    stack.append("()")
                elif item.seed is not None and item.skel is not None:
                    stack.append(item.skel)
                elif item.seed is not None and need is not None and type(item.seed[0]) is Sub:
                    need.append(item)
                else:
                    stack.extend(reversed(item))
                continue
            if anchors is not None:
                if item.memo:
                    _memo_anchors(item.memo, size, anchors)
                if item.seed is not None:
                    text = item.text if item.text is not None else _render([item])
                    anchors.append((size, size + len(text), item))
                    stack.append(text)
                else:
                    stack.append(("tpl_end", item, len(out)))
                    stack.extend(reversed(item))
            elif item.text is not None:
                stack.append(item.text)
            else:
                stack.append(("tpl_end", item, len(out)))
                stack.extend(reversed(item))
            continue
        if kind is list:
            stack.extend(reversed(item))
            continue
        if kind is tuple:
            tag = item[0]
            if tag == "tpl_end":
                obj, mark = item[1], item[2]
                obj.text = "".join(out[mark:])
                del out[mark:]
                out.append(obj.text)
            elif tag == "sub":
                stack.extend(reversed(_sub_items(item[1])))
            elif tag == "hd":
                pending[-1].append(item[1])
            elif tag == "conn":
                text = item[1]
                if pending[-1]:
                    head = text.rstrip(" ;")
                    docs, pending[-1] = pending[-1], []
                    stack.append("" if text in (";", "\n") else " ")
                    stack.append(("bodies", docs))
                    stack.append(head + "\n")
                else:
                    stack.append(text)
            elif tag == "flush":
                if pending[-1]:
                    docs, pending[-1] = pending[-1], []
                    stack.append(("bodies", docs))
                    stack.append("\n")
            elif tag == "bodies":
                # Тела heredoc подряд; в anchors — подстановки, разобранные при раскрытии тела (body_parts).
                for doc in reversed(item[1]):
                    stack.append(doc.term + "\n")
                    stack.append(("body", doc))
            elif tag == "body":
                doc = item[1]
                if skel:
                    stack.append(_doc_skel(doc, need) or "")
                    continue
                if anchors is not None and getattr(doc, "memo", None):
                    _memo_anchors(doc.memo, size, anchors)
                stack.append(doc.body_text)
            elif tag == "semi":
                if not (tail.endswith("\n") or tail.endswith(";") or tail == " &"):
                    stack.append(";")
            elif tag == "fend":
                stack.append(" }" if tail[-1:] in ("\n", ";", "&") else "; }")
            elif tag == "enter":
                pending.append([])
            elif tag == "leave":
                pending.pop()
            continue
        stack.extend(reversed(_node_items(item)))
    return "".join(out)


def _memo_anchors(memo, base, anchors):
    """Внешние чистые записи memo (позиция `$` → запись parse_comsub) — в anchors со сдвигом base."""
    covered = -1
    for key in sorted(memo):
        if key < covered:
            continue
        node, end, tpl, ctx, clean = memo[key]
        if clean:
            anchors.append((base + key + 1, base + end, tpl[0]))
            covered = end


def _seeds(anchors, shift=0):
    """Записи foreign для разбора текста с anchors (_render): ключ — позиция `$` (`<`, `>`) перед куском."""
    out = {}
    for start, end, piece in anchors:
        if start + shift > 0:
            node, ctx = piece.seed
            out[start + shift - 1] = (node, end + shift, [piece], ctx, True)
    return out


def _sub_items(node):
    """Текст подстановки без её первого символа (`$`, `<`, `>`)."""
    body = node.body
    items = _script_items(body)
    if node.kind in ("${ ", "${|"):
        return ["{" + node.kind[2], ("enter",)] + items + [("flush",), ("leave",), ("fend",)]
    first = body.commands[0] if body.commands else None
    while type(first) in (Sequence, AndOr):
        first = first.items[0]
    space = " " if type(first) in (Subshell, ArithCmd) else ""
    return ["(" + space, ("enter",)] + items + [("flush",), ("leave",), ")"]


def _script_items(script):
    items = []
    for k, command in enumerate(script.commands):
        if k:
            items.append(("conn", "\n"))
        items.append(command)
    return items


def _join(nodes, sep):
    items = []
    for k, node in enumerate(nodes):
        if k:
            items.append(sep)
        items.append(node)
    return items


def _node_items(node):
    """Элементы печати узла (print_cmd.c: make_command_string_internal и print_* по видам команд)."""
    kind = type(node)
    if kind is Word:
        if node._tok is not None:
            return list(node._tok)
        return ["".join(p.text for p in node.parts if type(p) is Lit)]
    if kind is Simple:
        items = _join(node.assigns + node.words, " ")
        for redir in node.redirs:
            if items:
                items.append(" ")
            items.append(redir)
        return items
    if kind is Redir:
        fd = node.fd or ""
        target = node.target
        if type(target) is Heredoc:
            term = _sq(target.term) if target.quoted else target.term
            return [fd + node.op + term, ("hd", target)]
        if node.op in ("<&", ">&"):
            return [fd + node.op, target]
        return [fd + node.op + " ", target]
    if kind is Script:
        return _script_items(node)
    if kind is Sequence:
        items = []
        last = len(node.items) - 1
        for k, item in enumerate(node.items):
            items.append(item)
            sep = node.seps[k] if k < len(node.seps) else None
            if k < last:
                items.append(("conn", {";": ";", "&": " &", "\n": "\n"}[sep] if sep else ";"))
            elif sep == "&":
                items.append(" &")
            elif sep == ";":
                items.append(("semi",))
        return items
    if kind is AndOr:
        items = [node.items[0]]
        for op, item in zip(node.ops, node.items[1:]):
            items.append(("conn", " " + op))
            items.append(item)
        return items
    if kind is Pipeline:
        prefix = (node.time + " " if node.time else "") + ("! " if node.bang else "")
        items = [prefix] if prefix else []
        for k, command in enumerate(node.commands):
            if k:
                items.append(("conn", " |"))
            items.append(command)
        return items
    items = _compound_items(node)
    redirs = getattr(node, "redirs", None)
    for redir in redirs or ():
        items.append(" ")
        items.append(redir)
    return items


def _compound_items(node):
    kind = type(node)
    end = [("flush",), ("semi",)]
    if kind is Subshell:
        return ["( ", node.body, ("flush",), " )"]
    if kind is Group:
        return ["{ ", node.body] + end + [" }"]
    if kind is If:
        items = []
        for k, (cond, body) in enumerate(node.clauses):
            items += (["if "] if not k else end + [" elif "]) + [cond] + end + [" then ", body]
        if node.orelse is not None:
            items += end + [" else ", node.orelse]
        return items + end + [" fi"]
    if kind in (While, Until):
        return ["while " if kind is While else "until ", node.cond] + end + [" do ", node.body] + end + [" done"]
    if kind in (For, Select):
        if kind is For and node.arith is not None:
            head = ["for ((", *(node.arith._tok or []), "))"]
        else:
            words = _join(node.words, " ") if node.words is not None else ['"$@"']
            head = ["for " if kind is For else "select ", node.name, " in "] + words + [";"]
        return head + [" do ", node.body] + end + [" done"]
    if kind is Case:
        items = ["case ", node.word, " in"]
        for patterns, body, term in node.items:
            items.append("\n")
            if patterns and patterns[0].literal() == "esac":
                items.append("(")
            items += _join(patterns, " | ") + [")\n"]
            if body is not None:
                items.append(body)
            items += [("flush",), "\n" + (term or ";;")]
        return items + ["\nesac"]
    if kind is Function:
        return ["function ", node.name, " () ", node.body]
    if kind is Coproc:
        return ["coproc "] + ([node.name, " "] if node.name is not None else []) + [node.body]
    if kind is ArithCmd:
        return ["((", *(node.expr._tok or []), "))"]
    if kind is Cond:
        return ["[[ "] + _join(node._toks or node.words, " ") + [" ]]"]
    return []


def _failed_sub(r, pos, start=None, kind="$("):
    """Подстановка, которую bash не разобрал при раскрытии: тело пустое, с фатальной ошибкой на pos (позиция
    исходного текста)."""
    start = pos if start is None else start
    body = _node(Script, start, max(start, r.PE(r.n0)), commands=[])
    body.error = SyntaxIssue(pos, True)
    body.dropped = []
    body.comments = []
    return _node(Sub, start, max(start, r.PE(r.n0)), kind=kind, body=body)


def _dolbrace_state(state, ch, n):
    """dolbrace_state после символа ch (parse_matched_pair с P_DOLBRACE): n — символов в `${…}` с ним."""
    if state == _DB_PARAM:
        if n > 1 and ch in "%#^,":
            return _DB_QUOTE
        if n > 1 and ch == "/":
            return _DB_QUOTE2
        if ch in "#%^,~:-=?+/":
            return _DB_OP
    elif state == _DB_OP and ch not in "#%^,~:-=?+/":
        return _DB_WORD
    return state


def _close_end(r, close_pos):
    """Конец конструкции, закрытой символом на позиции close_pos."""
    return r.P(close_pos) + 1


def _strip_parens(parts):
    """Части `(…)` без первой `(` и последней `)` (выражение `$((…))`, `((…))`)."""
    parts = list(parts)
    if parts and type(parts[0]) is Lit and parts[0].text.startswith("("):
        first = parts[0]
        rest = first.text[1:]
        parts[0] = _node(Lit, first.start + 1, first.end, text=rest) if rest else None
    if parts and parts[-1] is not None and type(parts[-1]) is Lit and parts[-1].text.endswith(")"):
        last = parts[-1]
        rest = last.text[:-1]
        parts[-1] = _node(Lit, last.start, last.end - 1, text=rest) if rest else None
    return [p for p in parts if p is not None]


def _valid_array_ref(s):
    """`имя[…]` (valid_array_reference, упрощённо)."""
    k = s.find("[")
    return k > 0 and s.endswith("]") and _valid_ident(s[:k])


def _seq(items, seps):
    """Список из items и разделителей seps; один элемент без разделителя — он сам."""
    if len(items) == 1 and not seps:
        return items[0]
    return _node(Sequence, items[0].start, items[-1].end, items=items, seps=seps)


def _split_lines(node):
    """Команды списка по переводам строки верхнего уровня."""
    if type(node) is not Sequence or "\n" not in node.seps:
        return [node]
    out = []
    items, seps = [], []
    for k, item in enumerate(node.items):
        items.append(item)
        sep = node.seps[k] if k < len(node.seps) else None
        if sep == "\n":
            out.append(_seq(items, seps))
            items, seps = [], []
        elif sep is not None:
            seps.append(sep)
    if items:
        out.append(_seq(items, seps))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Публичные функции.


def parse(text, eof=""):
    """Разбор text так, как его читает bash 5.3 в `eval`. eof — знак конца heredoc внешней подстановки (")" для
    тела `$(…)`, "}" для `${ …; }`, "" — текст команды целиком): с ним text разбирается как тело подстановки, а
    строка тела heredoc, начатая терминатором, со знаком конца в остатке кончает тело (make_here_document)."""
    if not isinstance(text, str):
        text = str(text)
    reader = _R(text)
    try:
        return _run(reader.toplevel(eof or None))
    except Exception:  # noqa: BLE001 — сбой разбора не выходит наружу: весь текст — ошибка в начале
        script = _node(Script, 0, len(text), commands=[])
        script.error = SyntaxIssue(0, True)
        script.dropped = []
        script.comments = []
        return script


def walk(node):
    """Все узлы под node (включая его), в порядке текста, без рекурсии. Узел, разобранный один раз и взятый
    готовым в нескольких местах (повторный разбор напечатанного текста, _R.shared), — один раз: иначе обход
    дерева, где одно место раскрывается в разных разборах, рос бы с глубиной экспоненциально."""
    stack = [node]
    seen = set()
    while stack:
        cur = stack.pop()
        if isinstance(cur, Node):
            if id(cur) in seen:
                continue
            seen.add(id(cur))
        yield cur
        kids = cur.children() if isinstance(cur, Node) else []
        stack.extend(reversed(kids))


def simple_commands(script):
    """Все Simple дерева в порядке текста: в телах подстановок, функций, арифметики, heredoc. Команды после
    фатальной ошибки в дерево не попадают."""
    out = []
    for node in walk(script):
        if type(node) is Simple:
            out.append(node)
    out.sort(key=lambda node: node.start)
    return out


def heredocs(line):
    """Heredoc, открытые строкой, в порядке чтения их тел: (терминатор, снимать ли ведущие табы).

    Строка разбирается, как bash читает её первой строкой ввода: тела heredoc, открытых в подстановке, которая
    закрывается на этой же строке (`$(cat <<E)`) или не закрыта до её конца (`` `cat <<E ``, `$(cat <<E`), bash
    читает раньше тел остальной строки. Строка с синтаксической ошибкой скобок присваивания массива heredoc не
    открывает: bash отбрасывает её целиком."""
    if not isinstance(line, str):
        return []
    text = line.split("\n", 1)[0]
    reader = _R(text)
    try:
        script = _run(reader.toplevel())
    except Exception:  # noqa: BLE001
        return []
    if script.error is not None and not script.error.fatal:
        return []
    found = list(reader.gathered)
    seen = {id(doc) for doc in found}
    # Heredoc вне незакрытой на строке подстановки: их тела bash читает после её тел — по порядку открытия.
    for doc in reader.opened:
        if id(doc) not in seen:
            found.append(doc)
            seen.add(id(doc))
    if script.error is not None:
        k = _unclosed_backquote(text)
        if k is not None and script.error.pos == k:
            # `` `…` `` не закрыта до конца строки: bash дочитывает её на следующих строках и разбирает тело при
            # раскрытии — heredoc в её тексте на этой строке открыты, их тела — раньше тел строки.
            inner = heredocs(text[k + 1:])
            head = [(doc.term, doc.strip_tabs) for doc in found]
            return inner + head
    return [(doc.term, doc.strip_tabs) for doc in found]


def _unclosed_backquote(line):
    """Позиция последней неэкранированной `` ` `` строки или None."""
    k = len(line)
    while k > 0:
        k = line.rfind("`", 0, k)
        if k < 0:
            return None
        j = k
        while j > 0 and line[j - 1] == "\\":
            j -= 1
        if (k - j) % 2 == 0:
            return k
    return None
