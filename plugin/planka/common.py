"""Общее для хуков planka: барьеры, вход, транскрипт, судья, счётчик отказов, журнал, форматы ответа."""
import contextlib
import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from prompts import JUDGE_SCHEMA

MAX_DENIES = 2
JUDGE_TIMEOUT = 60
# Ожидание завершения группы судьи после SIGKILL и срок git rev-parse корня проекта, в секундах; входят в суммы
# сроков хуков против таймаутов hooks.json (context/architecture.md, «Сроки»).
KILL_WAIT = 5
GIT_ROOT_TIMEOUT = 5
MAX_REASON = 2000
MAX_DETAIL = 200
STATE_TTL = 7 * 86400
LOG_MAX_BYTES = 1_048_576
SESSION_MODEL = "session"
JUDGE_FLAGS = ["-p", "--setting-sources", "", "--strict-mcp-config",
               "--no-session-persistence", "--output-format", "json", "--tools", ""]


def barrier_active():
    """PLANKA_JUDGE помечает вложенный вызов судьи: хуки внутри него не работают."""
    return bool(os.environ.get("PLANKA_JUDGE"))


def read_input():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
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


# Предупреждения и ответ текущего вызова хука; run_hook сбрасывает их и выводит одним JSON.
_messages = []
_output = None


def _reset():
    global _output
    _messages.clear()
    _output = None


def warn(msg):
    """Строка «planka: msg» уходит пользователю полем systemMessage ответа хука."""
    _messages.append(f"planka: {msg}")


RULES_PLACEHOLDER = "{RULES}"
COMMENT_LANG_PLACEHOLDER = "{COMMENT_LANG}"
DOC_LANG_PLACEHOLDER = "{DOC_LANG}"
DEFAULT_LANG = "ru"
DOC_PATTERNS = ("README", "LICENSE")
DOC_DIRS = ("context/", "docs/")
CODE_EXTS = {
    "go", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "java", "kt", "kts", "swift",
    "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "dart", "rs", "scala", "m", "mm", "cs",
    "py", "pyi", "sh", "bash", "zsh", "rb", "pl", "pm", "toml", "yaml", "yml", "mk", "makefile", "cmake", "cfg",
    "ini", "ps1",
    "sql", "lua", "hs",
    "html", "xml", "vue", "svelte", "css", "scss", "sass", "less",
    "php", "r", "jl", "ex", "exs", "erl", "clj", "fs", "vb", "nim", "zig", "sol", "proto", "gradle",
    "groovy", "tf", "nix", "el", "vim", "bat", "cmd", "cljs", "edn", "fsx", "fsi", "vbs", "lisp",
}
# Манифесты по имени: остальные *.json и go.sum — прочее.
CODE_NAMES = {"Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "Dockerfile", "Justfile", "Rakefile",
              "Gemfile", "package.json", "tsconfig.json", "jsconfig.json", "composer.json", "deno.json", "go.mod"}


def rules_dir():
    return plugin_root() / "rules"


def settings():
    """Языки комментариев и документации из CLAUDE_PLUGIN_OPTION_*; пустое значение — DEFAULT_LANG."""
    return {"comment_lang": os.environ.get("CLAUDE_PLUGIN_OPTION_COMMENT_LANG") or DEFAULT_LANG,
            "doc_lang": os.environ.get("CLAUDE_PLUGIN_OPTION_DOC_LANG") or DEFAULT_LANG}


def substitute(text):
    """Метки каталога модулей и языков заменяются значениями."""
    s = settings()
    return (text.replace(RULES_PLACEHOLDER, str(rules_dir()))
                .replace(COMMENT_LANG_PLACEHOLDER, s["comment_lang"])
                .replace(DOC_LANG_PLACEHOLDER, s["doc_lang"]))


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
    """Решение судьи. error — пропуск проверки: короткое описание без текста модели, для judge.log;
    detail — фрагмент ответа судьи до 200 символов, только для пользователя (skip_message)."""
    ok: bool
    violated: list
    reason: str
    error: str | None = None
    detail: str | None = None


