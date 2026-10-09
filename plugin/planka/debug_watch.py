"""PostToolUse и PostToolUseFailure на Bash: повторная неудача той же команды подмешивает модуль debugging."""
import bisect
import hashlib
import os
import re
import shlex

import common
import shparse

MODULE = "debugging"
# Неудача с этого номера подряд подмешивает модуль.
REPEAT_THRESHOLD = 2
MAX_SHOWN_COMMAND = 200
# В файле состояния хранятся последние MAX_COUNTS ключей счётчика и последние MAX_SHOWN отметок показа.
MAX_COUNTS = 500
MAX_SHOWN = 100
# Имя команды, обёртки и подкоманда git разбираются только по началу сегмента, до _HEAD_LIMIT символов.
_HEAD_LIMIT = 4096

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


def _paren_pairs(command):
    """Закрывающая «)» каждой «(» вне кавычек, комментариев и тел heredoc: {индекс «(»: индекс «)»}. «<<» внутри
    «$[…]» и внутри открытой «((» — сдвиг, не heredoc, даже если «((» окажется двумя «(» подоболочек: так её
    содержимое читает bash, проверяя арифметику (parse_arith_cmd). Один проход, время линейно."""
    # stack — открытые «(»: (индекс, начинает ли она «((»); arith — сколько из них начинают «((».
    pairs, stack, pending = {}, [], []
    arith = 0
    # Глубина «[» внутри «$[…]», считая саму «$[»; 0 — вне «$[…]».
    brackets = 0
    quote, boundary, i, n = None, True, 0, len(command)
    while i < n:
        c = command[i]
        if quote is None and c == "#" and boundary:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if quote is None and c == "\n" and pending:
            i = _skip_heredocs(command, i + 1, pending)
            pending = []
            boundary = True
            continue
        step = 2 if c == "\\" and quote != "'" else 1
        boundary = quote is None and (c.isspace() or c in "&()<>;|")
        if quote is None:
            if c == "<" and not command.startswith("<<<", i) and not command.startswith("<<<", i - 1) \
                    and not arith and not brackets:
                m = HEREDOC.match(command, i)
                if m:
                    word = next(g for g in m.groups()[1:] if g is not None)
                    pending.append((word, m.group(1) == "-"))
            if c == "(":
                opens = command.startswith("((", i)
                stack.append((i, opens))
                arith += opens
            elif c == ")" and stack:
                j, opens = stack.pop()
                arith -= opens
                pairs[j] = i
            elif brackets and c in "[]":
                brackets += 1 if c == "[" else -1
            elif command.startswith("$[", i):
                brackets, step = brackets + 1, 2
            elif c in "'\"":
                quote = c
        elif c == quote:
            quote = None
        i += step
    return pairs


def _segments(command):
    """Непустые команды строки с разделителем перед каждой: [(разделитель, текст)]. Разделители вне кавычек —
    «|», «|&», «||», «&&», «;» и перевод строки; у первой команды разделитель "". Одиночный «&» и скобки не
    разделяют; внутри «[[ … ]]», «$((…))», «((…))» и «$[…]» разделителей нет («((» без закрытия «))» — две «(»
    подоболочек); тела heredoc отбрасываются, а «<<» внутри арифметики — сдвиг, не heredoc. Комментарий от «#»
    в начале слова (после пробела, «;», «&», «|», скобки, «<», «>» или в начале строки) до конца строки
    отбрасывается."""
    segments = [["", []]]
    i, n = 0, len(command)
    quote = None
    # Предыдущий символ — незаэкранированный разрыв слова вне кавычек: после него «#» начинает комментарий.
    boundary = True
    in_test = False
    pending = []
    # Последний « ]]»: «[[ » открывает проверку, только если закрытие стоит дальше (один поиск, не на каждый «[[ »).
    last_close = command.rfind(" ]]")
    # Глубина скобок внутри арифметики «$((…))», «((…))» и «$[…]»; 0 — вне её.
    arith = 0
    # Открытые «[» внутри «$[…]»: «$» — сама «$[…]», её «]» закрывает арифметику; «[» — индекс внутри неё. Вне
    # «$[…]» «[» и «]» — обычные символы.
    brackets = []
    # «((» в начале команды — арифметика, только если «)», закрывающая вторую «(», стоит перед «)» первой (bash,
    # parse_arith_cmd); иначе это подоболочка в подоболочке: «((cd a && make); false)».
    pairs = _paren_pairs(command) if "((" in command else {}
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
        if quote is None and not in_test and not arith and (c in "|;\n" or command.startswith("&&", i)):
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
                elif brackets and c in "[]":
                    if c == "[":
                        brackets.append("[")
                    elif brackets.pop() == "$":
                        arith -= 1
                elif command.startswith("$[", i):
                    arith, step = arith + 1, 2
                    brackets.append("$")
                elif command.startswith("$((", i):
                    arith, step = arith + 2, 3
                elif boundary and command.startswith("((", i) and i + 1 in pairs \
                        and pairs.get(i) == pairs[i + 1] + 1:
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


