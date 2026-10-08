"""Окружение для тестов planka: временный каталог плагина, подмена claude, запуск скриптов."""
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
STUB_DIR = REPO / "tests" / "stub"
PLANKA_DIR = REPO / "plugin" / "planka"

sys.path.insert(0, str(PLANKA_DIR))
import comments  # noqa: E402
import common  # noqa: E402

PHILOSOPHY = """# Философия работы

## Решения

1. Правило решений один.
2. Правило решений два.

### Подраздел

3. Правило подраздела решений.

## Поведение

- Правило поведения.

## Спецификации

1. Правило спеки.

## Планы

1. Правило планов один.
2. Правило планов два.

## Модули

Каталог: {RULES}
Язык комментариев: {COMMENT_LANG}; язык документации: {DOC_LANG}.
"""

RULES = {
    "verification": "# Доказательство\n\n- Правило проверки.\n",
    "planning": "# Планирование\n\n- Правило планирования.\n",
    "subagents": "# Субагенты\n\n- Правило субагентов.\n",
    "docs": "# Документация\n\n## Что сверяется с каждой правкой кода\n\n1. Правило документации.\n\n## Инициализация проекта без документации\n\n1. Правило инициализации.\n",
    "comments": "# Комментарии\n\n- Язык: {COMMENT_LANG}.\n",
    "refactoring": "# Рефакторинг\n\nЧитай при рефакторинге.\n\n- Правило рефакторинга.\n",
    "design-patterns": "# Паттерны\n\nЧитай при выборе паттерна.\n\n- Правило паттернов.\n",
    "debugging": "# Лестница отладки\n\nЧитай, когда фикс не удался дважды.\n\n- Каталог: {RULES}.\n",
    "heuristics": "# Эвристики\n\nЧитай при правке эвристики.\n\n- Правило эвристик.\n",
    "memory": "# Гигиена памяти\n\nЧитай при работе с памятью.\n\n- Правило модуля памяти.\n",
}


class Env:
    """Временный CLAUDE_PLUGIN_ROOT с philosophy.md, rules/ и CLAUDE_PLUGIN_DATA."""

    def __init__(self, philosophy=PHILOSOPHY, rules=RULES):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "root"
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.root.mkdir()
        self.data.mkdir()
        self.project = pathlib.Path(self.tmp.name) / "project"
        self.project.mkdir()
        if philosophy is not None:
            (self.root / "philosophy.md").write_text(philosophy, encoding="utf-8")
        if rules is not None:
            (self.root / "rules").mkdir()
            for name, text in rules.items():
                (self.root / "rules" / f"{name}.md").write_text(text, encoding="utf-8")
        self.transcript = pathlib.Path(self.tmp.name) / "transcript.jsonl"
        self.transcript.write_text(
            json.dumps({"type": "assistant", "message": {"model": "claude-test-model"}}) + "\n", encoding="utf-8")

    def environ(self, **extra):
        """Окружение хука. PYTHONUTF8=0: хук работает в кодировке локали, как python3 пользователя без UTF-8 mode;
        в локали C (make test-hostile) это ascii, а не UTF-8, которую Python включает там сам."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PLANKA_", "CLAUDE_PLUGIN_OPTION_"))
               and k not in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR",
                            "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE")}
        home = pathlib.Path(self.tmp.name) / "home"
        home.mkdir(exist_ok=True)
        env.update({
            "HOME": str(home),
            "CLAUDE_PLUGIN_ROOT": str(self.root),
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "PATH": f"{STUB_DIR}:{env.get('PATH', '')}",
            "PLANKA_STUB": "ok",
            "PYTHONUTF8": "0",
        })
        env.update(extra)
        return env

    def run(self, script, hook_input, **extra):
        """Запускает plugin/planka/<script> как хук: stdin — JSON, возвращает CompletedProcess.
        Обмен — в UTF-8, как у Claude Code, при любой локали процесса тестов."""
        stdin = hook_input if isinstance(hook_input, str) else json.dumps(hook_input)
        return subprocess.run(
            [sys.executable, str(PLANKA_DIR / script)],
            input=stdin, capture_output=True, text=True, encoding="utf-8",
            env=self.environ(**extra), timeout=30,
        )

    def hook_input(self, event, **fields):
        base = {
            "session_id": "sess-1", "prompt_id": "p-1", "cwd": str(self.project),
            "permission_mode": "default", "hook_event_name": event,
            "transcript_path": str(self.transcript),
        }
        base.update(fields)
        return base

    def log_lines(self):
        p = self.data / "judge.log"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]

    def close(self):
        self.tmp.cleanup()


def output(r):
    """Ответ хука из stdout процесса без systemMessage; None, если ответа нет."""
    if not r.stdout:
        return None
    out = json.loads(r.stdout)
    out.pop("systemMessage", None)
    return out or None


def messages(r):
    """Строки systemMessage из stdout процесса; пустой список, если их нет."""
    if not r.stdout:
        return []
    msg = json.loads(r.stdout).get("systemMessage")
    return msg.splitlines() if msg else []


def run_in_process(env, main, hook_input, **environ):
    """main хука через common.run_hook в этом процессе, с окружением env.environ(**environ) и подменой stdin и
    stdout; ответ хука словарём, пустой ответ — None. Подмены модулей ставит вызывающий вокруг вызова."""
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(hook_input).encode("utf-8")), encoding="utf-8")
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
    with mock.patch.dict(os.environ, env.environ(**environ), clear=True), \
            mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", new=stdout):
        common.run_hook(main)
    return json.loads(raw.getvalue().decode("utf-8")) if raw.getvalue() else None


def fill_budget(env, hook, prompt_id="p-1"):
    """Счётчик отказов sess-1 по реплике и хуку — на пределе common.MAX_DENIES."""
    state = env.data / "state"
    state.mkdir(exist_ok=True)
    (state / "sess-1.json").write_text(json.dumps({f"{prompt_id}:{hook}": common.MAX_DENIES}), encoding="utf-8")


def assert_not_logged(test, env, *texts):
    """Ни одна строка журнала env не содержит texts: журнал хранит содержимое только длиной и SHA-256."""
    lines = env.log_lines()
    test.assertTrue(lines)
    for entry in lines:
        dumped = json.dumps(entry, ensure_ascii=False)
        for text in texts:
            test.assertNotIn(text, dumped)


def author_block(rec):
    """Блок <author> промпта судьи из записи заглушки PLANKA_STUB_RECORD."""
    return rec.read_text(encoding="utf-8").split("\n<author>\n", 1)[1].split("\n</author>\n", 1)[0]


def cpu_seconds(run):
    """Наименьшее процессорное время трёх запусков run: нагрузка машины его почти не растягивает."""
    best = float("inf")
    for _ in range(3):
        start = time.process_time()
        run()
        best = min(best, time.process_time() - start)
    return best


def assert_linear(test, small, large, ratio=8, msg=None):
    """Запуск large (вход вчетверо больше, чем у small) не дольше ratio запусков small и 5 мс: линейный — около
    4 раз, квадратичный — около 16."""
    test.assertLess(cpu_seconds(large), ratio * cpu_seconds(small) + 0.005, msg)


def comment_lines(text, ext):
    """Строки комментариев текста по синтаксису расширения ext (comments._comments) без номеров; строка блока —
    целиком."""
    return [c for _, c in comments._comments(text, ext)]
