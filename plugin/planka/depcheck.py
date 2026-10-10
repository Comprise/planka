"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import base64
import bisect
import fnmatch
import re
import shlex
from collections import deque
from itertools import chain, islice, product

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
    # Слова из stdin xargs не видны: команду установки без пакета, менеджер без подкоманды и `sh -c` без строки под
    # xargs отдаёт _doubt (_xargs_doubt); строку замены `-I`/`-i`/`--replace` в словах команды _strip_command
    # заменяет заглушкой раскрытия _PLACEHOLDER. Флаги xargs разбирает _xargs_flag.
    "xargs": (frozenset(), 0),
}
# Флаги GNU xargs (findutils 4.11): короткие — по optstring `+0a:E:e::i::I:l::L:n:oprs:txP:d:`, длинные — по
# longopts xargs.c; 1 — значение обязательно, 2 — необязательно, остальные — без значения.
_XARGS_SHORT = {**dict.fromkeys("aEILnsPd", 1), **dict.fromkeys("eil", 2)}
_XARGS_LONG = {"--null": 0, "--arg-file": 1, "--delimiter": 1, "--eof": 2, "--replace": 2, "--max-lines": 2,
               "--max-args": 1, "--open-tty": 0, "--interactive": 0, "--no-run-if-empty": 0, "--max-chars": 1,
               "--verbose": 0, "--show-limits": 0, "--exit": 0, "--max-procs": 1, "--process-slot-var": 1,
               "--version": 0, "--help": 0}
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
# `composer global` и однозначные сокращения имени (Symfony Console: других команд composer на `g` нет).
_COMPOSER_GLOBAL = {"g", "gl", "glo", "glob", "globa", "global"}
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
_WHY_XARGS = "пакеты или слова команды приходят из stdin xargs, разбор их не видит"
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
_WHY_STRING = ("программа получает строкой или на stdin текст, который разбирается как команда установки пакета: "
               "она может его исполнить")
_WHY_RENAMED = ("имя команды — переменная, шаблон имён или alias, и под ним стоит команда установки пакета: менеджер "
                "известен только при исполнении")
_WHY_RUNTIME_NAME = ("имя команды — ссылка на переменную со значением в тексте команды или склейка с ней, но значение "
                     "имени известно только при исполнении (замена, обрезка, подстрока, смена регистра, косвенная "
                     "ссылка или слишком много сочетаний значений): под ним может стоять менеджер пакетов")
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
# Программы, чьи слова — данные, а не команда: текст, поиск, справка, буфер обмена (`pbcopy`, `xclip`, `wl-copy`
# кладут stdin в буфер, man pbcopy, man xclip, man wl-clipboard), git (команды, которые git запускает сам, — _git_runs).
_DATA_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "git", "gh", "cat", "sed", "awk", "head",
                  "tail", "jq", "wc", "ls", "diff", "man", "tldr", "help", "info", "apropos", "which", "type",
                  "whereis", "sort", "uniq", "tee", "less", "more", "test", "[", "[[", "true", "false", ":", "read",
                  "export", "declare", "local", "set", "unset", "alias", "pbcopy", "xclip", "wl-copy"}


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
    elif name in pkgmanagers._CONDAS:
        w = pkgmanagers._subcommand(w, pkgmanagers._CONDA_GLOBAL_VALUE_FLAGS)
    if name == "yarn" and len(w) > 2 and w[1] == "workspace":
        w = [w[0], *w[3:]]
    if name == "yarn" and w[1:3] == ["workspaces", "foreach"]:
        # Команда yarn в каждом workspace.
        rest = pkgmanagers._yarn_foreach(w[3:])
        return rest is not None and _is_add([w[0], *rest], depth + 1, mode)
    if name in ("poetry", "composer", "yarn") and len(w) > 1 and (
            w[1] in ("self", "global") or name == "composer" and w[1] in _COMPOSER_GLOBAL):
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
            # `пакет@none` убирает зависимость.
            return any(not p.endswith("@none") for p in pkgmanagers._positionals(args, pkgmanagers._GO_VALUE_FLAGS))
        if sub == "install":
            # `go install` без версии собирает пакет из go.mod проекта.
            return any("@" in p for p in pkgmanagers._positionals(args, pkgmanagers._GO_BUILD_VALUE_FLAGS))
        return False
    if name == "npm":
        return pkgmanagers._npm_command(sub) in pkgmanagers._NPM_INSTALL and pkgmanagers._has(
            args, pkgmanagers._NPM_VALUE_FLAGS)
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
        sub, args = pkgmanagers._gem_sub_args(w)
        return sub in _GEM_INSTALL and not pkgmanagers._gem_file(args) and pkgmanagers._has(
            args, pkgmanagers._GEM_VALUE_FLAGS)
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
        args = pkgmanagers._subcommand(["pub", *args], pkgmanagers._PUB_GLOBAL_VALUE_FLAGS)[1:]
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
            if a[:1] in "-/" and flag and (flag == "ec" or "encodedcommand".startswith(flag)):
                # `-EncodedCommand` (`-e`, `-ec`): строка — base64 текста UTF-16LE (about_Pwsh).
                # Не base64 или не UTF-16LE — pwsh команду не запустит.
                value = args[k + 1] if k + 1 < len(args) else ""
                try:
                    text = base64.b64decode(value, validate=True).decode("utf-16-le")
                except ValueError:
                    text = None
                return [text] if text else []
        return []
    if name == "fish":
        # Строки `-C`/`--init-command` — команды до строки `-c` или скрипта; флаги — getopt_long до первого
        # слова-не-флага (_FISH_SHORT, _FISH_LONG).
        out, k = [], 0
        while k < len(args) and args[k].startswith("-") and args[k] not in ("-", "--"):
            a = args[k]
            k += 1
            if a.startswith("--"):
                flag, eq, value = a.partition("=")
                flag = pkgmanagers._long_flag(flag, _FISH_LONG | _FISH_PLAIN_LONG)
                if flag in _FISH_LONG and not eq:
                    value = args[k] if k < len(args) else None
                    k += 1
                if flag == "--init-command" and value is not None:
                    out.append(pkgmanagers._part_of(a, value) if eq else value)
                continue
            for j in range(1, len(a)):
                if a[j] in _FISH_SHORT:
                    value = a[j + 1:]
                    if not value:
                        value = args[k] if k < len(args) else None
                        k += 1
                    elif a[j] == "C":
                        value = pkgmanagers._part_of(a, value)
                    if a[j] == "C" and value is not None:
                        out.append(value)
                    break
        return out
    if name == "nix" and args[:1] in (["develop"], ["shell"]):
        for k, a in enumerate(args):
            if a in ("-c", "--command"):
                return [pkgmanagers._shell_join(args[k + 1:])]
        return []
    if name == "nix-shell":
        return [args[k + 1] for k, a in enumerate(args[:-1]) if a in ("--run", "--command")]
    if name in ("mise", "rtx") and args[:1] in (["exec"], ["x"]):
        return _mise_runs(args[1:])
    return []


# Флаги fish со значением (fish.rs, fish_parse_opt: SHORT_OPTS `+hPilNnvc:C:p:d:f:D:o:`, LONG_OPTS); длинные без
# значения узнаются только для сокращений getopt_long.
_FISH_SHORT = frozenset("cCpdfDo")
_FISH_LONG = {"--command", "--init-command", "--features", "--debug", "--debug-output", "--debug-stack-frames",
              "--profile", "--profile-startup"}
