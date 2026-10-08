"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import re
from collections.abc import Set

DEP_OK_MARKER = "PLANKA_DEP_OK=1"

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Ключевые слова shell перед командой.
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
# Обёртки, запускающие следующую команду: флаги со значением и число позиционных слов обёртки
# (длительность у timeout) перед командой.
_WRAPPERS = {
    "time": (frozenset(), 0),
    "nohup": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "env": (frozenset({"-u", "--unset", "-C", "--chdir"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "sudo": (frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    # Пакеты из stdin xargs не видны: проверяются только слова самой команды.
    "xargs": (frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--arg-file", "--delimiter",
                         "--max-args", "--max-procs", "--max-lines", "--max-chars", "--eof",
                         "--process-slot-var"}), 0),
}
# Флаг env, чьё значение — строка команды, разбиваемая на слова.
_ENV_SPLIT = ("-S", "--split-string")
# Оператор перенаправления в начале слова: `2>&1`, `>log`, `&>/dev/null`, `<<EOF`; без цели в слове
# (`>`, `2>`) цель — следующее слово.
_REDIRECT = re.compile(r"^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:&>>|&>|>>|>&|>\||>|<<<|<<-|<<|<&|<>|<)")
# Команды, которым тело heredoc передаётся как команды, если у них нет скрипта и `-c`.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ssh"}
# Оболочки, исполняющие строку после `-c`.
_C_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_PIP = re.compile(r"pip\d*(?:\.\d+)*$")
_PYTHON = re.compile(r"(?:python|pypy)\d*(?:\.\d+)*$|py$")
# Флаги интерпретатора Python со значением.
_PYTHON_VALUE_FLAGS = {"-W", "-X"}
# Суффиксы исполняемых файлов Windows: `npm.cmd` — это `npm`.
_WIN_EXT = re.compile(r"\.(?:exe|cmd|bat|ps1)$", re.IGNORECASE)
# Глубина разбора вложенных команд: `sh -c`, `eval`, подстановка в двойных кавычках.
_MAX_DEPTH = 4

# Флаги, чьё следующее слово — значение, а не пакет; наборы по менеджерам. Они же — глобальные
# флаги перед подкомандой.
_NPM_VALUE_FLAGS = {"--prefix", "-C", "--dir", "--filter", "-w", "--workspace", "--registry", "--tag", "--cache",
                    "--omit", "--include", "--loglevel", "--before", "--install-strategy", "--audit-level",
                    "--userconfig", "--globalconfig", "--location", "--save-prefix", "--os", "--cpu", "--libc"}
_PNPM_VALUE_FLAGS = {"--prefix", "-C", "--dir", "--filter", "-F", "--registry", "--reporter", "--loglevel",
                     "--store-dir", "--virtual-store-dir"}
_YARN_VALUE_FLAGS = {"--cwd", "--registry", "--modules-folder", "--cache-folder", "--mutex", "--network-timeout"}
_BUN_VALUE_FLAGS = {"--cwd", "-c", "--config", "--registry", "--cache-dir", "--backend", "--omit", "--filter", "-F",
                    "--network-concurrency", "--concurrent-scripts", "--ca", "--cafile", "--linker", "--cpu", "--os",
                    "--minimum-release-age"}
# Флаги deno с необязательным значением (`--lock`, `--check`, `--env-file`) значение берут только через `=`.
_DENO_VALUE_FLAGS = {"-c", "--config", "--cert", "--import-map", "-n", "--name", "--root", "-L", "--log-level",
                     "--location", "--seed"}
# Общие опции pip со значением (`pip --help`, General Options): стоят и перед подкомандой.
_PIP_GLOBAL_VALUE_FLAGS = {"--python", "--log", "--log-file", "--keyring-provider", "--proxy", "--retries",
                           "--timeout", "--exists-action", "--trusted-host", "--cert", "--client-cert",
                           "--cache-dir", "--use-feature", "--use-deprecated", "--resume-retries"}
_PIP_VALUE_FLAGS = _PIP_GLOBAL_VALUE_FLAGS | {
    "-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
    "-t", "--target", "--prefix", "--root", "-e", "--editable", "--platform", "--python-version",
    "--implementation", "--abi", "--upgrade-strategy", "--only-binary", "--no-binary", "--src", "--progress-bar",
    "--config-settings", "-C", "--report", "--global-option", "--root-user-action", "--target-dir", "--group",
}
# Наборы uv сверены с `--help` подкоманд uv 0.12.
_UV_VALUE_FLAGS = {"--directory", "--project", "--python", "-p", "--cache-dir", "--config-file", "--color",
                   "--allow-insecure-host", "--cert"}
# Опции индекса, разрешения и сборки, общие для `uv pip install`, `uv add`, `uv tool install`, `uv run`.
_UV_INDEX_FLAGS = {"--index", "--default-index", "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
                   "--index-strategy", "--keyring-provider", "--allow-insecure-host", "-C", "--config-setting",
                   "--config-settings-package", "--prerelease", "--prerelease-package", "--resolution",
                   "--fork-strategy", "--exclude-newer", "--exclude-newer-package", "--link-mode",
                   "--python-platform", "-P", "--upgrade-package", "--upgrade-group", "--reinstall-package",
                   "--refresh-package", "--no-binary-package", "--no-build-package", "--no-sources-package",
                   "--no-build-isolation-package"}
_UV_PIP_VALUE_FLAGS = _PIP_VALUE_FLAGS | _UV_VALUE_FLAGS | _UV_INDEX_FLAGS | {
    "--requirements", "--constraints", "--overrides", "--excludes", "-b", "--build-constraints", "--extra",
    "--no-editable-package", "--output-format", "--torch-backend"}
_UV_ADD_VALUE_FLAGS = _UV_VALUE_FLAGS | _UV_INDEX_FLAGS | {
    "-r", "--requirements", "-c", "--constraints", "-m", "--marker", "--optional", "--group", "--bounds", "--rev",
    "--tag", "--branch", "--extra", "--package", "--script", "--no-install-package"}
_UV_TOOL_VALUE_FLAGS = _UV_VALUE_FLAGS | _UV_INDEX_FLAGS | {
    "--with", "-w", "--with-requirements", "--with-editable", "--with-executables-from", "--from", "-c",
    "--constraints", "--overrides", "--excludes", "-b", "--build-constraints", "--torch-backend"}
_UV_RUN_VALUE_FLAGS = _UV_VALUE_FLAGS | _UV_INDEX_FLAGS | {
    "--with", "-w", "--with-requirements", "--with-editable", "--package", "--extra", "--group", "--only-group",
    "--no-group", "--no-extra", "--env-file", "--no-editable-package"}
_CARGO_VALUE_FLAGS = {"--path", "--git", "--registry", "--branch", "--tag", "--rev", "--features", "-F",
                      "--package", "-p", "--manifest-path", "--rename", "--target", "--config", "-Z", "-C"}
_CARGO_INSTALL_VALUE_FLAGS = _CARGO_VALUE_FLAGS | {"--version", "--vers", "--root", "--index", "--target-dir",
                                                   "--profile", "-j", "--jobs", "--bin", "--example", "--color"}
_POETRY_VALUE_FLAGS = {"--group", "-G", "--source", "--extras", "-E", "--python", "--platform", "--markers",
                       "--directory", "-C", "--project", "-P"}
_BUNDLE_VALUE_FLAGS = {"--version", "-v", "--source", "-s", "--group", "-g", "--require", "-r", "--git",
                       "--branch", "--ref", "--path", "--github", "--glob"}
_GEM_VALUE_FLAGS = {"-v", "--version", "--source", "-s", "-i", "--install-dir", "-n", "--bindir", "--platform",
                    "-P", "--trust-policy"}
_COMPOSER_VALUE_FLAGS = {"--with", "--working-dir", "-d"}
# `composer require`, его псевдоним `r` и однозначные сокращения имени (Symfony Console, от `req`).
_COMPOSER_REQUIRE = {"r", "req", "requ", "requi", "requir", "require"}
_GO_VALUE_FLAGS = {"-C", "-modfile"}
_GO_BUILD_VALUE_FLAGS = _GO_VALUE_FLAGS | {"-o", "-p", "-mod", "-tags", "-ldflags", "-gcflags", "-asmflags",
                                           "-gccgoflags", "-buildmode", "-compiler", "-installsuffix", "-pkgdir",
                                           "-toolexec", "-overlay", "-pgo", "-coverpkg", "-covermode"}
_PIPX_VALUE_FLAGS = {"--suffix", "--python", "--pip-args", "-i", "--index-url", "--preinstall", "--backend",
                     "-r", "--requirement", "--spec"}
_PIPENV_VALUE_FLAGS = {"-r", "--requirements", "--python", "--pypi-mirror", "-i", "--index", "--categories",
                       "--extra-pip-args", "-e", "--editable"}
_CONDA_VALUE_FLAGS = {"-n", "--name", "-p", "--prefix", "-c", "--channel", "--file", "-f", "--revision",
                      "--repodata-fn", "--solver", "--experimental-solver", "--subdir", "--platform", "-r",
                      "--root-prefix", "--rc-file", "--channel-priority", "--cert"}
_CONDA_RUN_VALUE_FLAGS = {"-n", "--name", "-p", "--prefix", "--cwd", "-r", "--root-prefix", "-a", "--attach"}
_DOTNET_ADD_VALUE_FLAGS = {"-v", "--version", "-f", "--framework", "-s", "--source", "--package-directory",
                           "--project"}
_DOTNET_TOOL_VALUE_FLAGS = {"--tool-path", "--version", "--add-source", "--configfile", "--framework", "-a",
                            "--arch", "--tool-manifest", "-v", "--verbosity"}
_PUB_VALUE_FLAGS = {"-C", "--directory", "--git-url", "--git-ref", "--git-path", "--hosted-url", "--path", "--sdk",
                    "-s", "--source", "-x", "--executable"}
_SWIFT_PACKAGE_VALUE_FLAGS = {"--package-path", "--scratch-path", "--build-path", "--cache-path", "--config-path",
                              "--security-path", "--swift-sdks-path", "--toolset", "--pkg-config-path", "-j",
                              "--jobs", "-c", "--configuration"}
_SWIFT_ADD_VALUE_FLAGS = {"--exact", "--revision", "--branch", "--from", "--up-to-next-minor-from", "--to",
                          "--type"}
_NO_VALUE_FLAGS = frozenset()
_ADD_FLAGS = {"poetry": _POETRY_VALUE_FLAGS, "cargo": _CARGO_VALUE_FLAGS, "bundle": _BUNDLE_VALUE_FLAGS}
# Глобальные флаги менеджера перед подкомандой.
_GLOBAL_FLAGS = {"npm": _NPM_VALUE_FLAGS, "pnpm": _PNPM_VALUE_FLAGS, "yarn": _YARN_VALUE_FLAGS,
                 "bun": _BUN_VALUE_FLAGS, "deno": _DENO_VALUE_FLAGS,
                 "uv": _UV_VALUE_FLAGS, "poetry": _POETRY_VALUE_FLAGS, "cargo": _CARGO_VALUE_FLAGS,
                 "composer": _COMPOSER_VALUE_FLAGS, "bundle": _BUNDLE_VALUE_FLAGS, "go": _GO_VALUE_FLAGS,
                 "pipenv": _PIPENV_VALUE_FLAGS, "pipx": _NO_VALUE_FLAGS, "dotnet": _NO_VALUE_FLAGS,
                 "dart": _NO_VALUE_FLAGS, "flutter": _NO_VALUE_FLAGS}
# Подкоманда `run`, исполняющая следующую команду в окружении проекта: флаги со значением перед ней.
_RUNNERS = {"uv": _UV_RUN_VALUE_FLAGS, "poetry": _NO_VALUE_FLAGS, "pipenv": _NO_VALUE_FLAGS,
            "conda": _CONDA_RUN_VALUE_FLAGS, "mamba": _CONDA_RUN_VALUE_FLAGS,
            "micromamba": _CONDA_RUN_VALUE_FLAGS, "pdm": _NO_VALUE_FLAGS, "rye": _NO_VALUE_FLAGS,
            "hatch": _NO_VALUE_FLAGS, "pixi": {"--manifest-path", "-e", "--environment"}}
_CONDAS = {"conda", "mamba", "micromamba"}
# Псевдонимы `npm install`.
_NPM_INSTALL = {"install", "i", "add", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal",
                "isntall", "it", "install-test"}

# Локальные архивы пакетов.
_ARCHIVES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".gem", ".conda", ".deb",
             ".rpm", ".apk", ".snap", ".flatpak", ".nupkg", ".rock", ".rockspec", ".ez")
# Именованное требование на локальный путь: `name@file:///x`, `name @ file:x`.
_NAMED_FILE = re.compile(r"@\s*file:")
# Сколько первых символов сегмента _command разбирает на слова: пакет, названный дальше, не виден.
_WORDS_LIMIT = 4096


# Склейка коротких флагов одним словом: `-qr`, `-euo`, `+eo`.
_GLUED = re.compile(r"[-+][A-Za-z]{2,}")


def _takes_value(flag, value_flags):
    """Следующее слово — значение флага: флаг из value_flags или склейка коротких флагов, последний из
    которых в value_flags (`-qr` при `-r`)."""
    return flag in value_flags or bool(_GLUED.fullmatch(flag)) and flag[0] + flag[-1] in value_flags


def _basename(word):
    return _WIN_EXT.sub("", re.split(r"[/\\]", word)[-1])


def _split(text):
    """Слова по правилам POSIX shell: кавычки и `\\` сняты. Каждое слово — пара (слово, начало слова
    до первой кавычки или `\\`): оператор перенаправления узнаётся только в этом начале.

    Незакрытая кавычка продолжается до конца текста.
    """
    out = []
    buf, lead, started = [], None, False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            if started:
                word = "".join(buf)
                out.append((word, word if lead is None else lead))
                buf, lead, started = [], None, False
            i += 1
            continue
        started = True
        if c in "'\"\\" or c == "$" and text.startswith("'", i + 1):
            if lead is None:
                lead = "".join(buf)
        if c == "'":
            end = text.find("'", i + 1)
            end = n if end < 0 else end
            buf.append(text[i + 1:end])
            i = end + 1
        elif c == "$" and text.startswith("'", i + 1):
            i += 2
            while i < n and text[i] != "'":
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '$`"\\':
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1
        elif c == "\\":
            buf.append(text[i + 1:i + 2])
            i += 2
        else:
            buf.append(c)
            i += 1
    if started:
        word = "".join(buf)
        out.append((word, word if lead is None else lead))
    return out


def _drop_redirects(pairs):
    """Слова без перенаправлений и их целей; оператор в кавычках (`">"`) — обычное слово."""
    out = []
    i = 0
    while i < len(pairs):
        word, lead = pairs[i]
        m = _REDIRECT.match(lead)
        if m:
            i += 1 if len(word) > m.end() else 2
            continue
        out.append(word)
        i += 1
    return out


def _command(segment):
    """Слова команды сегмента и стоит ли маркер согласия среди её ведущих присваиваний.

    Перенаправления с их целью выброшены; ведущие присваивания, ключевые слова shell и обёртки
    (`sudo`, `env`, `timeout`, `nice`, `xargs`, `stdbuf` и др.) с их флагами сняты; строка `env -S`
    разбита на слова. Комментарий из сегмента уже убран _segments.
    """
    words = _drop_redirects(_split(segment[:_WORDS_LIMIT]))
    marker = False
    while words:
        word = words[0]
        if _ENV_ASSIGN.match(word):
            marker = marker or word == DEP_OK_MARKER
            words.pop(0)
        elif word in _KEYWORDS:
            words.pop(0)
        elif _basename(word) in _WRAPPERS:
            name = _basename(word)
            value_flags, operands = _WRAPPERS[name]
            words.pop(0)
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag == "--":
                    break
                if name == "env" and (flag in _ENV_SPLIT or flag.startswith(("-S", "--split-string="))):
                    if flag in _ENV_SPLIT:
                        value = words.pop(0) if words else ""
                    else:
                        value = flag[2:] if flag.startswith("-S") else flag.partition("=")[2]
                    words[:0] = _drop_redirects(_split(value))
                    continue
                if _takes_value(flag, value_flags) and words:
                    words.pop(0)
            del words[:operands]
        else:
            break
    return words, marker


def _is_local(word, pip):
    """Слово — локальный путь или архив, а не пакет; URL со схемой, кроме `file:`, — пакет."""
    if word.startswith("file:") or _NAMED_FILE.search(word):
        return True
    if "://" in word:
        return False
    if word.startswith((".", "/", "~")) or word.endswith(_ARCHIVES):
        return True
    # У pip слово со `/` — путь; у npm `user/repo` — пакет с GitHub.
    return pip and "/" in word


def _join_at(args):
    """Требование pip `name @ url`, разбитое пробелами, — одно слово `name@url`."""
    out = []
    for a in args:
        if out and not out[-1].startswith("-") and (a.startswith("@") or out[-1].endswith("@")):
            out[-1] += a
        else:
            out.append(a)
    return out


def _positionals(args, value_flags: Set[str] = _NO_VALUE_FLAGS, pip=False):
    """Слова-аргументы, похожие на имя пакета: не флаг, не значение флага, не локальный путь или архив."""
    skip = False
    for w in args:
        if skip:
            skip = False
            continue
        if w.startswith("-"):
            skip = _takes_value(w, value_flags)
            continue
        if not w or _is_local(w, pip):
            continue
        yield w


def _positional(args, value_flags: Set[str] = _NO_VALUE_FLAGS):
    return next(_positionals(args, value_flags), None)


def _has(args, value_flags: Set[str] = _NO_VALUE_FLAGS):
    return _positional(args, value_flags) is not None


# Установщик и его спутники в venv пакетов в проект не добавляют.
_PIP_BOOTSTRAP = {"pip", "setuptools", "wheel"}
# Значение `-e`, указывающее на репозиторий или URL, а не на каталог.
_VCS_OR_URL = re.compile(r"(?:git|hg|svn|bzr)\+|[A-Za-z][A-Za-z0-9+.-]*://")
# Имя проекта в требовании pip кончается на первом спецификаторе версии, extras, маркере или URL.
_PIP_NAME_END = re.compile(r"[<>=!~\[;@]")


def _vcs_editable(args):
    """`-e git+https://…` — пакет из репозитория; `-e .` и `-e mypkg` — локальный каталог."""
    return any(flag in ("-e", "--editable") and _VCS_OR_URL.match(value) and not _is_local(value, True)
               for flag, value in zip(args, args[1:]))


def _pip_add(args, value_flags: Set[str] = _PIP_VALUE_FLAGS):
    if _vcs_editable(args):
        return True
    names = {_PIP_NAME_END.split(w, 1)[0].strip().lower()
             for w in _positionals(_join_at(args), value_flags, pip=True)}
    return bool(names - _PIP_BOOTSTRAP)


def _subcommand(words, value_flags):
    """Слова с подкоманды: глобальные флаги менеджера перед ней (`pnpm -C sub add`) пропускаются."""
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if _takes_value(words[i], value_flags) else 1
    return [words[0], *words[i:]]


def _after_flags(args, value_flags):
    """Слова с первого позиционного: флаги и их значения сняты, `--` завершает флаги."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--":
            return args[i + 1:]
        i += 2 if _takes_value(args[i], value_flags) else 1
    return args[i:]


def _python_module(w):
    """Слова `python -m <модуль> …` с модуля; None, если модуль не запускается. Короткие флаги склеиваются
    (`-Im pip`, `-Impip`); значение `-W`/`-X` — остаток слова или следующее слово."""
    i = 1
    while i < len(w) and w[i].startswith("-"):
        a = w[i]
        if a == "-":
            return None
        if a.startswith("--"):
            i += 1
            continue
        step = 1
        for j, letter in enumerate(a[1:], 1):
            rest = a[j + 1:]
            if letter == "m":
                return [rest, *w[i + 1:]] if rest else w[i + 1:]
            if letter == "c":
                return None
            if "-" + letter in _PYTHON_VALUE_FLAGS:
                step = 1 if rest else 2
                break
        i += step
    return None


def _flag_value(args, flag):
    """Значение флага `--flag value` или `--flag=value`; None, если флага нет."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return None


def _git_source(args):
    """`--git <url>` — пакет из репозитория и без позиционного имени."""
    value = _flag_value(args, "--git")
    return value is not None and not _is_local(value, False)


def _is_add(words, depth=0):
    """Команда добавляет пакет: в проект или глобально (`npm i -g`, `cargo install`, `pipx install`).

    Разовый запуск без установки (`npx`, `bunx`, `uvx`, `pipx run`, `go run`) — не добавление.
    """
    if not words or depth > _MAX_DEPTH:
        return False
    w = [_basename(words[0]), *words[1:]]
    name = w[0]
    if _PYTHON.match(name):
        module = _python_module(w)
        return module is not None and _is_add(module, depth + 1)
    if name in _OTHER_MANAGERS:
        sub, args = _sub_args(w)
        if name in _RUNNERS and sub == "run":
            return _is_add(_after_flags(args, _RUNNERS[name]), depth + 1)
        return _OTHER_MANAGERS[name](w)
    # `cargo +nightly install …`: выбор toolchain rustup перед подкомандой.
    if name == "cargo" and w[1:2] and w[1].startswith("+"):
        w = [w[0], *w[2:]]
    if name in _GLOBAL_FLAGS:
        w = _subcommand(w, _GLOBAL_FLAGS[name])
    elif _PIP.match(name):
        w = _subcommand(w, _PIP_GLOBAL_VALUE_FLAGS)
    if name == "yarn" and len(w) > 2 and w[1] == "workspace":
        w = [w[0], *w[3:]]
    if name in ("poetry", "composer", "yarn") and len(w) > 1 and w[1] in ("self", "global"):
        w = [w[0], *w[2:]]
    sub = w[1] if len(w) > 1 else None
    args = w[2:]
    if sub is None:
        return False
    if name in _RUNNERS and sub == "run":
        return _is_add(_after_flags(args, _RUNNERS[name]), depth + 1)
    if name == "go":
        if sub == "get":
            return _has(args, _GO_VALUE_FLAGS)
        if sub == "install":
            # `go install` без версии собирает пакет из go.mod проекта.
            return any("@" in p for p in _positionals(args, _GO_BUILD_VALUE_FLAGS))
        return False
    if name == "npm":
        return sub in _NPM_INSTALL and _has(args, _NPM_VALUE_FLAGS)
    if name == "pnpm":
        return sub in ("add", "install", "i") and _has(args, _PNPM_VALUE_FLAGS)
    if name == "yarn":
        return sub == "add" and _has(args, _YARN_VALUE_FLAGS)
    if name == "bun":
        return sub in ("add", "a", "install", "i") and _has(args, _BUN_VALUE_FLAGS)
    if name == "deno":
        # `--entrypoint` кэширует зависимости локальных файлов.
        if sub not in ("add", "install", "i") or _flag_value(args, "--entrypoint") is not None or "-e" in args:
            return False
        return _has(args, _DENO_VALUE_FLAGS)
    if _PIP.match(name):
        return sub == "install" and _pip_add(args)
    if name == "uv":
        if sub == "add":
            return _has(args, _UV_ADD_VALUE_FLAGS)
        if sub == "pip":
            args = _subcommand(["pip", *args], _UV_PIP_VALUE_FLAGS)[1:]
            return args[:1] == ["install"] and _pip_add(args[1:], _UV_PIP_VALUE_FLAGS)
        if sub == "tool" and args[:1] == ["install"]:
            return _pip_add(args[1:], _UV_TOOL_VALUE_FLAGS)
        return False
    if name == "pipx":
        if sub == "install":
            return _pip_add(args, _PIPX_VALUE_FLAGS)
        if sub == "inject":
            # Первое позиционное — имя окружения pipx.
            return len(list(_positionals(_join_at(args), _PIPX_VALUE_FLAGS, pip=True))) > 1
        return False
    if name == "pipenv":
        return sub == "install" and _pip_add(args, _PIPENV_VALUE_FLAGS)
    if name in _CONDAS:
        return sub == "install" and _has(args, _CONDA_VALUE_FLAGS)
    if name == "cargo":
        if sub == "install":
            return _has(args, _CARGO_INSTALL_VALUE_FLAGS) or _git_source(args)
        return sub == "add" and (_has(args, _CARGO_VALUE_FLAGS) or _git_source(args))
    if name in _ADD_FLAGS:
        return sub == "add" and _has(args, _ADD_FLAGS[name])
    if name == "gem":
        return sub == "install" and _has(args, _GEM_VALUE_FLAGS)
    if name == "composer":
        return sub in _COMPOSER_REQUIRE and _has(args, _COMPOSER_VALUE_FLAGS)
    if name == "dotnet":
        if sub == "add":
            # `dotnet add [<проект>] package <пакет>`; `add reference` — ссылка на проект.
            if args[1:2] == ["package"] and args[:1] != ["package"]:
                args = args[1:]
            return args[:1] == ["package"] and _has(args[1:], _DOTNET_ADD_VALUE_FLAGS)
        if sub == "package":
            return args[:1] == ["add"] and _has(args[1:], _DOTNET_ADD_VALUE_FLAGS)
        if sub == "tool":
            return args[:1] == ["install"] and _has(args[1:], _DOTNET_TOOL_VALUE_FLAGS)
        return False
    if name in ("dart", "flutter"):
        if sub != "pub":
            return False
        if args[:1] == ["add"]:
            return _has(args[1:], _PUB_VALUE_FLAGS)
        return args[:2] == ["global", "activate"] and _has(args[2:], _PUB_VALUE_FLAGS)
    if name == "swift" and sub == "package":
        w = _subcommand([name, *args], _SWIFT_PACKAGE_VALUE_FLAGS)
        return w[1:2] == ["add-dependency"] and _has(w[2:], _SWIFT_ADD_VALUE_FLAGS)
    return False


# Системные менеджеры и редкие языковые менеджеры: обработчик получает слова команды с именем.
# Установка по манифесту без названного пакета (`vcpkg install`, `conan install .`, `cpanm --installdeps .`),
# обновление, поиск, список и удаление — не добавление.

_APT_VALUE_FLAGS = {"-o", "--option", "-c", "--config-file", "-t", "--target-release", "--default-release",
                    "-a", "--host-architecture", "-P", "--build-profiles", "--solver"}
_APTITUDE_VALUE_FLAGS = _APT_VALUE_FLAGS | {"-F", "--display-format", "-w", "--width", "-S", "--sort"}
_DNF_VALUE_FLAGS = {"-c", "--config", "--repo", "--repoid", "--enablerepo", "--disablerepo", "--setopt",
                    "--installroot", "--releasever", "-d", "--debuglevel", "-e", "--errorlevel", "-x", "--exclude",
                    "--forcearch", "--downloaddir", "--destdir", "--comment", "--color", "--repofrompath",
                    "--advisory", "--advisories", "--bz", "--bzs", "--cve", "--cves", "--sec-severity",
                    "--secseverity", "--rpmverbosity", "-R", "--randomwait", "--from-repo", "--use-host-config",
                    "--setvar", "--exclude-from-repo"}
_ZYPPER_VALUE_FLAGS = {"-R", "--root", "-c", "--config", "-D", "--reposd-dir", "-C", "--cache-dir",
                       "--raw-cache-dir", "--solv-cache-dir", "--pkg-cache-dir", "-p", "--plus-repo",
                       "--plus-content", "--installroot", "--userdata", "-r", "--repo", "--from", "-t", "--type",
                       "--download"}
_PACMAN_VALUE_FLAGS = {"-r", "--root", "-b", "--dbpath", "--cachedir", "--config", "--gpgdir", "--hookdir",
                       "--logfile", "--arch", "--sysroot", "--ignore", "--ignoregroup", "--assume-installed",
                       "--overwrite", "--print-format", "--color"}
# Буквы при `-S`, с которыми pacman ищет, показывает, чистит кэш или только скачивает, а не ставит.
_PACMAN_SYNC_QUERY = frozenset("silgcpw")
_PACMAN_SYNC_QUERY_LONG = {"--search", "--info", "--list", "--groups", "--clean", "--print", "--downloadonly"}
_APK_VALUE_FLAGS = {"-X", "--repository", "-p", "--root", "-t", "--virtual", "--repositories-file", "--keys-dir",
                    "--cache-dir", "--arch", "--wait", "--timeout", "--cache-max-age", "--from"}
# Флаги choco и nuget сравниваются в нижнем регистре.
_CHOCO_VALUE_FLAGS = {"--version", "-s", "--source", "--params", "--package-parameters",
                      "--package-parameters-sensitive", "--ia", "--install-arguments",
                      "--install-arguments-sensitive", "--cache-location", "--cache", "--execution-timeout", "-u",
                      "--user", "-p", "--password", "--cert", "--cp", "--certpassword", "--checksum",
                      "--checksum64", "--checksumtype", "--checksum-type", "--download-checksum",
                      "--download-checksum-x64", "--download-checksum-type", "--log-file", "--timeout", "--proxy",
                      "--proxy-user", "--proxy-password", "--proxy-bypass-list"}
_NUGET_VALUE_FLAGS = {"-outputdirectory", "-o", "-version", "-source", "-configfile", "-framework",
                      "-fallbacksource", "-dependencyversion", "-packagesavemode", "-solutiondirectory",
                      "-verbosity", "-msbuildversion", "-msbuildpath"}
_WINGET_VALUE_FLAGS = {"-q", "--query", "--id", "--name", "--moniker", "-v", "--version", "-s", "--source",
                       "--scope", "-a", "--architecture", "--installer-type", "-l", "--location", "-o", "--log",
                       "--override", "--custom", "--locale", "--header", "--authentication-mode",
                       "--authentication-account", "-m", "--manifest", "--rename"}
# Флаги winget, чьё значение называет пакет.
_WINGET_NAME_FLAGS = ("--id", "--name", "--moniker", "-q", "--query")
_SCOOP_VALUE_FLAGS = {"-a", "--arch"}
_PORT_VALUE_FLAGS = {"-D", "-F"}
_SNAP_VALUE_FLAGS = {"--channel", "--revision", "--name", "--cohort", "--quota-group"}
_FLATPAK_VALUE_FLAGS = {"--arch", "--subpath", "--installation", "--sideload-repo", "--default-branch"}
_NIX_VALUE_FLAGS = {"-f", "--file", "-p", "--profile", "-I", "--include", "--max-jobs", "-j", "--cores",
                    "--store", "--eval-store", "--log-format", "--priority", "--extra-experimental-features",
                    "--experimental-features", "--expr", "-E"}
# Флаги nix с двумя значениями: `--arg имя выражение`.
_NIX_PAIR_FLAGS = {"--arg", "--argstr", "--option", "--override-input", "--override-flake", "--arg-from-file"}
_PDM_ADD_VALUE_FLAGS = {"-G", "--group", "-p", "--project", "-L", "--lockfile", "-S", "--strategy", "--venv",
                        "-e", "--editable", "-C", "--config-setting", "--override"}
_RYE_VALUE_FLAGS = {"--git", "--url", "--path", "--tag", "--rev", "--branch", "--features", "--optional", "--pin",
                    "--pyproject", "-p", "--python", "--include-dep", "--extra-requirement", "-i", "--index-url",
                    "--extra-index-url"}
_PIXI_VALUE_FLAGS = {"--manifest-path", "-p", "--platform", "-f", "--feature", "-g", "--git", "--branch", "--tag",
                     "--rev", "-s", "--subdir", "--concurrent-downloads", "--concurrent-solves", "-c", "--channel",
                     "-e", "--environment", "--expose", "--with", "--path"}
_MIX_INSTALL_VALUE_FLAGS = {"--sha512", "--organization", "--repo", "--app", "--branch", "--ref", "--tag",
                            "--sparse", "--subdir"}
# Источник архива mix перед его именем: `mix archive.install hex phx_new`.
_MIX_SOURCES = {"hex", "github", "git"}
_CABAL_VALUE_FLAGS = {"--installdir", "--install-method", "--overwrite-policy", "-w", "--with-compiler",
                      "--with-hc-pkg", "--package-env", "--env", "--project-file", "--project-dir", "--constraint",
                      "--index-state", "-f", "--flags", "--ghc-options", "--program-suffix", "--program-prefix",
                      "--builddir", "--store-dir", "--config-file", "--prefix", "--with-gcc", "--with-ld",
                      "--repl-options"}
_STACK_VALUE_FLAGS = {"--stack-yaml", "--resolver", "--snapshot", "--compiler", "--work-dir", "--local-bin-path",
                      "--ghc-options", "--flag", "--ta", "--test-arguments", "--ba", "--benchmark-arguments",
                      "-j", "--jobs", "--arch", "--stack-root", "--docker-image", "--color", "--verbosity",
                      "--ghc-variant", "--ghc-build", "--with-gcc", "--with-hpack", "--extra-include-dirs",
                      "--extra-lib-dirs", "--setup-info-yaml"}
_OPAM_VALUE_FLAGS = {"--switch", "--root", "-j", "--jobs", "--criteria", "--solver", "--color", "--destdir",
                     "--cli", "--formula", "--ignore-constraints-on", "--assume-depexts", "--verbose-on"}
_LUAROCKS_VALUE_FLAGS = {"--server", "--only-server", "--tree", "--lua-version", "--lua-dir", "--deps-mode",
                         "--branch", "--namespace", "--only-sources", "--project-tree", "--global-config",
                         "--timeout"}
# Буквы флагов `cpan`, допустимые при установке; любая другая — показ, тест, сборка, список или обновление
# всего установленного.
_CPAN_INSTALL_LETTERS = frozenset("ifFTIwjM")
_CPAN_VALUE_FLAGS = {"-j", "-M"}
_CPANM_VALUE_FLAGS = {"-l", "--local-lib", "-L", "--local-lib-contained", "--mirror", "--from", "-M",
                      "--cpanfile", "--with-feature", "--without-feature", "--configure-timeout",
                      "--build-timeout", "--test-timeout", "--save-dists", "--format", "--resolver"}
# Режимы cpanm без установки: удаление, показ, обновление самого cpanm.
_CPANM_NON_INSTALL = {"-U", "--uninstall", "--info", "--look", "--showdeps", "--scandeps", "-V", "--version",
                      "-h", "--help", "--self-upgrade"}
_VCPKG_VALUE_FLAGS = {"--triplet", "--host-triplet", "--overlay-ports", "--overlay-triplets", "--x-manifest-root",
                      "--x-install-root", "--vcpkg-root", "--downloads-root", "--x-buildtrees-root",
                      "--x-packages-root", "--binarysource", "--x-feature", "--feature-flags", "--x-asset-sources"}
_CONAN_VALUE_FLAGS = {"-r", "--remote", "-pr", "--profile", "-pr:b", "-pr:h", "-pr:a", "--profile:build",
                      "--profile:host", "--profile:all", "-s", "--settings", "-s:b", "-s:h", "-s:a", "-o",
                      "--options", "-o:b", "-o:h", "-o:a", "-c", "--conf", "-c:b", "-c:h", "-c:a", "-b", "--build",
                      "-of", "--output-folder", "-g", "--generator", "-f", "--format", "-l", "--lockfile",
                      "--lockfile-out", "--name", "--version", "--user", "--channel", "-d", "--deployer",
                      "--deployer-folder", "--requires", "--tool-requires", "-if", "--install-folder"}
# Вызовы R, ставящие названный пакет; без аргументов (`pak::pak()`, `renv::install()`) они ставят по
# DESCRIPTION или lockfile проекта.
_R_INSTALL = re.compile(
    r"\b(?:install\.packages|BiocManager::install|renv::install|pak::pak|pkg_install)\s*\(\s*[^)\s]"
    r"|\binstall_(?:github|gitlab|bitbucket|cran|url|git|bioc|version|svn|dev|univ)\s*\(")
# `install.packages(…, repos = NULL)` ставит локальный архив.
_R_LOCAL = re.compile(r"\brepos\s*=\s*NULL\b")


def _sub_args(w, value_flags: Set[str] = _NO_VALUE_FLAGS):
    """Подкоманда и слова после неё; глобальные флаги перед подкомандой пропущены."""
    w = _subcommand(w, value_flags)
    return (w[1] if len(w) > 1 else None), w[2:]


def _apt(w):
    flags = _APTITUDE_VALUE_FLAGS if w[0] == "aptitude" else _APT_VALUE_FLAGS
    sub, args = _sub_args(w, flags)
    return sub == "install" and _has(args, flags)


def _brew(w):
    sub, args = _sub_args(w)
    # `brew cask install` — старая форма `brew install --cask`.
    if sub == "cask" and args[:1] == ["install"]:
        sub, args = "install", args[1:]
    return sub == "install" and _has(args)


def _dnf(w):
    sub, args = _sub_args(w, _DNF_VALUE_FLAGS)
    if sub == "group" and args[:1] == ["install"]:
        sub, args = "install", args[1:]
    return sub in ("install", "in", "groupinstall", "localinstall") and _has(args, _DNF_VALUE_FLAGS)


def _zypper(w):
    sub, args = _sub_args(w, _ZYPPER_VALUE_FLAGS)
    return sub in ("install", "in") and _has(args, _ZYPPER_VALUE_FLAGS)


def _pacman(w):
    """Установка — операция `-S` с пакетами без поиска и показа или `-U` с URL; `-Syu` без пакетов —
    обновление системы."""
    args = w[1:]
    letters, longs, skip = set(), set(), False
    for a in args:
        if skip:
            skip = False
        elif a == "--":
            break
        elif a.startswith("--"):
            longs.add(a.partition("=")[0])
            skip = a in _PACMAN_VALUE_FLAGS
        elif a.startswith("-") and len(a) > 1:
            letters.update(a[1:])
            skip = _takes_value(a, _PACMAN_VALUE_FLAGS)
    sync = "S" in letters or "--sync" in longs
    if sync and (letters & _PACMAN_SYNC_QUERY or longs & _PACMAN_SYNC_QUERY_LONG):
        return False
    return (sync or "U" in letters or "--upgrade" in longs) and _has(args, _PACMAN_VALUE_FLAGS)


def _apk(w):
    sub, args = _sub_args(w, _APK_VALUE_FLAGS)
    return sub == "add" and _has(args, _APK_VALUE_FLAGS)


def _lower_flags(args):
    return [a.lower() if a.startswith("-") else a for a in args]


def _named(args, value_flags):
    """Есть позиционное слово, кроме манифеста `*.config` (`packages.config`)."""
    return any(not p.lower().endswith(".config") for p in _positionals(args, value_flags))


def _choco(w):
    sub, args = _sub_args(w, _CHOCO_VALUE_FLAGS)
    return sub == "install" and _named(_lower_flags(args), _CHOCO_VALUE_FLAGS)


def _nuget(w):
    sub, args = _sub_args(w)
    return sub is not None and sub.lower() == "install" and _named(_lower_flags(args), _NUGET_VALUE_FLAGS)


def _winget(w):
    sub, args = _sub_args(w)
    if sub not in ("install", "add"):
        return False
    # `-m` — локальный манифест.
    if _flag_value(args, "-m") is not None or _flag_value(args, "--manifest") is not None:
        return False
    return any(_flag_value(args, f) for f in _WINGET_NAME_FLAGS) or _has(args, _WINGET_VALUE_FLAGS)


def _scoop(w):
    sub, args = _sub_args(w)
    return sub == "install" and _has(args, _SCOOP_VALUE_FLAGS)


def _port(w):
    sub, args = _sub_args(w, _PORT_VALUE_FLAGS)
    # `+x11` — вариант порта, `name=value` — переменная сборки.
    return sub == "install" and any(not p.startswith("+") and "=" not in p for p in _positionals(args))


def _snap(w):
    sub, args = _sub_args(w)
    return sub == "install" and _has(args, _SNAP_VALUE_FLAGS)


def _flatpak(w):
    sub, args = _sub_args(w)
    return sub == "install" and _has(args, _FLATPAK_VALUE_FLAGS)


def _drop_pairs(args, flags):
    """Слова без флагов с двумя значениями и этих значений."""
    out, i = [], 0
    while i < len(args):
        if args[i] in flags:
            i += 3
            continue
        out.append(args[i])
        i += 1
    return out


def _nix_local_file(args):
    """`-f`/`--file` указывает на локальный файл: ставятся его атрибуты, а не пакеты канала;
    `<nixpkgs>` и URL — канал."""
    value = _flag_value(args, "--file") or _flag_value(args, "-f")
    return value is not None and not value.startswith("<") and "://" not in value


def _nix_installables(args):
    """Есть позиционное, кроме локальных: путь, `path:`, `.#attr`."""
    return any(not p.startswith(("path:", "git+file:")) for p in _positionals(args, _NIX_VALUE_FLAGS))


def _nix_env(w):
    args = _drop_pairs(w[1:], _NIX_PAIR_FLAGS)
    install, skip = False, False
    for a in args:
        if skip:
            skip = False
        elif a == "--install":
            install = True
        elif a.startswith("--"):
            skip = a in _NIX_VALUE_FLAGS
        elif a.startswith("-") and len(a) > 1:
            install = install or "i" in a[1:]
            skip = _takes_value(a, _NIX_VALUE_FLAGS)
    return install and not _nix_local_file(args) and _nix_installables(args)


def _nix(w):
    w = _drop_pairs(w, _NIX_PAIR_FLAGS)
    sub, args = _sub_args(w, _NIX_VALUE_FLAGS)
    if sub != "profile" or args[:1] not in (["install"], ["add"]):
        return False
    args = args[1:]
    return _flag_value(args, "--expr") is None and not _nix_local_file(args) and _nix_installables(args)


def _pdm(w):
    sub, args = _sub_args(w)
    if sub == "self":
        sub, args = (args[0] if args else None), args[1:]
    return sub == "add" and (_vcs_editable(args) or
                             any(_positionals(_join_at(args), _PDM_ADD_VALUE_FLAGS, pip=True)))


def _rye(w):
    sub, args = _sub_args(w)
    if sub == "tools" and args[:1] == ["install"]:
        sub, args = "install", args[1:]
    # `--path` — локальный пакет под названным именем.
    return sub in ("add", "install") and _flag_value(args, "--path") is None and _has(args, _RYE_VALUE_FLAGS)


def _pixi(w):
    sub, args = _sub_args(w, {"--color"})
    if sub == "global" and args[:1] in (["install"], ["add"]):
        sub, args = "add", args[1:]
    return sub == "add" and _has(args, _PIXI_VALUE_FLAGS)


def _mix(w):
    sub, args = _sub_args(w)
    if sub not in ("archive.install", "escript.install"):
        return False
    # Без аргументов ставится архив самого проекта.
    named = list(_positionals(args, _MIX_INSTALL_VALUE_FLAGS))
    if named and named[0] in _MIX_SOURCES:
        return len(named) > 1
    return bool(named)


def _haskell_targets(args, value_flags):
    """Есть цель cabal или stack, кроме компонентов проекта (`exe:foo`, `:test`) и `all`."""
    return any(":" not in p and p != "all" for p in _positionals(args, value_flags))


def _cabal(w):
    sub, args = _sub_args(w, _CABAL_VALUE_FLAGS)
    return (sub in ("install", "v2-install", "new-install", "v1-install")
            and _haskell_targets(args, _CABAL_VALUE_FLAGS))


def _stack(w):
    sub, args = _sub_args(w, _STACK_VALUE_FLAGS)
    return sub == "install" and _haskell_targets(args, _STACK_VALUE_FLAGS)


def _opam(w):
    sub, args = _sub_args(w)
    return sub == "install" and _has(args, _OPAM_VALUE_FLAGS)


def _luarocks(w):
    sub, args = _sub_args(w, _LUAROCKS_VALUE_FLAGS)
    return sub == "install" and _has(args, _LUAROCKS_VALUE_FLAGS)


def _cpan(w):
    args = w[1:]
    if any(a.startswith("-") and not set(a[1:]) <= _CPAN_INSTALL_LETTERS for a in args):
        return False
    return _has(args, _CPAN_VALUE_FLAGS)


def _cpanm(w):
    args = w[1:]
    if any(a.partition("=")[0] in _CPANM_NON_INSTALL for a in args):
        return False
    return _has(args, _CPANM_VALUE_FLAGS)


def _vcpkg(w):
    sub, args = _sub_args(w, _VCPKG_VALUE_FLAGS)
    if sub == "add":
        return args[:1] in (["port"], ["artifact"]) and _has(args[1:], _VCPKG_VALUE_FLAGS)
    return sub == "install" and _has(args, _VCPKG_VALUE_FLAGS)


def _conan(w):
    sub, args = _sub_args(w)
    if sub != "install":
        return False
    if _flag_value(args, "--requires") or _flag_value(args, "--tool-requires"):
        return True
    # Ссылка Conan 1 `zlib/1.2.11@` отличается от пути знаком `@`.
    return any("/" in p and "@" in p for p in _positionals(args, _CONAN_VALUE_FLAGS))


def _r(w):
    """`R -e '…'`, `Rscript -e '…'`: выражение ставит названный пакет."""
    exprs = [b for a, b in zip(w[1:], w[2:]) if a == "-e"]
    return any(_R_INSTALL.search(e) and not _R_LOCAL.search(e) for e in exprs)


_OTHER_MANAGERS = {
    "apt": _apt, "apt-get": _apt, "aptitude": _apt, "brew": _brew, "dnf": _dnf, "dnf5": _dnf, "microdnf": _dnf,
    "yum": _dnf, "zypper": _zypper, "pacman": _pacman, "yay": _pacman, "paru": _pacman, "apk": _apk,
    "choco": _choco, "nuget": _nuget, "winget": _winget, "scoop": _scoop, "port": _port, "snap": _snap,
    "flatpak": _flatpak, "nix-env": _nix_env, "nix": _nix, "pdm": _pdm, "rye": _rye, "pixi": _pixi, "mix": _mix,
    "cabal": _cabal, "stack": _stack, "opam": _opam, "luarocks": _luarocks, "cpan": _cpan, "cpanm": _cpanm,
    "vcpkg": _vcpkg, "conan": _conan, "R": _r, "Rscript": _r,
}


def _inline_script(words):
    """Строка, которую команда исполняет как команду: `sh -c '…'`, `eval …`; None, если такой нет."""
    if not words:
        return None
    name = _basename(words[0])
    if name == "eval":
        return " ".join(words[1:])
    if name not in _C_SHELLS:
        return None
    has_c, skip = False, False
    for a in words[1:]:
        if skip:
            skip = False
        elif a in _SHELL_VALUE_FLAGS:
            skip = True
        elif a.startswith("--"):
            continue
        elif a.startswith(("-", "+")) and len(a) > 1:
            has_c = has_c or a[0] == "-" and "c" in a[1:]
            skip = _takes_value(a, _SHELL_VALUE_FLAGS)
        else:
            return a if has_c else None
    return None


def _close_paren(text, i):
    """Позиция `)`, закрывающей подстановку, тело которой начинается с i; кавычки внутри учтены."""
    depth, n = 1, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            end = text.find("'", i + 1)
            i = n if end < 0 else end + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _quoted_substitutions(text):
    """Тела подстановок `$(…)` и `` `…` `` внутри двойных кавычек; вне кавычек их уже разделил
    _segments."""
    found = []
    in_dq = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif not in_dq and c == "'":
            if text[i - 1:i] == "$":
                i += 1
                while i < n and text[i] != "'":
                    i += 2 if text[i] == "\\" else 1
                i += 1
            else:
                end = text.find("'", i + 1)
                i = n if end < 0 else end + 1
        elif c == '"':
            in_dq = not in_dq
            i += 1
        elif in_dq and text.startswith("$(", i):
            end = _close_paren(text, i + 2)
            # `$((…))` — арифметика, не команда.
            if not text.startswith("$((", i):
                found.append(text[i + 2:end])
            i = end + 1
        elif in_dq and c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            found.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return found


# Флаги sh-подобных оболочек со значением.
_SHELL_VALUE_FLAGS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
# Флаги ssh со значением.
_SSH_VALUE_FLAGS = {"-i", "-p", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-E", "-S", "-W", "-b", "-c", "-m",
                    "-O", "-Q", "-w", "-B", "-e", "-I"}


def _heredoc_runs(words):
    """Тело heredoc этой команды — команды: оболочка без скрипта и без `-c` (или с `-s`), ssh без
    удалённой команды."""
    if not words or _basename(words[0]) not in _SHELLS:
        return False
    args = words[1:]
    if _basename(words[0]) == "ssh":
        words, skip = 0, False
        for a in args:
            if skip:
                skip = False
            elif a.startswith("-"):
                skip = _takes_value(a, _SSH_VALUE_FLAGS)
            else:
                words += 1
        return words <= 1
    positional, skip = False, False
    for a in args:
        if skip:
            skip = False
        elif a in _SHELL_VALUE_FLAGS:
            skip = True
        elif a.startswith(("-", "+")) and not a.startswith("--"):
            if a[0] == "-" and "s" in a[1:]:
                return True
            if a[0] == "-" and "c" in a[1:]:
                return False
            skip = _takes_value(a, _SHELL_VALUE_FLAGS)
        elif not a.startswith("--"):
            positional = True
    return not positional


_WORD_END = " \t;&|<>()"


def _heredoc_word(line, i):
    """Терминатор heredoc, начинающийся с позиции i, после снятия кавычек, и позиция за ним."""
    word = []
    while i < len(line) and line[i] not in _WORD_END:
        c = line[i]
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
    return "".join(word), i


def heredocs(line):
    """Heredoc, открытые строкой, по порядку: (терминатор, снимать ли ведущие табы).

    `<<` в кавычках (в том числе `$'…'` с `\\'`), here-string `<<<`, сдвиг в арифметике `((…))` и
    комментарий не считаются; подстановка `$(…)` внутри двойных кавычек — снова команда.
    """
    found = []
    stack = []  # "'", '"', "$'", "(" — подстановка или подоболочка, "A" — арифметика, "a" — скобка в ней
    i = 0
    while i < len(line):
        top = stack[-1] if stack else None
        c = line[i]
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
        elif top == '"':
            if c == '"':
                stack.pop()
                i += 1
            elif line.startswith("$((", i):
                stack.append("A")
                i += 3
            elif line.startswith("$(", i):
                stack.append("(")
                i += 2
            else:
                i += 1
        elif line.startswith("$'", i):
            stack.append("$'")
            i += 2
        elif c in "'\"":
            stack.append(c)
            i += 1
        elif top in ("A", "a"):
            if top == "A" and line.startswith("))", i):
                stack.pop()
                i += 2
            else:
                if c == "(":
                    stack.append("a")
                elif c == ")" and top == "a":
                    stack.pop()
                i += 1
        elif line.startswith("$((", i) or line.startswith("((", i):
            stack.append("A")
            i += 3 if c == "$" else 2
        elif line.startswith("$(", i) or c == "(":
            stack.append("(")
            i += 2 if c == "$" else 1
        elif c == ")":
            if top == "(":
                stack.pop()
            i += 1
        elif c == "#" and (i == 0 or line[i - 1].isspace()):
            break
        elif line.startswith("<<<", i):
            i += 3
        elif line.startswith("<<", i):
            i += 2
            strip_tabs = line.startswith("-", i)
            i += strip_tabs
            while i < len(line) and line[i] in " \t":
                i += 1
            word, i = _heredoc_word(line, i)
            if word:
                found.append((word, strip_tabs))
        else:
            i += 1
    return found


def _quoted_heredoc_runs(text):
    """Heredoc внутри `$(…)` в тексте от открывающей кавычки: его команда — оболочка, исполняющая тело
    (_heredoc_runs), а не `cat`, для которого тело — данные."""
    cut = text.find("<<")
    start = text.rfind("$(", 0, cut)
    if cut < 0 or start < 0:
        return False
    return any(_heredoc_runs(_command(s)[0]) for s in _segments(text[start + 2:cut]))


def _segments(command):
    """Сегменты команды: границы — `&&`, `||`, `;`, `|`, `&`, `(`, `)`, обратная кавычка и перевод
    строки вне кавычек.

    Кавычка, открытая на одной строке, продолжается на следующих; `\\` в конце строки продолжает
    сегмент. Тело heredoc — данные: строки после конца логической строки с `<<` до терминатора не
    входят ни в один сегмент; у heredoc внутри `"$(…)"`, открытой до конца строки, тело начинается со
    следующей строки, внутри кавычки; тело heredoc оболочки без скрипта (`bash <<EOF`, `ssh host <<EOF`),
    в том числе внутри `"$(…)"`, — команды (_heredoc_runs). Комментарий — `#` в начале строки или после пробела вне кавычек — до
    конца строки.
    """
    segments, current = [], []
    quote = None
    pending = []
    # Heredoc логической строки: тело начинается после её конца.
    opened = []
    logical_start = 0

    def flush():
        segments.append("".join(current))
        current.clear()

    for line in command.split("\n"):
        if pending:
            term, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == term:
                pending.pop(0)
            continue
        # Позиция, с которой строка идёт вне кавычки, перенесённой с прошлых строк.
        outside = 0 if quote is None else None
        # Позиция последней кавычки, открытой на этой строке.
        quote_start = None
        continued = False
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if quote == "'":
                current.append(c)
                if c == "'":
                    quote = None
            elif quote == "$'":
                if c == "\\" and i + 1 < n:
                    current.append(line[i:i + 2])
                    i += 2
                    continue
                current.append(c)
                if c == "'":
                    quote = None
            elif quote == '"':
                if c == "\\" and i + 1 < n:
                    current.append(line[i:i + 2])
                    i += 2
                    continue
                current.append(c)
                if c == '"':
                    quote = None
            elif c == "\\":
                if i + 1 == n:
                    continued = True
                    break
                current.append(line[i:i + 2])
                i += 2
                continue
            elif c == "'" and line[i - 1:i] == "$":
                quote = "$'"
                quote_start = i
                current.append(c)
            elif c in "'\"":
                quote = c
                quote_start = i
                current.append(c)
            elif c == "#" and (i == 0 or line[i - 1].isspace()):
                break
            elif line.startswith(("&&", "||"), i):
                flush()
                i += 2
                continue
            elif c in ";|()`" or (c == "&" and line[i - 1:i] not in ("<", ">") and line[i + 1:i + 2] != ">"):
                flush()
            else:
                current.append(c)
            if outside is None and quote is None:
                outside = i + 1
            i += 1
        if quote is not None:
            # Heredoc в `"$(…)"`, открытой до конца строки, — тело идёт со следующей строки внутри кавычки
            # и кончается до её закрытия; тело — данные.
            if outside is not None and quote_start is not None:
                opened.extend(heredocs(line[outside:quote_start]))
                pending = [] if _quoted_heredoc_runs(line[quote_start:]) else heredocs(line[quote_start:])
            current.append("\n")
            continue
        if outside is not None:
            opened.extend(heredocs(line[outside:i] if continued else line[outside:]))
        if continued:
            current.append(" ")
            continue
        flush()
        if not any(_heredoc_runs(_command(s)[0]) for s in segments[logical_start:]):
            pending = opened
        opened = []
        logical_start = len(segments)
    flush()
    return segments


def _find(command, depth):
    """Первый сегмент команды, добавляющий пакет; вложенные команды разбираются до _MAX_DEPTH."""
    for segment in _segments(command):
        segment = segment.strip()
        if segment and _segment_adds(segment, depth):
            return segment
    return None


def _segment_adds(segment, depth):
    """Сегмент добавляет пакет сам, через строку `sh -c`/`eval` или через подстановку в двойных
    кавычках. Маркер сегмента покрывает его строку `-c`/`eval`, но не подстановки: они исполняются
    до команды сегмента."""
    words, marker = _command(segment)
    if depth >= _MAX_DEPTH:
        return not marker and _is_add(words)
    if not marker:
        script = _inline_script(words)
        if _is_add(words) or script and _find(script, depth + 1):
            return True
    return any(_find(s, depth + 1) for s in _quoted_substitutions(segment))


def dependency_add(command):
    """Сегмент команды, добавляющий пакет; None, если такого нет. Маркер согласия снимает проверку с
    сегмента, где он стоит среди ведущих присваиваний команды, в том числе после `sudo`, `env`, `if`
    (`PLANKA_DEP_OK=1 npm install x`)."""
    if not isinstance(command, str):
        return None
    return _find(command, 0)
