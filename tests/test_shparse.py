import contextlib
import importlib.util
import json
import pathlib
import re
import signal
import subprocess
import sys
import time
import tracemalloc
import unittest
from unittest import mock

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import shparse  # noqa: E402
from tests.helpers import assert_linear  # noqa: E402


def cmds(text, eof=""):
    """Простые команды дерева: списки значений слов (None — слово с раскрытием)."""
    return [[w.literal() for w in s.words] for s in shparse.simple_commands(shparse.parse(text, eof))]


def runs(text, words=("npm", "i", "x")):
    """Есть ли среди простых команд дерева команда, чьи слова начинаются с words."""
    words = list(words)
    return any(c[:len(words)] == words for c in cmds(text))


def nodes(text, cls):
    return [n for n in shparse.walk(shparse.parse(text)) if type(n) is cls]


class ContractTest(unittest.TestCase):
    """Публичный контракт: parse → Script, узлы с позициями, Word.literal, walk, simple_commands, heredocs."""

    def test_script_fields(self):
        script = shparse.parse("echo a; echo b\necho c # d\n")
        self.assertIsInstance(script, shparse.Script)
        self.assertEqual(len(script.commands), 2)
        self.assertIsNone(script.error)
        self.assertEqual(script.dropped, [])
        self.assertEqual(script.comments, [(22, 25)])
        first = script.commands[0]
        self.assertIsInstance(first, shparse.Sequence)
        self.assertEqual(first.seps, [";"])
        self.assertEqual([type(i) for i in first.items], [shparse.Simple, shparse.Simple])

    def test_positions(self):
        text = "x=1 echo 'a b' \"c$d\" >out <<E\nbody\nE\n"
        script = shparse.parse(text)
        simple = script.commands[0]
        self.assertEqual(text[simple.start:simple.end], "x=1 echo 'a b' \"c$d\" >out <<E")
        self.assertEqual([text[w.start:w.end] for w in simple.assigns], ["x=1"])
        self.assertEqual([text[w.start:w.end] for w in simple.words], ["echo", "'a b'", '"c$d"'])
        self.assertEqual([(r.op, r.fd) for r in simple.redirs], [(">", None), ("<<", None)])
        doc = simple.redirs[1].target
        self.assertIsInstance(doc, shparse.Heredoc)
        self.assertEqual(text[doc.body_start:doc.body_end], "body\n")
        self.assertEqual((doc.term, doc.strip_tabs, doc.quoted), ("E", False, False))

    def test_word_literal(self):
        words = shparse.parse("a\\ b 'c d' \"e f\" $'g\\n' x$y \"$(z)\" `w` ${v} $((1)) $[2] ~/p *").commands[0].words
        self.assertEqual([w.literal() for w in words],
                         ["a b", "c d", "e f", "g\n", None, None, None, None, None, None, "~/p", "*"])

    def test_part_kinds(self):
        word = shparse.parse("a'b'\"c$x\"$'d'$y${z:-q}$(e)`f`${ g; }${| h; }$((1))$[2]").commands[0].words[0]
        kinds = [type(p).__name__ for p in word.parts]
        self.assertEqual(kinds, ["Lit", "SQ", "DQ", "AnsiC", "Param", "Param", "Sub", "Sub", "Sub", "Sub", "Arith",
                                 "Arith"])
        self.assertEqual([p.kind for p in word.parts if type(p) is shparse.Sub], ["$(", "`", "${ ", "${|"])
        self.assertEqual([type(p).__name__ for p in word.parts[2].parts], ["Lit", "Param"])

    def test_arithmetic_regions(self):
        # Индекс присваивания, индекс `${a[…]}` и смещение `${x:…}` — Arith (bash раскрывает их арифметикой); у слова
        # без `=` за `]` индекс — текст.
        text = "a[1+$(x)]=v echo ${b[2]} ${c:1:$(y)} ${d:-e} f[3]"
        arith = [text[n.start:n.end] for n in nodes(text, shparse.Arith)]
        self.assertEqual(arith, ["1+$(x)", "2", "1:$(y)"])
        simple = shparse.parse(text).commands[0]
        self.assertEqual([w.literal() for w in simple.assigns], [None])
        self.assertEqual(simple.words[-1].literal(), "f[3]")

    def test_process_substitution_kinds(self):
        subs = nodes("cat <(a) >(b) x<(c)", shparse.Sub)
        self.assertEqual([s.kind for s in subs], ["<(", ">(", "<("])

    def test_node_kinds(self):
        text = ("if a; then b; elif c; then d; else e; fi; while f; do g; done; until h; do i; done\n"
                "for j in k; do l; done; for ((m=0; m<1; m++)); do n; done; select o in p; do q; done\n"
                "case r in s|t) u;; (v) w;& esac; f1() { x; }; function f2 { y; }; coproc z; coproc C { aa; }\n"
                "(( 1 + 2 )); [[ -n bb && cc == dd ]]; (ee) | ff && ! time -p gg || hh &\n")
        kinds = {type(n).__name__ for n in shparse.walk(shparse.parse(text))}
        self.assertGreaterEqual(kinds, {"If", "While", "Until", "For", "Select", "Case", "Function", "Coproc",
                                        "ArithCmd", "Cond", "Subshell", "Group", "Pipeline", "AndOr", "Sequence"})
        case = nodes(text, shparse.Case)[0]
        self.assertEqual([([p.literal() for p in pats], term) for pats, _, term in case.items],
                         [(["s", "t"], ";;"), (["v"], ";&")])
        pipes = [(p.bang, p.time, len(p.commands)) for p in nodes(text, shparse.Pipeline)]
        self.assertEqual(pipes, [(False, None, 2), (True, "time -p", 1)])
        cond = nodes(text, shparse.Cond)[0]
        self.assertEqual([w.literal() for w in cond.words], ["-n", "bb", "cc", "==", "dd"])
        coproc = nodes(text, shparse.Coproc)
        self.assertEqual([c.name.literal() if c.name else None for c in coproc], [None, "C"])
        loops = nodes(text, shparse.For)
        self.assertEqual([w.literal() for w in loops[0].words], ["k"])
        self.assertIsNotNone(loops[1].arith)

    def test_walk_and_simple_commands_order(self):
        text = "a $(b `c`) | d <<E\n$(e)\nE\nf() { g; }"
        self.assertEqual([c[0] for c in cmds(text)], ["a", "b", "c", "d", "e", "g"])
        script = shparse.parse(text)
        all_nodes = list(shparse.walk(script))
        self.assertIs(all_nodes[0], script)
        self.assertEqual(sum(type(n) is shparse.Simple for n in all_nodes), 6)

    def test_heredocs_compat(self):
        cases = [
            ("cat <<E", [("E", False)]), ("cat <<-'E' <<\"F\" <<\\G", [("E", True), ("F", False), ("G", False)]),
            ('echo "`echo "x"` <<EOF"', []), ("((true) ) <<EOF", [("EOF", False)]), ("(( x << 2 ))", []),
            ("echo `cat <<EOF` <<END", [("END", False)]), ("x=`cat <<EOF", [("EOF", False)]),
            ("cat <<A; x=`cat <<-B", [("B", True), ("A", False)]), ("x=\"`cat <<EOF", [("EOF", False)]),
            ("x=`echo \\` <<B", [("B", False)]), ("x=`cat <<B \\`", [("B", False)]), ("x=`echo '<<B'", []),
            ("echo '`' <<A", [("A", False)]), ("echo $${a; cat <<E", [("E", False)]),
            ('echo "${ cat <<E', [("E", False)]), ("echo \\ #<<E", [("E", False)]), ("echo \\\\ #<<E", []),
            ("x=(<<E", []), ("x=( $(cat <<E) )", [("E", False)]), ("(cat <<E)", [("E", False)]),
            ("x=( <(cat <<E) )", [("E", False)]), ("cat <<E; x=( a ; b )", []), ("x=( a ) <<E", [("E", False)]),
            ("echo a\r#<<E", [("E", False)]), ("cat <<A; x=$(cat <<B", [("B", False), ("A", False)]),
            ("cat <<A; echo $(cat <<B)", [("B", False), ("A", False)]), ("cat <<<E", []), ("echo '<<E'", []),
            ("cat <<'E F'", [("E F", False)]), ("cat <<E$'x'", [("Ex", False)]),
        ]
        for line, want in cases:
            with self.subTest(line):
                self.assertEqual(shparse.heredocs(line), want)


