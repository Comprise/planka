#!/usr/bin/env python3
"""Дифференциальный фаззер детектора зависимостей и разбора shparse против настоящего bash.

Формы команд строит только генератор этого файла из фрагментов грамматики bash, детерминированно по зерну и номеру
формы (`random.Random(f"{seed}:{index}")`): команды из файлов, stdin, корпусов и транскриптов сюда не попадают и не
исполняются никогда. Единственное внешнее действие формы — маркер `touch P<n>`. Генератор ставит маркер только туда,
где он исполнится, если bash прочтёт его командой: без `false &&`, без невызванных функций, без ложных условий;
маркер в данных (кавычки, комментарий, тело heredoc `cat`) — проверка, что bash его командой не читает.

Форма исполняется так, как её исполняет инструмент Bash Claude Code (`bash -c 'shopt -u extglob … && eval '<форма>''`),
и только внутри `bwrap`: корень только для чтения, запись — в новый пустой каталог случая, сеть, PID и прочие
пространства имён отделены, окружение очищено. Без `bwrap` или если пробный запуск показывает, что песочница
пропускает запись наружу или сеть, фаззер выходит с кодом 2 и ничего не исполняет.

Режимы:
- `--target detector --engine old|new` — маркер исполнен, а детектор на той же форме с `npm i evil` вместо маркеров
  молчит — потеря; не исполнен ни один, а детектор отказал — строгость (счётчик).
- `--target parser` — множество исполненных маркеров против имён `touch P<n>` среди `shparse.simple_commands`;
  исполненный маркер, которого в дереве нет, но текст которого стоит в подстановке в слове команды, — вид `output`
  (имя или аргумент команды — вывод подстановки; семантика исполнения), не потеря.
- `--compare old new` — расхождения вердиктов двух движков детектора на одних формах (bash не нужен).

Расхождения — JSON-строками в файл внутри `--out`; итог — в stdout; код выхода 1 при потере или ошибке разбора."""

import argparse
import collections
import json
import multiprocessing
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLANKA = os.path.join(REPO, "plugin", "planka")

# Без \b: маркер в данных может стоять вплотную к следующему (`touch P2touch P3`), подстановка заменяет оба.
MARKER = re.compile(r"touch (P\d+)")
MARKER_FILE = re.compile(r"P\d+")
EVIL = "npm i evil"
TIMEOUT = 5
# Глубина вложения команд по умолчанию; меньшая (--depth) даёт короткие формы для поиска минимального случая.
MAX_DEPTH = 3
# Разбор дольше этого — строка `slow` в файле расхождений (линейность разбора), не потеря.
SLOW = 1.0


# ---------------------------------------------------------------------------------------------------------------
# Генератор форм