def _skipped(error, detail=None):
    return Verdict(ok=True, violated=[], reason="", error=error, detail=detail[:MAX_DETAIL] if detail else None)


def skip_message(verdict):
    """Предупреждение о пропуске проверки по Verdict с error: описание и фрагмент ответа судьи, если он есть."""
    msg = f"судья пропущен: {verdict.error}"
    return f"{msg}: {verdict.detail}" if verdict.detail else msg


def _kill_group(proc):
    """SIGKILL группе процессов судьи (он лидер своей группы) и ожидание завершения до KILL_WAIT с."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.communicate(timeout=KILL_WAIT)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


# Сторож судьи: лидер группы судьи, запускает claude потомком и раз в WATCHDOG_POLL с сверяет своего родителя
# с PID хука; хук умер — SIGKILL всей группе, себе и потомкам claude тоже. Сторож умирает только с группой:
# убитый отдельно, он оставляет группу таймауту судьи (_kill_group). Аргументы: PID хука, команда claude с путём.
WATCHDOG_POLL = 0.5
_WATCHDOG = f"""
import os, signal, subprocess, sys
hook = int(sys.argv[1])
try:
    proc = subprocess.Popen(sys.argv[2:])
except OSError:
    sys.exit(127)
while True:
    if os.getppid() != hook:
        os.killpg(0, signal.SIGKILL)
    try:
        sys.exit(proc.wait(timeout={WATCHDOG_POLL}))
    except subprocess.TimeoutExpired:
        pass
"""


def _start_judge(cmd, **popen_kwargs):
    """Popen сторожа _WATCHDOG с судьёй cmd лидером своей группы процессов: вместе с хуком умирает вся группа.
    FileNotFoundError — нет cmd[0] в PATH окружения popen_kwargs["env"]."""
    env = popen_kwargs.get("env") or os.environ
    exe = shutil.which(cmd[0], path=env.get("PATH"))
    if exe is None:
        raise FileNotFoundError(cmd[0])
    return subprocess.Popen([sys.executable, "-I", "-c", _WATCHDOG, str(os.getpid()), exe, *cmd[1:]],
                            start_new_session=True, **popen_kwargs)


@dataclasses.dataclass
class Transcript:
    """Сведения из транскрипта сессии.

    model — message.model последнего ответа ассистента основной ветки, кроме служебных моделей вида «<…>»;
    plan_file — последний attachment.planFilePath; turn_messages — непустые тексты ответов ассистента основной
    ветки после последней реплики автора, по порядку; author_turn — текст этой реплики, сообщений человека,
    отправленных посреди хода после неё, и ответов автора при отклонении инструмента; author_answers —
    тексты ответов на вызовы AskUserQuestion после неё; message_before_author — последнее непустое сообщение
    ассистента до неё; earlier_turns — непустые прежние реплики основной ветки от старых к новым, каждая
    собрана как author_turn, ответы на AskUserQuestion дописаны к ней через перевод строки.
    """
    model: str | None = None
    plan_file: pathlib.Path | None = None
    turn_messages: list = dataclasses.field(default_factory=list)
    author_turn: str = ""
    author_answers: list = dataclasses.field(default_factory=list)
    message_before_author: str = ""
    earlier_turns: list = dataclasses.field(default_factory=list)


def _text_blocks(content):
    """Тексты блоков type == "text" списка content; строка content — один текст."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def _result_text(content):
    """Текст tool_result: строка как есть, блоки text через перевод строки."""
    if isinstance(content, str):
        return content
    return "\n".join(_text_blocks(content))


def _origin_kind(obj):
    """origin.kind записи или вложения; нет origin — None."""
    origin = obj.get("origin")
    return origin.get("kind") if isinstance(origin, dict) else None


def _has_origin(entry):
    """Есть ли поле origin у записи или её вложения."""
    att = entry.get("attachment")
    return "origin" in entry or isinstance(att, dict) and "origin" in att


