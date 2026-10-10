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
# исполняет слово как имя команды (`'A=1' npm i x` — команда `A=1`); проверяется по началу слова (_word_lead): за
# обёрткой (`sudo A=1 npm i x`) и в строке `env -S`. Присваивания перед именем команды узнаёт разбор (_pairs).
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
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
# Оператор перенаправления в начале слова: `2>&1`, `>log`, `&>/dev/null`, `<<EOF` — пары перенаправлений _split
# (их зовёт manifest_watch).
_REDIRECT = re.compile(r"^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:&>>|&>|>>|>&|>\||>|<<<|<<-|<<|<&|<>|<)")
# Глубина разбора вложенных строк команд: `sh -c`, `eval`, вход оболочки, тела heredoc оболочки, команды
# _launched (подстановки — команды того же дерева).
_MAX_DEPTH = 4
# Обёрток, снимаемых с одного сегмента (_strip_command); слова за последней остаются как есть.
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
# Сколько первых символов слов команды видит разбор (_strip_command): пакет, названный дальше, не виден — команду
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
# Разбор не дал дерева: вложенность глубже предела shparse (_STACK_MAX уровней стека разбора) или сбой разбора или
# детектора на этом тексте. Добавляет ли команда пакет, неизвестно, а пропуск открыл бы обход проверки той же
# установкой рядом с такой конструкцией (`npm i x; echo $(… ×5000 …)`): сомнение, маркер — в начале всей команды.
_WHY_NESTING = (f"вложенность конструкций глубже предела разбора ({shparse._STACK_MAX} уровней стека разбора): "
                "команды не разобраны; маркер согласия — в начале всей команды")
_WHY_UNPARSED = ("разбор команды не удался (внутренняя ошибка детектора): команды не разобраны; маркер согласия — в "
                 "начале всей команды")
# Сколько знаков команды показывает сомнение без разбора.
_UNPARSED_SHOWN = 200
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


# Сколько символов слов (и шагов) создаёт раскрытие фигурных скобок всех слов одного вызова dependency_add или
# dependency_doubt — всех его сегментов и вложенных строк (_pairs); слова с места, где предел кончился, не видны:
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
    слова раскрытия выброшены. budget — список из одного числа, остаток предела _BRACE_BUDGET (_pairs): каждое
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


# Операнд `cat`, который читает stdin: `-`, `/dev/stdin`, `/dev/fd/N`, `/proc/<процесс>/fd/N`.
_STDIN_OPERAND = re.compile(r"-|/dev/stdin|/dev/fd/\d+|/proc/[^/]+/fd/\d+")


# Переменная с абсолютным путём в начале слова (`$HOME/x`, `${PWD}`): пустая даёт `/…`, тоже абсолютный путь.
_ABSOLUTE_VAR = re.compile(r"^\$(?:HOME|PWD|OLDPWD|TMPDIR|\{(?:HOME|PWD|OLDPWD|TMPDIR)\})(?=/|$)")
# Флаги `find`, за которыми до `;` или `+` идёт команда.
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}


def _find_runs(args):
    """Команды `find -exec`/`-execdir`/`-ok`/`-okdir` строками. `{}` в них — найденный путь: у `-exec` с начальной
    точкой поиска впереди (`find src` — `src/…`; из нескольких — первая, что не начинается с `.`, `/`, `~`: npm
    читает `src/a` как репозиторий GitHub; `$HOME`, `$PWD`, `$OLDPWD`, `$TMPDIR` — абсолютные, _ABSOLUTE_VAR), у
    `-execdir` — `./…`; путь — локальный, не пакет. Слова — аргументы команды, не строка оболочки: запись
    pkgmanagers._shell_join, раскрытие в `sh -c` за `-exec` видит повторный разбор."""
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
            out.append(pkgmanagers._shell_join(run))
            run = None
        else:
            run.append(pkgmanagers._replaced(a, "{}", place))
    if run:
        out.append(pkgmanagers._shell_join(run))
    return out


def _launched(name, args):
    """Команды, которые name запускает из своих аргументов args, строками для разбора."""
    if name == "git":
        return pkgmanagers._git_runs(args)
    if name == "ssh":
        # Удалённая оболочка разбирает слова, склеенные пробелом: раскрытие в любом из них — текст строки.
        command = pkgmanagers._ssh_command(args)
        return [pkgmanagers._joined(command, " ".join(command))] if command else []
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
                return [pkgmanagers._joined(args[k:], " ".join([a[2:], *args[k + 1:]]).strip())]
        return []
    if name in ("pwsh", "powershell"):
        for k, a in enumerate(args):
            flag = a.lower().lstrip("-/")
            if a[:1] in "-/" and flag and "command".startswith(flag) and (flag == "c" or len(flag) >= 3):
                return [pkgmanagers._joined(args[k + 1:], " ".join(args[k + 1:]))]
        return []
    if name == "nix" and args[:1] in (["develop"], ["shell"]):
        for k, a in enumerate(args):
            if a in ("-c", "--command"):
                return [pkgmanagers._shell_join(args[k + 1:])]
        return []
    if name == "nix-shell":
        return [args[k + 1] for k, a in enumerate(args[:-1]) if a in ("--run", "--command")]
    if name in ("mise", "rtx") and args[:1] in (["exec"], ["x"]) and "--" in args:
        return [pkgmanagers._shell_join(args[args.index("--") + 1:])]
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