class Gen:
    """Одна форма: маркеры нумеруются по порядку, имена переменных и функций свежие, теги — классы форм."""

    def __init__(self, rng, runtime=True, depth=MAX_DEPTH):
        self.r = rng
        self.depth = depth
        # Формы, которые исполняют строку во время работы (eval, sh -c, вход оболочки): в дереве разбора их маркеры —
        # строки, не команды; режим parser их не строит.
        self.runtime = runtime
        self.n = 0
        self.serial = 0
        self.tags = set()
        # Стек отложенных тел heredoc: тело идёт за ближайшим переводом строки своего уровня подстановки.
        self.scopes = [[]]

    # -- служебное

    def chance(self, p):
        return self.r.random() < p

    def pick(self, seq):
        return self.r.choice(seq)

    def weighted(self, table):
        names = [name for name, _ in table]
        weights = [w for _, w in table]
        return self.r.choices(names, weights)[0]

    def tag(self, name):
        self.tags.add(name)

    def fill(self, form, d):
        """Подставляет в шаблон только его места, слева направо: {b} — список команд, {s} — подстановка `$(…)`,
        {m} — маркер, {name} — свежее имя. Неиспользованное не строится: его heredoc не попали бы в форму."""
        def one(match):
            key = match.group(1)
            if key == "b":
                return self.clist(d + 1)
            if key == "s":
                return "$(" + self.scoped(d + 1, ")", ")", ")")
            if key == "m":
                return self.marker()
            return self.fresh("C")
        return re.sub(r"\{(b|s|m|name)\}", one, form.replace("{{", "\0").replace("}}", "\1")).replace(
            "\0", "{").replace("\1", "}")

    def marker(self):
        self.n += 1
        return f"touch P{self.n}"

    def fresh(self, prefix):
        self.serial += 1
        return f"{prefix}{self.serial}"

    def newline(self):
        """Перевод строки, за ним — тела heredoc, открытых на этой строке."""
        pending = self.scopes[-1]
        out = "\n"
        while pending:
            out += pending.pop(0) + "\n"
        return out

    def script(self, d):
        """Самостоятельный текст (eval, sh -c, вход оболочки): свой уровень heredoc, тела — в конце."""
        self.scopes.append([])
        body = self.clist(d)
        pending = self.scopes.pop()
        if pending:
            body += "\n" + "\n".join(pending)
        return body

    def scoped(self, d, inline, line, rest, transform=None):
        """Тело подстановки со своими heredoc. inline — конец без тел heredoc, line — конец строкой после тел,
        rest — знак конца в остатке строки терминатора (`E)`: bash 5.3 кончает тело и подстановку)."""
        self.scopes.append([])
        body = self.clist(d)
        pending = self.scopes.pop()
        if pending:
            body += "\n" + "\n".join(pending)
            if rest and self.chance(0.4):
                self.tag("heredoc-term-closer")
                tail = rest
            else:
                tail = "\n" + line
        else:
            tail = inline
        if transform:
            body = transform(body)
        return body + tail

    # -- списки и конвейеры

    def clist(self, d):
        k = self.r.randint(1, 3) if d == 0 else (1 if self.chance(0.7) else 2)
        out = ""
        for i in range(k):
            if i:
                out += self.sep()
            out += self.andor(d)
        return out

    def sep(self):
        kind = self.weighted([("semi", 5), ("newline", 5), ("amp", 1), ("cont", 1)])
        if kind == "semi":
            return self.pick(["; ", ";", " ; "])
        if kind == "newline":
            return self.newline()
        if kind == "amp":
            self.tag("background")
            return " & "
        self.tag("continuation")
        return " \\\n; "

    def andor(self, d):
        text = self.pipeline(d)
        if self.chance(0.2):
            self.tag("andor")
            # Левая часть с известным исходом: правая исполнится.
            lead = self.pick(["true && ", ": && ", "false || ", "[[ a == a ]] && ", "[ -n a ] && ",
                              "test 1 && ", "! false && ", "{ false; } || ", f"{self.marker()} && "])
            text = lead + text
        return text

    def pipeline(self, d):
        if self.chance(0.12):
            self.tag("pipeline")
            return self.command(d) + self.pick([" | ", " |& ", "|"]) + self.command(d)
        if self.chance(0.05):
            self.tag("bang-time")
            return self.pick(["! ", "time ", "time -p ", "! time "]) + self.command(d)
        return self.command(d)

    # -- команды

    def command(self, d):
        if d >= self.depth:
            kind = self.weighted([("marker", 6), ("echo", 2), ("true", 1)])
        else:
            table = [("marker", 8), ("echo", 10), ("assign", 3), ("array", 3), ("array_error", 2),
                     ("heredoc_cat", 6), ("herestring", 2), ("subshell", 3), ("group", 3), ("function", 2),
                     ("if", 3), ("loop", 3), ("case", 3), ("cond", 3), ("arith_cmd", 2), ("procsub", 2),
                     ("comment", 3), ("syntax_error", 2), ("continuation", 2), ("coproc", 1)]
            if self.runtime:
                table += [("eval", 3), ("heredoc_shell", 3), ("pipe_shell", 2)]
            kind = self.weighted(table)
        return getattr(self, "c_" + kind)(d)

    def c_true(self, d):
        return self.pick(["true", ":"])

    def c_marker(self, d):
        return self.marker()

    def c_echo(self, d):
        words = [self.word(d) for _ in range(self.r.randint(1, 2))]
        cmd = self.pick(["echo", "printf '%s\\n'", "echo -n", ":"]) + " " + " ".join(words)
        if self.chance(0.2):
            self.tag("redirect")
            cmd += self.pick([" >/dev/null", " 2>&1", " >|/dev/null", " 2>/dev/null", " </dev/null"])
        return cmd

    def c_assign(self, d):
        self.tag("assign")
        text = self.fresh("v") + self.pick(["=", "+="]) + self.word(d)
        if self.chance(0.3):
            text += " " + self.marker()
        return text

    def c_array(self, d):
        self.tag("array")
        words = " ".join(self.word(d + 1) for _ in range(self.r.randint(0, 3)))
        return self.fresh("a") + self.pick(["=(", "+=(", "=( "]) + words + self.pick([")", " )"])

    def c_array_error(self, d):
        # bash 5.3 (parse_compound_assignment) отбрасывает строку с такой скобкой, следующие строки читает.
        self.tag("array-error")
        bad = self.pick(["; ", " | ", " & ", " && ", " >/dev/null ", " (x) ", " <<E ", " ;; ", "\n"])
        return self.fresh("a") + "=(" + self.word(d + 1) + bad + self.marker() + ")"

    def heredoc_op(self):
        """Оператор и терминатор: (текст оператора, терминатор, снимать ли табы, в кавычках ли)."""
        term = self.pick(["E", "EOF", "END"]) + str(self.fresh(""))
        strip = self.chance(0.25)
        op = "<<-" if strip else "<<"
        style = self.weighted([("plain", 5), ("space", 1), ("sq", 2), ("dq", 1), ("bs", 1), ("partial", 1)])
        if style == "plain":
            word, quoted = term, False
        elif style == "space":
            word, quoted = " " + term, False
        elif style == "sq":
            word, quoted = f"'{term}'", True
        elif style == "dq":
            word, quoted = f'"{term}"', True
        elif style == "bs":
            word, quoted = "\\" + term, True
        else:
            word, quoted = term + "''", True
        if strip:
            self.tag("heredoc-strip-tabs")
        if quoted:
            self.tag("heredoc-quoted-term")
        return op + word, term, strip, quoted

    def heredoc_body_line(self, d, term):
        kind = self.weighted([("data", 3), ("sub", 4), ("escaped", 1), ("sq", 1), ("cont", 2), ("like", 2),
                              ("closer", 1)])
        if kind == "data":
            return self.pick(["", "x ", "# ", "'", '"']) + self.marker()
        if kind == "sub":
            return self.pick(["", "a ", '"']) + self.part(d + 1, subs_only=True)
        if kind == "escaped":
            return "\\$(" + self.marker() + ")"
        if kind == "sq":
            return "'$(" + self.marker() + ")'"
        if kind == "cont":
            self.tag("heredoc-continuation")
            return self.pick(["x \\", "\\", "x \\\\", term + "\\"])
        if kind == "like":
            self.tag("heredoc-lookalike")
            return self.pick([term + " x", " " + term, term + ";", term + term, "\t" + term, term + "'"])
        self.tag("heredoc-closer-line")
        return self.pick([")", "}", "`", "))", "; }"])

    def queue_heredoc(self, term, strip, body_lines):
        lines = body_lines + [term]
        if strip:
            lines = ["\t" + line for line in lines]
        self.scopes[-1].append("\n".join(lines))
        if len(self.scopes[-1]) > 1:
            self.tag("heredoc-several")
        if len(self.scopes) > 1:
            self.tag("heredoc-in-substitution")

    def c_heredoc_cat(self, d):
        self.tag("heredoc")
        op, term, strip, quoted = self.heredoc_op()
        body = [self.heredoc_body_line(d, term) for _ in range(self.r.randint(1, 3))]
        self.queue_heredoc(term, strip, body)
        cmd = self.pick(["cat", "cat >/dev/null", ": ", "true", "cat -"]) + " " + op
        if self.chance(0.3):
            cmd += self.pick([" | cat", "; " + self.marker(), " && " + self.marker(), " >/dev/null"])
        return cmd

    def c_heredoc_shell(self, d):
        self.tag("heredoc-shell")
        op, term, strip, quoted = self.heredoc_op()
        body = self.script(d + 1).split("\n")
        self.queue_heredoc(term, strip, body)
        return self.pick(["bash ", "sh ", "bash -s ", "true | bash ", "cat ", "source /dev/stdin "]) + op + (
            " | bash" if self.chance(0.2) else "")

    def c_herestring(self, d):
        self.tag("herestring")
        if self.runtime and self.chance(0.5):
            return self.pick(["bash", "sh"]) + " <<< " + self.quoted_script(d)
        return self.pick(["cat", "cat >/dev/null", "read -r x"]) + " <<< " + self.word(d)

    def quoted_script(self, d):
        inner = self.script(d + 1)
        style = self.pick(["sq", "dq", "ansi"])
        if style == "sq":
            return shlex.quote(inner) if self.chance(0.7) else "'" + inner.replace("'", "'\\''") + "'"
        if style == "dq":
            escaped = inner.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
            return '"' + escaped + '"'
        escaped = inner.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        return "$'" + escaped + "'"

    def c_eval(self, d):
        self.tag("eval-sh-c")
        return self.pick(["eval ", "bash -c ", "sh -c ", "bash -c -- ", "eval -- "]) + self.quoted_script(d)

    def c_pipe_shell(self, d):
        self.tag("pipe-shell")
        return self.pick(["echo ", "printf '%s\\n' "]) + self.quoted_script(d) + self.pick([" | bash", " | sh",
                                                                                            "|bash -s"])

    def c_subshell(self, d):
        self.tag("subshell")
        return self.pick(["( ", "("]) + self.clist(d + 1) + self.pick([" )", ")"])

    def c_group(self, d):
        self.tag("group")
        return "{ " + self.clist(d + 1) + self.pick(["; }", "\n}", " ;}"])

    def c_function(self, d):
        self.tag("function")
        name = self.fresh("f")
        body = self.clist(d + 1)
        form = self.pick(["{name}() {{ {b}; }}", "function {name} {{ {b}; }}", "{name}() ( {b} )",
                          "function {name}() {{ {b}\n}}"])
        return form.format(name=name, b=body) + self.pick(["; ", "\n"]) + name

    def c_if(self, d):
        self.tag("if")
        body = self.clist(d + 1)
        cond = self.pick(["true", ":", "[[ a ]]", "! false", "test 1", self.marker()])
        form = self.pick(["if {c}; then {b}; fi", "if false; then :; elif {c}; then {b}; fi",
                          "if false; then :; else {b}; fi", "if {c}\nthen {b}\nfi"])
        return form.format(c=cond, b=body)

    def c_loop(self, d):
        self.tag("loop")
        # Счётчик, а не `while :`: если тело съест `break` (кавычка, heredoc), цикл всё равно кончится.
        n = self.fresh("n")
        form = self.pick([f"while (( {n}++ < 1 )); do {{b}}; break; done", f"until (( {n}++ >= 1 )); do {{b}}; done",
                          "for i in a; do {b}; done", "for ((i=0; i<1; i++)); do {b}; done",
                          "for i in $({m}) a; do :; done", f"while {{m}} && (( {n}++ < 1 )); do :; done",
                          "for i in a\ndo {b}\ndone"])
        return self.fill(form, d)

    def c_case(self, d):
        self.tag("case")
        subject = self.pick(["a", '"a"', "'a'", "$({m})a", "${{u:-a}}"])
        pattern = self.pick(["a", "*", "(a)", "a|b", "[a]", "$({m})|a", "a)|(b"])
        end = self.pick([";;", ";&", ";;&"])
        form = self.pick(["case {s} in {p}) {b}{e} esac", "case {s} in\n{p}) {b}\n{e}\nesac",
                          "case {s} in b) :;& {p}) {b};; esac", "case {s} in ({p}) {b}{e} esac"])
        return self.fill(form.replace("{s}", subject).replace("{p}", pattern).replace("{e}", end), d)

    def c_cond(self, d):
        self.tag("cond")
        form = self.pick(["[[ -z {s} ]]", '[[ -z "{s}" ]]', "[[ a == a && -z {s} ]]", "[[ a == b || -z {s} ]]",
                          "[[ {s} == * ]]", "[[ ( -z {s} ) ]]", "[[ a =~ ({s}) ]]", "[[ -n a ]] && {m}"])
        return self.fill(form, d)

    def c_arith_cmd(self, d):
        self.tag("arith-cmd")
        return self.fill(self.pick(["(( {s} 1 ))", "(( x = {s} 1 ))", "(( 0 && {s} 1 ))", "((({b}) ))",
                                    "( ({b}) )"]), d)

    def c_procsub(self, d):
        self.tag("procsub")
        sub = self.scoped(d + 1, ")", ")", ")")
        return self.pick(["cat <({s}", "cat < <({s}", "echo x > >({s}", ": <({s}", "cat <({s} >/dev/null"]).format(
            s=sub)

    def c_comment(self, d):
        self.tag("comment")
        bits = "".join(self.pick([" ", "x", ")", "'", '"', "`", "$(", "}", ";", self.marker()])
                       for _ in range(self.r.randint(1, 3)))
        lead = self.pick(["", "true ", "echo a", "echo a#b ", "echo $# ", "echo \\#", "true;"])
        return lead + "#" + bits

    def c_syntax_error(self, d):
        self.tag("syntax-error")
        m = self.marker()
        return self.pick([
            "echo )", ")", "fi", "done", "esac", "then", ";;", "| true", "&& true", "( )", "{ }", "echo (",
            "if true; then " + m, "echo 'a; " + m, 'echo "a; ' + m, "echo $(true; " + m, "case a in",
            "echo a >", "cat <<", "function", "while", "x=(a", "echo `" + m, "echo ${u:-" + m,
            "echo $((1 + ))", "for in; do " + m + "; done", "echo }; " + m])

    def c_continuation(self, d):
        self.tag("continuation")
        return self.pick(["echo", "true", ":"]) + " \\\n" + self.word(d) + self.pick(["", " \\\n" + self.word(d)])

    def c_coproc(self, d):
        self.tag("coproc")
        # Вход coproc — канал от оболочки, он не закрывается: читающая stdin команда ждала бы вечно.
        return self.fill(self.pick(["coproc {m} </dev/null", "coproc {name} {{ {b}; }} </dev/null", "{{ {b}; }} &"]),
                         d)

    # -- слова

    def word(self, d):
        k = 1 if self.chance(0.75) else 2
        return "".join(self.part(d) for _ in range(k))

    LITS = ["a", "x.y", "-f", "--opt=v", "a#b", "{a,b}", "\\;", "\\'", '\\"', "\\#", "\\(", "\\)", "\\$x", "~",
            "$HOME", "$$", "$#", "$?", "a\\ b", "%", "@", "]", "[", "=", "a=b", "}", "{", "\\`", "\\\\", "$", "$x"]

    def part(self, d, subs_only=False):
        if d >= self.depth:
            kind = self.weighted([("lit", 4), ("sq", 2), ("ansi", 1)] if not subs_only else [("sq", 1)])
        elif subs_only:
            kind = self.weighted([("comsub", 5), ("backtick", 2), ("funsub", 1), ("valsub", 1), ("param", 2),
                                  ("arith", 1)])
        else:
            kind = self.weighted([("lit", 8), ("sq", 4), ("dq", 5), ("ansi", 2), ("comsub", 5), ("backtick", 2),
                                  ("funsub", 2), ("valsub", 1), ("param", 3), ("arith", 2), ("procsub", 1),
                                  ("brace", 1), ("arith_ambig", 1)])
        return getattr(self, "w_" + kind)(d)

    def w_lit(self, d):
        return self.pick(self.LITS)

    def data_bits(self, table, k=(1, 3)):
        return "".join(self.marker() if bit is None else bit
                       for bit in (self.pick(table) for _ in range(self.r.randint(*k))))

    def w_sq(self, d):
        self.tag("single-quote")
        return "'" + self.data_bits(["a b", ";", "#", '"', "$(", ")", "`", "\\", "$x", "\n", "}", "<<E", None,
                                     None]) + "'"

    def w_ansi(self, d):
        self.tag("ansi-c")
        return "$'" + self.data_bits(["a", "\\'", "\\\\", "\\n", "\\x41", '"', "$(", "`", ";", " ", None,
                                      None]) + "'"

    def w_dq(self, d):
        self.tag("double-quote")
        out = '"'
        for _ in range(self.r.randint(1, 3)):
            if d < self.depth and self.chance(0.3):
                out += self.part(d, subs_only=True)
            else:
                out += self.pick(["a b", ";", "#", "'", '\\"', "\\\\", "\\$", "\\`", "$x", "${HOME}", ")", "}",
                                  "<<E", "\n", self.marker()])
        return out + '"'

    def w_comsub(self, d):
        self.tag("comsub")
        return self.pick(["$(", "$( "]) + self.scoped(d + 1, ")", ")", ")")

    def w_backtick(self, d):
        self.tag("backtick")

        def escape(body):
            # Вложенная `…` — с `\` перед `\`, обратной кавычкой и `$`, как требует bash.
            if "`" in body:
                self.tag("backtick-nested")
                return body.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
            return body

        return "`" + self.scoped(d + 1, "`", "`", "`", transform=escape)

    def w_funsub(self, d):
        self.tag("funsub")
        return "${ " + self.scoped(d + 1, self.pick(["; }", ";}", "\n}"]), "}", "}")

    def w_valsub(self, d):
        self.tag("valsub")
        return "${| " + self.scoped(d + 1, self.pick(["; }", "\n}"]), "}", "}")

    def w_param(self, d):
        self.tag("param")
        u = self.fresh("u")
        # Операторы, при которых слово раскрывается: u не задана, HOME задана.
        form = self.pick(["${{{u}:-{w}}}", "${{{u}-{w}}}", "${{{u}:={w}}}", "${{{u}={w}}}", "${{HOME:+{w}}}",
                          "${{HOME+{w}}}", "${{HOME#{w}}}", "${{HOME##{w}}}", "${{HOME%{w}}}", "${{HOME%%{w}}}",
                          "${{HOME/{w}/x}}", "${{HOME//x/{w}}}", "${{HOME:{a}0:1}}", "${{{u}[{a}0]}}",
                          '"${{{u}:-{w}}}"'])
        if "{a}" in form:
            self.tag("param-arith")
        return form.format(u=u, w=self.pword(d), a=self.part(d, subs_only=True))

    def pword(self, d):
        """Слово оператора `${…}`: пробелы, кавычки, `}` в кавычках, вложенные подстановки."""
        out = ""
        for _ in range(self.r.randint(1, 3)):
            kind = self.weighted([("lit", 3), ("q", 3), ("sub", 4), ("procsub", 1)])
            if kind == "lit":
                out += self.pick(["a", "a b", "{a,b}", "\\}", "#", ";", "x y z", "$HOME"])
            elif kind == "q":
                out += self.pick(["'}'", '"}"', "'a b'", '"a b"']) if self.chance(0.6) else self.w_sq(d)
            elif kind == "sub" and d < self.depth:
                out += self.part(d, subs_only=True)
            elif kind == "procsub" and d < self.depth:
                self.tag("procsub-in-param")
                out += "<(" + self.scoped(d + 1, ")", ")", ")")
            else:
                out += "a"
        return out

    def w_arith(self, d):
        self.tag("arith")
        bits = []
        for _ in range(self.r.randint(1, 3)):
            kind = self.weighted([("num", 3), ("sub", 4), ("quoted", 1), ("paren", 1)])
            if kind == "num":
                bits.append(self.pick(["1", "+ 1", "x", "${u:-1}", "- 0"]))
            elif kind == "sub":
                bits.append(self.part(d, subs_only=True))
            elif kind == "quoted":
                self.tag("arith-quote")
                bits.append("'$(" + self.marker() + ")'")
            else:
                bits.append("(1)")
        expr = " ".join(bits) + " 1"
        if self.chance(0.3):
            self.tag("arith-bracket")
            return "$[ " + expr + " ]"
        return "$(( " + expr + " ))"

    def w_procsub(self, d):
        self.tag("procsub")
        return self.pick(["<(", ">("]) + self.scoped(d + 1, ")", ")", ")")

    def w_brace(self, d):
        self.tag("brace")
        return "{a," + self.part(d, subs_only=True) + "}"

    def w_arith_ambig(self, d):
        # `$((…) …)` — в bash 5.3 подстановка с подоболочкой, не арифметика.
        self.tag("arith-ambiguous")
        return "$((" + self.clist(d + 1) + ") )"


