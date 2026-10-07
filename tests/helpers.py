"""Окружение для тестов planka: временный каталог плагина, подмена claude, запуск скриптов."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
STUB_DIR = REPO / "tests" / "stub"
PLANKA_DIR = REPO / "planka"

PHILOSOPHY = """# Философия работы

## Решения

1. Правило решений один.
2. Правило решений два.

## Поведение

- Правило поведения.

## Спецификации

1. Правило спеки.

## Планы

1. Правило планов один.
2. Правило планов два.

## Модули

Каталог: {RULES}
"""

RULES = {
    "verification": "# Доказательство\n\n- Правило проверки.\n",
    "planning": "# Планирование\n\n- Правило планирования.\n",
    "subagents": "# Субагенты\n\n- Правило субагентов.\n",
}


class Env:
    """Временный CLAUDE_PLUGIN_ROOT с philosophy.md, rules/ и CLAUDE_PLUGIN_DATA."""

    def __init__(self, philosophy=PHILOSOPHY, rules=RULES):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "root"
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.root.mkdir()
        self.data.mkdir()
        if philosophy is not None:
            (self.root / "philosophy.md").write_text(philosophy, encoding="utf-8")
        if rules is not None:
            (self.root / "rules").mkdir()
            for name, text in rules.items():
                (self.root / "rules" / f"{name}.md").write_text(text, encoding="utf-8")

    def environ(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLANKA_")}
        env.update({
            "CLAUDE_PLUGIN_ROOT": str(self.root),
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "PATH": f"{STUB_DIR}:{env.get('PATH', '')}",
            "PLANKA_STUB": "ok",
        })
        env.update(extra)
        return env

    def run(self, script, hook_input, **extra):
        """Запускает planka/<script> как хук: stdin — JSON, возвращает CompletedProcess."""
        stdin = hook_input if isinstance(hook_input, str) else json.dumps(hook_input)
        return subprocess.run(
            [sys.executable, str(PLANKA_DIR / script)],
            input=stdin, capture_output=True, text=True,
            env=self.environ(**extra), timeout=30,
        )

    def log_lines(self):
        p = self.data / "judge.log"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]

    def close(self):
        self.tmp.cleanup()


def hook_input(event, **fields):
    base = {
        "session_id": "sess-1", "prompt_id": "p-1", "cwd": "/tmp",
        "permission_mode": "default", "hook_event_name": event,
        "transcript_path": "/nonexistent/transcript.jsonl",
    }
    base.update(fields)
    return base
