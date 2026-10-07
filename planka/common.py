"""Общее для хуков planka: барьеры, вход, судья, счётчик отказов, журнал, форматы ответа."""
import dataclasses
import datetime
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

from prompts import JUDGE_SCHEMA

MAX_DENIES = 2
JUDGE_TIMEOUT = 60
MAX_REASON = 2000
STATE_TTL = 7 * 86400
DEFAULT_JUDGE_MODEL = "sonnet"
JUDGE_FLAGS = ["-p", "--setting-sources", "", "--strict-mcp-config",
               "--no-session-persistence", "--output-format", "json", "--tools", ""]


def barrier_active():
    """PLANKA_JUDGE помечает вложенный вызов судьи: хуки внутри него не работают."""
    return bool(os.environ.get("PLANKA_JUDGE"))


def read_input():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def plugin_root():
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return pathlib.Path(env) if env else pathlib.Path(__file__).resolve().parent.parent


def data_dir():
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    d = pathlib.Path(env) if env else plugin_root() / ".data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def warn(msg):
    print(f"planka: {msg}", file=sys.stderr)


RULES_PLACEHOLDER = "{RULES}"
COMMENT_LANG_PLACEHOLDER = "{COMMENT_LANG}"
DOC_LANG_PLACEHOLDER = "{DOC_LANG}"
UNSET_LANG = "не задан"
DOC_PATTERNS = ("README", "LICENSE")
DOC_DIRS = ("context/", "docs/")


def rules_dir():
    return plugin_root() / "rules"


def settings():
    """Языки комментариев и документации из CLAUDE_PLUGIN_OPTION_*; пустое значение — None."""
    return {"comment_lang": os.environ.get("CLAUDE_PLUGIN_OPTION_COMMENT_LANG") or None,
            "doc_lang": os.environ.get("CLAUDE_PLUGIN_OPTION_DOC_LANG") or None}


def substitute(text):
    """Метки каталога модулей и языков заменяются значениями; незаданный язык — UNSET_LANG."""
    s = settings()
    return (text.replace(RULES_PLACEHOLDER, str(rules_dir()))
                .replace(COMMENT_LANG_PLACEHOLDER, s["comment_lang"] or UNSET_LANG)
                .replace(DOC_LANG_PLACEHOLDER, s["doc_lang"] or UNSET_LANG))


def philosophy_text():
    p = plugin_root() / "philosophy.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        warn(f"нет файла правил {p}")
        return None
    except UnicodeDecodeError:
        warn(f"файл правил {p} не в UTF-8")
        return None
    return substitute(text)


def rule_texts(*names):
    """Модули rules/<name>.md в порядке names; None, если хоть одного нет."""
    out = []
    for name in names:
        p = rules_dir() / f"{name}.md"
        try:
            out.append(substitute(p.read_text(encoding="utf-8").strip()))
        except OSError:
            warn(f"нет модуля правил {p}")
            return None
        except UnicodeDecodeError:
            warn(f"модуль правил {p} не в UTF-8")
            return None
    return "\n\n".join(out)


def rubric(sections, modules):
    """Рубрика судьи: разделы ядра, затем модули; None, если любая часть недоступна."""
    parts = []
    if sections:
        core = philosophy_sections(*sections)
        if core is None:
            return None
        parts.append(core)
    if modules:
        mods = rule_texts(*modules)
        if mods is None:
            return None
        parts.append(mods)
    return "\n\n".join(parts)


def philosophy_sections(*names):
    """Разделы «## <name>» в порядке names; None, если хоть одного нет."""
    text = philosophy_text()
    if text is None:
        return None
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = [line]
        elif current is not None:
            sections[current].append(line)
    out = []
    for name in names:
        if name not in sections:
            warn(f"в philosophy.md нет раздела «{name}»")
            return None
        out.append("\n".join(sections[name]).strip())
    return "\n\n".join(out)


@dataclasses.dataclass
class Verdict:
    ok: bool
    violated: list
    reason: str
    error: str = None


def _skipped(error):
    return Verdict(ok=True, violated=[], reason="", error=error)