# ---------------------------------------------------------------------------------------------------------------
# Разбор текста команды: его читает shparse.parse (грамматика bash 5.3), вопросы детектора задаются над деревом.
#
# Сегмент — простая команда дерева (shparse.Simple, её текст без пробелов по краям). Подстановки, тела функций,
# арифметика и тела heredoc уже лежат в дереве: их простые команды проверяются на той же глубине, что и команда
# вокруг. Глубина _MAX_DEPTH считает только тексты, которые разбираются заново: строки `sh -c`/`eval`, вход
# оболочки (`echo … | bash`, `bash <(…)`), тела heredoc оболочки без скрипта, команды _launched.

# Раскрытие (параметр, подстановка, арифметика) длиннее _EXPANSION_MAX символов в слове — заглушка
# _PLACEHOLDER: его вывод неизвестен до исполнения, а текст в слове повторял бы вложенный текст на каждом уровне
# вложенности. Имя команды с раскрытием проверяет по узлу _name_doubt.
_PLACEHOLDER = "$_"
_EXPANSION_MAX = 256
# Сколько символов текста имён команд с подстановкой (_name_doubt) разбирает один вызов dependency_doubt; дальше —
# сомнение без разбора: проход линеен.
_NAME_BUDGET = 4 * _WORDS_LIMIT
# Операторы перенаправления вывода: `>(…)` их целью получает вывод команды.
_OUT_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


class _Word(str):
    """Слово команды из дерева; node — его shparse.Word (имя команды с подстановкой — _name_doubt); expands — текст
    слова несёт значение внешнего раскрытия (_expands): строку `sh -c`, `eval`, `<<<`, тело heredoc оболочки, вход
    оболочки и строку запускателя с ним оболочка разбирает заново (_computed_script, _stdin_scripts, _find_doubt);
    shell — запись слова для повторного разбора, где раскрытия остаются раскрытиями (_shell_text; None — в слове нет
    раскрытий или оно получено из другого слова, pkgmanagers._shell_join)."""
    node = None
    expands = False
    shell = None


# Параметры, чьё значение — число или пусто: `$$`, `$?`, `$#`, `$!`.
_NUMERIC_PARAM = re.compile(r"\$(?:[$?#!]|\{[$?#!]\})")


def _expands(parts, text=None):
    """В частях слова вне `'…'` и `$'…'` (в том числе в "…") есть раскрытие, чьё значение — произвольный текст:
    параметр, подстановка `$(…)`, `` `…` ``, `${ …; }`, а при text (разобранный текст слова команды; here-string и
    тело heredoc bash шаблоном имён не раскрывает) — и шаблон имён в литерале без кавычек (_globs: имя файла
    становится текстом; bash 5.3: `touch 'touch Q13'; bash -c *` создаёт Q13). Арифметика и _NUMERIC_PARAM дают
    число (`bash -c "echo $((n+1)) $$"` печатает числа), процесс-подстановка — путь `/dev/fd/N`, тильда — путь
    домашнего каталога: команд в текст они не вносят."""
    stack = [(p, text) for p in parts]
    while stack:
        part, src = stack.pop()
        kind = type(part)
        if kind is shparse.Lit:
            if src is not None and _globs(src, part):
                return True
        elif kind is shparse.DQ:
            stack.extend((p, None) for p in part.parts)
        elif kind is shparse.Param:
            if not _NUMERIC_PARAM.fullmatch(part.text):
                return True
        elif kind is shparse.Sub:
            if part.kind not in ("<(", ">("):
                return True
    return False


class _Segment:
    """Простая команда дерева для вопросов детектора: text — разобранный текст, segment — её текст без пробелов по
    краям; words, marker, xargs, cut, wrapped — слова команды и признаки _strip_command; herestrings — строки `<<<`;
    bodies — тела её heredoc (_heredoc_text); docs — heredoc текста: (начала их слов терминатора по возрастанию,
    узлы)."""
    __slots__ = ("simple", "text", "segment", "words", "marker", "xargs", "cut", "wrapped", "herestrings", "bodies",
                 "docs")


def _glob_marks(text, part):
    """Символы литерала без кавычек part по его исходному тексту в text парами (символ, шаблон ли имён): `*`, `?`,
    `[`, `]` без `\\` перед ними (`\\` перед литералом shparse в его начало не включает: `\\*` — литерал `*` за
    `\\`)."""
    start, end = part.start, part.end
    j = start
    # Обратный проход по `\` — только перед символом шаблона в начале: такие ряды `\` не пересекаются, проход линеен.
    while start < end and text[start] in "*?[]" and j > 0 and text[j - 1] == "\\":
        j -= 1
    escaped = (start - j) % 2 == 1
    out, i = [], start
    while i < end:
        c = text[i]
        if c == "\\" and not escaped:
            escaped = True
            i += 1
            continue
        out.append((c, not escaped and c in "*?[]"))
        escaped = False
        i += 1
    return out


def _globs(text, part):
    """В литерале без кавычек part есть шаблон имён (_glob_marks): `*`, `?` или `[` с `]` за ним."""
    bracket = False
    for c, glob in _glob_marks(text, part):
        if glob and (c in "*?" or c == "]" and bracket):
            return True
        bracket = bracket or glob and c == "["
    return False


