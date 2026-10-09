"""Сборка корпуса команд Bash из транскриптов Claude Code: tests/fixtures/bash-transcripts.jsonl.

Запуск: python3 tests/tools/build_corpus.py [--private слово,слово] [каталог транскриптов] [файл вывода]

--private — личные слова (фамилии, компания, имена закрытых проектов) через запятую: каждое вхождение без учёта
регистра заменяется на `private`. Список передаётся при запуске и в репозиторий не пишется.

Команды из транскриптов только читаются как строки и подаются в функции depcheck; они никогда не исполняются.
Вердикты (add, doubt) берутся у depcheck версии HEAD (`git show HEAD:plugin/planka/depcheck.py`), рабочее
дерево не читается. Строка с секретом выбрасывается; в stderr печатается номер и вид шаблона, но не значение.
"""
import getpass
import json
import pathlib
import re
import subprocess
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_SOURCE = pathlib.Path.home() / ".claude" / "projects"
DEFAULT_OUT = REPO / "tests" / "fixtures" / "bash-transcripts.jsonl"

# Виды секретов: (имя вида, шаблон). В отчёт идёт только имя.
SECRET_PATTERNS = [
    ("ghp_", re.compile(r"ghp_")),
    ("gho_", re.compile(r"gho_")),
    ("github_pat_", re.compile(r"github_pat_")),
    ("sk-", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("AKIA", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("xox", re.compile(r"xox[bpas]-")),
    ("BEGIN", re.compile(r"-----BEGIN")),
    ("Authorization", re.compile(r"authorization:", re.I)),
    ("password=", re.compile(r"password=", re.I)),
    ("passwd=", re.compile(r"passwd=", re.I)),
    ("token=", re.compile(r"token=", re.I)),
    ("api_key=", re.compile(r"api_key=", re.I)),
    ("secret=", re.compile(r"secret=", re.I)),
    # Длинное значение после `=` или `:`: hex или base64 без начального `/` и `.` (пути не секреты), с цифрой и буквой.
    ("long-value", re.compile(r"[=:]\s*[\"']?(?![/.~])(?=[A-Za-z0-9+/_-]*[0-9])(?=[A-Za-z0-9+/_-]*[A-Za-z])"
                              r"[A-Za-z0-9+_-][A-Za-z0-9+/_-]{39,}={0,2}")),
]

UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HOME = re.compile(r"/home/[^/\s\"'`:;|&<>()$]+")
MAC_HOME = re.compile(r"/Users/[^/\s\"'`:;|&<>()$]+")


def anonymize(text, user, private=()):
    text = UUID.sub("00000000-0000-0000-0000-000000000000", text)
    text = HOME.sub("/home/user", text)
    text = MAC_HOME.sub("/Users/user", text)
    text = EMAIL.sub("user@example.com", text)
    if user:
        text = text.replace("-home-" + user + "-", "-home-user-")
        text = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(user) + r"(?![A-Za-z0-9_])", "user", text)
    for word in private:
        text = re.sub(re.escape(word), "private", text, flags=re.I)
    return text


def secret_kinds(text):
    """Имена видов найденных секретов (не значения)."""
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def commands_of(path):
    """Команды Bash из одного транскрипта; битые строки и не-строки пропускаются."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"):
                    continue
                args = block.get("input")
                command = args.get("command") if isinstance(args, dict) else None
                if isinstance(command, str):
                    yield command


def load_head_depcheck():
    src = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:plugin/planka/depcheck.py"],
                         capture_output=True, text=True, check=True).stdout
    mod = types.ModuleType("depcheck_head")
    sys.path.insert(0, str(REPO / "plugin" / "planka"))
    exec(compile(src, "depcheck_head", "exec"), mod.__dict__)
    return mod


def dedupe(items):
    return list(dict.fromkeys(items))


def main(argv):
    private = ()
    if argv[:1] == ["--private"]:
        private = tuple(w for w in argv[1].split(",") if w)
        argv = argv[2:]
    source = pathlib.Path(argv[0]).expanduser() if argv else DEFAULT_SOURCE
    out = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    files = sorted(source.rglob("*.jsonl"))
    raw = [c for f in files for c in commands_of(f)]
    unique = dedupe(raw)
    user = getpass.getuser()
    anonymous = dedupe(anonymize(c, user, private) for c in unique)
    depcheck = load_head_depcheck()
    dropped = {}
    kept = []
    for number, command in enumerate(anonymous, 1):
        kinds = secret_kinds(command)
        if kinds:
            for kind in kinds:
                dropped[kind] = dropped.get(kind, 0) + 1
            sys.stderr.write("строка %d выброшена: %s\n" % (number, ", ".join(kinds)))
            continue
        kept.append(command)
    with open(out, "w", encoding="utf-8") as fh:
        for command in kept:
            add = depcheck.dependency_add(command)
            doubt = None if add else depcheck.dependency_doubt(command)
            fh.write(json.dumps({"command": command, "add": add, "doubt": doubt}, ensure_ascii=False) + "\n")
    sys.stderr.write("транскриптов %d; команд %d, после повторов %d, после обезличивания %d, выброшено %d, записано %d\n"
                     % (len(files), len(raw), len(unique), len(anonymous), len(anonymous) - len(kept), len(kept)))
    for kind, count in sorted(dropped.items()):
        sys.stderr.write("вид %s: %d\n" % (kind, count))


if __name__ == "__main__":
    main(sys.argv[1:])