def generate(seed, index, runtime=True, depth=MAX_DEPTH):
    """Форма номер index для зерна seed: (текст, число маркеров, классы)."""
    gen = Gen(random.Random(f"{seed}:{index}"), runtime, depth)
    text = gen.clist(0)
    pending = gen.scopes[0]
    if pending:
        text += "\n" + "\n".join(pending)
    # Маркер в отброшенной ветке шаблона в текст не попал: число — по тексту.
    return text, len(set(MARKER.findall(text))), gen.tags


# ---------------------------------------------------------------------------------------------------------------
# Песочница


def sandbox_argv(bwrap, case, script):
    return [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", case, case, "--chdir", case, "--unshare-all", "--die-with-parent", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", case, "bash", "-c", script]


def claude_wrapper(form):
    """Обёртка инструмента Bash Claude Code; `wait` за ней дожидается фоновых задач и `>(…)` формы."""
    return "shopt -u extglob 2>/dev/null || true && eval " + shlex.quote(form) + " ; wait"


def run_sandboxed(bwrap, root, script):
    """Исполняет script в новом пустом каталоге случая внутри root: (созданные имена, истёк ли срок)."""
    case = tempfile.mkdtemp(prefix="case-", dir=root)
    try:
        timeout = False
        try:
            subprocess.run(sandbox_argv(bwrap, case, script), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            timeout = True
        return set(os.listdir(case)), timeout
    finally:
        # Каталог случая создан здесь же и лежит внутри --out.
        shutil.rmtree(case, ignore_errors=True)


def check_sandbox(out):
    """Пробный запуск: песочница пишет только в каталог случая и не видит сети. Причина отказа или None."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None, "bwrap не найден: без песочницы фаззер формы не исполняет"
    tag = f".bashdiff-probe-{os.getpid()}"
    outside = [os.path.join(REPO, tag), os.path.join(os.path.expanduser("~"), tag), os.path.join(out, tag),
               os.path.join(os.path.dirname(os.path.abspath(out)), tag)]
    for path in outside:
        if os.path.lexists(path):
            return None, f"пробный файл уже существует: {path}"
    script = "touch ok; " + "; ".join("touch " + shlex.quote(p) for p in outside) + \
        "; (exec 3<>/dev/tcp/1.1.1.1/53) 2>/dev/null && touch net; " + \
        "(exec 3<>/dev/tcp/127.0.0.1/22) 2>/dev/null && touch lo; true"
    try:
        made, timeout = run_sandboxed(bwrap, out, script)
    except OSError as error:
        return None, f"bwrap не запускается: {error}"
    if timeout:
        return None, "пробный запуск bwrap не уложился в срок"
    if "ok" not in made:
        return None, "пробный запуск bwrap не создал файл в каталоге случая: песочница не работает"
    leaked = [p for p in outside if os.path.lexists(p)]
    if leaked:
        return None, "песочница пропустила запись наружу: " + ", ".join(leaked) + " (файлы оставлены)"
    if "net" in made or "lo" in made:
        return None, "песочница пропустила сеть"
    return bwrap, None


# ---------------------------------------------------------------------------------------------------------------
# Проверяемые движки


def load_engine(name):
    """(add, doubt) детектора: old — публичные функции, new — путь над деревом shparse."""
    if PLANKA not in sys.path:
        sys.path.insert(0, PLANKA)
    import depcheck
    if name == "old":
        return depcheck.dependency_add, depcheck.dependency_doubt
    add, doubt = getattr(depcheck, "_tree_add", None), getattr(depcheck, "_tree_doubt", None)
    if add is None or doubt is None:
        raise ImportError("в depcheck нет _tree_add/_tree_doubt (появятся в волне 2, задача 5)")
    return add, doubt


def load_parser():
    if PLANKA not in sys.path:
        sys.path.insert(0, PLANKA)
    import shparse
    return shparse


def verdict(engine, form):
    """Вердикт детектора на форме с `npm i evil` вместо маркеров: {"add", "doubt"}."""
    add, doubt = engine
    text = MARKER.sub(EVIL, form)
    found = add(text)
    return {"add": found, "doubt": None if found is not None else doubt(text)}


def parsed_markers(shparse, form):
    """Маркеры среди команд дерева: имя — `touch`, перед ним только подстановки и параметры (`` `…`touch ``: пустой
    вывод даёт имя `touch`), аргумент начинается литералом `P<n>`, за ним — только подстановки и параметры
    (`` P1`…` ``). Текст в кавычках маркером не считается: `echo 'touch P1'` маркер прятал бы."""
    names = set()
    expansions = (shparse.Sub, shparse.Param)
    for simple in shparse.simple_commands(shparse.parse(form)):
        words = simple.words
        if len(words) < 2:
            continue
        name, arg = words[0].parts, words[1].parts
        if not (name and isinstance(name[-1], shparse.Lit) and name[-1].text == "touch"
                and all(isinstance(p, expansions) for p in name[:-1])):
            continue
        # `touch` создаёт файл по каждому аргументу: маркер — любой аргумент такого вида (`touch P4`…`touch P7`).
        for word in words[1:]:
            arg = word.parts
            if (arg and isinstance(arg[0], shparse.Lit) and MARKER_FILE.fullmatch(arg[0].text)
                    and all(isinstance(p, expansions) for p in arg[1:])):
                names.add(arg[0].text)
    return names


def output_markers(shparse, form, lost):
    """Маркеры из lost, текст которых стоит в теле или данных подстановки в слове команды (Simple.words): имя или
    аргумент команды — вывод подстановки, bash исполняет его при раскрытии (`${ printf 'touch P1'; }`,
    `$(cat <<E` ⏎ `touch P1` ⏎ `E` ⏎ `)`). Это семантика исполнения: дерево такой команды не показывает."""
    found = set()
    script = shparse.parse(form)
    for simple in shparse.simple_commands(script):
        for word in simple.words:
            for part in word.parts:
                if not isinstance(part, shparse.Sub):
                    continue
                texts = [form[part.start:part.end]]
                texts += [node.body_text for node in shparse.walk(part.body) if isinstance(node, shparse.Heredoc)]
                for marker in lost - found:
                    pattern = re.compile(re.escape(marker) + r"(?!\d)")
                    if any(pattern.search(text) for text in texts):
                        found.add(marker)
    return found


# ---------------------------------------------------------------------------------------------------------------
# Прогон

CONFIG = {}


def init_worker(config):
    CONFIG.update(config)
    target = config["target"]
    if target == "detector":
        CONFIG["engine"] = load_engine(config["engine_name"])
    elif target == "parser":
        CONFIG["shparse"] = load_parser()
    else:
        CONFIG["engines"] = [load_engine(name) for name in config["compare"]]


def run_case(index):
    cfg = CONFIG
    form, count, tags = generate(cfg["seed"], index, runtime=cfg["target"] != "parser", depth=cfg["depth"])
    record = {"seed": cfg["seed"], "index": index, "depth": cfg["depth"], "form": form, "markers": count,
              "classes": sorted(tags)}
    kinds = []
    if cfg["target"] in ("detector", "parser"):
        made, timeout = run_sandboxed(cfg["bwrap"], cfg["cases"], claude_wrapper(form))
        executed = sorted((m for m in made if MARKER_FILE.fullmatch(m)), key=lambda m: int(m[1:]))
        record["executed"] = executed
        if timeout:
            kinds.append("timeout")
    started = time.monotonic()
    try:
        if cfg["target"] == "detector":
            record["verdict"] = v = verdict(cfg["engine"], form)
            denied = v["add"] is not None or v["doubt"] is not None
            if executed and not denied:
                kinds.append("loss")
            elif not executed and denied:
                kinds.append("strict")
        elif cfg["target"] == "parser":
            parsed = parsed_markers(cfg["shparse"], form)
            record["parsed"] = sorted(parsed, key=lambda m: int(m[1:]))
            lost = set(executed) - parsed
            output = output_markers(cfg["shparse"], form, lost) if lost else set()
            if output:
                record["output"] = sorted(output, key=lambda m: int(m[1:]))
                kinds.append("output")
            if lost - output:
                kinds.append("loss")
            if parsed - set(executed):
                kinds.append("strict")
        else:
            verdicts = [verdict(engine, form) for engine in cfg["engines"]]
            record["verdicts"] = verdicts
            if verdicts[0] != verdicts[1]:
                kinds.append("differ")
    except Exception as error:  # сбой разбора — находка, а не конец прогона
        record["error"] = f"{type(error).__name__}: {error}"
        kinds.append("error")
    elapsed = time.monotonic() - started
    if elapsed > SLOW:
        record["seconds"] = round(elapsed, 3)
        kinds.append("slow")
    record["kinds"] = kinds
    return record


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", choices=["detector", "parser"])
    parser.add_argument("--engine", choices=["old", "new"], default="old")
    parser.add_argument("--compare", nargs=2, choices=["old", "new"], metavar=("A", "B"))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--start", type=int, default=0, help="номер первой формы")
    parser.add_argument("--depth", type=int, default=MAX_DEPTH, help="глубина вложения команд генератора")
    parser.add_argument("--show", type=int, metavar="INDEX", help="напечатать форму INDEX и выйти (без bash)")
    parser.add_argument("--out", help="каталог для расхождений и каталогов случаев")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    if args.show is None:
        if bool(args.target) == bool(args.compare):
            parser.error("нужен ровно один режим: --target или --compare")
        if not args.out:
            parser.error("нужен --out")
    return args


def main(argv):
    args = parse_args(argv)
    if args.show is not None:
        form, count, tags = generate(args.seed, args.show, runtime=args.target != "parser", depth=args.depth)
        print(json.dumps({"form": form, "markers": count, "classes": sorted(tags)}, ensure_ascii=False))
        print(form)
        return 0
    os.makedirs(args.out, exist_ok=True)
    out = os.path.abspath(args.out)
    config = {"seed": args.seed, "target": args.target or "compare", "engine_name": args.engine,
              "compare": args.compare, "depth": args.depth}
    try:
        init_worker(dict(config))  # проверка импорта до песочницы и пула
    except ImportError as error:
        print(f"bashdiff: модуль не загружен: {error}")
        return 2
    if args.target:
        bwrap, reason = check_sandbox(out)
        if reason:
            print(f"bashdiff: {reason}")
            return 2
        config["bwrap"] = bwrap
        config["cases"] = out
    label = f"{args.target}-{args.engine}" if args.target == "detector" else (
        args.target or "compare-" + "-".join(args.compare))
    report = os.path.join(out, f"bashdiff-{label}-s{args.seed}-d{args.depth}.jsonl")
    totals = collections.Counter()
    classes = collections.Counter()
    started = time.monotonic()
    indices = range(args.start, args.start + args.count)
    with open(report, "w", encoding="utf-8") as sink, \
            multiprocessing.get_context("fork").Pool(max(1, args.jobs), init_worker, (config,)) as pool:
        for record in pool.imap_unordered(run_case, indices, chunksize=16):
            totals["forms"] += 1
            classes.update(record["classes"])
            totals["executed"] += bool(record.get("executed"))
            for kind in record["kinds"]:
                totals[kind] += 1
            if record["kinds"]:
                sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    elapsed = time.monotonic() - started
    rate = totals["forms"] / elapsed if elapsed else 0.0
    print(f"режим: {label}, зерно {args.seed}, глубина {args.depth}, формы {args.start}..{args.start + args.count - 1}")
    print(f"форм: {totals['forms']}, потерь: {totals['loss']}, строгостей: {totals['strict']}, "
          f"расхождений движков: {totals['differ']}, ошибок: {totals['error']}, медленных: {totals['slow']}, "
          f"таймаутов: {totals['timeout']}, форм в секунду: {rate:.1f}")
    if args.target:
        print(f"форм с исполненным маркером: {totals['executed']}")
    if args.target == "parser":
        print(f"вывод подстановки исполнен командой (output, не потеря): {totals['output']}")
    print("классы: " + ", ".join(f"{name} {n}" for name, n in sorted(classes.items())))
    print(f"расхождения: {report}")
    return 1 if totals["loss"] or totals["error"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
