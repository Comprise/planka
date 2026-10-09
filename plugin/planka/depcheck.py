"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import bisect
import re
import shlex
from collections import deque
from itertools import chain, islice

import pkgmanagers
import shparse

DEP_OK_MARKER = "PLANKA_DEP_OK=1"

# Присваивание перед командой: `A=1`, `PATH+=:/x`, элементу массива `a[0]=1`. Имя и `=` без кавычек — иначе bash
# исполняет слово как имя команды (`'A=1' npm i x` — команда `A=1`); индекс в `[…]` может быть в кавычках: у слова
# с индексом начало для этой проверки даёт _split (_subscript_head).
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
# Начало присваивания элементу массива до индекса: `a["k y"]=1` — начало слова до кавычки `a[`.
_SUBSCRIPT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\[")
# Ключевые слова shell перед командой; `coproc` без имени запускает следующую команду.
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "coproc"}
# Составные команды, перед которыми `coproc` берёт имя: `coproc NAME { …; }`.
_COMPOUND = {"{", "while", "until", "if", "for", "select", "case", "[[", "(("}
# Обёртки, запускающие следующую команду: флаги со значением и число позиционных слов обёртки
# (длительность у timeout) перед командой.
_WRAPPERS = {
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "nohup": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    # `-a`/`--argv0`, `--env0-from` — GNU coreutils 9; `-P`, `-L`, `-U` — env BSD и macOS.
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0", "--env0-from", "-P", "-L", "-U"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    # Флаги со значением sudo 1.9 (`sudo --help`, optstring `+Aa:BbC:c:D:Eeg:Hh::iKklNnPp:R:r:SsT:t:U:u:Vv`).
    "sudo": (frozenset({"-a", "-c", "-C", "-D", "-g", "-h", "-p", "-R", "-r", "-t", "-T", "-U", "-u", "--auth-type",
                        "--login-class", "--close-from", "--chdir", "--group", "--host", "--prompt", "--chroot",
                        "--role", "--type", "--command-timeout", "--other-user", "--user"}), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "setsid": (frozenset(), 0),
    "busybox": (frozenset(), 0),
    "ionice": (frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"}), 0),
    # Без `-x` watch склеивает слова команды пробелом в строку `sh -c` (_WATCH_EXEC).
    "watch": (frozenset({"-n", "--interval", "-q", "--equexit", "-s", "--shotsdir"}), 0),
    # Файл блокировки перед командой; `flock файл -c '…'` — строка оболочки.
    "flock": (frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}), 1),
    # `su [-] [пользователь] -c '…'`; без `-c` слова за пользователем — аргументы оболочки (`su app -- -c '…'`) или
    # программы `-s`, без них оболочка читает stdin;
    # `runuser -u пользователь [--] команда` запускает команду без оболочки (_C_WRAPPERS).
    "su": (frozenset({"-c", "--command", "--session-command", "-s", "--shell", "-g", "--group", "-G",
                      "--supp-group", "-w", "--whitelist-environment"}), 0),
    "runuser": (frozenset({"-c", "--command", "--session-command", "-s", "--shell", "-g", "--group", "-G",
                           "--supp-group", "-w", "--whitelist-environment", "-u", "--user"}), 0),
    # Пакеты из stdin xargs не видны: команду установки без пакета под xargs отдаёт _doubt.
    "xargs": (frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--arg-file", "--delimiter",
                         "--max-args", "--max-procs", "--max-lines", "--max-chars", "--eof",
                         "--process-slot-var"}), 0),
}
# Флаг env, чьё значение — строка команды, разбиваемая на слова.
_ENV_SPLIT = ("-S", "--split-string")
# Обёртки, чья команда — строка оболочки `-c`/`--command`, у runuser — слова после `-u`, иначе — оболочка или
# программа `-s` с аргументами за именем пользователя (_c_wrapper); у `flock` строка `-c` стоит за файлом.
_C_WRAPPERS = {"su", "runuser"}
# Флаг `-x`/`--exec` watch: команда запускается без `sh -c`. По optstring watch `+bCcefd::ghq:n:prs:twvx` буква
# со значением (`n`, `q`, `s`, необязательным — `d`) забирает остаток склейки: `-xn5` — `-x`, `-n5x` и `-dx` — нет.
_WATCH_EXEC = re.compile(r"^(?:--exec$|-[bCcefghprtwv]*x)")
# Оператор перенаправления в начале слова: `2>&1`, `>log`, `&>/dev/null`, `<<EOF`; без цели в слове
# (`>`, `2>`) цель — следующее слово.
_REDIRECT = re.compile(r"^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:&>>|&>|>>|>&|>\||>|<<<|<<-|<<|<&|<>|<)")
# Глубина разбора вложенных команд: `sh -c`, `eval`, подстановка в двойных кавычках.
_MAX_DEPTH = 4
# Обёрток, снимаемых с одного сегмента (_command_info); слова за последней остаются как есть.
_MAX_WRAPPERS = 16
# `gem install`: псевдоним `i` (ALIAS_COMMANDS RubyGems) и однозначные сокращения имени (CommandManager, от `ins`:
# `in` — ещё и `info`).
_GEM_INSTALL = {"i", "ins", "inst", "insta", "instal", "install"}
# `composer require`, его псевдоним `r` и однозначные сокращения имени (Symfony Console, от `req`).
_COMPOSER_REQUIRE = {"r", "req", "requ", "requi", "requir", "require"}
_ADD_FLAGS = {"poetry": pkgmanagers._POETRY_VALUE_FLAGS, "cargo": pkgmanagers._CARGO_VALUE_FLAGS, "bundle": pkgmanagers._BUNDLE_VALUE_FLAGS}
# Пакеты, без которых окружение conda не создаётся: `conda create -n x python=3.11` нового пакета не вносит.
_CONDA_BASE = {"python", "pip"}
# Подкоманды, исполняющие следующую команду или программу пакета (`npm exec pnpm add x`, `bundle exec gem install
# x`), как npx.
_EXECS = {"npm": {"exec", "x"}, "pnpm": {"dlx", "exec"}, "yarn": {"dlx", "exec"}, "bun": {"x"},
          "bundle": {"exec"}}
# Запускатели пакета npm (`npx pnpm add x`): флаги со значением перед пакетом.
_NPX_VALUE_FLAGS = {"-p", "--package", "-c", "--call", "--cache", "--userconfig"}
# Версия в спецификации пакета: `pnpm@9`, `@scope/x@1`.
_VERSION = re.compile(r"(?<=.)@[^/]*$")
# Псевдонимы `npm install`.
_NPM_INSTALL = {"install", "i", "add", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal",
                "isntall", "it", "install-test"}
# Сколько первых символов сегмента _command разбирает на слова: пакет, названный дальше, не виден — команду
# установки без пакета в длинном сегменте отдаёт _doubt.
_WORDS_LIMIT = 4096
# Пакет вместо слов, которых разбор не видит (stdin xargs, хвост сегмента за _WORDS_LIMIT): с `@`, чтобы
# `go install` считал его пакетом с версией.
_STDIN_PACKAGE = "x@1"
# Режимы _is_add при сомнении: _LOOSE — флаг перед подкомандой берёт следующее слово значением (_loosened),
# _MASKED — `--dry-run` значением флага менеджера не пробный прогон (_dry_run), _DEEP — команда глубже _MAX_DEPTH
# ищется по словам (_flat_add), _COMPUTED — подкоманда менеджера или имя вложенной команды с глаголом установки —
# подстановка или переменная.
_LOOSE, _MASKED, _DEEP, _COMPUTED = "loose", "masked", "deep", "computed"
# Доводы сомнения dependency_doubt.
_WHY_FLAG = "флаг перед подкомандой вне известных наборов: если он берёт значение, подкоманда — установка"
_WHY_DRY = "`--dry-run` стоит значением другого флага: пробного прогона может не быть"
_WHY_XARGS = "пакеты приходят из stdin xargs, разбор их не видит"
_WHY_DEPTH = f"вложенность глубже {_MAX_DEPTH} уровней разобрана только по словам"
_WHY_CUT = f"команда длиннее {_WORDS_LIMIT} символов: пакет может стоять дальше"
_WHY_NAME = "имя команды — подстановка или переменная: менеджер известен только при исполнении"
_WHY_SUBCOMMAND = ("подкоманда менеджера — подстановка или переменная: установка ли это, известно только при "
                   "исполнении")
_WHY_WRAPPERS = (f"больше {_MAX_WRAPPERS} обёрток подряд: команда за ними не разобрана; маркер согласия — в начале "
                 "сегмента, до обёрток")
_WHY_COMPUTED = ("строка команд вычисляется при исполнении (переменная, подстановка, вывод программы) и отдаётся "
                 "оболочке: её команды не видны")
_WHY_LAUNCHER = ("программа, неизвестная разбору, получает словами менеджер пакетов и его команду установки: она "
                 "может их запустить")
# Глаголы установки за именем менеджера в словах неизвестной программы (_launches_install; флаг за менеджером —
# тоже: `pacman -S`, `npm -g i`): `go get`, `pipx inject`, `dnf in`, `bun a`, `apt satisfy`.
_LAUNCH_VERBS = pkgmanagers._INSTALL_VERBS | {"get", "in", "inject", "a", "satisfy"}
# Сколько пар «менеджер, глагол или флаг» в словах неизвестной программы разбирает _launches_install; больше —
# сомнение.
_MAX_LAUNCH_PAIRS = 16
# Программы, чьи слова — данные, а не команда: текст, поиск, справка, git (команды, которые git запускает сам, —
# _git_runs).
_DATA_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "git", "gh", "cat", "sed", "awk", "head",
                  "tail", "jq", "wc", "ls", "diff", "man", "tldr", "help", "info", "apropos", "which", "type",
                  "whereis", "sort", "uniq", "tee", "less", "more", "test", "[", "[[", "true", "false", ":", "read",
                  "export", "declare", "local", "set", "unset", "alias"}


# Escape-последовательности `$'…'` bash: однобуквенные; `\nnn`, `\xHH`, `\uHHHH`, `\UHHHHHHHH`, `\cX` — в _ansi_c.
_ANSI_C_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                   "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
_ANSI_C_NUMBER = re.compile(r"[0-7]{1,3}|x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}")


def _ansi_c(text, i):
    """Текст строки `$'…'`, тело которой начинается с i, с раскрытыми escape-последовательностями bash, и позиция
    за закрывающей `'`. Неизвестная последовательность остаётся с `\\`; символ с кодом 0 обрывает строку."""
    out, n, cut = [], len(text), False
    while i < n and text[i] != "'":
        c = text[i]
        if c != "\\" or i + 1 >= n:
            piece, i = c, i + 1
        elif text[i + 1] in _ANSI_C_ESCAPES:
            piece, i = _ANSI_C_ESCAPES[text[i + 1]], i + 2
        elif text[i + 1] == "c" and i + 2 < n:
            piece, i = chr(ord(text[i + 2]) & 0x1F), i + 3
        else:
            m = _ANSI_C_NUMBER.match(text, i + 1)
            if m:
                digits = m[0]
                code = int(digits, 8) if digits[0].isdigit() else int(digits[1:], 16)
                piece, i = (chr(code) if code <= 0x10FFFF else "�"), m.end()
            else:
                piece, i = text[i:i + 2], i + 2
        cut = cut or piece == "\0"
        if not cut:
            out.append(piece)
    return "".join(out), i + 1


# Сколько символов слов (и шагов) создаёт раскрытие фигурных скобок всех слов одного вызова dependency_add или
# dependency_doubt — всех его сегментов и вложенных строк (_split_cut); слова с места, где предел кончился, не видны:
# сомнение у любой команды (_doubt).
_BRACE_BUDGET = 2 * 4096
_WHY_BRACES = (f"раскрытие фигурных скобок в команде дало больше {_BRACE_BUDGET} символов слов: пакет может стоять "
               "дальше")
