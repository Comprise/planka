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
  раскрывается заново по своему тексту (`_reexpand_word`); его границу задаёт чтение. Так же раскрывается заново
  слово с `$((…)`, чей конец при раскрытии (extract_delimited_string: `#` за пробелом — комментарий) не там, где у
  чтения (`_xscan`, `_expanded_dparen`).
- Печать подстановки (print_comsub) опускает `;` сразу за телами heredoc (print_cmd.c, was_heredoc): `$(a <<E; b;
  c` ⏎ `E` ⏎ `)` bash исполняет как `b c`. Тело с таким `;` разбирается заново по печати (`_print_drops`, `_render`).
  Подстановка в тексте, который bash раскрывает (тело heredoc), исполняется по исходному тексту до конца, найденного
  разбором со знаком конца, — без знака конца (`_EOF_RULE`). Группа `=~` раскрывается по тексту parse_matched_pair:
  `$'…'` в ней — значением (в '…' или, в "…" подстановки, без кавычек; `group_word`).
- Слово `${имя OP слово}` (OP из `%#/^,:`) прямо в теле heredoc bash извлекает extract_heredoc_dolbrace_string и
  переписывает: `$'…'` — значением, в теле `${ …; }` в нём — без кавычек (`_hd_value`, `_heredoc_dolbrace`).
- Позиции узлов — в исходном тексте: 0 ≤ start ≤ end ≤ len(text). Повторный разбор напечатанного текста (слово,
  раскрытое заново, `$((…) …)`, текст за незакрытой подстановкой в '…' арифметики, `<(…)` в `${…}`) переводит позиции
  по кускам (`printed`): отметки узлов и кусков печати (`_render` с where) и посимвольное совпадение между ними
  (`_align`); разбор куска строки (`'…'` арифметики) — куски самой строки (`orig_pieces`).
- Рекурсии Python по глубине входа нет: разбор написан генераторами, вложенный вызов — `yield генератор`, стек
  вызовов ведёт `_run`. Разбор, `walk` и `simple_commands` линейны по длине текста на видах `LinearityTest`,
  `ReparseLinearityTest` (в том числе `$(( #` и `$(( #)` вложением: комментарий пропускается поиском, `$((…)`,
  чей конец при раскрытии за комментарием, не читается parse_matched_pair — `_commented_dparen`; концы вложенных
  `$((` при раскрытии запоминаются — `_xscan` с memo). Время по квадрату длины и выше остаётся, как в bash, у цепочек
  heredoc в подстановке в теле heredoc (строки тела внутреннего heredoc перечитываются на каждом уровне — поиском
  строки терминатора, без прохода Python) и у вложенных подстановок, чьи слова скобок массива с `\\` при чтении и
  при раскрытии делятся по-разному (`\\'` открывает кавычку только при чтении): каждый уровень раскрытия читает весь
  вложенный текст заново. Работу повторного раскрытия ограничивает `_REEXPAND_BASE` + `_REEXPAND_PER` × длина
  текста: сверх — ошибка вида DEPTH.
- Стек генераторов не глубже `_STACK_MAX`: за пределом разбор кончается ошибкой вида DEPTH, дерево не строится.

Синтаксическая ошибка (`Script.error`): `fatal=True` — bash прекращает чтение (команды до неё исполнены, её команда и
следующие — нет); `fatal=False` — ошибка скобок присваивания массива (parse_compound_assignment): bash отбрасывает
команду и остаток физической строки (reset_parser) и читает со следующей строки (`Script.dropped`). Тела heredoc,
уже прочитанные с той строки, остаются прочитанными; heredoc после ошибки не открыт, его тело читается командами.
`SyntaxIssue.kind` — DEPTH (предел стека) или INTERNAL (сбой разбора): дерева нет, `commands` пусты;
`SyntaxIssue.open` — начало команды верхнего уровня, где ошибка.
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
# `\\` вне кавычек и символ за ним: снятие кавычек оставляет символ.
_BACKSLASH = re.compile(r"\\(.)", re.S)

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
# Своё: верхний уровень тела heredoc (expand_string с quoted == Q_HERE_DOCUMENT): слово `${x#…}` bash извлекает
# extract_heredoc_dolbrace_string.
_P_HDBODY = 0x2000

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
    """Узел класса cls с позициями и полями (не заданные — None). Конец — не раньше начала: bash читает отложенные
    куски ввода не по порядку текста (push_string `((…)`, оказавшейся подоболочкой, остаток строки терминатора), и
    последняя лексема узла бывает левее первой."""
    node = cls.__new__(cls)
    node.start = start
    node.end = end if end >= start else start
    for name in cls.__slots__:
        setattr(node, name, fields.get(name))
    if cls is Lit and "text" in fields:
        node._text = fields["text"]
    return node


class SyntaxIssue:
    """Синтаксическая ошибка: pos — начало лексемы, на которой bash её нашёл; fatal — прекращает ли bash чтение.
    kind — None (ошибка грамматики bash), DEPTH (вложенность глубже _STACK_MAX: разбор остановлен, дерево не
    построено) или INTERNAL (сбой разбора: весь текст — ошибка в начале); open — у ошибки в команде верхнего уровня
    начало этой команды (у ошибки на конце ввода — команды, где открыта незакрытая конструкция), иначе None."""
    __slots__ = ("pos", "fatal", "kind", "open")

    def __init__(self, pos, fatal, kind=None, open=None):
        self.pos = pos
        self.fatal = fatal
        self.kind = kind
        self.open = open

    def __repr__(self):
        kind = f", kind={self.kind!r}" if self.kind is not None else ""
        return f"SyntaxIssue({self.pos}, fatal={self.fatal}{kind})"

    def __eq__(self, other):
        return isinstance(other, SyntaxIssue) and (self.pos, self.fatal, self.kind) == (other.pos, other.fatal,
                                                                                         other.kind)

    __hash__ = None


class Script(Node):
    """Разобранный текст: commands — полные команды в порядке чтения bash (по переводу строки верхнего уровня, с
    телами heredoc); error — None или SyntaxIssue; dropped — диапазоны (start, end), отброшенные после ошибки скобок
    массива; comments — диапазоны (start, end) комментариев `#…` (без перевода строки); rewritten (у parse) —
    переписанные куски (start, end): текст, который bash разбирает в печати, не идущей за исходным посимвольно
    (значение `$'…'`, заголовок heredoc с новым терминатором); узел, начатый в таком куске, начинается в его начале,
    кончающийся в нём — кончается в его конце."""
    __slots__ = ("commands", "error", "dropped", "comments", "rewritten")
    _fields = ("commands",)


class Simple(Node):
    """Простая команда: assigns — присваивания перед именем, words — слова, redirs — перенаправления."""
    __slots__ = ("assigns", "words", "redirs")
    _fields = ("assigns", "words", "redirs")