def _shell_text(parts, text):
    """Запись слова из частей parts для повторного разбора: литерал и `'…'`, `$'…'` — в кавычках shlex (символы
    шаблона имён литерала — без кавычек, _glob_marks), "…" — в двойных кавычках со `\\` перед `\\`, `"`, `$`, `` ` ``
    литералов, раскрытия — своим текстом (_part_text): повторный разбор видит те же раскрытия (_expands)."""
    out = []
    for part in parts:
        kind = type(part)
        if kind is shparse.Lit:
            if _globs(text, part):
                out.extend(c if glob else shlex.quote(c) for c, glob in _glob_marks(text, part))
            else:
                out.append(shlex.quote(part.text))
        elif kind is shparse.SQ or kind is shparse.AnsiC:
            out.append(shlex.quote(_part_text(part, text)))
        elif kind is shparse.DQ:
            inner = (_DQ_SPECIAL.sub(r"\\\g<0>", p.text) if type(p) is shparse.Lit else _part_text(p, text)
                     for p in part.parts)
            out.append('"' + "".join(inner) + '"')
        else:
            out.append(_part_text(part, text))
    return "".join(out)


# Символы, которые в "…" снимает `\`.
_DQ_SPECIAL = re.compile(r'[\\"$`]')


def _part_text(part, text):
    """Текст части слова дерева: кавычки и `\\` сняты, `$'…'` раскрыта, раскрытие (параметр, подстановка,
    арифметика) — своим текстом в text, с кавычками (длиннее _EXPANSION_MAX — _PLACEHOLDER)."""
    kind = type(part)
    if kind is shparse.Lit or kind is shparse.SQ:
        return part.text
    if kind is shparse.AnsiC:
        # Символ с кодом 0 обрывает строку `$'…'` (bash 5.3: `touch $'P1\0x'y` создаёт P1y).
        return part.value.partition("\0")[0]
    if kind is shparse.DQ:
        # В "…" нет "…" и $'…': глубина — один уровень.
        return "".join(_part_text(p, text) for p in part.parts)
    if part.end - part.start > _EXPANSION_MAX:
        return _PLACEHOLDER
    return text[part.start:part.end]


def _word_text(word, text):
    """Текст слова дерева (_part_text) с узлом слова (_Word)."""
    out = _Word("".join(_part_text(p, text) for p in word.parts))
    out.node = word
    out.expands = _expands(word.parts, text)
    if out.expands:
        out.shell = _shell_text(word.parts, text)
    return out


def _word_lead(word, text):
    """Начало слова до первой кавычки или `\\`: по нему узнаются присваивание за обёрткой и ключевое слово
    (`'A=1' npm i x` — команда `A=1`, `\\if` — не ключевое слово). Литерал с `\\` (текст в text отличается от
    значения) начало кончает."""
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
            out.append(_part_text(part, text))
    return "".join(out)


def _brace_marks(word, text):
    """Символы слова дерева парами (символ, раскрывается ли) для _brace_words: раскрываются `{`, `,`, `}` литералов
    вне кавычек без `\\` перед ними; None — в литералах слова нет `{`. Литерал, чей текст в text после снятия `\\` не
    совпадает со значением (тело `` `…` `` с позициями из другой строки), раскрывается целиком: лишнее слово
    раскрытия дешевле пропуска."""
    if not any(type(p) is shparse.Lit and "{" in p.text for p in word.parts):
        return None
    out = []
    for part in word.parts:
        if type(part) is not shparse.Lit:
            out.extend((c, False) for c in _part_text(part, text))
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


def _procsub(word):
    """Слово — одна процесс-подстановка `<(…)` или `>(…)`: её Sub или None."""
    parts = word.parts
    if len(parts) == 1 and type(parts[0]) is shparse.Sub and parts[0].kind in ("<(", ">("):
        return parts[0]
    return None


def _pairs(simple, text, budget, braces=True):
    """Пары (слово, начало) простой команды дерева: ведущие присваивания, затем слова (_word_text, _word_lead);
    перенаправления не входят, слово из одной процесс-подстановки — тоже (её вход и выход — связи _procsub_feeds).
    Начало присваивания — "_=" (присваивание при любом индексе: его узнал разбор, bash 5.3: `a["]"]=1`,
    `a[b[1]]=1`), у маркера согласия без кавычек и `\\` — сам маркер. При braces фигурные скобки слов вне
    присваиваний раскрываются (_brace_marks, _brace_words), как в словах команды bash, с общим пределом budget (None
    — свой полный предел); у слов раскрытия начало пустое: они не присваивания и не ключевые слова. Возвращает (пары,
    кончился ли предел раскрытия): если кончился, пары — только до места, где он кончился."""
    out = []
    budget = [_BRACE_BUDGET] if budget is None else budget
    for word in simple.assigns:
        src = text[word.start:word.end]
        out.append((_word_text(word, text), DEP_OK_MARKER if src == DEP_OK_MARKER else "_="))
    for word in simple.words:
        if _procsub(word) is not None:
            continue
        pairs, cut = _word_pairs(word, text, budget, braces)
        out.extend(pairs)
        if cut:
            return out, True
    return out, False


def _word_pairs(word, text, budget, braces):
    """Пары (слово, начало) одного слова команды (_pairs) и кончился ли на нём предел раскрытия budget."""
    value, lead = _word_text(word, text), _word_lead(word, text)
    marks = _brace_marks(word, text) if braces and not _ENV_ASSIGN.match(lead) else None
    if marks:
        words, cut = _brace_words(marks, budget)
        if cut or words != [value]:
            return [(_derived(value, w), "") for w in words], cut
    return [(value, lead)], False