# Последовательность `{x..y}` и `{x..y..шаг}`: целые или одиночные буквы.
_BRACE_SEQ = re.compile(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?|([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?")
_ZERO_PADDED = re.compile(r"-?0\d")
# Длиннее последовательность не бывает: края — целые 64 бит.
_BRACE_SEQ_MAX = 64


def _brace_sequence(text):
    """Слова последовательности `{x..y[..шаг]}` по bash (text — без скобок): шаг по модулю, 0 — 1, направление —
    от x к y, целые с ведущим нулём дополняются нулями до ширины длиннейшего края; None — не последовательность."""
    m = _BRACE_SEQ.fullmatch(text)
    if not m:
        return None
    step = abs(int(m[3] or m[6] or 1)) or 1
    if m[1] is not None:
        a, b = int(m[1]), int(m[2])
        width = max(len(m[1]), len(m[2])) if _ZERO_PADDED.match(m[1]) or _ZERO_PADDED.match(m[2]) else 0
        values = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
        return (str(v).zfill(width) for v in values)
    a, b = ord(m[4]), ord(m[5])
    return (chr(v) for v in (range(a, b + 1, step) if a <= b else range(a, b - 1, -step)))


def _brace_group(w):
    """Первая слева группа `{…}`, которую раскрывает bash, в слове w (пары (символ, раскрывается ли)): (начало, конец,
    варианты — списки пар) или None. Группа раскрывается с запятой своего уровня или последовательностью; без них
    скобки — текст, вложенные группы раскрываются."""
    stack, best = [], None
    for k, (c, mark) in enumerate(w):
        if not mark:
            continue
        if c == "{":
            stack.append((k, []))
        elif c == "," and stack:
            stack[-1][1].append(k)
        elif c == "}" and stack:
            start, commas = stack.pop()
            if best is not None and best[0] < start:
                continue
            if commas:
                bounds = [start, *commas, k]
                best = (start, k, [w[a + 1:b] for a, b in zip(bounds, bounds[1:])])
            elif k - start <= _BRACE_SEQ_MAX:
                seq = _brace_sequence("".join(ch for ch, _ in w[start + 1:k]))
                if seq is not None:
                    best = (start, k, ([(ch, False) for ch in v] for v in seq))
    return best


def _brace_words(w, budget):
    """Слова, в которые bash раскрывает фигурные скобки слова w (пары (символ, раскрывается ли)), по порядку; пустые
    слова раскрытия выброшены. budget — список из одного числа, остаток предела _BRACE_BUDGET (_split_cut): каждое
    слово раскрытия, в том числе промежуточное, тратит свою длину и 1; остаток меньше нуля — предел кончился на
    первой группе. Второе значение — предел кончился: тогда слова — только те, что идут до места, где он
    кончился."""
    out, todo = [], [w]
    while todo:
        word = todo.pop()
        group = _brace_group(word)
        if group is None:
            text = "".join(c for c, _ in word)
            if text:
                out.append(text)
            continue
        start, end, alternatives = group
        pre, post = word[:start], word[end + 1:]
        made = []
        for alt in islice(alternatives, max(budget[0], 0) + 1):
            budget[0] -= 1 + len(pre) + len(alt) + len(post)
            if budget[0] < 0:
                # Видны слова раскрытия до этого места без своих групп: группа в слове — ещё не раскрытые слова.
                for x in made:
                    if _brace_group(x) is not None:
                        break
                    if x:
                        out.append("".join(c for c, _ in x))
                return out, True
            made.append(pre + alt + post)
        todo.extend(reversed(made))
    return out, False


def _subscript_head(word, plain):
    """Начало слова word с индексом массива (`a[…`) для _ENV_ASSIGN: `имя[]=` или `имя[]+=`, если слово —
    присваивание элементу, иначе `имя[`. plain — позиции `[`, `]`, `+`, `=`, `}` и `$` перед `{` слова вне кавычек
    и `\\`, по возрастанию. Индекс кончается на `]` вне кавычек и вне `${…}`, закрывающей вложенные `[…]`, за ней —
    `=` или `+=` вне кавычек (bash 5.3: `a["]"]=1`, `a[b[1]]=1`, `a[${x:-]}]=1` — присваивания, `a[x]"="1` — нет).
    `${…}` кончается на первой `}` вне кавычек, вложенные `${…}` учтены."""
    start = _SUBSCRIPT.match(word).end()
    unquoted = set(plain)
    depth = 1
    # Глубина открытых `${…}`: скобки индекса в них не считаются.
    param = 0
    for k in plain:
        if k < start:
            continue
        if word[k] in "$}":
            param += 1 if word[k] == "$" else -1 if param else 0
            continue
        if param:
            continue
        depth += 1 if word[k] == "[" else -1 if word[k] == "]" else 0
        if depth == 0:
            if k + 1 in unquoted and word[k + 1] == "=":
                return word[:start] + "]="
            if k + 2 in unquoted and word[k + 1:k + 3] == "+=":
                return word[:start] + "]+="
            break
    return word[:start]


def _split(text, braces=False):
    """Слова текста (_split_cut) без признака предела раскрытия."""
    return _split_cut(text, braces)[0]


def _split_cut(text, braces=False, budget=None):
    """Слова по правилам POSIX shell: кавычки и `\\` сняты, `\\` с переводом строки удалены, `$'…'` раскрыта
    (_ansi_c). Каждое слово — пара (слово, начало слова до первой кавычки или `\\`; у слова `имя[…` — _subscript_head):
    оператор перенаправления и присваивание узнаются только в этом начале.

    Подстановки `$(…)` и `` `…` `` внутри двойных кавычек, `` `…` `` вне кавычек (до первой неэкранированной
    обратной кавычки, _close_backtick) и подстановка функции `${ …; }`, `${| …; }` (_close_paren с funsub) входят в
    слово текстом, с их кавычками. Пробел и перевод строки внутри `${…}` вне кавычек (до первой `}` вне кавычек,
    вложенные `${…}` учтены) слово не кончают; `$$` — параметр целиком, `{` за ним не начинает `${…}`. Незакрытая
    кавычка продолжается до конца текста. При braces фигурные скобки вне кавычек и `${…}` раскрываются в слова
    (_brace_words), как в словах команды bash, кроме присваиваний (`A={a,b}`) и перенаправлений с целью;
    начало у слов раскрытия пустое: они не присваивания, не ключевые слова и не перенаправления.

    budget — список из одного числа, остаток предела _BRACE_BUDGET, общий для нескольких текстов (_brace_words);
    None — свой полный предел. Возвращает (слова, кончился ли предел раскрытия): если кончился, слова — только те,
    что идут до места, где он кончился.
    """
    out = []
    buf, lead, started, marks, size, param = [], None, False, [], 0, 0
    # Позиции `[`, `]`, `+`, `=`, `}` и `$` перед `{` слова вне кавычек: для индекса массива (_subscript_head).
    plain = []
    # Предыдущее слово — оператор перенаправления без цели: это слово — цель.
    target = False
    budget = [_BRACE_BUDGET] if budget is None else budget
    cut = False

    def finish():
        nonlocal target, cut
        if cut:
            return
        word = "".join(buf)
        head = word if lead is None else lead
        if _SUBSCRIPT.match(head):
            head = _subscript_head(word, plain)
        redirect = _REDIRECT.match(head)
        if marks and not target and not redirect and not _ENV_ASSIGN.match(head):
            marked = set(marks)
            words, cut = _brace_words([(c, k in marked) for k, c in enumerate(word)], budget)
            if cut or words != [word]:
                out.extend((w, "") for w in words)
                target = False
                return
        out.append((word, head))
        target = bool(redirect) and len(word) == redirect.end()

    # Глубина открытых `${…}` вне кавычек: пробел и перевод строки в них — часть слова (`${x:- }npm`), как в bash.
    param_depth = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and text.startswith("\n", i + 1):
            i += 2
            continue
        if c in " \t\n" and not param_depth:
            if started:
                finish()
                buf, lead, started, marks, size, param = [], None, False, [], 0, 0
                plain = []
            i += 1
            continue
        started = True
        if c in "'\"\\" or c == "$" and text.startswith("'", i + 1):
            if lead is None:
                lead = "".join(buf)
        piece = c
        if c == "'":
            end = text.find("'", i + 1)
            end = n if end < 0 else end
            piece = text[i + 1:end]
            i = end + 1
        elif c == "$" and text.startswith("'", i + 1):
            piece, i = _ansi_c(text, i + 2)
        elif c == '"':
            i += 1
            parts = []
            while i < n and text[i] != '"':
                if text.startswith("$$", i):
                    parts.append("$$")
                    i += 2
                    continue
                funsub = _funsub_open(text, i)
                if funsub:
                    end = min(_close_paren(text, i + funsub, funsub=True), n - 1)
                    parts.append(text[i:end + 1])
                    i = end + 1
                    continue
                if text.startswith("$(", i):
                    arith = text.startswith("$((", i)
                    end = min(_close_paren(text, i + (3 if arith else 2), arith), n - 1)
                    parts.append(text[i:end + 1])
                    i = end + 1
                    continue
                if text[i] == "`":
                    end = _close_backtick(text, i + 1)
                    parts.append(text[i:end + 1])
                    i = end + 1
                    continue
                if text[i] == "\\" and text.startswith("\n", i + 1):
                    i += 2
                    continue
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '$`"\\':
                    i += 1
                parts.append(text[i])
                i += 1
            piece = "".join(parts)
            i += 1
        elif c == "\\":
            piece = text[i + 1:i + 2]
            i += 2
        elif c == "`":
            end = min(_close_backtick(text, i + 1), n - 1)
            piece = text[i:end + 1]
            i = end + 1
        elif c == "$" and text.startswith("$", i + 1):
            # `$$` — параметр целиком: `{` за ним не начинает `${…}`.
            piece = "$$"
            i += 2
        elif c == "$" and _funsub_open(text, i):
            # Подстановка функции `${ …; }` входит в слово текстом, как `$(…)`.
            end = min(_close_paren(text, i + _funsub_open(text, i), funsub=True), n - 1)
            piece = text[i:end + 1]
            i = end + 1
        elif braces and c == "$" and text.startswith("{", i + 1):
            plain.append(size)
            piece = "${"
            param += 1
            param_depth += 1
            i += 2
        else:
            if c == "$" and text.startswith("{", i + 1):
                param_depth += 1
            elif c == "}" and param_depth:
                param_depth -= 1
            if c in "[]+=}" or c == "$" and text.startswith("{", i + 1):
                plain.append(size)
            if braces and c in "{,}":
                if param:
                    param += 1 if c == "{" else -1 if c == "}" else 0
                else:
                    marks.append(size)
            i += 1
        buf.append(piece)
        size += len(piece)
    if started:
        finish()
    return out, cut


def _drop_redirect_pairs(pairs):
    """Пары (слово, начало) без перенаправлений и их целей; оператор в кавычках (`">"`) — обычное слово."""
    out = []
    i = 0
    while i < len(pairs):
        word, lead = pairs[i]
        m = _REDIRECT.match(lead)
        if m:
            i += 1 if len(word) > m.end() else 2
            continue
        out.append(pairs[i])
        i += 1
    return out


# Текущий вызов dependency_add или dependency_doubt: (остаток предела раскрытия _BRACE_BUDGET — список из одного
# числа, разобранные сегменты _command_info по тексту) или None вне вызова.
_call = None


def _within_call(find, command):
    """find(command, 0) с одним пределом раскрытия _BRACE_BUDGET на весь вызов: сегмент, прочитанный разбором
    несколько раз, разбирается и тратит предел один раз (_command_info)."""
    global _call
    outer = _call
    _call = ([_BRACE_BUDGET], {})
    try:
        return find(command, 0)
    finally:
        _call = outer


def _command(segment):
    """Слова команды сегмента и стоит ли маркер согласия среди её ведущих присваиваний (_command_info)."""
    words, marker, _, _, _ = _command_info(segment)
    return words, marker


def _command_info(segment):
    """_command_info_parsed сегмента; в вызове dependency_add и dependency_doubt (_within_call) — с общим пределом
    раскрытия и разобранный один раз: без пробелов по краям, как его читают _find и _find_doubt. Слова — свой
    список."""
    if _call is None:
        return _command_info_parsed(segment, None)
    budget, parsed = _call
    segment = segment.strip()
    if segment not in parsed:
        parsed[segment] = _command_info_parsed(segment, budget)
    words, *rest = parsed[segment]
    return (list(words), *rest)


def _command_info_parsed(segment, budget):
    """Слова команды сегмента, стоит ли маркер согласия среди её ведущих присваиваний, снята ли обёртка
    `xargs`, довод, если слова команды не видны целиком — _WHY_BRACES для слов за пределом раскрытия фигурных скобок
    (budget — его остаток, _split_cut), _WHY_CUT для слов-не-флагов, начатых за её первыми _WORDS_LIMIT символами,
    иначе None, — и стоит ли за _MAX_WRAPPERS снятыми обёртками ещё обёртка.

    Фигурные скобки слов раскрыты (_split_cut с braces). Перенаправления с их целью выброшены; ведущие присваивания
    (и элементу массива `a[0]=1`), ключевые слова shell, `function` и `coproc` с именем и до _MAX_WRAPPERS обёрток
    (`sudo`, `env`, `timeout`, `nice`, `xargs`, `stdbuf` и др.) с их флагами сняты; строка `env -S`
    разбита на слова, `su`/`runuser`/`flock` с `-c` и `watch` без `-x` — команда `sh -c`, `su`/`runuser` без `-c` и
    `-u` — оболочка (_c_wrapper). Предел _WORDS_LIMIT считается от первого слова команды: длинные присваивания и
    обёртки перед ней его не прячут. Комментарий из сегмента уже убран _segments.
    """
    # Очередь: снятие слова спереди и вставка строки `env -S` — без копирования хвоста, проход линеен.
    pairs, braced = _split_cut(segment, braces=True, budget=budget)
    words = deque(_drop_redirect_pairs(pairs))
    marker = xargs = False
    wrappers = 0
    while words:
        word, lead = words[0]
        if _ENV_ASSIGN.match(lead):
            # Маркер — слово целиком без кавычек и `\`, только `=`.
            marker = marker or word == lead == DEP_OK_MARKER
            words.popleft()
        elif word == lead == "function" or word == lead == "coproc" and len(words) > 2 and (
                words[2][0] == words[2][1] and words[2][0] in _COMPOUND):
            # `function f { …; }`, `coproc NAME { …; }`: имя функции или сопроцесса — не команда.
            words.popleft()
            if words:
                words.popleft()
        elif word in _KEYWORDS and word == lead:
            words.popleft()
        elif pkgmanagers._basename(word) in _WRAPPERS and wrappers < _MAX_WRAPPERS:
            wrappers += 1
            name = pkgmanagers._basename(word)
            xargs = xargs or name == "xargs"
            value_flags, operands = _WRAPPERS[name]
            words.popleft()
            if name in _C_WRAPPERS:
                pkgmanagers._c_wrapper(words, value_flags)
                continue
            shell = name == "watch"
            while words and words[0][0].startswith("-"):
                flag = words.popleft()[0]
                if flag == "--":
                    break
                if name == "watch" and _WATCH_EXEC.match(flag):
                    shell = False
                if name == "env" and (flag in _ENV_SPLIT or flag.startswith(("-S", "--split-string="))):
                    if flag in _ENV_SPLIT:
                        value = words.popleft()[0] if words else ""
                    else:
                        value = flag[2:] if flag.startswith("-S") else flag.partition("=")[2]
                    words.extendleft(reversed(_drop_redirect_pairs(_split(value))))
                    continue
                if pkgmanagers._takes_value(flag, value_flags) and words:
                    words.popleft()
            for _ in range(min(operands, len(words))):
                words.popleft()
            if name == "flock" and words and words[0][0] in pkgmanagers._C_FLAGS:
                words.popleft()
                script = words.popleft()[0] if words else ""
                words.clear()
                words.extend([("sh", "sh"), ("-c", "-c"), (script, script)])
            elif shell and words:
                script = " ".join(word for word, _ in words)
                words.clear()
                words.extend([("sh", "sh"), ("-c", "-c"), (script, script)])
        else:
            break
    out, offset = [], 0
    for word, _ in words:
        if offset >= _WORDS_LIMIT:
            break
        out.append(word)
        offset += len(word) + 1
    # Слова за пределом раскрытия скобок и слова-не-флаги, начатые за _WORDS_LIMIT: разбор их не видит.
    cut = _WHY_BRACES if braced else _WHY_CUT if any(
        not w.startswith("-") for w, _ in islice(words, len(out), None)) else None
    wrapped = bool(words) and pkgmanagers._basename(words[0][0]) in _WRAPPERS
    return out, marker, xargs, cut, wrapped


def _is_add(words, depth=0, mode=None):
    """Команда добавляет пакет: в проект или глобально (`npm i -g`, `cargo install`, `pipx install`).

    Разовый запуск без установки (`npx`, `bunx`, `uvx`, `pipx run`, `go run`) — не добавление, кроме запуска
    менеджера (`npx pnpm add x`, `corepack pnpm add x`); пробный прогон (_dry_run, у apt — _apt_parse, у brew —
    _brew_dry_run) — тоже не добавление. mode — режим сомнения (_LOOSE, _MASKED, _DEEP, _COMPUTED) или None; он
    переходит во вложенные команды.
    """
    if not words:
        return False
    if depth > _MAX_DEPTH:
        return mode == _DEEP and _flat_add(words)
    if mode == _COMPUTED and depth and pkgmanagers._expanded_install(words):
        return True
    w = [pkgmanagers._basename(words[0]), *words[1:]]
    name = w[0]
    if name == "corepack" or name in pkgmanagers._NPX:
        # `corepack <менеджер>[@версия] …`, `npx [флаги] <пакет>[@версия] …`: исполняется программа пакета.
        rest = w[1:] if name == "corepack" else pkgmanagers._after_flags(w[1:], _NPX_VALUE_FLAGS)
        return bool(rest) and _is_add([_VERSION.sub("", rest[0]), *rest[1:]], depth + 1, mode)
    if pkgmanagers._PYTHON.match(name):
        module = pkgmanagers._python_module(w)
        return module is not None and _is_add(module, depth + 1, mode)
    if mode == _LOOSE and pkgmanagers._has_subcommand(name) and any(_is_add(v, depth) for v in pkgmanagers._loosened(w)):
        return True
    if name in pkgmanagers._OTHER_MANAGERS:
        sub, args = pkgmanagers._sub_args(w)
        if mode == _COMPUTED and pkgmanagers._has_subcommand(name) and pkgmanagers._expanded(sub) and not pkgmanagers._asks_help(name, args):
            return True
        if name in pkgmanagers._RUNNERS and sub == "run":
            return _is_add(pkgmanagers._after_flags(args, pkgmanagers._RUNNERS[name]), depth + 1, mode)
        return not pkgmanagers._dry_run(w, mode == _MASKED) and pkgmanagers._OTHER_MANAGERS[name](w)
    dry = pkgmanagers._dry_run(w, mode == _MASKED)
    # `cargo +nightly install …`: выбор toolchain rustup перед подкомандой.
    if name == "cargo" and w[1:2] and w[1].startswith("+"):
        w = [w[0], *w[2:]]
    if name == "npm":
        w = pkgmanagers._npm_subcommand(w)
    elif name in pkgmanagers._GLOBAL_FLAGS:
        w = pkgmanagers._subcommand(w, pkgmanagers._GLOBAL_FLAGS[name])
    elif pkgmanagers._PIP.match(name):
        w = pkgmanagers._subcommand(w, pkgmanagers._PIP_GLOBAL_VALUE_FLAGS)
    if name == "yarn" and len(w) > 2 and w[1] == "workspace":
        w = [w[0], *w[3:]]
    if name in ("poetry", "composer", "yarn") and len(w) > 1 and w[1] in ("self", "global"):
        w = [w[0], *w[2:]]
    sub = w[1] if len(w) > 1 else None
    args = w[2:]
    if sub is None:
        return False
    if (mode == _COMPUTED and pkgmanagers._expanded(sub) and (pkgmanagers._has_subcommand(name) or name in pkgmanagers._CONDAS or name == "gem")
            and not pkgmanagers._asks_help(name, args)):
        return True
    if name in pkgmanagers._RUNNERS and sub == "run":
        rest = pkgmanagers._after_flags(args, pkgmanagers._RUNNERS[name])
        # `hatch run env:cmd` — команда cmd в окружении env.
        if name == "hatch" and rest:
            rest = [rest[0].partition(":")[2] or rest[0], *rest[1:]]
        return _is_add(rest, depth + 1, mode)
    if sub in _EXECS.get(name, ()):
        rest = pkgmanagers._after_flags(args, _NPX_VALUE_FLAGS | pkgmanagers._GLOBAL_FLAGS[name])
        return bool(rest) and _is_add([_VERSION.sub("", rest[0]), *rest[1:]], depth + 1, mode)
    if dry:
        return False
    if name == "go":
        if sub == "get":
            return pkgmanagers._has(args, pkgmanagers._GO_VALUE_FLAGS)
        if sub == "install":
            # `go install` без версии собирает пакет из go.mod проекта.
            return any("@" in p for p in pkgmanagers._positionals(args, pkgmanagers._GO_BUILD_VALUE_FLAGS))
        return False
    if name == "npm":
        return sub in _NPM_INSTALL and pkgmanagers._has(args, pkgmanagers._NPM_VALUE_FLAGS)
    if name == "pnpm":
        # `--workspace` берёт пакеты только из workspace проекта.
        return sub in ("add", "install", "i") and "--workspace" not in args and pkgmanagers._has(args, pkgmanagers._PNPM_VALUE_FLAGS)
    if name == "yarn":
        return sub == "add" and pkgmanagers._has(args, pkgmanagers._YARN_VALUE_FLAGS)
    if name == "bun":
        return sub in ("add", "a", "install", "i") and pkgmanagers._has(args, pkgmanagers._BUN_VALUE_FLAGS)
    if name == "deno":
        # `--entrypoint` кэширует зависимости локальных файлов.
        if sub not in ("add", "install", "i") or pkgmanagers._flag_value(args, "--entrypoint") is not None or "-e" in args:
            return False
        return pkgmanagers._has(args, pkgmanagers._DENO_VALUE_FLAGS)
    if pkgmanagers._PIP.match(name):
        return sub == "install" and pkgmanagers._pip_add(args)
    if name == "uv":
        if sub == "add":
            return pkgmanagers._has(args, pkgmanagers._UV_ADD_VALUE_FLAGS)
        if sub == "pip":
            args = pkgmanagers._subcommand(["pip", *args], pkgmanagers._UV_PIP_VALUE_FLAGS)[1:]
            return args[:1] == ["install"] and pkgmanagers._pip_add(args[1:], pkgmanagers._UV_PIP_VALUE_FLAGS)
        if sub == "tool" and args[:1] == ["install"]:
            return pkgmanagers._pip_add(args[1:], pkgmanagers._UV_TOOL_VALUE_FLAGS) or pkgmanagers._with_packages(args[1:])
        return False
    if name == "pipx":
        if sub == "install":
            return pkgmanagers._pip_add(args, pkgmanagers._PIPX_VALUE_FLAGS)
        if sub == "inject":
            # Первое позиционное — имя окружения pipx.
            return len(list(pkgmanagers._positionals(pkgmanagers._join_at(args), pkgmanagers._PIPX_VALUE_FLAGS, pip=True))) > 1
        if sub == "runpip":
            # `pipx runpip <окружение> <аргументы pip>` — pip внутри окружения.
            rest = pkgmanagers._after_flags(args, pkgmanagers._PIPX_VALUE_FLAGS)
            return bool(rest) and _is_add(["pip", *rest[1:]], depth + 1, mode)
        return False
    if name == "pipenv":
        return sub == "install" and pkgmanagers._pip_add(args, pkgmanagers._PIPENV_VALUE_FLAGS)
    if name in pkgmanagers._CONDAS:
        if sub == "create":
            # Спецификация `канал::имя=версия`; окружение с одним интерпретатором пакетов не вносит.
            return any(re.split(r"[=<>!~\[ ]", p, maxsplit=1)[0].rpartition("::")[2].lower() not in _CONDA_BASE
                       for p in pkgmanagers._positionals(args, pkgmanagers._CONDA_VALUE_FLAGS))
        return sub == "install" and pkgmanagers._has(args, pkgmanagers._CONDA_VALUE_FLAGS)
    if name in ("cargo", "bundle") and pkgmanagers._flag_value(args, "--path") is not None:
        # `--path` — локальный пакет под названным именем.
        return False
    if name == "cargo":
        if sub == "install":
            return pkgmanagers._has(args, pkgmanagers._CARGO_INSTALL_VALUE_FLAGS) or pkgmanagers._git_source(args)
        return sub == "add" and (pkgmanagers._has(args, pkgmanagers._CARGO_VALUE_FLAGS) or pkgmanagers._git_source(args))
    if name in _ADD_FLAGS:
        return sub == "add" and pkgmanagers._has(args, _ADD_FLAGS[name])
    if name == "gem":
        return sub in _GEM_INSTALL and pkgmanagers._has(args, pkgmanagers._GEM_VALUE_FLAGS)
    if name == "composer":
        return sub in _COMPOSER_REQUIRE and pkgmanagers._has(args, pkgmanagers._COMPOSER_VALUE_FLAGS)
    if name == "dotnet":
        if sub == "add":
            # `dotnet add [<проект>] package <пакет>`; `add reference` — ссылка на проект.
            if args[1:2] == ["package"] and args[:1] != ["package"]:
                args = args[1:]
            return args[:1] == ["package"] and pkgmanagers._has(args[1:], pkgmanagers._DOTNET_ADD_VALUE_FLAGS)
        if sub == "package":
            return args[:1] == ["add"] and pkgmanagers._has(args[1:], pkgmanagers._DOTNET_ADD_VALUE_FLAGS)
        if sub == "tool":
            return args[:1] == ["install"] and pkgmanagers._has(args[1:], pkgmanagers._DOTNET_TOOL_VALUE_FLAGS)
        return False
    if name in ("dart", "flutter"):
        if sub != "pub":
            return False
        if args[:1] == ["add"]:
            return pkgmanagers._has(args[1:], pkgmanagers._PUB_VALUE_FLAGS)
        return args[:2] == ["global", "activate"] and pkgmanagers._has(args[2:], pkgmanagers._PUB_VALUE_FLAGS)
    if name == "swift" and sub == "package":
        w = pkgmanagers._subcommand([name, *args], pkgmanagers._SWIFT_PACKAGE_VALUE_FLAGS)
        return w[1:2] == ["add-dependency"] and pkgmanagers._has(w[2:], pkgmanagers._SWIFT_ADD_VALUE_FLAGS)
    return False

# Менеджеры, чьё имя с глаголом установки в словах неизвестной программы — сомнение (_launches_install); у R
# установка — выражением `-e`, не глаголом.
_PAIR_MANAGERS = (set(pkgmanagers._GLOBAL_FLAGS) | set(pkgmanagers._OTHER_MANAGERS) | set(pkgmanagers._RUNNERS) | pkgmanagers._CONDAS | {"gem"}) - {"r", "rscript"}
# Программы, разбор которых детектор знает: менеджеры, запускатели, обёртки, оболочки, `eval`, `source`, команды
# _launched и программы-данные; у прочих _launches_install ищет менеджер с глаголом установки в словах.
_KNOWN_PROGRAMS = (_PAIR_MANAGERS | {"r", "rscript", "corepack", "eval", "find", "trap", "cmd", "pwsh", "powershell",
                                     "nix-shell", "mise", "rtx"}
                   | pkgmanagers._NPX | set(_WRAPPERS) | pkgmanagers._SHELLS | pkgmanagers._C_SHELLS | pkgmanagers._SOURCES | _DATA_PROGRAMS)


def _funsub_open(text, i, end=None):
    """Длина начала подстановки функции bash 5.3 в позиции i: `${` с пробелом, табом или переводом строки за ним
    (`${ …; }`; `${` в конце строки text[:end] — тоже) — 2, `${|` — 3; 0 — её нет."""
    if not text.startswith("${", i):
        return 0
    n = len(text) if end is None else end
    if i + 2 >= n:
        return 2
    return 3 if text[i + 2] == "|" else 2 if text[i + 2] in " \t\n" else 0


def _close_paren(text, i, arith=False, end=None, funsub=False):
    """Позиция последней `)` подстановки `$(…)` (при arith — арифметики `$((…))`, при funsub — `}` подстановки
    функции `${ …; }`), тело которой начинается с i, в text[:end]; end (по умолчанию len(text)), если она там не
    закрыта. Кавычки, вложенные подстановки и тела heredoc внутри учтены."""
    stack = ["F" if funsub else "A" if arith else "("]
    pending = deque()
    n = len(text) if end is None else end
    while i < n:
        eol = text.find("\n", i, n)
        eol = n if eol < 0 else eol
        if pending:
            line, pieces, eol = _body_line(text, i, eol, n, pending[0][2])
            closes = _closes(line)
            pos = 0
            while pending and pos < len(line):
                stop = _heredoc_end(line, pos, pending[0], closes)
                if stop is None:
                    break
                pending.popleft()
                pos = stop
            if not pending and pos < len(line):
                # Остаток строки за терминатором bash читает заново, с начала команды.
                i = _at(pieces, pos)
                if stack[-1] in ("f", "N"):
                    stack[-1] = "F"
                continue
        else:
            found, stop = _scan(text, stack, i, eol)
            if stack[-1:] == ["!"]:
                # Синтаксическая ошибка скобок массива: подстановка не закрыта, следующие строки bash читает заново
                # командами, их разбирает разбор тела.
                return n
            if stop is not None:
                return stop - 1
            pending.extend(found)
        i = eol + 1
    return n


def _eof_token(stack):
    """Знак конца heredoc, открытого при стеке _scan stack (bash 5.3, shell_eof_token): `)` у самой внутренней
    подстановки `$(…)`, `<(…)`, `>(…)`, `}` у `${ …; }` (группа в нём — тоже его состояние), у подоболочки — знак
    вокруг неё; "" — вне подстановки. Верх стека при `<<` и `(` — подстановка, подоболочка или пусто."""
    top = stack[-1] if stack else ""
    return ")" if top == "(" else "}" if top in _FUNSUB_STATES else top[1:] if top[:1] == "s" else ""


class _Body(str):
    """Тело подстановки (_quoted_substitutions) со знаком конца heredoc в нём (_eof_token): bash разбирает тело внутри
    подстановки, разбор тела отдельной командой (_parse) начинает с этого знака."""

    eof: str

    def __new__(cls, text, eof):
        body = super().__new__(cls, text)
        body.eof = eof
        return body


def _closes(line):
    """Позиции последних `)` и `}` в строке тела по знаку конца heredoc (_heredoc_end); у "" — -1."""
    return {")": line.rfind(")"), "}": line.rfind("}"), "": -1}


def _heredoc_end(line, pos, heredoc, closes):
    """Конец терминатора heredoc в строке тела line[pos:] (_body_line; heredoc — элемент _scan: терминатор, снимать
    ли ведущие табы, кавычки, знак самой внутренней подстановки вокруг); None — строка не кончает тело. Строка —
    терминатор целиком (конец — len(line)) или, у heredoc внутри подстановки, терминатор в начале строки, а дальше в
    ней есть её знак (closes — _closes(line)): bash 5.3 (make_cmd.c, make_here_document: strchr (line + redir_len,
    shell_eof_token)) кончает тело, остаток строки за терминатором читает заново командами (`$(cat <<E` ⏎ `x` ⏎
    `E); npm i x` — `npm i x` исполняется). Знак — `)` у `$(…)`, `<(…)`, `>(…)`, `}` у `${ …; }`; знак другой
    подстановки тело не кончает (`$(cat <<E` ⏎ `E}` — строка тела)."""
    term, strip_tabs, _, eof = heredoc
    if strip_tabs:
        while pos < len(line) and line[pos] == "\t":
            pos += 1
    if not line.startswith(term, pos):
        return None
    stop = pos + len(term)
    if stop == len(line) or closes[eof] >= stop:
        return stop
    return None


def _joins(text, start, end):
    """Физическая строка text[start:end] тела heredoc с терминатором без кавычек склеивается со следующей: кончается
    нечётным числом `\\` (bash 5.3, read_secondary_line: `\\` перед `\\` — пара, перевод строки за ней не
    снимается)."""
    k = end
    while k > start and text[k - 1] == "\\":
        k -= 1
    return (end - k) % 2 == 1


def _body_line(text, i, eol, n, quoted):
    """Строка тела heredoc с позиции i в text[:n] (eol — конец её физической строки): у терминатора без кавычек
    (quoted ложно) физические строки, кончающиеся нечётным числом `\\`, склеены со следующими без `\\` и перевода
    строки — так bash 5.3 читает тело и сравнивает строку с терминатором (`E\\` ⏎ `)` — строка `E)`, ведущие табы
    `<<-` снимаются только в её начале). Возвращает (строка, куски — пары (начало куска в строке, его позиция в
    text), конец последней физической строки)."""
    pieces = [(0, i)]
    if quoted or eol >= n or not _joins(text, i, eol):
        return text[i:eol], pieces, eol
    parts, length = [], 0
    while eol < n and _joins(text, i, eol):
        parts.append(text[i:eol - 1])
        length += eol - 1 - i
        i = eol + 1
        eol = text.find("\n", i, n)
        eol = n if eol < 0 else eol
        pieces.append((length, i))
    parts.append(text[i:eol])
    return "".join(parts), pieces, eol


def _at(pieces, pos):
    """Позиция в тексте места pos строки тела (_body_line, её куски pieces); место на стыке кусков — начало
    следующего: снятые `\\` и перевод строки пропущены."""
    start, at = pieces[bisect.bisect_right(pieces, pos, key=lambda piece: piece[0]) - 1]
    return at + pos - start


def _close_backtick(text, i, end=None):
    """Позиция обратной кавычки, закрывающей подстановку `` `…` ``, тело которой начинается с i: первая
    неэкранированная в text[:end]; end (по умолчанию len(text)), если её там нет."""
    n = len(text) if end is None else end
    while i < n and text[i] != "`":
        i += 2 if text[i] == "\\" else 1
    return min(i, n)


_BACKTICK_ESCAPE = re.compile(r"\\([\\`$])")
_BACKTICK_ESCAPE_DQ = re.compile(r'\\([\\`$"])')


def _backtick_body(body, dq=False):
    """Команда в теле `` `…` ``: bash снимает `\\` перед `\\`, `` ` `` и `$` (в двойных кавычках — и перед `"`) до
    разбора тела (5.3: `` echo `echo \\`npm i x\\`` `` исполняет `npm i x`)."""
    return (_BACKTICK_ESCAPE_DQ if dq else _BACKTICK_ESCAPE).sub(r"\1", body)


# Начало имени параметра в `${…}` (`#`, `!` перед ним — длина и косвенное раскрытие) и слова с индексом массива
# (`a[`, `[` — элемент в `(…)` присваивания массива).
_PARAM_NAME = re.compile(r"[#!]?(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])")
_SUBSCRIPT_WORD = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*)?\[")
# Знаки после `:` в `${x:…}`, с которыми это не смещение, а значение по умолчанию, присваивание, ошибка, замена.
_PARAM_OPS = ("-", "=", "?", "+")


def _quoted_substitutions(text, heredoc=False, arith=False):
    """Тела подстановок `$(…)` и `` `…` `` вне одинарных кавычек — внутри двойных кавычек, внутри `${…}` и `$[…]` вне
    кавычек и внутри арифметики `$((…))` в них, и вне кавычек: _parse оставляет `` `…` `` в слове, — тела подстановок
    функции `${ …; }`, `${| …; }` везде вне одинарных кавычек (_parse оставляет их в слове; конец — _close_paren с
    funsub) и тела процесс-подстановок `<(…)`, `>(…)` внутри `${…}` вне кавычек; прочие вне кавычек уже разделил
    _segments. `${…}` кончается на первой `}` вне кавычек, `$[…]` — на `]`, как у _scan: голые `{…}` в значении bash не
    вкладывает. `$((…))` кончается на `))` вне своих скобок и кавычек. Кавычки в арифметике — свой уровень для поиска
    конца, как в bash 5.3: `'…'` прячет скобки, но не подстановки (bash раскрывает выражение, как текст в `"…"`; конец
    такой подстановки — не дальше кавычки), `"…"` — как двойные кавычки. Арифметика — `$((…))`, `$[…]`, индекс `${a[…]}`
    и смещение `${x:…}` (`:` не перед `-`, `=`, `?`, `+`) вне кавычек, индекс слова-присваивания `a[…]=`, `a[…]+=` и
    элемента `[…]=` вне кавычек (в любом месте команды: так же раскрывают `declare`, `typeset`, `local`; индекс может
    содержать пробелы); слово с индексом без `=` за ним — не присваивание: отбрасываются тела из `'…'` прямо в его
    индексе (вне присваивания это литерал), а `"$(…)"`, `` `…` ``, `${…}` в слове — подстановки (`[ -n "$(…)" ]`).
    `$$` — параметр целиком: `{`, `'`, `(` за ним ничего не открывают. `)` вне скобок `$((…))` не перед второй `)` —
    `$((…) …)`: bash читает её подстановкой `$(` с подоболочкой, её тело берётся целиком. При heredoc text — тело
    heredoc без кавычек: весь текст как внутри `"…"`, кавычки вне арифметики — обычные символы. При arith text —
    выражение арифметики `((…))` или `$((…))` вне кавычек без скобок по краям (_parse)."""
    found = []
    in_dq = heredoc
    # Открытые в живом контексте: `}` и `]` — `${…}` и `$[…]` вне кавычек; список [глубина скобок, позиция начала,
    # сколько тел найдено до неё, вид, присваивание ли] — арифметика: вид "))" — `$((…))` (позиция `$`), "]" — индекс
    # (кончается на `]` вне своих `[…]`; присваивание — "=", индекс `${a[…]}` — None), "}" — смещение `${x:…}`
    # (кончается перед `}`); `"` — двойная кавычка в арифметике; ("'", позиция закрывающей кавычки) — одинарная
    # кавычка в арифметике. Внутри них подстановка — команда. У индекса слова шестой элемент — номера тел из `'…'`
    # прямо в нём: если слово не присваивание, отбрасываются только они (_drop_literal).
    closers = [[0, -2, 0, "))", None]] if arith else []
    # Позиция начала очередного слова вне кавычек и подстановок: за пробелом вне них или в начале текста.
    boundary = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        top = closers[-1] if closers else None
        if isinstance(top, tuple):
            stop = top[1]
            if i >= stop:
                closers.pop()
                i = stop + 1
            elif c == "\\" or text.startswith("$$", i):
                i += 2
            elif text.startswith("$(", i) or _funsub_open(text, i, stop) or c == "`":
                if len(closers) > 1 and isinstance(closers[-2], list) and closers[-2][4]:
                    # Кавычка прямо в индексе слова: вне присваивания её тело — литерал.
                    closers[-2][5].append(len(found))
                funsub = _funsub_open(text, i, stop)
                if c == "`":
                    end = _close_backtick(text, i + 1, stop)
                    found.append(_backtick_body(text[i + 1:end]))
                else:
                    start = i + (funsub or 2)
                    end = _close_paren(text, start, end=stop, funsub=bool(funsub))
                    found.append(_Body(text[start:end], "}" if funsub else ")"))
                i = end + 1
            else:
                i += 1
            continue
        dq = in_dq or top == '"'
        live = dq or bool(closers)
        kind = top[3] if isinstance(top, list) else None
        if c == "\\" or text.startswith("$$", i):
            # `$$` — параметр целиком: `{`, `'`, `(` за ним не начинают `${…}`, `$'…'`, `$(…)`.
            i += 2
        elif (kind or top == "]") and c == "'":
            stop = text.find("'", i + 1)
            closers.append(("'", n if stop < 0 else stop))
            i += 1
        elif (kind or top == "]") and c == '"':
            closers.append('"')
            i += 1
        elif top == '"' and c == '"':
            closers.pop()
            i += 1
        elif heredoc and c in "'\"":
            i += 1
        elif kind == "))" and c in "()":
            depth = top
            if c == "(":
                depth[0] += 1
            elif depth[0]:
                depth[0] -= 1
            elif text.startswith("))", i):
                closers.pop()
                i += 1
            else:
                # `)` нулевой глубины не перед второй `)`: это `$(` с подоболочкой, она кончилась здесь. Тело `$(`
                # — команда целиком; подстановки, найденные в нём арифметикой, она и найдёт.
                closers.pop()
                del found[depth[2]:]
                end = _close_paren(text, i + 1)
                found.append(_Body(text[depth[1] + 2:end], ")"))
                i = end
            i += 1
        elif kind == "]" and c in "[]":
            if c == "[":
                top[0] += 1
            elif top[0]:
                top[0] -= 1
            else:
                closers.pop()
                if top[4]:
                    if not text.startswith(("=", "+="), i + 1):
                        _drop_literal(found, top)
                elif text.startswith(":", i + 1) and text[i + 2:i + 3] not in _PARAM_OPS:
                    closers.append([0, i + 1, len(found), "}", None])
                    i += 1
            i += 1
        elif kind == "}" and c == "}":
            # Смещение кончилось; `}` закроет `${…}`.
            closers.pop()
        elif not dq and not kind and text.startswith("$'", i):
            i += 2
            while i < n and text[i] != "'":
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif not dq and c == "'":
            end = text.find("'", i + 1)
            i = n if end < 0 else end + 1
        elif c == '"':
            in_dq = not in_dq
            i += 1
        elif _funsub_open(text, i):
            start = i + _funsub_open(text, i)
            end = _close_paren(text, start, funsub=True)
            found.append(_Body(text[start:end], "}"))
            i = end + 1
        elif not dq and text.startswith(("${", "$["), i):
            closers.append("}" if text[i + 1] == "{" else "]")
            i += 2
            name = _PARAM_NAME.match(text, i) if text[i - 1] == "{" else None
            if name and text.startswith("[", name.end()):
                closers.append([0, name.end(), len(found), "]", None])
                i = name.end() + 1
            elif name and text.startswith(":", name.end()) and text[name.end() + 1:name.end() + 2] not in _PARAM_OPS:
                closers.append([0, name.end(), len(found), "}", None])
                i = name.end() + 1
        elif not dq and c == top:
            closers.pop()
            i += 1
        elif live and text.startswith("$((", i):
            closers.append([0, i, len(found), "))", None])
            i += 3
        elif live and text.startswith("$(", i) or not dq and top == "}" and text.startswith(("<(", ">("), i):
            end = _close_paren(text, i + 2)
            found.append(_Body(text[i + 2:end], ")"))
            i = end + 1
        elif c == "`":
            end = _close_backtick(text, i + 1)
            found.append(_backtick_body(text[i + 1:end], top == '"' or in_dq and not heredoc))
            i = end + 1
        elif i == boundary and not closers and not in_dq and _SUBSCRIPT_WORD.match(text, i):
            closers.append([0, i, len(found), "]", "=", []])
            i = _SUBSCRIPT_WORD.match(text, i).end()
        else:
            if c in " \t\n" and not closers and not in_dq:
                boundary = i + 1
            i += 1
    if closers and isinstance(closers[0], list) and closers[0][4]:
        # Индекс слова не закрыт до конца текста: слово — не присваивание.
        _drop_literal(found, closers[0])
    return found


def _drop_literal(found, subscript):
    """Убирает из found тела из `'…'` прямо в индексе слова subscript (элемент closers _quoted_substitutions): слово
    — не присваивание, `'…'` в нём — литерал. Тела `"…"`, `` `…` ``, `$(…)` и `${…}` в слове — подстановки, они
    остаются. Перебирает только тела, найденные с начала слова."""
    drop = set(subscript[5])
    if drop:
        start = subscript[2]
        found[start:] = [body for k, body in enumerate(found[start:], start) if k not in drop]


_WORD_END = " \t\n;&|<>()"


def _heredoc_word(line, i):
    """Терминатор heredoc, начинающийся с позиции i, после снятия кавычек, позиция за ним и были ли в нём кавычки
    или `\\` (тогда тело — данные без подстановок)."""
    word = []
    quoted = False
    while i < len(line) and line[i] not in _WORD_END:
        c = line[i]
        quoted = quoted or c in "'\"\\"
        if c in "'\"":
            end = line.find(c, i + 1)
            end = len(line) if end < 0 else end
            word.append(line[i + 1:end])
            i = end + 1
        elif c == "\\":
            word.append(line[i + 1:i + 2])
            i += 2
        else:
            word.append(c)
            i += 1
    return "".join(word), i, quoted


def _comment_start(text, i):
    """`#` в позиции i начинает комментарий: она в начале text или за пробелом, табом или переводом строки без `\\`
    перед ним (`echo \\ #$(…)` — слово ` #…`, подстановка исполняется); `\\r`, `\\v`, неразрывный пробел для bash —
    часть слова (`echo a\\r#$(…)` — подстановка исполняется)."""
    if i == 0:
        return True
    if text[i - 1] not in " \t\n":
        return False
    k = i - 1
    while k and text[k - 1] == "\\":
        k -= 1
    return (i - 1 - k) % 2 == 0


# Состояние тела подстановки функции `${ …; }` и группы `{ …; }` в нём на стеке _scan: "F" — bash примет здесь
# зарезервированное слово (parse.y, reserved_word_acceptable: после `;`, `&`, `|`, перевода строки, `(`, `)`
# подоболочки или шаблона `case`, `{`, `}`, зарезервированного слова, `]]`, `))`), "f" — не примет (за обычным
# словом), "N" — примет и после следующего обычного слова (имя за `function`, `coproc`), "C" — внутри `[[ … ]]`.
# `\` в конце строки дописывает к состоянию "\\": перевод строки за ним снят, он не разделитель.
_FUNSUB_STATES = ("F", "f", "N", "C")
_RESERVED_BEFORE = {"!", "{", "}", "do", "done", "elif", "else", "esac", "fi", "if", "then", "time", "until", "while"}
# Обычные символы слова: без пробела, таба, перевода строки, операторов, кавычек, `$` и `\`; `\r`, `\v`,
# неразрывный пробел для bash — часть слова (`${ echo;\xa0}` — команда `\xa0}`, не конец тела).
_FUNSUB_WORD = re.compile(r"[^ \t\n;&|()<>'\"`$\\]+")
_METACHARS = " \t\n;&|()<>"


def _funsub_step(text, stack, i, end):
    """Шаг _scan в теле `${ …; }` (верх стека — состояние из _FUNSUB_STATES), как в bash 5.3: `}` там, где
    принимается зарезервированное слово, закрывает тело или группу (и без разделителя за ней: `${ echo a; }x`), `{`
    с разделителем за ним открывает группу; слова и операторы меняют состояние. Позиция за разобранным или None:
    символ разбирает сам _scan (кавычки, подстановки, скобки, комментарий, перенаправления), а состояние уже то, что
    будет за этим словом или скобкой."""
    state, c = stack[-1], text[i]
    if c in " \t":
        return i + 1
    if state == "C":
        # Внутри `[[ … ]]` важна только `]]`; `&&`, `||`, `<`, `>`, `(` там — операторы условия.
        m = _FUNSUB_WORD.match(text, i, end)
        if m:
            if m.group() == "]]" and (m.end() >= end or text[m.end()] in _METACHARS):
                stack[-1] = "F"
            return m.end()
        return i + 1 if c in ";&|<>)" else None
    if c == "\\" and i + 1 >= end:
        stack[-1] = state + "\\"
        return i + 1
    acceptable = state in ("F", "N")
    if acceptable and c == "}":
        stack.pop()
        return i + 1
    if acceptable and c == "{" and (i + 1 >= end or text[i + 1] in _METACHARS):
        # Группа: за её `}` зарезервированное слово снова принимается.
        stack[-1] = "F"
        stack.append("F")
        return i + 1
    if c in ";&|":
        # `>&`, `<&`, `>|` — перенаправление: за ним цель, обычное слово.
        stack[-1] = "f" if i and text[i - 1] in "<>" else "F"
        return i + 1
    if c == ")":
        stack[-1] = "F"
        return i + 1
    if c == "(":
        # Подоболочка, `((…))` и `f()` — за ними слово принимается; `<(…)`, `>(…)`, `x=(…)` — часть слова.
        stack[-1] = "f" if i and text[i - 1] in "<>=" else "F"
        return None
    if c == "#" and _comment_start(text, i):
        return None
    if c in "<>'\"`$\\":
        stack[-1] = "f"
        return None
    m = _FUNSUB_WORD.match(text, i, end)
    if not m:
        return i + 1
    after = m.end()
    if acceptable and (after >= end or text[after] in _METACHARS):
        w = m.group()
        stack[-1] = ("N" if w in ("function", "coproc") else "C" if w == "[[" else
                     "F" if w in _RESERVED_BEFORE or state == "N" else "f")
    else:
        stack[-1] = "f"
    return after


def _scan(text, stack, i=0, end=None):
    """Проход text[i:end] (одна строка) со стеком открытых кавычек и скобок stack; стек меняется на месте.

    Символы стека: "'", '"', "$'", "(" — подстановка `$(…)`, `<(…)`, `>(…)`, "s" со знаком конца heredoc внешней
    подстановки (_eof_token) — подоболочка, "`" — подстановка `` `…` `` (до неэкранированной обратной кавычки,
    кавычки, скобки и `#` внутри не считаются), "A" — арифметика `$((…))`, "D" — `((…))` (`)` их нулевой глубины не
    перед второй `)` делает "A" подстановкой "(", "D" — подоболочкой "s": внутри — подоболочка), "a" — скобка в них, "{" — `${…}`, "[" — арифметика `$[…]` (внутри них `<<` не heredoc, кавычки и
    подстановки вложены), "=" — скобки присваивания массива `x=(…)` (`;`, `&`, `|`, перенаправление, `<<` и `(` не
    за `<`, `>` в них — синтаксическая ошибка bash: на стек кладётся "!", проход кончается; строку с ней bash
    отбрасывает вместе с телами её heredoc и читает следующую заново), состояние из _FUNSUB_STATES — тело
    подстановки функции `${ …; }`, `${| …; }` или группа `{ …; }` в нём (_funsub_step; кавычки, подстановки, heredoc и
    комментарии в нём — как в `$(…)`). `$$` — параметр целиком: `{`, `'`, `(` за ним ничего не открывают.
    Возвращает heredoc, открытые вне кавычек и арифметики, — (терминатор, снимать ли ведущие табы, был ли
    терминатор в кавычках, знак самой внутренней подстановки вокруг: _eof_token, _heredoc_end), — и позицию за символом,
    после которого непустой на входе стек опустел (при ошибке скобок массива — позицию ошибки); None — не опустел до
    конца строки или комментария. С пустым стеком на входе проход идёт до конца строки.
    """
    found = []
    nested = bool(stack)
    end = len(text) if end is None else end
    if stack and stack[-1][:-1] in _FUNSUB_STATES and stack[-1].endswith("\\"):
        stack[-1] = stack[-1][:-1]
    elif stack and stack[-1] in ("f", "N") and (i == 0 or text[i - 1] == "\n"):
        # Перевод строки в теле `${ …; }` — разделитель команд.
        stack[-1] = "F"
    while i < end:
        top = stack[-1] if stack else None
        c = text[i]
        if top in _FUNSUB_STATES:
            step = _funsub_step(text, stack, i, end)
            if step is not None:
                i = step
                if nested and not stack:
                    return found, i
                continue
            top = stack[-1]
        if top == "'":
            if c == "'":
                stack.pop()
            i += 1
        elif top == "$'":
            if c == "'":
                stack.pop()
            i += 2 if c == "\\" else 1
        elif c == "\\":
            i += 2
        elif top == "`":
            if c == "`":
                stack.pop()
            i += 1
        elif top == '"':
            if c == '"':
                stack.pop()
                i += 1
            elif c == "`":
                stack.append("`")
                i += 1
            elif text.startswith("$$", i):
                i += 2
            elif text.startswith("$((", i):
                stack.append("A")
                i += 3
            elif text.startswith("$(", i):
                stack.append("(")
                i += 2
            elif _funsub_open(text, i, end):
                i += _funsub_open(text, i, end)
                stack.append("F")
            else:
                i += 1
        elif text.startswith("$$", i):
            # `$$` — параметр целиком: `{`, `'`, `(` за ним не начинают `${…}`, `$'…'`, `$(…)`.
            i += 2
        elif _funsub_open(text, i, end):
            i += _funsub_open(text, i, end)
            stack.append("F")
        elif text.startswith("$'", i):
            stack.append("$'")
            i += 2
        elif c in "'\"`":
            stack.append(c)
            i += 1
        elif top in ("{", "[") and c == ("}" if top == "{" else "]"):
            stack.pop()
            i += 1
        elif text.startswith(("${", "$["), i) and top not in ("A", "D", "a"):
            stack.append(text[i + 1])
            i += 2
        elif top in ("{", "[") and not text.startswith("$(", i):
            i += 1
        elif top in ("A", "D", "a"):
            if top != "a" and text.startswith("))", i):
                stack.pop()
                i += 2
            elif top != "a" and c == ")":
                # `)` нулевой глубины не перед второй `)`: bash читает `$((…) …)` подстановкой с подоболочкой,
                # `((…) …)` — подоболочкой с подоболочкой; подоболочка кончилась, дальше — тело `$(` или `(`.
                stack.pop()
                stack.append("(" if top == "A" else "s" + _eof_token(stack))
                i += 1
            else:
                if c == "(":
                    stack.append("a")
                elif c == ")" and top == "a":
                    stack.pop()
                i += 1
        elif top == "=" and (c in ";&|" or c in "<>" and not text.startswith("(", i + 1)
                             or c == "(" and not (i and text[i - 1] in "<>")):
            # Оператор, перенаправление или `(` не процесс-подстановки в скобках присваивания массива — синтаксическая
            # ошибка bash 5.3 (parse_compound_assignment): строка отбрасывается вместе с телами своих heredoc, следующая
            # читается заново, с пустым стеком.
            stack.append("!")
            return found, i
        elif c == "(" and top != "=" and i and text[i - 1] == "=":
            # Скобки присваивания массива: `x=(…)`, `x+=(…)`, `a[1]=(…)`.
            stack.append("=")
            i += 1
        elif text.startswith("$((", i) or text.startswith("((", i):
            stack.append("A" if c == "$" else "D")
            i += 3 if c == "$" else 2
        elif text.startswith("$(", i) or c == "(":
            # `$(…)`, `<(…)`, `>(…)` — подстановка; иначе подоболочка, она знак конца heredoc не меняет.
            stack.append("(" if c == "$" or i and text[i - 1] in "<>" else "s" + _eof_token(stack))
            i += 2 if c == "$" else 1
        elif c == ")":
            if top in ("(", "=") or top and top[0] == "s":
                stack.pop()
            i += 1
        elif c == "#" and _comment_start(text, i):
            break
        elif text.startswith("<<<", i):
            i += 3
        elif text.startswith("<<", i):
            i += 2
            strip_tabs = text.startswith("-", i)
            i += strip_tabs
            while i < end and text[i] in " \t":
                i += 1
            word, i, quoted = _heredoc_word(text, i)
            if word:
                found.append((word, strip_tabs, quoted, _eof_token(stack)))
        else:
            i += 1
        if nested and not stack:
            return found, i
    return found, None


def heredocs(line):
    """Heredoc, открытые строкой, по порядку: (терминатор, снимать ли ведущие табы).

    `<<` в кавычках (в том числе `$'…'` с `\\'`) и в `` `…` ``, закрытой на строке, here-string `<<<`, сдвиг в
    арифметике `((…))` и комментарий не считаются; подстановка `$(…)` внутри двойных кавычек — снова команда.
    `` `…` ``, не закрытая до конца строки, — команда с телом heredoc на следующих строках: bash читает подстановку
    до закрывающей обратной кавычки и разбирает её заново; тела её heredoc идут раньше тел остальной строки.
    Строка с синтаксической ошибкой скобок присваивания массива (`x=(<<E`, `cat <<E; x=( a ; )`) heredoc не
    открывает: bash отбрасывает её целиком (_scan).
    """
    stack = []
    found = _scan(line, stack)[0]
    if stack[-1:] == ["!"]:
        return []
    if stack and stack[-1] == "`":
        # Открывает её последняя неэкранированная обратная кавычка: за ней неэкранированных нет.
        k = len(line)
        while k > 0:
            k = line.rfind("`", 0, k)
            j = k
            while j > 0 and line[j - 1] == "\\":
                j -= 1
            if (k - j) % 2 == 0:
                break
        # Тела её heredoc bash читает раньше тел строки: подстановку он дочитывает до конца вместе с телами.
        found = _scan(line[k + 1:], [])[0] + found
    return [(term, strip_tabs) for term, strip_tabs, *_ in found]


def _line_heredocs(text, arrays):
    """Heredoc куска строки вне кавычек; None — в нём синтаксическая ошибка скобок присваивания массива: bash
    отбрасывает строку вместе с телами её heredoc. arrays — скобки массива, открытые на прошлых строках (символы "="
    стека _scan; `x=(` ⏎ `a ;` — ошибка): меняется на месте, за концом куска остаются только они."""
    found, i = [], 0
    while True:
        more, stop = _scan(text, arrays, i)
        found.extend(more)
        if arrays[-1:] == ["!"]:
            return None
        if stop is None:
            break
        # Скобки с прошлых строк закрылись: дальше — с пустым стеком до конца куска.
        i = stop
    if any(s != "=" for s in arrays):
        arrays.clear()
    return found


def _segments(command):
    """Сегменты команды (_parse)."""
    return _parse(command)[0]


def _parse(command):
    """Сегменты команды, связи «вывод сегмента — вход сегмента» и тела heredoc с подстановками.

    Границы сегментов — `&&`, `||`, `;`, `|`, `&`, `(`, `)` и перевод строки вне кавычек.
    Кавычка, открытая на одной строке, продолжается на следующих; внутри `"$(…)"` кавычки вложены, внутри
    `` `…` `` не считаются до закрывающей обратной кавычки (_scan). Подстановка `$(…)` и
    процесс-подстановка `<(…)`, `>(…)` вне кавычек — свои сегменты, а сегмент вокруг продолжается после неё
    с заглушкой `$` на месте подстановки (`npm install $(echo x)` — `npm install $`, `$(which npm) i x` —
    `$ i x`). `${…}`, `$[…]`, `` `…` `` и подстановку функции `${ …; }` вне кавычек _scan ведёт, как кавычку: `#`,
    `;`, `|`, скобки в них не делят сегмент; bash кончает `` `…` `` на первой неэкранированной обратной кавычке и
    разбирает тело заново, команды `` `…` `` и `${ …; }` находит _quoted_substitutions. `$$` — параметр целиком.
    `\\` в конце строки продолжает сегмент: bash удаляет его с переводом строки, слова по краям склеиваются
    (`n\\` ⏎ `pm i x` — `npm i x`). Тело heredoc — данные: строки после конца логической строки с
    `<<` до терминатора не входят ни в один сегмент; тело heredoc оболочки без скрипта (`bash <<EOF`,
    `ssh host <<EOF`) — команды (_heredoc_runs); тело heredoc внутри подстановки кончает и терминатор с `)` или `}`
    дальше в строке, остаток строки — команды (_heredoc_end). Тело с терминатором без кавычек bash читает как текст в
    `"…"`:
    оно возвращается для поиска подстановок (_quoted_substitutions с heredoc). Heredoc внутри кавычки, открытой
    до конца строки, тело начинает со следующей строки: тело и терминатор входят в сегмент текстом без разбора,
    их разбирает _quoted_substitutions. Комментарий — `#` в начале слова вне кавычек (в начале строки, кроме
    продолжения после `\\`, или после неэкранированного пробела, _comment_start) — до конца строки.

    Конвейер, продолженный за телом heredoc (`cat <<EOF |` ⏎ тело ⏎ `EOF` ⏎ `bash`), решает о телах по своим
    сегментам до конца конвейера: если оболочка в нём читает stdin (_heredoc_runs), тела heredoc его строк с `<<`
    — команды; они возвращаются строками для разбора, как вход оболочки.

    `((…))` и `$((…))` вне кавычек, закрытые `))` (`)` их нулевой глубины не перед второй `)` — подоболочка, как в
    bash), делятся на сегменты, как подоболочки, а их выражение ещё разбирается арифметикой: тела подстановок в нём, в
    том числе в его `'…'` (bash раскрывает выражение, как текст в `"…"`), — тоже строки команд
    (_quoted_substitutions с arith; вложенная арифметика — внутри внешней).

    Возвращает (сегменты, связи, тела, строки команд): связь — (номер сегмента-источника, номер
    сегмента-приёмника) для конвейера `a | b` и процесс-подстановки `b <(a)`.
    """
    segments, feeds, bodies, scripts = [], [], [], []
    # Конвейер, продолженный за телом heredoc: (номер его первого сегмента, строки тел heredoc) или None.
    carry = None
    # Текущий сегмент: [символы, номера сегментов, чей вывод идёт ему на вход].
    cur = [[], []]
    # Открытые кавычки и подстановки в них (символы стека _scan).
    stack = []
    # Открытые вне кавычек скобки: (вид, сегмент вокруг, знак конца heredoc в них: _eof_token). Вид "(" —
    # подоболочка, "$" — подстановка, "<" — процесс-подстановка входа, ">" — выхода. paren_at — позиции их `(` в
    # command.
    parens = []
    paren_at = []
    # Выражения арифметики `((…))` и `$((…))` вне кавычек, закрытой `))`: (начало, конец) в command, только
    # внешние.
    ariths = []
    # Позиция начала строки в command.
    base = 0
    pending = deque()
    # Строки тела heredoc с терминатором без кавычек.
    body = []
    # Heredoc логической строки: тело начинается после её конца.
    opened = []
    # Скобки присваивания массива вне кавычек, открытые на прошлых строках (_line_heredocs).
    arrays = []
    logical_start = 0
    # Перед началом строки — пробел или начало команды: `#` в начале строки — комментарий. После `\` с переводом
    # строки слова склеиваются: `x\` ⏎ `#y` — слово `x#y`.
    blank = True

    def flush(pipe=False):
        """Закрывает текущий сегмент; пустой сегмент без конвейера передаёт свой вход следующему
        (`a |` ⏎ `b`, `a |& b`). Номер закрытого сегмента или None."""
        nonlocal cur
        text = "".join(cur[0])
        if cur[1] and not pipe and not text.strip():
            cur = [[], cur[1]]
            return None
        segments.append(text)
        index = len(segments) - 1
        feeds.extend((source, index) for source in cur[1])
        cur = [[], [index] if pipe else []]
        return index

    def outer_eof():
        """Знак конца heredoc вне кавычек: у самой внутренней открытой подстановки или тела команды (_Body)."""
        return parens[-1][2] if parens else command.eof if isinstance(command, _Body) else ""

    def open_paren(kind):
        nonlocal cur
        parens.append((kind, cur, ")"))
        cur = [[], []]

    def close_paren():
        nonlocal cur
        index = flush()
        paren_at.pop()
        kind, outer, _ = parens.pop()
        if outer is not None:
            cur = outer
            if kind == "<" and index is not None:
                cur[1].append(index)

    size = len(command)
    while base <= size:
        eol = command.find("\n", base)
        eol = size if eol < 0 else eol
        line, line_at = command[base:eol], base
        base = eol + 1
        if pending:
            # Строка тела: у терминатора без кавычек — склеенная по `\` с переводом строки (_body_line).
            text, pieces, eol = _body_line(command, line_at, eol, size, pending[0][2])
            base = eol + 1
            closes = _closes(text)
            pos, stop, quoted = 0, None, pending[0][2]
            while pending and pos < len(text):
                term, strip_tabs, quoted, eof = pending[0]
                # Heredoc вне кавычек строки — внутри незакрытых с прошлых строк скобок или подстановки, тело которой
                # разбирается (_heredoc_end).
                stop = _heredoc_end(text, pos, (term, strip_tabs, quoted, eof or outer_eof()), closes)
                if stop is None:
                    break
                pending.popleft()
                if body:
                    bodies.append("\n".join(body))
                    body = []
                if stack:
                    cur[0].append(command[_at(pieces, pos):_at(pieces, stop)])
                pos = stop
            if stop is None:
                if stack:
                    cur[0].append(command[_at(pieces, pos):eol] + "\n")
                if not stack and not quoted:
                    body.append(text[pos:])
                if carry is not None and not stack:
                    carry[1].append(text[pos:])
                continue
            if pos == len(text):
                if stack:
                    cur[0].append("\n")
                continue
            # Остаток строки за терминатором bash читает заново командами (_heredoc_end) — с физической строки, где
            # он начинается; следующие склеенные строки — снова строки команды.
            line_at = _at(pieces, pos)
            eol = command.find("\n", line_at, size)
            eol = size if eol < 0 else eol
            line, base = command[line_at:eol], eol + 1
        # Позиция, с которой строка идёт вне кавычки, перенесённой с прошлых строк.
        outside = 0 if not stack else None
        # Позиция последней кавычки, открытой на этой строке вне кавычек.
        quote_start = None
        # Heredoc, открытые внутри кавычек этой строки.
        inner = []
        continued = False
        # Синтаксическая ошибка скобок присваивания массива на строке (_scan).
        error = False
        # Позиция последнего `$` вне кавычек, не экранированного: за ним `(` — подстановка.
        dollar = -2
        i, n = 0, len(line)
        while i < n:
            if stack:
                found, stop = _scan(line, stack, i)
                end = n if stop is None else stop
                cur[0].append(line[i:end])
                if stack[-1:] == ["!"]:
                    error = True
                    break
                inner.extend(found)
                i = end
                if not stack and outside is None:
                    outside = i
                continue
            c = line[i]
            if c == "\\":
                if i + 1 == n:
                    continued = True
                    blank = line[i - 1] in " \t" if i else blank
                    break
                cur[0].append(line[i:i + 2])
                i += 2
                continue
            if line.startswith("$$", i):
                # `$$` — параметр целиком: `{`, `'`, `(` за ним не начинают `${…}`, `$'…'`, `$(…)`.
                cur[0].append("$$")
                i += 2
                continue
            if line.startswith(("$'", "${", "$["), i) or c in "'\"`":
                # `${…}`, `$[…]`, `` `…` `` и подстановку функции `${ …; }` _scan ведёт, как кавычку: `#`, `;`, `|`,
                # `&`, скобки, кавычки внутри — часть слова; тела `` `…` `` и `${ …; }` найдёт _quoted_substitutions.
                funsub = _funsub_open(line, i)
                opener = line[i:i + (funsub or 2)] if c == "$" else c
                stack.append("F" if funsub else "$'" if opener == "$'" else opener[-1])
                quote_start = i
                cur[0].append(opener)
                i += len(opener)
                continue
            if c == "#" and (_comment_start(line, i) if i else blank):
                break
            if line.startswith(("&&", "||"), i):
                flush()
                i += 2
                continue
            if c == "(":
                kind = "$" if dollar == i - 1 else line[i - 1] if i and line[i - 1] in "<>" else "("
                if kind == "(":
                    flush()
                    parens.append(("(", None, outer_eof()))
                else:
                    open_paren(kind)
                paren_at.append(line_at + i)
            elif c == ")":
                if len(paren_at) > 1 and paren_at[-1] == paren_at[-2] + 1 and line.startswith("))", i):
                    # `((…))` и `$((…))`, закрытые `))`, — арифметика: bash раскрывает выражение, как текст в
                    # `"…"`, подстановки в её `'…'` — команды (_quoted_substitutions с arith). `)` её нулевой
                    # глубины не перед второй `)` — подоболочка в подоболочке или подстановке, как здесь.
                    start = paren_at[-1] + 1
                    while ariths and ariths[-1][0] >= start:
                        ariths.pop()
                    ariths.append((start, line_at + i))
                if parens:
                    close_paren()
                else:
                    flush()
            elif c == "|" and not line.startswith("||", i):
                flush(pipe=True)
            elif c == ";" or (c == "&" and line[i - 1:i] not in ("<", ">") and line[i + 1:i + 2] != ">"):
                flush()
            else:
                if c == "$":
                    dollar = i
                cur[0].append(c)
            i += 1
        if stack and not error:
            if outside is not None and quote_start is not None:
                found = _line_heredocs(line[outside:quote_start], arrays)
                error = found is None
                opened.extend(found or ())
            if not error:
                pending = deque(inner)
                cur[0].append("\n")
                continue
        if outside is not None and not error:
            found = _line_heredocs(line[outside:i] if continued else line[outside:], arrays)
            error = found is None
            opened.extend(found or ())
        if error:
            # Синтаксическая ошибка скобок присваивания массива: bash отбрасывает строку и всю незаконченную команду
            # вместе с телами heredoc, следующую строку читает заново (5.3: `x=( a ; ) <<E` ⏎ `npm i x` исполняет
            # `npm i x`).
            stack.clear()
            arrays.clear()
            opened, pending = [], deque()
            flush()
            while parens:
                _, outer, _ = parens.pop()
                if outer is not None:
                    cur = outer
                    flush()
            paren_at.clear()
        elif continued:
            continue
        blank = True
        flush()
        if not any(pkgmanagers._heredoc_runs(_command(s)[0]) for s in segments[logical_start:]):
            pending = deque(opened)
            if opened and cur[1] and carry is None:
                # Конвейер продолжается за телом: приёмник — на следующих строках.
                carry = (logical_start, [])
        if carry is not None and not cur[1]:
            if any(pkgmanagers._heredoc_runs(_command(s)[0]) for s in segments[carry[0]:]):
                scripts.append("\n".join(carry[1]))
            carry = None
        opened = []
        logical_start = len(segments)
    if body:
        bodies.append("\n".join(body))
    flush()
    if carry is not None and any(pkgmanagers._heredoc_runs(_command(s)[0]) for s in segments[carry[0]:]):
        scripts.append("\n".join(carry[1]))
    while parens:
        _, outer, _ = parens.pop()
        if outer is not None:
            cur = outer
            flush()
    for start, end in ariths:
        scripts.extend(_quoted_substitutions(command[start:end], arith=True))
    return segments, feeds, bodies, scripts


def _echo_text(segment):
    """Вывод сегмента `echo …` или `printf …` с аргументами-литералами; None — вывод неизвестен. `\\n` — перевод
    строки, у printf каждый аргумент — с новой строки: лишний перевод строки команд не создаёт."""
    words, _ = _command(segment)
    if not words:
        return None
    name = pkgmanagers._basename(words[0])
    args = words[1:]
    if name == "echo":
        while args and re.fullmatch(r"-[neE]+", args[0]):
            args = args[1:]
        text = " ".join(args)
    elif name == "printf":
        if args[:1] == ["--"]:
            args = args[1:]
        if args[:1] == ["-v"]:
            return None
        text = "\n".join(args)
    else:
        return None
    return text.replace("\\n", "\n").replace("\\t", " ")


# Операнд `cat`, который читает stdin: `-`, `/dev/stdin`, `/dev/fd/N`, `/proc/<процесс>/fd/N`.
_STDIN_OPERAND = re.compile(r"-|/dev/stdin|/dev/fd/\d+|/proc/[^/]+/fd/\d+")


def _heredoc_cat(segment):
    """Сегмент — `cat` без флагов с heredoc, без операндов или с операндом stdin (_STDIN_OPERAND): его вывод — тело
    heredoc и файлы."""
    words = _command(segment)[0]
    return words[:1] == ["cat"] and not any(a.startswith("-") and a != "-" for a in words[1:]) and (
        len(words) == 1 or any(_STDIN_OPERAND.fullmatch(a) for a in words[1:])) and bool(heredocs(segment))


def _computed(text):
    """Имя хотя бы одной команды строки вычисляется при исполнении: первое слово команды сегмента (_command) —
    подстановка или переменная (`$X`, `"$X"`, `` `…` ``, заглушка `$` _parse на месте `$(…)`). Кавычки ANSI-C
    `$'…'` — не подстановка: _split их раскрывает."""
    return any(words[0].startswith(("$", "`")) for words in (_command(s)[0] for s in _parse(text)[0]) if words)


def _file_output(segment):
    """Вывод сегмента — содержимое названных файлов: `cat` с файлами без флагов и операндов stdin (_STDIN_OPERAND),
    `git show <ревизия>:<путь>`. Скрипт из файла детектор не видит, как у `bash файл`."""
    words = _command(segment)[0]
    if not words:
        return False
    name, args = pkgmanagers._basename(words[0]), words[1:]
    if name == "cat":
        return bool(args) and not any(a.startswith("-") or _STDIN_OPERAND.fullmatch(a) for a in args)
    return name == "git" and args[:1] == ["show"] and len(args) > 1 and all(
        ":" in a and not a.startswith("-") for a in args[1:])


def _stdin_scripts(segments, feeds, bodies):
    """Текст, который команда исполняет со stdin или из тела heredoc: (сегмент для отказа, текст). Вывод `echo`/
    `printf` в оболочку без скрипта (`echo 'npm i x' | bash`, `bash <(echo …)`) или в `source` и `.` без скрипта
    (`source <(…)`: процесс-подстановку с её `<` снимает _drop_redirect_pairs) либо со скриптом `-`, `/dev/stdin`,
    если приёмник без маркера; тела подстановок в теле heredoc без кавычек. Вход такого приёмника не из литерального
    `echo`/`printf` (`curl … | bash`, `echo $X | bash`) — (сегмент приёмника, None): текст неизвестен до
    исполнения; кроме `cat` с heredoc (_heredoc_cat) в оболочку, читающую stdin: тело уже разобрано командами, — и
    файлов (_file_output) без входа из другого сегмента: скрипт из файла не виден, как у `bash файл`."""
    # Приёмник с многими источниками (`bash <(…) <(…)`) разбирается один раз.
    runs = {}
    # Сегменты со входом из другого сегмента: у `cat <(curl …)` файл — вывод программы.
    fed = {target for _, target in feeds}
    for source, target in feeds:
        if target not in runs:
            words, marker = _command(segments[target])
            runs[target] = not marker and bool(words) and (
                pkgmanagers._heredoc_runs(words) or pkgmanagers._basename(words[0]) in pkgmanagers._SOURCES and len(words) == 1)
        if not runs[target]:
            continue
        if _heredoc_cat(segments[source]) and pkgmanagers._heredoc_runs(_command(segments[target])[0]):
            # Тело heredoc в логической строке оболочки, читающей stdin, _parse уже разделил на сегменты-команды.
            continue
        if _file_output(segments[source]) and source not in fed:
            continue
        text = _echo_text(segments[source])
        if text is None or _computed(text):
            yield segments[target].strip(), None
        elif text:
            yield segments[source].strip(), text
    for body in bodies:
        for text in _quoted_substitutions(body, heredoc=True):
            yield text.strip(), text


def _find(command, depth):
    """Первый сегмент команды, добавляющий пакет; вложенные команды разбираются до _MAX_DEPTH."""
    segments, feeds, bodies, scripts = _parse(command)
    for segment in segments:
        segment = segment.strip()
        if segment and _segment_adds(segment, depth):
            return segment
    if depth < _MAX_DEPTH:
        for segment, text in _stdin_scripts(segments, feeds, bodies):
            if text is not None and _find(text, depth + 1):
                return segment
        for text in scripts:
            found = _find(text, depth + 1)
            if found:
                return found
    return None


def _segment_adds(segment, depth):
    """Сегмент добавляет пакет сам, через строку, которую его команда исполняет (_scripts), или через подстановку в
    двойных кавычках. Маркер сегмента покрывает его строки _scripts, но не подстановки: они исполняются до команды
    сегмента."""
    words, marker = _command(segment)
    if depth >= _MAX_DEPTH:
        return not marker and _is_add(words)
    if not marker and (_is_add(words) or any(_find(s, depth + 1) for s in _scripts(segment, words))):
        return True
    return any(_find(s, depth + 1) for s in _quoted_substitutions(segment))


def _scripts(segment, words):
    """Строки, которые команда сегмента исполняет как команды: `sh -c '…'`, `eval …`, here-string оболочки без
    скрипта или `source /dev/stdin` (`bash <<< '…'`, _heredoc_runs), удалённая команда ssh, `trap '…'`,
    `find -exec …`, `cmd /c`, `pwsh -Command`, `nix develop -c`, `nix-shell --run`, `mise exec --`, команды git
    (_git_runs)."""
    out = []
    script = pkgmanagers._inline_script(words)
    if script is not None:
        out.append(script)
    if pkgmanagers._heredoc_runs(words):
        out.extend(_herestrings(segment))
    if words:
        out.extend(_launched(pkgmanagers._basename(words[0]), words[1:]))
    return out


# Here-string: `<<<`, `0<<<`.
_HERESTRING = re.compile(r"^\d*<<<")


def _herestrings(segment):
    """Строки here-string сегмента."""
    pairs = _split(segment)
    out = []
    for k, (word, lead) in enumerate(pairs):
        m = _HERESTRING.match(lead)
        if m:
            value = word[m.end():] if len(word) > m.end() else pairs[k + 1][0] if k + 1 < len(pairs) else ""
            out.append(value)
    return out


# Переменная с абсолютным путём в начале слова (`$HOME/x`, `${PWD}`): пустая даёт `/…`, тоже абсолютный путь.
_ABSOLUTE_VAR = re.compile(r"^\$(?:HOME|PWD|OLDPWD|TMPDIR|\{(?:HOME|PWD|OLDPWD|TMPDIR)\})(?=/|$)")
# Флаги `find`, за которыми до `;` или `+` идёт команда.
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}


def _find_runs(args):
    """Команды `find -exec`/`-execdir`/`-ok`/`-okdir` строками. `{}` в них — найденный путь: у `-exec` с начальной
    точкой поиска впереди (`find src` — `src/…`; из нескольких — первая, что не начинается с `.`, `/`, `~`: npm
    читает `src/a` как репозиторий GitHub; `$HOME`, `$PWD`, `$OLDPWD`, `$TMPDIR` — абсолютные, _ABSOLUTE_VAR), у
    `-execdir` — `./…`; путь — локальный, не пакет."""
    i = 0
    # Флаги find перед начальными точками: `-H`, `-L`, `-P`, `-D отладка`, `-Oуровень`.
    while i < len(args) and (args[i] in ("-H", "-L", "-P") or args[i].startswith("-O")):
        i += 1
    while i < len(args) and args[i] == "-D":
        i += 2
    starts = []
    while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", "!", ")", ","):
        starts.append(args[i])
        i += 1
    starts = [_ABSOLUTE_VAR.sub("/", p) for p in starts]
    start = next((p for p in starts if not p.startswith((".", "/", "~"))), starts[0] if starts else ".")
    out, run, place = [], None, None
    for a in args[i:]:
        if run is None:
            if a in _FIND_EXEC:
                run = []
                place = "./{}" if a.endswith("dir") else start.rstrip("/") + "/{}"
        elif a in (";", "+"):
            out.append(shlex.join(run))
            run = None
        else:
            run.append(a.replace("{}", place))
    if run:
        out.append(shlex.join(run))
    return out


def _launched(name, args):
    """Команды, которые name запускает из своих аргументов args, строками для разбора."""
    if name == "git":
        return pkgmanagers._git_runs(args)
    if name == "ssh":
        command = pkgmanagers._ssh_command(args)
        return [" ".join(command)] if command else []
    if name == "trap":
        rest = pkgmanagers._after_flags(args, pkgmanagers._NO_VALUE_FLAGS)
        return rest[:1] if len(rest) > 1 else []
    if name == "find":
        return _find_runs(args)
    if name == "cmd":
        # Ключи cmd (`/d`, `/s`, `/q`, `/v:on` и др.) стоят до `/c` или `/k`; команда — остаток слова и слова за ним.
        for k, a in enumerate(args):
            if not a.startswith("/"):
                break
            if a[1:2].lower() in ("c", "k"):
                return [" ".join([a[2:], *args[k + 1:]]).strip()]
        return []
    if name in ("pwsh", "powershell"):
        for k, a in enumerate(args):
            flag = a.lower().lstrip("-/")
            if a[:1] in "-/" and flag and "command".startswith(flag) and (flag == "c" or len(flag) >= 3):
                return [" ".join(args[k + 1:])]
        return []
    if name == "nix" and args[:1] in (["develop"], ["shell"]):
        for k, a in enumerate(args):
            if a in ("-c", "--command"):
                return [shlex.join(args[k + 1:])]
        return []
    if name == "nix-shell":
        return [args[k + 1] for k, a in enumerate(args[:-1]) if a in ("--run", "--command")]
    if name in ("mise", "rtx") and args[:1] in (["exec"], ["x"]) and "--" in args:
        return [shlex.join(args[args.index("--") + 1:])]
    return []


# Разделители команд и кавычки в тексте глубже _MAX_DEPTH для _soup_add.
_SOUP_SPLIT = re.compile(r"[;&|()`\n]+")
_SOUP_QUOTES = re.compile(r"[\"'\\]")


def _flat_add(words):
    """Слова, где вложенность уже не разбирается, ставят пакет: первое слово после присваиваний, ключевых слов,
    флагов, обёрток, `eval`, оболочек, запускателей (`npx`, `corepack`, `python`) и `<менеджер> run` с версией
    `@…` без неё и слова за ним разбирает _is_add. Маркер согласия среди присваиваний перед ним снимает
    проверку."""
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        name = pkgmanagers._basename(word)
        if word == DEP_OK_MARKER:
            return False
        if (_ENV_ASSIGN.match(word) or word.startswith(("-", "+")) or word in _KEYWORDS or name in _WRAPPERS
                or name in pkgmanagers._C_SHELLS or name in pkgmanagers._NPX or name in ("eval", "corepack") or pkgmanagers._PYTHON.match(name)):
            i += 1
        elif name in pkgmanagers._RUNNERS and words[i + 1:i + 2] == ["run"]:
            i += 2
        else:
            break
    return i < n and _is_add([_VERSION.sub("", words[i]), *words[i + 1:]])


def _soup_add(text):
    """Текст глубже _MAX_DEPTH ставит пакет: кавычки и `\\` сняты, команды разделены по `;`, `&`, `|`, скобкам,
    обратной кавычке и переводу строки, каждая проверена _flat_add."""
    return any(_flat_add(_SOUP_QUOTES.sub("", piece).split()) for piece in _SOUP_SPLIT.split(text))


# Начало `${…}` с оператором значения по умолчанию, присваивания, замены или ошибки (`${x:-`, `${x+`).
_PARAM_OPEN = re.compile(r"\$\{[#!]?(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])?:?[-=+?]?")


def _expanded_name_install(words):
    """Имя команды — подстановка с пробелом внутри `${…}` (`${x:-npm i x}`, `${x:- npm i x }+`): bash делит её
    раскрытие на слова, и слова значения по умолчанию могут быть командой установки. Слова значения — текст слова без
    `${…`-начал и `}`, разделённый по пробелам; с ними и словами за именем — _is_add, _expanded_install или
    _launches_install."""
    if not words or "${" not in words[0] or not any(c.isspace() for c in words[0]):
        return False
    flat = _PARAM_OPEN.sub(" ", words[0]).replace("}", " ").split() + words[1:]
    return bool(flat) and (_is_add(flat) or pkgmanagers._expanded_install(flat) or _launches_install(flat))


def _doubt(words, xargs, cut, wrapped):
    """Довод сомнения для слов команды сегмента и признаков _command_info (cut — довод, если слова видны не
    целиком: за пределом раскрытия скобок — сомнение у любой команды; за _WORDS_LIMIT — если видимое имя — менеджер
    или запускатель (_installer), кроме `python` со скриптом, `-c` или `-` в видимых словах (_python_target: слова
    дальше — их аргументы): слово дальше может быть подкомандой или пакетом, — или за пределом может стоять строка
    `eval`/`sh -c`): менеджер и команда установки распознаны, а пакет или подкоманда под сомнением, менеджер или его
    подкоманда неизвестны до исполнения (подкоманда — подстановка, кроме вызова справки менеджера: _asks_help),
    команда стоит за пределом обёрток или неизвестная программа получает словами менеджер и за ним глагол установки
    или флаг (_launches_install); None — сомнения нет."""
    if wrapped:
        return _WHY_WRAPPERS
    if xargs and _is_add([*words, _STDIN_PACKAGE]):
        return _WHY_XARGS
    if cut == _WHY_BRACES or cut and words and (pkgmanagers._installer(words[0]) or pkgmanagers._cut_script(words)) and not (
            pkgmanagers._PYTHON.match(pkgmanagers._basename(words[0])) and pkgmanagers._python_target(words)[0] in ("c", "script")):
        return cut
    if _is_add(words, mode=_MASKED):
        return _WHY_DRY
    if _is_add(words, mode=_LOOSE):
        return _WHY_FLAG
    if _is_add(words, mode=_DEEP):
        return _WHY_DEPTH
    if pkgmanagers._expanded_install(words) or _expanded_name_install(words):
        return _WHY_NAME
    if _is_add(words, mode=_COMPUTED):
        return _WHY_SUBCOMMAND
    if _launches_install(words):
        return _WHY_LAUNCHER
    return None


def _computed_script(segment, words):
    """Довод сомнения, если в строке, которую команда исполняет оболочкой, имя команды вычисляется при исполнении
    (_computed): `eval "$X"`, `eval $(…)`, `sh -c "$(…)"`, `sh -c ":; $X"`, here-string оболочки `bash <<< "$X"`;
    None — такой строки нет. Строка, которая пакета не ставит (`sh -c "$(jq -r .cmd settings.json)"`), — ложный
    отказ, обход — маркер."""
    name = pkgmanagers._basename(words[0]) if words else None
    script = pkgmanagers._inline_script(words) if name == "eval" or name in pkgmanagers._C_SHELLS else None
    texts = ([script] if script is not None else []) + (_herestrings(segment) if pkgmanagers._heredoc_runs(words) else [])
    return _WHY_COMPUTED if any(_computed(t) for t in texts) else None


def _launches_install(words):
    """Программа, неизвестная детектору (не менеджер, не обёртка, не оболочка, не запускатель из _launched и не
    программа-данные _DATA_PROGRAMS), получает словами менеджер пакетов и за ним глагол установки или флаг
    (`direnv exec . npm install x`, `docker exec c npm --weird v i x`), и слова с менеджера по его же правилам
    разбора — добавление пакета или сомнение (_is_add, _doubt): программа может запустить их командой. Установка
    без пакета (`docker exec web npm install`) и пробный прогон — не сомнение."""
    if len(words) < 3:
        return False
    name = pkgmanagers._basename(words[0])
    # Слово с `=` в кавычках (`'A=1' npm i x`) bash ищет как программу и не находит.
    if name in _KNOWN_PROGRAMS or "=" in name or pkgmanagers._PIP.match(name) or pkgmanagers._PYTHON.match(name):
        return False
    pairs = 0
    for k in range(1, len(words) - 1):
        head, following = pkgmanagers._basename(words[k]), words[k + 1]
        if not (head in _PAIR_MANAGERS or pkgmanagers._PIP.match(head)) or not (
                following in _LAUNCH_VERBS or following.startswith("-")):
            continue
        # Каждая пара разбирает хвост целиком: за пределом пар — сомнение без разбора, проход линеен.
        pairs += 1
        tail = words[k:]
        if pairs > _MAX_LAUNCH_PAIRS or _is_add(tail) or _doubt(tail, False, None, False):
            return True
    return False


def _segment_doubt(segment, depth):
    """Довод сомнения сегмента: его команда (_doubt), вычисляемая строка `eval` или here-string (_computed_script),
    строка `sh -c`/`eval` и подстановки в двойных кавычках — как в _segment_adds; на глубине _MAX_DEPTH вложенный
    текст проверяет _soup_add."""
    words, marker, xargs, cut, wrapped = _command_info(segment)
    why = None if marker else _doubt(words, xargs, cut, wrapped) or _computed_script(segment, words)
    if why:
        return why
    nested = ([] if marker else _scripts(segment, words)) + _quoted_substitutions(segment)
    if depth >= _MAX_DEPTH:
        return _WHY_DEPTH if any(_soup_add(t) for t in nested) else None
    for text in nested:
        found = _find_doubt(text, depth + 1)
        if found:
            return found[1]
    return None


def _find_doubt(command, depth):
    """Первый сегмент команды с сомнением и довод (_segment_doubt, затем _stdin_scripts: вход оболочки не из
    литерального `echo`/`printf`, heredoc `cat` и файлов — _WHY_COMPUTED, — и тела heredoc конвейера, продолженного
    за ними, _parse); None, если такого нет."""
    segments, feeds, bodies, scripts = _parse(command)
    for segment in segments:
        segment = segment.strip()
        why = _segment_doubt(segment, depth)
        if why:
            return segment, why
    for segment, text in chain(_stdin_scripts(segments, feeds, bodies), ((t.strip(), t) for t in scripts)):
        if text is None:
            return segment, _WHY_COMPUTED
        if depth >= _MAX_DEPTH:
            if _soup_add(text):
                return segment, _WHY_DEPTH
            continue
        found = _find_doubt(text, depth + 1)
        if found:
            return segment, found[1]
    return None


def dependency_doubt(command):
    """Сегмент команды, где установка пакета под сомнением, и довод (_WHY_*): флаг перед подкомандой вне известных
    наборов, `--dry-run` значением флага, пакеты из stdin xargs, вложенность глубже _MAX_DEPTH, слова за пределом
    раскрытия скобок, слово менеджера или строка `eval`/`sh -c` за _WORDS_LIMIT (_installer, _cut_script), команда
    за _MAX_WRAPPERS обёртками, имя команды — подстановка с глаголом установки или с пробелом внутри `${…}`
    (_WHY_NAME: _expanded_install, _expanded_name_install), подкоманда менеджера — подстановка или переменная
    (_WHY_SUBCOMMAND), строка оболочки вычисляется при исполнении (_WHY_COMPUTED), неизвестная программа получает
    словами менеджер и глагол установки (_WHY_LAUNCHER). None — сомнения нет. Смысл — для
    команды, где dependency_add добавления не нашёл; маркер согласия снимает сомнение там же, где и проверку."""
    if not isinstance(command, str):
        return None
    return _within_call(_find_doubt, command)


def dependency_add(command):
    """Сегмент команды, добавляющий пакет; None, если такого нет. Маркер согласия снимает проверку с
    сегмента, где он стоит среди ведущих присваиваний команды, в том числе после `sudo`, `env`, `if`
    (`PLANKA_DEP_OK=1 npm install x`)."""
    if not isinstance(command, str):
        return None
    return _within_call(_find, command)


# ---------------------------------------------------------------------------------------------------------------
# Вопросы детектора над деревом shparse: _tree_add и _tree_doubt — тот же смысл, что у dependency_add и
# dependency_doubt, а текст читает shparse.parse вместо лексера выше. Семантика менеджеров, обёрток и доводов — та
# же (_is_add, _doubt, pkgmanagers).
#
# Сегмент — простая команда дерева (shparse.Simple, её текст без пробелов по краям). Подстановки, тела функций,
# арифметика и тела heredoc уже лежат в дереве: их простые команды проверяются на той же глубине, что и команда
# вокруг. Глубина _MAX_DEPTH считает только тексты, которые разбираются заново: строки `sh -c`/`eval`, вход
# оболочки (`echo … | bash`, `bash <(…)`), тела heredoc оболочки без скрипта, команды _launched.

# Раскрытие (параметр, подстановка, арифметика) длиннее _TREE_EXPANSION_MAX символов в слове — заглушка
# _TREE_PLACEHOLDER: его вывод неизвестен до исполнения, а текст в слове повторял бы вложенный текст на каждом уровне
# вложенности. Имя команды с раскрытием проверяет по узлу _tree_name_doubt.
_TREE_PLACEHOLDER = "$_"
_TREE_EXPANSION_MAX = 256
# Сколько символов текста имён команд с подстановкой (_tree_name_doubt) разбирает один вызов _tree_doubt; дальше —
# сомнение без разбора: проход линеен.
_TREE_NAME_BUDGET = 4 * _WORDS_LIMIT
# Операторы перенаправления вывода: `>(…)` их целью получает вывод команды.
_TREE_OUT_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


class _TreeWord(str):
    """Слово команды из дерева; node — его shparse.Word (имя команды с подстановкой — _tree_name_doubt)."""
    node = None


class _TreeCommand:
    """Простая команда дерева для вопросов детектора: text — разобранный текст, segment — её текст без пробелов по
    краям; words, marker, xargs, cut, wrapped — как у _command_info_parsed; herestrings — строки `<<<`; bodies —
    тела её heredoc (_tree_body); docs — heredoc текста: (начала их слов терминатора по возрастанию, узлы)."""
    __slots__ = ("simple", "text", "segment", "words", "marker", "xargs", "cut", "wrapped", "herestrings", "bodies",
                 "docs")


def _tree_part(part, text):
    """Текст части слова дерева, как его отдаёт _split_cut: кавычки и `\\` сняты, `$'…'` раскрыта, раскрытие —
    своим текстом в text (длиннее _TREE_EXPANSION_MAX — _TREE_PLACEHOLDER)."""
    kind = type(part)
    if kind is shparse.Lit or kind is shparse.SQ:
        return part.text
    if kind is shparse.AnsiC:
        return part.value
    if kind is shparse.DQ:
        # В "…" нет "…" и $'…': глубина — один уровень.
        return "".join(_tree_part(p, text) for p in part.parts)
    if part.end - part.start > _TREE_EXPANSION_MAX:
        return _TREE_PLACEHOLDER
    return text[part.start:part.end]


def _tree_word(word, text):
    """Текст слова дерева (_tree_part) с узлом слова (_TreeWord)."""
    out = _TreeWord("".join(_tree_part(p, text) for p in word.parts))
    out.node = word
    return out


def _tree_lead(word, text):
    """Начало слова до первой кавычки или `\\`, как у _split_cut: по нему узнаются присваивание за обёрткой и
    ключевое слово. Литерал с `\\` (текст в text отличается от значения) начало кончает."""
    out = []
    for part in word.parts:
        kind = type(part)
        if kind is shparse.Lit:
            if text[part.start:part.end] != part.text:
                break
            out.append(part.text)
        elif kind is shparse.SQ or kind is shparse.DQ or kind is shparse.AnsiC:
            break
        else:
            out.append(_tree_part(part, text))
    return "".join(out)


def _tree_marks(word, text):
    """Символы слова дерева парами (символ, раскрывается ли) для _brace_words: раскрываются `{`, `,`, `}` литералов
    вне кавычек без `\\` перед ними; None — в литералах слова нет `{`. Литерал, чей текст в text после снятия `\\` не
    совпадает со значением (тело `` `…` `` с позициями из другой строки), раскрывается целиком: лишнее слово
    раскрытия дешевле пропуска."""
    if not any(type(p) is shparse.Lit and "{" in p.text for p in word.parts):
        return None
    out = []
    for part in word.parts:
        if type(part) is not shparse.Lit:
            out.extend((c, False) for c in _tree_part(part, text))
            continue
        src, marks, i = text[part.start:part.end], [], 0
        while i < len(src):
            if src[i] == "\\" and i + 1 < len(src):
                if src[i + 1] != "\n":
                    marks.append((src[i + 1], False))
                i += 2
                continue
            marks.append((src[i], src[i] in "{,}"))
            i += 1
        if "".join(c for c, _ in marks) != part.text:
            marks = [(c, c in "{,}") for c in part.text]
        out.extend(marks)
    return out


def _tree_procsub(word):
    """Слово — одна процесс-подстановка `<(…)` или `>(…)`: её Sub или None."""
    parts = word.parts
    if len(parts) == 1 and type(parts[0]) is shparse.Sub and parts[0].kind in ("<(", ">("):
        return parts[0]
    return None


def _tree_pairs(simple, text, budget, braces=True):
    """Пары (слово, начало) простой команды дерева, как у _split_cut с braces: ведущие присваивания, затем слова;
    перенаправления не входят, слово из одной процесс-подстановки — тоже (как `<` и `>` у _drop_redirect_pairs:
    её вход и выход — связи _tree_feeds). Начало присваивания — "_=" (присваивание при любом индексе: его узнал
    разбор), у маркера согласия без кавычек и `\\` — сам маркер. Фигурные скобки слов вне присваиваний раскрываются
    (_brace_words) с общим пределом budget. Возвращает (пары, кончился ли предел раскрытия)."""
    out = []
    for word in simple.assigns:
        src = text[word.start:word.end]
        out.append((_tree_word(word, text), DEP_OK_MARKER if src == DEP_OK_MARKER else "_="))
    for word in simple.words:
        if _tree_procsub(word) is not None:
            continue
        value, lead = _tree_word(word, text), _tree_lead(word, text)
        marks = _tree_marks(word, text) if braces and not _ENV_ASSIGN.match(lead) else None
        if marks:
            words, cut = _brace_words(marks, budget)
            if cut:
                out.extend((w, "") for w in words)
                return out, True
            if words != [value]:
                out.extend((w, "") for w in words)
                continue
        out.append((value, lead))
    return out, False


def _tree_split(value):
    """Пары (слово, начало) первой простой команды текста value (строка `env -S`), без раскрытия скобок."""
    simples = shparse.simple_commands(shparse.parse(value))
    return _tree_pairs(simples[0], value, None, braces=False)[0] if simples else []


def _tree_strip(pairs, braced):
    """Слова команды, маркер, `xargs`, довод `cut` и обёртка за пределом — как у _command_info_parsed, по парам
    дерева (_tree_pairs); строка `env -S` разбирается _tree_split."""
    words = deque(pairs)
    marker = xargs = False
    wrappers = 0
    while words:
        word, lead = words[0]
        if _ENV_ASSIGN.match(lead):
            marker = marker or word == lead == DEP_OK_MARKER
            words.popleft()
        elif word == lead == "function" or word == lead == "coproc" and len(words) > 2 and (
                words[2][0] == words[2][1] and words[2][0] in _COMPOUND):
            words.popleft()
            if words:
                words.popleft()
        elif word in _KEYWORDS and word == lead:
            words.popleft()
        elif pkgmanagers._basename(word) in _WRAPPERS and wrappers < _MAX_WRAPPERS:
            wrappers += 1
            name = pkgmanagers._basename(word)
            xargs = xargs or name == "xargs"
            value_flags, operands = _WRAPPERS[name]
            words.popleft()
            if name in _C_WRAPPERS:
                pkgmanagers._c_wrapper(words, value_flags)
                continue
            shell = name == "watch"
            while words and words[0][0].startswith("-"):
                flag = words.popleft()[0]
                if flag == "--":
                    break
                if name == "watch" and _WATCH_EXEC.match(flag):
                    shell = False
                if name == "env" and (flag in _ENV_SPLIT or flag.startswith(("-S", "--split-string="))):
                    if flag in _ENV_SPLIT:
                        value = words.popleft()[0] if words else ""
                    else:
                        value = flag[2:] if flag.startswith("-S") else flag.partition("=")[2]
                    words.extendleft(reversed(_tree_split(value)))
                    continue
                if pkgmanagers._takes_value(flag, value_flags) and words:
                    words.popleft()
            for _ in range(min(operands, len(words))):
                words.popleft()
            if name == "flock" and words and words[0][0] in pkgmanagers._C_FLAGS:
                words.popleft()
                script = words.popleft()[0] if words else ""
                words.clear()
                words.extend([("sh", "sh"), ("-c", "-c"), (script, script)])
            elif shell and words:
                script = " ".join(word for word, _ in words)
                words.clear()
                words.extend([("sh", "sh"), ("-c", "-c"), (script, script)])
        else:
            break
    out, offset = [], 0
    for word, _ in words:
        if offset >= _WORDS_LIMIT:
            break
        out.append(word)
        offset += len(word) + 1
    cut = _WHY_BRACES if braced else _WHY_CUT if any(
        not w.startswith("-") for w, _ in islice(words, len(out), None)) else None
    wrapped = bool(words) and pkgmanagers._basename(words[0][0]) in _WRAPPERS
    return out, marker, xargs, cut, wrapped


def _tree_body(doc, text):
    """Текст тела heredoc, который получает команда: у терминатора в кавычках — тело как есть, иначе — части тела,
    раскрытия своим текстом (_tree_part): вывод подстановки неизвестен, а `\\$(…)` тела — уже `$(…)`."""
    if doc.quoted:
        return doc.body_text or ""
    return "".join(_tree_part(p, text) for p in doc.parts)


# Текущий вызов _tree_add или _tree_doubt: (остаток предела раскрытия _BRACE_BUDGET — список из одного числа,
# разборы текстов _tree_analysis по тексту, остаток _TREE_NAME_BUDGET — список из одного числа) или None вне
# вызова.
_tree_call = None


def _tree_within(find, command):
    """find(command, 0) с одним пределом раскрытия _BRACE_BUDGET на весь вызов: текст, прочитанный несколько раз,
    разбирается и тратит предел один раз (_tree_analysis)."""
    global _tree_call
    outer = _tree_call
    _tree_call = ([_BRACE_BUDGET], {}, [_TREE_NAME_BUDGET])
    try:
        return find(command, 0)
    finally:
        _tree_call = outer


# Сколько раз начало строки с фатальной ошибкой укорачивается до новой ошибки и сколько раз остаток текста за
# фатальной ошибкой разбирается заново (_tree_scripts): bash за первой фатальной ошибкой не исполняет ничего, проверка
# остатка — запас на ошибку разбора, а каждый повторный разбор держит свою копию остатка.
_TREE_PREFIX_TRIES = 4
_TREE_RECOVERIES = 16


def _tree_scripts(command):
    """Разборы текста (текст, Script). После фатальной синтаксической ошибки bash не исполняет ни её команды, ни
    остаток, но ошибка разбора, которой у bash нет, не должна прятать команды («Ошибка дешевле»: ложный отказ
    дешевле пропуска): остаток разбирается заново со следующей строки, а начало строки с ошибкой до её лексемы — с
    командой `:` на следующей строке (оператор `&&`, `|` в конце начала не ошибка); новая ошибка в нём укорачивает
    его до себя, не больше _TREE_PREFIX_TRIES раз. Остаток разбирается заново не больше _TREE_RECOVERIES раз."""
    out = []
    for _ in range(_TREE_RECOVERIES + 1):
        script = shparse.parse(command)
        out.append((command, script))
        error = script.error
        if error is None or not error.fatal:
            return out
        last = script.commands[-1].end if script.commands else 0
        prefix = command[max(last, command.rfind("\n", 0, error.pos) + 1):error.pos]
        for _ in range(_TREE_PREFIX_TRIES):
            if not prefix.strip():
                break
            text = prefix + "\n:"
            head = shparse.parse(text)
            out.append((text, head))
            if head.error is None or not head.error.fatal or head.error.pos >= len(prefix):
                break
            prefix = prefix[:head.error.pos]
        eol = command.find("\n", error.pos)
        if eol < 0:
            return out
        command = command[eol + 1:]
    return out


def _tree_analysis(command):
    """Разбор текста для вопросов детектора: (команды — _TreeCommand по порядку текста, связи «вывод — вход»,
    тела heredoc, которые исполняет оболочка). В вызове (_tree_within) — один раз на текст.

    Связь — (источник: _TreeCommand или None, если вывод неизвестен, приёмник: _TreeCommand): соседние команды
    конвейера (составная команда-источник — None; приёмник-составная — каждая её простая команда вне подстановок),
    `<(…)` в словах и цели `<` команды (источник — единственная простая команда тела), `>(…)` — цель `>` команды
    (источник — она) или слово (источник неизвестен), приёмник — простые команды тела. Тела heredoc — команды, если
    в той же полной команде (элемент Script.commands, тела подстановок — свои) есть оболочка, читающая stdin
    (_heredoc_runs): `bash <<E`, `cat <<E | bash`, как у _parse."""
    cache = _tree_call[1] if _tree_call is not None else None
    if cache is not None and command in cache:
        return cache[command]
    budget = _tree_call[0] if _tree_call is not None else [_BRACE_BUDGET]
    commands, feeds, scripts = [], [], []
    for text, script in _tree_scripts(command):
        simples, docs, pipes = [], [], []
        seen = set()
        context = 0
        stack = [(script, 0)]
        while stack:
            node, ctx = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            kind = type(node)
            if kind is shparse.Script:
                for item in reversed(node.commands):
                    context += 1
                    stack.append((item, context))
                continue
            if kind is shparse.Simple:
                simples.append((node, ctx))
            elif kind is shparse.Heredoc:
                docs.append((node, ctx))
            elif kind is shparse.Pipeline:
                pipes.append(node)
            stack.extend((child, ctx) for child in reversed(node.children()))
        simples.sort(key=lambda item: item[0].start)
        ordered = sorted((doc for doc, _ in docs), key=lambda doc: doc.start)
        found = ([doc.start for doc in ordered], ordered)
        infos = {}
        runs = set()
        for simple, ctx in simples:
            info = _tree_command(simple, text, budget)
            info.docs = found
            infos[id(simple)] = info
            commands.append(info)
            if pkgmanagers._heredoc_runs(info.words):
                runs.add(ctx)
        scripts.extend(_tree_body(doc, text) for doc, ctx in docs if ctx in runs)
        for pipe in pipes:
            for source, target in zip(pipe.commands, pipe.commands[1:]):
                source = infos.get(id(source)) if type(source) is shparse.Simple else None
                feeds.extend((source, infos[id(t)]) for t in _tree_heads(target) if id(t) in infos)
        for simple, _ in simples:
            feeds.extend(_tree_feeds(simple, infos))
    result = (commands, feeds, scripts)
    if cache is not None:
        cache[command] = result
    return result


def _tree_heads(node):
    """Простые команды узла вне тел подстановок: те, что могут читать его stdin."""
    out, stack = [], [node]
    while stack:
        cur = stack.pop()
        kind = type(cur)
        if kind is shparse.Simple:
            out.append(cur)
            continue
        if kind is shparse.Sub or kind is shparse.Heredoc:
            continue
        stack.extend(reversed(cur.children()))
    return out


def _tree_output(sub, infos):
    """Команда, чей вывод — вывод тела подстановки sub: единственная простая команда тела; None — вывод
    неизвестен."""
    commands = sub.body.commands
    if len(commands) == 1 and type(commands[0]) is shparse.Simple:
        return infos.get(id(commands[0]))
    return None


def _tree_feeds(simple, infos):
    """Связи процесс-подстановок простой команды (_tree_analysis)."""
    info = infos[id(simple)]
    out = []
    places = [(w, None) for w in simple.words] + [(r.target, r.op) for r in simple.redirs
                                                  if type(r.target) is shparse.Word]
    for word, op in places:
        for part in word.parts:
            if type(part) is not shparse.Sub or part.kind not in ("<(", ">("):
                continue
            if part.kind == "<(":
                out.append((_tree_output(part, infos), info))
                continue
            source = info if op in _TREE_OUT_REDIRECTS and len(word.parts) == 1 else None
            out.extend((source, infos[id(t)]) for t in _tree_heads(part.body) if id(t) in infos)
    return out


def _tree_command(simple, text, budget):
    """_TreeCommand простой команды дерева: слова — _tree_pairs и _tree_strip."""
    info = _TreeCommand()
    info.simple, info.text = simple, text
    info.segment = text[simple.start:simple.end].strip()
    pairs, braced = _tree_pairs(simple, text, budget)
    info.words, info.marker, info.xargs, info.cut, info.wrapped = _tree_strip(pairs, braced)
    info.herestrings = [_tree_word(r.target, text) for r in simple.redirs
                        if r.op == "<<<" and type(r.target) is shparse.Word]
    info.bodies = [_tree_body(r.target, text) for r in simple.redirs if type(r.target) is shparse.Heredoc]
    return info


def _tree_echo_text(words):
    """_echo_text по словам команды."""
    if not words:
        return None
    name = pkgmanagers._basename(words[0])
    args = words[1:]
    if name == "echo":
        while args and re.fullmatch(r"-[neE]+", args[0]):
            args = args[1:]
        text = " ".join(args)
    elif name == "printf":
        if args[:1] == ["--"]:
            args = args[1:]
        if args[:1] == ["-v"]:
            return None
        text = "\n".join(args)
    else:
        return None
    return text.replace("\\n", "\n").replace("\\t", " ")


def _tree_heredoc_cat(info):
    """_heredoc_cat команды дерева: `cat` без флагов с heredoc, без операндов или с операндом stdin."""
    words = info.words
    return words[:1] == ["cat"] and not any(a.startswith("-") and a != "-" for a in words[1:]) and (
        len(words) == 1 or any(_STDIN_OPERAND.fullmatch(a) for a in words[1:])) and bool(info.bodies)


def _tree_file_output(words):
    """_file_output по словам команды."""
    if not words:
        return False
    name, args = pkgmanagers._basename(words[0]), words[1:]
    if name == "cat":
        return bool(args) and not any(a.startswith("-") or _STDIN_OPERAND.fullmatch(a) for a in args)
    return name == "git" and args[:1] == ["show"] and len(args) > 1 and all(
        ":" in a and not a.startswith("-") for a in args[1:])


def _tree_computed(text):
    """_computed над деревом: имя хотя бы одной команды текста — подстановка или переменная."""
    return any(info.words[0].startswith(("$", "`")) for info in _tree_analysis(text)[0] if info.words)


def _tree_stdin_scripts(feeds):
    """_stdin_scripts над связями дерева: (сегмент для отказа, текст или None). Вывод `cat` с heredoc в оболочку,
    читающую stdin, — тела heredoc."""
    runs = {}
    fed = {id(target) for _, target in feeds}
    for source, target in feeds:
        if id(target) not in runs:
            words = target.words
            runs[id(target)] = not target.marker and bool(words) and (
                pkgmanagers._heredoc_runs(words) or pkgmanagers._basename(words[0]) in pkgmanagers._SOURCES
                and len(words) == 1)
        if not runs[id(target)]:
            continue
        if source is None:
            yield target.segment, None
            continue
        if _tree_heredoc_cat(source) and pkgmanagers._heredoc_runs(target.words):
            for body in source.bodies:
                yield source.segment, body
            continue
        if _tree_file_output(source.words) and id(source) not in fed:
            continue
        text = _tree_echo_text(source.words)
        if text is None or _tree_computed(text):
            yield target.segment, None
        elif text:
            yield source.segment, text


def _tree_inline_script(words):
    """_inline_script с `eval --`: встроенная eval принимает `--` концом флагов (bash 5.3, no_options), строка — слова
    за ним. Старый путь `--` оставлял в строке, а команды строки находил разбор без грамматики."""
    if words[1:2] == ["--"] and pkgmanagers._basename(words[0]) == "eval":
        words = [words[0], *words[2:]]
    return pkgmanagers._inline_script(words)


def _tree_runs(info):
    """_scripts команды дерева: строки, которые её команда исполняет как команды."""
    words = info.words
    out = []
    script = _tree_inline_script(words)
    if script is not None:
        out.append(script)
    if pkgmanagers._heredoc_runs(words):
        out.extend(info.herestrings)
    if words:
        out.extend(_launched(pkgmanagers._basename(words[0]), words[1:]))
    return out


def _tree_find(command, depth):
    """_find над деревом: первый сегмент текста, добавляющий пакет."""
    commands, feeds, scripts = _tree_analysis(command)
    for info in commands:
        if info.segment and _tree_adds(info, depth):
            return info.segment
    if depth < _MAX_DEPTH:
        # Тела heredoc оболочки — раньше входа из связей: _parse разбирал их командами строки.
        for text in scripts:
            found = _tree_find(text, depth + 1)
            if found:
                return found
        for segment, text in _tree_stdin_scripts(feeds):
            if text is not None and _tree_find(text, depth + 1):
                return segment
    return None


def _tree_adds(info, depth):
    """_segment_adds над деревом: команда ставит пакет сама или строкой, которую она исполняет (_tree_runs);
    подстановки в её словах — свои команды дерева."""
    if info.marker:
        return False
    if _is_add(info.words):
        return True
    return depth < _MAX_DEPTH and any(_tree_find(s, depth + 1) for s in _tree_runs(info))


def _tree_name_expands(word):
    """В слове есть подстановка (в том числе в "…" и в операторах `${…}`) или `${…}` с пробелом внутри: вывод или
    значение делится на слова, и первое из них — имя команды. Тела подстановок не обходятся."""
    stack = list(word.parts)
    while stack:
        part = stack.pop()
        kind = type(part)
        if kind is shparse.Sub:
            return True
        if kind is shparse.Param and any(c.isspace() for c in part.text):
            return True
        if kind is shparse.DQ or kind is shparse.Param or kind is shparse.Arith:
            stack.extend(part.parts or ())
    return False


def _tree_name_doubt(info):
    """Имя команды — вывод подстановки или `${…}` с пробелом: bash исполняет вывод командой. Слова всего текста слова
    имени и тел heredoc, открытых в нём (кавычки сняты, `${…`-начала и `}` — пробелы, разделители команд — границы),
    со словами за именем ставят пакет с какого-то места (_is_add, _expanded_install; `$(echo npm) i x`, `${
    $(cat <<E` ⏎ `npm i x` ⏎ `E` ⏎ `); }`) — сомнение. Текст длиннее _WORDS_LIMIT, больше _MAX_LAUNCH_PAIRS мест
    с менеджером или текст сверх остатка _TREE_NAME_BUDGET вызова — сомнение без разбора: проход линеен."""
    words = info.words
    node = getattr(words[0], "node", None) if words else None
    if node is None or not _tree_name_expands(node):
        return False
    starts, docs = info.docs
    lo, hi = bisect.bisect_left(starts, node.start), bisect.bisect_left(starts, node.end)
    texts = [info.text[node.start:node.end]] + [doc.body_text or "" for doc in docs[lo:hi]]
    size = sum(len(t) for t in texts)
    budget = _tree_call[2] if _tree_call is not None else [_TREE_NAME_BUDGET]
    budget[0] -= size
    if size > _WORDS_LIMIT or budget[0] < 0:
        return True
    flat = []
    for text in texts:
        text = _SOUP_QUOTES.sub("", _PARAM_OPEN.sub(" ", text).replace("}", " "))
        for piece in _SOUP_SPLIT.split(text):
            flat.extend(piece.split())
    flat.extend(words[1:])
    pairs = 0
    for k, word in enumerate(flat):
        if not (pkgmanagers._installer(word) or pkgmanagers._expanded(word)
                and flat[k + 1:k + 2] and flat[k + 1] in pkgmanagers._INSTALL_VERBS):
            continue
        pairs += 1
        tail = flat[k:]
        if pairs > _MAX_LAUNCH_PAIRS or _is_add([_VERSION.sub("", tail[0]), *tail[1:]]) or (
                pkgmanagers._expanded_install(tail)):
            return True
    return False


def _tree_computed_script(info):
    """_computed_script над деревом: в строке `eval`, `sh -c` или here-string оболочки имя команды вычисляется."""
    words = info.words
    name = pkgmanagers._basename(words[0]) if words else None
    script = _tree_inline_script(words) if name == "eval" or name in pkgmanagers._C_SHELLS else None
    texts = ([script] if script is not None else []) + (
        info.herestrings if pkgmanagers._heredoc_runs(words) else [])
    return _WHY_COMPUTED if any(_tree_computed(t) for t in texts) else None


def _tree_segment_doubt(info, depth):
    """_segment_doubt над деревом; имя команды — вывод подстановки (_tree_name_doubt) — _WHY_NAME на месте, где
    _doubt проверяет имя."""
    why = None
    if not info.marker:
        why = _doubt(info.words, info.xargs, info.cut, info.wrapped)
        if why in (None, _WHY_SUBCOMMAND, _WHY_LAUNCHER) and _tree_name_doubt(info):
            why = _WHY_NAME
        why = why or _tree_computed_script(info)
    if why:
        return why
    nested = [] if info.marker else _tree_runs(info)
    if depth >= _MAX_DEPTH:
        return _WHY_DEPTH if any(_soup_add(t) for t in nested) else None
    for text in nested:
        found = _tree_find_doubt(text, depth + 1)
        if found:
            return found[1]
    return None


def _tree_find_doubt(command, depth):
    """_find_doubt над деревом: первый сегмент с сомнением и довод."""
    commands, feeds, scripts = _tree_analysis(command)
    for info in commands:
        why = _tree_segment_doubt(info, depth)
        if why:
            return info.segment, why
    for segment, text in chain(((t.strip(), t) for t in scripts), _tree_stdin_scripts(feeds)):
        if text is None:
            return segment, _WHY_COMPUTED
        if depth >= _MAX_DEPTH:
            if _soup_add(text):
                return segment, _WHY_DEPTH
            continue
        found = _tree_find_doubt(text, depth + 1)
        if found:
            return segment, found[1]
    return None


def _tree_doubt(command):
    """dependency_doubt над деревом shparse."""
    if not isinstance(command, str):
        return None
    return _tree_within(_tree_find_doubt, command)


def _tree_add(command):
    """dependency_add над деревом shparse."""
    if not isinstance(command, str):
        return None
    return _tree_within(_tree_find, command)