def _transcript_has_origin(path):
    """Есть ли в файле JSONL path запись с origin (_has_origin); чтение до первой такой записи.
    OSError и ValueError — наружу."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"origin"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and _has_origin(entry):
                return True
    return False


def _is_author_turn(entry, msg, origin_format):
    """Реплика автора: запись user основной ветки, которую написал человек.

    Записи isMeta — служебные вставки Claude Code (тело навыка, оговорки команд, сообщения peer), они приходят
    и посреди реплики. С origin — только origin.kind == "human": task-notification и peer — не человек.
    Без origin в транскрипте, где origin есть хоть у одной записи (origin_format), — локальные команды и их
    вывод, в том числе записанные раньше первой записи с origin; в транскрипте без origin — запись с текстом
    без tool_result."""
    if entry.get("isMeta"):
        return False
    if "origin" in entry:
        return _origin_kind(entry) == "human"
    if origin_format:
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    kinds = {b.get("type") for b in content if isinstance(b, dict)}
    return "text" in kinds and "tool_result" not in kinds


def _rejection_feedback(entry):
    """Ответ автора при отклонении инструмента: userFeedback записи с toolDenialKind "user-rejected" и
    permissionDecision.source "user_reject"; нет — "". Отказы правилом, хуком и классификатором автоматического
    режима (permission-rule, automode-blocked) — не автор."""
    decision = entry.get("permissionDecision")
    if entry.get("toolDenialKind") != "user-rejected" or not isinstance(decision, dict) \
            or decision.get("source") != "user_reject":
        return ""
    feedback = entry.get("userFeedback")
    return feedback if isinstance(feedback, str) else ""


def _join(*texts):
    return "\n".join(t for t in texts if t)


def read_transcript(path):
    """Transcript из файла JSONL path (путь из входа хука, input_path); нет пути, файла, путь с нулевым байтом
    или ошибка чтения — пустой Transcript.
    Строки не JSON и записи не словари пропускаются."""
    out = Transcript()
    if not isinstance(path, str) or not path:
        return out
    path = input_path(path)
    plan = None
    asked = set()
    try:
        origin_format = _transcript_has_origin(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                att = entry.get("attachment")
                p = att.get("planFilePath") if isinstance(att, dict) else None
                if isinstance(p, str) and p:
                    plan = p
                # Сообщение человека посреди хода — вложение queued_command, оно дополняет реплику, не начинает новую.
                if (isinstance(att, dict) and att.get("type") == "queued_command" and not entry.get("isSidechain")
                        and not att.get("isMeta") and _origin_kind(att) == "human"):
                    out.author_turn = _join(out.author_turn, *_text_blocks(att.get("prompt")))
                msg = entry.get("message")
                if entry.get("isSidechain") or not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if entry.get("type") == "user" and _is_author_turn(entry, msg, origin_format):
                    if out.turn_messages:
                        out.message_before_author = out.turn_messages[-1]
                    earlier = _join(out.author_turn, *out.author_answers)
                    if earlier:
                        out.earlier_turns.append(earlier)
                    out.author_turn = "\n".join(_text_blocks(content))
                    out.author_answers, out.turn_messages = [], []
                    asked.clear()
                elif entry.get("type") == "user" and entry.get("toolDenialKind"):
                    # Ответ при отклонении — указание автора посреди хода; его tool_result — не ответ AskUserQuestion.
                    out.author_turn = _join(out.author_turn, _rejection_feedback(entry))
                elif entry.get("type") == "user" and isinstance(content, list):
                    out.author_answers += [_result_text(b.get("content")) for b in content
                                           if isinstance(b, dict) and b.get("type") == "tool_result"
                                           and b.get("tool_use_id") in asked]
                elif entry.get("type") == "assistant":
                    if isinstance(content, list):
                        asked.update(b.get("id") for b in content if isinstance(b, dict)
                                     and b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion")
                    model = msg.get("model")
                    # Служебные ответы помечены моделью вида «<synthetic>»: --model её не примет.
                    if isinstance(model, str) and model and not model.startswith("<"):
                        out.model = model
                    text = "".join(_text_blocks(content))
                    if text.strip():
                        out.turn_messages.append(text)
    except (OSError, ValueError):
        return Transcript()
    out.plan_file = pathlib.Path(input_path(plan)) if plan else None
    return out


def judge_model(data, transcript=None):
    """Модель судьи из judge_model; «session» — модель сессии из transcript (None — транскрипт входа хука
    читается здесь).

    Модель сессии не найдена — None с предупреждением раз на сессию: судья идёт на модели claude по умолчанию.
    """
    model = os.environ.get("CLAUDE_PLUGIN_OPTION_JUDGE_MODEL") or SESSION_MODEL
    if model != SESSION_MODEL:
        return model
    if transcript is None:
        transcript = read_transcript(data.get("transcript_path"))
    if transcript.model is None:
        session_id = data.get("session_id")
        warn_once(session_id if isinstance(session_id, str) else "", "session-model",
                  "модель сессии не найдена в транскрипте, судья на модели claude по умолчанию")
    return transcript.model


def input_path(path):
    """Путь из JSON входа хука или транскрипта — строка в кодировке файловой системы процесса.

    Claude Code пишет пути байтами UTF-8 независимо от локали; в локали не UTF-8 (ascii, latin-1) строка
    с кириллицей не кодируется для open и subprocess, а возвращённая строка отдаёт им исходные байты.
    В локали UTF-8 — та же строка. Не строка или суррогат вне U+DC80..U+DCFF — path как есть."""
    if not isinstance(path, str):
        return path
    try:
        return os.fsdecode(path.encode("utf-8", "surrogateescape"))
    except UnicodeEncodeError:
        return path


def _utf8(text):
    """Текст для claude байтами UTF-8 при любой локали: claude читает argv и stdin в UTF-8.

    Суррогат U+DC80..U+DCFF — байт пути, декодированного os.fsdecode, — уходит исходным байтом: в локали
    не UTF-8 так путь с кириллицей доходит буквами. Прочие одиночные суррогаты — «?», остальной текст как есть."""
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return "".join("?" if "\ud800" <= c <= "\udfff" and not "\udc80" <= c <= "\udcff" else c
                       for c in text).encode("utf-8", "surrogateescape")


def run_judge(system_prompt, user_prompt, model, *, timeout=JUDGE_TIMEOUT):
    """Вложенный claude -p; model None — без --model; любая ошибка — пропуск с описанием в error.
    Аргументы после claude и stdin — байты _utf8, ответ декодируется из UTF-8 с заменой."""
    args = [*JUDGE_FLAGS, "--json-schema", json.dumps(JUDGE_SCHEMA),
            *(["--model", model] if model else []), "--system-prompt", system_prompt]
    cmd = ["claude", *map(_utf8, args)]
    env = dict(os.environ, PLANKA_JUDGE="1")
    try:
        proc = _start_judge(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=str(data_dir()))
    except FileNotFoundError:
        return _skipped("claude не найден в PATH")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return _skipped(f"claude не запущен: {e}")
    try:
        out, err = proc.communicate(_utf8(user_prompt), timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return _skipped(f"таймаут судьи {timeout} с")
    except (OSError, ValueError) as e:
        _kill_group(proc)
        return _skipped(f"обмен с claude не удался: {e}")
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    # Строки ответа — только по "\n": U+2028, U+2029 и U+0085 JSON оставляет символами внутри строки.
    line = next((l for l in stdout.split("\n") if l.startswith("{")), None)
    if line is None:
        return _skipped("ответ судьи не JSON", f"{stdout[:200]!r} {stderr[:200]!r}")
    try:
        result = json.loads(line)
    except json.JSONDecodeError:
        return _skipped("ответ судьи не разобран", repr(line[:200]))
    if result.get("is_error"):
        return _skipped("ошибка судьи", str(result.get("result"))[:200])
    so = result.get("structured_output")
    if not isinstance(so, dict) or "ok" not in so:
        return _skipped("в ответе судьи нет structured_output", repr(line[:200]))
    reason = str(so.get("reason") or "")
    if len(reason) > MAX_REASON:
        reason = reason[:MAX_REASON - 1] + "…"
    violated = so.get("violated")
    violated = [str(v) for v in violated] if isinstance(violated, list) else []
    return Verdict(ok=bool(so["ok"]), violated=violated, reason=reason)


def dumps(obj):
    """JSON без ASCII-экранирования; одиночный суррогат (имя файла не в UTF-8) — JSON-escape `\\udcXX`."""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8", "backslashreplace").decode("utf-8")


def atomic_write_json(path, obj):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(obj))
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def safe_name(session_id):
    """Имя файла состояния из id сессии: только ASCII-буквы, цифры и «._-»."""
    safe = "".join(c if c.isalnum() and c.isascii() or c in "._-" else "_" for c in session_id)
    return safe or "unknown"


def read_json(path, kind):
    """Содержимое файла состояния; нет файла, битый JSON или значение не типа kind — пустой kind()."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return kind()
    return value if type(value) is kind else kind()