def _derived(word, text):
    """Слово text, полученное из слова word (раскрытие скобок): признак expands — от word, узла нет."""
    out = _Word(text)
    out.expands = getattr(word, "expands", False)
    return out


def _env_split(value):
    """Пары (слово, начало) первой простой команды текста value (строка `env -S`), без раскрытия скобок."""
    simples = shparse.simple_commands(shparse.parse(value))
    return _pairs(simples[0], value, None, braces=False)[0] if simples else []


def _strip_command(pairs, braced):
    """Слова команды по парам дерева (_pairs; braced — кончился ли предел раскрытия скобок), стоит ли маркер
    согласия среди её ведущих присваиваний, снята ли обёртка `xargs`, довод, если слова команды не видны целиком —
    _WHY_BRACES для слов за пределом раскрытия фигурных скобок, _WHY_CUT для слов-не-флагов, начатых за её первыми
    _WORDS_LIMIT символами, иначе None, — и стоит ли за _MAX_WRAPPERS снятыми обёртками ещё обёртка.

    Ведущие присваивания (и элементу массива `a[0]=1`), ключевые слова shell, `function` и `coproc` с именем и до
    _MAX_WRAPPERS обёрток (`sudo`, `env`, `timeout`, `nice`, `xargs`, `stdbuf` и др.) с их флагами сняты; строка
    `env -S` разбита на слова (_env_split), `su`/`runuser`/`flock` с `-c` и `watch` без `-x` — команда `sh -c`,
    `su`/`runuser` без `-c` и `-u` — оболочка (_c_wrapper). Предел _WORDS_LIMIT считается от первого слова команды:
    длинные присваивания и обёртки перед ней его не прячут."""
    # Очередь: снятие слова спереди и вставка строки `env -S` — без копирования хвоста, проход линеен.
    words = deque(pairs)
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
                    words.extendleft(reversed(_env_split(value)))
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
                script = _Word(" ".join(word for word, _ in words))
                script.expands = any(getattr(word, "expands", False) for word, _ in words)
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


def _heredoc_text(doc, text):
    """Текст тела heredoc, который получает команда (_Word): у терминатора в кавычках — тело как есть, иначе — части
    тела, раскрытия своим текстом (_part_text): вывод подстановки неизвестен, а `\\$(…)` тела — уже `$(…)`;
    expands — в теле без кавычек у терминатора есть внешнее раскрытие (_expands)."""
    if doc.quoted:
        return _Word(doc.body_text or "")
    out = _Word("".join(_part_text(p, text) for p in doc.parts))
    out.expands = _expands(doc.parts)
    return out


# Текущий вызов dependency_add или dependency_doubt: (остаток предела раскрытия _BRACE_BUDGET — список из одного числа,
# разборы текстов _analysis по тексту, остаток _NAME_BUDGET — список из одного числа) или None вне
# вызова.
_call = None


def _within_call(find, command):
    """find(command, 0) с одним пределом раскрытия _BRACE_BUDGET на весь вызов: текст, прочитанный несколько раз,
    разбирается и тратит предел один раз (_analysis)."""
    global _call
    outer = _call
    _call = ([_BRACE_BUDGET], {}, [_NAME_BUDGET])
    try:
        return find(command, 0)
    finally:
        _call = outer


# Сколько раз начало строки с фатальной ошибкой укорачивается до новой ошибки и сколько раз остаток текста за
# фатальной ошибкой разбирается заново (_parses): bash за первой фатальной ошибкой не исполняет ничего, проверка
# остатка — запас на ошибку разбора, а каждый повторный разбор держит свою копию остатка.
_PREFIX_TRIES = 4
_RECOVERIES = 16


class _Unparsed(Exception):
    """Разбор текста не дал дерева (shparse.DEPTH, shparse.INTERNAL): why — довод сомнения."""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


def _parse(text):
    """shparse.parse(text); разбор без дерева — _Unparsed."""
    script = shparse.parse(text)
    kind = script.error.kind if script.error is not None else None
    if kind is not None:
        raise _Unparsed(_WHY_NESTING if kind == shparse.DEPTH else _WHY_UNPARSED)
    return script


def _parses(command):
    """Разборы текста (текст, Script). После фатальной синтаксической ошибки bash не исполняет ни её команды, ни
    остаток, но ошибка разбора, которой у bash нет, не должна прятать команды («Ошибка дешевле»: ложный отказ
    дешевле пропуска): остаток разбирается заново со следующей строки, а начало строки с ошибкой до её лексемы — с
    командой `:` на следующей строке (оператор `&&`, `|` в конце начала не ошибка); новая ошибка в нём укорачивает
    его до себя, не больше _PREFIX_TRIES раз. Ошибка на конце начала (конструкция, не закрытая до конца ввода:
    `npm i x; echo $(a`) и ошибка скобок массива, отбросившая строку начала, укорачивают его до начала команды
    верхнего уровня, где ошибка (SyntaxIssue.open).
    Остаток разбирается заново не больше _RECOVERIES раз. Разбор без дерева — _Unparsed."""
    out = []
    for _ in range(_RECOVERIES + 1):
        script = _parse(command)
        out.append((command, script))
        error = script.error
        if error is None or not error.fatal:
            return out
        last = script.commands[-1].end if script.commands else 0
        begin = max(last, command.rfind("\n", 0, error.pos) + 1)
        prefix = command[begin:error.pos]
        for _ in range(_PREFIX_TRIES):
            if not prefix.strip():
                break
            text = prefix + "\n:"
            head = _parse(text)
            out.append((text, head))
            if head.error is None:
                break
            cut = head.error.pos
            if not head.error.fatal or cut >= len(prefix):
                # Ошибка скобок массива отбросила строку начала, или конструкция не закрыта до его конца: начало —
                # до команды верхнего уровня, где ошибка (SyntaxIssue.open).
                cut = head.error.open
                if cut is None or cut >= len(prefix):
                    break
            prefix = prefix[:cut]
        eol = command.find("\n", error.pos)
        if eol < 0:
            return out
        command = command[eol + 1:]
    return out


