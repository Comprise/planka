"""PostToolUse и PostToolUseFailure на Bash: повторная неудача той же команды подмешивает модуль debugging."""
import hashlib
import os
import re
import shlex

import common

MODULE = "debugging"
# Неудача с этого номера подряд подмешивает модуль.
REPEAT_THRESHOLD = 2
MAX_SHOWN_COMMAND = 200

# Код выхода 1 этих команд — ответ «не найдено», «ложно» или «различаются», не сбой; код 2 и выше — сбой.
CODE1_ANSWERS = frozenset({"grep", "egrep", "fgrep", "zgrep", "rg", "test", "[", "[[", "diff", "cmp",
                           "pgrep", "pkill", "pidof", "which", "type"})
# Подкоманды git, у которых код 1 — ответ при одном из флагов; None — при любых флагах.
GIT_CODE1_ANSWERS = {
    "grep": None,
    "diff": {"--quiet", "--exit-code", "--no-index"},
    "diff-index": {"--quiet", "--exit-code"},
    "diff-files": {"--quiet", "--exit-code"},
    "merge-base": {"--is-ancestor"},
}
# Обёртки, передающие код выхода команды, и их опции с отдельным аргументом.
WRAPPERS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "time": {"-f", "-o", "--format", "--output"},
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "command": set(),
    "exec": {"-a"},
    "builtin": set(),
}
GIT_ARG_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# Присваивание одной подстановки без скобок внутри: код выхода — код команды подстановки.
SUBSTITUTION_ASSIGNMENT = re.compile(r"\s*[A-Za-z_][A-Za-z0-9_]*=(\"?)\$\(([^()]*)\)\1\s*")
# Команды, которые после «&&» не возвращают 1 сами: код 1 цепочки — код команды перед ними.
PASS_THROUGH = frozenset({"echo", "printf", "true", ":"})
# Участки в кавычках и экранированные символы: перенаправление ищется вне них.
UNQUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"|\\.", re.DOTALL)
EXIT_LINE = re.compile(r"Exit code (\d+)")
# Открытие heredoc: «<<» или «<<-», терминатор — слово, возможно в кавычках (here-string «<<<» не подходит).
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.-]+))")

LINE = "Команда `{command}` упала {ordinal} раз подряд — дальше по модулю {rules}/debugging.md."


def command_key(command, agent_id=""):
    """(SHA-256, текст) команды без краевых пробелов и с пробельными промежутками, схлопнутыми в один пробел;
    пустая команда — (None, ""). Субагент (agent_id не пуст) считает отдельно от основного агента и других
    субагентов: идентификатор входит в хэш; у основного агента хэш — хэш одного текста команды."""
    norm = re.sub(r"\s+", " ", command).strip()
    if not norm:
        return None, ""
    data = f"{agent_id}\0{norm}" if agent_id else norm
    return hashlib.sha256(data.encode("utf-8", "surrogatepass")).hexdigest(), norm


def exit_code(error):
    """Код выхода из первой строки «Exit code N» поля error; нет такой строки — None."""
    if not isinstance(error, str):
        return None
    m = EXIT_LINE.fullmatch(error.split("\n", 1)[0])
    return int(m.group(1)) if m else None


def _skip_heredocs(command, i, pending):
    """Индекс после тел heredoc, открытых строкой: i — первый символ после перевода строки. Тело и строка
    терминатора пропускаются для каждого heredoc по порядку; терминатора нет — до конца строки команды."""
    for terminator, strip_tabs in pending:
        while i < len(command):
            end = command.find("\n", i)
            line = command[i:] if end < 0 else command[i:end]
            i = len(command) if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == terminator:
                break
    return i


