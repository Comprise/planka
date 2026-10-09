import json
import pathlib
import sys
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import depcheck  # noqa: E402
from tests.helpers import assert_linear  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# Корпус настоящих команд и образцов краёв: expect — add, doubt или pass (CorpusTest в test_depcheck.py).
COMMANDS = FIXTURES / "bash-commands.jsonl"
# Корпус команд из транскриптов: add и doubt — вердикты dependency_add и dependency_doubt (test_bash_corpus.py).
TRANSCRIPTS = FIXTURES / "bash-transcripts.jsonl"

# Строки корпусов, где путь над деревом расходится с записанным вердиктом: номер строки — (начало команды,
# вердикт нового пути). Каждое расхождение объяснено прогоном bash 5.3 в песочнице bashdiff (формы с `touch`).
#
# bash-commands.jsonl, вид вердикта:
COMMANDS_DIVERGENT = {
    # Слова `for … in` — не команда: раскрытие `{1..10000}` ничего не запускает; старый разбор считал сегмент
    # `for i in {1..10000}` командой и упирался в предел раскрытия (`for i in {1..3}; do :; done; touch P1` — P1).
    1485: ("for i in {1..10000}", "pass"),
    # bash 5.3 читает `a[${x:-]}]=1` присваиванием (`a[${x:-]}]=1 touch P1` создаёт P1), shparse — словом-именем
    # команды: ошибка разбора, передана координатору.
    1506: ("a[${x:-]}]=1 npm install left-pad", "pass"),
}
# bash-transcripts.jsonl: (вид, сегмент, довод) нового пути.
TRANSCRIPTS_DIVERGENT = {
    # Строка `bash -c` начинается ошибкой скобок массива `x=((`: bash отбрасывает строку вместе с `` `cat <<E ``,
    # вывод `cat` командой не становится (`x=((coproc ;{ `cat <<E` ⏎ `echo touch P1` ⏎ `E` ⏎ `` `) `` — P1 нет);
    # старый разбор видел `` `…` `` именем команды.
    279: ("R=/tmp/", ("pass", None, None)),
    # `$(( "$(…)" + 1 ))` — арифметика с подстановкой, имя команды — `echo` (`echo $(( "$(echo touch P1)" + 1 ))` —
    # P1 нет); старый разбор читал `$((` с кавычкой подоболочкой и видел вычисляемое имя.
    823: ("cd /tmp; bash -c", ("pass", None, None)),
    # `x=( $t )` — присваивание массива, `$t` не исполняется (`t='touch P1'; x=( $t )` — P1 нет); старый разбор
    # делил `(` подоболочкой.
    1246: ("cd /tmp/", ("pass", None, None)),
    # Только сегмент: ключевое слово `do` — не часть простой команды.
    338: ("cd /tmp/", ("doubt", 'env -i PATH=/usr/bin:/bin bash -c "$s" 2>&1', depcheck._WHY_COMPUTED)),
    1195: ("cd /tmp/", ("doubt", 'bash -c "$c"', depcheck._WHY_COMPUTED)),
    2131: ("cd /home/user/", ("doubt", 'bash -c "$c"', depcheck._WHY_COMPUTED)),
    # Только сегмент: команда внутри `$((…) …)` — своя простая команда дерева.
    1078: ("cd /tmp; bash -c ", ("add", "npm install evil", None)),
    # Только сегмент: тело `cat > cases1.txt <<'EOF'` кончается раньше на строке `EOF` внутри, дальше — команды;
    # путь над деревом проверяет сначала команды текста, затем тела heredoc оболочки (`bash /dev/stdin <<'EOF'`).
    6810: ("mkdir -p ", ("add", "python --check-hash-based-pycs always -m pip install requests", None)),
}


def _rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _verdict(command):
    """(вид, сегмент, довод) пути над деревом."""
    add = depcheck._tree_add(command)
    if add is not None:
        return "add", add, None
    doubt = depcheck._tree_doubt(command)
    return ("doubt", *doubt) if doubt else ("pass", None, None)


class CommandsCorpusTest(unittest.TestCase):
    """Вид вердикта на bash-commands.jsonl — как записан, кроме COMMANDS_DIVERGENT."""

    def test_corpus(self):
        rows = _rows(COMMANDS)
        self.assertGreater(len(rows), 1000)
        for number, row in enumerate(rows, 1):
            command = row["command"]
            want = row["expect"]
            if number in COMMANDS_DIVERGENT:
                prefix, want = COMMANDS_DIVERGENT[number]
                self.assertTrue(command.startswith(prefix), number)
            got = _verdict(command)[0]
            if got != want:
                with self.subTest(line=number, command=command[:200]):
                    self.fail("ожидалось %r, получено %r" % (want, got))


class TranscriptsCorpusTest(unittest.TestCase):
    """Вердикт на bash-transcripts.jsonl — сегмент и довод как записаны, кроме TRANSCRIPTS_DIVERGENT."""

    def test_corpus(self):
        rows = _rows(TRANSCRIPTS)
        self.assertGreater(len(rows), 1000)
        for number, row in enumerate(rows, 1):
            command = row["command"]
            if row["add"] is not None:
                want = ("add", row["add"], None)
            elif row["doubt"] is not None:
                want = ("doubt", *row["doubt"])
            else:
                want = ("pass", None, None)
            if number in TRANSCRIPTS_DIVERGENT:
                prefix, want = TRANSCRIPTS_DIVERGENT[number]
                self.assertTrue(command.startswith(prefix), number)
            got = _verdict(command)
            if got != want:
                with self.subTest(line=number, command=command[:200]):
                    self.fail("ожидалось %r, получено %r" % (want, got))