def _analysis(command):
    """Разбор текста для вопросов детектора: (команды — _Segment по порядку текста, связи «вывод — вход»,
    тела heredoc, которые исполняет оболочка). В вызове (_within_call) — один раз на текст.

    Связь — (источник: _Segment или None, если вывод неизвестен, приёмник: _Segment): соседние команды
    конвейера (составная команда-источник — None; приёмник-составная — каждая её простая команда вне подстановок),
    `<(…)` в словах и цели `<` команды (источник — единственная простая команда тела), `>(…)` — цель `>` команды
    (источник — она) или слово (источник неизвестен), приёмник — простые команды тела. Тела heredoc — команды, если
    в той же полной команде (элемент Script.commands, тела подстановок — свои) есть оболочка без маркера, читающая
    stdin (_heredoc_runs): `bash <<E`, `cat <<E | bash`, `cat <<'EOF' |` ⏎ тело ⏎ `EOF` ⏎ `bash`."""
    cache = _call[1] if _call is not None else None
    if cache is not None and command in cache:
        return cache[command]
    budget = _call[0] if _call is not None else [_BRACE_BUDGET]
    commands, feeds, scripts = [], [], []
    for text, script in _parses(command):
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
            info = _command_info(simple, text, budget)
            info.docs = found
            infos[id(simple)] = info
            commands.append(info)
            # Маркер у оболочки покрывает её тело heredoc, как строку `-c` и вход по конвейеру.
            if pkgmanagers._heredoc_runs(info.words) and not info.marker:
                runs.add(ctx)
        scripts.extend(_heredoc_text(doc, text) for doc, ctx in docs if ctx in runs)
        for pipe in pipes:
            for source, target in zip(pipe.commands, pipe.commands[1:]):
                source = infos.get(id(source)) if type(source) is shparse.Simple else None
                feeds.extend((source, infos[id(t)]) for t in _stdin_heads(target) if id(t) in infos)
        for simple, _ in simples:
            feeds.extend(_procsub_feeds(simple, infos))
    result = (commands, feeds, scripts)
    if cache is not None:
        cache[command] = result
    return result


def _stdin_heads(node):
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


def _sub_output(sub, infos):
    """Команда, чей вывод — вывод тела подстановки sub: единственная простая команда тела; None — вывод
    неизвестен."""
    commands = sub.body.commands
    if len(commands) == 1 and type(commands[0]) is shparse.Simple:
        return infos.get(id(commands[0]))
    return None


def _procsub_feeds(simple, infos):
    """Связи процесс-подстановок простой команды (_analysis)."""
    info = infos[id(simple)]
    out = []
    places = [(w, None) for w in simple.words] + [(r.target, r.op) for r in simple.redirs
                                                  if type(r.target) is shparse.Word]
    for word, op in places:
        for part in word.parts:
            if type(part) is not shparse.Sub or part.kind not in ("<(", ">("):
                continue
            if part.kind == "<(":
                out.append((_sub_output(part, infos), info))
                continue
            source = info if op in _OUT_REDIRECTS and len(word.parts) == 1 else None
            out.extend((source, infos[id(t)]) for t in _stdin_heads(part.body) if id(t) in infos)
    return out


def _command_info(simple, text, budget):
    """_Segment простой команды дерева simple в разобранном тексте text: слова — _pairs с пределом раскрытия budget
    и _strip_command."""
    info = _Segment()
    info.simple, info.text = simple, text
    info.segment = text[simple.start:simple.end].strip()
    pairs, braced = _pairs(simple, text, budget)
    info.words, info.marker, info.xargs, info.cut, info.wrapped = _strip_command(pairs, braced)
    info.herestrings = []
    for r in simple.redirs:
        if r.op == "<<<" and type(r.target) is shparse.Word:
            # Here-string bash шаблоном имён не раскрывает.
            word = _word_text(r.target, text)
            word.expands = _expands(r.target.parts)
            info.herestrings.append(word)
    info.bodies = [_heredoc_text(r.target, text) for r in simple.redirs if type(r.target) is shparse.Heredoc]
    return info


def _echo_text(words):
    """Вывод команды `echo …` или `printf …` по её словам; None — вывод неизвестен (не echo/printf, `printf -v`).
    `\\n` — перевод строки, у printf каждый аргумент — с новой строки: лишний перевод строки команд не создаёт.
    Слово с раскрытием несёт его текст: вычисляемое имя в выводе ловит _computed."""
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


def _heredoc_cat(info):
    """Команда — `cat` без флагов с heredoc, без операндов или с операндом stdin (_STDIN_OPERAND): её вывод — тело
    heredoc и файлы."""
    words = info.words
    return words[:1] == ["cat"] and not any(a.startswith("-") and a != "-" for a in words[1:]) and (
        len(words) == 1 or any(_STDIN_OPERAND.fullmatch(a) for a in words[1:])) and bool(info.bodies)