def _segments(command):
    """Непустые команды строки с разделителем перед каждой: [(разделитель, текст)]. Разделители вне кавычек —
    «|», «|&», «||», «&&», «;» и перевод строки; у первой команды разделитель "". Одиночный «&» и скобки не
    разделяют; внутри «[[ … ]]» разделителей нет; тела heredoc отбрасываются, а «<<» внутри «$((…))» и
    «((…))» — сдвиг, не heredoc. Комментарий от «#» в начале слова (после пробела, «;», «&», «|», скобки,
    «<», «>» или в начале строки) до конца строки отбрасывается."""
    segments = [["", []]]
    i, n = 0, len(command)
    quote = None
    # Предыдущий символ — незаэкранированный разрыв слова вне кавычек: после него «#» начинает комментарий.
    boundary = True
    in_test = False
    pending = []
    # Последний « ]]»: «[[ » открывает проверку, только если закрытие стоит дальше (один поиск, не на каждый «[[ »).
    last_close = command.rfind(" ]]")
    # Глубина скобок внутри арифметики «$((…))» и «((…))»; 0 — вне её.
    arith = 0
    while i < n:
        c = command[i]
        step = 1
        if quote is None and c == "#" and boundary:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if quote is None and boundary and not in_test and re.match(r"\[\[\s", command[i:i + 3]) \
                and last_close >= i:
            in_test = True
        elif quote is None and in_test and boundary and command.startswith("]]", i):
            in_test = False
        if quote is None and not in_test and (c in "|;\n" or command.startswith("&&", i)):
            step = 2 if command[i:i + 2] in ("||", "&&", "|&") else 1
            segments.append([command[i:i + step], []])
            boundary = True
            if c == "\n" and pending:
                i = _skip_heredocs(command, i + 1, pending)
                pending = []
                continue
        else:
            if quote is None:
                if arith and c in "()":
                    arith += 1 if c == "(" else -1
                elif command.startswith("$((", i):
                    arith, step = arith + 2, 3
                elif boundary and command.startswith("((", i):
                    arith, step = arith + 2, 2
            boundary = quote is None and (c.isspace() or c in "&()<>")
            m = HEREDOC.match(command, i) if quote is None and c == "<" and not in_test and not arith and \
                not command.startswith("<<<", i) and not command.startswith("<<<", i - 1) else None
            if m:
                word = next(g for g in m.groups()[1:] if g is not None)
                pending.append((word, m.group(1) == "-"))
            if c == "\\" and quote != "'":
                step = 2
            elif quote is None and c in "'\"":
                quote = c
            elif c == quote:
                quote = None
            segments[-1][1].append(command[i:i + step])
        i += step
    texts = [(sep, "".join(chars)) for sep, chars in segments]
    return [(sep, text) for sep, text in texts if text.strip()]


def _passes_code(text):
    """Команда не возвращает 1 сама: первое слово из PASS_THROUGH и вне кавычек и экранирования нет
    «<» и «>» (перенаправление может не открыться и дать 1; «echo "a > b"» — не перенаправление)."""
    if re.search(r"[<>]", UNQUOTED.sub("", text)):
        return False
    try:
        words = shlex.split(text)
    except ValueError:
        return False
    return bool(words) and words[0] in PASS_THROUGH


def _last_pipeline_command(command):
    """Текст команды, чей код выхода — код строки, если цепочку не оборвала неудача раньше и не включён
    pipefail: последняя команда (_segments); стоящая после «&&» команда из PASS_THROUGH код не меняет —
    тогда команда перед ней. Нет команд — ""."""
    segments = _segments(command)
    while len(segments) > 1 and segments[-1][0] == "&&" and _passes_code(segments[-1][1]):
        segments.pop()
    return segments[-1][1] if segments else ""


def _skip_options(words, i, with_arg):
    """Индекс первого слова после опций с i: «--» завершает опции, опция из with_arg без «=» берёт
    следующее слово."""
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        if words[i] == "--":
            return i + 1
        if words[i] in with_arg:
            i += 1
        i += 1
    return i