class NameOutputTest(unittest.TestCase):
    """Имя команды — вывод подстановки: bash исполняет вывод командой."""

    def test_doubt(self):
        for cmd in ["x=${ $(cat <<E\nnpm i x\nE\n); }", "$(cat <<E\nnpm i x\nE\n)", "$(echo npm) i x",
                    "${ printf 'npm i x'; }", "`echo npm i x`", "\"$(echo pip)\" install requests",
                    "sudo $(printf 'npm i') x", "$(cat <<E) i x\nnpm\nE"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck._tree_add(cmd))
                self.assertEqual(depcheck._tree_doubt(cmd)[1], depcheck._WHY_NAME)

    def test_no_install(self):
        # Вывод подстановки без менеджера и глагола установки: не сомнение.
        for cmd in ['"$(git rev-parse --show-toplevel)/bin/x" install', "$(npm bin)/eslint .",
                    "$(dirname \"$0\")/run.sh a b", "echo $(echo npm i x)"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck._tree_add(cmd))
                self.assertIsNone(depcheck._tree_doubt(cmd))

    def test_marker(self):
        self.assertIsNone(depcheck._tree_doubt("PLANKA_DEP_OK=1 $(echo npm) i x"))

    def test_long_text_doubt(self):
        # Текст имени длиннее _WORDS_LIMIT не разбирается: сомнение.
        cmd = "$(echo " + "a " * depcheck._WORDS_LIMIT + ") x"
        self.assertEqual(depcheck._tree_doubt(cmd)[1], depcheck._WHY_NAME)


class TreeFormsTest(unittest.TestCase):
    """Формы, которые дерево читает как bash (проверено прогоном bash с `touch` в песочнице bashdiff)."""

    def test_add(self):
        for cmd, segment in [
            # `eval --`: строка — слова за `--`.
            ("eval -- 'for i in a; do npm i x; done'", "eval -- 'for i in a; do npm i x; done'"),
            # Вывод команды в `>(…)` — вход оболочки.
            ("echo 'npm i x' > >(bash)", "echo 'npm i x' > >(bash)"),
            # Тело heredoc `cat` в процесс-подстановке — вход оболочки.
            ("bash <(cat <<E\nnpm i x\nE\n)", "cat <<E"),
            # `#` за `;` — комментарий: кавычка в нём строку не продолжает.
            ("true;#\" \nnpm i x", "npm i x"),
            # Тело heredoc в конвейере с оболочкой.
            ("cat <<E | tee /dev/null | bash\nnpm i x\nE", "npm i x"),
            ("npm i x", "npm i x"),
            ("echo \"$(npm i x)\"", "npm i x"),
            ("f() { npm i x; }", "npm i x"),
        ]:
            with self.subTest(cmd):
                self.assertEqual(depcheck._tree_add(cmd), segment)

    def test_syntax_error_recovery(self):
        # После фатальной ошибки — остаток со следующей строки и начало строки с ошибкой до неё.
        self.assertEqual(depcheck._tree_add(")\nnpm i x"), "npm i x")
        self.assertEqual(depcheck._tree_add("npm i x && )"), "npm i x")
        self.assertEqual(depcheck._tree_add("npm i x |)"), "npm i x")
        # Остаток строки за ошибкой bash не исполняет (`( ); touch P1` — P1 нет).
        self.assertIsNone(depcheck._tree_add("( ); npm i x"))

    def test_pass(self):
        for cmd in ["echo 'npm i x'", "cat <<E\nnpm i x\nE", "PLANKA_DEP_OK=1 npm i x", "x=( $t )",
                    "# npm i x", "echo $(( \"$(echo 1)\" + 1 ))"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck._tree_add(cmd))
                self.assertIsNone(depcheck._tree_doubt(cmd))

    def test_not_str(self):
        self.assertIsNone(depcheck._tree_add(None))
        self.assertIsNone(depcheck._tree_doubt(b"npm i x"))


class TreeLinearTest(unittest.TestCase):
    """Путь над деревом линеен по длине команды."""

    def test_linear(self):
        forms = {
            "nested": lambda n: "$(" * n + "npm i x" + ")" * n,
            "nested name": lambda n: "echo " + "$(" * n + "echo" + ")" * n + " i x",
            "quoted name": lambda n: "echo " + '"$(' * n + "echo" + ')"' * n,
            "funsub": lambda n: "${ " * n + "echo x; }" * n,
            "heredocs": lambda n: "cat <<E\nx\nE\n" * n,
            "shell heredoc": lambda n: "bash <<E\n" + "echo x\n" * n + "E\n",
            "pipes": lambda n: "echo a | " * n + "bash",
            "procsubs": lambda n: "cat " + "<(echo a) " * n,
            "errors": lambda n: "x; )\n" * n,
        }
        for name, form in forms.items():
            small, large = form(250), form(1000)
            with self.subTest(name):
                assert_linear(self, lambda: depcheck._tree_add(small), lambda: depcheck._tree_add(large))
                assert_linear(self, lambda: depcheck._tree_doubt(small), lambda: depcheck._tree_doubt(large))


if __name__ == "__main__":
    unittest.main()