def _file_output(words):
    """Вывод команды — содержимое названных файлов: `cat` с файлами без флагов и операндов stdin (_STDIN_OPERAND),
    `git show <ревизия>:<путь>`. Скрипт из файла детектор не видит, как у `bash файл`."""
    if not words:
        return False
    name, args = pkgmanagers._basename(words[0]), words[1:]
    if name == "cat":
        return bool(args) and not any(a.startswith("-") or _STDIN_OPERAND.fullmatch(a) for a in args)
    return name == "git" and args[:1] == ["show"] and len(args) > 1 and all(
        ":" in a and not a.startswith("-") for a in args[1:])


def _computed(text):
    """Имя хотя бы одной команды текста вычисляется при исполнении: первое слово команды — подстановка или
    переменная (`$X`, `"$X"`, `` `…` ``, `$(…)`). Кавычки ANSI-C `$'…'` — не подстановка: они раскрыты."""
    return any(info.words[0].startswith(("$", "`")) for info in _analysis(text)[0] if info.words)


def _stdin_scripts(feeds):
    """Текст, который команда исполняет со stdin, по связям feeds (_analysis): (сегмент для отказа, текст). Вывод
    `echo`/`printf` в оболочку без скрипта (`echo 'npm i x' | bash`, `bash <(echo …)`) или в `source` и `.` без
    скрипта (`source <(…)`) либо со скриптом `-`, `/dev/stdin`, если приёмник без маркера; вывод `cat` с heredoc
    (_heredoc_cat) в оболочку, читающую stdin, — тела heredoc. Вход такого приёмника не из литерального
    `echo`/`printf` или из его слова с внешним раскрытием (`curl … | bash`, `echo $X | bash`, `echo "$t" | bash`) —
    (сегмент приёмника, None), тело heredoc `cat` с внешним раскрытием — (сегмент `cat`, None): текст неизвестен до
    исполнения;
    кроме файлов (_file_output) без входа из другой команды: скрипт из файла не виден, как у `bash файл`."""
    # Приёмник с многими источниками (`bash <(…) <(…)`) разбирается один раз.
    runs = {}
    # Команды со входом из другой команды: у `cat <(curl …)` файл — вывод программы.
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
        if _heredoc_cat(source) and pkgmanagers._heredoc_runs(target.words):
            for body in source.bodies:
                yield source.segment, None if body.expands else body
            continue
        if _file_output(source.words) and id(source) not in fed:
            continue
        text = _echo_text(source.words)
        # Слово `echo`/`printf` с внешним раскрытием: его значение — текст для оболочки (`echo "$t" | bash`).
        if text is None or _computed(text) or any(getattr(w, "expands", False) for w in source.words[1:]):
            yield target.segment, None
        elif text:
            yield source.segment, text


def _scripts(info):
    """Строки, которые команда исполняет как команды: `sh -c '…'`, `eval …` (_inline_script), here-string оболочки
    без скрипта или `source /dev/stdin` (`bash <<< '…'`, _heredoc_runs), удалённая команда ssh, `trap '…'`,
    `find -exec …`, `cmd /c`, `pwsh -Command`, `nix develop -c`, `nix-shell --run`, `mise exec --`, команды git
    (_launched)."""
    words = info.words
    out = []
    script = pkgmanagers._inline_script(words)
    if script is not None:
        out.append(script)
    if pkgmanagers._heredoc_runs(words):
        out.extend(info.herestrings)
    if words:
        out.extend(_launched(pkgmanagers._basename(words[0]), words[1:]))
    return out


def _find(command, depth):
    """Первый сегмент текста, добавляющий пакет; вложенные строки (_scripts, тела heredoc оболочки, вход оболочки
    _stdin_scripts) разбираются до _MAX_DEPTH."""
    commands, feeds, scripts = _analysis(command)
    for info in commands:
        if info.segment and _segment_adds(info, depth):
            return info.segment
    if depth < _MAX_DEPTH:
        # Тела heredoc оболочки — раньше входа из связей.
        for text in scripts:
            found = _find(text, depth + 1)
            if found:
                return found
        for segment, text in _stdin_scripts(feeds):
            if text is not None and _find(text, depth + 1):
                return segment
    return None


def _segment_adds(info, depth):
    """Команда ставит пакет сама или строкой, которую она исполняет (_scripts); подстановки в её словах — свои
    команды дерева. Маркер команды покрывает её строки _scripts, но не подстановки: они исполняются до команды."""
    if info.marker:
        return False
    if _is_add(info.words):
        return True
    return depth < _MAX_DEPTH and any(_find(s, depth + 1) for s in _scripts(info))


def _name_expands(word):
    """В слове есть подстановка (в том числе в "…" и в операторах `${…}`) или `${…}` с пробелом внутри: вывод или
    значение делится на слова, и первое из них — имя команды. Тела подстановок не обходятся. Пробел ищется только в
    тексте внешнего `${…}`: текст вложенного — часть его текста, и перечитывать его на каждом уровне вложенности
    значило бы квадратичное время."""
    stack = [(part, False) for part in word.parts]
    while stack:
        part, inner = stack.pop()
        kind = type(part)
        if kind is shparse.Sub:
            return True
        if kind is shparse.Param and not inner and any(c.isspace() for c in part.text):
            return True
        if kind is shparse.DQ or kind is shparse.Param or kind is shparse.Arith:
            inner = inner or kind is shparse.Param
            stack.extend((child, inner) for child in part.parts or ())
    return False