def code1_is_answer(command):
    """Код выхода 1 команды — штатный ответ, а не сбой: последняя команда строки после присваиваний и
    обёрток WRAPPERS — из CODE1_ANSWERS, git с подкомандой и флагом из GIT_CODE1_ANSWERS, «command -v»,
    «command -V» или отрицание «!»; присваивание x=$(…) — по команде подстановки. Неразборная строка —
    False."""
    last = _last_pipeline_command(command)
    substitution = SUBSTITUTION_ASSIGNMENT.fullmatch(last)
    if substitution:
        return code1_is_answer(substitution.group(2))
    try:
        words = shlex.split(last)
    except ValueError:
        return False
    i = 0
    while i < len(words):
        word = words[i]
        if ASSIGNMENT.match(word):
            i += 1
            continue
        if word == "!":
            return True
        name = os.path.basename(word)
        if name == "command" and i + 1 < len(words) and words[i + 1] in ("-v", "-V"):
            return True
        if name not in WRAPPERS:
            break
        i = _skip_options(words, i + 1, WRAPPERS[name])
        if name == "timeout":
            i += 1
    if i >= len(words):
        return False
    name = os.path.basename(words[i])
    if name in CODE1_ANSWERS:
        return True
    if name != "git":
        return False
    i = _skip_options(words, i + 1, GIT_ARG_OPTIONS)
    if i >= len(words) or words[i] not in GIT_CODE1_ANSWERS:
        return False
    flags = GIT_CODE1_ANSWERS[words[i]]
    return flags is None or any(w in flags for w in words[i + 1:])


def _ordinal(n):
    return "второй" if n == 2 else f"{n}-й"


def _shown(norm):
    return norm if len(norm) <= MAX_SHOWN_COMMAND else norm[:MAX_SHOWN_COMMAND - 1] + "…"


def _counts(state):
    counts = state.get("counts")
    if not isinstance(counts, dict):
        return {}
    return {k: v for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0}


def _shown_marks(state):
    shown = state.get("shown")
    if not isinstance(shown, dict):
        return {}
    return {k: v for k, v in shown.items() if isinstance(v, str)}


def _state(counts, shown):
    return {"counts": counts, "shown": shown} if shown else {"counts": counts}


def update(session, prompt_id, key, failed, agent_id=""):
    """Счётчик неудач подряд по ключу в state/<session>.debug.json: неудача +1, успех — удаление ключа.
    Модуль подмешивается с REPEAT_THRESHOLD-й неудачи подряд, не чаще раза за prompt_id для каждого агента
    (отметки — state["shown"][agent_id], у основного агента ключ ""): возвращает (число неудач подряд,
    текст модуля), иначе (None, None). Нет модуля — (None, None) с предупреждением от
    common.rule_texts, отметка о показе не ставится."""
    state_dir = common.data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"{common.safe_name(session)}.debug.json"
    with common.state_lock(state_dir):
        old = common.read_json(path, dict)
        counts = _counts(old)
        shown = _shown_marks(old)
        # Пишутся только counts и shown: прочие поля прежних форматов не переносятся.
        if not failed:
            if key not in counts:
                return None, None
            del counts[key]
            common.atomic_write_json(path, _state(counts, shown))
            return None, None
        counts[key] = counts.get(key, 0) + 1
        n = counts[key]
        text = None
        if n >= REPEAT_THRESHOLD and shown.get(agent_id) != prompt_id:
            text = common.rule_texts(MODULE)
            if text is not None:
                shown[agent_id] = prompt_id
        common.atomic_write_json(path, _state(counts, shown))
    common.prune_state(state_dir)
    return (n, text) if text is not None else (None, None)


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if data is None or data.get("tool_name") != "Bash":
        return
    event = data.get("hook_event_name")
    if event not in ("PostToolUseFailure", "PostToolUse"):
        return
    if event == "PostToolUseFailure" and data.get("is_interrupt") is True:
        return
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return
    # Код 1 команды-ответа сбрасывает счётчик, как успех.
    failed = event == "PostToolUseFailure" and not (
        exit_code(data.get("error")) == 1 and code1_is_answer(command))
    agent_id = data.get("agent_id")
    agent_id = agent_id if isinstance(agent_id, str) else ""
    key, norm = command_key(command, agent_id)
    if key is None:
        return
    session = data.get("session_id")
    session = session if isinstance(session, str) else ""
    prompt_id = data.get("prompt_id")
    prompt_id = prompt_id if isinstance(prompt_id, str) else ""
    try:
        n, text = update(session, prompt_id, key, failed, agent_id)
    except OSError as e:
        common.warn(f"счётчик неудач команд не записан: {e!r}")
        return
    if n is None:
        return
    line = LINE.format(command=_shown(norm), ordinal=_ordinal(n), rules=common.rules_dir())
    common.emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": f"{line}\n\n{text}"}})


if __name__ == "__main__":
    common.run_hook(main)