def _bashdiff():
    """Генератор форм фаззера tests/tools/bashdiff.py (скрипт разработки без пакета): формы только разбираются."""
    path = pathlib.Path(__file__).parent / "tools" / "bashdiff.py"
    spec = importlib.util.spec_from_file_location("bashdiff", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def out_of_bounds(text, script=None):
    """Узлы дерева text, чьи позиции не в 0 ≤ start ≤ end ≤ len(text): (вид, start, end)."""
    script = shparse.parse(text) if script is None else script
    return [(type(n).__name__, n.start, n.end) for n in shparse.walk(script)
            if not 0 <= n.start <= n.end <= len(text)]


class PositionsTest(unittest.TestCase):
    """Позиции каждого узла — в исходном тексте: 0 ≤ start ≤ end ≤ len(text). Детектор и разбор комментариев режут
    текст по ним (depcheck._glob_marks, _part_text); позиция за концом текста роняла детектор IndexError, и хук
    пропускал команду."""

    def test_reexpanded_word(self):
        # Слово, раскрытое заново по напечатанному тексту (`for a do` печатается `for a in "$@"; do`): позиции
        # частей — по узлам, а не одним сдвигом от начала слова.
        text = "echo $(for a do a=(\\\\);done)"
        self.assertEqual(out_of_bounds(text), [])
        lits = [n for n in shparse.walk(shparse.parse(text)) if type(n) is shparse.Lit and n.text == "a=(\\)"]
        self.assertEqual([(n.start, n.end) for n in lits], [(text.index("a=("), text.index(";done"))])

    def test_heredoc_body_at_end(self):
        # Тело, склеенное `\` с переводом строки до конца ввода, кончается дописанным переводом строки.
        for text in [":  <<E1\nx \\\nE1", ": <<E\na $(echo b\nE\\\nE"]:
            with self.subTest(text):
                self.assertEqual(out_of_bounds(text), [])

    def test_trailing_backslash(self):
        # Нечётный ряд `\` в конце ввода: shell_getc дописывает ещё `\`, и последний `\` текста — литерал; его
        # позиция — последний символ текста (строка `eval` формы фаззера: зерно 7, номер 4138).
        text = "! true \\\n\\ \\\n\\"
        self.assertEqual(out_of_bounds(text), [])
        self.assertEqual(shparse.parse(text).commands[0].commands[0].words[-1].literal(), " \\")

    def test_corpora(self):
        root = pathlib.Path(__file__).parent / "fixtures"
        for path in sorted(root.glob("bash-*.jsonl")):
            with open(path, encoding="utf-8") as fh:
                commands = [json.loads(line).get("command") for line in fh if line.strip()]
            for number, text in enumerate(commands, 1):
                if isinstance(text, str):
                    bad = out_of_bounds(text)
                    if bad:
                        with self.subTest(file=path.name, line=number):
                            self.fail(f"{bad[:3]}: {text[:200]!r}")

    def test_node_text_in_reparsed_text(self):
        # Повторные разборы напечатанного текста (`$((…) …)`, `<(…)` в `${…}` в арифметике и в "…", слово, раскрытое
        # заново) переводят позиции по отметкам печати и посимвольно между ними, а не одним сдвигом: печать
        # `$'\x41'` — `'A'`, `\` с переводом строки снят, и сдвиг уводил позиции следующих узлов на чужие символы.
        for text in ["echo $((true \\\n$'a'${HOME%x}) )", "echo $((echo $'\\x41\\x42' \"$y\" ${z:-w}) )",
                     "echo $(( ${x/<(echo $'\\x41' $y)/z} ))", "echo $(( ${x/<(echo \\\n$y)/z} ))",
                     "echo \"${x:-<(echo $'\\x41' $y)}\"", "x=$(a=(\\'y) ; echo $'\\x41' \"$v\" ; b=(\\'))",
                     "echo `echo $(( '\\$(echo a)' + $x ))`", "cat <<E\n$(( '$(echo a)' + 1 ))\nE",
                     "echo $(( ${| case a in b) :;& a) echo;; esac; } 1 ))"]:
            with self.subTest(text):
                self.assertEqual(text_mismatches(text), [])

    def test_rewritten_text(self):
        # Текст, у которого нет посимвольного соответствия в исходном (Script.rewritten): значение `$'…'` в
        # арифметике (раскрывается как '…': `` `…` `` в нём исполняется) — узлы в нём указывают на весь `$'…'`;
        # заголовок heredoc, ждущего тела снова, печатается с новым терминатором — на исходное слово `E`.
        text = "echo $(( $'`npm i x`' ))"
        script = shparse.parse(text)
        self.assertEqual(script.rewritten, [(9, 21)])
        sub = [n for n in shparse.walk(script) if type(n) is shparse.Sub][0]
        self.assertEqual((sub.start, sub.end, sub.kind), (9, 21, "`"))
        self.assertTrue(runs(text))
        text = "(($(<<E|a=($(npm i x)\nt)\"${ '''';}\nE)))"
        script = shparse.parse(text)
        self.assertIn((6, 7), script.rewritten)
        self.assertEqual(text_mismatches(text, script), [])
        # Незакрытая при раскрытии "…" — до конца слова.
        text = 'echo $(( # "x\n1) )"\n\'$(npm i x)\'\n))'
        dq = [n for n in shparse.walk(shparse.parse(text)) if type(n) is shparse.DQ][0]
        self.assertEqual((text[dq.start], dq.end), ('"', len(text)))

    def test_text_of_words_and_literals(self):
        # Текст по позициям простой команды, слова и Lit — их текст: Lit перед `$имя` кончался на `$` (`D=$`).
        for text in ["D=$S/x", "echo a$b", 'echo "a$1"', "cat <<E\n${HOME/${| echo $'a\nnpm i x'; }/x}\nE"]:
            with self.subTest(text):
                self.assertEqual(text_mismatches(text), [])
        lits = [(n.start, n.end) for n in nodes("D=$S/x", shparse.Lit)]
        self.assertEqual(lits, [(0, 2), (4, 6)])
        # Комментарий в скобках присваивания массива bash отбрасывает: Lit не лежит поверх него.
        text = 'a=(npm # c\n i lodash); "${a[@]}"'
        self.assertEqual(text_mismatches(text), [])
        self.assertEqual([(n.start, n.end) for n in nodes(text, shparse.Lit)][:2], [(0, 6), (12, 21)])
        # Перед первым словом и за последним — так же.
        for text, lits in [("a=(# c\n x)", [(0, 3), (8, 10)]), ("a=(x # c\n)", [(0, 4), (9, 10)])]:
            with self.subTest(text):
                self.assertEqual(text_mismatches(text), [])
                self.assertEqual([(n.start, n.end) for n in nodes(text, shparse.Lit)], lits)

    def test_array_close_at_end(self):
        # `)` скобок массива с `\\` и переводом строки за ней в конце ввода: позиция `)` — её место, а не конец ввода
        # (чтение заглянуло за неё): Lit кончался за концом текста.
        for text in ["a=(x)\\\n", "a=($'x')\\\n", "a=('x')\\\n"]:
            with self.subTest(text):
                self.assertEqual(out_of_bounds(text), [])
                self.assertEqual(text_mismatches(text), [])

    def test_substitution_after_printed_newline(self):
        # Печать `case` ставит перевод строки за `)` подстановки, которого нет в исходном тексте: он переводится не
        # левее конца подстановки, и `)` остаётся в ней.
        text = "x=$(cat <<E; true; case a in b) :;; *) v=$(npm i x);; esac\nE\n)"
        self.assertEqual(text_mismatches(text), [])
        self.assertIn((41, 51), [(n.start, n.end) for n in nodes(text, shparse.Sub)])

    def test_heredoc_dolbrace_ansi_positions(self):
        # Значение `$'…'` в слове `${имя OP …}` тела heredoc — переписанный кусок: команды из него начинаются в его
        # начале и кончаются в его конце (на куски печати позиции не делятся).
        text = "cat <<E\n${HOME/${| echo $'a\nnpm i x'; }/x}\nE"
        script = shparse.parse(text)
        self.assertIn((24, 36), script.rewritten)
        found = [text[c.start:c.end] for c in shparse.simple_commands(script)]
        self.assertEqual(found, ["cat <<E", "echo $'a\nnpm i x'", "$'a\nnpm i x'"])

    def test_backquote_body_in_rewritten(self):
        # Тело `` `…` ``, лежащее в переписанном куске (зерно 23, без исполнения, номер 2330): узлы, начатые в нём,
        # кончаются в его конце, а не на символе за его началом.
        bashdiff = _bashdiff()
        text = bashdiff.generate(23, 2330, runtime=False)[0]
        script = shparse.parse(text)
        spans = script.rewritten
        bad = [(type(n).__name__, n.start, n.end) for n in shparse.walk(script)
               for a, b in spans if n.start == a < n.end < b]
        self.assertEqual(bad, [])
        self.assertEqual(text_mismatches(text, script), [])

    def test_empty_arith_quote(self):
        # Пустая '' в арифметике: перевод её позиций — пустой список кусков (разбор падал IndexError, ошибка
        # INTERNAL; зерно 2, глубина 2, номер 9939).
        for text in ["echo $(( '' 1 ))", "echo ${u1[$((a\nb ) |& : $(npm i x)\ncat <<E2'' | cat\nE2)0]}"]:
            with self.subTest(text):
                self.assertIsNone(shparse.parse(text).error)
        self.assertTrue(runs("echo ${u1[$((a\nb ) |& : $(npm i x)\ncat <<E2'' | cat\nE2)0]}"))

    def test_node_text_corpora(self):
        root = pathlib.Path(__file__).parent / "fixtures"
        for path in sorted(root.glob("bash-*.jsonl")):
            with open(path, encoding="utf-8") as fh:
                commands = [json.loads(line).get("command") for line in fh if line.strip()]
            for number, text in enumerate(commands, 1):
                if isinstance(text, str):
                    bad = text_mismatches(text)
                    if bad:
                        with self.subTest(file=path.name, line=number):
                            self.fail(f"{bad[:3]}: {text[:200]!r}")

    def test_fuzz_forms(self):
        # Зёрна 7 и 13, обе разновидности генератора: здесь находились позиции за концом текста (зерно 13, номера
        # 2682, 3208; зерно 7 без исполнения, номер 1465) и конец раньше начала (зерно 13, номер 1649). Текст каждого
        # узла по позициям — его текст (text_mismatches): до перевода позиций по кускам нарушали 178 форм.
        bashdiff = _bashdiff()
        for seed in (7, 13):
            for runtime in (True, False):
                for index in range(4000):
                    text = bashdiff.generate(seed, index, runtime=runtime)[0]
                    script = shparse.parse(text)
                    bad = out_of_bounds(text, script) + text_mismatches(text, script)
                    if bad:
                        with self.subTest(seed=seed, runtime=runtime, index=index):
                            self.fail(f"{bad[:3]}: {text[:200]!r}")


# Знаки начала и конца подстановки по виду (Sub.kind) — в исходном тексте по позициям узла.
SUB_MARKS = {"$(": ("$(", ")"), "`": ("`", "`"), "${ ": ("${", "}"), "${|": ("${", "}"), "<(": ("<(", ")"),
             ">(": (">(", ")")}
BACKQUOTE_ESCAPE = re.compile(r'\\([\\`$"])')


def unescaped(fragment):
    """Варианты исходного текста, как его видит разбор: без `\\` с переводом строки и без `\\` перед `\\`, `` ` ``,
    `$`, `"` по уровню на каждую вложенную `` `…` `` (её тело bash читает без одного уровня), в любом порядке."""
    seen = {fragment}
    todo = [fragment]
    while todo and len(seen) < 64:
        cur = todo.pop()
        for nxt in (cur.replace("\\\n", ""), BACKQUOTE_ESCAPE.sub(r"\1", cur)):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def _squeezed(text):
    """Текст без пробелов, табуляций и переводов строки."""
    return "".join(text.split())


def text_mismatches(text, script=None):
    """Узлы, чей текст по позициям не их текст в дереве: подстановка — от своего знака начала до знака конца,
    параметр — с `$` (`${…}` — до `}`, `$x` — сам текст), '…' — её текст в кавычках, "…" — от `"` до `"` (без
    закрывающей — до конца, где её нет: слово, раскрытое заново, берёт незакрытую "…" до конца), $'…' — с `$'` или `'`
    (печать значения). Текст, который bash разбирает в печати, не идущей за исходным посимвольно (Script.rewritten:
    значение `$'…'`, заголовок heredoc с новым терминатором, `in "$@"` у `for`), определён так: узел, начатый в таком
    куске, начинается в его начале, кончающийся в нём — кончается в его конце; знак у такой границы не сверяется.
    Подстановка с ошибкой в теле (не разобрана) не проверяется: её конец — конец текста. Простая команда и слово — от
    начала первой части до конца последней (между ними и краем — только пробелы; у слова — `\\`, кавычки и `$`
    пустых "", '', `$'`), Lit — его текст (_lit_texts). (вид, начало, конец, текст по позициям)."""
    out = []
    script = shparse.parse(text) if script is None else script
    starts = {span[0] for span in script.rewritten or ()}
    ends = {span[1] for span in script.rewritten or ()}
    skip, bodies = _lit_exempt(script)
    for node in shparse.walk(script):
        kind = type(node)
        frag = text[node.start:node.end]
        head = tail = body = True
        if kind is shparse.Lit:
            if id(node) in skip or any(a < node.end and node.start < b for a, b in bodies):
                continue
            body = _norm(node.text) in _lit_texts(frag) or any(node.start < a < node.end for a in starts)
        elif kind in (shparse.Word, shparse.Simple):
            kids = node.parts if kind is shparse.Word else node.assigns + node.words + node.redirs
            if not kids or id(node) in skip or any(_failed(k) for k in kids):
                continue
            first, last = min(k.start for k in kids), max(k.end for k in kids)
            edge = "\\'\"$" if kind is shparse.Word else " \t\n"
            # Тело heredoc (с переводом строки перед ним), прочитанное за закрывающей скобкой подстановки, — в слове
            # вокруг неё.
            gaps = [(node.start, first), (last, node.end)]
            fine = [_blank(text[a:b], edge) or any(x - 1 <= a and b <= y for x, y in bodies) for a, b in gaps]
            head = node.start <= first and (first in starts or fine[0])
            tail = last <= node.end and (last in ends or fine[1])
            if kind is shparse.Simple:
                # Слова команды — по порядку текста: слово печати, переведённое не на своё место (в переписанный
                # кусок за телом heredoc), стоит правее следующего.
                order = [w.start for w in node.assigns + node.words]
                if order != sorted(order):
                    out.append((kind.__name__, node.start, node.end, frag[:40]))
                    continue
        elif kind is shparse.Sub:
            if node.body.error is not None and not node.body.commands:
                continue
            first, last = SUB_MARKS[node.kind]
            head = frag.startswith(first)
            tail = frag.endswith(last) or node.kind == "`"
        elif kind is shparse.Param:
            head = frag.startswith("$")
            if node.text.startswith("${"):
                tail = frag.endswith("}")
            else:
                body = node.text in unescaped(frag)
        elif kind is shparse.SQ:
            head = frag.startswith("$'") or frag.startswith("'")
            # Текст '…' в напечатанном тексте — печать (`$( : a)` — `$(: a)`): сверка без пробелов.
            body = (frag.startswith("$'") or "'" + node.text + "'" in unescaped(frag)
                    or _squeezed("'" + node.text + "'") in {_squeezed(v) for v in unescaped(frag)})
        elif kind is shparse.DQ:
            head = frag[:1] == '"' or frag[:2] == '$"'
            tail = frag.endswith('"') or (head and shparse._skip_double_quoted(frag, frag.index('"') + 1) >= len(frag))
        elif kind is shparse.AnsiC:
            head = frag[:2] == "$'" or frag[:1] == "'"
        if node.start == node.end:
            # Последнюю лексему узла bash прочитал левее первой (отложенные куски ввода): конец — его начало.
            continue
        at_start, at_end = node.start in starts, node.end in ends
        if not ((head or at_start) and (tail or at_end) and (body or at_start or at_end)):
            out.append((kind.__name__, node.start, node.end, frag[:40]))
    return out


# '…' с подстановкой (с `$'…'`): в арифметике и в слове `${…}` в "…" её текст разбирается отдельно, в Lit его нет.
SQ_WITH_SUB = re.compile(r"\$?'[^']*(?:\$\(|`)[^']*'")
# Текст печати подстановки (print_comsub) в Lit: слово `${x:-<(…)}`, разобранное заново по печати.
PRINTED_SUB = re.compile(r"[$<>]\(|\$\{[ |\t\n]|`")


def _norm(text):
    """Текст без пробелов, кавычек и `\\`: Lit хранит значение (кавычки и экранирование сняты)."""
    return "".join(c for c in "".join(text.split()) if c not in "'\"\\")


def _lit_texts(frag):
    """Тексты Lit, которые дают исходные позиции frag: варианты unescaped, без '…' с подстановкой (SQ_WITH_SUB) и без
    одной внутренней строки (терминатор heredoc, прочитанный внутри слова `a=(…)`)."""
    out = set()
    for v in unescaped(frag):
        out.add(_norm(v))
        out.add(_norm(SQ_WITH_SUB.sub("", v)))
    lines = frag.split("\n")
    if len(lines) < 64:
        out |= {_norm("\n".join(lines[:k] + lines[k + 1:])) for k in range(1, len(lines) - 1)}
    return out


def _lit_exempt(script):
    """Lit, слова и команды, чей текст — печать, а не исходный текст: в `${…}` и арифметике с подстановкой (разбор
    по печати); Lit с печатью подстановки в тексте; диапазоны тел heredoc с их терминаторами."""
    skip, bodies = set(), []
    for node in shparse.walk(script):
        kind = type(node)
        if kind is shparse.Heredoc:
            bodies.append((node.body_start, max(node.body_end, getattr(node, "term_end", None) or 0)))
        elif kind in (shparse.Param, shparse.Arith):
            inner = list(shparse.walk(node))
            if any(type(n) is shparse.Sub and n.kind in ("<(", ">(", "$(") for n in inner):
                skip.update(id(n) for n in inner if type(n) in (shparse.Lit, shparse.Word, shparse.Simple))
        elif kind is shparse.Lit and PRINTED_SUB.search(node.text):
            skip.add(id(node))
    return skip, bodies


def _failed(node):
    """Подстановка, тело которой не разобрано: её позиции — место ошибки и конец текста."""
    return type(node) is shparse.Sub and node.body.error is not None and not node.body.commands


def _blank(gap, edge):
    """Между краем узла и его частью — только знаки edge и `\\` с переводом строки."""
    return not gap.replace("\\\n", "").strip(edge)


# Вложенность на 10⁵ уровней каждого вида: (начало, открытие, середина, закрытие).
DEEP_FORMS = {
    "param": ("echo ", "${a:-", "x", "}"),
    "arith": ("echo ", "$(( ", "1", " ))"),
    "subscript": ("echo ", "${a[", "1", "]}"),
    "comsub": ("echo ", "$(", "a", ")"),
    "comsub_dq": ("echo ", '"$(', "a", ')"'),
    "funsub": ("echo ", "${ ", "a", "; }"),
    "procsub": ("cat ", "<(", "a", ")"),
    "subshell": ("", "( ", "a", " )"),
    "group": ("", "{ ", "a", "; }"),
    "if": ("", "if a; then ", "b", "; fi"),
    "case": ("", "case a in a) ", "b", " ;; esac"),
    "cond": ("[[ ", "( ", "a", " )"),
}

# Разбор в отдельном процессе: segfault CPython не роняет тест-раннер. Печатает вид ошибки разбора.
DEEP_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
import shparse
head, opening, middle, closing = sys.argv[2:6]
n = int(sys.argv[6])
script = shparse.parse(head + opening * n + middle + closing * n + (" ]]" if head == "[[ " else ""))
print(script.error.kind if script.error is not None else None)
"""


class DepthLimitTest(unittest.TestCase):
    """Стек разбора глубже shparse._STACK_MAX — ошибка вида DEPTH: дерево не строится. Без предела вложенность
    10⁴–10⁵ уровней съедала память и роняла CPython segfault (код 139 без ответа хука — команда исполнялась)."""

    def test_deep_forms_in_subprocess(self):
        for name, form in DEEP_FORMS.items():
            with self.subTest(name):
                done = subprocess.run([sys.executable, "-c", DEEP_SCRIPT, str(PLANKA_DIR), *form, "100000"],
                                      capture_output=True, text=True, timeout=300)
                self.assertEqual((done.returncode, done.stdout.strip()), (0, shparse.DEPTH), done.stderr[-500:])

    def test_error_and_empty_tree(self):
        text = "npm i x; echo " + "$(" * 10000 + "a" + ")" * 10000
        script = shparse.parse(text)
        self.assertEqual((script.error.fatal, script.error.kind), (True, shparse.DEPTH))
        self.assertEqual(script.commands, [])
        self.assertEqual(shparse.heredocs("cat <<E " + "$(" * 10000), [])

    def test_real_inputs_far_below_limit(self):
        # Корпуса и формы фаззера не доходят и до 100 уровней стека: предел 4000 — с запасом в 40 раз.
        root = pathlib.Path(__file__).parent / "fixtures"
        texts = []
        for path in sorted(root.glob("bash-*.jsonl")):
            with open(path, encoding="utf-8") as fh:
                texts += [json.loads(line).get("command") for line in fh if line.strip()]
        bashdiff = _bashdiff()
        texts += [bashdiff.generate(seed, index)[0] for seed in (7, 13) for index in range(2000)]
        with mock.patch.object(shparse, "_STACK_MAX", 100):
            deep = [text[:100] for text in texts if isinstance(text, str)
                    and getattr(shparse.parse(text).error, "kind", None) == shparse.DEPTH]
        self.assertEqual(deep, [])

    def test_tree_depth_within_limit(self):
        # Дерево, построенное до предела, — не глубже двух пределов: у вложенных видов узлов на уровень не больше
        # 1,5 уровня стека (heredoc в `$((…) …)`), в том числе у повторных разборов — они идут тем же стеком.
        limit = 400
        kinds = {**LinearityTest.KINDS, **LinearityTest.HEREDOCS}
        kinds.update({"reparse_" + name: (lambda wrap: lambda n: chain(wrap, n))(wrap)
                      for name, wrap in ReparseLinearityTest.KINDS.items()})
        with mock.patch.object(shparse, "_STACK_MAX", limit):
            for name, make in kinds.items():
                with self.subTest(name):
                    for n in range(1, limit, 7):
                        script = shparse.parse(make(n))
                        self.assertLessEqual(tree_depth(script), 2 * limit)
                        if script.error is not None and script.error.kind == shparse.DEPTH:
                            break


def tree_depth(node):
    """Глубина дерева узлов без рекурсии; узел, взятый готовым в нескольких местах, считается один раз."""
    depth = {}
    stack = [(node, False)]
    while stack:
        cur, done = stack.pop()
        if done:
            depth[id(cur)] = 1 + max((depth[id(c)] for c in cur.children()), default=0)
        elif id(cur) not in depth:
            stack.append((cur, True))
            stack.extend((c, False) for c in cur.children() if id(c) not in depth)
    return depth[id(node)]


class InputClassesTest(unittest.TestCase):
    """Классы входов плана: пустой, пробелы, комментарии, не-UTF-8 (суррогаты), `\\0`, огромный вход."""

    def test_empty_and_blank(self):
        for text in ["", " ", "\t\t", "\n\n\n", " \n \t\n", "\\\n", ";", "#", "# x\n# y"]:
            with self.subTest(text):
                script = shparse.parse(text)
                self.assertEqual(shparse.simple_commands(script), [])
        self.assertIsNone(shparse.parse("").error)
        self.assertIsNone(shparse.parse("  \n# c\n").error)
        self.assertEqual(shparse.parse("# a\n  # b").comments, [(0, 3), (6, 9)])
        self.assertEqual(shparse.parse(";").error, shparse.SyntaxIssue(0, True))

    def test_any_string(self):
        for text in ["\ud800echo \udfff", "echo \x00a", "\x00", "\ud83d\ude00", "echo \u00e9\u4e2d", "\x1b[0m",
                     "'", '"', "`", "$(", "${", "$((", "<<", "(((", "))))", "\\", "a\\", "a\\\\", "\r\n",
                     "\x00\n<<E\x00\nE\x00", "cat <<\ud800\nx\n\ud800"]:
            with self.subTest(repr(text)):
                script = shparse.parse(text)
                list(shparse.walk(script))
                shparse.simple_commands(script)
                shparse.heredocs(text)
        self.assertEqual(cmds("echo \ud800x"), [["echo", "\ud800x"]])

    def test_non_string(self):
        self.assertEqual(shparse.heredocs(None), [])
        self.assertIsInstance(shparse.parse(b"echo"), shparse.Script)

    def test_huge(self):
        text = "echo $(printf 'a b' \"$x\") | cat <<E\nbody $(c)\nE\n" * 3000
        script = shparse.parse(text)
        self.assertEqual(len(script.commands), 3000)
        self.assertEqual(len(shparse.simple_commands(script)), 12000)


class QuotingTest(unittest.TestCase):
    """Кавычки, экранирование, $'…', `\\` с переводом строки (shell_getc, read_token_word, parse_matched_pair)."""

    def test_values(self):
        self.assertEqual(cmds("echo 'a\"b' \"c'd\" a\\'b \"e\\\"f\" \"g\\$h\" \"i\\j\""),
                         [["echo", 'a"b', "c'd", "a'b", 'e"f', "g$h", "i\\j"]])
        self.assertEqual(cmds("echo $'a\\'b' $'\\x41\\101\\u00e9\\t'"), [["echo", "a'b", "AA\u00e9\t"]])
        self.assertEqual(cmds("echo $\"a\""), [["echo", "a"]])

    def test_line_continuation(self):
        # `\\` с переводом строки снимается вне '…' и склеивает слово (np\\ ⏎ m — npm).
        self.assertEqual(cmds("np\\\nm i x"), [["npm", "i", "x"]])
        self.assertEqual(cmds("echo 'a\\\nb' \"c\\\nd\""), [["echo", "a\\\nb", "cd"]])
        self.assertEqual(cmds("echo a \\\n; echo b"), [["echo", "a"], ["echo", "b"]])
        self.assertEqual(cmds("echo a\\\\\necho b"), [["echo", "a\\"], ["echo", "b"]])
        # `\\` в конце текста: shell_getc дописывает ещё `\\` вместо перевода строки.
        self.assertEqual(cmds("echo a\\"), [["echo", "a\\"]])

    def test_comments(self):
        # `#` — комментарий только в начале лексемы: за пробелом, `;`, `&`, `|`, `(`; `\\ #` — часть слова.
        self.assertTrue(runs("true;#'\nnpm i x"))
        self.assertTrue(runs("(#)\nnpm i x)"))
        self.assertTrue(runs("echo \\ #$(npm i x)"))
        self.assertFalse(runs("echo \\\\ #$(npm i x)"))
        self.assertTrue(runs("echo a#b $(npm i x)"))
        self.assertTrue(runs("echo a\r#$(npm i x)"))
        self.assertTrue(runs("echo $(echo \\ #; npm i x)"))
        self.assertTrue(runs("(( 1 #x )); npm i x"))
        self.assertEqual(shparse.parse("a # b\nc #d").comments, [(2, 5), (8, 10)])
        self.assertEqual(shparse.parse("echo $(a #b\n)").comments, [(9, 11)])
        self.assertEqual(shparse.parse("echo `a #b`").comments, [(8, 10)])
        self.assertEqual(shparse.parse("echo $((a #b\n) )").comments, [(10, 12)])

    def test_word_separators(self):
        # Разделители слов — пробел, таб, перевод строки; `\\r`, `\\v`, неразрывный пробел — часть слова.
        self.assertEqual(cmds("X=1\recho a"), [["a"]])
        self.assertEqual(shparse.parse("X=1\recho a").commands[0].assigns[0].literal(), "X=1\recho")
        self.assertEqual(cmds("echo a\xa0b\x0bc"), [["echo", "a\xa0b\x0bc"]])

    def test_double_dollar(self):
        # `$$` — параметр целиком: `$${` и `"$$(…)"` не открывают подстановку.
        self.assertTrue(runs("x=$${ npm i x }", ("npm", "i", "x")))
        self.assertFalse(runs('echo "$$(npm i x)"'))
        self.assertEqual(cmds("echo $$'\\' \"$(npm i x)\""), [["echo", None, None], ["npm", "i", "x"]])
        self.assertFalse(runs("echo $(( '$$(npm i x)' ))"))


class SubstitutionTest(unittest.TestCase):
    """Подстановки: вид, тело, вложенность, где bash исполняет тело."""

    def test_bodies_run(self):
        for text in ["echo $(npm i x)", "echo \"$(npm i x)\"", "echo `npm i x`", "echo \"`npm i x`\"",
                     "echo ${ npm i x; }", "echo ${| npm i x; }", "echo \"${ npm i x; }\"", "cat <(npm i x)",
                     "tee >(npm i x)", "echo ${y:-$(npm i x)}", "echo \"${y:-`npm i x`}\"",
                     "echo $(( $(npm i x) ))", "echo $[ `npm i x` ]", "(( $(npm i x) ))",
                     "for (( i=$(npm i x); 0; )); do :; done", "a[$(npm i x)]=1", "echo ${a[$(npm i x)]}",
                     "echo ${y:-<(npm i x)}", "[[ $(npm i x) ]]", "case $(npm i x) in a) ;; esac",
                     "case a in $(npm i x)) ;; esac", "cat <<< $(npm i x)", "echo >$(npm i x)",
                     "x=( $(npm i x) )", "echo $(echo $(echo $(npm i x)))", "echo ${x:-${y:-$(npm i x)}}",
                     "echo $(case a in *) npm i x;; esac)", "echo $(true)${u-$(npm i x)}",
                     "echo $(true)$[ $(npm i x) ]", "echo ${x:-{a,b}$(npm i x)}"]:
            with self.subTest(text):
                self.assertTrue(runs(text))

    def test_bodies_data(self):
        for text in ["echo '$(npm i x)'", "echo \\$(npm i x)", "echo \"\\$(npm i x)\"", "echo '`npm i x`'",
                     "echo \"${y:-<(npm i x)}\"", "echo $(( <(npm i x) ))", "echo \"<(npm i x)\"",
                     "cat <<'E'\n$(npm i x)\nE", "cat <<\\E\n`npm i x`\nE", "echo \"$$(npm i x)\"",
                     "echo # $(npm i x)", "git commit -m \"fix `echo \"a; npm i x\"` text\""]:
            with self.subTest(text):
                self.assertFalse(runs(text))

    def test_process_substitution_in_pattern(self):
        # В образце `"${x#…}"`, `"${x%…}"`, `"${x/…}"` bash 5.3 исполняет процесс-подстановку, в значении
        # `"${x:-…}"` — нет; внутри образца вложенная `${…}` раскрывается не как в "…".
        for text in ['echo "${HOME#<(npm i x)}"', 'echo "${HOME%%<(npm i x)}"', 'echo "${HOME/<(npm i x)/y}"',
                     'echo "${HOME//x/${u=a<(npm i x)}}"', 'echo "${HOME#${u:-${v:-<(npm i x)}}}"',
                     'echo "${u:-${HOME#<(npm i x)}}"', "cat <<E\n${HOME#<(npm i x)}\nE",
                     'echo "$(: ${HOME:+${u:-<(npm i x)}} )"', "echo $(( ${HOME#<(npm i x)} ))"]:
            with self.subTest(text):
                self.assertTrue(runs(text))
        for text in ['echo "${u:-<(npm i x)}"', 'echo "${u=a<(npm i x)}"', 'echo "${HOME#"${u=<(npm i x)}"}"',
                     "cat <<E\n${u:-<(npm i x)}\nE", "echo $(( ${u:-<(npm i x)} ))"]:
            with self.subTest(text):
                self.assertFalse(runs(text))
        # Конец `<((…` в `${…}` при раскрытии — конец разбора (xparse_dolparen), а не счёт скобок при чтении.
        self.assertTrue(runs("echo \"${HOME/<(((case a in\n*) npm i x\n;;\nesac\n) ))/y}\""))
        self.assertTrue(shparse.parse(": <(((case a in\n*) npm i x\n;;\nesac\n) ))y").error.fatal)
        # Текст не исполненной процесс-подстановки раскрывается, как текст в "…": `$(…)` в нём исполняется.
        self.assertTrue(runs('echo "${u:-<(case a in $(npm i x)|a) ;; esac)}"'))
        self.assertFalse(runs('echo "${u:-<(case a in a) npm i x;; esac)}"'))

    def test_arithmetic_quotes(self):
        # Арифметику bash раскрывает, как текст в "…": `'…'` в ней — не кавычки, подстановки в ней исполняются;
        # так же индекс присваивания, индекс `${a[…]}` и смещение `${x:…}`.
        for text in ["echo $(( '$(npm i x)' ))", "echo \"$(( '$(npm i x)' ))\"", "(( '$(npm i x)' ))",
                     "for (( i='$(npm i x)'; 0; )); do :; done", "echo $[ '`npm i x`' ]",
                     "a['$(npm i x)']=1", "a['$(npm i x)']+=1", "declare b['$(npm i x)']=1",
                     "typeset b['$(npm i x)']=1", "a=(['$(npm i x)']=1)", "echo ${a['$(npm i x)']}",
                     "echo ${x:'$(npm i x)'}", "echo ${x: '$(npm i x)'}", "echo ${x:1:'$(npm i x)'}",
                     "echo \"${a['$(npm i x)']}\"", "echo $(( '))' + '$(npm i x)' ))",
                     "(((echo $'\\n$(\\\\'|( y); npm i x ) ))"]:
            with self.subTest(text):
                self.assertTrue(runs(text))
        for text in ["a['$(npm i x)' \"y\"]", "echo ${x:-'$(npm i x)'}", "export b['$(npm i x)']=1",
                     "readonly b['$(npm i x)']=1", "echo a['$(npm i x)']=1", "a[1]='$(npm i x)'"]:
            with self.subTest(text):
                self.assertFalse(runs(text))

    def test_arithmetic_or_command(self):
        # `$((…) …)`: арифметика, только если текст после `$(` — `(…)` со сбалансированными скобками
        # (chk_arithsub: '…', "…" пропускаются, `` `…` `` — нет); иначе подстановка команды с подоболочкой.
        for text in ["echo $((npm i x) )", "echo \"$((npm i x) )\"", "echo $((true\nnpm i x) )",
                     "echo \"$((echo '))' ) ; npm i x)\"", "echo $(( (1) `: ;npm i x` ))"]:
            with self.subTest(text):
                self.assertTrue(runs(text))
        arith = nodes("echo $(( (1) + 2 ))", shparse.Arith)
        self.assertEqual(len(arith), 1)
        self.assertEqual(nodes("echo $(( (1) + 2 ))", shparse.Sub), [])

    def test_dparen_subshell(self):
        # `((…) …)` в позиции команды — подоболочка: текст возвращается во ввод со второй `(` (parse_dparen).
        self.assertEqual(cmds("((npm i x) )"), [["npm", "i", "x"]])
        self.assertEqual(cmds("(((npm i x) ) )"), [["npm", "i", "x"]])
        self.assertEqual(cmds("((npm i x)) "), [])
        self.assertEqual(cmds("((true) ) <<E\nx\nE\necho y"), [["true"], ["echo", "y"]])

    def test_backquote(self):
        # `` `…` `` кончается на первой неэкранированной обратной кавычке; `\\` снимается перед `\\`, `` ` ``, `$`
        # (прямо в "…" — и перед `"`); тело bash разбирает при раскрытии.
        self.assertTrue(runs("echo `'` && npm i x"))
        self.assertTrue(runs("echo `echo a #b`; npm i x"))
        self.assertTrue(runs("echo `echo \\`npm i x\\``"))
        self.assertTrue(runs("echo \"a `echo 'it\"s'` c\"; npm i x"))
        self.assertEqual(cmds("echo \"a `echo \\\"b; c\\\"` d\""), [["echo", None], ["echo", "b; c"]])
        self.assertEqual(cmds("echo \"${u:-`echo \\\" `}\""), [["echo", None], ["echo", '"']])
        script = shparse.parse("echo \"a `echo '`'` b\"")
        self.assertTrue(script.error.fatal)
        # Ошибка в теле `` `…` `` — его ошибка: bash разбирает тело при раскрытии, команда исполняется.
        script = shparse.parse("echo `if`; npm i x")
        self.assertIsNone(script.error)
        sub = nodes("echo `if`; npm i x", shparse.Sub)[0]
        self.assertTrue(sub.body.error.fatal)
        self.assertTrue(runs("echo `if`; npm i x"))

    def test_backquote_positions(self):
        text = "echo `printf \\`a\\` \\$x`"
        inner = [s for s in shparse.simple_commands(shparse.parse(text)) if s.words[0].literal() == "a"][0]
        self.assertEqual(text[inner.start:inner.end], "a")

    def test_funsub_end(self):
        # `}` кончает `${ …; }` там, где bash принимает зарезервированное слово; `{` там же открывает группу.
        for text in ['echo "${ echo }; npm i x; }"', 'echo "${ { echo a; }; npm i x; }"', "x=${ a; }; npm i x",
                     'echo "${ (echo a) }"; npm i x', 'echo "${ echo a; }}"; npm i x', 'echo "${ echo "}"; npm i x; }"',
                     'echo "${ echo \\}; npm i x; }"', 'echo "${ [[ a ]]\n}"; npm i x', "echo ${\nnpm i x\n}",
                     'echo "${ function f { echo; }; npm i x; }"', 'echo "${ cat <<E\n}\nE\nnpm i x; }"']:
            with self.subTest(text):
                self.assertTrue(runs(text))
        self.assertFalse(runs('echo "${ echo a; } npm i x"'))
        self.assertFalse(runs("echo ${ echo a; } npm i x"))
        self.assertFalse(runs("echo '${ npm i x; }'"))

    def test_regex_and_extglob_groups(self):
        # `(…)` образца `=~` и шаблон `==` (extglob в `[[ ]]` включён) — одно слово; подстановки в нём bash
        # разбирает при раскрытии слова, со своим пустым стеком разделителей.
        self.assertTrue(runs("[[ a =~ ($(npm i x)) ]]"))
        self.assertTrue(runs("[[ a == @(b|$(npm i x)) ]]"))
        self.assertTrue(runs("[[ a =~ ($(npm i x \\\n; a1+=(% \\( b))) ]]"))
        self.assertEqual(nodes("[[ a =~ (b|c) ]]", shparse.Cond)[0].words[2].literal(), "(b|c)")

    def test_array_escape_in_substitution(self):
        # В скобках массива внутри `$(…)` bash не экранирует `\\(` (разделитель — `(`): ошибка скобок массива.
        script = shparse.parse("echo $(a=(\\( x); npm i x)\nnpm i y")
        self.assertEqual((script.error.fatal, cmds("echo $(a=(\\( x); npm i x)\nnpm i y")),
                         (False, [["npm", "i", "y"]]))
        self.assertTrue(runs("a=(\\( x); npm i x"))

    def test_eof_parameter(self):
        # parse(text, eof): text — тело подстановки; строка тела heredoc с терминатором и знаком в остатке кончает
        # тело, остаток читается командами до знака.
        self.assertEqual(cmds("cat <<E\nx\nE) y", ")"), [["cat"]])
        self.assertEqual(cmds("cat <<E\nx\nE) y", ""), [["cat"]])
        self.assertEqual(shparse.parse("cat <<E\nx\nE) y", ")").error, None)
        doc = nodes("cat <<E\nx\nE)", shparse.Heredoc)[0]
        self.assertEqual(doc.body_text, "x\nE)\n")
        script = shparse.parse("cat <<E\nx\nE}\nnpm i x", "}")
        self.assertEqual(cmds("cat <<E\nx\nE}\nnpm i x", "}"), [["cat"]])
        self.assertIsNone(script.error)


class ExpansionClassesTest(unittest.TestCase):
    """Классы потерь, найденные фаззером после правки parsed_markers (проверены прогоном bash 5.3 с `touch`)."""

    def test_single_quotes_in_double_quoted_word(self):
        # В слове `${x:-…}` внутри "…" и тела heredoc '…' — не кавычки: подстановка в них исполняется, в том числе
        # не закрытая до кавычки; в образце `${x#'…'}` '…' — кавычки.
        for text in ["echo \"${u:-'$(npm i x)'}\"", "echo \"${u:='$(npm i x)'}\"", "echo \"${HOME:+'$(npm i x)'}\"",
                     "echo \"${u:-'$(npm i x'')'}\"", "cat <<E\n${u:-'$(npm i x)'}\nE"]:
            with self.subTest(text):
                self.assertTrue(runs(text))
        for text in ["echo \"${HOME#'$(npm i x)'}\"", "echo ${u:-'$(npm i x)'}"]:
            with self.subTest(text):
                self.assertFalse(runs(text))

    def test_offset_word_expanded_as_double_quoted(self):
        # Смещение `${x:…}` bash раскрывает арифметикой, как текст в "…": `<(…)` в значении вложенной `${…}` — не
        # подстановка, а `$(…)` в её тексте исполняется.
        self.assertTrue(runs("echo ${HOME:${u5:-<(: <<E''\n$(npm i x)\nE\n)}0:1}"))
        self.assertFalse(runs("echo ${HOME:${u5:-<(npm i x)}0:1}"))

    def test_brace_inside_process_substitution_in_arithmetic(self):
        # `}` в теле `<(…)` внутри `${…}` арифметики не кончает `${`: конец `<(…)` находит разбор при раскрытии.
        self.assertTrue(runs("echo $(( ${HOME%%#<({ :; })<(npm i x)} ))"))
        self.assertTrue(runs("echo $(( ${HOME%%#<(f() { :; }; f)<(npm i x)} ))"))

    def test_arithmetic_check_sees_printed_text(self):
        # chk_arithsub: "…" пропускается целиком вместе с `${…<(…)…}` в ней; heredoc подстановки в арифметике —
        # с телом (кавычка тела считается).
        self.assertTrue(runs("echo $(( '$(npm i x)' `echo \"${HOME%%<(echo '\")''a b'; :)}\"` ))"))
        self.assertTrue(runs("echo $(( ${HOME+<(npm i x\n\"$(true)\"[)$(cat <<E\n'y\nE\n)} ))"))

    def test_ambiguous_arithmetic_falls_back_to_read_substitutions(self):
        # bash разбирает при раскрытии напечатанный текст `$((…) …)`; если исходный текст не разбирается,
        # подстановки, разобранные при чтении, остаются.
        self.assertTrue(runs("echo \"$(( $( npm i x; a3=(\\)) ) 1 ))\""))
        # Ошибка дальше в теле: команды до неё bash исполняет.
        self.assertTrue(runs("$(( (npm i x) )\n\nesac)"))

    def test_printed_text_reparsed(self):
        # Класс 2: `\\` в скобках массива в `$(…)` внутри "…" при чтении не экранирует, при раскрытии — экранирует
        # (ArrayBackslashReexpandTest); `\\(` ошибка уже при чтении.
        self.assertTrue(runs('b="$(true\n a5=( \\)) npm i x)"'))
        self.assertTrue(runs('a1=("$(a5=( \\)) npm i x)")'))
        self.assertFalse(runs('x="$(a=(x \\( y); npm i x)"'))
        # Класс 3: $'…' в подстановке внутри арифметики bash хранит в '…' — от этого зависит, арифметика ли `$((`.
        self.assertTrue(runs("echo \"$(( '$(npm i x)' $( '$(<<E'$'\\'' >|/dev/null) ))\""))
        # Класс 5: тело `$((…) …)` с зарезервированными словами — команды до ошибки исполняются.
        self.assertTrue(runs("$(({ false; } || npm i x\ncase \"a\" in\n esac)\nthen)"))

    def test_empty_quotes_join_literal(self):
        word = shparse.parse("touch P1''\"\"").commands[0].words[1]
        self.assertEqual(([type(p).__name__ for p in word.parts], word.literal()), (["Lit"], "P1"))
        self.assertEqual(shparse.parse("echo ''").commands[0].words[1].literal(), "")


class DparenExpansionEndTest(unittest.TestCase):
    """Конец `$((…)` при раскрытии находит extract_delimited_string (SX_COMMAND) по тексту слова, а не
    parse_matched_pair чтения: `#` за пробелом — комментарий до конца строки, и подстановка кончается дальше.
    Проверено прогоном bash 5.3 с `touch`."""

    def test_comment_moves_end(self):
        # Чтение кончает `$(( #))`; раскрытие — `( #))"⏎npm i x)`: скобки не сбалансированы, это подстановка команды.
        for text in ['$(( #))"\nnpm i x))"', 'echo $()$(( #))"\nnpm i x))"', 'cat <<E\n$(( #))"\nnpm i x))"\nE']:
            with self.subTest(text):
                self.assertTrue(runs(text))

    def test_same_or_no_end(self):
        # В "…" раскрытие кончает на той же `)`; без закрывающей при раскрытии — bad substitution, тело не исполняется.
        self.assertFalse(runs('echo "$(( #))\nnpm i x)"'))
        self.assertFalse(runs("echo $((npm i x) #x)"))

    def test_unclosed_quote_after_end(self):
        # Остаток слова за концом подстановки раскрывается с открытой "…": string_extract_double_quoted берёт её
        # до конца слова, подстановки в ней исполняются ('…' — нет).
        self.assertTrue(runs('echo $(( # "x\n1) )"\n\'$(npm i x)\'\n))'))
        self.assertFalse(runs('echo $(( # \'x\n1) )\'\n"$(npm i x)"\n))'))
        # Зерно 7, глубина 2, номер 2814: исполнены P6, P7, P8.
        text = ('v1=$(({ false; } || true # "|touch P3\ncat - <<END2) ) & echo $(function f3() { touch P7 | touch P8 ; '
                'time -p echo -n $?\'a ba b\'\n}; f3)\n"touch P5\n\'$(touch P6)\'\n))\nEND2')
        found = {c[1] for c in cmds(text) if c[:1] == ["touch"] and len(c) > 1}
        self.assertEqual({"P6", "P7", "P8"} - found, set())

    def test_fuzz_form(self):
        # Зерно 17, глубина 2, номер 15006: `|& #xtouch P9) )` — комментарий до конца строки.
        text = ('false || echo -n $( touch P1\ncat <<"E1" | cat\n\'touch P2\nx touch P3\nE1\n) $((cat <(touch P6\nt'
                'ime touch P7) >/dev/null |& #xtouch P9) )"<<E$( cat < <(time -p echo -n \\"@ \'#$x\' >|/dev/null\nto'
                'uch P11); : <(time touch P12))$(time (( $(touch P13) 1 ))\ntouch P14)"')
        found = {c[1] for c in cmds(text) if c[:1] == ["touch"] and len(c) > 1}
        self.assertEqual({"P1", "P6", "P7", "P11", "P12", "P13", "P14"} - found, set())


class HeredocDolbraceTest(unittest.TestCase):
    """`${имя OP слово}` с OP из `%#/^,:` прямо в теле heredoc: bash извлекает слово extract_heredoc_dolbrace_string и
    переписывает его до раскрытия — `$'…'` значением; в теле `${| …; }`, `${ …; }` внутри слова (для этой функции —
    вложенный `${`, состояние PARAM) значение без кавычек, и `\\n` в нём делит команды. Проверено прогоном bash 5.3 с
    `touch`."""

    def test_ansi_c_value_unquoted(self):
        for text in ["cat <<E\n${HOME/${| echo $'a\\nnpm i x'; }/x}\nE",
                     "cat <<E\n${HOME#${ echo $'a\\nnpm i x'; }}\nE",
                     "cat <<E\n${HOME#$'x'}${HOME%${| echo $'a\\nnpm i x'; }}\nE"]:
            with self.subTest(text):
                self.assertTrue(runs(text))

    def test_other_contexts_keep_value(self):
        # `$(…)` слово переносит как есть; в "…" и без оператора `$'…'` — одно слово.
        for text in ["cat <<E\n${HOME/$(echo $'a\\nnpm i x')}\nE", "echo \"${HOME/${| echo $'a\\nnpm i x'; }/x}\"",
                     "cat <<E\n${| echo $'a\\nnpm i x'; }\nE"]:
            with self.subTest(text):
                self.assertFalse(runs(text))

    def test_value_expanded_alone(self):
        # Конец слова — по счёту `${`, `}` (тело `${ …; }` для этой функции — вложенный `${`): здесь `}` группы
        # `{ false; }`. Образец раскрывается отдельно: `<(…)` исполняется, затем тело `${ …` без `}` — ошибка
        # (зерно 5, глубина 2, номер 16786: исполнен P2).
        text = ('touch P1 ; : && true <<EOF1\nx \\\\\n"${HOME/<(touch P2)${ { false; } || echo $\' \\\\\\\\\' a#b--opt='
                "v; }$(: && echo -n $'\\'\\\\\\x41' a#b 2>&1)/x}\nEOF1")
        self.assertIn(["touch", "P2"], cmds(text))
        self.assertTrue(runs("cat <<E\n${HOME/<(npm i x)${ { a; } b; }/c}\nE"))

    # Формы генератора фаззера (зерно 13, глубина 3): маркеры, которые исполнил bash.
    FUZZ = {
        7023: ('echo \'touch P1touch P2\'\\\\ >/dev/null;( read -r x <<< "touch P3"\ncat - <<-END1 |& cat >/dev/nu'
               'll <<END3 >/dev/null ); ! time touch P12\n\t"${HOME/${| false || echo -n $\'touch P4\\ntouch P5\' '
               ">|/dev/null\ntouch P7; }#/x}\n\tEND1\nEND3 x\nx touch P9\nEND3'\nEND3",
               {"P5", "P7", "P12"}),
        11307: ('cat - <<END1 | touch P8 \\\n; cat <<< `touch P14 && a3+=(\\`touch P9\\` \\$[ - 0 (1) \\$( touch P1'
                "0 & touch P11) 1 ]\\\\; \\`[ -n a ] && touch P12\\`)`\n${HOME//x/${| echo $';touch P1;' [$( touch "
                "P2)\n}`true \\\\\n{a,\\`{ false; } || touch P3\\`}{a,\\$( touch P5)} \\\\\n}`'a b'}\n; }\nEND1",
                {"P1", "P2", "P3", "P5", "P8", "P9", "P10", "P11", "P14"}),
    }

    def test_fuzz_forms(self):
        for index, (text, executed) in self.FUZZ.items():
            with self.subTest(index):
                found = {c[1] for c in cmds(text) if c[:1] == ["touch"] and len(c) > 1}
                self.assertEqual(executed - found, set())


def touched(text):
    """Аргументы `touch` среди простых команд дерева."""
    return {w for c in cmds(text) if c[:1] == ["touch"] for w in c[1:] if w}


class GroupAnsiQuoteTest(unittest.TestCase):
    """Группа `(…)` образца `=~` (и extglob): bash хранит её текстом parse_matched_pair, подстановки в ней не
    разбирая, и `$'…'` вне "…" — значением: в '…' (sh_single_quote), а если разделитель — `"` (группа в подстановке
    в "…": стек разделителей parse_comsub не сбрасывает) — без кавычек. Раскрытие группы (конец `$((…)` —
    extract_delimited_string, тело подстановки) читает этот текст. Проверено прогоном bash 5.3 с `touch`."""

    def test_dparen_end_after_ansi_quote(self):
        # `$'\'…'` в `$((…) )` группы: в исходном тексте `'` после `\` закрывала бы '…', и конец `$((…)` при
        # раскрытии не находился (тело терялось; зерно 23, глубина 3, номер 16571).
        for text in ["[[ a =~ ($(( touch P2; : $'\\'x' ) )) ]]", "[[ a =~ x($(( touch P2; echo $'it\\'s' ) )) ]]",
                     "[[ a =~ (x|$(( touch P2; echo $'it\\'s' ) )) ]]",
                     "f() { [[ a =~ ($(( touch P2; echo $'it\\'s' ) )) ]]; }; f",
                     "echo $( [[ a =~ ($(( touch P2; echo $'it\\'s' ) )) ]] )",
                     "[[ a =~ ( \"$(( touch P2; : $'\\'' ) )\" ) ]]",
                     "[[ a =~ ( $(( touch P2; : \"${x#$'\\''}\" ) ) ) ]]"]:
            with self.subTest(text):
                self.assertIn("P2", touched(text))
        self.assertTrue(runs("[[ a =~ ($(( npm i x; echo $'it\\'s' ) )) ]]"))

    def test_value_unquoted_in_double_quotes(self):
        for text in ['echo "$( [[ a =~ ($(echo $\';touch P2\')) ]])"',
                     'echo "${ [[ a =~ ($(echo $\';touch P2\')) ]];}"',
                     # Зерно 31, глубина 2, номер 12932.
                     "v1+=\"}$( [[ a =~ ($(echo -n $';touch P2' \\)\n[ -n a ] && echo -n $';' ]'$x')) ]])\\$\""]:
            with self.subTest(text):
                self.assertIn("P2", touched(text))

    def test_value_quoted_elsewhere(self):
        for text in ["echo $( [[ a =~ ($(echo $';touch P2')) ]])", "[[ a =~ ($(echo $';touch P2')) ]]",
                     'echo "$( : $( [[ a =~ ($(echo $\';touch P2\')) ]]))"',
                     'echo "$( [[ a =~ ("$(echo $\';touch P2\')") ]])"', 'echo "$( [[ a =~ ($\'x;touch P2\') ]])"']:
            with self.subTest(text):
                self.assertNotIn("P2", touched(text))

    def test_positions(self):
        # Позиции вне `$'…'` — по строке разбора (тело `` `…` `` без `\\`): зерно 13, номер 3480.
        text = "v1+=[`[[ a =~ (\\$(echo {a,\\$(( (1) 1 ))}\\$'a\\`;' \\$(! false && touch P1))) ]];touch P3`"
        self.assertEqual(text_mismatches(text), [])
        self.assertEqual(touched(text), {"P1", "P3"})


class PrintedSemicolonTest(unittest.TestCase):
    """bash исполняет тело `$(…)`, `${ …; }`, `<(…)` по его печати (print_comsub), а печать (print_cmd.c) опускает `;`
    сразу за телами heredoc (was_heredoc): следующая команда становится словами предыдущей. Сбрасывают это
    перенаправления команды, конец `( )`, `{ }`, `then`, `do`, перевод строки; в функции вместо `;` печатается перевод
    строки. Конец такой подстановки при раскрытии находит разбор печати со знаком конца: печать `case` бывает
    раньше него, остаток печати — текст слова. Проверено прогоном bash 5.3 с `touch`."""

    TOUCHED = {
        "echo $(true <<E; touch; P1\nE\n)": {"P1"},
        "echo $(true <<E\nE\ntouch; P1)": {"P1"},
        "echo ${ true <<E; touch; P1\nE\n}": {"P1"},
        "cat <(true <<E; touch; P1\nE\n)": {"P1"},
        "echo $(true && true <<E; touch; P1\nE\n)": {"P1"},
        "echo $(true <<E &\nE\ntouch; P1)": {"P1"},
        "echo $(true <<E\nE\n( touch; P1 ))": {"P1"},
        "echo $(echo $(true <<E; touch; P1\nE\n))": {"P1"},
        "echo $(( $(true <<E; touch; P1\nE\n) ))": {"P1"},
        "echo $(true <<E && touch P1; touch P2\nE\n)": {"P1", "P2", "touch"},
        "echo $(true <<E; touch; P1; touch P2\nE\n)": {"P1", "P2"},
        "echo $({ true <<E; }; touch; P1\nE\n)": set(),
        "echo $(f() { true <<E; touch; P1; }\nE\nf)": set(),
        "echo $(true <<E | true; touch; P1\nE\n)": set(),
        "echo $(true <<E\nE\n:\ntouch; P1)": set(),
        "echo `true <<E; touch; P1\nE\n`": set(),
        # Тело heredoc раскрывается по исходному тексту: печати нет.
        "cat <<X\n$(true <<E; touch; P1\nE\n)\nX": set(),
        # `|&` печатается `2>&1 |`: перенаправление сбрасывает was_heredoc.
        "v1+=${ true <<E; touch P3 |& touch P5\nE\n}": {"P3", "P5"},
        # Печать `case` — на нескольких строках, конец подстановки — `)` образца; остаток печати раскрывается.
        "x=$(cat <<E; touch P15;time case 'a' in b) :;& *) v=$(touch P17); : && a=(  );; esac\nE\n)": {"P15", "P17"},
    }

    def test_touched(self):
        for text, expected in self.TOUCHED.items():
            with self.subTest(text):
                self.assertEqual(touched(text) & {"P1", "P2", "P3", "P5", "P15", "P17", "touch"}, expected)

    def test_merged_command_positions(self):
        # Слова команды за телом heredoc в печати переводятся на своё место в исходном тексте (не в переписанный
        # кусок терминатора за телом): команда — от `npm` до `x`, слова по порядку. Сегмент детектора — её текст.
        import depcheck
        for text, segment in [("echo $(cat <<E\nE\nnpm; i x)", "npm; i x"), ("x=$(cat <<E; npm; i x\nE\n)", "npm; i x"),
                              ("x=$(cat <<E && npm i x\nE\n)", "npm i x")]:
            with self.subTest(text):
                self.assertEqual(text_mismatches(text), [])
                found = [text[c.start:c.end] for c in shparse.simple_commands(shparse.parse(text))]
                self.assertIn(segment, found)
                self.assertEqual(depcheck.dependency_add(text), segment)
        self.assertFalse(runs("x=$(f() { cat <<E; npm; i x; }\nE\n)"))

    # Формы генератора фаззера: (зерно, глубина, номер) → маркеры, которые исполнил bash.
    FUZZ = {(23, 2, 6555): {"P2", "P3", "P6", "P7", "P11", "P13"}, (1, 3, 217): {"P3", "P4", "P12", "P15", "P17"},
            (7, 3, 14475): {"P3", "P5", "P6", "P9"},
            (13, 3, 13180): {"P1", "P2", "P4", "P5", "P9", "P11", "P13", "P15", "P16"}}

    def test_fuzz_forms(self):
        bashdiff = _bashdiff()
        for (seed, depth, index), executed in self.FUZZ.items():
            with self.subTest(seed=seed, depth=depth, index=index):
                text = bashdiff.generate(seed, index, runtime=False, depth=depth)[0]
                self.assertEqual(executed - bashdiff.parsed_markers(shparse, text), set())


class FunsubInArithTest(unittest.TestCase):
    """`${ …}` в "${…}" в `$((…)`: печать parse_comsub ставит перед `}` перевод строки, если тело кончил перевод строки
    (`\\;` ⏎ `}` — `\\;` ⏎ ` }`, а не `\\; }`, где `}` — слово и тело не закрыто). Конец `$((…)`, который без разбора
    вложенной подстановки не найти (в `${ …}` внутри "…"), — не расхождение чтения и раскрытия. Проверено прогоном
    bash 5.3 с `touch` (зерно 43, глубина 3, номер 15654)."""

    def test_body_runs(self):
        for text in ['echo $(( "${u-${ touch P1;$()\\;\n}}" ))', 'x=$(("${u-${ touch P1;$()\\;\n}}"))',
                     'echo $(("${u-${ touch P1; echo $(:)\\;\n}}"))']:
            with self.subTest(text):
                self.assertIn("P1", touched(text))
                self.assertTrue(runs(text.replace("touch P1", "npm i x")))

    def test_print_before_brace(self):
        # Перед `}` печати — `;` по виду последней команды (was_semi), а не по последнему знаку печати: `\\;` — слово,
        # `${ echo \\; ;}` печатается `{ echo \\;; }`; без `;` — только за фоновой командой и за переводом строки.
        b = "\\"
        for text, printed in [("echo ${ echo " + b + "; ;}", "{ echo " + b + ";; }"), ("echo ${ a; }", "{ a; }"),
                              ("echo ${ a & }", "{ a & }"), ("echo ${ a\n}", "{ a\n }"), ("echo ${| a; }", "{|a; }")]:
            with self.subTest(text):
                sub = nodes(text, shparse.Sub)[0]
                self.assertEqual(shparse._render([("sub", sub)]), printed)

    def test_chain_depth(self):
        # Цепочка не раскрывается заново на каждом уровне: 200 уровней — без DEPTH.
        script = shparse.parse("echo " + '$(("${u-${ :;$() ' * 200)
        self.assertNotEqual(getattr(script.error, "kind", None), shparse.DEPTH)


class HeredocBodySubstitutionTest(unittest.TestCase):
    """Подстановка в теле heredoc: конец находит xparse_dolparen — разбор со знаком конца, где строка тела
    вложенного heredoc, начатая терминатором, со знаком конца в остатке кончает тело (make_here_document); исполняет
    bash исходный текст до этого конца без знака конца (parse_and_execute), и та же строка там — строка тела.
    Проверено прогоном bash 5.3 с `touch`."""

    def test_line_with_terminator_is_body(self):
        for text in ["cat <<E1\n$(cat <<E2\nE2\\\n'$(touch P4)'\nE2\n)\nE1",
                     "cat <<E1\n$(cat <<E2\nE2'$(touch P4)'\nE2\n)\nE1",
                     "cat <<E1\n${ cat <<E2\nE2'$(touch P4)'}\nE2\n}\nE1"]:
            with self.subTest(text):
                self.assertIn("P4", touched(text))

    def test_outside_body_rule_stays(self):
        # Вне тела heredoc печать — тело, прочитанное по правилу знака конца: остаток строки — команда в '…'.
        self.assertNotIn("P4", touched("echo $(cat <<E2\nE2'$(touch P4)'\nE2\n)"))
        self.assertNotIn("P1", touched("cat <<E1\n$(cat <<E2\nE2 ); touch P1\nE2\n)\nE1"))


class CompoundAssignmentHeredocTest(unittest.TestCase):
    """Перевод строки в скобках `a=(…)` читает тела ожидающих heredoc, а parse_compound_assignment затем возвращает
    need_here_doc и redir_stack (restore_parser_state): heredoc ждёт тела снова, со следующего перевода строки, и его
    терминатор — уже прочитанное тело (make_here_document заменило им слово). Тело с переводом строки не совпадает ни
    с одной строкой — второе тело идёт до конца ввода; пустое кончает пустая строка. Проверено прогоном bash 5.3 с
    `touch` (предупреждение «wanted `…'» называет второй терминатор)."""

    def test_second_body_to_end(self):
        text = "cat <<E; a=(x\nnpm i x\nE\ny)\nnpm i x\nE\nnpm i x"
        self.assertEqual(cmds(text), [["cat"], []])
        docs = nodes(text, shparse.Heredoc)
        self.assertEqual((docs[0].term, docs[0].body_text), ("npm i x\n", "npm i x\nE\nnpm i x\n"))

    def test_empty_first_body(self):
        # Пустое тело: терминатор — пустая строка.
        self.assertEqual(cmds("cat <<E; a=(x\nE\ny); npm i x\ntouch P2\n\nnpm i y"),
                         [["cat"], [], ["npm", "i", "x"], ["npm", "i", "y"]])
        self.assertEqual(cmds("cat <<E; a=(x\nE\ny); npm i x\ntouch P2\nE\nnpm i y"), [["cat"], [], ["npm", "i", "x"]])
        # Слово в кавычках: терминатор — тело после снятия кавычек.
        self.assertEqual(cmds("cat <<'E'; a=(x\n'E'\nE\ny); npm i x\nE\nnpm i y"), [["cat"], [], ["npm", "i", "x"]])

    def test_dparen_subshell_printed(self):
        # `((…)`, оказавшаяся подоболочкой: bash возвращает во ввод прочитанное с подстановками в печати. Заголовок
        # heredoc, ждущего тела снова, печатается с новым терминатором (`<<t)"${ '''';}⏎`): `)` в нём кончает
        # подстановку раньше, и `a=($(…))` — команда подоболочки (зерно 13, глубина 3, номер 15333; исполнены P1,
        # P4, P5).
        text = "(($(<<E|a=($(npm i x)\nt)\"${ '''';}\nE)))"
        self.assertTrue(runs(text))
        self.assertEqual(out_of_bounds(text), [])
        text = ('(( $(:  <<-EOF1 | cat|a2=($( touch P4\ntouch P5)\ntouch P6) ; (: && (((test 1 && touch P7) )) )\n\t"${'
                '| touch P1 | printf \'%s\\n\' \\"\'$(;\'; }\n\tEOF1\n) 1 ))\ntouch P10\ncat >/dev/null <<\'E3\'\nE3\''
                '\nE3E3\nE3')
        found = {c[1] for c in cmds(text) if c[:1] == ["touch"] and len(c) > 1}
        self.assertEqual({"P1", "P4", "P5"} - found, set())
        self.assertEqual(out_of_bounds(text), [])


class SubscriptAssignmentTest(unittest.TestCase):
    """Конец индекса `имя[…]=` ищет skipsubscript (skip_matched_pair): `]` в `${…}`, `` `…` `` и вложенных `[…]`
    индекс не кончает. Проверено прогоном bash 5.3 с `touch`."""

    def test_bracket_inside_expansion(self):
        for text in ["a[${x:-]}]=1 npm i x", "a[`echo ]`]=1 npm i x", "a[${x:-${y:-]}}]=1 npm i x",
                     "a[${x[${y:-]}]}]=1 npm i x", "a[${x//]/y}]=1 npm i x", "a[${x:-`echo ]`}]=1 npm i x",
                     "a[${x:-a[]]}]=1 npm i x", "a[${x:-]}]+=1 npm i x", "a[${x:-]}]=(1) npm i x",
                     "x=1 a[${x:-]}]=1 npm i x", "a[$((1 # ]\n))]=1 npm i x"]:
            with self.subTest(text):
                simple = shparse.simple_commands(shparse.parse(text))[0]
                self.assertEqual([w.literal() for w in simple.words], ["npm", "i", "x"])
                self.assertEqual(len(simple.assigns), 2 if text.startswith("x=1") else 1)

    def test_not_assignment(self):
        # Индекс кончается первой `]` вне `${…}`: за ней не `=` — слово не присваивание, а имя команды.
        for text in ["a[${x:-}]]=1 npm i x", "a[${x:-]}]x=1 npm i x"]:
            with self.subTest(text):
                simple = shparse.simple_commands(shparse.parse(text))[0]
                self.assertEqual((len(simple.assigns), len(simple.words)), (0, 4))

    def test_subscript_word_quote_removal(self):
        # Без `=` за `]` слово раскрывается как обычное: `\\` вне кавычек снимается (проверено bash 5.3: argv).
        for text, word in [("y=1 x[\\n\\p\\m] c", "x[npm]"), ("x[\\a] c", "x[a]"), ("x[ |\\'] c", "x[ |']"),
                           ("x[a\\\"b] c", 'x[a"b]'), ("x[\\\\] c", "x[\\]"), ("x[\\]] c", "x[]]"),
                           ("x[\"\\a\"] c", "x[\\a]"), ("x[a]b[\\c] d", "x[a]b[c]")]:
            with self.subTest(text):
                simple = shparse.simple_commands(shparse.parse(text))[0]
                self.assertEqual(simple.words[0].literal(), word)


class ArrayBackslashReexpandTest(unittest.TestCase):
    """`\\` в слове скобок массива внутри подстановки: parse_compound_assignment снимает PST_NOEXPAND, и read_token_word
    экранирует следующий символ, только если разделитель (current_delimiter) пуст или `"` перед `\\`, `` ` ``, `$`,
    `"`. При чтении разделитель — `(` подстановки или `"` вокруг неё: `\\)` — символ `\\` и конец скобок. bash хранит
    подстановку напечатанной (print_comsub) и при раскрытии разбирает её текст с остатком слова заново без
    разделителей (xparse_dolparen): там `\\` экранирует, и конец подстановки, а с ним команды, бывают другими.
    Проверено прогоном bash 5.3 с `touch`."""

    def test_reexpanded_substitution_runs_hidden_command(self):
        # При чтении `\\'` открывает '…' до `\\'` в `b=(…)`; при раскрытии обе `'` экранированы.
        self.assertTrue(runs("x=$(a=(\\'y) ; npm i x ; b=(\\')); echo"))
        self.assertTrue(runs('x="$(a=(\\\'y) ; npm i x ; b=(\\\'))"; echo'))
        self.assertTrue(runs("echo ${x:-$(a=(\\'y) ; npm i x ; b=(\\'))}"))
        # Конец подстановки при раскрытии дальше, чем при чтении: остаток "…" — её тело.
        self.assertTrue(runs('echo "$(a=(\\)) ; npm i x)$(true)"'))

    def test_reexpanded_substitution_error_hides_read_commands(self):
        # При раскрытии `\\)` экранирована, `&&` в скобках — ошибка: подстановка не исполняется.
        self.assertFalse(runs('echo "$( a6=(a=b\\) && npm i x)"'))
        self.assertFalse(runs('x="$(a=(\\)\'<<E\'\nnpm i x)\n{ :; })"; echo'))

    def test_read_reading_keeps_word_end(self):
        # Слово кончается там, где его кончает разбор при чтении: `"…"` после `\\)` — внешняя кавычка.
        self.assertTrue(runs('v=$(: "$(a=(\\)) ; true)" | npm i x)'))
        # Процесс-подстановка в той же команде исполняется до ошибки раскрытия "…".
        self.assertTrue(runs(': \\\n[")" \\\n<(npm i x)"$[ $( a6=(a=b\\) && touch P2) 1 ]"'))

    # Формы генератора фаззера: (зерно, глубина, номер) → маркеры, которые исполнил bash.
    FUZZ = {
        (11, 3, 11823): ("v1+=$( touch P1)$( : && [[ -z \"$(! time a2=(\\)'<<E'\ntouch P2)\n{ })\" ]])",
                         {"P1"}),
        (1, 3, 16359): ("test 1 && cat >/dev/null <<-'END1' ; cat >/dev/null <<< '<<E'$( cat <<< \"\n$(a2+=($'a\\n' a"
                        "#b \\)) ; case \"a\" in\na)|(b) touch P5\n;&\nesac)\"$ | { touch P6 ;})\n\t'touch P1\n\ttouc"
                        "h P2\n\tEND1;\n\tEND1",
                        {"P6"}),
        (5, 2, 7424): ("{ false; } || v1+=${HOME#<({ false; } || function f3() { : && touch P1\n}\nf3)}; false || tru"
                       "e;#}`\n( v4=$xa\\ b touch P10) | : \\\n[\")\" \\\n<(! false && for ((i=0; i<1; i++)); do touc"
                       "h P12; touch P13; done)\"`(( x = $(touch P15) 1 ))`$[ $([ -n a ] && printf '%s\\n' $'`' \"<<E"
                       "\" >/dev/null & printf '%s\\n' $(( - 0 (1) $( touch P18 \\\n; : && touch P19) 1 )) $( touch P"
                       "21 |& true)) $( a6=(a=b\\) && touch P22)) 1 ]\\\"\"",
                       {"P1", "P10", "P12"}),
        # Разбор при раскрытии брал готовым разбор той же подстановки из исходного текста (длина другая, чем у
        # печати): чтение сдвигалось, `"}"` открывал кавычку до конца.
        (2, 3, 7715): ("printf '%s\\n' $(( (1) - 0 $( if touch P3\nthen [[ a == a ]] && true #'; ( printf '%s\\n' \\'"
                       " )\nfi |& touch P4\nread -r x <<< ${u1-\"a b\"<((( $(test 1 && touch P5;touch P7) 1 ))|printf"
                       " '%s\\n' ${HOME:+$(: & ! touch P8)\"}\"})a} |& a3=(\\# `test 1 && touch P14`)) 1 )) $'\\'touc"
                       "h P16'",
                       {"P3", "P4", "P5", "P8", "P14"}),
    }

    def test_fuzz_forms(self):
        # Ни один исполненный маркер не потерян (лишние — строгость: ошибка раскрытия `$[ … ]` прерывает команду).
        for key, (text, executed) in self.FUZZ.items():
            with self.subTest(key):
                found = {c[1] for c in cmds(text) if c[:1] == ["touch"] and len(c) > 1}
                self.assertEqual(executed - found, set())
        self.assertNotIn(["touch", "P2"], cmds(self.FUZZ[11, 3, 11823][0]))


class PrintedTextTest(unittest.TestCase):
    """bash хранит подстановку, разобранную при чтении, напечатанной (print_comsub, make_command_string) и при
    раскрытии разбирает этот текст; печать модуля — его аналог. Образцы — вывод `declare -f` bash 5.3."""

    @staticmethod
    def printed(text):
        script = shparse.parse(text)
        return shparse._render(shparse._script_items(script))

    def test_word_tokens(self):
        # $'…' вне "…" — значением в '…'; в "…" — как есть; подстановки — напечатанным телом.
        self.assertEqual(self.printed("x=$'a\\'b' $(echo $'c\\'d' \"$'e'\")"),
                         "x='a'\\''b' $(echo 'c'\\''d' \"$'e'\")")
        self.assertEqual(self.printed("echo $(( (1) + $( echo 2 ) )) ${x:-$( echo y)} ${ echo a; }"),
                         "echo $(( (1) + $(echo 2) )) ${x:-$(echo y)} ${ echo a; }")
        # `\\` в скобках массива в подстановке не экранирует `)`: она кончает скобки, следующая — подстановку
        # (declare -f bash 5.3: `echo "$(touch P6; a3=(\\)) )"`).
        self.assertEqual(self.printed('echo "$( touch P6; a3=(\\)) )"'), 'echo "$(touch P6;a3=(\\)) )"')

    def test_commands_and_heredocs(self):
        # Тело heredoc — за разделителем после строки команды; `for` без `in` — `in "$@"`.
        self.assertEqual(self.printed("cat <<E; echo b\nbody $x\nE\necho c"), "cat <<E\nbody $x\nE\necho b\necho c")
        text = self.printed("cat <<'E' | cat <<-F\nq\nE\n\tw\n\tF")
        self.assertEqual(text, "cat <<'E' |\nq\nE\n cat <<-F\nw\nF\n")
        self.assertEqual(self.printed("for i; do :; done"), 'for i in "$@"; do :; done')
        self.assertEqual(self.printed("echo $(cat <<E\nx\nE\n)"), "echo $(cat <<E\nx\nE\n)")
        # Повторный разбор напечатанного даёт те же команды.
        for text in ["if a; then b; elif c; then d; else e; fi; while a; do b; done",
                     "case a in esac|b) x;& c) y;; esac", "f() ( a ); function g { b; } >log; coproc C { d; }",
                     "[[ ! -n a && ( b == c || d =~ (e) ) ]]", "a 2>&1 >&- {fd}<x 3<<<y &>z <>v", "time -p ! a | b"]:
            with self.subTest(text):
                self.assertEqual(cmds(self.printed(text)), cmds(text))

    @mock.patch.object(shparse, "_STACK_MAX", 10 ** 9)  # линейность разбора, а не предел (DepthLimitTest)
    def test_linear(self):
        for make in [lambda n: "echo " + "$(" * n + "a" + ")" * n,
                     lambda n: "echo \"" + "$(echo " * n + "a" + ")" * n + "\"",
                     lambda n: "echo $(( " + "$(( " * n + "1" + " ))" * n + " ))",
                     lambda n: "echo " + "${ " * n + "a" + "; }" * n]:
            small, large = make(300), make(1200)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: self.printed(small), lambda: self.printed(large))


class HeredocTest(unittest.TestCase):
    """Heredoc: тело — со строки после конца логической строки, знак конца самой внутренней подстановки,
    склейка `\\`, `<<-`, несколько на строке, тело в подстановке, растянутой на несколько строк."""

    def test_body_is_data_or_parts(self):
        self.assertEqual(cmds("cat <<E\nnpm i x\nE\necho y"), [["cat"], ["echo", "y"]])
        self.assertTrue(runs("cat <<E\n$(npm i x)\nE"))
        self.assertTrue(runs("cat <<E\n'$(npm i x)'\nE"))
        self.assertTrue(runs("cat <<E\n\"`npm i x`\"\nE"))
        self.assertTrue(runs("bash <<E\n: '$(npm i x)'\nE"))
        self.assertTrue(runs("cat <<E;printf x | bash\n'$(npm i x)'\nE"))
        self.assertFalse(runs("cat <<'E'\n$(npm i x)\nE"))
        self.assertFalse(runs("cat <<E\n\\$(npm i x)\nE"))
        doc = nodes("cat <<'E'\na $x\nE", shparse.Heredoc)[0]
        self.assertEqual(([type(p).__name__ for p in doc.parts], doc.parts[0].text), (["Lit"], "a $x\n"))

    def test_terminators(self):
        self.assertEqual(cmds("cat <<-E\n\tx\n\t\tE\nnpm i x"), [["cat"], ["npm", "i", "x"]])
        self.assertEqual(cmds("cat <<E\n\tE\nnpm i x\nE"), [["cat"]])
        self.assertEqual(cmds("cat <<'E F'\nx\nE F\nnpm i x"), [["cat"], ["npm", "i", "x"]])
        self.assertEqual(cmds("cat <<\"E\"\\F\nx\nEF\nnpm i x"), [["cat"], ["npm", "i", "x"]])
        self.assertEqual(cmds("cat <<''\nx\n\nnpm i x"), [["cat"], ["npm", "i", "x"]])
        self.assertEqual(cmds("cat <<E\nE \nnpm i y\nE\nnpm i x"), [["cat"], ["npm", "i", "x"]])
        # Конец ввода без терминатора — тело до конца.
        self.assertEqual(cmds("cat <<E\nnpm i x"), [["cat"]])

    def test_line_join(self):
        # Терминатор без кавычек: строки тела склеены по `\\` с переводом строки (read_secondary_line); склеенная
        # строка сравнивается с терминатором, `\\\\` — пара.
        self.assertTrue(runs("cat <<E\nE\\\n\nnpm i x"))
        self.assertTrue(runs("cat <<EOF\nEO\\\nF\nnpm i x"))
        self.assertTrue(runs("cat <<-E\n\tE\\\n\nnpm i x"))
        self.assertTrue(runs("cat <<-E\n\t\\\n\tE\nnpm i x"))
        self.assertTrue(runs("cat <<E\n$(echo a #\\\n)\nnpm i x)\nE"))
        for text in ["cat <<E\nx\\\nE\nnpm i x", "cat <<E\nE\\\nE\nnpm i x", "cat <<'E'\nE\\\n\nnpm i x",
                     "cat <<E\nE\\\\\n\nnpm i x", "cat <<-E\n\tE\\\n\t\nnpm i x", "cat <<E\nE\\\n)\nnpm i x"]:
            with self.subTest(text):
                self.assertFalse(runs(text))
        doc = nodes("cat <<E\na\\\nb\nE", shparse.Heredoc)[0]
        self.assertEqual(doc.body_text, "ab\n")
        doc = nodes("cat <<'E'\na\\\nb\nE", shparse.Heredoc)[0]
        self.assertEqual(doc.body_text, "a\\\nb\n")

    def test_several_on_line(self):
        self.assertEqual(cmds("cat <<A <<B; cat <<C\na\nA\nb\nB\nc\nC\nnpm i x"),
                         [["cat"], ["cat"], ["npm", "i", "x"]])
        docs = nodes("cat <<A <<-B\na\nA\n\tb\n\tB\n", shparse.Heredoc)
        self.assertEqual([(d.term, d.strip_tabs, d.body_text) for d in docs], [("A", False, "a\n"), ("B", True, "b\n")])

    def test_body_after_logical_line(self):
        # Тело — после перевода строки, которым кончается команда: `\\` с переводом строки и подстановка через
        # строку отодвигают его.
        self.assertEqual(cmds("cat <<E \\\n; npm i x\nbody\nE"), [["cat"], ["npm", "i", "x"]])
        self.assertTrue(runs("cat <<E; echo $(:\nnpm i x)\nE"))
        self.assertEqual(cmds("cat <<E; echo \"a\nb\"\nnpm i y\nE\nnpm i x"),
                         [["cat"], ["echo", "a\nb"], ["npm", "i", "x"]])
        # Подстановка, закрытая на строке: тела её heredoc — первыми, тела строки — за ними.
        self.assertEqual(cmds("echo $(cat <<E) ; npm i x\nnpm i y\nE\nnpm i z"),
                         [["echo", None], ["cat"], ["npm", "i", "x"], ["npm", "i", "z"]])
        self.assertEqual(cmds("cat <<A; echo $(cat <<B)\nb\nB\na\nA\nnpm i x"),
                         [["cat"], ["echo", None], ["cat"], ["npm", "i", "x"]])
        self.assertEqual(cmds("cat <<A; echo $(cat <<B)\nnpm i y\nA\nnpm i z\nB\nnpm i x"),
                         [["cat"], ["echo", None], ["cat"]])

    def test_pipeline_after_body(self):
        script = shparse.parse("cat <<'EOF' |\nnpm i x\nEOF\nbash")
        pipe = script.commands[0]
        self.assertIsInstance(pipe, shparse.Pipeline)
        self.assertEqual([c.words[0].literal() for c in pipe.commands], ["cat", "bash"])

    def test_inner_substitution_end(self):
        # make_here_document: строка, начатая терминатором, с `)` (у `$(`, `<(`, `>(`) или `}` (у `${ `, `${|`) в
        # остатке кончает тело heredoc внутри подстановки; подоболочка и группа знак не меняют.
        for text in ['echo "$(cat <<E\nx\nE) b"; npm i x', "echo $(cat <<E\nx\nE); npm i x",
                     "echo $(cat <<E\nx\nEzz y); npm i x", 'echo "${ cat <<E\nx\nE} b"; npm i x',
                     "echo ${ cat <<E\nx\nE }; npm i x", "x=$(cat <<-E\n\tx\n\tE ); npm i x",
                     "x=${ { cat <<E\nx\nE}\nnpm i x\nE\n} }", 'echo "${ (cat <<E\nx\nE foo}\n); npm i x; }"',
                     "cat <(cat <<E\nx\nE)\nnpm i x"]:
            with self.subTest(text):
                self.assertTrue(runs(text))
        for text in ['echo "$(cat <<E\nx\nE ); npm i x; ) b"', "cat <<E\nx\nE); npm i x\nE",
                     "x=$(cat <<E\nx\nE}\nnpm i x\nE\n)", "x=$( (cat <<E\nx\nE}\nnpm i x\nE\n) )",
                     "(cat <<E\nx\nE)\nnpm i x\nE\n)", "x=${ (cat <<E\nx\nE)\nnpm i x\nE\n); }",
                     "x=${ echo $(cat <<E\nx\nE}\nnpm i x\nE\n); }", "x=$( echo ${ cat <<E\nx\nE)\nnpm i x\nE\n}; )"]:
            with self.subTest(text):
                self.assertFalse(runs(text))

    def test_in_substitution_body(self):
        # Тело heredoc в подстановке — данные; имя команды из вывода подстановки видно по тексту тела.
        script = shparse.parse("x=${ $(cat <<E\nnpm i x\nE\n); }")
        docs = [n for n in shparse.walk(script) if type(n) is shparse.Heredoc]
        self.assertEqual([d.body_text for d in docs], ["npm i x\n"])
        self.assertFalse(runs("x=${ $(cat <<E\nnpm i x\nE\n); }"))

    def test_terminator_with_substitution(self):
        # bash сравнивает строку с текстом слова, где подстановка напечатана print_comsub; саму подстановку
        # терминатора не исполняет. Печать проверена bash 5.3.20 (предупреждение о конце ввода называет ожидаемый
        # терминатор).
        printed = {"$(b)": "$(b)", "$( b )": "$(b)", "$(b;c)": "$(b; c)", "$(b;)": "$(b)", "$(b|c)": "$(b | c)",
                   "$(b&&c||d)": "$(b && c || d)", "$(b&c)": "$(b & c)", "$(b;c&)": "$(b; c &)", "$(<b)": "$(< b)",
                   "$(b 1>c)": "$(b > c)", "$(b 01>c)": "$(b > c)", "$(b 0<>c)": "$(b <> c)",
                   "$(b 3<<<c)": "$(b 3<<< c)", "$(b &>>c)": "$(b &>> c)", "$(x=1 >c b d)": "$(x=1 b d > c)",
                   "$(b;\nc)": "$(b; c)", "$(b &&\nc)": "$(b && c)", "$(b |\nc)": "$(b | c)", "$(b\n)": "$(b)",
                   "$(b \"c\")": "$(b \"c\")", "\"$(b \"c\")\"": "$(b c)", "x\"$(b \"c\")\"": "x$(b c)",
                   "$(b $'\\x41')": "$(b 'A')", "$(b c\\\nd)": "$(b cd)", "${ b;}": "${ b; }",
                   "${ b & }": "${ b & }", "${| b; }": "${|b; }", "${|b;c;}": "${|b; c; }", ">(b;c)": ">(b; c)",
                   "x<(b)y": "x<(b)y", "${x:-$(b;c)}": "${x:-$(b; c)}", "$(($(b;c)))": "$(($(b; c)))",
                   "$(b \"$(c;d)\")": "$(b \"$(c; d)\")", "$(b <(c;d))": "$(b <(c; d))", "`b;c`": "`b;c`"}
        for word, term in printed.items():
            with self.subTest(word):
                docs = nodes("cat <<" + word + "\nq", shparse.Heredoc)
                self.assertEqual(docs[0].term, term)
        for text in ["cat <<$(b)\nq\n$(b)\nnpm i x", "cat <<$(b;c)\nq\n$(b; c)\nnpm i x",
                     "cat <<\"$(b)\"\nq\n$(b)\nnpm i x", "cat <<x$(b)\nq\nx$(b)\nnpm i x",
                     "cat <<${ b; }\nq\n${ b; }\nnpm i x", "cat <<x<(b)\nq\nx<(b)\nnpm i x",
                     "cat <<$(<b)\nq\n$(< b)\nnpm i x"]:
            with self.subTest(text):
                self.assertEqual(cmds(text), [["cat"], ["npm", "i", "x"]])
        # Строка не в напечатанном виде тело не кончает.
        self.assertEqual(cmds("cat <<$(b;c)\nq\n$(b;c)\nnpm i x"), [["cat"], ["b"], ["c"]])

    def test_terminator_funsub_newline(self):
        # Перевод строки перед `}` у `${ …}`, `${|…}` bash печатает `\n }` (was_newline в parse_comsub), `;` перед
        # ним — нет. Печать проверена bash 5.3.20.
        printed = {"${ b\n}": "${ b\n }", "${|b\n}": "${|b\n }", "${ b &\n}": "${ b &\n }",
                   "${ b;\n}": "${ b\n }", "${ b; c\n}": "${ b; c\n }", "${ b && c\n}": "${ b && c\n }",
                   "${ b\n\n}": "${ b\n }", "${ b;  \n}": "${ b\n }", "${ b;\\\n}": "${ b; }"}
        for word, term in printed.items():
            with self.subTest(word):
                docs = nodes("cat <<" + word + "\nq", shparse.Heredoc)
                self.assertEqual(docs[0].term, term)
        # Терминатор с переводом строки bash сравнивает с одной строкой: тело — до конца ввода.
        script = shparse.parse("cat <<${ b\n}\nq\n${ b\n }\nnpm i x")
        self.assertEqual((cmds("cat <<${ b\n}\nq\n${ b\n }\nnpm i x"), script.error), ([["cat"], ["b"]], None))
        for text in ["cat <<'a\nb'\nx\na\nb\nnpm i x",
                     "cat <<a\"\n\"b\nx\na\nb\nnpm i x", "cat <<-'a\nb'\nx\na\nb\nnpm i x"]:
            with self.subTest(text):
                script = shparse.parse(text)
                self.assertEqual((cmds(text), script.error), ([["cat"]], None))

    def test_terminator_quotes_top_level(self):
        # W_QUOTED — за кавычками и `\\` на верхнем уровне слова; внутри `${…}`, `$((…))`, `$[…]`, `` `…` ``,
        # `$(…)` они терминатор в кавычках не делают: тело раскрывается, терминатор — без снятия кавычек.
        # Проверено bash 5.3.20.
        for word in ['${x:-"a"}', '$((1+"2"))', '`b "c"`', '$[1+"2"]', "${x#\\a}", '$(echo "a")']:
            with self.subTest(word):
                doc = nodes("cat <<" + word + "\nq", shparse.Heredoc)[0]
                self.assertEqual((doc.term, doc.quoted), (word, False))
                self.assertEqual(cmds("cat <<" + word + "\nq\n" + word + "\nnpm i x"), [["cat"], ["npm", "i", "x"]])
                self.assertTrue(runs("cat <<" + word + "\n$(npm i x)\n" + word))
        quoted = {"$'a'": "a", '$"a"': "a", 'a"b"': "ab", "a\\b": "ab", '"$(b)"': "$(b)", "`b`\\x": "`b`x"}
        for word, term in quoted.items():
            with self.subTest(word):
                doc = nodes("cat <<" + word + "\nq", shparse.Heredoc)[0]
                self.assertEqual((doc.term, doc.quoted), (term, True))
        doc = nodes("cat <<${x:-$'a'}\nq", shparse.Heredoc)[0]
        self.assertEqual((doc.term, doc.quoted), ("${x:-'a'}", False))

    def test_terminator_print_unknown(self):
        # Печать тела, которую разбор не повторяет (комментарий, `[[ … ]]`, `((…))`, coproc): тело кончить нечем —
        # ошибка вида INTERNAL, а не тело до конца ввода.
        for word in ["$(b # z\n)", "$(b # z\nc)", "$( )", "$([[ b ]])", "$( ((1)) )", "$(coproc b)"]:
            with self.subTest(word):
                script = shparse.parse("cat <<" + word + "\nq\nnpm i x")
                self.assertEqual(script.error, shparse.SyntaxIssue(0, True, shparse.INTERNAL))

    def test_terminator_print_compound(self):
        # `( … )`, `{ …; }`, `!`, `time`, `|&` (печать — `2>&1 |`), `<&`, `>&` и `{имя}` перед оператором: печать
        # повторяется. Печать проверена declare -f bash 5.3 (зерно 2, глубина 3, номер 14404: `$(>&\\')` и
        # `${|(:)|4⏎}` в терминаторе давали ошибку INTERNAL, и `$(touch P9)` той же команды терялся).
        printed = {"$(b|&c)": "$(b 2>&1 | c)", "$(b 2>&1)": "$(b 2>&1)", "$(! b)": "$(! b)",
                   "$({ b; })": "$({ b; })", "$(b {x}>c)": "$(b {x}> c)", "$(<&3)": "$(0<&3)", "$(>&-)": "$(1>&-)",
                   "$(<&-)": "$(0>&-)", "$(2>&3-)": "$(2>&3-)", "$(>&x)": "$(>&x)", "$(4>&x-)": "$(4>&x-)",
                   "$(<&007)": "$(0<&7)", "$({fd}>&2)": "$({fd}>&2)", "$(a {fd}<&-)": "$(a {fd}>&-)",
                   "$(a >&2 <&0)": "$(a 1>&2 0<&0)", "$(a 1>&2x)": "$(a >&2x)", "$(a 0<&x)": "$(a <&x)",
                   "$(>&\\')": "$(>&\\')", "$( (:) )": "$( ( : ))", "$({ a & })": "$({ a & })",
                   "$( ( a; b ) )": "$( ( a; b ))", "$( (a) >x )": "$( ( a ) > x)", "$({ a; } 2>&1)": "$({ a; } 2>&1)",
                   "$( (a)|&b )": "$( ( a ) 2>&1 | b)", "$(time (a))": "$(time ( a ))", "${ (a) }": "${ ( a ); }",
                   "$(! time -p a | b)": "$(time -p ! a | b)", "$(a >x |& b 2>y)": "$(a > x 2>&1 | b 2> y)",
                   "$(a>&'3'-)": "$(a 1>&'3'-)", "$(a>&3-)": "$(a 1>&3-)"}
        for word, term in printed.items():
            with self.subTest(word):
                docs = nodes("cat <<" + word + "\nq", shparse.Heredoc)
                self.assertEqual(docs[0].term, term)
                self.assertEqual(cmds("cat <<" + word + "\nq\n" + term + "\nnpm i x"), [["cat"], ["npm", "i", "x"]])
        # Составную команду на нескольких строках и heredoc bash печатает с переводом строки: такой терминатор не
        # совпадает ни с одной строкой, тело — до конца ввода (проверено bash 5.3 с `touch`).
        for word in ["$(if b; then c; fi)", "$(while :; do :; done)", "$(f() { a; })", "$(case a in b) c;; esac)",
                     "$(cat <<E\nE\n)", "${|(:)|4\n}"]:
            with self.subTest(word):
                text = "cat <<" + word + "\nq\nnpm i x"
                self.assertNotIn(["npm", "i", "x"], cmds(text))
                self.assertIsNone(shparse.parse(text).error)
        self.assertIn(["touch", "P9"], cmds("<<''\"``\"$(>&\\') $(touch P9)"))

    def test_terminator_print_newline(self):
        # Команды тела на разных строках print_comsub печатает через один `\n`, без отступов и пустых строк.
        # Печать проверена bash 5.3.
        printed = {"$(b\nc)": "$(b\nc)", "$(b\n\nc)": "$(b\nc)", "$(b\nc;d)": "$(b\nc; d)",
                   "$(b|c\nd)": "$(b | c\nd)", "${ b\nc; }": "${ b\nc; }", "$(b\nc &)": "$(b\nc &)",
                   "$(b $(c\nd))": "$(b $(c\nd))", "$(\nb\n\tc\n)": "$(b\nc)", "${ b\nc &\n}": "${ b\nc &\n }",
                   "${|b\nc;}": "${|b\nc; }", "x<(b\nc)y": "x<(b\nc)y", "$(b && c\nd || e)": "$(b && c\nd || e)"}
        for word, term in printed.items():
            with self.subTest(word):
                docs = nodes("cat <<" + word + "\nq", shparse.Heredoc)
                self.assertEqual(docs[0].term, term)
        # Терминатор с переводом строки bash сравнивает с одной строкой: тело — до конца ввода.
        script = shparse.parse("cat <<$(b\nc)\nq\nnpm i x")
        self.assertEqual((cmds("cat <<$(b\nc)\nq\nnpm i x"), script.error), ([["cat"]], None))

    def test_heredoc_limit(self):
        # Больше HEREDOC_MAX (16) heredoc в одной команде bash не принимает.
        self.assertIsNone(shparse.parse("cat" + " <<E" * 16 + "\n" + "E\n" * 16).error)
        self.assertTrue(shparse.parse("cat" + " <<E" * 17 + "\n" + "E\n" * 17).error.fatal)


class KeywordsTest(unittest.TestCase):
    """Зарезервированные слова только в позиции команды (reserved_word_acceptable), `case`, `[[ ]]`, функции,
    `coproc`, `time`, `!`."""

    def test_position(self):
        self.assertEqual(cmds("echo if then fi"), [["echo", "if", "then", "fi"]])
        self.assertEqual(cmds("x=1 if"), [["if"]])
        self.assertEqual(cmds("\"if\" a; \\if b"), [["if", "a"], ["if", "b"]])
        self.assertTrue(shparse.parse("echo a; fi").error.fatal)
        self.assertEqual(cmds("{ echo }; }"), [["echo", "}"]])
        self.assertEqual(cmds("for i in do done; do echo $i; done"), [["echo", None]])

    def test_case(self):
        self.assertEqual(cmds("case a in esac) echo;; esac"), [])
        self.assertTrue(shparse.parse("case a in esac) echo;; esac").error.fatal)
        self.assertEqual(cmds("case if in if|then) npm i x;; (fi) y;;& *) z;& esac"),
                         [["npm", "i", "x"], ["y"], ["z"]])
        self.assertEqual(cmds("case a in\na)\nnpm i x\n;;\nesac"), [["npm", "i", "x"]])
        self.assertEqual(cmds("case a in a) ;; b) esac"), [])
        self.assertIsNone(shparse.parse("case a in esac").error)
        self.assertTrue(runs("echo $(case a in (a) npm i x;; esac)"))

    def test_cond(self):
        self.assertTrue(runs("[[ -z $(npm i x) ]]"))
        self.assertTrue(runs("[[ a == a && -z $(npm i x) ]]"))
        self.assertTrue(runs("[[ ( -z $(npm i x) ) ]]"))
        self.assertTrue(runs("[[ ! ! a ]] && npm i x"))
        self.assertTrue(runs("[[ a &&\n b ]] && npm i x"))
        self.assertTrue(shparse.parse("[[ a\n]] && npm i x").error.fatal)
        self.assertTrue(shparse.parse("a | ! b").error.fatal)
        self.assertTrue(runs("[[ a < b ]] && npm i x"))
        self.assertTrue(shparse.parse("[[ ]]").error.fatal)
        self.assertTrue(shparse.parse("[[ a b ]]").error.fatal)
        self.assertEqual(cmds("[[ if ]] && echo fi"), [["echo", "fi"]])
        self.assertTrue(shparse.parse("[[ if ]] && fi").error.fatal)

    def test_functions(self):
        for text in ["f() { npm i x; }", "f()\n{ npm i x; }", "function f { npm i x; }", "function f() ( npm i x )",
                     "function f\n{ npm i x; }", "f() if true; then npm i x; fi", "f() { npm i x; } >log"]:
            with self.subTest(text):
                script = shparse.parse(text)
                self.assertIsNone(script.error)
                self.assertIsInstance(script.commands[0], shparse.Function)
                self.assertTrue(runs(text))
        self.assertTrue(shparse.parse("f() npm i x").error.fatal)
        self.assertTrue(shparse.parse("echo (x)").error.fatal)

    def test_coproc_time_bang(self):
        self.assertEqual(cmds("coproc npm i x"), [["npm", "i", "x"]])
        self.assertEqual(cmds("coproc C { npm i x; }"), [["npm", "i", "x"]])
        self.assertEqual(cmds("coproc C npm i x"), [["C", "npm", "i", "x"]])
        self.assertEqual(cmds("time -p -- npm i x"), [["npm", "i", "x"]])
        self.assertEqual(cmds("echo | time x"), [["echo"], ["time", "x"]])
        self.assertEqual(cmds("! ! npm i x"), [["npm", "i", "x"]])
        self.assertIsNone(shparse.parse("!\ntime\n! ;").error)
        self.assertEqual(shparse.parse("! npm i x").commands[0].bang, True)

    def test_redirections(self):
        script = shparse.parse("a 2>&1 >&- {fd}<x 3<<<y &>z &>>w 4<>v >|u")
        redirs = script.commands[0].redirs
        self.assertEqual([(r.op, r.fd, r.target.literal()) for r in redirs],
                         [(">&", "2", "1"), (">&", None, "-"), ("<", "{fd}", "x"), ("<<<", "3", "y"),
                          ("&>", None, "z"), ("&>>", None, "w"), ("<>", "4", "v"), (">|", None, "u")])
        self.assertTrue(shparse.parse("echo > 2>x").error.fatal)
        self.assertTrue(shparse.parse("echo >").error.fatal)
        self.assertEqual(cmds(">f x=1 npm i x"), [["npm", "i", "x"]])
        self.assertEqual(shparse.parse(">f x=1 npm").commands[0].assigns[0].literal(), "x=1")


class SyntaxErrorTest(unittest.TestCase):
    """Фатальная ошибка: команды до неё — в дереве, её команда и следующие — нет. Ошибка скобок присваивания
    массива: команда и остаток физической строки отброшены, чтение — со следующей строки (bash 5.3)."""

    def test_fatal(self):
        script = shparse.parse("touch A\nif then fi\ntouch B")
        self.assertEqual(script.error, shparse.SyntaxIssue(11, True))
        self.assertEqual(cmds("touch A\nif then fi\ntouch B"), [["touch", "A"]])
        self.assertEqual(cmds("touch A; )\ntouch B"), [])
        self.assertEqual(cmds("echo $(touch A; if)\ntouch B"), [])
        for text in ["echo )", ")", "fi", "done", "esac", "then", ";;", "| true", "&& true", "( )", "{ }", "echo (",
                     "if true; then a", "echo 'a", 'echo "a', "echo $(true", "case a in", "echo a >", "cat <<",
                     "function", "while", "echo `a", "echo ${u:-", "for in; do", "echo $(( 1", "x=(a", "a &&", "a |",
                     "a ||"]:
            with self.subTest(text):
                self.assertIsNotNone(shparse.parse(text).error)

    def test_array_error(self):
        script = shparse.parse("touch A\nx=( a ; )\ntouch B")
        self.assertEqual(script.error, shparse.SyntaxIssue(14, False))
        self.assertEqual(script.dropped, [(8, 18)])
        self.assertEqual(cmds("touch A\nx=( a ; )\ntouch B"), [["touch", "A"], ["touch", "B"]])
        self.assertEqual(cmds("x=( a ; ) <<E\ntouch B\nE"), [["touch", "B"], ["E"]])
        # Heredoc до ошибки на той же строке: bash сбрасывает ожидание тела, строки тела — команды.
        self.assertEqual(cmds("cat <<E; x=( a ; )\ntouch B\nE\ntouch C"), [["touch", "B"], ["E"], ["touch", "C"]])
        self.assertEqual(cmds("touch A; x=( a\n; touch B )\ntouch C"), [["touch", "C"]])
        self.assertEqual(shparse.parse("touch A; x=( a\n; touch B )\ntouch C").dropped, [(0, 27)])
        # Ошибка в скобках внутри подстановки: отбрасывается и строка, на которой bash её нашёл.
        self.assertEqual(cmds("echo $(x=( a ; ) ; touch A)\ntouch B\ntouch C )"), [["touch", "B"]])
        for bad in ["; ", " | ", " & ", " && ", " >/dev/null ", " (x) ", " <<E ", " ;; ", "<(", ">("][:-2]:
            with self.subTest(bad):
                self.assertEqual(shparse.parse("a=(x" + bad + "y)\ntouch B").error.fatal, False)
        self.assertIsNone(shparse.parse("a=(x <(y) >(z))").error)
        self.assertIsNone(shparse.parse("a=(\nx # c\n[k]=v\n)").error)

    def test_dropped_lines_then_fatal(self):
        # Фатальная ошибка важнее: error — она, отброшенные строки — в dropped.
        script = shparse.parse("x=(;)\ny=(;)\n)\ntouch A")
        self.assertEqual(script.error, shparse.SyntaxIssue(12, True))
        self.assertEqual(script.dropped, [(0, 6), (6, 12)])
        self.assertEqual(cmds("x=(;)\ny=(;)\n)\ntouch A"), [])

    def test_expansion_errors_stay_in_body(self):
        # Подстановки, которые bash разбирает при раскрытии (тело heredoc, `` `…` ``, `'…'` арифметики): ошибка —
        # в теле подстановки, найденное до неё остаётся, разбор команды продолжается.
        text = "cat <<E\n$(npm i x)\n$(if\nE\nnpm i y"
        script = shparse.parse(text)
        self.assertIsNone(script.error)
        self.assertEqual(cmds(text), [["cat"], ["npm", "i", "x"], ["npm", "i", "y"]])
        failed = [n for n in shparse.walk(script) if type(n) is shparse.Sub and n.body.error is not None]
        self.assertEqual(len(failed), 1)
        self.assertTrue(runs("cat <<E\n$[ $(npm i x) $( true #\n) ]\nE"))


class DepcheckFormsTest(unittest.TestCase):
    """Формы, где самописный лексер depcheck ошибался (context/architecture.md, «Детектор зависимостей»;
    context/deferred/depcheck-lexer-losses.md; классы tests/test_depcheck.py) — деревом: исполняется ли `npm i x`
    командой."""

    RUNS = [
        # depcheck-lexer-losses.md
        "true;#'\nnpm i x", "echo $(case a in *) npm i x;; esac)", "sh <<'E'\necho \"\nE\nnpm i x",
        "bash <<E\n: '$(npm i x)'\nE", 'echo "${HOME#<(npm i x)}"', "echo \"${u-'\"'}\"\nnpm i x",
        "cat <<E; echo $(:\nnpm i x)\nE", "(( 1 #x )); npm i x", "echo $(true)${u-$(npm i x)}",
        "cat <<E;printf x | bash\n'$(npm i x)'\nE", "(( $(cat <<E\n'\nE\n) )); npm i x",
        # architecture.md
        'echo "$((echo \'))\' ) ; npm i x)"', "echo \"$(( '$(npm i x)' ))\"", 'echo "$((npm i x) )"',
        "echo ${x:-<(npm i x)}", '[ -n "$(npm i x)" ]', "x=${ { cat <<E\nx\nE}\nnpm i x\nE\n}",
        "echo `'` && npm i x", "echo `echo a #b`; npm i x", "echo \"a `echo 'it\"s'` c\"; npm i x", "np\\\nm i x",
        'gh pr create --body "$(printf "a"; npm i x)"', "a[\"]\"]=1; npm i x", "a[b[1]]=$(npm i x)",
        "a[${x:-]}]=1; npm i x",
        # DoubleDollarTest
        "x=$${ npm i x }", "echo $${a; npm i x }", "echo $$$${a; npm i x }", 'echo "$$(cat <<E)"\nnpm i x\nE',
        # EscapedBlankCommentTest
        "echo \\ #$(npm i x)", "echo \\\t#$(npm i x)", 'echo "$(echo \\ #)"; npm i x', "x=\\\n#$(npm i x)",
        # ArrayAssignmentHeredocTest
        "x=(<<E\nnpm i x\nE\n)", "x+=(<<E)\nnpm i x", "declare -a x=(<<E)\nnpm i x", "a[1]=(<<E)\nnpm i x",
        "echo $(x=(<<E)\nnpm i x\n)", "x=(( `cat <<E\nnpm i x\nE\n`)", "x=(a (b) `cat <<E\nnpm i x\nE\n`)",
        "echo \"${\ttime echo q & x=(1; echo '}' || }\"\nnpm i x", "cat <<E; x=( a | b )\nnpm i x\nE",
        'x=( a <<<b ) "$(cat <<E\nnpm i x\nE\n)"', "x=( #]\r; #(${\n;; esac`cat <<E\nnpm i x\n",
        "x=(\n|;; esaccat <<'E'\nnpm i x", 'x=( a\n(b) "$(cat <<E\nnpm i x\nE\n)" )', 'x=( "a\nb" ; <<E\nnpm i x\nE\n)',
        # FunctionSubstitutionTest
        "echo $(( '${ npm i x; }' ))", 'echo "${ echo a <(echo b) }; npm i x; }"', 'echo "${ x=1 }; npm i x; }"',
        'echo "${ echo a >& }; npm i x; }"', 'echo "${ echo a # }\nnpm i x; }"', 'echo "${ [[ a &&\n b ]] }"; npm i x',
        # HeredocInSubstitutionEndTest
        'echo "a ${| { echo b; }\necho fi} | cat <<E\n}\nE }|| } b"x; npm i x',
        'echo "${\nfunction f { echo g; } || cat <<E\n}\nE\\ & }"; npm i x', "echo \"${ cat <<'E'\nE}${ npm i x; }\"",
        "${ cat <<E\nnpm i x \\${| [[ [ ${| \nE)]=then \"\nE}fi\nnpm i x",
        # HeredocLineJoinTest
        "echo ${ cat <<E\nE\\\n}; npm i x\nE\n}", "x=$(cat <<E\nE\\\n)\nnpm i x\nE\n)", "x=$(cat <<E\nE \\\n)\nnpm i x",
        "cat <<E\n${| echo \"}\" || echo a # }; echo \\\n}\nnpm i x & (echo d) & }; npm i x\nE",
        "x=$(cat <<E\nE)\\\n; npm i x\nE\n)", 'echo "$(cat <<E\nE\\\n)"; npm i x',
        # SubscriptWordQuotesTest
        'a[ "$(npm i x)" ]', "a['$(npm i y)' \"$(npm i x)\"]", "a['x' $(( '$(npm i x)' ))]",
        "x=1; a[${x:'$(npm i x)'}]",
    ]
    DATA = [
        "cat <<'E'\nnpm i x\nE", "echo '$(npm i x)'", 'echo "${x:-<(npm i x)}"', "echo $[ <(npm i x) ]",
        "git commit -m \"fix `echo \"a; npm i x\"` text\"", "echo \"$$(npm i x)\"", "echo \\\\ #$(npm i x)",
        "x=( <(cat <<E\nnpm i x\nE\n) )", 'x=( a "$(cat <<E\nnpm i x\nE\n)" )', "x=(\na\n) <<E\nnpm i x\nE",
        "[ '$(npm i x)' ]", "[[ '`npm i x`' == a ]]", "a['$(npm i x)' \"y\"]", "echo \"${ echo 'npm i x'; }\"",
        'echo "$${ npm i x; }"', "cat <<E\nE\\\\\\\n\nnpm i x",
    ]

    def test_runs(self):
        for text in self.RUNS:
            with self.subTest(text):
                self.assertTrue(runs(text))

    def test_data(self):
        for text in self.DATA:
            with self.subTest(text):
                self.assertFalse(runs(text))


class LinearityTest(unittest.TestCase):
    """Разбор и обход линейны по длине текста на каждом виде вложенности и heredoc; глубина 10⁴ — без
    RecursionError (рекурсии Python по глубине входа нет)."""

    KINDS = {
        "comsub": lambda n: "echo " + "$(" * n + "a" + ")" * n,
        "comsub_dq": lambda n: "echo " + '"$(' * n + "a" + ')"' * n,
        "param": lambda n: "echo " + "${x:-" * n + "a" + "}" * n,
        "param_dq": lambda n: 'echo "' + "${x:-" * n + "a" + "}" * n + '"',
        "funsub": lambda n: "echo " + "${ " * n + "a" + "; }" * n,
        "procsub": lambda n: "cat " + "<(" * n + "a" + ")" * n,
        "backquote_seq": lambda n: "echo " + "`a`" * n,
        "subshell": lambda n: "( " * n + "a" + " )" * n,
        "group": lambda n: "{ " * n + "a" + "; }" * n,
        "if": lambda n: "if a; then " * n + "b" + "; fi" * n,
        "while": lambda n: "while a; do " * n + "b" + "; done" * n,
        "case": lambda n: "case a in a) " * n + "b" + " ;; esac" * n,
        "function": lambda n: "f() { " * n + "a" + "; }" * n,
        "arith_parens": lambda n: "echo $((" + "(" * n + "1" + ")" * n + "))",
        "arith_nest": lambda n: "echo " + "$(( " * n + "1" + " ))" * n,
        "arith_cmd": lambda n: "((" + "(" * n + "1" + ")" * n + "))",
        "dparen_subshell": lambda n: "((" * n + "a" + ") )" * n,
        "cond_parens": lambda n: "[[ " + "( " * n + "a" + " )" * n + " ]]",
        "cond_bang": lambda n: "[[ " + "! " * n + "a ]]",
        "cond_and": lambda n: "[[ " + "a && " * n + "a ]]",
        "bang": lambda n: "! " * n + "a",
        "pipeline": lambda n: "a | " * n + "a",
        "andor": lambda n: "a && " * n + "a",
        "lines": lambda n: "a b c\n" * n,
        "dq": lambda n: 'echo "' + "a $x " * n + '"',
        "words": lambda n: "echo " + "a " * n,
        "continuation": lambda n: "echo " + "a \\\n" * n,
        "comments": lambda n: "# c\n" * n,
        "array_errors": lambda n: "x=( a ; )\n" * n,
        "array_words": lambda n: "a=(" + "x " * n + ")",
        "sq_arith": lambda n: "echo $(( " + "'$(a)' + " * n + "1 ))",
        "regex_parens": lambda n: "[[ a =~ " + "(" * n + "a" + ")" * n + " ]]",
        "unclosed": lambda n: "echo " + "$(" * n,
        "subscript_param": lambda n: "a[" + "${x:-" * n + "]" + "}" * n + "]=1 b",
        "subscript_nest": lambda n: "a[" + "${x[" * n + "]" + "]}" * n + "]=1 b",
        "subscript_dq": lambda n: "a[" + '"${x:-' * n + "]" + '}"' * n + "]=1 b",
        # Конец `$((…)` при раскрытии — за комментарием: вложенные уровни в нём не читаются, концы вложенных
        # `$((` запоминаются, тексты кусков не снимаются при повторной сборке (было время по квадрату глубины).
        "arith_comment": lambda n: "echo " + "$(( #" * n + "x\n" + "))" * n,
        "arith_comment_dq": lambda n: 'echo "' + "$(( #" * n + "x\n" + "))" * n + '"',
        "arith_comment_heredoc": lambda n: "cat <<E\n" + "$(( #" * n + "x\n" + "))" * n + "\nE",
        "arith_comment_array": lambda n: "a=(" + "$(( #" * n + "x\n" + "))" * n + ")",
        "arith_comment_paren": lambda n: "echo " + "$(( #)" * n + "x\n" + "))" * n,
        "arith_param_paren": lambda n: "echo " + "$(( ${x:-" * n + ")" + "}))" * n,
    }
    HEREDOCS = {
        "many": lambda n: "cat <<E\nx\nE\n" * n,
        "long_body": lambda n: "cat <<E\n" + "x $(a)\n" * n + "E\n",
        "quoted_body": lambda n: "cat <<'E'\n" + "x $(a)\n" * n + "E\n",
        "joined_body": lambda n: "cat <<E\n" + "x\\\n" * n + "E\n",
        "strip_tabs": lambda n: "cat <<-E\n" + "\tx\n" * n + "\tE\n",
        "on_one_line": lambda n: ("cat" + " <<E" * 16 + "\n" + "E\n" * 16) * (n // 16 + 1),
        "in_comsub": lambda n: "echo " + "$(" * n + "cat <<E\nx\nE\n" + ")" * n,
        "comsub_end": lambda n: "echo $(cat <<E\n" + "E x\n" * n + "E)",
        "funsub_end": lambda n: 'echo "${ ' + "cat <<E\nE };" * n + '"',
        "terminator_lines": lambda n: "x=$(cat" + " <<-E" * 16 + "\n" + "E" * n + ")\n)\n",
        "subshell_in_funsub": lambda n: "x=${ " + "(" * n + "cat <<E\n" + "E)\n" * n + ")" * n + "}",
        "body_after_comsubs": lambda n: "cat <<E; " + "echo $(cat <<F)\nF\n" * n + "E\n",
        "array_error_bodies": lambda n: "x=( a ;" + " <<E" * 16 + "\nx\n" + "E\n" * n,
        # heredoc в подстановке в теле heredoc, n уровней: тело внутреннего bash перечитывает на каждом уровне,
        # здесь — поиском строки терминатора без прохода Python по строкам.
        "chain_in_body": lambda n: ("cat <<X\n" + "".join(f"$(cat <<E{i}\n" for i in range(n)) + "x\n"
                                    + "".join(f"E{i}\n)\n" for i in reversed(range(n))) + "X\n"),
    }

    # Виды, где у разбора есть вложенность по входу (стек генераторов растёт с глубиной).
    DEEP = ["comsub", "comsub_dq", "param", "funsub", "procsub", "group", "if", "case", "function", "arith_nest",
            "dparen_subshell", "cond_parens", "cond_bang", "bang", "unclosed", "in_comsub", "subshell_in_funsub",
            "subscript_param", "subscript_nest", "subscript_dq"]

    @staticmethod
    def walk_all(text):
        script = shparse.parse(text)
        shparse.simple_commands(script)
        list(shparse.walk(script))

    def check(self, make, n=250):
        small, large = make(n), make(4 * n)
        assert_linear(self, lambda: self.walk_all(small), lambda: self.walk_all(large))

    @mock.patch.object(shparse, "_STACK_MAX", 10 ** 9)  # линейность разбора, а не предел (DepthLimitTest)
    def test_nesting(self):
        for name, make in self.KINDS.items():
            with self.subTest(name):
                self.check(make)

    @mock.patch.object(shparse, "_STACK_MAX", 10 ** 9)  # линейность разбора, а не предел (DepthLimitTest)
    def test_heredocs(self):
        for name, make in self.HEREDOCS.items():
            with self.subTest(name):
                self.check(make)

    def test_param_text_memory(self):
        # Текст `${…}` собирается по требованию (Param.text): строки уровней не повторяют текст вложенных `${…}`,
        # память разбора линейна по глубине. Глубины — под пределом стека разбора (_STACK_MAX: около 2000 уровней).
        def peak(n):
            text = "echo " + "${a:-" * n + "x" + "}" * n
            tracemalloc.start()
            try:
                script = shparse.parse(text)
                size = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertIsNone(script.error)
            params = [node for node in shparse.walk(script) if type(node) is shparse.Param]
            self.assertEqual(len(params), n)
            self.assertEqual(params[0].text, text[5:])
            self.assertEqual(params[-1].text, "${a:-x}")
            return size

        small, large = peak(900), peak(1800)
        self.assertLess(large, 2.5 * small)
        small, large = "echo " + "${a:-" * 450 + "x" + "}" * 450, "echo " + "${a:-" * 1800 + "x" + "}" * 1800
        assert_linear(self, lambda: shparse.parse(small), lambda: shparse.parse(large))

    def test_array_comments(self):
        # Комментарии между словами скобок массива: поиск по комментариям этих скобок, а не проход всех комментариев
        # текста на каждое слово (на 250/1000 квадратичное время не видно).
        make = lambda n: "a=(\n" + "x # c\n" * n + "); bash run.sh npm i x"  # noqa: E731
        small, large = make(1000), make(4000)
        assert_linear(self, lambda: self.walk_all(small), lambda: self.walk_all(large))

    def test_heredocs_line(self):
        small, large = "cat <<E " * 2000, "cat <<E " * 8000
        assert_linear(self, lambda: shparse.heredocs(small), lambda: shparse.heredocs(large))
        # Не закрытая до конца строки `` `…` `` с `\``: тело подстановки разбирается заново (bash читает её до
        # закрывающей обратной кавычки).
        small, large = "x=`" + "\\`" * 20000, "x=`" + "\\`" * 80000
        assert_linear(self, lambda: shparse.heredocs(small), lambda: shparse.heredocs(large))

    @mock.patch.object(shparse, "_STACK_MAX", 10 ** 9)
    def test_deep_nesting(self):
        # Глубина 10⁴ без предела стека разбора: рекурсии Python нет (предел — DepthLimitTest).
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(1000)
        kinds = {**self.KINDS, **self.HEREDOCS}
        try:
            for name in self.DEEP:
                with self.subTest(name):
                    self.walk_all(kinds[name](10000))
        finally:
            sys.setrecursionlimit(limit)
        script = shparse.parse("echo " + "$(" * 10000 + "npm i x" + ")" * 10000)
        self.assertIsNone(script.error)
        self.assertEqual(len(shparse.simple_commands(script)), 10001)


def chain(wrap, n):
    """Вложение на n уровней: wrap(текст, номер уровня) — текст уровня вокруг более глубокого."""
    text = "x"
    for i in range(n):
        text = wrap(text, i)
    return "echo " + text


class MemoryLinearityTest(unittest.TestCase):
    """Пик памяти разбора (tracemalloc) линеен по глубине вложенности под действующими пределами (_STACK_MAX, предел
    повторного раскрытия): готовые тексты уровней (Param.text, печать и skel кусков, тела heredoc, записи подстановок)
    — срезы общих строк, а не строка на каждый уровень с текстом всех более глубоких. Квадратичная память: при
    удвоении глубины пик растёт вчетверо; линейная — вдвое (запас — на шаги роста словарей и списков)."""

    EXTRA = {
        "arith_paren_chain": lambda n: "echo " + "$((x " * n + ") " * n,
        "funsub_seq": lambda n: "${ " * n + "echo x; }" * n,
        "param_open": lambda n: "echo " + "${x:- " * n,
        "param_index": lambda n: "echo " + "${a[ " * n,
        "param_offset": lambda n: "echo " + "${x:" * n,
        "param_offset_quote": lambda n: "echo " + "${a[1]:'" * n,
        "param_dq_nest": lambda n: 'echo "' + '${x:-"' * n + "a" + '"}' * n + '"',
        "param_comsub": lambda n: "echo " + "${x:-$(echo " * n + "a" + ")}" * n,
        "arith_param_nest": lambda n: "echo $(( " + "${x:-$(( " * n + "1" + " ))}" * n + " ))",
        "heredoc_in_param": lambda n: "echo " + "${x:-$(cat <<E\nx\nE\n)" * n + "}" * n,
        "array_nest": lambda n: "a=(" + "$(b=(" * n + "x" + "))" * n + ")",
        "quoted_name": lambda n: "echo " + '"$(' * n + "echo" + ')"' * n,
        "reexpand_chain": lambda n: chain(lambda s, i: "$(a=(\\'y) ; " + s + " ; b=(\\'))", n),
        # Тело heredoc с терминатором в кавычках (Lit в Heredoc.parts) на каждом уровне вложенности: `$((` читается
        # сначала арифметикой, и вложенные уровни разбираются внутри тела.
        "quoted_body_param": lambda n: chain(lambda s, i: f"${{x:-$((true) ; cat <<'E{i}'\n{s}\nE{i}\n)}}", n),
        "quoted_body_reparse": lambda n: chain(lambda s, i: f"$((true) ; cat <<'E{i}'\n{s}\nE{i}\n)", n),
    }
    SIZES = (1000, 500, 250, 120, 60, 30, 15)

    @staticmethod
    def peak(text):
        shparse.parse(text)  # кеши модулей (re) — до замера
        tracemalloc.start()
        try:
            shparse.parse(text)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_nesting(self):
        kinds = {**LinearityTest.KINDS, **LinearityTest.HEREDOCS, **self.EXTRA}
        kinds.update({"reparse_" + name: (lambda wrap: lambda n: chain(wrap, n))(wrap)
                      for name, wrap in ReparseLinearityTest.KINDS.items()})
        for name, make in kinds.items():
            with self.subTest(name):
                n = next((n for n in self.SIZES
                          if getattr(shparse.parse(make(2 * n)).error, "kind", None) != shparse.DEPTH), None)
                self.assertIsNotNone(n)
                small, large = self.peak(make(n)), self.peak(make(2 * n))
                self.assertLessEqual(large, 2.5 * small + 256 * 1024, f"глубина {n}")

    @mock.patch.object(shparse, "_STACK_MAX", 10 ** 9)  # память, а не предел (под пределом — test_nesting)
    def test_heredoc_body_chain(self):
        # Тело heredoc разбирается окном строки, где оно прочитано (_R с window), без копии: разборы тел вложенных
        # уровней (heredoc в подстановке в теле внешнего) живут на стеке все сразу, и копия тела на каждом уровне
        # давала пик по квадрату глубины.
        make = LinearityTest.HEREDOCS["chain_in_body"]
        small, large = self.peak(make(960)), self.peak(make(1920))
        self.assertLessEqual(large, 2.5 * small + 256 * 1024)


class DeadlineExpired(BaseException):
    """Срок разбора истёк. Не Exception: parse переводит любое Exception в ошибку разбора и продолжил бы работу."""


@contextlib.contextmanager
def deadline(test, seconds):
    """Разбор, не кончившийся за seconds, — провал теста, а не зависание набора (SIGALRM, где он есть)."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def expired(*_):
        raise DeadlineExpired(f"разбор дольше {seconds} с")

    old = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class ReparseLinearityTest(unittest.TestCase):
    """Текст, который bash разбирает ещё раз при раскрытии (напечатанный текст `$((…) …)`, '…' в арифметике и
    остаток за ней, тело heredoc, `` `…` ``, группа `=~`, процесс-подстановка в "${…}"), содержит подстановки, уже
    разобранные при чтении: повторный разбор берёт их готовыми. Иначе каждый уровень вложенности разбирал бы все
    более глубокие заново — время и память росли бы экспоненциально по глубине (обход проверки по таймауту хука)."""

    KINDS = {
        "arith_comsub": lambda s, i: "$((true) " + s + ")",
        "arith_quote": lambda s, i: "$(( '$(echo " + s + ")' ))",
        "arith_heredoc": lambda s, i: f"$((true) ; cat <<E{i}\n" + s + f"\nE{i}\n)",
        "heredoc_arith": lambda s, i: f"$(cat <<E{i}\n$((true) " + s + f")\nE{i}\n)",
        "arith_backquote": lambda s, i: "$((true) `echo a` " + s + ")",
        "regex": lambda s, i: "$([[ a =~ ($(echo " + s + ")) ]])",
        "arith_regex": lambda s, i: "$((true) [[ a =~ ($(echo " + s + ")) ]])",
        "param_arith": lambda s, i: "${x:-$((true) " + s + ")}",
        "arith_param": lambda s, i: "$(( ${x:-$((true) " + s + ")} ))",
        "param_procsub_dq": lambda s, i: '"${x:-<(echo ' + s + ')}"',
        "arith_cond": lambda s, i: "$(( $([[ -z " + s + " ]]) ))",
        # `<((…) …)`: текст после `<(` читает parse_matched_pair, тело — разбор при раскрытии; конец вложенной
        # такой же подстановки уже нашёл parse_matched_pair внешней.
        "procsub_arith": lambda s, i: "<((echo " + s + ") )",
        "outsub_arith": lambda s, i: ">((true) " + s + ")",
        "procsub_arith_param": lambda s, i: "${x:-<((echo " + s + ") )}",
        # `((` в теле `<(((…) …)`: пару скобок и символ за ней уже нашёл parse_matched_pair внешней — подоболочка.
        "procsub_dparen": lambda s, i: "<(((echo " + s + ") ) )",
    }

    @staticmethod
    def walk_all(text):
        script = shparse.parse(text)
        shparse.simple_commands(script)
        list(shparse.walk(script))

    @mock.patch.object(shparse, "_STACK_MAX", 10 ** 9)  # линейность разбора, а не предел (DepthLimitTest)
    def test_nesting(self):
        for name, wrap in self.KINDS.items():
            small, large = chain(wrap, 200), chain(wrap, 800)
            with self.subTest(name), deadline(self, 60):
                assert_linear(self, lambda: self.walk_all(small), lambda: self.walk_all(large))

    def test_backquote_body(self):
        # Тело `` `…` `` с вложенными `$((…) …)`: разбирается один раз при раскрытии.
        make = lambda n: "echo `echo " + chain(self.KINDS["arith_comsub"], n)[5:] + "`"  # noqa: E731
        small, large = make(200), make(800)
        with deadline(self, 60):
            assert_linear(self, lambda: self.walk_all(small), lambda: self.walk_all(large))

    def test_commands_found(self):
        # Подстановки, взятые готовыми, находят те же команды, что разбор каждого уровня заново: по одной на уровень.
        forms = {
            "arith_comsub": lambda s, i: f"$((touch P{i}) ; " + s + ")",
            "arith_heredoc": lambda s, i: f"$((true) ; cat <<E{i}\n$(touch P{i})" + s + f"\nE{i}\n)",
            "heredoc_arith": lambda s, i: f"$(cat <<E{i}\n$((touch P{i}) ; " + s + f")\nE{i}\n)",
            "arith_quote": lambda s, i: f"$(( '$(touch P{i}; echo " + s + ")' ))",
            "procsub_arith": lambda s, i: f"<((touch P{i}) ; echo " + s + ")",
            "procsub_dparen": lambda s, i: f"<(((touch P{i}) ; echo " + s + ") )",
        }
        for name, wrap in forms.items():
            with self.subTest(name), deadline(self, 60):
                found = {c[1] for c in cmds(chain(wrap, 50)) if c[:1] == ["touch"]}
                self.assertEqual(found, {f"P{i}" for i in range(50)})

    # Формы генератора фаззера (зерно 2, глубина 3, номера 11368, 19048, 13083): до правки разбор шёл десятки
    # секунд и кончался MemoryError.
    FUZZ = [
        "touch P1; time : $(( ${HOME+$(v2+=$({ : && touch P2; }))a b$( time -p v3+='a b<<E'\n[[ $(printf '%s\\n"
        '\' $( touch P4)"\\`;)" a=b\nuntil (( n4++ >= 1 )); do time -p touch P8; done|f5() ( [[ a == a ]] && to'
        'uch P9 | touch P10 )\nf5) == * ]])} `for i in a; do echo a#touch P32\nprintf \'%s\\n\' \\"[ 2>/dev/nul'
        'l; done |& (echo -n "$( touch P33)${HOME}" \'touch P35}\' 2>/dev/null\nv9+=$[ ${u:-1} + 1 1 ]) ; while'
        ' touch P36 && (( n10++ < 1 )); do :; done` 1 ))\na11=({a,`{ touch P37; }`} | touch P38)',
        'touch P1 \\\n; a1=("\na b$x"\'}`\'); test 1 && a3=( \'$xtouch P5)\' <(! time (touch P6)\n[[ -z "$(time'
        ' touch P7)" ]]) $(( \'$(touch P8)\' $(a2=( % \'"touch P9touch P10\' $HOME)) ${ [[ -z "$([[ a == a ]] &'
        '& printf \'%s\\n\' \'`\' \\;;touch P12)" ]];} 1 ))) |& case \'a\' in a|b) a4+=($( touch P13 |& touch P'
        '14\ntouch P15));& esac',
        '{ false; } || echo $(( `cat <<< "\n"$( : \\()` ${u:-1} $( [[ a == b || -z $([[ -z $(touch P2 | echo \''
        ";\\;'$'\\\\touch P3`' {a,b}'}') ]]) ]]) 1 ))\n: && a1=($( touch P7 && ( touch P5\ntouch P6 ) ; v2='tou"
        "ch P8`') >/dev/null touch P9)",
    ]

    def test_fuzz_forms(self):
        for text in self.FUZZ:
            with self.subTest(text[:30]), deadline(self, 60):
                start = time.monotonic()
                self.walk_all(text)
                self.assertLess(time.monotonic() - start, 1)

    @mock.patch.object(shparse, "_STACK_MAX", 10 ** 9)  # предел работы повторного раскрытия, а не стека
    def test_reexpand_chain_limit(self):
        # Цепочка, где каждый уровень раскрывается заново по напечатанному тексту (`\\'` в скобках массива делит слова
        # при чтении и при раскрытии по-разному): каждый уровень перечитывает весь вложенный текст. Линейного разбора с
        # точностью bash нет; сверх предела _REEXPAND_BASE + _REEXPAND_PER × длина — ошибка вида DEPTH (у детектора —
        # сомнение). До предела команды каждого уровня найдены. До правки 200 уровней разбирались около 115 с.
        wrap = lambda s, i: "$(a=(\\'y) ; " + s + " ; b=(\\'))"  # noqa: E731
        script = shparse.parse(chain(wrap, 10))
        self.assertIsNone(script.error)
        self.assertEqual(len(shparse.simple_commands(script)), 31)
        for n in (200, 400, 1000):
            with self.subTest(n), deadline(self, 60):
                start = time.process_time()
                script = shparse.parse(chain(wrap, n))
                self.assertEqual((script.error.kind, script.commands), (shparse.DEPTH, []))
                self.assertLess(time.process_time() - start, 5)

    def test_arith_check_after_open_quote(self):
        # Незакрытая '…' в теле heredoc подстановки идёт в счёте chk_arithsub дальше: подстановка за ней с `\\'`
        # (её '…' кончает кавычку) не заменяется на `()`. Иначе `$((…))` признана бы арифметикой без команд.
        text = "$(('$(touch P10)'$(<<E\n''()'\nE)$(if :;then \\';fi)))"
        self.assertIn(["touch", "P10"], cmds(text))

    def test_cond_in_cond_substitution(self):
        # `[[` в подстановке внутри `[[`: лексемы печати у каждого свои (общий список давал цикл: печать
        # внутреннего включала внешнее слово с той же подстановкой — разбор не кончался).
        text = "echo $(( $([[ -z $([[ a ]]) ]]) ))"
        with deadline(self, 60):
            self.assertEqual(cmds(text), [["echo", None]])
        self.assertEqual(PrintedTextTest.printed("[[ -z $([[ a ]]) ]]"), "[[ -z $([[ a ]]) ]]")


if __name__ == "__main__":
    unittest.main()