# Операторы, разделяющие команды строки: «|», «|&», «||», «&&», «;», перевод строки; «\\» с переводом строки —
# продолжение строки, не разделитель. Одиночный «&» и скобки не разделяют.
_SEPARATOR = re.compile(r"\\\n|\|[|&]?|&&|;|\n")
# Узлы, внутри которых разделителей нет: слова (кавычки, подстановки, скобки массива), перенаправления
# («>|» — не конвейер), «((…))», «[[ … ]]».
_NO_SPLIT = (shparse.Word, shparse.Redir, shparse.ArithCmd, shparse.Arith, shparse.Cond)


def _tree_skip_heredocs(command, script, nodes=None):
    """Диапазоны (начало, конец) тел heredoc вместе со строкой терминатора и её переводом строки; терминатора нет —
    до конца текста; терминатор со знаком конца подстановки в остатке строки («E)») — до знака. Тела берутся из
    дерева script (shparse.parse(command)), а не из порядка открытий. Heredoc, тело которого bash не читал (в
    теле `` `…` `` оно в самой строке подстановки), диапазона не даёт. nodes — уже обойдённые узлы script."""
    n = len(command)
    out = []
    for node in shparse.walk(script) if nodes is None else nodes:
        if type(node) is not shparse.Heredoc or node.body_start == node.end:
            continue
        i = node.body_end
        if i >= n:
            out.append((node.body_start, n))
            continue
        if node.strip_tabs:
            while i < n and command[i] == "\t":
                i += 1
        if command.startswith(node.term, i):
            i += len(node.term)
            if i < n and command[i] != "\n":
                out.append((node.body_start, i))
                continue
        end = command.find("\n", i)
        out.append((node.body_start, n if end < 0 else end + 1))
    return out


def _merge(ranges):
    """Отсортированные непересекающиеся диапазоны: пересекающиеся и смежные склеены."""
    out = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _covered(ranges, starts, i):
    """Конец диапазона из ranges (starts — их начала), накрывающего позицию i; не накрыта — None."""
    k = bisect.bisect_right(starts, i) - 1
    return ranges[k][1] if k >= 0 and i < ranges[k][1] else None


def _tree_paren_pairs(command, script=None):
    """Закрывающая «)» каждой «(» вне кавычек, комментариев и тел heredoc: {индекс «(»: индекс «)»}. Кавычки,
    комментарии и heredoc берутся из дерева (shparse.parse), скобки считаются по тексту; «\\» вне кавычек
    экранирует следующий символ."""
    script = shparse.parse(command) if script is None else script
    nodes = list(shparse.walk(script))
    cuts = list(_comment_ranges(nodes)) + _tree_skip_heredocs(command, script, nodes)
    cuts += [(node.start, node.end) for node in nodes if type(node) in (shparse.SQ, shparse.DQ, shparse.AnsiC)]
    cuts = _merge(cuts)
    starts = [start for start, _ in cuts]
    pairs, stack = {}, []
    skip_to = 0
    for m in re.finditer(r"[()\\]", command):
        i = m.start()
        if i < skip_to or _covered(cuts, starts, i) is not None:
            continue
        c = m.group()
        if c == "\\":
            skip_to = i + 2
        elif c == "(":
            stack.append(i)
        elif stack:
            pairs[stack.pop()] = i
    return pairs