@contextlib.contextmanager
def state_lock(state_dir):
    """Чтение и запись файлов состояния — под блокировкой state/.lock: параллельные хуки не теряют записи."""
    with open(state_dir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _deny_count(state, key):
    count = state.get(key, 0)
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def deny_budget_left(session_id, prompt_id, hook):
    """True, если по этому ключу отказов меньше MAX_DENIES; счётчик не меняется. Сбой чтения — True."""
    try:
        state_dir = data_dir() / "state"
        state_dir.mkdir(exist_ok=True)
        with state_lock(state_dir):
            state = read_json(state_dir / f"{safe_name(session_id)}.json", dict)
    except OSError:
        return True
    return _deny_count(state, f"{prompt_id}:{hook}") < MAX_DENIES


def deny_budget_exhausted(session_id, prompt_id, hook):
    """True, если по этому ключу уже было MAX_DENIES отказов; счётчик растёт при каждом вызове."""
    state_dir = data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{safe_name(session_id)}.json"
    with state_lock(state_dir):
        state = read_json(path, dict)
        key = f"{prompt_id}:{hook}"
        before = _deny_count(state, key)
        state[key] = before + 1
        atomic_write_json(path, state)
    prune_state(state_dir)
    return before >= MAX_DENIES


def _git_out(base, timeout, *args):
    """stdout `git <args>` в каталоге base байтами; None при коде не 0, сбое или таймауте timeout с."""
    try:
        proc = subprocess.run(["git", *args], cwd=base, capture_output=True, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _same_dir(a, b):
    """Один ли каталог a и b; ошибка stat — True."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return True


def _existing_dir(path):
    """path, если это каталог, иначе ближайший существующий каталог-предок."""
    while not os.path.isdir(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


def _home_or_above(top):
    """top — домашний каталог пользователя (~) или его предок; символические ссылки разрешены. Нет дома или
    ошибка — False."""
    home = os.path.expanduser("~")
    if not os.path.isabs(home):
        return False
    try:
        home, top = os.path.realpath(home), os.path.realpath(top)
        return os.path.commonpath([home, top]) == top
    except (OSError, ValueError):
        return False


def project_root(cwd):
    """Корень проекта сессии: вершина git-репозитория для CLAUDE_PROJECT_DIR, без неё — для cwd;
    вне репозитория — сам каталог. Каталога нет — git спрашивается из ближайшего существующего предка.

    Вершина — домашний каталог или его предок, CLAUDE_PROJECT_DIR ниже неё и репозиторий не отслеживает под
    ним ни одного файла (проект без своего git под домашним каталогом-репозиторием dotfiles) — корень сам
    каталог: репозиторий выше — не репозиторий проекта, git о таком корне спрашивается с окружением git_env.
    Сбой этой проверки — вершина. Под любой другой вершиной (монорепозиторий) корень — вершина, в том числе
    для нового или исключённого .gitignore подкаталога.
    CLAUDE_PROJECT_DIR — каталог запуска сессии, после cd агента он не меняется; cwd хука меняется.
    cwd — путь из входа хука, он приводится к кодировке файловой системы (input_path).
    Оба вызова git вместе — не дольше GIT_ROOT_TIMEOUT.
    """
    launch = os.environ.get("CLAUDE_PROJECT_DIR")
    base = launch or input_path(cwd)
    found = _existing_dir(base)
    deadline = time.monotonic() + GIT_ROOT_TIMEOUT
    top = _git_out(found, GIT_ROOT_TIMEOUT, "rev-parse", "--show-toplevel")
    top = top.strip() if top else None
    if not top:
        return pathlib.Path(base)
    top = pathlib.Path(os.fsdecode(top))
    if launch and _home_or_above(top) and (found != base or not _same_dir(top, base)):
        left = deadline - time.monotonic()
        tracked = _git_out(found, left, "--literal-pathspecs", "ls-files", "-z", "--cached", "--",
                           os.path.relpath(base, found)) if left > 0 else None
        if tracked == b"":
            return pathlib.Path(base)
    return top


def git_env(root):
    """Окружение вызова git о проекте с корнем root (project_root): GIT_CEILING_DIRECTORIES с родителем root
    (символические ссылки разрешены) первым — git ищет репозиторий в root и ниже, но не выше: корень, который
    project_root отделил от репозитория выше, остаётся вне git. Вызов идёт из root или каталога под ним."""
    ceiling = os.path.dirname(os.path.realpath(root))
    inherited = os.environ.get("GIT_CEILING_DIRECTORIES")
    return dict(os.environ, GIT_CEILING_DIRECTORIES=f"{ceiling}{os.pathsep}{inherited}" if inherited else ceiling)


def is_doc_path(relpath):
    """Путь относительно корня проекта, с прямыми слэшами: документация или нет."""
    name = relpath.rsplit("/", 1)[-1]
    if name.endswith(".md") or name.startswith(DOC_PATTERNS):
        return True
    return relpath.startswith(DOC_DIRS)


def path_kind(relpath):
    """Класс пути относительно корня проекта, с прямыми слэшами: "code", "doc" или "other".

    Расширение или имя кода дают "code" в любом каталоге, в том числе под context/ и docs/.
    """
    name = relpath.rsplit("/", 1)[-1]
    if name in CODE_NAMES or ("." in name and name.rsplit(".", 1)[1].lower() in CODE_EXTS):
        return "code"
    if is_doc_path(relpath):
        return "doc"
    return "other"


def warn_once(session_id, key, msg):
    """Предупреждение один раз на сессию и ключ; факт записан в state/<session>.warned.json."""
    state_dir = data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{safe_name(session_id)}.warned.json"
    with state_lock(state_dir):
        seen = read_json(path, list)
        if key in seen:
            return False
        seen.append(key)
        atomic_write_json(path, seen)
    warn(msg)
    return True


def prune_state(state_dir):
    """Удаляет файлы состояния — счётчики, снимки, предупреждения — и брошенные .tmp-* старше STATE_TTL."""
    cutoff = time.time() - STATE_TTL
    for p in [*state_dir.glob("*.json"), *state_dir.glob(".tmp-*")]:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def log_event(hook, session_id, *, content=None, **fields):
    """Строка judge.log; содержимое — только длина и SHA-256. Файл от LOG_MAX_BYTES уходит в judge.log.1."""
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "hook": hook, "session_id": session_id, **fields}
    if content is not None:
        entry["content_len"] = len(content)
        entry["content_sha256"] = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
    path = data_dir() / "judge.log"
    with open(path.with_name("judge.log.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if path.stat().st_size >= LOG_MAX_BYTES:
                os.replace(path, path.with_name("judge.log.1"))
        except FileNotFoundError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(dumps(entry) + "\n")


def deny_output(reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}


def block_output(reason):
    return {"decision": "block", "reason": reason}


def context_output(text):
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def emit(obj):
    """Ответ хука; выводит его run_hook."""
    global _output
    _output = obj


def run_hook(main):
    """Граница отказа хука: исключение — предупреждение «внутренняя ошибка»; stdout — один JSON
    из ответа и systemMessage или пусто; stderr не пишется."""
    _reset()
    try:
        main()
    except Exception as e:
        warn(f"внутренняя ошибка: {e!r}")
    out = dict(_output or {})
    if _messages:
        out["systemMessage"] = "\n".join(_messages)
    if out:
        sys.stdout.buffer.write(dumps(out).encode("utf-8"))
        sys.stdout.buffer.flush()