def run_judge(system_prompt, user_prompt, *, timeout=JUDGE_TIMEOUT):
    """Вложенный claude -p; любая ошибка — пропуск с описанием в error."""
    model = os.environ.get("CLAUDE_PLUGIN_OPTION_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL
    cmd = ["claude", *JUDGE_FLAGS, "--json-schema", json.dumps(JUDGE_SCHEMA),
           "--model", model, "--system-prompt", system_prompt]
    env = dict(os.environ, PLANKA_JUDGE="1")
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", env=env, cwd=str(data_dir()),
                                start_new_session=True)
    except FileNotFoundError:
        return _skipped("claude не найден в PATH")
    except OSError as e:
        return _skipped(f"claude не запущен: {e}")
    try:
        stdout, stderr = proc.communicate(user_prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Судья — отдельная группа процессов: убивается вместе с потомками, иначе
        # потомок держит stdout и communicate ждёт его до конца.
        os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return _skipped(f"таймаут судьи {timeout} с")
    except (OSError, ValueError) as e:
        os.killpg(proc.pid, signal.SIGKILL)
        return _skipped(f"обмен с claude не удался: {e}")
    line = next((l for l in stdout.splitlines() if l.startswith("{")), None)
    if line is None:
        return _skipped(f"ответ судьи не JSON: {stdout[:200]!r} {stderr[:200]!r}")
    try:
        result = json.loads(line)
    except json.JSONDecodeError:
        return _skipped(f"ответ судьи не разобран: {line[:200]!r}")
    if result.get("is_error"):
        return _skipped(f"ошибка судьи: {result.get('result')}")
    so = result.get("structured_output")
    if not isinstance(so, dict) or "ok" not in so:
        return _skipped(f"нет structured_output: {line[:200]!r}")
    reason = str(so.get("reason") or "")
    if len(reason) > MAX_REASON:
        reason = reason[:MAX_REASON - 1] + "…"
    return Verdict(ok=bool(so["ok"]), violated=list(so.get("violated") or []), reason=reason)


def _atomic_write_json(path, obj):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def _safe_name(session_id):
    """Имя файла состояния из id сессии: только ASCII-буквы, цифры и «._-»."""
    safe = "".join(c if c.isalnum() and c.isascii() or c in "._-" else "_" for c in session_id)
    return safe or "unknown"


def deny_budget_exhausted(session_id, prompt_id, hook):
    """True, если по этому ключу уже было MAX_DENIES отказов; счётчик растёт при каждом вызове."""
    state_dir = data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{_safe_name(session_id)}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    key = f"{prompt_id}:{hook}"
    before = int(state.get(key, 0))
    state[key] = before + 1
    _atomic_write_json(path, state)
    _prune_state(state_dir)
    return before >= MAX_DENIES


def project_root(cwd):
    """Вершина git-репозитория для cwd; без git или вне репозитория — сам cwd."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return pathlib.Path(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return pathlib.Path(cwd)


def is_doc_path(relpath):
    """Путь относительно корня проекта, с прямыми слэшами: документация или нет."""
    name = relpath.rsplit("/", 1)[-1]
    if name.endswith(".md") or name.startswith(DOC_PATTERNS):
        return True
    return relpath.startswith(DOC_DIRS)


def warn_once(session_id, key, msg):
    """Предупреждение один раз на сессию и ключ; факт записан в state/<session>.warned.json."""
    state_dir = data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{_safe_name(session_id)}.warned.json"
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        seen = []
    if key in seen:
        return False
    seen.append(key)
    _atomic_write_json(path, seen)
    warn(msg)
    return True


def _prune_state(state_dir):
    """Удаляет счётчики сессий, не менявшиеся дольше STATE_TTL."""
    cutoff = time.time() - STATE_TTL
    for p in state_dir.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def log_event(hook, session_id, **fields):
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "hook": hook, "session_id": session_id, **fields}
    with open(data_dir() / "judge.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def deny_output(reason):
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, ensure_ascii=False)


def block_output(reason):
    return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)


def context_output(text):
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": text}}, ensure_ascii=False)


def run_hook(main):
    """Граница отказа хука: любое исключение — строка planka: и выход 0, без решения."""
    try:
        main()
    except Exception as e:
        warn(f"внутренняя ошибка: {e!r}")
        sys.exit(0)


def emit(text):
    sys.stdout.write(text)
    sys.stdout.flush()