def _comment_ranges(nodes):
    """Диапазоны комментариев всех разборов среди узлов дерева (тело подстановки хранит свои)."""
    for node in nodes:
        if type(node) is shparse.Script:
            yield from node.comments


def _tree_segments(command, script=None):
    """То же, что _segments, по дереву shparse: непустые команды строки с разделителем перед каждой. Разделитель —
    оператор между командами (_SEPARATOR) вне слов, перенаправлений, «((…))», «[[ … ]]», комментариев и тел
    heredoc; внутри подстановки, группы слов и скобок массива разделителей нет; комментарии и тела heredoc из
    текста команд убраны. После синтаксической ошибки bash не исполняет остаток: от начала команды с ошибкой он
    одна команда без разделителей. script — готовый shparse.parse(command)."""
    script = shparse.parse(command) if script is None else script
    nodes = list(shparse.walk(script))
    cuts = _merge(list(_comment_ranges(nodes)) + _tree_skip_heredocs(command, script, nodes))
    cut_starts = [start for start, _ in cuts]
    fixed = _merge([(node.start, node.end) for node in nodes if isinstance(node, _NO_SPLIT)]
                   + [(item[0][0].start, item[0][-1].end) for node in nodes if type(node) is shparse.Case
                      for item in node.items if item[0]]
                   + [(a, b - 1 if command[b - 1:b] == "\n" else b) for a, b in script.dropped or ()] + cuts)
    fixed_starts = [start for start, _ in fixed]
    # Фатальная ошибка: bash не исполняет ни команды с ошибкой (всю строку верхнего уровня, где она), ни остаток;
    # границы ищутся только до конца последней прочитанной команды и за пробелами после неё.
    fatal = None
    if script.error is not None and script.error.fatal:
        fatal = script.commands[-1].end if script.commands else 0
    # Границы команд: (начало оператора, конец, сам оператор).
    bounds = []
    for m in _SEPARATOR.finditer(command):
        if fatal is not None and m.start() >= fatal and command[max(fatal, bounds[-1][1] if bounds else 0):
                                                               m.start()].strip():
            break
        if m.group()[0] == "\\" or _covered(fixed, fixed_starts, m.start()) is not None:
            continue
        bounds.append((m.start(), m.end(), m.group()))
    bounds.append((len(command), len(command), None))
    segments = []
    sep, begin = "", 0
    for start, end, op in bounds:
        text = _without(command, begin, start, cuts, cut_starts)
        if text.strip():
            segments.append((sep, text))
        if op is None:
            break
        sep, begin = op, end
    return segments


def _without(command, start, end, cuts, starts):
    """command[start:end] без диапазонов cuts (starts — их начала)."""
    out = []
    i = start
    k = max(bisect.bisect_right(starts, start) - 1, 0)
    while k < len(cuts) and cuts[k][0] < end:
        a, b = cuts[k]
        if b > i:
            if a > i:
                out.append(command[i:a])
            i = max(i, b)
        k += 1
    if i < end:
        out.append(command[i:end])
    return "".join(out)


def _head_words(text):
    """Слова начала text (до _HEAD_LIMIT символов) по правилам shlex; None — не разбирается. Слово, которое
    обрезал предел, отбрасывается; кавычка, не закрытая в пределах, у обрезанной строки не ошибка."""
    head = text[:_HEAD_LIMIT]
    cut = len(text) > len(head)
    lexer = shlex.shlex(head, posix=True)
    lexer.whitespace_split = True
    # «#» внутри слова («VAR=a#b») — не комментарий: слова после него нужны.
    lexer.commenters = ""
    words = []
    try:
        words.extend(lexer)
    except ValueError:
        # Недочитанное слово в кавычках shlex не отдаёт — отбрасывать нечего.
        return words if cut else None
    if cut and words and not head[-1].isspace():
        words.pop()
    return words


def _passes_code(text):
    """Команда не возвращает 1 сама: первое слово из PASS_THROUGH и вне кавычек и экранирования нет
    «<» и «>» (перенаправление может не открыться и дать 1; «echo "a > b"» — не перенаправление)."""
    if re.search(r"[<>]", UNQUOTED.sub("", text)):
        return False
    words = _head_words(text)
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
    words = _head_words(last)
    if words is None:
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