def _name_doubt(info):
    """Имя команды — вывод подстановки или `${…}` с пробелом: bash исполняет вывод командой. Слова всего текста слова
    имени и тел heredoc, открытых в нём (кавычки сняты, `${…`-начала и `}` — пробелы, разделители команд — границы),
    со словами за именем ставят пакет с какого-то места (_is_add, _expanded_install; `$(echo npm) i x`, `${
    $(cat <<E` ⏎ `npm i x` ⏎ `E` ⏎ `); }`) — сомнение. Текст длиннее _WORDS_LIMIT, больше _MAX_LAUNCH_PAIRS мест
    с менеджером или текст сверх остатка _NAME_BUDGET вызова — сомнение без разбора: проход линеен."""
    words = info.words
    node = getattr(words[0], "node", None) if words else None
    if node is None or not _name_expands(node):
        return False
    starts, docs = info.docs
    lo, hi = bisect.bisect_left(starts, node.start), bisect.bisect_left(starts, node.end)
    texts = [info.text[node.start:node.end]] + [doc.body_text or "" for doc in docs[lo:hi]]
    size = sum(len(t) for t in texts)
    budget = _call[2] if _call is not None else [_NAME_BUDGET]
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


def _computed_script(info):
    """Довод сомнения, если строка, которую команда исполняет оболочкой (`eval`, `sh -c`, here-string оболочки,
    строка запускателя _launched), — вычисляемая: в её слове (у `eval` — в любом слове строки) внешнее раскрытие
    (expands, _expands; `bash -c "echo $t"`, `eval "x=( $t )"`, `bash <<< "cd $D"`, `ssh host "make $X"`: внешняя
    оболочка вставляет значение в текст, внутренняя разбирает его заново, bash 5.3: `t='a $(touch P1)'; bash -c "x=(
    $t )"` создаёт P1) или имя команды строки вычисляется (_computed: `bash -c '$X'`); None — такой строки нет.
    Строка, которая пакета не ставит (`sh -c "$(jq -r .cmd settings.json)"`, `bash -c "cd $D && make"`), — ложный
    отказ, обход — маркер."""
    words = info.words
    name = pkgmanagers._basename(words[0]) if words else None
    script = pkgmanagers._inline_script(words) if name == "eval" or name in pkgmanagers._C_SHELLS else None
    herestrings = info.herestrings if pkgmanagers._heredoc_runs(words) else []
    texts = ([script] if script is not None else []) + herestrings
    sources = (words[1:] if name == "eval" else [script] if script is not None else []) + herestrings
    # Строки запускателей, которые разбирает оболочка (`ssh host "…"`, `trap "…"`, `git -c alias.x='!…'`), несут
    # expands своих слов; строки из слов-аргументов (`find -exec`) — запись _shell_join, их проверяет разбор.
    if words:
        sources = sources + _launched(name, words[1:])
    if any(getattr(w, "expands", False) for w in sources) or any(_computed(t) for t in texts):
        return _WHY_COMPUTED
    return None


def _segment_doubt(info, depth):
    """Довод сомнения команды: её слова (_doubt; имя команды — вывод подстановки, _name_doubt, — _WHY_NAME на месте,
    где _doubt проверяет имя), вычисляемая строка `eval` или here-string (_computed_script), строки _scripts — как в
    _segment_adds; на глубине _MAX_DEPTH вложенный текст проверяет _soup_add."""
    why = None
    if not info.marker:
        why = _doubt(info.words, info.xargs, info.cut, info.wrapped)
        if why in (None, _WHY_SUBCOMMAND, _WHY_LAUNCHER) and _name_doubt(info):
            why = _WHY_NAME
        why = why or _computed_script(info)
    if why:
        return why
    nested = [] if info.marker else _scripts(info)
    if depth >= _MAX_DEPTH:
        return _WHY_DEPTH if any(_soup_add(t) for t in nested) else None
    for text in nested:
        found = _find_doubt(text, depth + 1)
        if found:
            return found[1]
    return None


def _find_doubt(command, depth):
    """Первый сегмент текста с сомнением и довод (_segment_doubt, затем тела heredoc оболочки — с внешним раскрытием
    _WHY_COMPUTED — и _stdin_scripts: вход оболочки не из литерального `echo`/`printf`, heredoc `cat` и файлов —
    _WHY_COMPUTED); None, если такого нет."""
    commands, feeds, scripts = _analysis(command)
    for info in commands:
        why = _segment_doubt(info, depth)
        if why:
            return info.segment, why
    for segment, text in chain(((t.strip(), t) for t in scripts), _stdin_scripts(feeds)):
        # Тело heredoc оболочки с внешним раскрытием (`bash <<E` ⏎ `$t` ⏎ `E`) — вычисляемый текст.
        if text is None or getattr(text, "expands", False):
            return segment, _WHY_COMPUTED
        if depth >= _MAX_DEPTH:
            if _soup_add(text):
                return segment, _WHY_DEPTH
            continue
        found = _find_doubt(text, depth + 1)
        if found:
            return segment, found[1]
    return None


def heredocs(line):
    """Heredoc, открытые строкой, в порядке чтения их тел: (терминатор, снимать ли ведущие табы) — shparse.heredocs."""
    return shparse.heredocs(line)


