"""PostToolUse и PostToolUseFailure на Bash: повторная неудача той же команды подмешивает модуль debugging."""
import hashlib
import os
import re

import common
import shparse

MODULE = "debugging"
# Неудача с этого номера подряд подмешивает модуль.
REPEAT_THRESHOLD = 2
MAX_SHOWN_COMMAND = 200
# В файле состояния хранятся последние MAX_COUNTS ключей счётчика и последние MAX_SHOWN отметок показа.
MAX_COUNTS = 500
MAX_SHOWN = 100

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
# Команды, которые после «&&» не возвращают 1 сами: код 1 цепочки — код команды перед ними.
PASS_THROUGH = frozenset({"echo", "printf", "true", ":"})
EXIT_LINE = re.compile(r"Exit code (\d+)")

LINE = "Команда `{command}` упала {ordinal} раз подряд — дальше, пожалуйста, по модулю {rules}/debugging.md."


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


def _passes_code(node):
    """Команда не возвращает 1 сама: простая команда без присваиваний и перенаправлений (перенаправление может не
    открыться и дать 1) с именем из PASS_THROUGH."""
    if type(node) is not shparse.Simple or node.assigns or node.redirs or not node.words:
        return False
    return node.words[0].literal() in PASS_THROUGH


def _last_substitution(assigns):
    """Последняя подстановка «$(…)» или «`…`» присваиваний — её код bash отдаёт командой без имени; None — подстановки
    нет или последней может оказаться другая: подстановка внутри «${…}» или арифметики (исполняется не всегда),
    «${ …; }», «<(…)»."""
    last = None
    stack = [iter(word.parts) for word in reversed(assigns)]
    while stack:
        part = next(stack[-1], None)
        if part is None:
            stack.pop()
            continue
        kind = type(part)
        if kind is shparse.DQ:
            stack.append(iter(part.parts))
        elif kind is shparse.Sub:
            last = part if part.kind in ("$(", "`") else None
        elif kind is shparse.Param or kind is shparse.Arith:
            if any(type(node) is shparse.Sub for node in shparse.walk(part)):
                last = None
    return last


def _deciding(script):
    """Узел, чей код выхода — код строки, если цепочку не оборвала неудача раньше и не включён pipefail: последняя
    команда последней полной команды, списка, «&&»/«||», конвейера, тела «( … )» и «{ …; }»; стоящая после «&&»
    команда из PASS_THROUGH код не меняет — берётся команда перед ней; у команды из одних присваиваний — последняя
    команда подстановки (_last_substitution). Конвейер с «!» — сам конвейер. None — кода 1 у строки нет или он не
    от одной команды: синтаксическая ошибка (bash отдаёт 2), последняя команда в фоне «&» (код 0), строка пуста."""
    node = script
    while True:
        kind = type(node)
        if kind is shparse.Script:
            if node.error is not None and node.error.fatal or not node.commands:
                return None
            node = node.commands[-1]
        elif kind is shparse.Sequence:
            if len(node.seps) == len(node.items) and node.seps[-1] == "&":
                return None
            node = node.items[-1]
        elif kind is shparse.AndOr:
            k = len(node.items) - 1
            while k > 0 and node.ops[k - 1] == "&&" and _passes_code(node.items[k]):
                k -= 1
            node = node.items[k]
        elif kind is shparse.Pipeline:
            if node.bang:
                return node
            node = node.commands[-1]
        elif kind is shparse.Subshell or kind is shparse.Group:
            node = node.body
        elif kind is shparse.Simple and not node.words and not node.redirs and node.assigns:
            sub = _last_substitution(node.assigns)
            if sub is None:
                return node
            node = sub.body
        else:
            return node


def _skip_options(words, i, with_arg):
    """Индекс первого слова после опций с i: «--» завершает опции, опция из with_arg без «=» берёт
    следующее слово. words — значения слов, None у слова с раскрытием."""
    while i < len(words) and words[i] is not None and words[i].startswith("-") and words[i] != "-":
        if words[i] == "--":
            return i + 1
        if words[i] in with_arg:
            i += 1
        i += 1
    return i


def _is_assignment(word, value):
    """Слово вида «имя=…» (аргумент env и sudo): по значению value (word.literal()) или, у слова с раскрытием, по
    его первому тексту."""
    if value is None:
        value = word.parts[0].text if word.parts and type(word.parts[0]) is shparse.Lit else ""
    return ASSIGNMENT.match(value) is not None


def _simple_answers(node):
    """Код 1 простой команды — ответ: после обёрток WRAPPERS и их аргументов «имя=…» — из CODE1_ANSWERS, git с
    подкомандой и флагом из GIT_CODE1_ANSWERS, «command -v» или «command -V». Слово с раскрытием на месте имени,
    опции или подкоманды — не ответ."""
    words = [word.literal() for word in node.words]
    i = 0
    while i < len(words):
        # Присваивания перед именем — в node.assigns; «имя=…» после обёртки — её аргумент.
        if i > 0 and _is_assignment(node.words[i], words[i]):
            i += 1
            continue
        if words[i] is None:
            return False
        name = os.path.basename(words[i])
        if name == "command" and i + 1 < len(words) and words[i + 1] in ("-v", "-V"):
            return True
        if name not in WRAPPERS:
            break
        i = _skip_options(words, i + 1, WRAPPERS[name])
        if name == "timeout":
            i += 1
    if i >= len(words) or words[i] is None:
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


def code1_is_answer(command, script=None):
    """Код выхода 1 команды — штатный ответ, а не сбой: команда, решающая код строки (_deciding по дереву
    shparse), — «[[ … ]]», конвейер с «!» или простая команда-ответ (_simple_answers). script — готовый
    shparse.parse(command). Разбор без дерева (вложенность глубже предела shparse, сбой разбора) — False, с
    предупреждением."""
    script = shparse.parse(command) if script is None else script
    kind = script.error.kind if script.error is not None else None
    if kind is not None:
        why = "вложенность глубже предела разбора" if kind == shparse.DEPTH else "сбой разбора"
        common.warn(f"команда не разобрана ({why}): её код выхода 1 считается неудачей")
        return False
    node = _deciding(script)
    kind = type(node)
    if kind is shparse.Pipeline or kind is shparse.Cond:
        return True
    return kind is shparse.Simple and bool(node.words) and _simple_answers(node)


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


def _recent(d, limit):
    """Последние limit записей: словарь хранит порядок вставки, свежие записи переставляются в конец."""
    return dict(list(d.items())[-limit:]) if len(d) > limit else d


def _state(counts, shown):
    counts, shown = _recent(counts, MAX_COUNTS), _recent(shown, MAX_SHOWN)
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
        n = counts.pop(key, 0) + 1
        counts[key] = n
        text = None
        if n >= REPEAT_THRESHOLD and shown.get(agent_id) != prompt_id:
            text = common.rule_texts(MODULE)
            if text is not None:
                shown.pop(agent_id, None)
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
    common.emit(common.context_output(f"{line}\n\n{text}", event))


if __name__ == "__main__":
    common.run_hook(main)