_FISH_PLAIN_LONG = {"--interactive", "--login", "--no-config", "--no-execute", "--print-rusage-self",
                    "--print-debug-categories", "--private", "--help", "--version"}


# Флаги `mise exec` со значением следующим словом, кроме `-c`/`--command` (mise.jdx.dev/cli/exec.html).
_MISE_EXEC_VALUE_FLAGS = {"-j", "--jobs", "--allow-env", "--allow-net", "--allow-read", "--allow-write", "--secrets"}


def _mise_runs(args):
    """Команды `mise exec` из слов за `exec`: строка оболочки `-c`/`--command` (`-c '…'`, `-c'…'`, `-c=…`,
    `--command=…`) и слова за `--` (запуск без оболочки — запись _shell_join). Флаги и `TOOL@VERSION` — до `--`."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.append(pkgmanagers._shell_join(args[i + 1:]))
            break
        if a in ("-c", "--command"):
            if i + 1 < len(args):
                out.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--command="):
            out.append(pkgmanagers._part_of(a, a.partition("=")[2]))
        elif a.startswith("-c") and not a.startswith("--"):
            out.append(pkgmanagers._part_of(a, a[3:] if a[2:3] == "=" else a[2:]))
        elif a in _MISE_EXEC_VALUE_FLAGS:
            i += 1
        i += 1
    return out


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
    if xargs and _xargs_doubt(words):
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


def _xargs_doubt(words):
    """Слова команды под xargs с дописанными словами stdin ставят пакет: пакет (_STDIN_PACKAGE в конце), подкоманда
    менеджера (`xargs npm`: слово раскрытия в конце, _is_add в режиме _COMPUTED), менеджер или запускатель без
    позиционных слов (`xargs pacman`, `xargs python3`: операция — из stdin), строка `-c` оболочки (`xargs bash
    -c`) или вся команда — из stdin (_XARGS_STDIN)."""
    if not words:
        return False
    if words[0] is _XARGS_STDIN:
        return True
    stdin = _Word(_PLACEHOLDER)
    stdin.expands = True
    tail = [*words, stdin]
    return (_is_add([*words, _STDIN_PACKAGE]) or _is_add(tail, mode=_COMPUTED)
            or pkgmanagers._installer(words[0]) and not pkgmanagers._after_flags(words[1:], pkgmanagers._NO_VALUE_FLAGS)
            or pkgmanagers._inline_script(tail) is stdin)


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
# Сколько символов текста имён команд с подстановкой (_name_doubt) и значений имён-переменных и alias (_renamed)
# разбирает один вызов dependency_doubt; дальше — сомнение без разбора: проход линеен.
_NAME_BUDGET = 4 * _WORDS_LIMIT
# Сколько символов строк и stdin программ (_string_doubt, _program_inputs) разбирает один вызов dependency_doubt;
# дальше — сомнение _WHY_INPUT без разбора: разбор текста в сотни килобайт занял бы секунды при сроке хука 10 с.
_INPUT_BUDGET = 1 << 16
_WHY_INPUT = (f"строки и stdin программ в команде длиннее {_INPUT_BUDGET} символов: команда установки в них не "
              "проверена; маркер согласия — в начале команды")
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


# Команда под xargs, вся пришедшая из stdin: за xargs обёртка без своей команды (`xargs env`, `xargs timeout 5`).
_XARGS_STDIN = _Word(_PLACEHOLDER)
_XARGS_STDIN.expands = True

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


# Значение слова из одной процесс-подстановки: путь, который bash передаёт команде вместо неё.
_PROCSUB_PATH = "/dev/fd/63"


def _pairs(simple, text, budget, braces=True):
    """Пары (слово, начало) простой команды дерева: ведущие присваивания, затем слова (_word_text, _word_lead);
    перенаправления не входят. Слово из одной процесс-подстановки — путь _PROCSUB_PATH (её вход и выход — связи
    _procsub_feeds): оно держит место операнда (`bash <(…) arg` — скрипт `<(…)`, `pip install -r <(…) x` — `x`
    пакет), а оболочка или `source` с ним скриптом исполняют её вывод (pkgmanagers._heredoc_runs); начало у него
    пустое. Начало присваивания — "_=" (присваивание при любом индексе: его узнал разбор, bash 5.3: `a["]"]=1`,
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
            out.append((_Word(_PROCSUB_PATH), ""))
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
    `su`/`runuser` без `-c` и `-u` — оболочка (_c_wrapper); строка замены xargs (_xargs_flag) в словах за ним —
    заглушка раскрытия _PLACEHOLDER с признаком expands, а команда из одних обёрток за xargs — слово _XARGS_STDIN.
    Предел _WORDS_LIMIT считается от первого слова команды: длинные присваивания и обёртки перед ней его не
    прячут."""
    # Очередь: снятие слова спереди и вставка строки `env -S` — без копирования хвоста, проход линеен.
    words = deque(pairs)
    marker = xargs = False
    replace = None
    wrappers = 0
    last = None
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
            last = name
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
                if name == "xargs":
                    found, takes = _xargs_flag(flag, words)
                    replace = found or replace
                    if takes and words:
                        words.popleft()
                    continue
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
    if xargs and not words and last != "xargs":
        words.append((_XARGS_STDIN, _XARGS_STDIN))
    if replace:
        # xargs подставляет строку stdin на место строки замены в каждом слове: значение неизвестно до исполнения.
        for k, (word, lead) in enumerate(words):
            if replace in word:
                value = _Word(word.replace(replace, _PLACEHOLDER))
                value.expands = True
                words[k] = (value, lead)
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


def _xargs_flag(flag, words):
    """Флаг xargs flag по getopt_long GNU xargs (_XARGS_SHORT, _XARGS_LONG; words — очередь пар за ним):
    (строка замены или None, берёт ли флаг значением следующее слово). Склейка коротких флагов читается по буквам:
    буква со значением забирает остаток склейки, а без остатка обязательное значение — следующее слово
    (`-0I{}`, `-rI {}`); необязательное значение — только остаток склейки (`-ri` — замена `{}`, `-il` — замена `l`).
    Длинный флаг узнаётся и по однозначному началу имени (`--rep`), значение необязательного — только после `=`.
    Строка замены — значение `-I`, `-i`, `--replace`, без значения — `{}`."""
    if flag.startswith("--"):
        name, eq, value = flag.partition("=")
        name = pkgmanagers._long_flag(name, _XARGS_LONG)
        if name == "--replace":
            return (value if eq else "{}"), False
        return None, _XARGS_LONG.get(name) == 1 and not eq
    for j in range(1, len(flag)):
        letter = flag[j]
        kind = _XARGS_SHORT.get(letter)
        if kind is None:
            continue
        rest = flag[j + 1:]
        replace = None
        if letter in "Ii":
            replace = rest or (words[0][0] if words else None) if letter == "I" else rest or "{}"
        return replace, kind == 1 and not rest
    return None, False


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
    _call = ([_BRACE_BUDGET], {}, [_NAME_BUDGET], [_INPUT_BUDGET])
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
    commands, feeds, scripts, loops = [], [], [], []
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
            elif kind is shparse.For and node.name is not None and node.words:
                loops.append((node, text))
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
    result = (commands, feeds, scripts, loops)
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


# Экранирования с `\\` и буквой в выводе `echo -e`, `%b` и формате printf (bash 5.3, builtins/printf.def и
# lib/sh/strtrans.c).
_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
            "\\": "\\"}
# Спецификатор формата printf: флаги, ширина, точность, модификаторы длины (bash их пропускает), преобразование.
_PRINTF_SPEC = re.compile(r"%([-+ #0]*)(\*|\d*)(?:\.(\*|\d*))?[hjlLtz]*(.)", re.S)
# Целое для ширины и точности `*` из аргумента.
_PRINTF_INT = re.compile(r"\s*[-+]?\d{1,9}")
# Предел длины вывода echo/printf и ширины поля: дальше вывод не строится (сомнение), формат с повтором по
# аргументам иначе дал бы вывод квадратичной длины.
_ECHO_MAX = 1 << 16


def _escape_at(text, i, kind):
    """Экранирование `\\` в text с позиции i по bash 5.3: kind "echo" — `echo -e`, "b" — аргумент `%b`, "format" —
    формат printf. (символы, позиция за экранированием, оборван ли вывод `\\c`); None — экранирование, которое bash
    раскрывает иначе, чем внешние echo и printf (`\\c` в формате: bash печатает его как есть, coreutils обрывает
    вывод), или `\\u` вне Юникода. Восьмеричное: в формате — `\\` и до 3 цифр, в `%b` — `\\0` и до 3 цифр или
    `\\1`–`\\7` и до 2, у echo — только `\\0` и до 3 цифр; значение — младший байт. `\\x` — до 2 шестнадцатеричных
    цифр, `\\u` — до 4, `\\U` — до 8; без цифр, неизвестная буква и `\\` в конце остаются как есть: в формате
    печатается только `\\`, символ за ним разбирается дальше (`\\%s` — `\\` и спецификатор). Только в формате `\\"`,
    `\\'`, `\\?` — сам символ."""
    n = len(text)
    if i + 1 >= n:
        return "\\", i + 1, False
    e = text[i + 1]
    i += 2
    if e in _ESCAPES:
        return _ESCAPES[e], i, False
    if e == "c":
        return None if kind == "format" else ("", i, True)
    if kind == "format" and e in "\"'?":
        return e, i, False
    if e in "01234567" and (kind == "format" or e == "0" or kind == "b"):
        j = i - 1 if kind == "format" or e != "0" else i
        limit = min(j + 3, n)
        value = 0
        while j < limit and text[j] in "01234567":
            value = value * 8 + int(text[j])
            j += 1
        return chr(value & 0xFF), j, False
    if e in "xuU":
        limit = min(i + {"x": 2, "u": 4, "U": 8}[e], n)
        j = i
        while j < limit and text[j] in "0123456789abcdefABCDEF":
            j += 1
        if j == i:
            return "\\" + e, i, False
        value = int(text[i:j], 16)
        if e != "x" and (value > 0x10FFFF or 0xD800 <= value <= 0xDFFF):
            return None
        return chr(value), j, False
    if kind == "format":
        return "\\", i - 1, False
    return "\\" + e, i, False


def _unescape(text, kind):
    """Текст text с раскрытыми экранированиями (_escape_at): (вывод, оборван ли `\\c`); None — как у _escape_at."""
    out, i = [], 0
    while True:
        j = text.find("\\", i)
        if j < 0:
            out.append(text[i:])
            return "".join(out), False
        out.append(text[i:j])
        done = _escape_at(text, j, kind)
        if done is None:
            return None
        piece, i, stop = done
        out.append(piece)
        if stop:
            return "".join(out), True


def _printf_output(args):
    """Вывод `printf формат [аргументы]` по bash 5.3: экранирования формата (_unescape), `%%`, `%s`, `%b`, `%c` с
    флагом `-`, шириной и точностью (`*` — из аргумента); формат повторяется, пока остаются аргументы и проход их
    берёт; недостающий аргумент — пустая строка. None — вывод неизвестен: спецификатор не из этих (`%d`, `%q`),
    ошибка формата (bash обрывает вывод), экранирование _unescape без значения, вывод или ширина больше
    _ECHO_MAX."""
    if not args:
        return None
    fmt, rest = args[0], deque(args[1:])
    out, size = [], 0
    while True:
        took = False
        i = 0
        while i < len(fmt):
            c = fmt[i]
            if c == "%":
                if fmt[i + 1:i + 2] == "%":
                    piece, i = "%", i + 2
                else:
                    m = _PRINTF_SPEC.match(fmt, i)
                    if m is None or m[4] not in "sbc":
                        return None
                    i = m.end()
                    bounds = []
                    for value in (m[2], m[3]):
                        if value == "*":
                            took = took or bool(rest)
                            number = rest.popleft() if rest else "0"
                            if not _PRINTF_INT.fullmatch(number):
                                return None
                            value = number.strip()
                        bounds.append(None if value is None or value == "" and not bounds else int(value or 0))
                    width, precision = bounds
                    if width is not None and abs(width) > _ECHO_MAX:
                        return None
                    took = took or bool(rest)
                    arg = rest.popleft() if rest else ""
                    stop = False
                    if m[4] == "b":
                        done = _unescape(arg, "b")
                        if done is None:
                            return None
                        arg, stop = done
                    elif m[4] == "c":
                        arg = arg[:1]
                    if precision is not None and precision >= 0 and m[4] != "c":
                        arg = arg[:precision]
                    if width is not None and len(arg) < abs(width):
                        pad = " " * (abs(width) - len(arg))
                        arg = arg + pad if "-" in m[1] or width < 0 else pad + arg
                    piece = arg
                    if stop:
                        out.append(piece)
                        return "".join(out)
            elif c == "\\":
                done = _escape_at(fmt, i, "format")
                if done is None:
                    return None
                piece, i, _ = done
            else:
                j = i
                while j < len(fmt) and fmt[j] not in "\\%":
                    j += 1
                piece, i = fmt[i:j], j
            out.append(piece)
            size += len(piece)
            if size > _ECHO_MAX:
                return None
        if not rest or not took:
            return "".join(out)


def _echo_texts(words):
    """Варианты вывода команды `echo …` или `printf …` по её словам (bash 5.3); None — вывод неизвестен (не
    echo/printf, `printf -v`, `printf` с иным флагом, формат _printf_output без вывода). `echo` — слова через пробел,
    флаги `-n`, `-e`, `-E` — ведущие слова из этих букв, последний из `e`/`E` решает, раскрывать ли экранирования
    (_unescape, `\\c` обрывает вывод); `echo` без `-e` и `-E` со `\\` — ещё вариант с раскрытыми экранированиями:
    echo dash и bash с `xpg_echo` их раскрывает. Символ с кодом 0 bash при чтении скрипта отбрасывает. Слово с
    раскрытием несёт его текст: вычисляемое имя в выводе ловит _computed."""
    if not words:
        return None
    name = pkgmanagers._basename(words[0])
    args = words[1:]
    if name == "echo":
        escapes, flagged, newline = False, False, "\n"
        while args and re.fullmatch(r"-[neE]+", args[0]):
            for letter in args[0][1:]:
                if letter == "n":
                    newline = ""
                else:
                    escapes, flagged = letter == "e", True
            args = args[1:]
        text = " ".join(args)
        if escapes:
            texts = [_unescape(text, "echo")]
        else:
            texts = [(text, False)]
            if not flagged and "\\" in text:
                texts.append(_unescape(text, "echo"))
        out = [(t[0] + ("" if t[1] else newline)).replace("\0", "") for t in texts if t is not None]
        return out if out else None
    if name == "printf":
        if args[:1] == ["--"]:
            args = args[1:]
        elif args[:1] and args[0].startswith("-") and args[0] != "-":
            # `-v имя` пишет вывод в переменную; иной флаг — ошибка, вывода нет.
            return None
        text = _printf_output(args)
        return None if text is None else [text.replace("\0", "")]
    return None


def _heredoc_cat(info):
    """Команда — `cat` без флагов с heredoc, без операндов или с операндом stdin (pkgmanagers._STDIN_SCRIPT): её
    вывод — тело heredoc и файлы."""
    words = info.words
    return words[:1] == ["cat"] and not any(a.startswith("-") and a != "-" for a in words[1:]) and (
        len(words) == 1 or any(pkgmanagers._STDIN_SCRIPT.fullmatch(a) for a in words[1:])) and bool(info.bodies)


def _file_output(words):
    """Вывод команды — содержимое названных файлов: `cat` с файлами без флагов и операндов stdin
    (pkgmanagers._STDIN_SCRIPT), `git show <ревизия>:<путь>`. Скрипт из файла детектор не видит, как у `bash файл`."""
    if not words:
        return False
    name, args = pkgmanagers._basename(words[0]), words[1:]
    if name == "cat":
        return bool(args) and not any(a.startswith("-") or pkgmanagers._STDIN_SCRIPT.fullmatch(a) for a in args)
    return name == "git" and args[:1] == ["show"] and len(args) > 1 and all(
        ":" in a and not a.startswith("-") for a in args[1:])


def _computed(text):
    """Имя хотя бы одной команды текста вычисляется при исполнении: первое слово команды — подстановка или
    переменная (`$X`, `"$X"`, `` `…` ``, `$(…)`). Кавычки ANSI-C `$'…'` — не подстановка: они раскрыты."""
    return any(info.words[0].startswith(("$", "`")) for info in _analysis(text)[0] if info.words)


def _stdin_scripts(feeds):
    """Текст, который команда исполняет со stdin или из процесс-подстановки, по связям feeds (_analysis): (сегмент
    для отказа, текст). Вывод `echo`/`printf` (_echo_texts) в оболочку без скрипта или со скриптом
    pkgmanagers._STDIN_SCRIPT (`echo 'npm i x' | bash`, `bash <(echo …) arg`) или в `source` и `.` с таким скриптом
    (`source <(…)`), если приёмник без маркера; вывод `cat` с heredoc
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
            runs[id(target)] = not target.marker and pkgmanagers._heredoc_runs(words)
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
        texts = _echo_texts(source.words)
        # Слово `echo`/`printf` с внешним раскрытием: его значение — текст для оболочки (`echo "$t" | bash`).
        if texts is None or any(_computed(t) for t in texts) or any(
                getattr(w, "expands", False) for w in source.words[1:]):
            yield target.segment, None
            continue
        for text in texts:
            if text:
                yield source.segment, text


def _scripts(info):
    """Строки, которые команда исполняет как команды: `sh -c '…'`, `eval …` (_inline_script), here-string оболочки
    без скрипта или `source /dev/stdin` (`bash <<< '…'`, _heredoc_runs), удалённая команда ssh, `trap '…'`,
    `find -exec …`, `cmd /c`, `pwsh -Command`, `pwsh -EncodedCommand`, `fish -C`, `nix develop -c`, `nix-shell --run`, `mise exec -c|--`, команды git
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
    commands, feeds, scripts, _ = _analysis(command)
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
    return _flat_install(texts, words[1:])


def _flat_install(texts, tail=()):
    """Слова текстов texts (кавычки сняты, `${…`-начала и `}` — пробелы, разделители команд — границы) и слова tail
    ставят пакет с какого-то места (_is_add, _expanded_install); больше _MAX_LAUNCH_PAIRS мест с менеджером — True
    без разбора: проход линеен."""
    flat = []
    for text in texts:
        text = _SOUP_QUOTES.sub("", _PARAM_OPEN.sub(" ", text).replace("}", " "))
        for piece in _SOUP_SPLIT.split(text):
            flat.extend(piece.split())
    flat.extend(tail)
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


# Символы, без которых строка не делится на команду со словами: пробельные и разделители команд.
_STRING_SPLITS = re.compile(r"[\s;&|]")
# Присваивание переменной словом: имя, индекс, `+=`, значение (`X=npm i x`, `export X=…`, `alias n=npm`).
_VALUE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?\+?=(.*)", re.S)
# Слово — ссылка целиком на переменную, элемент или все элементы массива (`${a[@]}`, `${a[0]}`), позиционные
# параметры (`$@`, `${*}`, `$1`, `${10}`, `${01}` — номер с ведущими нулями; `$10` — `$1` и «0»; `$0`, `${0}` — не
# позиционный), срез `${@:N}`, `${*:N:L}` (две последние группы: двоеточие среза и двоеточие длины): bash делит
# значение без кавычек на слова.
_NAME_VAR = re.compile(r"\$(?:\{(?:([A-Za-z_][A-Za-z0-9_]*|[@*])(?:\[[^\]]*\])?|(0*[1-9][0-9]*))\}"
                       r"|([A-Za-z_][A-Za-z0-9_]*|[@*1-9])|\{[@*](:)[^:}]*(:)?[^}]*\})")
# Текст `${…}` с оператором (bash 5.3, man bash, «Parameter Expansion»): `-`, `=`, `+`, `?` с двоеточием и без
# у переменной, элемента массива или позиционного параметра (номер и с ведущими нулями); группы — имя, индекс,
# оператор, слово (`${X:-w}`, `${a[0]+w}`, `${1:?w}`, `${01:-w}`).
_PARAM_OP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*|[@*]|0*[1-9][0-9]*)(?:\[([^\]]*)\])?(:?[-=+?])(.*)\}", re.S)
# Срез массива `${a[@]:N}`, `${a[*]:N:L}`; группы — имя и двоеточие длины.
_ARRAY_SLICE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[[@*]\]:[^:}]*(:)?[^}]*\}")
# Элемент массива `${a[N]}` (индекс — не `@` и не `*`); группа — имя.
_ARRAY_ITEM = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[(?![@*]\])[^\]]*\]\}")
# Начало текста раскрытия параметра: `!` косвенной ссылки или `#` длины и имя — переменная, позиционный параметр,
# `@`, `*` (`${X/a/b}`, `${!Y}`, `${#X}`, `$1`); имени нет у специальных `$$`, `$?`, `$#`, `$-`.
_REF = re.compile(r"\$\{?([!#]?)([A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*])")
# Имена переменных по началу: `${!начало*}`, `${!начало@}`.
_NAMES_BY_PREFIX = re.compile(r"\$\{!([A-Za-z_][A-Za-z0-9_]*)[*@]\}")
# Интерпретаторы, чьи строки и stdin — код своего языка, а не команды оболочки (ещё _PYTHON — шаблоном): строка
# оболочки в нём — литерал другого языка, разбор оболочки её не видит.
_CODE_PROGRAMS = {"node", "nodejs", "perl", "ruby", "php", "lua", "luajit", "r", "rscript", "julia", "osascript"}
# Имена, которые шаблон имён в имени команды может дать командой установки (_renamed): программы, разбор которых
# детектор знает, и менеджеры с запускателями pkgmanagers._INSTALLERS, кроме программ-данных; имена, которые
# разбор узнаёт регулярным выражением, — _NAME_FAMILIES.
_GLOB_PROGRAMS = sorted((_KNOWN_PROGRAMS | pkgmanagers._INSTALLERS) - _DATA_PROGRAMS)
# Имена, которые разбор узнаёт регулярным выражением (pkgmanagers._PIP, _PYTHON), семействами для сверки с шаблоном
# имён (_glob_name): начала имени, имена без хвоста, символы суффикса; хвост за началом — `\d*(?:\.\d+)*` и суффиксы.
_NAME_FAMILIES = ((("pip",), (), ""), (("python", "pypy"), ("py",), "dmt"))
# Длина имени файла (NAME_MAX): шаблон длиннее даёт только имена длиннее, таких программ нет.
_NAME_MAX = 255
# Встроенные, чьи слова `ИМЯ=значение` — присваивания переменных.
_DECLARES = {"export", "declare", "typeset", "local", "readonly"}


def _text_program(words, git=False):
    """Программа слов words может исполнить строку или stdin как команды оболочки: не программа-данные _DATA_PROGRAMS
    (git — при git: он исполняет строки своих настроек и флагов) и не интерпретатор (_CODE_PROGRAMS, _PYTHON)."""
    name = pkgmanagers._basename(words[0]) if words else None
    if name is None or name in _DATA_PROGRAMS and not (git and name == "git"):
        return False
    return name not in _CODE_PROGRAMS and not pkgmanagers._PYTHON.match(name)


def _family_step(family, state, c):
    """Переход автомата имён семейства family (_NAME_FAMILIES) из state по символу c; None — имени дальше нет.
    Состояние — ("h", начало имени) или хвост: "A" — за началом или цифрой, "B" — за `.`, "C" — за суффиксом."""
    heads, exact, suffix = family
    if state[0] == "h":
        word = state[1] + c
        if any(n.startswith(word) for n in heads + exact):
            return ("h", word)
        state = ("A",) if state[1] in heads else None
        if state is None:
            return None
    if c.isdigit() and state[0] in "AB":
        return ("A",)
    if c == "." and state[0] == "A":
        return ("B",)
    return ("C",) if c in suffix and state[0] in "AC" else None


def _family_accepts(family, state):
    """Состояние state — конец имени семейства family."""
    return state[0] in "AC" or state[0] == "h" and state[1] in family[0] + family[1]


def _glob_tokens(pattern):
    """Элементы шаблона имён по разбору fnmatch.translate: `*`, `?`, `[…]` (без `]` — литерал `[`), символ."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        j = i + 1
        if pattern[i] == "[":
            j += pattern[j:j + 1] == "!"
            j += pattern[j:j + 1] == "]"
            j = pattern.find("]", j) + 1 or i + 1
        out.append(pattern[i:j])
        i = j
    return out


def _glob_name(pattern, family):
    """Имя семейства family (_NAME_FAMILIES), подходящее под шаблон имён pattern, или None: обход произведения
    автомата имён (_family_step) и элементов шаблона (_glob_tokens), каждое состояние — с одним именем."""
    heads, exact, suffix = family
    alphabet = sorted(set("".join(heads + exact) + suffix + "0123456789."))
    states = {("h", ""): ""}
    for token in _glob_tokens(pattern):
        chars = [c for c in alphabet if fnmatch.fnmatchcase(c, token)]
        if token == "*":
            queue = deque(states)
            while queue:
                state = queue.popleft()
                for c in chars:
                    following = _family_step(family, state, c)
                    if following is not None and following not in states:
                        states[following] = states[state] + c
                        queue.append(following)
            continue
        following = {}
        for state, name in states.items():
            for c in chars:
                nxt = _family_step(family, state, c)
                if nxt is not None:
                    following.setdefault(nxt, name + c)
        states = following
        if not states:
            return None
    return next((name for state, name in states.items() if _family_accepts(family, state)), None)


def _installs(text, depth):
    """Текст, разобранный как команды на уровень глубже, ставит пакет (_find; на глубине _MAX_DEPTH — _soup_add)."""
    if depth >= _MAX_DEPTH:
        return _soup_add(text)
    return _find(text, depth + 1) is not None


def _input_doubt(text, depth):
    """Довод сомнения для строки или stdin text программы: _WHY_INPUT сверх остатка _INPUT_BUDGET вызова, _WHY_STRING,
    если текст ставит пакет (_installs); иначе None."""
    budget = _call[3] if _call is not None else [_INPUT_BUDGET]
    budget[0] -= len(text)
    if budget[0] < 0:
        return _WHY_INPUT
    return _WHY_STRING if _installs(text, depth) else None


def _string_texts(word):
    """Тексты слова, которые программа может исполнить строкой команд: слово целиком и значение за первым `=`
    (`--tree-filter=…`, `core.pager=…`), каждый ещё без ведущего `!` (псевдоним git `!…`); только тексты с
    пробельным символом или разделителем команд — без них команды со словами нет."""
    texts = [word]
    if "=" in word:
        texts.append(pkgmanagers._part_of(word, word.partition("=")[2]))
    texts += [pkgmanagers._part_of(t, t[1:]) for t in texts if t.startswith("!")]
    return [t for t in texts if _STRING_SPLITS.search(t)]


# Склейка коротких флагов git с `-m` (parse-options: буквы до `m` — флаги, остаток слова или следующее слово —
# сообщение): `-m`, `-am`, `-sm'…'`.
_GIT_MESSAGE = re.compile(r"-[A-Za-z]*m")


def _git_texts(args):
    """Слова git, кроме текстов, которые git хранит или разбирает сам: значений `-m`/`--message` и склейки с `-m`
    (_GIT_MESSAGE; сообщение коммита, тега, слияния, stash) и строк `-c alias.<имя>=…` (псевдоним, вызванный
    подкомандой, — _git_runs)."""
    out, skip = [], False
    for k, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("--message", "-c") or _GIT_MESSAGE.fullmatch(a):
            skip = a != "-c" or args[k + 1:k + 2] and args[k + 1].partition(".")[0].lower() == "alias"
            continue
        if a.startswith("--message=") or _GIT_MESSAGE.match(a):
            continue
        out.append(a)
    return out


def _string_doubt(info, depth):
    """Довод сомнения, если слово команды за именем или его значение за `=` разбирается как команда установки
    (_input_doubt; сверх остатка _INPUT_BUDGET — _WHY_INPUT): программа может исполнить строку (`docker exec c sh -c
    '…'`, `parallel ::: '…'`, `npm exec -c '…'`, `git -c core.pager='…'`, `git filter-branch --tree-filter '…'`).
    Слова программ-данных
    _DATA_PROGRAMS — текст, кроме git: он исполняет строки своих настроек и флагов (_git_texts). Строки оболочек
    разбирают _inline_script, _heredoc_runs и _launched (`fish -C`), их позиционное слово — путь скрипта. Имя команды
    строкой не исполняется: программы с пробелом в имени нет, а `sudo -s` экранирует пробелы слов (man sudo 1.9)."""
    words = info.words
    if not _text_program(words, git=True) or pkgmanagers._basename(words[0]) in pkgmanagers._C_SHELLS:
        return None
    args = _git_texts(words[1:]) if pkgmanagers._basename(words[0]) == "git" else words[1:]
    for word in args:
        for text in _string_texts(word):
            why = _input_doubt(text, depth)
            if why:
                return why
    return None


def _program_inputs(commands, feeds):
    """Тексты, которые получает на stdin программа не из _DATA_PROGRAMS и не оболочка или `source` (их вход и скрипт
    разбирает _heredoc_runs: вход исполняет оболочка без скрипта, `source` — только скриптом stdin): тела её heredoc
    и here-string, вывод литерального `echo`/`printf` (_echo_texts) и тела heredoc
    `cat` (_heredoc_cat) по связям feeds; пары (сегмент программы, текст). Неизвестный вывод — не текст: что
    программа получит, неизвестно, сомнения нет."""
    def reads(info):
        words = info.words
        return (not info.marker and _text_program(words)
                and pkgmanagers._basename(words[0]) not in pkgmanagers._SHELLS | pkgmanagers._SOURCES)

    for info in commands:
        if reads(info):
            for text in chain(info.bodies, info.herestrings):
                yield info.segment, text
    for source, target in feeds:
        if source is None or not reads(target):
            continue
        texts = source.bodies if _heredoc_cat(source) else _echo_texts(source.words) or []
        for text in texts:
            yield target.segment, text


def _assigned(word, src, out, alias=False):
    """Присваивание word (слово команды _Word или узел присваивания с исходным текстом src) пишет в out значение
    (текст, вычисляемое ли): у массива `a=(…)` — слова в скобках текстом; вычисляемое — с подстановкой
    (_name_expands: `X=$(…)`, `${x:-…}`). Ключ — имя, у alias — ("alias", имя)."""
    m = _VALUE.fullmatch(word)
    if not m:
        return
    value = m[2]
    raw = _VALUE.fullmatch(src)
    if raw and raw[2].startswith("(") and value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    node = getattr(word, "node", None)
    out.setdefault(("alias", m[1]) if alias else m[1], []).append((value, node is not None and _name_expands(node)))


def _set_args(args):
    """Позиционные параметры `set` по его словам args: слова за флагами (каждая буква `o` флага — и в склейке
    `-eo`, `-oe` — берёт значением имя настройки следующим словом, если оно непустое и не начато с `-` или `+`;
    иначе печатает настройки, а слово разбирается дальше, как в bash 5.3), `--` или `-`; None — `set` их не
    меняет."""
    k = 0
    while k < len(args) and args[k][:1] in ("-", "+"):
        flag = args[k]
        k += 1
        if flag in ("--", "-"):
            return args[k:]
        for _ in range(flag[1:].count("o")):
            if k < len(args) and args[k][:1] not in ("", "-", "+"):
                k += 1
    return args[k:] or None


# Ключ значений _values: слова `set` и сумма сдвигов `shift` для позиционных параметров (_positional).
_POSITIONAL = ("set",)
# Флаги `read` со значением (bash 5.3, `help read`).
_READ_VALUE_FLAGS = {"-a", "-d", "-i", "-n", "-N", "-p", "-t", "-u"}


def _values(commands, loops):
    """Значения переменных и alias текста: имя — список пар (текст значения, вычисляемое ли). Переменные —
    присваивания перед командой и в словах `export`/`declare`/`typeset`/`local`/`readonly` (массив `a=(…)` — слова в
    скобках, _assigned), слова цикла `for`, строка here-string или heredoc `read` во все её имена (вычисляемое) и
    вывод `printf -v` (_printf_output; неизвестный — слова формата и аргументов, вычисляемое); alias — слова
    `alias имя=…`, ключ ("alias", имя); слово оператора `=`, `:=` в раскрытиях слов (_default_assigns). Одинаковые
    пары имени не повторяются. Позиционные параметры — слова `set` (_set_args) и сумма сдвигов `shift` (литерал N,
    без числа — 1, иначе — любой сдвиг) под ключом _POSITIONAL: их значения считает _var_values по ссылке
    (_positional). Значения `read` из файла или конвейера, `mapfile` и `readarray` не видны."""
    out, sets, shifts = {}, [], 0
    for info in commands:
        for word in info.simple.assigns:
            _assigned(_word_text(word, info.text), info.text[word.start:word.end], out)
        words = info.words
        name = pkgmanagers._basename(words[0]) if words else None
        if name in _DECLARES or name == "alias":
            for word in words[1:]:
                src = info.text[word.node.start:word.node.end] if getattr(word, "node", None) else word
                _assigned(word, src, out, name == "alias")
        elif name == "set":
            args = _set_args(words[1:])
            if args is not None:
                sets.append(args)
        elif name == "shift":
            # `shift N` сдвигает на N, без числа — на 1; N не литералом может быть любым.
            n = words[1] if len(words) == 2 else "1" if len(words) == 1 else ""
            shifts += int(n) if n.isdecimal() and len(n) < 5 else _WORDS_LIMIT
        elif name == "read":
            args, targets = list(words[1:]), []
            while args and args[0].startswith("-") and args[0] != "--":
                if args[0] in _READ_VALUE_FLAGS:
                    targets += args[1:2] if args[0] == "-a" else []
                    args = args[1:]
                args = args[1:]
            targets += args[args[:1] == ["--"]:]
            for text in chain(info.bodies, info.herestrings):
                for target in targets:
                    out.setdefault(target, []).append((text, True))
        elif name == "printf" and words[1:2] == ["-v"] and len(words) > 3:
            args = words[3 + (words[3] == "--"):]
            text = _printf_output(args)
            computed = text is None or any(getattr(w, "expands", False) for w in args)
            out.setdefault(words[2], []).append((" ".join(args) if text is None else text, computed))
        _default_assigns(info.simple, out)
    for node, text in loops:
        out.setdefault(text[node.name.start:node.name.end], []).extend(
            (_word_text(w, text), False) for w in node.words)
    # Одинаковые пары (повторный `set` с теми же словами) умножали бы сочетания _renamed: остаётся первая.
    out = {key: list(dict.fromkeys(pairs)) for key, pairs in out.items()}
    out[_POSITIONAL] = (sets, shifts)
    return out


def _default_assigns(simple, out):
    """Присваивания раскрытиями `${X=слово}`, `${X:=слово}` в словах и присваиваниях простой команды simple (и в "…",
    и в словах других операторов) пишут в out слово без кавычек (вычисляемое — с раскрытием) значением имени.
    Позиционному параметру bash так не присваивает (ошибка)."""
    stack = [part for word in chain(simple.assigns, simple.words) for part in word.parts]
    while stack:
        part = stack.pop()
        kind = type(part)
        if kind is shparse.DQ:
            stack.extend(part.parts)
        elif kind is shparse.Param:
            m = _PARAM_OP.fullmatch(part.text)
            if m and m[3][-1] == "=" and m[1][0] not in "@*0123456789":
                out.setdefault(m[1], []).append((shparse._quote_removal(m[4]), _expands(part.parts)))
            stack.extend(part.parts or ())


def _spans(n, length):
    """Границы (k, j) срезов n элементов: хвосты [k:n] с любого k, при length — все отрезки [k:j] (срез с длиной
    `:L` обрезает хвост, и отброшенный конец может быть `--dry-run`)."""
    for k in range(n + 1):
        for j in range(k, n + 1) if length else (n,):
            yield k, j


def _capped(pairs):
    """Пары pairs без повторов; список обрывается, когда его тексты длиннее _NAME_BUDGET: _renamed тратит на каждое
    значение не меньше его длины и символа, и полный список исчерпал бы остаток так же — сомнением."""
    out, size = {}, 0
    for pair in pairs:
        if pair not in out:
            out[pair] = None
            size += len(pair[0]) + 1
            if size > _NAME_BUDGET:
                break
    return list(out)


def _positional(sets, shifts, key):
    """Значения позиционных параметров key ("@" — все, "@:" — срез `${@:N}`, "@::" — срез с длиной `${@:N:L}`, номер
    "1", "2", …) по словам `set` sets (_set_args) после сдвигов `shift` на сумму shifts: параметры с k-го для каждого
    k от 0 до shifts — место сдвига в тексте не сверяется, — у среза любой хвост, у среза с длиной любой отрезок
    (_spans). Одинаковые пары не повторяются, список обрезает _capped."""
    index = (int(key) - 1 if len(key) < 6 else _WORDS_LIMIT) if key.isdecimal() else None

    def pairs():
        for args in sets:
            if key in ("@:", "@::"):
                spans = _spans(len(args), key == "@::")
            else:
                spans = ((k, len(args)) for k in range(min(shifts, len(args)) + 1))
            for k, j in spans:
                if index is None:
                    run = args[k:j]
                    yield " ".join(run), any(getattr(w, "expands", False) for w in run)
                elif k + index < len(args):
                    yield args[k + index], getattr(args[k + index], "expands", False)
                else:
                    break
    return _capped(pairs())


def _slices(pairs, length):
    """Значения среза массива `${a[@]:N}` (при length — `${a[@]:N:L}`) по значениям массива pairs: отрезки _spans
    по словам текста значения между пробельными символами — среди них все отрезки элементов (кавычки внутри
    элемента дают лишние отрезки, разбор их отвергает); вычисляемое значение остаётся вычисляемым. Список обрезает
    _capped."""
    def runs():
        for text, computed in pairs:
            words = [m.span() for m in re.finditer(r"\S+", text)]
            for k, j in _spans(len(words), length):
                yield (text[words[k][0]:words[j - 1][1]] if j > k else ""), computed
    return _capped(runs())


# Предел глубины ссылок, чьи значения подставляет _ref_values: цепочка `X=$Y; Y=$X` замкнута.
_REF_DEPTH = 4


def _key_values(key, values, depth=0):
    """Значения имени key из values. Позиционные параметры ("@" и "*" — все, номер — и с ведущими нулями `${01}`;
    `$0` — не позиционный, значений нет) считает _positional с запоминанием в values под ключом (_POSITIONAL, key):
    цель `read 1`, `printf -v @` или цикла с таким именем их не подменяет. Значение переменной — ссылку целиком
    (`Y=$X`, `Y="${X}"`, `Y=${X:-q}`) — заменяют значения ссылки (_ref_values на глубине depth): текст значения
    литерала `Y='$X'` от ссылки не отличим и тоже заменяется (лишнее сомнение); список запоминается по имени и
    глубине и обрезается _capped."""
    if key in ("@", "*"):
        key = "@"
    elif key.isdecimal():
        key = key.lstrip("0")
        if not key:
            return []
    if key[0] in "@123456789":
        cache = (_POSITIONAL, key)
        if cache not in values:
            values[cache] = _positional(*values[_POSITIONAL], key)
        return values[cache]
    cache = ("var", key, depth)
    if cache not in values:
        values[cache] = _capped(chain.from_iterable(
            _ref_values(text, values, depth) or [(text, computed)]
            for text, computed in values.get(key, ())))
    return values[cache]


def _ref_values(text, values, depth):
    """Значения текста text — ссылки целиком (и в "…") на переменную, элемент или срез массива, позиционные параметры
    или ссылки с оператором (`$Y`, `${Y}`, `"${Y[@]}"`, `${Y:-q}`) — по values (_var_values) на глубине depth + 1.
    Пусто — text не такая ссылка, значений нет в тексте команды или глубина достигла _REF_DEPTH."""
    if depth >= _REF_DEPTH:
        return []
    if len(text) > 1 and text[0] == text[-1] == '"' and '"' not in text[1:-1]:
        text = text[1:-1]
    return _var_values(text, values, depth + 1) if text[:1] == "$" else []


def _single_param(word):
    """Раскрытие `${…}`, из которого целиком состоит слово word (и в "…"): узел shparse.Param; у слова без узла —
    само слово; None — в слове есть другие части."""
    node = getattr(word, "node", None)
    if node is None:
        return word
    parts = node.parts
    if len(parts) == 1 and type(parts[0]) is shparse.DQ:
        parts = parts[0].parts
    return parts[0] if len(parts) == 1 and type(parts[0]) is shparse.Param else None


def _var_values(word, values, depth=0):
    """Значения слова word — ссылки целиком на переменную, элемент или все элементы массива, позиционные параметры
    (_NAME_VAR) — из values (_key_values на глубине ссылок depth); у среза массива — его отрезки (_slices), у
    элемента `${a[N]}` — любой отрезок (_slices с длиной: слова массива и значение переменной целиком); у ссылки с
    оператором (_PARAM_OP) — то, что оператор может подставить (bash 5.3): `-`, `=` — значения имени и слово, `+` —
    слово и пустое значение, `?` — значения имени (иначе ошибка). Слово оператора — значения ссылки целиком
    (_ref_values: `${X:-$Y}`), иначе сырой текст с кавычками (его слова делит разбор), с раскрытием — вычисляемое.
    Пусто — слово не ссылка или значения нет в тексте."""
    param = _single_param(word)
    text = getattr(param, "text", param)
    m = _PARAM_OP.fullmatch(text) if text else None
    if m:
        key = ("op", text, depth)
        if key not in values:
            name, index, op, arg = m[1], m[2], m[3][-1], m[4]
            computed = _expands(param.parts) if type(param) is shparse.Param else "$" in arg or "`" in arg
            if op == "+":
                own = []
            elif index is not None and index not in ("@", "*"):
                own = _slices(_key_values(name, values, depth), True)
            else:
                own = _key_values(name, values, depth)
            if op in "-=+":
                own = own + (_ref_values(arg, values, depth) or [(arg, computed)])
            values[key] = own + ([("", False)] if op == "+" else [])
        return values[key]
    m = _ARRAY_SLICE.fullmatch(word)
    if m:
        key = ("slice", m[1], bool(m[2]), depth)
        if key not in values:
            values[key] = _slices(_key_values(m[1], values, depth), bool(m[2]))
        return values[key]
    m = _ARRAY_ITEM.fullmatch(word)
    if m:
        key = ("item", m[1], depth)
        if key not in values:
            values[key] = _slices(_key_values(m[1], values, depth), True)
        return values[key]
    m = _NAME_VAR.fullmatch(word)
    if not m:
        return []
    return _key_values(m[1] or m[2] or m[3] or ("@::" if m[5] else "@:"), values, depth)


def _valued_ref(text, values):
    """Раскрытие параметра с текстом text ссылается на имя со значениями в тексте команды (values): переменную,
    массив, позиционные параметры, у косвенной `${!Y}` и ключей `${!a[@]}` — Y и a, у `${!начало*}` — переменную с
    таким началом. Длина `${#…}` — число, не ссылка на значение."""
    m = _NAMES_BY_PREFIX.fullmatch(text)
    if m:
        return any(type(key) is str and key.startswith(m[1]) for key in values)
    m = _REF.match(text)
    return bool(m) and m[1] != "#" and bool(_key_values(m[2], values))


def _composed(word, values):
    """Значения слова word (узел shparse.Word), склеенного из литералов и ссылок на переменные (`${X}ash`,
    `$R/bin/run`, `${a[@]:1}ash`; части в "…" — тоже): сочетания значений частей (_var_values) склейкой, литерал — в
    кавычках shlex, ссылка без значения в тексте — пустая и вычисляемая (значение из окружения не видно). Пусто — у
    слова нет узла, в нём подстановка или арифметика (имя команды из них проверяет _name_doubt) или ни у одной
    ссылки нет значений в тексте. None — значение известно только при исполнении: в слове ссылка, которую разбор не
    вычисляет (замена, обрезка, подстрока, смена регистра, преобразование `@…`, косвенная `${!…}`), на имя со
    значениями в тексте (_valued_ref), или тексты сочетаний длиннее остатка _NAME_BUDGET вызова."""
    node = getattr(word, "node", None)
    if node is None:
        return []
    options, valued, exact = [], False, True
    stack = list(reversed(node.parts))
    while stack:
        part = stack.pop()
        kind = type(part)
        if kind is shparse.DQ:
            stack.extend(reversed(part.parts))
        elif kind is shparse.Param:
            own = _var_values(part.text, values)
            if not own and _valued_ref(part.text, values):
                return None
            valued = valued or bool(own)
            options.append(own or [("", True)])
        elif kind is shparse.Lit or kind is shparse.SQ:
            options.append([(shlex.quote(part.text), False)])
        elif kind is shparse.AnsiC:
            options.append([(shlex.quote(part.value), False)])
        else:
            exact = False
    if not valued or not exact:
        return []
    # Сочетания одинаковых значений не растят список, но тратят время: остаток _NAME_BUDGET вызова тратит каждое.
    out, budget = {}, _call[2] if _call is not None else [_NAME_BUDGET]
    for combo in product(*(dict.fromkeys(o) for o in options)):
        text = "".join(t for t, _ in combo)
        budget[0] -= len(text) + 1
        if budget[0] < 0:
            return None
        out[text, any(c for _, c in combo)] = None
    return list(out)


def _renamed(info, values, depth):
    """Довод сомнения, если имя команды — переменная без кавычек со значением из текста (`X="npm install x"; $X`,
    массив `"${a[@]}"`, позиционные параметры `set` и `"$@"`, `$1`), alias из текста (`alias n=npm` ⏎
    `n install x`) или шаблон имён, под который подходит имя известной программы (`/usr/bin/np? install x`) или
    имя из семейств _NAME_FAMILIES (`pi[p] install x`, _glob_name), и с ним слова за именем — с подставленными
    значениями слов-ссылок на переменные (_var_values, `$X $Y`) — ставят пакет: известное значение разбирается как
    команда (_installs), вычисляемое (`X=$(…)`, `read`, неизвестный вывод `printf -v`) — по словам текста
    (_flat_install). Значение переменной из окружения не видно: сомнения нет. Тексты сверх остатка _NAME_BUDGET
    вызова — сомнение без разбора: проход линеен. Слово, склеенное из ссылок и литералов (`${X}ash`), даёт сочетания
    значений частей (_composed); имя из ссылки, которую разбор не вычисляет (`${X/a/b}`, `${X,,}`, `${!Y}`), на имя
    со значениями в тексте и имя со сочетаниями сверх остатка у _composed — сомнение _WHY_RUNTIME_NAME; такое слово
    за именем остаётся своим текстом, и строка проверяется ещё как команда с раскрытием (_find_doubt: `npm ${Y,,}` —
    подкоманда неизвестна)."""
    words = info.words
    texts = _var_values(words[0], values) or _composed(words[0], values)
    if texts is None:
        return _WHY_RUNTIME_NAME
    texts = list(texts)
    texts += values.get(("alias", words[0]), ())
    node = getattr(words[0], "node", None)
    if node is not None and any(type(p) is shparse.Lit and _globs(info.text, p) for p in node.parts):
        pattern = pkgmanagers._basename(words[0])
        texts += [(name, False) for name in _GLOB_PROGRAMS if fnmatch.fnmatchcase(name, pattern)]
        if len(pattern) <= _NAME_MAX:
            texts += [(name, False) for name in map(lambda f: _glob_name(pattern, f), _NAME_FAMILIES) if name]
    if not texts:
        return None
    options = []
    for w in words[1:]:
        own = _var_values(w, values) or _composed(w, values)
        # Значение известно только при исполнении (None у _composed): слово — своим текстом, признак None.
        options.append(own or [(pkgmanagers._shell_join([w]), None if own is None else False)])
    budget = _call[2] if _call is not None else [_NAME_BUDGET]
    for text, computed in texts:
        for rest in product(*options):
            # Значений, их сочетаний и мест имени может быть много: текст сверх остатка _NAME_BUDGET — сомнение без
            # разбора. Место с пустым текстом (`X=`) тоже тратит остаток.
            line = " ".join([text] + [t for t, _ in rest])
            budget[0] -= len(line) + 1
            if budget[0] < 0:
                return _WHY_RENAMED
            if any(c is None for _, c in rest) and (depth >= _MAX_DEPTH or _find_doubt(line, depth + 1)):
                return _WHY_RENAMED
            if computed or any(c for _, c in rest):
                if _flat_install([line]):
                    return _WHY_RENAMED
            elif _installs(line, depth):
                return _WHY_RENAMED
    return None


def _segment_doubt(info, depth):
    """Довод сомнения команды: её слова (_doubt; имя команды — вывод подстановки, _name_doubt, — _WHY_NAME на месте,
    где _doubt проверяет имя), вычисляемая строка `eval` или here-string (_computed_script), строки _scripts — как в
    _segment_adds (на глубине _MAX_DEPTH вложенный текст проверяет _soup_add), последней — строка в словах, которую
    программа может исполнить (_string_doubt)."""
    if info.marker:
        return None
    why = _doubt(info.words, info.xargs, info.cut, info.wrapped)
    if why in (None, _WHY_SUBCOMMAND, _WHY_LAUNCHER) and _name_doubt(info):
        why = _WHY_NAME
    why = why or _computed_script(info)
    if why:
        return why
    nested = _scripts(info)
    if depth >= _MAX_DEPTH:
        if any(_soup_add(t) for t in nested):
            return _WHY_DEPTH
    else:
        for text in nested:
            found = _find_doubt(text, depth + 1)
            if found:
                return found[1]
    return _string_doubt(info, depth)


def _find_doubt(command, depth):
    """Первый сегмент текста с сомнением и довод (_segment_doubt, затем тела heredoc оболочки — с внешним раскрытием
    _WHY_COMPUTED — и _stdin_scripts: вход оболочки не из литерального `echo`/`printf`, heredoc `cat` и файлов —
    _WHY_COMPUTED, затем входы программ _program_inputs — _input_doubt); None, если такого нет."""
    commands, feeds, scripts, loops = _analysis(command)
    values = None
    for info in commands:
        why = _segment_doubt(info, depth)
        if not why and not info.marker and info.words:
            if values is None:
                values = _values(commands, loops)
            why = _renamed(info, values, depth)
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
    for segment, text in _program_inputs(commands, feeds):
        why = _input_doubt(text, depth)
        if why:
            return segment, why
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
    наборов, `--dry-run` значением флага, пакеты или слова команды из stdin xargs (_xargs_doubt), вложенность
    глубже _MAX_DEPTH, слова за пределом раскрытия скобок, слово менеджера или строка `eval`/`sh -c` за _WORDS_LIMIT (_installer, _cut_script), команда
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