def _segments(command):
    """Сегменты текста — простые команды дерева (_analysis) по порядку текста, в том числе в подстановках, телах
    функций и heredoc оболочки, без пробелов по краям; их слова даёт _command. Разбор без дерева (_Unparsed) — весь
    текст одним сегментом: маркер согласия — в его начале, как у сомнения без разбора."""
    try:
        return [info.segment for info in _analysis(command)[0]]
    except _Unparsed:
        return [command.strip()] if command.strip() else []


def _command(segment):
    """Слова команды сегмента (_strip_command) и стоит ли маркер согласия среди её ведущих присваиваний. Сегмент —
    текст простой команды (_segments); из нескольких команд текста берётся первая по тексту. Разбор без дерева — слов
    нет, маркер — в начале текста (_UNPARSED_MARKER)."""
    try:
        commands = _analysis(segment)[0]
    except _Unparsed:
        return [], bool(_UNPARSED_MARKER.match(segment))
    if not commands:
        return [], False
    return list(commands[0].words), commands[0].marker


def _split(text, braces=False):
    """Пары (слово, начало) первой по тексту простой команды text (_pairs; при braces — с раскрытием фигурных
    скобок), с перенаправлениями по порядку текста: перенаправление — пара (оператор с номером и целью слитно,
    оператор с номером), её узнаёт _REDIRECT (`2>&1`; `< p.patch` — `<p.patch` и `<`); цель heredoc — терминатор.
    Процесс-подстановка словом — её текст. Разбор без дерева — пар нет."""
    try:
        commands = _analysis(text)[0]
    except _Unparsed:
        return []
    if not commands:
        return []
    simple, text = commands[0].simple, commands[0].text
    budget = [_BRACE_BUDGET]
    out = [(_word_text(word, text), DEP_OK_MARKER if text[word.start:word.end] == DEP_OK_MARKER else "_=")
           for word in simple.assigns]
    for item in sorted([*simple.words, *simple.redirs], key=lambda node: node.start):
        if type(item) is shparse.Redir:
            op = (item.fd or "") + item.op
            target = item.target
            value = target.term if type(target) is shparse.Heredoc else _word_text(target, text)
            out.append((op + value, op))
            continue
        pairs, cut = _word_pairs(item, text, budget, braces)
        out.extend(pairs)
        if cut:
            break
    return out


def dependency_doubt(command):
    """Сегмент команды, где установка пакета под сомнением, и довод (_WHY_*): флаг перед подкомандой вне известных
    наборов, `--dry-run` значением флага, пакеты из stdin xargs, вложенность глубже _MAX_DEPTH, слова за пределом
    раскрытия скобок, слово менеджера или строка `eval`/`sh -c` за _WORDS_LIMIT (_installer, _cut_script), команда
    за _MAX_WRAPPERS обёртками, имя команды — подстановка с глаголом установки, вывод подстановки или `${…}` с
    пробелом (_WHY_NAME: _expanded_install, _expanded_name_install, _name_doubt), подкоманда менеджера — подстановка
    или переменная (_WHY_SUBCOMMAND), строка оболочки вычисляется при исполнении (_WHY_COMPUTED), неизвестная
    программа получает словами менеджер и глагол установки (_WHY_LAUNCHER); разбор без дерева — вложенность глубже
    предела shparse (_WHY_NESTING) — или сбой разбора или детектора на тексте (_WHY_UNPARSED): сегмент — начало
    команды, маркер — в начале всей команды. None — сомнения нет. Смысл — для команды, где dependency_add добавления
    не нашёл; маркер согласия снимает сомнение там же, где и проверку."""
    if not isinstance(command, str):
        return None
    try:
        return _within_call(_doubt_pass, command)
    except _Unparsed as exc:
        why = exc.why
    except Exception:  # noqa: BLE001 — сбой детектора на тексте команды: сомнение (_WHY_UNPARSED)
        why = _WHY_UNPARSED
    if _UNPARSED_MARKER.match(command):
        return None
    shown = command.strip()
    if len(shown) > _UNPARSED_SHOWN:
        shown = shown[:_UNPARSED_SHOWN - 1] + "…"
    return shown, why


# Маркер согласия в начале всей команды снимает сомнение без разбора (_WHY_NESTING, _WHY_UNPARSED).
_UNPARSED_MARKER = re.compile(r"\s*" + re.escape(DEP_OK_MARKER) + r"[ \t]")


def _doubt_pass(command, depth):
    """Проход сомнения: сначала проход добавления (_find) — его сбой на тексте тоже сомнение, а не пропуск: хук
    спрашивает dependency_doubt, когда dependency_add ничего не вернул, в том числе из-за сбоя; затем _find_doubt.
    Разборы текстов вызова общие (_within_call): проход добавления их не повторяет."""
    _find(command, depth)
    return _find_doubt(command, depth)


def dependency_add(command):
    """Сегмент команды, добавляющий пакет; None, если такого нет. Маркер согласия снимает проверку с
    сегмента, где он стоит среди ведущих присваиваний команды, в том числе после `sudo`, `env`, `if`
    (`PLANKA_DEP_OK=1 npm install x`). Разбор без дерева и сбой детектора — None: сомнение о такой команде отдаёт
    dependency_doubt."""
    if not isinstance(command, str):
        return None
    try:
        return _within_call(_find, command)
    except Exception:  # noqa: BLE001 — разбор без дерева или сбой детектора: сомнение отдаёт dependency_doubt
        return None