class Pipeline(Node):
    """Конвейер: commands; bang — `!` (нечётное число); time — None, "time" или "time -p"; _ops — операторы между
    командами ("|", "|&")."""
    __slots__ = ("commands", "bang", "time", "_ops")
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
    без кавычек, табы `<<-` сняты); word — Word терминатора. start, end — слово терминатора.

    Текст тела, совпадающий с исходным текстом на его месте, хранится ссылкой (_body — (строка, начало, конец)): тело
    heredoc в подстановке в теле внешнего входит в тело внешнего, и строки тел всех уровней вложенности повторяли бы
    текст более глубоких — память по квадрату глубины."""
    __slots__ = ("term", "strip_tabs", "quoted", "body_start", "body_end", "parts", "word", "_body", "_ranges",
                 "_pieces", "memo", "skel", "term_end", "_win")
    _fields = ("parts",)

    @property
    def body_text(self):
        body = self._body
        if type(body) is tuple:
            src, a, b = body
            return src[a:b]
        return body

    @body_text.setter
    def body_text(self, text):
        self._body = text


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
    """Текст без кавычек (с `\\` уже снятым). Текст тела heredoc с терминатором в кавычках, совпадающий с исходным
    текстом, хранится ссылкой (строка, начало, конец), как Heredoc.body_text: строки тел всех уровней вложенности
    повторяли бы текст более глубоких."""
    __slots__ = ("_text",)

    @property
    def text(self):
        text = self._text
        if type(text) is tuple:
            src, a, b = text
            return src[a:b]
        return text

    @text.setter
    def text(self, text):
        self._text = text


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
    """Раскрытие параметра: `$x`, `$$`, `${…}`; text — его текст; parts — части слов операторов `${…}`.

    Текст `${…}` не хранится своей строкой: у вложенных `${…}` строки повторяли бы текст вложенных и память росла
    бы квадратично с глубиной. _src — строка текста (`$x`, `$$`); кусок _Tpl текста за `${` (tpl_out pmp, тот же
    объект, что в кусках лексемы: текст — `${` и _flat куска — срез общей строки, _tpl_text); или текст, который
    разбирался (_R.s — у `${имя OP слово}` в теле heredoc, _heredoc_dolbrace), с границами _a, _b в нём."""
    __slots__ = ("_src", "_a", "_b", "parts")
    _fields = ("parts",)

    @property
    def text(self):
        src = self._src
        if type(src) is str:
            return src if self._a is None else src[self._a:self._b]
        return "${" + _tpl_text(src)


class Sub(Node):
    """Подстановка, которую bash исполняет: kind — "$(", "`", "${ ", "${|", "<(", ">("; body — Script тела; _nl — у
    `${ …}`, `${|…}`: перед `}` была лексема перевода строки (was_newline в parse_comsub). _rest — раскрытия текста
    печати тела за знаком конца, который нашёл разбор при раскрытии (_parse_comsub): bash раскрывает их частью слова
    вокруг подстановки, и слово (_Parts.part) берёт их вслед за ней."""
    __slots__ = ("kind", "body", "_nl", "_rest")
    _fields = ("body",)


class Arith(Node):
    """Арифметика `$((…))`, `$[…]`, `((…))`, `for ((…))`: parts — части выражения (подстановки в нём и в его `'…'`
    — Sub)."""
    __slots__ = ("parts", "_tok")
    _fields = ("parts",)


def _same_text(s, pos, ref, bound=None):
    """Конец текста ref — (строка, начало, конец): ссылка вместо среза, чтобы записи подстановок всех уровней
    вложенности не хранили каждая свой текст, — если s (до bound — окно разбора, _R.bound) с позиции pos начинается
    им; иначе None."""
    src, a, b = ref
    end = pos + b - a
    if end > (len(s) if bound is None else bound):
        return None
    if (src is s and a == pos) or s[pos:end] == src[a:b]:
        return end
    return None


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
    """Синтаксическая ошибка, после которой bash прекращает чтение (yyerror, YYABORT). open — начало команды
    верхнего уровня, внутри которой она найдена (SyntaxIssue.open)."""
    def __init__(self, pos):
        super().__init__(pos)
        self.pos = pos
        self.open = None


class _TooDeep(Exception):
    """Стек генераторов разбора глубже _STACK_MAX: разбор остановлен (SyntaxIssue.kind == DEPTH)."""


class _Discard(Exception):
    """Ошибка скобок присваивания массива: jump_to_top_level (DISCARD) — команда и остаток строки отброшены. open —
    как у _Fatal."""
    def __init__(self, pos):
        super().__init__(pos)
        self.pos = pos
        self.open = None


class _ScanStop(Exception):
    """Раскрытие строки (тело heredoc, слово `=~`) кончилось ошибкой разбора подстановки: parts — найденное до неё
    на уровне, откуда брошено."""

    def __init__(self, parts, pos):
        super().__init__(pos)
        self.parts = parts
        self.pos = pos


# Вид SyntaxIssue.kind: вложенность глубже предела и сбой разбора.
DEPTH = "depth"
INTERNAL = "internal"

# Наибольшая глубина стека генераторов разбора (_run). Вложенная конструкция занимает от 2 (`${a:-…}`) до 25
# (`$([[ a =~ ($(…)) ]])`) уровней стека; на корпусах tests/fixtures/bash-*.jsonl и формах фаззера — не больше 80.
# Без предела глубокая вложенность (`${a:-` ×16000, `$(( ` ×16000, `$(` ×20000…40000) съедала память (тексты уровней
# хранили текст вложенных; теперь они — срезы общих строк, _share) и роняла CPython segfault при
# освобождении дерева под пределом памяти: хук выходил с кодом 139 без ответа, и команда исполнялась. За пределом
# разбор кончается ошибкой DEPTH; дерево глубже не строится (у повторных разборов — тот же стек: они вызываются из
# того же _run).
_STACK_MAX = 4000
# Предел работы повторного раскрытия слов (_reexpand_word): сумма длин текстов, разобранных заново, — не больше
# _REEXPAND_BASE + _REEXPAND_PER × длина текста. Без цепочки (каждое слово раскрывается заново один раз) сумма не
# больше длины текста; цепочка `$(a=(\'y) ; … ; b=(\'))` уровень в уровне растёт по квадрату числа уровней (и
# выше: уровень раскрывается и внутри разбора внешнего уровня, который потом раскрывается заново). Около 6 мкс на
# знак: предел — около 0,5 с сверх линейного разбора.
_REEXPAND_BASE = 65536
_REEXPAND_PER = 8
# Ключ остатка предела в _R.shared (общем для всех разборов одного текста).
_REEXPAND_LEFT = "reexpand_left"
# Ключ исходного текста в _R.shared: по нему уточняется перевод позиций напечатанного текста (_align).
_SOURCE = "source"
# Ключ множества переписанных кусков (начало, конец) в _R.shared — Script.rewritten у parse.
_SPANS = "spans"
# Ключ счётчика в _R.shared: сколько раз тело heredoc кончила строка с терминатором и знаком конца подстановки
# (make_here_document, правило shell_eof_token).
_EOF_RULE = "eof_rule"


def _run(gen):
    """Исполняет генератор разбора: `yield g` — вызов вложенного генератора g, его возврат — значение `yield`.
    Стек вызовов — список, рекурсии Python нет. Исключение вложенного генератора бросается в вызвавший. Стек глубже
    _STACK_MAX — _TooDeep из самого _run, мимо обработчиков генераторов."""
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
        if len(stack) > _STACK_MAX:
            raise _TooDeep()
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
    __slots__ = ("parts", "buf", "bstart", "bend", "dollar", "dbend", "pname", "pstart", "pend")

    def __init__(self):
        self.parts = []
        self.buf = []
        self.bstart = self.bend = 0
        self.dollar = None  # позиция неэкранированного `$` в конце buf
        self.dbend = 0  # конец буфера до этого `$`
        self.pname = None  # имя параметра `$имя`, которое ещё читается
        self.pstart = self.pend = 0

    def char(self, c, pos, esc=False, end=None):
        """Символ c на исходной позиции pos (end — позиция за ним; в переписанном куске — его конец)."""
        if end is None:
            end = pos + 1
        if self.pname is not None:
            if not esc and c in _NAME_CHAR:
                self.pname.append(c)
                self.pend = end
                return
            self._end_param()
        if self.dollar is not None:
            dpos = self.dollar
            self.dollar = None
            if not esc and (c in _NAME_START or c in _SPECIAL_PARAM):
                # `$` — начало параметра: текст до него кончается перед ним.
                self.buf.pop()
                self.bend = self.dbend
                if c in _NAME_START:
                    self._flush(dpos)
                    self.pname = ["$", c]
                    self.pstart, self.pend = dpos, end
                else:
                    self._flush(dpos)
                    self.parts.append(_node(Param, dpos, end, _src="$" + c, parts=[]))
                return
        if not self.buf:
            self.bstart = pos
            self.bend = pos
        self.dbend = self.bend
        self.buf.append(c)
        self.bend = end
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
        if type(node) is Sub and node._rest:
            self.parts.extend(node._rest)

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
        self.parts.append(_node(Param, self.pstart, self.pend, _src=name, parts=[]))

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


def _xscan(s, i, known, frame="D", end=None, ansi=False, memo=None, key=None, shallow=False):
    """Конец `$(…)`, найденный при раскрытии (subst.c: extract_command_subst → extract_delimited_string с SX_COMMAND):
    с позиции i (за `$(`) — индекс закрывающей `)` или -1 (конструкция не закрыта: bad substitution). Не
    parse_matched_pair чтения: `#` за пробелом или переводом строки — комментарий до конца строки (`$(( #))"⏎…)`
    кончается дальше), `${…}` вне "…" — простые символы, в "…" — skip_double_quoted и extract_dollar_brace_string.
    Конец вложенной `$(…)`,
    `${ …; }`, `<(…)` (её находит разбор, xparse_dolparen) — known(k) по позиции k первого символа или, если неизвестен,
    запрос: генератор отдаёт k и получает индекс закрывающего символа (None — неизвестен: итог None). Кадры стека: D —
    extract_delimited_string (глубина скобок, комментарий), Q — skip_double_quoted, B — extract_dollar_brace_string в
    "…" (вложенность, dolbrace_state, состояния уровней, начало). frame="Q" — skip_double_quoted с позиции за `"`:
    итог — индекс за закрывающей `"`. end — конец текста в s (окно разбора тела heredoc, _R.bound), иначе len(s).
    ansi — s прочитал parse_matched_pair (группа `=~`, extglob): `$'…'` вне "…" он хранит значением в '…'
    (sh_single_quote), и `$'…'` здесь — одна строка в кавычках, а не `$` и '…' до первой `'` внутри.

    memo — словарь концов вложенных `$((…)`, найденных этим проходом: позиция `$` → (индекс закрывающей `)`, был ли
    в ней комментарий); key — позиция `$` самой конструкции (её итог — туда же). Конец вложенной `$((` не зависит от
    счёта скобок вокруг (тот же проход с её `(`), и проход по внешней конструкции, найдя её, отдаёт её разбору готовой:
    каждый уровень вложенности не перечитывает все более глубокие. Комментарий пропускается поиском перевода строки.
    shallow — проход без вложенных подстановок: неизвестная вложенная — итог None сразу."""
    n = len(s) if end is None else end
    # Кадр D: [вид, глубина, комментарий, открытые вложенные `$((` — (позиция `$`, глубина до неё, комментариев до
    # неё), позиция `$` кадра из "…" или None, комментариев до него]. comments — комментариев с начала прохода.
    stack = [["D", 1, False, [], None, 0]] if frame == "D" else [["Q"]]
    comments = 0
    while True:
        if i >= n:
            return -1
        f = stack[-1]
        kind = f[0]
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if kind == "D" and f[2]:
            j = s.find("\n", i, n)
            if j < 0:
                return -1
            f[2] = False
            i = j + 1
            continue
        if kind == "D" and c == "#" and (i == 0 or s[i - 1] in " \t\n"):
            f[2] = True
            comments += 1
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if ansi and c == "$" and nxt == "'" and kind != "Q":
            # В "…" parse_matched_pair `$'…'` не переводит, и skip_double_quoted видит `'` простым символом.
            k = i + 2
            while k < n and s[k] != "'":
                k += 2 if s[k] == "\\" else 1
            if k >= n:
                return -1
            i = k + 1
            continue
        sub = None
        if c == "$" and nxt == "(":
            stop = known(i)
            if stop is None and memo is not None:
                hit = memo.get(i)
                stop = hit[0] if hit is not None else None
            if stop is not None:
                # Уже известный конец (в том числе вложенной `$((…))`): не перебирать её символы на каждом уровне.
                if stop < 0:
                    return -1
                i = stop + 1
                continue
            if shallow:
                return None
            if i + 2 < n and s[i + 2] == "(":
                # `$((`: extract_delimited_string со второй `(` — она сама откроет следующий уровень.
                if kind == "D":
                    f[3].append((i, f[1], comments))
                    f[1] += 1
                else:
                    stack.append(["D", 1, False, [], i, comments])
                i += 2
                continue
            sub = i
        elif c == "$" and nxt == "{" and i + 2 < n and s[i + 2] in _FUNSUB and kind != "B":
            # В extract_dollar_brace_string `${` раньше проверки `${ …; }` — вложенный `${` (кадр B ниже).
            sub = i
        elif kind == "B" and c in "<>" and nxt == "(":
            sub = i
        if sub is not None:
            stop = known(sub)
            if stop is None:
                stop = yield sub
                if stop is None:
                    return None
            if stop < 0:
                return -1
            i = stop + 1
            continue
        if c == "`":
            j = _backquote_end(s, i + 1, n)
            if j >= n:
                return -1
            i = j + 1
            continue
        if kind == "D":
            if c == "(":
                f[1] += 1
            elif c == ")":
                f[1] -= 1
                opened = f[3]
                if opened and opened[-1][1] == f[1]:
                    at, _, before = opened.pop()
                    if memo is not None:
                        memo[at] = (i, comments > before)
                if f[1] == 0:
                    stack.pop()
                    if memo is not None and f[4] is not None:
                        memo[f[4]] = (i, comments > f[5])
                    if not stack:
                        if memo is not None and key is not None:
                            memo[key] = (i, comments > 0)
                        return i
            elif c == "'":
                j = s.find("'", i + 1, n)
                if j < 0:
                    return -1
                i = j
            elif c == '"':
                stack.append(["Q"])
            i += 1
            continue
        if kind == "Q":
            if c == '"':
                stack.pop()
                if not stack:
                    return i + 1
            elif c == "$" and nxt == "{":
                stack.append(["B", 1, _DB_PARAM, [_DB_PARAM] * _PARAMEXPNEST_MAX, i + 2])
                i += 1
            i += 1
            continue
        # kind == "B"
        if c == "$" and nxt == "{":
            if f[1] < _PARAMEXPNEST_MAX:
                f[3][f[1]] = f[2]
            f[1] += 1
            i += 2
            if f[2] in (_DB_QUOTE, _DB_WORD):
                f[2] = _DB_PARAM
            continue
        if c == "}":
            f[1] -= 1
            if f[1] == 0:
                stack.pop()
            else:
                f[2] = f[3][f[1]] if f[1] < _PARAMEXPNEST_MAX else f[3][0]
            i += 1
            continue
        if c == '"':
            stack.append(["Q"])
            i += 1
            continue
        if c == "'":
            j = s.find("'", i + 1, n)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == "[" and f[2] == _DB_PARAM:
            j = _skip_subscript(s, i, n)
            if j < n and s[j] == "]":
                c = "]"
                i = j
        i += 1
        f[2] = _dolbrace_state(f[2], c, i - f[4])


def _hd_value(s, t, known, end=None):
    """extract_heredoc_dolbrace_string (subst.c): слово `${имя OP слово}` с OP из `%#/^,:` в теле heredoc с позиции t
    (за OP) до закрывающей `}`. bash переписывает его до раскрытия: `$'…'` — значением (в состоянии QUOTE, QUOTE2 —
    в '…', иначе — без кавычек, extquote), `$"…"` — "…"; `${ …; }`, `${|…; }` — вложенный `${` (проверка `${` раньше
    проверки подстановки функции), его тело переписывается так же. `$(…)`, `<(…)`, `` `…` ``, '…', "…" — как есть.
    Генератор: запрос конца вложенной подстановки — как у _xscan. Итог — (индекс `}`, новый текст, [(позиция в новом
    тексте, позиция в s)] у начала каждого куска; у значения `$'…'` — (…, ("span", конец `$'…'` в s))) или -1 (нет
    `}`: bad substitution), None (конец неизвестен). end — конец текста в s (_xscan)."""
    n = len(s) if end is None else end
    out = []
    olen = 0
    marks = [(0, t)]
    nesting = 1
    state = _DB_QUOTE
    i = t
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        j = None
        if c == "\\":
            j = min(i + 2, n)
        elif c == "$" and nxt in ("'", '"'):
            k = i + 2
            if nxt == "'":
                while k < n and s[k] != "'":
                    k += 2 if s[k] == "\\" else 1
                if k >= n:
                    return -1
                value = _ansi_c(s[i + 2:k])
                text = _sq(value) if state in (_DB_QUOTE, _DB_QUOTE2) else value
                k += 1
            else:
                k = yield from _xscan(s, i + 2, known, "Q", end)
                if k is None or k < 0 or k > n:
                    return k if k is None else -1
                text = s[i + 1:k]
            # Значение `$'…'` не идёт за исходным текстом посимвольно — переписанный кусок до конца `'…'`; `$"…"` —
            # исходный текст без `$`.
            marks.append((olen, i, ("span", k)) if nxt == "'" else (olen, i + 1))
            out.append(text)
            olen += len(text)
            marks.append((olen, k))
            i = k
            continue
        elif c == "$" and nxt == "{":
            nesting += 1
            if state in (_DB_QUOTE, _DB_QUOTE2, _DB_WORD):
                state = _DB_PARAM
            j = i + 2
        elif c == "}":
            nesting -= 1
            if nesting == 0:
                return i, "".join(out), marks
            j = i + 1
        elif c == "`":
            j = _backquote_end(s, i + 1, n) + 1
            if j > n:
                return -1
        elif (c == "$" or c in "<>") and nxt == "(":
            if c == "$" and i + 2 < n and s[i + 2] == "(":
                stop = yield from _xscan(s, i + 2, known, "D", end)
            else:
                stop = known(i)
                if stop is None:
                    stop = yield i
            if stop is None:
                return None
            if stop < 0:
                return -1
            j = stop + 1
        elif c == '"':
            j = yield from _xscan(s, i + 1, known, "Q", end)
            if j is None:
                return None
            if j < 0:
                return -1
        elif c == "'":
            k = s.find("'", i + 1, n)
            if k < 0:
                return -1
            j = k + 1
        if j is not None:
            out.append(s[i:j])
            olen += j - i
            i = j
            continue
        out.append(c)
        olen += 1
        i += 1
        if state == _DB_PARAM and c in "%#^," and i - t > 1:
            state = _DB_QUOTE
        elif state == _DB_PARAM and c == "/" and i - t > 1:
            state = _DB_QUOTE2
        elif state == _DB_PARAM and c in "#%^,~:-=?+/":
            state = _DB_OP
        elif state == _DB_OP and c not in "#%^,~:-=?+/":
            state = _DB_WORD
    return -1


def _hd_operator(s, i, end=None):
    """parameter_brace_expand: позиция за оператором `${…}` с позиции i (за `${`), если оператор — из `%#/^,:`
    (bash извлекает слово тела heredoc extract_heredoc_dolbrace_string), иначе None. Имя — до одного из `#%^,:-=?+/@}`
    (`\\` и индекс `[…]` пропускаются), `${#имя}` — длина; `:` перед `-=?+` — проверка пустоты, оператор — следующий
    символ. end — конец текста в s (_xscan)."""
    n = len(s) if end is None else end
    if i < n and s[i] == "#" and i + 1 < n and s[i + 1] in _NAME_START:
        return None
    t = i
    while t < n and s[t] not in "#%^,:-=?+/@}":
        if s[t] == "\\":
            t += 2
            continue
        if s[t] == "[":
            k = _skip_subscript(s, t, n)
            if k < n and s[k] == "]":
                t = k
        t += 1
    if t == i and t < n and s[t] in "-?#@":
        # Особый параметр (`${#}`, `${-}`): имя — он и всё до оператора без `^,`.
        t += 1
        while t < n and s[t] not in "#%:-=?+/@}":
            if s[t] == "\\":
                t += 2
                continue
            t += 1
    if t >= n:
        return None
    c = s[t]
    t += 1
    if c == ":" and t < n and s[t] in "-=?+":
        return None
    return t if c in "%#/^,:" else None


def _xend(s, known):
    """_xscan по s с начала без разбора: индекс закрывающей `)`, -1 или None (нужен разбор неизвестной подстановки)."""
    scan = _xscan(s, 0, known)
    try:
        next(scan)
    except StopIteration as stop:
        return stop.value
    return None


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


_PARAMEXPNEST_MAX = 32


def _backquote_end(s, i, end=None):
    """string_extract с "`": позиция закрывающей `` ` `` с позиции после открывающей (`\\` экранирует) или конец
    текста (end, иначе len)."""
    n = len(s) if end is None else end
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i
        i += 1
    return n


def _skip_subscript(s, i, end=None):
    """skipsubscript (subst.c, skip_matched_pair с флагами 0): индекс `]`, закрывающей `[` на позиции i, или len.
    Как в bash, индекс не кончают `]` во вложенных `[…]`, '…', "…" (skip_double_quoted), `` `…` ``, `$(…)`
    (extract_delimited_string: скобки, кавычки, комментарий) и `${…}` (extract_dollar_brace_string: свои `[…]` в
    имени параметра, dolbrace_state). Текст — _flat: подстановка `$(…)`, `${ …; }`, `<(…)` в нём — заглушка `$( )`;
    xparse_dolparen, которым bash находит конец подстановки в "…" и `${…}`, на заглушке совпадает со счётом скобок.
    Расхождение: `$(…)` и `<(…)` прямо в индексе bash просматривает по напечатанному тексту символами (`)` метки
    `case`, `]` в `<(…)`), здесь — заглушкой. Незакрытая конструкция на любом уровне — len (CHECK_STRING_OVERRUN).
    Стек кадров вместо рекурсии: [вид, счётчик, …]; кадр, закрытый на позиции k, возвращает разбор к k + 1. end —
    конец текста в s (_xscan)."""
    n = len(s) if end is None else end
    stack = [["[", 1]]
    i += 1
    while i < n:
        f = stack[-1]
        kind = f[0]
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if kind == "(" and f[2]:
            # Комментарий в extract_delimited_string (SX_COMMAND) — до перевода строки.
            if c == "\n":
                f[2] = False
            i += 1
            continue
        if kind == "(" and c == "#" and s[i - 1] in " \t\n":
            f[2] = True
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "`":
            j = _backquote_end(s, i + 1, n)
            if j >= n:
                return n
            i = j + 1
            continue
        if kind == "[":
            if c == "[":
                f[1] += 1
            elif c == "]":
                f[1] -= 1
                if f[1] == 0:
                    stack.pop()
                    if not stack:
                        return i
                    # Вложенный `[…]` в имени `${…}`: `]` — символ, dolbrace_state с ним не меняется (PARAM).
            elif c == "'":
                j = s.find("'", i + 1, n)
                if j < 0:
                    return n
                i = j
            elif c == '"':
                stack.append(['"'])
            elif c == "$" and nxt == "(":
                stack.append(["(", 1, False])
                i += 1
            elif c == "$" and nxt == "{":
                stack.append(["{", 1, _DB_PARAM, [_DB_PARAM] * _PARAMEXPNEST_MAX, i + 2])
                i += 1
            i += 1
            continue
        if kind == '"':
            if c == '"':
                stack.pop()
            elif c == "$" and nxt == "(":
                # extract_command_subst: `$((` — extract_delimited_string, иначе xparse_dolparen (на заглушке — то же).
                stack.append(["(", 1, False])
                i += 1
            elif c == "$" and nxt == "{":
                # Функциональная подстановка `${ …; }` в _flat — заглушка; `${` — extract_dollar_brace_string.
                stack.append(["{", 1, _DB_PARAM, [_DB_PARAM] * _PARAMEXPNEST_MAX, i + 2])
                i += 1
            i += 1
            continue
        if kind == "(":
            if c == "$" and nxt == "(":
                # extract_command_subst: тот же счёт скобок, что у этого кадра.
                f[1] += 1
                i += 2
                continue
            if c == "$" and nxt == "{" and i + 2 < n and s[i + 2] in _FUNSUB:
                # extract_function_subst (xparse_dolparen до `}`): счёт `${…}` — приближение.
                stack.append(["{", 1, _DB_PARAM, [_DB_PARAM] * _PARAMEXPNEST_MAX, i + 2])
                i += 2
                continue
            if c == "(":
                f[1] += 1
            elif c == ")":
                f[1] -= 1
                if f[1] == 0:
                    stack.pop()
            elif c == "'":
                j = s.find("'", i + 1, n)
                if j < 0:
                    return n
                i = j
            elif c == '"':
                stack.append(['"'])
            i += 1
            continue
        # kind == "{": extract_dollar_brace_string; f = ["{", nesting, dolbrace_state, dbstate, начало после `${`].
        if c == "$" and nxt == "{":
            if f[1] < _PARAMEXPNEST_MAX:
                f[3][f[1]] = f[2]
            f[1] += 1
            i += 2
            if f[2] in (_DB_QUOTE, _DB_WORD):
                f[2] = _DB_PARAM
            continue
        if c == "}":
            f[1] -= 1
            if f[1] == 0:
                stack.pop()
            else:
                f[2] = f[3][f[1]] if f[1] < _PARAMEXPNEST_MAX else f[3][0]
            i += 1
            continue
        if c == "$" and nxt == "(" or c in "<>" and nxt == "(":
            # extract_command_subst, extract_process_subst: xparse_dolparen, на заглушке — счёт скобок.
            stack.append(["(", 1, False])
            i += 2
            continue
        if c == '"':
            stack.append(['"'])
            i += 1
            continue
        if c == "'":
            j = s.find("'", i + 1, n)
            if j < 0:
                return n
            i = j + 1
            continue
        if c == "[" and f[2] == _DB_PARAM:
            stack.append(["[", 1])
            i += 1
            continue
        i += 1
        f[2] = _dolbrace_state(f[2], c, i - f[4])
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

    def __init__(self, text, pieces=None, comments=None, tail=True, limit=None, foreign=None, shared=None,
                 window=None):
        # window — (начало, конец): разбор куска [начало, конец) строки text без копии (тело heredoc, body_parts):
        # позиции — позиции text, конец текста — конец куска (bound; без tail). Копия тела на каждом уровне
        # вложенности (heredoc в подстановке в теле внешнего) — память по квадрату глубины.
        base, self.bound = (window[0], window[1]) if window is not None else (0, None)
        self.base = base
        n0 = len(text) if window is None else window[1]
        add = ""
        if window is None and tail and text and text[-1] != "\n":
            # shell_getc: последней строке без перевода строки он дописывается; строке, кончающейся нечётным
            # числом `\\`, вместо него — ещё `\\` (снятие `\\` с переводом строки не съедает последний `\\`).
            k = n0
            while k > 0 and text[k - 1] == "\\":
                k -= 1
            add = "\\" if (n0 - k) % 2 else "\n"
        self.s = text + add if add else text
        self.n0 = n0
        self.n = n0 + len(add)
        if pieces is not None and not pieces:
            pieces = [(0, 0)]
        # Переписанные куски (_printed_pieces): номер куска → (начало, конец) в исходном тексте. Печать такого куска
        # не идёт за исходным текстом посимвольно (значение `$'…'`, заголовок heredoc с новым терминатором): узел,
        # начатый в нём, начинается в начале куска, кончающийся в нём — кончается в его конце (P, E).
        self.spans = None
        if pieces and any(len(p) > 2 for p in pieces):
            self.spans = {k: (p[2], p[3]) for k, p in enumerate(pieces) if len(p) > 2}
            pieces = [(p[0], p[1]) for p in pieces]
            if shared is not None:
                shared.setdefault(_SPANS, set()).update(self.spans.values())
        self.pieces = pieces  # [(локальное начало, исходное начало)] по возрастанию или None
        self.limit = limit  # наибольшая исходная позиция (строка напечатана bash: позиции — в пределах подстановки)
        self.piece_starts = [p[0] for p in pieces] if pieces else None
        # Наибольшая исходная позиция каждого куска или None (_printed_pieces): у напечатанного текста кусок между
        # двумя узлами бывает длиннее исходного, и без предела позиции его конца обгоняли бы начало следующего.
        self.caps = None
        # Начало текста, дописанного за концом строки (push_text), или None.
        self.ext = None
        self.ext_his = None
        # Куски печати подстановок, переведённые сдвигом до повторного разбора: [(начало, конец, исходные начало и
        # конец)] (_align, refine).
        self.lazy = None
        self.lazy_starts = None
        self.lazy_lo, self.lazy_hi = 0, -1
        # Сколько heredoc ждут тела снова после скобок присваивания массива (compound_assignment).
        self.regathered = 0
        # Ввод: текущий кусок [pos, lim], стек отложенных кусков, первая непрочитанная строка.
        self.pos = base
        self.lim = base - 1
        self.segs = []
        self.frontier = base
        self.cpos = base
        self.cprev = base
        self.last_i = base - 1
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
        self.gathered_ids = set()
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
        # Общие для всех разборов одного текста: (позиция в исходном тексте, обстановка) → [(текст, узел, куски,
        # исходный конец)] подстановок вне переписанных кусков (_placed): другой разбор того же места с тем же
        # текстом и тем же исходным концом берёт её готовой (раскрытие '…' в арифметике и остатка за ней
        # перечитывает одно место в разных разборах, не вложенных друг в друга).
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
        # Позиции `)` последних скобок присваивания массива (compound_assignment).
        self.assign_close = (0, 0)
        self.scanning = False
        # Строку прочитал parse_matched_pair, и `$'…'` в ней bash хранит переведённой (группа `=~`, extglob): _xscan
        # с ansi.
        self.ansi = False
        # Концы `$((…)` при раскрытии этой строки (_xscan с memo, _dparen_end).
        self.xmemo = {}
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
        sub.base, sub.bound = self.base, self.bound
        sub.pieces, sub.piece_starts, sub.caps, sub.ext = self.pieces, self.piece_starts, self.caps, self.ext
        sub.ext_his = self.ext_his
        sub.spans = self.spans
        sub.set_lazy(self.lazy)
        sub.frontier = start
        # Пары скобок, которые нашёл parse_matched_pair этой строки: `((` в теле `<(((…) …)` — подоболочка без
        # нового прохода (arith_cmd), `<((` — без нового parse_matched_pair (parse_comsub).
        sub.paren_tpl, sub.paren_memo, sub.paren_next = self.paren_tpl, self.paren_memo, self.paren_next
        return sub

    def printed(self, items, lo, hi, text, deep=False):
        """Куски и пределы перевода позиций для разбора напечатанного текста text (печать items, _render) в исходный
        текст: по отметкам узлов и кусков печати (where) и посимвольно между ними (_align); позиции — в [lo, hi].
        deep — отметки и внутри подстановок, разобранных parse_comsub (повторный разбор разбирает их заново)."""
        where = []
        again = _render(items, where=where, deep=deep)
        if again != text:
            return _printed_pieces([(0, lo)], lo, hi)
        return _printed_pieces([(0, lo)] + where, lo, hi, text, self.shared.get(_SOURCE))

    def printed_reader(self, items, lo, hi, text, deep=False, **kw):
        """Разбор напечатанного текста text (печать items): _R с переводом позиций printed и пределом hi."""
        pieces, caps, lazy = self.printed(items, lo, hi, text, deep)
        sub = _R(text, pieces, self.comments, shared=self.shared, limit=_top(pieces, hi), **kw)
        sub.caps = caps
        sub.set_lazy(lazy)
        return sub

    def refine(self, i):
        """Кусок печати подстановки, переведённый сдвигом (_align с lazy), с позицией i — по отметкам её печати и
        посимвольно: повторный разбор не взял её готовой и разбирает заново. Вложенные подстановки в нём снова
        остаются сдвигом до своего повторного разбора: каждый символ уточняется не больше раза на уровень разбора."""
        if not self.lazy:
            return
        idx = bisect.bisect_right(self.lazy_starts, i) - 1
        if idx < 0 or not i < self.lazy[idx][1]:
            return
        entry = self.lazy[idx]
        rest = self.lazy[:idx] + self.lazy[idx + 1:]
        self.set_lazy(rest)
        a, b, o, o2, piece = entry
        src = self.shared.get(_SOURCE)
        if src is None:
            return
        where = []
        text = _render(list(piece), where=where, deep=False)
        if text != self.s[a:b]:
            where = []
        entries = [(a, o)] + [(a + w[0],) + tuple(w[1:]) for w in where]
        new, _, lazy = _printed_pieces(entries, o, max(o, o2 - 1), self.s, src, end=b)
        # Символ за куском (позиция b) — не левее исходного конца куска o2: печать вставляет за подстановкой символы,
        # которых нет в исходном (перевод строки за `)` в печати `case`), и прежний перевод прижимал их к последнему
        # символу куска — предел этого символа сдвигался на символ назад (`)` подстановки кончалась перед собой).
        k = max(0, bisect.bisect_right(self.piece_starts, b) - 1)
        tail = (b, max(self.P(b), o2)) if b < len(self.s) and self.piece_starts[k] != b else None
        old = [p if not (self.spans and j in self.spans) else (p[0], p[1]) + self.spans[j]
               for j, p in enumerate(self.pieces)]
        pieces = [p for p in old if p[0] < a] + new + ([tail] if tail else []) + [p for p in old if p[0] >= b]
        self.spans = {j: (p[2], p[3]) for j, p in enumerate(pieces) if len(p) > 2} or None
        if self.spans:
            self.shared.setdefault(_SPANS, set()).update(self.spans.values())
        self.pieces = [(p[0], p[1]) for p in pieces]
        self.piece_starts = [p[0] for p in self.pieces]
        self.caps = _caps(self.pieces)
        self.set_lazy((self.lazy or []) + lazy)
        if self.ext is not None and a >= self.ext:
            top = _top(new, 0)
            self.ext_his = [(start, max(hi, top) if start <= a else hi) for start, hi in self.ext_his]
        if self.limit is not None:
            self.limit = max(self.limit, _top(new, 0))

    # -- позиции

    def P(self, i):
        """Позиция строки → позиция исходного текста."""
        if i > self.n0 and (self.ext is None or i < self.ext):
            i = self.n0
        if self.pieces is None:
            return i
        k = max(0, bisect.bisect_right(self.piece_starts, i) - 1)
        local, orig = self.pieces[k]
        out = orig + (i - local)
        if self.caps is not None and self.caps[k] is not None:
            out = min(out, self.caps[k])
        if self.spans is not None and k in self.spans:
            out = self.spans[k][0]
        if self.ext is not None and i >= self.ext:
            out = min(out, self._ext_hi(i))
        if self.limit is not None:
            return min(out, self.limit)
        return out

    def _ext_hi(self, i):
        """Наибольшая исходная позиция дописанного текста с позицией i (push_text), не дальше конца исходного."""
        hi = 0
        for start, top in self.ext_his:
            if start <= i:
                hi = top
        src = self.shared.get(_SOURCE)
        return hi if src is None else min(hi, len(src))

    def E(self, i):
        """Позиция за символом строки i в исходном тексте; в переписанном куске — его конец. У дописанного текста
        (push_text) — не дальше конца исходного."""
        end = None
        if (self.spans is not None and self.pieces is not None
                and not (i > self.n0 and (self.ext is None or i < self.ext))):
            k = max(0, bisect.bisect_right(self.piece_starts, i) - 1)
            span = self.spans.get(k)
            if span is not None:
                end = span[1] if self.limit is None else min(span[1], self.limit + 1)
        if end is None:
            end = self.P(i) + 1
        if self.ext is not None and i >= self.ext:
            src = self.shared.get(_SOURCE)
            end = min(end, self._ext_hi(i) + 1, len(src) if src is not None else end)
        return end

    def CE(self, i):
        """Конец символа строки i (E) с правилом CP для `\\`, дописанного в конце ввода."""
        if i >= self.n0 and self.n0 > self.base and (self.ext is None or i < self.ext):
            return self.E(self.n0 - 1)
        return self.E(i)

    def CP(self, i):
        """Позиция символа строки i в исходном тексте: `\\`, дописанный в конце ввода (shell_getc, _R), — на месте
        последнего символа текста: его позиция вышла бы за конец текста."""
        if i >= self.n0 and self.n0 > self.base and (self.ext is None or i < self.ext):
            return self.P(self.n0 - 1)
        return self.P(i)

    def PE(self, i):
        """Конец диапазона: позиция после последнего символа."""
        if i > self.n0 and (self.ext is None or i <= self.ext):
            i = self.n0
        if self.pieces is None or i <= self.base:
            return self.P(i)
        return self.E(i - 1)

    def set_lazy(self, lazy):
        """Куски печати, переведённые сдвигом (по возрастанию начала), и их границы для проверки в getc."""
        self.lazy = sorted(lazy, key=lambda e: e[0]) if lazy else None
        self.lazy_starts = [e[0] for e in self.lazy] if self.lazy else None
        self.lazy_lo = self.lazy[0][0] if self.lazy else 0
        self.lazy_hi = max(e[1] for e in self.lazy) if self.lazy else -1

    def child_lazy(self, ranges):
        """Куски печати подстановок, переведённые сдвигом (lazy), для строки, склеенной из кусков ranges этой строки:
        задетые куском частично уточняются здесь (refine), целиком внутри — переходят в её позиции. Вызывается до
        orig_pieces тех же кусков."""
        if not self.lazy:
            return None
        changed = True
        while changed and self.lazy:
            changed = False
            for la, lb, _, _, _ in list(self.lazy):
                if any(la < b and lb > a and not (a <= la and lb <= b) for a, b in ranges):
                    self.refine(la)
                    changed = True
                    break
        out = []
        local = 0
        for a, b in ranges:
            for la, lb, o, o2, piece in self.lazy or ():
                if a <= la and lb <= b:
                    out.append((local + la - a, local + lb - a, o, o2, piece))
            local += b - a
        return out or None

    def _span_of(self, i):
        """Переписанный кусок (начало, конец) в исходном тексте, где лежит символ строки i, или None."""
        if self.spans is None or self.pieces is None or (i > self.n0 and (self.ext is None or i < self.ext)):
            return None
        return self.spans.get(max(0, bisect.bisect_right(self.piece_starts, i) - 1))

    def orig_pieces(self, ranges):
        """Куски строки [(начало, конец)) → куски производной строки [(локальное начало, исходное начало)]; кусок
        внутри переписанного — (…, начало, конец) переписанного."""
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
                    span = self.spans.get(k) if self.spans is not None else None
                    if span is not None:
                        out.append((local + (i - a), span[0], span[0], span[1]))
                    else:
                        orig = po + (i - pl)
                        if self.caps is not None and self.caps[k] is not None:
                            orig = min(orig, self.caps[k])
                        out.append((local + (i - a), orig))
                    i = stop
                    k += 1
            local += b - a
        return _merge_pieces(out)

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
                e = s.find("\n", f, self.n)
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
            if self.lazy_lo <= i < self.lazy_hi and self.lazy:
                # Символ печати подстановки, переведённой сдвигом, читается заново (её не взяли готовой): уточнить.
                self.refine(i)
            if i != self.last_i + 1:
                self.jumps.append((self.last_i + 1, i))
            self.last_i = i
            return c

    def discard_line(self):
        """discard_until('\\n'): символы до перевода строки прочитаны (как getc(False)) поиском, без чтения по символу и
        без уточнения позиций кусков печати в них (refine): позиции символов комментария не нужны, а уточнение каждого
        куска на каждом уровне вложенности было бы квадратичным (`$(( #)` — комментарий над печатью всех более глубоких
        уровней). Итог — перевод строки или None (конец ввода)."""
        s = self.s
        while True:
            i = self.pos
            if i > self.lim:
                c = self.getc(False)
                if c is None or c == "\n":
                    return c
                continue
            j = s.find("\n", i, self.lim + 1)
            last = self.lim if j < 0 else j - 1
            if last >= i:
                if i != self.last_i + 1:
                    self.jumps.append((self.last_i + 1, i))
                self.cprev = last - 1 if last > i else self.cpos
                self.cpos = self.last_i = last
                self.pos = last + 1
            if j >= 0:
                return self.getc(False)

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

    def push_text(self, text, pieces, caps, hi, lazy=()):
        """push_string с текстом, которого нет в строке (печать print_comsub): он дописывается за концом строки (её
        чтение по строкам туда не доходит), позиции его символов переводят куски pieces с пределами caps (не дальше
        исходной позиции hi), и он читается раньше остатка текущего куска (push_input)."""
        if not text:
            return
        caps = [hi if cap is None else min(cap, hi) for cap in caps]
        if self.bound is not None:
            # Окно: дописанный текст — сразу за его концом, как у копии куска (позиции до конца те же).
            self.s, self.bound = self.s[:self.n0], None
        start = len(self.s)
        if self.ext is None:
            self.ext = start
            self.ext_his = []
        self.ext_his.append((start, hi))
        base = self.pieces if self.pieces is not None else [(0, 0)]
        base_caps = self.caps if self.caps is not None else [None] * len(base)
        spans = dict(self.spans) if self.spans is not None else {}
        for k, piece in enumerate(pieces):
            if len(piece) > 2:
                spans[len(base) + k] = (piece[2], piece[3])
                self.shared.setdefault(_SPANS, set()).add((piece[2], piece[3]))
        self.spans = spans or None
        if lazy:
            self.set_lazy((self.lazy or []) + [(start + a, start + b, o, o2, piece) for a, b, o, o2, piece in lazy])
        self.pieces = list(base) + [(start + piece[0], piece[1]) for piece in pieces]
        self.caps = list(base_caps) + list(caps)
        self.piece_starts = [p[0] for p in self.pieces]
        self.s += text
        self.push_input([(start, start + len(text) - 1)])

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
            if id(doc) not in self.gathered_ids:
                self.gathered_ids.add(id(doc))
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
                self.shared[_EOF_RULE] = self.shared.get(_EOF_RULE, 0) + 1
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
        # Конец строки терминатора: её печать (терминатор за телом) переводится на неё (_render с where).
        doc.term_end = max(doc.body_end, self.PE(min(f, self.n0))) if f > end else doc.body_end
        text = "".join(body_text)
        # Тело — кусок строки разбора подряд с начала строки: body_parts разбирает его окном этой строки (_R window).
        if len(body_ranges) == 1 and not doc.quoted:
            a, b = body_ranges[0]
            if b - a == len(text) and (a == self.base or self.s[a - 1] == "\n") and self.s[a:b] == text:
                doc._win = (self.s, a, b)
        src = self.shared.get(_SOURCE)
        if (src is not None and doc.body_end - doc.body_start == len(text) and src is not text
                and src[doc.body_start:doc.body_end] == text):
            doc._body = (src, doc.body_start, doc.body_end)
        else:
            doc.body_text = text
        doc._ranges = body_ranges
        # Перевод позиций тела в исходный текст: печать (_render с where) ставит тело за строкой команды.
        doc._pieces = self.orig_pieces(body_ranges)
        if doc.quoted:
            doc.parts = [_node(Lit, doc.body_start, doc.body_end, text=doc._body)] if text else []

    def _fast_body(self, f, term, strip, join, eof):
        """Тело heredoc поиском строки терминатора регулярным выражением (тот же результат, что построчное чтение
        read_body, без прохода Python по строкам тела: тело внутреннего heredoc в подстановке в теле внешнего не
        перечитывается построчно на каждом уровне). None — в теле есть склейка `\\` с переводом строки, нужен
        построчный разбор. Иначе (начало строки терминатора, начало следующей строки, остаток строки для shell_ungets
        или None, [текст тела], [куски тела])."""
        s, n = self.s, self.n0
        tabs = r"\t*" if strip else ""
        ends = r"(?:\n|\Z" + (r"|[^\n]*" + re.escape(eof) if eof else "") + ")"
        # Терминатор с переводом строки bash сравнивает с одной строкой (make_here_document): тело — до конца ввода.
        m = None if "\n" in term else re.compile(r"^" + tabs + re.escape(term) + ends, re.MULTILINE).search(s, f, n)
        line_start = m.start() if m else n
        line_end = s.find("\n", line_start, n) if m else n
        if line_end < 0:
            line_end = n
        top = len(s) if self.bound is None else self.bound
        if join and (s.find("\\\n", f, min(line_end + 1, top)) >= 0
                     or (line_end == n and n > f and s[n - 1] == "\\")):
            return None
        push = None
        if m:
            k = line_start
            while k < line_end and s[k] == "\t" and strip:
                k += 1
            if s[k + len(term):min(k + len(term) + 1, top)] not in ("\n", ""):
                # Правило знака конца подстановки: остаток строки — во ввод.
                self.shared[_EOF_RULE] = self.shared.get(_EOF_RULE, 0) + 1
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
        win, doc._win = doc._win, None
        if win is not None and win[0] is not self.s:
            win = None
        if win is not None:
            # Окно строки разбора: позиции тела в этой строке — со сдвигом base (куски, записи подстановок).
            _, base, end = win
            if end <= base:
                return []
            pieces = [(p[0] + base,) + tuple(p[1:]) for p in doc._pieces or [(0, doc.body_start)]]
            foreign = {k + base: (node, e + base, tpl, ctx, clean)
                       for k, (node, e, tpl, ctx, clean) in self.memo_in(doc._ranges).items()}
            sub = _R(self.s, pieces, self.comments, shared=self.shared, tail=False,
                     limit=max(doc.body_start, doc.body_end - 1), foreign=foreign, window=(base, end))
        else:
            text = doc.body_text
            if not text:
                return []
            base = 0
            # Предел — последний символ тела в исходном тексте: в конце ввода тело кончается переводом строки, которого
            # в тексте нет (read_a_line), и его позиция вышла бы за конец текста.
            sub = _R(text, doc._pieces or [(0, doc.body_start)], self.comments, shared=self.shared,
                     tail=False, limit=max(doc.body_start, doc.body_end - 1), foreign=self.memo_in(doc._ranges))
            sub.pos, sub.lim, sub.frontier = 0, -1, 0
        parts = yield sub.scan_text(_P_HEREDOC | _P_HDBODY)
        doc.memo = sub.sub_memo if not base else {k - base: (node, e - base, tpl, ctx, clean)
                                                  for k, (node, e, tpl, ctx, clean) in sub.sub_memo.items()}
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
                c = self.discard_line()
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
        regathered = self.regathered
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
        if self.regathered != regathered:
            # bash возвращает во ввод прочитанное parse_matched_pair — с подстановками в печати print_comsub. Печать
            # расходится с исходным текстом по существу, только если heredoc в подстановке ждёт тела снова
            # (compound_assignment): его заголовок печатается с новым терминатором — прочитанным телом
            # (print_heredoc_header, here_doc_eof). Тогда во ввод идёт печать (зерно 13, глубина 3, номер 15333).
            items = ["("] + expr_tok + [")"]
            text = _render(items)
            pieces, caps, lazy = self.printed(items, self.P(open_pos), self.P(close_pos), text)
            self.push_text(text, pieces, caps, _top(pieces, self.P(close_pos)), lazy)
            return 0, None
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
                parts.char(c, self.CP(self.cpos), True, end=self.CE(self.cpos))
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
                    # Сюда доходят слова, где разделитель — `(`, `{` подстановки или `"` вокруг неё, а PST_NOEXPAND
                    # снят: слова скобок массива в подстановке (parse_compound_assignment) и слова тела `${ …}`
                    # внутри "…" (`"${u-${ a;b\\;` ⏎ `}}"`): `\\` не экранирует. При
                    # раскрытии bash разбирает напечатанный текст подстановки заново без разделителей
                    # (xparse_dolparen), и там этот `\\` экранирует: `x=$(a=(\\'y) ; touch P ; b=(\\'))` исполняет
                    # `touch P`. Перед прочими символами `\\` меняет только значение слова, не границы.
                    self.esc_split = True
                quoted = True
                tok.append(c)
                if not pass_next:
                    parts.char(c, self.CP(self.cpos), True, end=self.CE(self.cpos))
                all_digit = False
                c = self.getc(False if pass_next else cd != "'")
                continue
            if (not whole and (self.state & _FUNSUBST) and not tok and not quoted and c == "}"
                    and self.resword_ok(self.last)):
                tok.append(c)
                parts.char(c, self.CP(self.cpos), end=self.CE(self.cpos))
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
                    parts.char(c, self.CP(self.cpos), end=self.CE(self.cpos))
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
                            body = _Tpl(self.tpl_out)
                            pieces = ["{", body]
                            node = self._param(node, cpos, body)
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
                    # Печать `$'…'` — значение в '…', `$"…"` — "…": отметки начала и конца в исходном тексте.
                    if peek == "'":
                        value = node.value
                        piece = _Tpl([_sq(value)])
                        node.start = P(cpos)
                    else:
                        piece = _Tpl(['"', _Tpl(raw)])
                        node.start = P(cpos)
                    piece.at, piece.to = node.start + (peek == '"'), node.end
                    piece.span = peek == "'"
                    tok.append(piece)
                    parts.part(node)
                    quoted = True
                    all_digit = False
                    c = self.getc(self._cd() != "'")
                    continue
                if c == "$" and peek == "$":
                    tok.append("$$")
                    parts.part(_node(Param, P(cpos), self.E(self.cpos), _src="$$", parts=[]))
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
                parts.text("[", P(bpos), self.E(bpos))
                # Индекс присваивания (за `]` — `=` или `+=`) bash раскрывает арифметикой.
                c2 = self.getc(True)
                c3 = self.getc(True) if c2 == "+" else None
                self.ungetc(c3)
                self.ungetc(c2)
                if c2 == "=" or (c2 == "+" and c3 == "="):
                    index = _node(Arith, P(bpos) + 1, P(close_pos), parts=list(inner))
                    parts.part(index)
                else:
                    # Без `=` за `]` слово раскрывается как обычное: `\\` вне кавычек снимается (`x[\\n] c` —
                    # `x[n]`, проверено bash 5.3); pmp отдаёт текст индекса с ним.
                    parts.extend([_node(Lit, p.start, p.end, text=_BACKSLASH.sub(r"\1", p.text))
                                  if type(p) is Lit and "\\" in p.text else p for p in inner])
                parts.text("]", P(close_pos), _close_end(self, close_pos))
                all_digit = False
                c = self.getc(self._cd() != "'")
                continue
            elif (not whole and c == "=" and tok and (self.assignment_ok(self.last) or (self.state & _ASSIGNOK))
                  and self._token_is_assignment(_flat(tok))):
                epos = self.cpos
                peek = self.getc(True)
                if peek == "(":
                    ncomments = len(self.comments)
                    words = yield self.compound_assignment()
                    # Комментарии, прочитанные в этих скобках, — по началу: для каждого промежутка между словами —
                    # поиск, а не проход всех комментариев текста (время по числу слов × числу комментариев).
                    marks = sorted(a for a, _ in self.comments[ncomments:])
                    tok.append("=(")
                    for k, w in enumerate(words):
                        if k:
                            tok.append(" ")
                        tok.append(_Tpl(w.value._tok or [w.text]))
                    tok.append(")")
                    parts.char("=", self.CP(epos), end=self.CE(epos))
                    parts.text("(", P(epos + 1), self.E(epos + 1))

                    def commented(a, b):
                        k2 = bisect.bisect_left(marks, a)
                        return k2 < len(marks) and marks[k2] < b

                    # Комментарий в скобках (перед первым словом, между словами, за последним) bash отбрасывает
                    # (read_token): текст по обе стороны от него — разные куски, а не один Lit поверх комментария.
                    prev = self.E(epos + 1)
                    for k, w in enumerate(words):
                        if commented(prev, w.value.start):
                            parts.mark()
                        if k:
                            parts.text(" ", w.value.start, w.value.start)
                        parts.extend(w.value.parts)
                        prev = w.value.end
                    if commented(prev, self.assign_close[0]):
                        parts.mark()
                    parts.text(")", *self.assign_close)
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
            parts.char(c, self.CP(self.cpos), end=self.CE(self.cpos))
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
                    lazy = self.child_lazy([(a + 1, b)])
                    sub, _ = yield self.arith_quote(self.s[a + 1:b], self.orig_pieces([(a + 1, b)]), a + 1, lazy)
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
        lo, hi = self.P(start), max(self.P(start), self.PE(end) - 1)
        key = ("reexpand_word", lo, text)
        done = self.shared.get(key)
        if done is None:
            # Предел работы повторного раскрытия (_REEXPAND_BASE, _REEXPAND_PER): цепочка вложенных подстановок, где
            # каждый уровень раскрывается заново по своему напечатанному тексту, перечитывает на каждом уровне весь
            # вложенный текст — время по квадрату длины и выше (как в bash: xparse_dolparen каждого уровня читает
            # вложенный текст заново). Сверх предела — ошибка вида DEPTH: дерево не строится. Считается длина
            # текста, а не прочитанное: печать с отметками позиций (printed, deep) проходит весь вложенный текст на
            # каждом уровне, и она — основная работа повторного раскрытия.
            left = self.shared.get(_REEXPAND_LEFT)
            if left is not None:
                left -= len(text)
                if left < 0:
                    raise _TooDeep()
                self.shared[_REEXPAND_LEFT] = left
            # Печать длиннее или короче исходного текста (`for a do` — `for a in "$@"; do`): позиции её частей
            # переводятся в исходный текст по узлам, чьи начала и концы известны (_printed_pieces), а не одним
            # сдвигом от начала слова; предел — последний символ слова.
            sub = self.printed_reader(tok, lo, hi, text, deep=True, tail=False, foreign=_seeds(anchors))
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
        tpl = None
        if close is not None and self.skip_to(close):
            # Конец группы уже нашёл parse_matched_pair внешней группы (вложенная `=~` в подстановке в группе).
            self.getc(qc != "'")
        else:
            saved, self.group_rec = self.group_rec, (gpos, qc)
            yield self.pmp(cd, "(", ")", 0, gpos)
            self.group_rec = saved
            tpl = self.tpl_out
        self._dpop()
        close_pos = self.cpos
        ranges = self.raw_ranges(wpos, close_pos + 1, k0, len(self.jumps))
        spans = [p for p in tpl or () if type(p) is _Tpl and p.span]
        if spans and all(p.loc is not None and any(a <= p.loc[0] and p.loc[1] <= b for a, b in ranges)
                         for p in spans):
            # Текст группы — прочитанный текст, `$'…'` в нём — значением (переписанные куски): позиции остальных
            # символов — по строке разбора, как у группы без `$'…'`.
            parts = yield self._group_scan(ranges, spans)
            return parts
        if spans:
            # bash хранит группу текстом parse_matched_pair: `$'…'` вне "…" и `` `…` `` — значением (подстановки
            # `$(…)` в группе он не разбирает, и `$'…'` в них переводится тоже), в '…' или, если разделитель — `"`
            # (группа в подстановке в "…"), без кавычек: `"$([[ a =~ ($(echo $';b'))]])"` исполняет `b`. Раскрытие
            # группы читает этот текст.
            items = ([self.s[wpos:gpos]] if wpos < gpos else []) + ["(", _Tpl(tpl)]
            anchors = []
            text = _render(items, anchors)
            lo = self.P(wpos)
            sub = self.printed_reader(items, lo, max(lo, self.P(close_pos)), text, tail=False, foreign=_seeds(anchors))
            sub.ansi = True
            parts = yield sub.scan_word()
            return parts
        text = "".join(self.s[a:b] for a, b in ranges)
        lazy = self.child_lazy(ranges)
        sub = _R(text, self.orig_pieces(ranges), self.comments, shared=self.shared, tail=False,
                 foreign=self.memo_in(ranges))
        sub.set_lazy(lazy)
        # `$'…'` в "…" группы: в `$((…)` и `${…}` в них parse_matched_pair переводит его (sh_single_quote) — конец
        # `$((…)` при раскрытии ищется по переведённому тексту (_xscan с ansi).
        sub.ansi = True
        if len(ranges) == 1:
            a = ranges[0][0]
            if self.group_pairs:
                sub.group_views.append((self.group_pairs, a))
            sub.group_views.extend((d, off + a) for d, off in self.group_views)
        parts = yield sub.scan_word()
        return parts

    def _group_scan(self, ranges, spans):
        """Части группы: куски ranges строки разбора, где `$'…'` (spans — куски с loc) заменены их значением."""
        cuts = sorted((p.loc[0], p.loc[1], p) for p in spans)
        segs = []
        for a, b in ranges:
            pos = a
            for la, lb, piece in cuts:
                if a <= la and lb <= b:
                    if la > pos:
                        segs.append((pos, la, None))
                    segs.append((la, lb, piece))
                    pos = lb
            if b > pos:
                segs.append((pos, b, None))
        texts, pieces, foreign, lazy = [], [], {}, []
        local = 0
        for a, b, piece in segs:
            if piece is not None:
                value = _render([piece])
                pieces.append((local, piece.at, piece.at, piece.to))
                texts.append(value)
                local += len(value)
                continue
            child = self.child_lazy([(a, b)]) or ()
            lazy.extend((x + local, y + local, o, o2, q) for x, y, o, o2, q in child)
            pieces.extend((p[0] + local,) + tuple(p[1:]) for p in self.orig_pieces([(a, b)]))
            for key, (node, end, tpl, ctx, clean) in self.memo_in([(a, b)]).items():
                foreign[key + local] = (node, end + local, tpl, ctx, clean)
            texts.append(self.s[a:b])
            local += b - a
        sub = _R("".join(texts), _merge_pieces(pieces), self.comments, shared=self.shared, tail=False,
                 foreign=foreign)
        sub.set_lazy(lazy or None)
        sub.ansi = True
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

    def _param(self, res, dpos, body):
        """Param `${…}` из результата pmp, позиции `$` и куска body — текста за `${` в кусках лексемы."""
        inner, close_pos = res
        return _node(Param, self.P(dpos), _close_end(self, close_pos), _src=body, parts=inner)

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
        текст), `{`, `(`, `[` → (части, позиция закрывающей; куски текста — tpl_out)."""
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
                if close is None or (scanning and (sq or dq)):
                    # Раскрытие слова: '…' и "…" без закрывающей кавычки — до конца строки
                    # (string_extract_single_quoted, string_extract_double_quoted), её содержимое раскрывается.
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
                        parts.char(ch, self.CP(cp), True, end=self.CE(cp))
                    else:
                        parts.char("\\", self.CP(cp - 1), True, end=self.CE(cp - 1))
                        parts.char(ch, self.CP(cp), True, end=self.CE(cp))
                else:
                    parts.char("\\", self.CP(cp - 1), True, end=self.CE(cp - 1))
                    parts.char(ch, self.CP(cp), True, end=self.CE(cp))
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
                                piece = _Tpl([_sq(node.value)])
                            else:
                                piece = _Tpl([node.value])
                            piece.at, piece.to = P(cp) - 1, _close_end(self, self.cpos)
                            piece.loc = (cp - 1, self.cpos + 1)
                            piece.span = True
                            raw.append(piece)
                        else:
                            piece = _Tpl(inner_raw)
                            if wasdol and ch == '"':
                                del raw[-2]
                                # Печать `$"…"` — "…": содержимое — с символа за `"`.
                                piece.at, piece.to = P(cp) + 1, _close_end(self, self.cpos)
                            raw.append(piece)
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
                            sub, failed = yield self.arith_quote(self.s[cp + 1:self.cpos],
                                                                 self.orig_pieces([(cp + 1, self.cpos)]), cp + 1,
                                                                 self.child_lazy([(cp + 1, self.cpos)]))
                            if type(raw[-1]) is _Tpl:
                                raw[-1].memo = self.quote_memo
                            if failed:
                                quotes.append((qidx, P(cp)))
                            else:
                                parts.text("'", P(cp), self.E(cp))
                                parts.extend(sub)
                                parts.text("'", P(self.cpos), self.E(self.cpos))
                        elif ch == "'" and (flags & _P_ARITH) and ("$" in node.value or "`" in node.value):
                            # $'…' в арифметике bash заменяет её значением в '…' (ansiexpand, sh_single_quote), а
                            # '…' арифметика раскрывает: подстановки в значении исполняются.
                            # Значения в исходном тексте нет: переписанный кусок — весь `$'…'`.
                            parts.part(node)
                            sub, failed = yield self.arith_quote(node.value, [(0, node.start, node.start, node.end)])
                            if failed:
                                quotes.append((qidx, node.start))
                            else:
                                parts.extend(x for x in sub if type(x) is not Lit)
                        else:
                            parts.part(node)
                        handled = True
                    elif (flags & (_P_ARRAYSUB | _P_DOLBRACE)) and wasdol and ch in "({[":
                        handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate,
                                                          arith_region=bool(sub_depth or substr))
                        if open_ == ch:
                            count -= 1
                    elif (flags & _P_ARITH) and wasdol and ch == "(":
                        handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate,
                                                          arith_region=bool(sub_depth or substr))
                        if open_ == ch:
                            count -= 1
                            if parens and parens[-1] == cp:
                                parens.pop()
                                pidx.pop()
                    elif (flags & _P_ARITH) and wasdol and ch == "{":
                        npeek = self.getc(True)
                        self.ungetc(npeek)
                        if npeek in _FUNSUB:
                            handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate,
                                                              arith_region=bool(sub_depth or substr))
                    elif (flags & (_P_ARRAYSUB | _P_DOLBRACE)) and gtlt and ch == "(":
                        executed = not (dq_ctx and not (flags & _P_UNQ) and (flags & _P_DOLBRACE)
                                        and dbstate not in (_DB_QUOTE, _DB_QUOTE2))
                        handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate,
                                                          arith_region=bool(sub_depth or substr), procsub=True,
                                                          executed=executed)
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
                    handled = yield self._dollar_word(ch, cp, open_, flags, rflags, parts, raw, dbstate=dbstate,
                                                      arith_region=bool(sub_depth or substr))
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
                    parts.char(ch, self.CP(cp), end=self.CE(cp))
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
        for idx, qstart in quotes[:1]:
            # Подстановка, не закрытая в '…' арифметики, при раскрытии продолжается за кавычкой: раскрывается
            # текст от кавычки (она — простой символ) до конца выражения, как его хранит bash; раскрывается только
            # текст от первой такой кавычки.
            anchors = []
            text = _render(raw[idx:], anchors)
            sub = self.printed_reader(raw[idx:], qstart, P(end_pos), text, tail=False, foreign=_seeds(anchors))
            inner = yield sub.scan_text(_P_HEREDOC)
            parts.extend(x for x in inner if type(x) is not Lit)
        if procs:
            for cp, idx in procs:
                anchors = []
                text = _render(raw[idx:], anchors)
                sub = self.printed_reader(raw[idx:], P(cp) + 1, P(end_pos), text, foreign=_seeds(anchors))
                body = yield sub.toplevel(")")
                # Конец — за `)`, которой разбор кончил тело (cpos), а не конец напечатанного текста.
                end = sub.E(sub.cpos) if body.error is None and sub.s[sub.cpos:sub.cpos + 1] == ")" else (
                    body.end + 1 if body.commands else self.E(cp))
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
        if (flags & _P_DOLBRACE) and arith_mark is not None:
            parts.wrap(arith_mark[0], arith_mark[1], P(end_pos))
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
                value = _hd_operator(self.s, cp + 1, self.bound) if flags & _P_HDBODY else None
                failed = None
                try:
                    res = yield self.pmp(None, "{", "}", _P_FIRSTCLOSE | _P_DOLBRACE | rflags | unq, cp)
                except _Fatal as exc:
                    if value is None:
                        raise
                    failed, res = exc, None
                if value is not None:
                    alt = yield self._heredoc_dolbrace(start, value, res[1] if res is not None else None)
                    if alt is not None:
                        if raw and raw[-1] == ch:
                            raw.pop()
                        raw.append(_Tpl(["{", self.s[cp + 1:self.cpos + 1]]))
                        parts.drop_last()
                        parts.extend(alt)
                        return True
                    if failed is not None:
                        raise failed
                body = _Tpl(self.tpl_out)
                pieces = ["{", body]
                node = self._param(res, start, body)
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
                    sub = self.printed_reader([self.s[start], ("sub", node)], node.start,
                                              max(node.start, node.end - 1), text, tail=False,
                                              foreign=_seeds(anchors, 1))
                    inner = yield sub.scan_text(0)
                    done = self.shared[key] = (node, inner)
                parts.extend(done[1])
                return True
        parts.part(node)
        return True

    def _heredoc_dolbrace(self, start, t, close_pos):
        """`${имя OP слово}` прямо в теле heredoc (OP из `%#/^,:`, `$` на позиции start, слово — с позиции t): bash
        извлекает слово extract_heredoc_dolbrace_string и переписывает его до раскрытия (_hd_value): `$'a⏎b c'` в теле
        `${| …; }` становится словами `a`, `b c` на двух строках. Текст не изменился и конец тот же, что у чтения
        (close_pos), — None. Иначе части `${…}` по переписанному тексту (раскрытие как в теле heredoc), чтение — за его
        `}`."""
        memo = self.sub_memo

        def known(k):
            hit = memo.get(k)
            return hit[1] - 1 if hit is not None else None

        scan = _hd_value(self.s, t, known, self.bound)
        value = None
        while True:
            try:
                k = scan.send(value)
            except StopIteration as stop:
                got = stop.value
                break
            value = yield self._sub_end(k)
        P = self.P
        if got is None or got == -1:
            raise _Fatal(P(start))
        close, text, marks = got
        if close == close_pos and text == self.s[t:close]:
            return None
        if not self._seek(close + 1):
            return None
        # Слово раскрывается само по себе: образец и замена — как слово вне кавычек (getpattern с Q_PATQUOTE,
        # замена без Q_HERE_DOCUMENT), смещение `${x:…}` — арифметикой (как текст в "…"). Ошибка в нём (тело
        # `${ …; }` без `}`) кончает раскрытие на ней; исполненное до неё (`<(…)`) остаётся.
        # Значение `$'…'` — переписанный кусок (Script.rewritten): узел в нём начинается в его начале и кончается в
        # его конце.
        pieces, caps, _ = _printed_pieces([(m[0], P(m[1])) + ((("span", self.E(m[2][1] - 1)),) if len(m) > 2 else ())
                                           for m in marks], P(t), P(close))
        sub = _R(text, pieces, self.comments, shared=self.shared, tail=False, limit=P(close))
        sub.caps = caps
        if self.s[t - 1] == ":":
            parts = yield sub.scan_text(_P_HEREDOC)
        else:
            parts = yield sub.scan_word()
        return [_node(Param, P(start), _close_end(self, close), _src=self.s, _a=start, _b=close + 1, parts=parts)]

    def arith_quote(self, text, pieces, src=None, lazy=None):
        """Текст '…' в арифметике (или значение $'…'): bash раскрывает его, как текст в "…" (Q_ARITH): подстановки
        в нём исполняются; разбор — отдельной строкой при раскрытии. (части, не закрыта ли в нём подстановка)."""
        self.quote_memo = None
        if not text:
            return [], False
        if "$" not in text and "`" not in text:
            end = pieces[-1][1] + len(text) - pieces[-1][0]
            return ([_node(Lit, pieces[0][1], end, text=text)] if text else []), False
        foreign = self.memo_in([(src, src + len(text))]) if src is not None else None
        sub = _R(text, pieces, self.comments, shared=self.shared, tail=False, foreign=foreign)
        sub.set_lazy(lazy)
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
            span = self._span_of(orig)
            # Символ в переписанном куске этой строки — в нём и в теле: узел, начатый или кончающийся в нём, —
            # в его начале и конце (Script.rewritten), а не на одном символе.
            pieces.append((local, span[0], span[0], span[1]) if span else (local, self.P(orig)))
            local += 1
            i += 1
        body_text = "".join(out)
        # Куски по одному символу сливаются в непрерывные.
        merged = []
        for piece in pieces:
            last = merged[-1] if merged else None
            if last is not None and len(piece) > 2:
                if len(last) > 2 and last[2:] == piece[2:]:
                    continue
            elif last is not None and len(last) == 2 and piece[1] - last[1] == piece[0] - last[0]:
                continue
            merged.append(piece)
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
        if self.lazy:
            # Разбор внутри подстановки, переведённой сдвигом: её позиции — точно, до поиска готовых разборов
            # (ключ — исходная позиция).
            self.refine(dpos)
            self.refine(dpos + 1)
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
                end = _same_text(self.s, dpos, alt_text, self.bound)
                if end is not None and self.skip_to(end):
                    self._memo(dpos, alt_node, alt_tpl, ctx)
                    self.tpl_out = alt_tpl
                    return alt_node
            # Подстановку разбирать заново (иная обстановка): готовыми остаются внешние подстановки в её тексте —
            # если он здесь в печатном виде (позиции записей — позиции печати).
            anchors = []
            printed = _render(list(origin), anchors)
            if self.s.startswith(printed, dpos + 1, len(self.s) if self.bound is None else self.bound):
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
                    if self.scanning and kind_char == "$":
                        node = yield self._commented_dparen(dpos, ppos)
                        if node is not None:
                            self.tpl_out = self._memo(dpos, node, ["(", self.s[ppos:self.xmemo[dpos][0]], ")"], ctx,
                                                      origin)
                            return node
                    failed = None
                    try:
                        inner, close_pos = yield self.pmp(qc, "(", ")", _P_ARITH, ppos - 1)
                        content = _Tpl(self.tpl_out[:-1])
                    except _Fatal as exc:
                        if not (self.scanning and kind_char == "$"):
                            raise
                        failed, close_pos = exc, None
                    if self.scanning and kind_char == "$":
                        # Раскрытие строки: конец `$((…)` находит extract_delimited_string, а не parse_matched_pair.
                        node = yield self._expanded_dparen(dpos, ppos, close_pos)
                        if node is not None:
                            self.tpl_out = self._memo(dpos, node, ["(", self.s[ppos:self.cpos], ")"], ctx, origin)
                            return node
                        if failed is not None:
                            raise failed
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
        rules = self.shared.get(_EOF_RULE, 0)
        t = yield self.yylex()
        body, t = yield self.compound_list(t, True)
        if t.kind != close:
            raise _Fatal(t.start)
        close_at = self.cpos
        newline = self.last == "\n"
        if self.pending:
            yield self.gather()
        self._restore(saved)
        split, self.esc_split = self.esc_split, split
        if open_ == "(":
            kind = kind_char + "("
        else:
            kind = "${|" if spec == "|" else "${ "
        script = self._script(body, None, P(dpos), t.end, ncomments)
        if saved[-1] and self.shared.get(_EOF_RULE, 0) != rules and open_ in "({" and not self.segs:
            # Раскрытие строки (тело heredoc): конец подстановки находит xparse_dolparen — разбор со знаком конца,
            # где строка тела heredoc, начатая терминатором, со знаком конца в остатке кончает тело. Исполняет bash
            # исходный текст до этого конца (command_substitute → parse_and_execute) без знака конца: там та же строка
            # — строка тела (`$(cat <<E2` ⏎ `E2'$(touch P)'` ⏎ `E2` ⏎ `)` исполняет `touch P`).
            a = dpos + (3 if open_ == "{" and spec == "|" else 2)
            if a <= close_at <= self.n0:
                lazy = self.child_lazy([(a, close_at)])
                sub = _R(self.s[a:close_at], self.orig_pieces([(a, close_at)]), self.comments, shared=self.shared,
                         limit=P(close_at))
                sub.set_lazy(lazy)
                again = yield sub.toplevel()
                again.start, again.end = script.start, script.end
                script = again
        rest = None
        if not saved[-1] and _print_drops(script):
            # bash исполняет тело по его печати (print_comsub), а печать опускает `;` за телами heredoc: тело — разбор
            # напечатанного текста. При раскрытии строки (saved[-1] — scanning: тело heredoc) тело — исходный текст.
            items = [("enter",)] + _script_items(script) + [("flush",), ("leave",)]
            anchors = []
            text = _render(items, anchors)
            start = P(dpos) + (3 if open_ == "{" and spec == "|" else 2)
            sub = self.printed_reader(items, start, max(start, t.start), text, foreign=_seeds(anchors))
            # Конец подстановки при раскрытии находит разбор со знаком конца (xparse_dolparen): в печати он бывает
            # раньше её конца (`a <<E; b; case c in d) e;; esac` печатается `b case c in d)` ⏎ …). Остаток печати —
            # текст слова: его раскрытия исполняются (_rest).
            again = yield sub.toplevel(close)
            if again.error is None and sub.cpos < len(text) and text[sub.cpos] == close:
                tail = sub.view(sub.cpos + 1)
                rest = [x for x in (yield tail.scan_word()) if type(x) is not Lit] or None
            if again.commands or again.error is None or not again.error.fatal:
                again.start, again.end, again.comments = script.start, script.end, script.comments
                script = again
            else:
                rest = None
        node = _node(Sub, P(dpos), t.end, kind=kind, body=script, _nl=newline, _rest=rest)
        if split:
            self.shared[("reexpand", id(node))] = node
        self.tpl_out = self._memo(dpos, node, [("sub", node)], ctx, origin)
        return node

    def _shared_get(self, dpos, ctx):
        """Подстановка с позиции dpos из self.shared: (узел, куски) или None."""
        if not self.shared:
            return None
        base = self._placed(dpos, dpos + 1)
        if base is None:
            return None
        for text, node, tpl, last in self.shared.get((base[0], ctx), ()):
            end = _same_text(self.s, dpos, text, self.bound)
            if end is not None and self._placed(dpos, end) == (base[0], last) and self.skip_to(end):
                return node, tpl
        return None

    def _text_ref(self, a, b, placed=None):
        """Текст [a, b) ссылкой (строка, начало, конец) для _same_text: в исходном тексте, если он там тот же (placed —
        его место, _placed), в строке разбора, если она — исходный текст, иначе срезом. Записи подстановок каждого
        уровня повторного разбора срезами хранили бы текст всех более глубоких уровней — память по квадрату
        глубины; ссылка на строку повторного разбора держала бы её всю."""
        src = self.shared.get(_SOURCE)
        if src is self.s:
            return (src, a, b)
        if placed is None:
            placed = self._placed(a, b)
        if src is not None and placed is not None:
            start, end = placed
            if end - start == b - a and self.s[a:b] == src[start:end]:
                return (src, start, end)
        return (self.s[a:b], 0, b - a)

    def _placed(self, a, b):
        """(исходное начало, исходный конец) строки [a, b), если она лежит в исходном тексте (не в переписанном куске,
        до конца строки, в пределе limit), иначе None: разбор того же места с тем же текстом — тот же узел."""
        if b > self.n0 or b <= a:
            return None
        if self.spans is not None and self.pieces is not None:
            for i in (a, b - 1):
                if max(0, bisect.bisect_right(self.piece_starts, i) - 1) in self.spans:
                    return None
        start, last = self.P(a), self.E(b - 1)
        if self.limit is not None and last > self.limit + 1:
            return None
        return start, last

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
            base = self._placed(dpos, self.pos)
            if base is not None:
                self.shared.setdefault((base[0], ctx), []).append((self._text_ref(dpos, self.pos, base), node, tpl,
                                                                   base[1]))
        if origin is not None and clean:
            # Тот же текст, разобранный в иной обстановке, чем при чтении (origin): следующий разбор в этой
            # обстановке возьмёт результат готовым.
            if origin.alts is None:
                origin.alts = {}
            origin.alts.setdefault(ctx, []).append((node, self._text_ref(dpos, self.pos), tpl))
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
        node = yield self._dparen_node(dpos, ppos, close_pos, inner, content, read, anchors)
        if not self.scanning and not self.reexpand:
            # При раскрытии слова конец `$((…)` находит extract_delimited_string по тексту слова (комментарий `#`,
            # `)` в `${…}`): если он не там, где его нашло чтение, слово раскрывается заново по напечатанному тексту
            # (_reexpand_word), где этот конец ищет _expanded_dparen.
            # None — конец вложенной подстановки, которой нет среди готовых (`$(…)` в `${ …}` внутри "…"), без разбора
            # неизвестен: расхождения нет, слово раскрывается как прочитано (как до проверки).
            known = {a - 1: b - 1 for a, b, _ in anchors if a > 0}
            end = _xend(read + ")", known.get)
            if end is not None and end != len(read):
                self.shared[("reexpand", id(node))] = node
        return node

    def _dparen_node(self, dpos, ppos, close_pos, inner, content, read, anchors):
        """Узел `$((…)…)` по тексту read (_arith_or_comsub)."""
        P = self.P
        if read[:1] == "(" and read[-1:] == ")":
            skel = _skel([content])
            arith = skel[:1] == "(" and skel[-1:] == ")" and _chk_arithsub(skel[1:-1])
        else:
            arith = False
        if arith:
            return _node(Arith, P(dpos), _close_end(self, close_pos), parts=_strip_parens(inner))
        # bash разбирает при раскрытии этот текст; позиции его узлов — по отметкам печати, в пределах подстановки.
        sub = self.printed_reader([content], P(ppos), P(close_pos), read, foreign=_seeds(anchors))
        script = yield sub.toplevel()
        if script.error is not None and script.error.fatal and not script.commands:
            # bash разбирает при раскрытии напечатанный текст (print_comsub), а не исходный; разбор исходного не
            # сошёлся — подстановки, разобранные при чтении, остаются (ложный отказ дешевле пропуска).
            return _node(Arith, P(dpos), _close_end(self, close_pos), parts=_strip_parens(inner))
        return _node(Sub, P(dpos), _close_end(self, close_pos), kind="$(", body=script)

    def _expanded_dparen(self, dpos, ppos, close_pos):
        """`$((…)` при раскрытии строки (тело heredoc, слово, раскрытое заново): конец — по extract_delimited_string
        (_xscan) с позиции ppos за `$(`; конец вложенных подстановок — из разобранных этой строкой (sub_memo) или
        разбором (_sub_end). Совпал с концом чтения close_pos — None (узел строит обычный путь). Иначе текст до этого
        конца — арифметика, если он `(…)` со сбалансированными скобками (chk_arithsub), или подстановка команды, чьё
        тело bash разбирает целиком; чтение продолжается за ним. Конца нет — ошибка раскрытия (bad substitution)."""
        hit = yield self._dparen_end(dpos, ppos, True)
        end = hit[0]
        if end == close_pos:
            return None
        if end is None or end < 0:
            raise _Fatal(self.P(dpos))
        node = yield self._dparen_text(dpos, ppos, end)
        return node

    def _commented_dparen(self, dpos, ppos):
        """`$((…)` при раскрытии строки, чей конец (_xscan) за комментарием: bash находит его extract_delimited_string,
        не читая конструкцию parse_matched_pair, и текст за `#` до перевода строки — вложенные `$((` в нём — не
        разбирает. Узел — по тексту до этого конца (_dparen_text) без чтения parse_matched_pair: чтение разбирало бы
        каждую вложенную `$((` из комментария, и её конец — снова за тем же комментарием (`$(( #$(( #…` — время по
        квадрату глубины). None — конец неизвестен без разбора вложенной подстановки или комментария нет."""
        end, commented = yield self._dparen_end(dpos, ppos, False)
        if not commented or end is None or end < 0:
            return None
        node = yield self._dparen_text(dpos, ppos, end)
        return node

    def _dparen_end(self, dpos, ppos, parse):
        """(конец `$((…)` с позиции dpos при раскрытии (_xscan), был ли в ней комментарий); концы вложенных `$((`
        запоминаются (xmemo). parse=False — проход до первой неизвестной вложенной подстановки, без её разбора: она
        есть — (None, False); проход по вложенным уровням на каждом уровне был бы квадратичным, их концы найдёт
        разбор этой `$((` (sub_memo)."""
        hit = self.xmemo.get(dpos)
        if hit is not None:
            return hit
        memo = self.sub_memo

        def known(k):
            hit = memo.get(k)
            return hit[1] - 1 if hit is not None else None

        scan = _xscan(self.s, ppos, known, "D", self.bound, self.ansi, self.xmemo, dpos, not parse)
        value = None
        while True:
            try:
                k = scan.send(value)
            except StopIteration as stop:
                end = stop.value
                break
            if not parse:
                return None, False
            value = yield self._sub_end(k)
        hit = self.xmemo.get(dpos)
        return hit if hit is not None and hit[0] == end else (end, False)

    def _dparen_text(self, dpos, ppos, end):
        """Узел `$((…)` по тексту до конца end, найденного при раскрытии; чтение продолжается за ним. None — переход
        не сделан (_seek)."""
        P = self.P
        if not self._seek(end + 1):
            return None
        temp = self.s[ppos:end]
        if temp[:1] == "(" and temp[-1:] == ")" and _chk_arithsub(temp[1:-1]):
            lazy = self.child_lazy([(ppos + 1, end - 1)])
            sub = _R(temp[1:-1], self.orig_pieces([(ppos + 1, end - 1)]), self.comments, shared=self.shared,
                     tail=False, limit=P(end))
            sub.ansi = self.ansi
            sub.set_lazy(lazy)
            parts = yield sub.scan_text(_P_HEREDOC)
            return _node(Arith, P(dpos), _close_end(self, end), parts=parts)
        lazy = self.child_lazy([(ppos, end)])
        sub = _R(temp, self.orig_pieces([(ppos, end)]), self.comments, shared=self.shared, limit=P(end))
        sub.set_lazy(lazy)
        script = yield sub.toplevel()
        return _node(Sub, P(dpos), _close_end(self, end), kind="$(", body=script)

    def _sub_end(self, k):
        """Индекс символа, закрывающего подстановку с позиции k (`$(`, `${ `, `${|`, `<(`, `>(`), по разбору её тела
        (xparse_dolparen); -1 — тело не разобрано."""
        s = self.s
        top = len(s) if self.bound is None else self.bound
        close = "}" if s[k + 1] == "{" else ")"
        start = k + 2 + (1 if close == "}" and s[k + 2:min(k + 3, top)] == "|" else 0)
        sub = self.view(start)
        script = yield sub.toplevel(close)
        if script.error is not None or s[sub.cpos:min(sub.cpos + 1, top)] != close:
            return -1
        return sub.cpos

    def _seek(self, e):
        """Чтение строки — с позиции e (конец `$((…)` при раскрытии дальше или ближе конца чтения). False — ввод
        читает отложенные куски, переход не сделан."""
        if self.segs:
            return False
        if e >= self.n:
            self.pos, self.lim, self.frontier = self.n, self.n - 1, self.n
        else:
            # Строки за этой — снова непрочитанные: раскрытие читает строку подряд, а чтение конца, найденного
            # parse_matched_pair, могло уйти дальше.
            le = self.s.find("\n", e, self.n)
            le = self.n - 1 if le < 0 else le
            self.pos, self.lim, self.frontier = e, le, le + 1
        self.last_i = e - 1
        return True

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
        before = list(self.pending)
        words = []
        while True:
            t = yield self.read_token()
            if t.kind == ")":
                # Позиции `)`: за ней чтение уже могло заглянуть дальше (`\\` с переводом строки, конец ввода), и
                # cpos — не она.
                self.assign_close = (t.start, t.end)
                break
            if t.kind == "\n":
                continue
            if t.kind not in ("WORD", "ASSIGN"):
                raise _Discard(t.start)
            words.append(t)
        self._restore(saved)
        # restore_parser_state возвращает need_here_doc и redir_stack: heredoc, чьё тело прочитал перевод строки в
        # скобках, ждёт тела снова, со следующего перевода строки. Его слово make_here_document уже заменило телом
        # (redirectee.filename->word = document): терминатор — прочитанное тело, у слова в кавычках — после снятия
        # кавычек; тело с переводом строки не совпадает ни с одной строкой (тело — до конца ввода), пустое — кончает
        # пустая строка. Проверено прогоном bash 5.3 с `touch`.
        left = {id(doc) for doc in self.pending}
        for doc in before:
            if id(doc) not in left:
                doc.term = _quote_removal(doc.body_text) if doc.quoted else doc.body_text
                self.regathered += 1
        self.pending = before
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

    def and_or(self, t, top=False):
        """list1 без `;`, `&`, перевода строки: конвейеры через `&&`, `||` (перевод строки за ними пропускается).
        top — список верхнего уровня (simple_list): фатальная ошибка в команде отмечает её начало (_Fatal.open)."""
        items, ops = [], []
        while True:
            node, t = yield self.pipeline_command(t, top)
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

    def pipeline_command(self, t, top=False):
        """pipeline_command: `!` и `time` перед конвейером (или пустой командой, nullcmd_terminator). top — как у
        and_or."""
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
        ops = []
        while True:
            if top:
                try:
                    node, t = yield self.command(t)
                except (_Fatal, _Discard) as exc:
                    if exc.open is None:
                        exc.open = t.start
                    raise
            else:
                node, t = yield self.command(t)
            cmds.append(node)
            if t.kind in ("|", "|&"):
                ops.append(t.kind)
                t = yield self.yylex()
                while t.kind == "\n":
                    t = yield self.yylex()
                continue
            break
        if len(cmds) == 1 and not bang and not timed:
            return cmds[0], t
        return _node(Pipeline, start, cmds[-1].end, commands=cmds, bang=bang, time=timed, _ops=ops), t

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
            # W_QUOTED read_token_word ставит за '…', "…", $'…', $"…" и `\\` на верхнем уровне слова; кавычки и `\\`
            # внутри `${…}`, `$(…)`, `$((…))`, `$[…]`, `` `…` `` его не ставят (`<<${x:-"a"}` кончает строка
            # `${x:-"a"}`, тело раскрывается, проверено bash 5.3). В word._tok эти конструкции — вложенные _Tpl,
            # верхний уровень — строки. Терминатор сравнивается с текстом, где подстановки напечатаны
            # (`<<$(b;c)` кончает строка `$(b; c)`, проверено bash 5.3).
            # `$'…'`, `$"…"` — куски с отметкой at.
            quoted = any((type(p) is str and ("'" in p or '"' in p or "\\" in p))
                         or (type(p) is _Tpl and p.at is not None) for p in word._tok)
            text = _term_text(word._tok)
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
                    error = SyntaxIssue(exc.pos, True, open=exc.open)
                break
            except _Discard as exc:
                if error is None:
                    error = SyntaxIssue(exc.pos, False, open=exc.open)
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
            node, t = yield self.and_or(t, True)
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
    """Кусок текста лексемы (вложенная конструкция): элементы — строки, ("sub", Sub), _Tpl; flat, text, skel —
    готовый текст (_flat, _render, _skel), чтобы вложенные куски не собирались заново на каждом уровне: ссылка на
    общую строку (_cached, _share)."""
    __slots__ = ("flat", "text", "skel", "balanced", "seed", "alts", "memo", "at", "to", "span", "loc")

    def __init__(self, items=()):
        super().__init__(items)
        # Начало и конец куска в исходном тексте, если печать меняет его длину (`$'…'` — значением в '…', `$"…"` —
        # "…"): отметки перевода позиций печати (_render с where).
        self.at = None
        self.to = None
        # Печать куска не идёт за исходным текстом посимвольно (`$'…'` — значением): переписанный кусок [at, to).
        self.span = False
        # Переписанный кусок в строке разбора, которая его прочла: (начало, конец) — `$'…'` группы (group_word).
        self.loc = None
        self.flat = None
        self.text = None
        self.skel = None
        self.balanced = False
        # Подстановка, разобранная parse_comsub (её текст — этот кусок): (узел, обстановка разбора). Повторный
        # разбор напечатанного текста берёт узел готовым (_render с anchors, _R.foreign).
        self.seed = None
        # Разборы того же текста в иной обстановке: обстановка → [(узел, прочитанный текст — (строка, начало, конец),
        # куски текста)].
        self.alts = None
        # Текст '…' в арифметике: записи sub_memo разбора, раскрывшего его (arith_quote), в позициях текста.
        self.memo = None


class _Text:
    """Общая строка готовых текстов кусков (_share): s — строка. Строка, вошедшая целиком в другую, переведена в неё:
    s — None, текст — в up со сдвигом d (путь сжимается при чтении, _text_root). Снятая строка — s и up None: ссылки
    на неё без текста. used — сколько знаков строки вошло готовыми кусками в другие строки."""
    __slots__ = ("s", "up", "d", "used")

    def __init__(self, s):
        self.s = s
        self.up = None
        self.d = 0
        self.used = 0


def _text_root(holder):
    """Строка _Text, в которой лежит текст holder (s None — снята), и сдвиг в ней; путь до неё сжимается."""
    path = []
    d = 0
    while holder.s is None and holder.up is not None:
        path.append(holder)
        d += holder.d
        holder = holder.up
    acc = d
    for node in path:
        own = node.d
        node.up, node.d = holder, acc
        acc -= own
    return holder, d


def _cached(ref):
    """Готовый текст по ссылке кеша (строка или (_Text, начало, конец)); None — его нет или строка снята."""
    if ref is None or type(ref) is str:
        return ref
    holder, a, b = ref
    root, d = _text_root(holder)
    if root.s is None:
        return None
    return root.s[a + d:b + d]


def _share(text, spans, uses, slot):
    """Запомнить тексты собранных кусков среза text без копии на каждый уровень вложенности.

    spans — собранные куски (объект, начало, конец) — вложены правильно; каждому — в поле slot ссылка на строку
    своего внешнего куска (копия его среза: одна на внешний кусок). uses — готовые куски (ссылка, позиция, длина,
    объект), вошедшие в text: строка, вошедшая во внешний кусок целиком, переводится в его строку и освобождается;
    строка, чьи куски вошли в другие строки на половину длины и больше, снимается (её куски соберутся заново по
    требованию). Так каждый знак живёт в немногих строках: память линейна по длине текста, а не квадратична по глубине
    вложенности. Строки на каждый уровень (как было) повторяли бы текст вложенных уровней. Объект куска, вошедшего
    частью строки, ссылается дальше на его копию в text: снятие строки не теряет его текст, и следующая лексема с тем же
    куском (тот же текст, разобранный ещё раз на каждом уровне) не собирает его заново со всеми вложенными."""
    if not spans:
        return
    spans.sort(key=lambda e: (e[1], -e[2]))
    tops = []
    for obj, a, b in spans:
        if not tops or a >= tops[-1][1]:
            tops.append((a, b, _Text(text if a == 0 and b == len(text) else text[a:b])))
        base, _, holder = tops[-1]
        setattr(obj, slot, (holder, a - base, b - base))
    starts = [t[0] for t in tops]
    for ref, x, n, obj in uses:
        if type(ref) is str:
            continue
        k = bisect.bisect_right(starts, x) - 1
        if k < 0 or x + n > tops[k][1]:
            continue
        base, _, holder = tops[k]
        root, d = _text_root(ref[0])
        if root.s is None or root is holder:
            continue
        if ref[1] + d == 0 and ref[2] + d == len(root.s):
            root.s, root.up, root.d = None, holder, x - base
        else:
            root.used += n
            setattr(obj, slot, (holder, x - base, x - base + n))
            if 2 * root.used >= len(root.s):
                root.s = None


def _tpl_text(tpl):
    """Текст куска tpl, как у _flat (готовый — срез общей строки)."""
    text = _cached(tpl.flat)
    return _flat([tpl]) if text is None else text


def _flat(pieces):
    """Текст лексемы для проверок вида (зарезервированное слово, присваивание): подстановка — заглушкой.

    Текст каждого собранного куска _Tpl запоминается (flat, _share): повторные проверки той же лексемы (каждый `=`
    слова) и лексемы внешних уровней, где кусок вложен (`$((x $((x …) ) )`), его не собирают заново."""
    out = []
    size = 0
    spans = []
    uses = []
    done = {}  # кусок, собранный в этом вызове, -> его элементы out (повтор того же куска)
    stack = [(pieces, 0, 0, 0)]
    while stack:
        seq, i, start, first = stack.pop()
        while i < len(seq):
            p = seq[i]
            i += 1
            if type(p) is str:
                out.append(p)
                size += len(p)
            elif type(p) is _Tpl:
                text = _cached(p.flat)
                if text is not None:
                    uses.append((p.flat, size, len(text), p))
                elif id(p) in done:
                    a, b = done[id(p)]
                    text = "".join(out[a:b])
                else:
                    stack.append((seq, i, start, first))
                    stack.append((p, 0, size, len(out)))
                    break
                out.append(text)
                size += len(text)
            else:
                out.append("$( )")
                size += 4
        else:
            if stack:
                done[id(seq)] = (first, len(out))
                spans.append((seq, start, size))
    text = "".join(out)
    _share(text, spans, uses, "flat")
    return text


class _Unprinted(Exception):
    """Терминатор heredoc с подстановкой, чью печать print_comsub разбор не повторяет точно: строку, которая кончает
    тело, узнать нельзя. parse отдаёт ошибку вида INTERNAL — сомнение у вызывающих, а не тело до конца ввода."""


# Перенаправления, которые печать терминатора повторяет: оператор → номер по умолчанию (print_redirection не
# печатает его). Проверено печатью терминатора bash 5.3.
_PRINT_REDIRS = {"<": 0, "<>": 0, "<<<": 0, ">": 1, ">>": 1, ">|": 1, "&>": None, "&>>": None}
# Составные команды, которые print_cmd.c печатает на нескольких строках (`if a; then⏎    b;⏎fi`, `function f ()⏎{`):
# в печати терминатора есть перевод строки, и тело кончает только конец ввода (make_here_document сравнивает
# терминатор с одной строкой) — точный текст печати не нужен.
_MULTILINE = (If, While, Until, For, Select, Case, Function)


def _term_text(tok):
    """Текст слова терминатора heredoc, как его хранит bash (read_token_word): подстановки `$(…)`, `${ …; }`,
    `${| …; }`, `<(…)`, `>(…)` — печатью print_comsub. Печать повторяется для тел из простых команд, `( … )`, `{ …; }`,
    соединённых `;`, `&`, `&&`, `||`, `|`, `|&` (печать — `2>&1 |`) и переводом строки, с `!` и `time`, с
    перенаправлениями из _PRINT_REDIRS и `<&`, `>&` (`$(b;c)` — `$(b; c)`, `$(<b)` — `$(< b)`, `$(b⏎⏎c)` — `$(b⏎c)`,
    `$((a)|&b)` — `$( ( a ) 2>&1 | b)`). Команда, которую bash печатает на нескольких строках (_MULTILINE, heredoc в
    теле), — перевод строки: такой терминатор не совпадает ни с одной строкой. Иное тело (`[[ … ]]`, `((…))`,
    `coproc`, комментарий) — _Unprinted. Без рекурсии: стек элементов."""
    out = []
    stack = [tok]
    while stack:
        item = stack.pop()
        kind = type(item)
        if kind is str:
            out.append(item)
        elif kind is _Tpl or kind is list:
            stack.extend(reversed(item))
        elif kind is tuple and item[0] == "sub" and type(item[1]) is Sub:
            stack.extend(reversed(_sub_print(item[1])))
        elif kind is Word and item._tok is not None:
            stack.append(item._tok)
        elif kind is Word:
            stack.append(item.literal() or "")
        elif kind is Simple:
            stack.extend(reversed(_simple_print(item)))
        elif kind is Sequence:
            parts = []
            for k, node in enumerate(item.items):
                parts.append(node)
                sep = item.seps[k] if k < len(item.seps) else None
                last = k == len(item.items) - 1
                if sep == "&":
                    parts.append(" &" if last else " & ")
                elif sep == ";" and not last:
                    parts.append("; ")
                elif sep == "\n" and not last:
                    parts.append("\n")
            stack.extend(reversed(parts))
        elif kind is AndOr:
            parts = [item.items[0]]
            for op, node in zip(item.ops, item.items[1:]):
                parts += [" " + op + " ", node]
            stack.extend(reversed(parts))
        elif kind is Pipeline:
            # print_cmd: `time -p` перед `!`; `|&` parse.y заменяет перенаправлением `2>&1` в конце левой команды.
            parts = [(item.time + " " if item.time else "") + ("! " if item.bang else "")]
            if not item.commands:
                raise _Unprinted()
            for k, node in enumerate(item.commands):
                if k:
                    parts.append(" | ")
                parts.append(node)
                if k < len(item._ops or ()) and item._ops[k] == "|&":
                    parts.append(" 2>&1")
            stack.extend(reversed(parts))
        elif kind is Subshell:
            stack.extend(reversed(["( ", item.body, " )"] + _redirs_print(item.redirs)))
        elif kind is Group:
            end = " }" if _background(item.body) else "; }"
            stack.extend(reversed(["{ ", item.body, end] + _redirs_print(item.redirs)))
        elif kind in _MULTILINE:
            out.append("\n")
        else:
            raise _Unprinted()
    return "".join(out)


def _background(node):
    """Список кончается `&` (печать `{ a & }`, `${ a & }`)."""
    return type(node) is Sequence and len(node.seps) == len(node.items) and node.seps[-1] == "&"


def _first_command(node):
    """Первая команда списка (сквозь `;`, `&&`, `|` без `!` и `time`): с неё начинается печать."""
    while True:
        kind = type(node)
        if kind in (Sequence, AndOr):
            node = node.items[0]
        elif kind is Pipeline and not node.bang and not node.time and node.commands:
            node = node.commands[0]
        else:
            return node


def _sub_print(node):
    """Элементы печати подстановки без её первого символа (`$`, `<`, `>` — в тексте слова) для _term_text."""
    body = node.body
    if not body.commands or body.error is not None or body.dropped or body.comments:
        raise _Unprinted()
    # Команды тела, разделённые переводами строк, print_comsub печатает через один `\n`.
    lines = [body.commands[0]]
    for line in body.commands[1:]:
        lines += ["\n", line]
    if node.kind in ("$(", "<(", ">("):
        # parse_comsub: печать, начатая `(`, — через пробел (`$( ( a ))`), иначе `$((` читалась бы арифметикой.
        space = " " if type(_first_command(body.commands[0])) in (Subshell, ArithCmd) else ""
        return ["(" + space, lines, ")"]
    # Конец `${ …}` по parse_comsub: перевод строки перед `}` — `\n }`, иначе ` }` за `&` и `; }` за прочим.
    end = "\n }" if node._nl else " }" if _background(body.commands[-1]) else "; }"
    return ["{ " if node.kind == "${ " else "{|", lines, end]


def _simple_print(node):
    """Элементы печати простой команды: слова через пробел, перенаправления за ними (print_simple_command)."""
    parts = []
    for word in (node.assigns or []) + (node.words or []):
        parts += [word, " "]
    redirs = _redirs_print(node.redirs)
    if redirs:
        parts += redirs[1:] + [" "]
    if not parts:
        raise _Unprinted()
    return parts[:-1]


def _redirs_print(redirs):
    """Элементы печати перенаправлений: каждое — за пробелом (print_redirection_list). Heredoc печатается с телом на
    следующих строках — перевод строки."""
    parts = []
    for redir in redirs or ():
        if type(redir.target) is Heredoc:
            parts += [" ", "\n"]
            continue
        if type(redir.target) is not Word:
            raise _Unprinted()
        varassign = redir.fd is not None and not redir.fd.isdigit()
        if varassign and not (redir.fd[:1] == "{" and redir.fd[-1:] == "}"):
            raise _Unprinted()
        if redir.op in ("<&", ">&"):
            parts += [" "] + _dup_print(redir, varassign)
            continue
        if redir.op not in _PRINT_REDIRS:
            raise _Unprinted()
        fd = ""
        if varassign:
            fd = redir.fd
        elif redir.fd is not None and int(redir.fd) != _PRINT_REDIRS[redir.op]:
            fd = str(int(redir.fd))
        parts += [" ", fd + redir.op + " ", redir.target]
    return parts


def _dup_print(redir, varassign):
    """print_redirection для `<&`, `>&` (виды make_redirection): `2>&1` — номер источника печатается всегда
    (`<&3` — `0<&3`), `<&-` — `0>&-`, `>&3-` — `1>&3-`, слово — `>&x` без номера по умолчанию (`1>&x` — `>&x`), с
    другим — `2>&x`. Проверено печатью declare -f bash 5.3. Элементы для _term_text."""
    out = redir.op == ">&"
    default = 1 if out else 0
    src = redir.fd if varassign else str(default if redir.fd is None else int(redir.fd))
    word = redir.target
    if word._tok is None:
        text = word.literal() or ""
        if text == "-":
            return [src + ">&-"]
        return [src + redir.op + str(int(text))]
    # make_redirection смотрит на текст лексемы (с кавычками): `>&3-` — перенос дескриптора 3, `>&'3'-` — слово.
    text = _flat(word._tok)
    if text.endswith("-"):
        base = text[:-1]
        if base and all(c in _DIGITS for c in base) and int(base) < 2 ** 31:
            return [src + redir.op + str(int(base)) + "-"]
    if text.endswith("-") or varassign or int(src) != default:
        return [src + redir.op, word]
    return [redir.op, word]


def _skel(pieces):
    """Текст для chk_arithsub: вложенная арифметика, уже признанная сбалансированной, — `()` (её скобки и кавычки
    не меняют счёт), остальное — как печатает bash; без этого счёт на каждом уровне вложенности перечитывал бы все
    более глубокие. Тексты собранных кусков запоминаются (skel, _share)."""
    out = []
    size = 0
    spans = []
    uses = []
    done = {}
    stack = [(pieces, 0, 0, 0)]
    while stack:
        seq, i, start, first = stack.pop()
        while i < len(seq):
            p = seq[i]
            i += 1
            if type(p) is str:
                text = p
            elif type(p) is _Tpl:
                if p.balanced:
                    text = "()"
                else:
                    text = _cached(p.skel)
                    if text is None and p.seed is not None and type(p.seed[0]) is Sub:
                        text = _sub_skel(p)
                    if text is not None:
                        uses.append((p.skel, size, len(text), p))
                    elif id(p) in done:
                        a, b = done[id(p)]
                        text = "".join(out[a:b])
                    else:
                        stack.append((seq, i, start, first))
                        stack.append((p, 0, size, len(out)))
                        break
            else:
                text = _render([p])
            out.append(text)
            size += len(text)
        else:
            if stack:
                done[id(seq)] = (first, len(out))
                spans.append((seq, start, size))
    text = "".join(out)
    _share(text, spans, uses, "skel")
    return text


def _sub_skel(piece):
    """skel подстановки (кусок с seed, узел Sub) для chk_arithsub: сбалансированная — `()` (её скобки и кавычки не
    меняют счёт), иначе её текст со skel вложенных. Снизу вверх, стеком: каждая считается один раз, иначе каждый
    уровень вложенности пересчитывал бы все более глубокие."""
    stack = [piece]
    while stack:
        cur = stack[-1]
        if _cached(cur.skel) is not None:
            stack.pop()
            continue
        need = []
        uses = []
        text = _render(list(cur), skel=True, need=need, uses=uses)
        if need:
            stack.extend(need)
            continue
        if _arith_neutral(text):
            cur.skel = "()"
        else:
            _share(text, [(cur, 0, len(text))], uses, "skel")
        stack.pop()
    return _cached(piece.skel)


def _doc_skel(doc, need):
    """skel тела heredoc (_sub_skel): подстановки, разобранные при раскрытии тела (doc.memo), — их skel, если их
    текст в теле — тот же, что печать. None — у них ещё нет skel (они — в need)."""
    done = _cached(doc.skel)
    if done is not None:
        return done
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
            if _cached(piece.skel) is None:
                if need is None:
                    return None
                need.append(piece)
            spans.append((key + 1, end, piece))
    if need:
        return None
    out = []
    uses = []
    size = 0
    prev = 0
    for a, b, piece in spans:
        printed = _printed(piece)
        if text[a:b] == printed:
            out.append(text[prev:a])
            size += a - prev
            skel = _cached(piece.skel)
            uses.append((piece.skel, size, len(skel), piece))
            out.append(skel)
            size += len(skel)
            prev = b
    out.append(text[prev:])
    skel = "".join(out)
    _share(skel, [(doc, 0, len(skel))], uses, "skel")
    return skel


def _printed(piece):
    """Печать куска piece (_render; готовая — срез общей строки)."""
    text = _cached(piece.text)
    return _render([piece]) if text is None else text


def _render(pieces, anchors=None, skel=False, need=None, where=None, deep=True, uses=None):
    """Текст, как его хранит и печатает bash (make_command_string, print_comsub в print_cmd.c): слова — текстом
    лексем, подстановки `$(…)`, `${ …; }`, `<(…)` — печатью их тела, heredoc — заголовок в строке команды, тела —
    за разделителем после неё. Пробелы и отступы bash не повторяются — только то, что меняет повторный разбор.
    Без рекурсии: стек элементов; время — по длине результата.

    anchors — список, куда добавляются (начало, конец, кусок) подстановок, уже разобранных parse_comsub (кусок с
    seed), внешних в тексте: повторный разбор текста берёт их готовыми (_seeds). skel — текст для chk_arithsub
    (_skel): вложенные подстановки — их skel; need — список, куда добавляются подстановки без skel (_sub_skel).
    where — список, куда добавляются пары (позиция результата, позиция исходного текста) у начала и конца каждого
    узла, подстановки и тела heredoc, по порядку результата: перевод позиций повторного разбора результата в
    исходный текст (_printed_pieces). С where куски с готовым текстом печатаются заново по их узлам; anchors с where
    не задаётся. deep=False — подстановка, разобранная parse_comsub (кусок с seed), печатается одним куском с
    отметкой (позиция, начало, True): повторный разбор берёт её готовой."""
    out = []
    size = 0
    tail = ""
    pending = [[]]
    # print_cmd.c: was_heredoc — последним напечатаны тела heredoc (за ними перевод строки), inside_function_def;
    # своё на каждую подстановку (print_comsub — новый make_command_string).
    was = [False]
    infunc = [0]
    semied = -1
    # Собранные куски (печать которых запоминается: text, _share), готовые куски в печати, повторы куска. uses
    # (параметр) — у skel: готовые skel вложенных кусков в результате (для _share вызывающего).
    spans = []
    if uses is None:
        uses = []
    done = {}
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
                text = None if item.balanced or item.seed is None else _cached(item.skel)
                if item.balanced:
                    stack.append("()")
                elif text is not None:
                    if uses is not None:
                        uses.append((item.skel, size, len(text), item))
                    stack.append(text)
                elif item.seed is not None and need is not None and type(item.seed[0]) is Sub:
                    need.append(item)
                else:
                    stack.extend(reversed(item))
                continue
            if where is not None:
                # Текст каждого куска печатается заново по узлам.
                if item.seed is not None:
                    node = item.seed[0]
                    if not deep:
                        # Подстановка, которую повторный разбор берёт готовой (_seeds): её текст — одним куском,
                        # иначе каждый уровень вложенности печатал бы все более глубокие заново.
                        text = _printed(item)
                        where.append((size, node.start + 1, ("atomic", item)))
                        where.append((size + len(text), node.end))
                        stack.append(text)
                        continue
                    where.append((size, node.start + 1))
                    stack.append(("at", node.end))
                elif item.at is not None:
                    where.append((size, item.at, ("span", item.to)) if item.span else (size, item.at))
                    stack.append(("at", item.to))
                stack.extend(reversed(item))
                continue
            if anchors is not None:
                if item.memo:
                    _memo_anchors(item.memo, size, anchors)
                if item.seed is not None:
                    text = _printed(item)
                    anchors.append((size, size + len(text), item))
                    uses.append((item.text, size, len(text), item))
                    stack.append(text)
                    continue
            else:
                text = _cached(item.text)
                if text is not None:
                    uses.append((item.text, size, len(text), item))
                    stack.append(text)
                    continue
            if anchors is None and id(item) in done:
                a, b = done[id(item)]
                stack.append("".join(out[a:b]))
            else:
                stack.append(("tpl_end", item, size, len(out)))
                stack.extend(reversed(item))
            continue
        if kind is list:
            stack.extend(reversed(item))
            continue
        if kind is tuple:
            tag = item[0]
            if tag == "tpl_end":
                obj = item[1]
                done[id(obj)] = (item[3], len(out))
                spans.append((obj, item[2], size))
            elif tag == "sub":
                if where is not None:
                    where.append((size, item[1].start + 1))
                    stack.append(("at", item[1].end))
                stack.extend(reversed(_sub_items(item[1])))
            elif tag == "at":
                where.append((size, item[1], item[2]) if len(item) > 2 else (size, item[1]))
            elif tag == "hd":
                pending[-1].append(item[1])
            elif tag == "conn":
                # Соединитель (make_command_string_internal, cm_connection): тела heredoc, ждущие его, — за ним
                # (print_deferred_heredocs). `;` сразу за телами (was_heredoc) bash не печатает: следующая команда
                # продолжает предыдущую (`a <<E && b; c` печатается `a <<E && ⏎E⏎ b c`); перевод строки печатает.
                text = item[1]
                if pending[-1]:
                    docs, pending[-1] = pending[-1], []
                    was[-1] = True
                    if text in (";", "\n"):
                        stack.append("\n" if text == "\n" or infunc[-1] else "")
                        stack.append(("bodies", docs))
                        stack.append("\n")
                    else:
                        stack.append(" ")
                        stack.append(("bodies", docs))
                        stack.append(text + "\n")
                elif text in (";", "\n") and was[-1]:
                    was[-1] = False
                    stack.append("\n" if text == "\n" or infunc[-1] else " ")
                else:
                    stack.append(text)
            elif tag == "flush":
                if pending[-1]:
                    docs, pending[-1] = pending[-1], []
                    was[-1] = True
                    stack.append(("bodies", docs))
                    stack.append("\n")
            elif tag == "was0":
                was[-1] = False
            elif tag == "fin":
                infunc[-1] += 1
            elif tag == "fout":
                infunc[-1] -= 1
            elif tag == "bodies":
                # Тела heredoc подряд; в anchors — подстановки, разобранные при раскрытии тела (body_parts).
                for doc in reversed(item[1]):
                    if where is not None:
                        stack.append(("at", getattr(doc, "term_end", None) or doc.body_end, ("end", doc.body_start)))
                    stack.append(doc.term + "\n")
                    if where is not None:
                        stack.append(("tspan", doc))
                    stack.append(("body", doc))
            elif tag == "body":
                doc = item[1]
                if skel:
                    text = _doc_skel(doc, need)
                    if text and uses is not None:
                        uses.append((doc.skel, size, len(text), doc))
                    stack.append(text or "")
                    continue
                if anchors is not None and getattr(doc, "memo", None):
                    _memo_anchors(doc.memo, size, anchors)
                if where is not None:
                    # Тело печатается как прочитано: его куски точны, посимвольно не уточняются.
                    where.extend((size + p[0], p[1], ("span", p[3]) if len(p) > 2 else "exact")
                                 for p in getattr(doc, "_pieces", None) or [(0, doc.body_start)])
                stack.append(doc.body_text)
            elif tag == "tspan":
                # Терминатор печатается за телом; в исходном — его строка (у тела до конца ввода — без ширины;
                # у heredoc, ждавшего тела снова, — печать нового терминатора, своей строки нет).
                doc = item[1]
                where.append((size, doc.body_end, ("span", getattr(doc, "term_end", None) or doc.body_end)))
            elif tag == "semi":
                if not (tail.endswith("\n") or tail.endswith(";") or tail == " &"):
                    stack.append(("semied",))
                    stack.append(";")
            elif tag == "semied":
                # Здесь кончается `;` за последней командой списка (печать `;` в конце тела, которой у bash нет).
                semied = size
            elif tag == "fend":
                # parse_comsub: перед `}` печати — перевод строки, если тело кончил перевод строки (was_newline), иначе
                # `;` по виду последней команды (was_semi, was_word): без него — только за фоновой (`a &`; `;`, который
                # здесь уже напечатан за последней командой, — он и есть). По виду печати судить нельзя: `${ echo \\;
                # ;}` печатается `${ echo \\;; }` (как _term_text).
                stack.append("\n }" if item[1] else " }" if item[2] or semied == size else "; }")
            elif tag == "enter":
                pending.append([])
                was.append(False)
                infunc.append(0)
            elif tag == "leave":
                pending.pop()
                was.pop()
                infunc.pop()
            continue
        if where is not None:
            where.append((size, item.start))
            stack.append(("at", item.end))
        stack.extend(reversed(_node_items(item)))
    text = "".join(out)
    _share(text, spans, uses, "text")
    return text


def _align(text, src, pieces, atomic, spans, hi, end=None, lazy=None, exact=()):
    """Куски перевода позиций печати text в исходный src, уточнённые посимвольно: внутри куска печать идёт за
    исходным текстом, пока символы совпадают; `\\` с переводом строки, снятый чтением, и пробел, которого нет в
    печати, — пропуск в исходном, добавленные печатью пробел, `;` и перевод строки — пропуск в печати (новый кусок).
    Остаток куска после иного несовпадения — переписанный кусок (начало, конец в исходном до следующей отметки):
    печать здесь не идёт за исходным текстом (новый терминатор heredoc в заголовке, `in "$@"` у `for`). Переписанные
    куски spans (значение `$'…'`) — переписанными целиком, atomic (подстановка одним куском) — сдвигом, с записью
    в lazy (начало, конец, исходные начало и конец, кусок печати) для _R.refine. Итог —
    [(начало в печати, начало в исходном)] и (…, начало, конец) у переписанных."""
    out = []
    n, m = len(text), len(src)
    for k, (a, o) in enumerate(pieces):
        b = pieces[k + 1][0] if k + 1 < len(pieces) else (n if end is None else end)
        o2 = pieces[k + 1][1] if k + 1 < len(pieces) else hi + 1
        if o2 <= o:
            # Следующий кусок — левее в исходном: этот идёт до конца исходного текста (не дальше hi).
            o2 = max(o + 1, min(hi + 1, o + (b - a)))
        if k in spans:
            out.append((a, o, o, max(o, spans[k])))
            continue
        if k in exact:
            out.append((a, o))
            continue
        if k in atomic and lazy is not None:
            # Подстановка, которую повторный разбор, скорее всего, возьмёт готовой: сдвигом; если разберёт заново —
            # кусок уточнится тогда по её печати (_R.refine), иначе каждый уровень вложенности перебирал бы все
            # более глубокие.
            out.append((a, o))
            lazy.append((a, b, o, o2, atomic[k]))
            continue
        out.append((a, o))
        i, j = a, o
        stop = min(o2, m)
        while i < b and j < stop:
            if text[i] == src[j]:
                i += 1
                j += 1
                continue
            if src.startswith("\\\n", j):
                j += 2
            elif src[j] in " \t\n":
                # Печать сжимает пробелы и переводы строки между словами (`a=( x)` — `a=(x)`).
                j += 1
            elif text[i] in " \t\n;":
                # Печать добавляет пробел, `;` или перевод строки (`${ a⏎}` — `${ a⏎ }`, `$(a⏎)` — `$(a)`).
                i += 1
            else:
                out.append((i, j, j, max(j + 1, o2)))
                break
            out.append((i, j))
        else:
            if i < b and j >= stop:
                # Исходный кусок кончился, печать — нет: напечатанное сверх него — переписанный кусок без ширины.
                out.append((i, j, j, j))
    return out


def _printed_pieces(entries, lo, hi, text=None, src=None, end=None):
    """Перевод позиций напечатанного текста (_render с where) в исходный текст: (куски [(начало в печати, начало
    в исходном)] — у переписанных (…, начало, конец), наибольшая исходная позиция каждого куска). Позиции — в [lo,
    hi]; кусок может вести назад (тело heredoc печатается за строкой команды, а `| cat` за ним — левее в исходном);
    предел куска — начало следующего, если оно правее. Отметка (позиция, начало, ("atomic", кусок печати)) —
    подстановка одним куском, (позиция, начало, ("span", конец)) — переписанный кусок, (позиция, начало, "exact") —
    тело heredoc (куски точны).
    Итог — (куски, пределы, lazy — см. _align)."""
    pieces = []
    atomic = {}
    exact = set()
    spans = {}
    # Куски, начатые отметкой конца строки терминатора за телом heredoc (("end", начало тела)): печать за телом
    # продолжает строку команды (`a <<E; b` печатается `a <<E` ⏎ `E` ⏎ `b`), и отметка следующего узла на той же
    # позиции печати, лежащего левее тела, переводит её на его исходное место, а не за терминатор.
    ended = {}
    for entry in entries:
        flag = entry[2] if len(entry) > 2 else None
        if type(flag) is tuple and flag[0] == "end":
            if pieces and pieces[-1][0] == entry[0]:
                continue
            pieces.append((entry[0], min(max(entry[1], lo), hi)))
            ended[len(pieces) - 1] = flag[1]
            continue
        k = len(pieces) - 1
        if pieces and pieces[-1][0] == entry[0] and k in ended and entry[1] < ended.pop(k):
            # Узел из строки команды (левее тела heredoc в исходном) — на своё место.
            pieces.pop()
        # Тело heredoc и его терминатор лежат в исходном там, где их прочитали (бывает за пределом hi конструкции).
        free = flag == "exact" or (type(flag) is tuple and flag[0] == "span" and entry[1] > hi)
        local, orig = entry[0], entry[1] if free else min(max(entry[1], lo), hi)
        if pieces and pieces[-1][0] == local:
            if orig > pieces[-1][1] or (orig == pieces[-1][1] and type(flag) is tuple and flag[0] == "span"):
                pieces[-1] = (local, orig)
                atomic.pop(len(pieces) - 1, None)
                exact.discard(len(pieces) - 1)
                spans.pop(len(pieces) - 1, None)
            else:
                continue
        elif pieces and orig == pieces[-1][1]:
            # Отметка с тем же исходным началом дальше в печати: напечатанное между ними (`;` списка внутри его
            # узла, исходный конец которого — до `;`) идёт от первой отметки.
            continue
        else:
            pieces.append((local, orig))
        if type(flag) is tuple and flag[0] == "atomic":
            atomic[len(pieces) - 1] = flag[1]
        elif flag == "exact":
            exact.add(len(pieces) - 1)
        elif flag is not None:
            spans[len(pieces) - 1] = max(flag[1], orig) if free else min(max(flag[1], orig), hi + 1)
    lazy = []
    if text is not None and src is not None and pieces:
        pieces = _align(text, src, pieces, atomic, spans, hi, end=end, lazy=lazy, exact=exact)
    elif spans:
        pieces = [(a, o, o, spans[k]) if k in spans else (a, o) for k, (a, o) in enumerate(pieces)]
    pieces = _merge_pieces(pieces)
    caps = _caps(pieces)
    if pieces and len(pieces[-1]) == 2 and pieces[-1][1] <= hi:
        caps[-1] = hi
    return pieces, caps, lazy


def _top(pieces, hi):
    """Наибольшая исходная позиция разбора напечатанного текста: hi или дальше — тело heredoc и его терминатор."""
    return max([hi] + [p[1] for p in pieces] + [p[3] - 1 for p in pieces if len(p) > 2])


def _caps(pieces):
    """Пределы кусков: символ куска — не дальше символа перед началом следующего (конец узла — позиция за его
    последним символом — не дальше начала следующего куска)."""
    return [max(pieces[k][1], pieces[k + 1][1] - 1) if pieces[k + 1][1] > pieces[k][1] else None
            for k in range(len(pieces) - 1)] + [None]


def _merge_pieces(pieces):
    """Соседние куски перевода, которые продолжают друг друга (сдвиг тот же), — одним куском: текст, легший в
    исходный подряд, — один кусок.
    Переписанные куски не сливаются."""
    out = []
    for piece in pieces:
        if (out and len(piece) == 2 and len(out[-1]) == 2
                and piece[1] - out[-1][1] == piece[0] - out[-1][0]):
            continue
        out.append(piece)
    return out


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
        return ["{" + node.kind[2], ("enter",)] + items + [("flush",), ("leave",),
                                                   ("fend", bool(node._nl), bool(body.commands)
                                                    and _background(body.commands[-1]))]
    first = _first_command(body.commands[0]) if body.commands else None
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
        # print_redirection_list сбрасывает was_heredoc.
        if type(target) is Heredoc:
            term = _sq(target.term) if target.quoted else target.term
            return [("was0",), fd + node.op + term, ("hd", target)]
        if node.op in ("<&", ">&"):
            return [("was0",), fd + node.op, target]
        return [("was0",), fd + node.op + " ", target]
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
        ops = node._ops or ()
        for k, command in enumerate(node.commands):
            if k:
                if k - 1 < len(ops) and ops[k - 1] == "|&":
                    # `a |& b` bash хранит как `a 2>&1 | b`: перенаправление печатается со списком перенаправлений
                    # `a` (print_redirection_list сбрасывает was_heredoc).
                    items += [("was0",), " 2>&1"]
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
    # was_heredoc (_render, "conn") сбрасывают конец `( )`, `{ }` и `then`, `do` после условия; за `fi`, `done`,
    # `esac` он остаётся (print_cmd.c).
    if kind is Subshell:
        return ["( ", node.body, ("flush",), " )", ("was0",)]
    if kind is Group:
        return ["{ ", node.body] + end + [" }", ("was0",)]
    if kind is If:
        items = []
        for k, (cond, body) in enumerate(node.clauses):
            items += (["if "] if not k else end + [" elif "]) + [cond] + end + [" then ", ("was0",), body]
        if node.orelse is not None:
            items += end + [" else ", node.orelse]
        return items + end + [" fi"]
    if kind in (While, Until):
        return (["while " if kind is While else "until ", node.cond] + end + [" do ", ("was0",), node.body] + end
                + [" done"])
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
        return ["function ", node.name, " () ", ("fin",), node.body, ("fout",), ("was0",)]
    if kind is Coproc:
        return ["coproc "] + ([node.name, " "] if node.name is not None else []) + [node.body]
    if kind is ArithCmd:
        return ["((", *(node.expr._tok or []), "))"]
    if kind is Cond:
        return ["[[ "] + _join(node._toks or node.words, " ") + [" ]]"]
    return []


def _print_drops(script):
    """Печать тела подстановки (print_comsub) опускает `;` сразу за телами heredoc (was_heredoc, _render "conn"):
    bash исполняет напечатанный текст, и команда за этим `;` — слова предыдущей (`$(a <<E; b; c` ⏎ `E` ⏎ `)`
    исполняет `b c`). Обход только команд этого уровня (слова и подстановки в них не печатаются заново): каждый
    узел — в одной подстановке."""
    pending = was = False
    infunc = 0
    stack = [script]
    while stack:
        item = stack.pop()
        kind = type(item)
        if kind is tuple:
            tag = item[0]
            if tag == "conn":
                if pending:
                    pending, was = False, True
                elif item[1] in (";", "\n") and was:
                    was = False
                    if item[1] == ";" and not infunc:
                        return True
            elif tag == "flush":
                if pending:
                    pending, was = False, True
            elif tag == "hd":
                pending = True
            elif tag == "was0":
                was = False
            elif tag == "fin":
                infunc += 1
            elif tag == "fout":
                infunc -= 1
        elif kind is list:
            stack.extend(reversed(item))
        elif isinstance(item, Node) and kind is not Word and kind is not Heredoc:
            stack.extend(reversed(_node_items(item)))
    return False


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
    return r.E(close_pos)


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
    строка тела heredoc, начатая терминатором, со знаком конца в остатке кончает тело (make_here_document).
    Вложенность глубже предела стека — Script без команд с ошибкой вида DEPTH, сбой разбора — с ошибкой вида
    INTERNAL на позиции 0."""
    if not isinstance(text, str):
        text = str(text)
    reader = _R(text)
    reader.shared[_REEXPAND_LEFT] = _REEXPAND_BASE + _REEXPAND_PER * len(text)
    reader.shared[_SOURCE] = text
    try:
        script = _run(reader.toplevel(eof or None))
        script.rewritten = sorted(reader.shared.get(_SPANS, ()))
        return script
    except _TooDeep:
        # Вложенность глубже предела: дерево не строится, ошибка — там, где остановилось чтение.
        error = SyntaxIssue(min(reader.cpos, reader.n0), True, DEPTH)
    except Exception:  # noqa: BLE001 — сбой разбора не выходит наружу: весь текст — ошибка в начале
        error = SyntaxIssue(0, True, INTERNAL)
    script = _node(Script, 0, len(text), commands=[])
    script.error = error
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
    reader.shared[_REEXPAND_LEFT] = _REEXPAND_BASE + _REEXPAND_PER * len(text)
    reader.shared[_SOURCE] = text
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
