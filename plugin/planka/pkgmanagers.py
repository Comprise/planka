"""Семантика менеджеров пакетов над списками слов команды: какие слова — флаги и их значения, подкоманда,
пакет; когда команда добавляет пакет, пробный прогон, справка и строки, которые команда исполняет. Текст команды
не разбирает: слова ей даёт depcheck."""
import re
import shlex
from collections.abc import Set


_C_FLAGS = ("-c", "--command")
# Команды, которым тело heredoc, строка here-string и вход конвейера передаются как команды, если у них нет
# скрипта и `-c`.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ssh"}
# Скрипт-аргумент, который читает stdin или открытый дескриптор: `-`, `/dev/stdin`, `/dev/fd/N` (так bash передаёт
# процесс-подстановку `<(…)`: `bash <(…) arg` исполняет её вывод), `/proc/<процесс>/fd/N`. Оболочка с ним читает
# вход конвейера, heredoc или процесс-подстановки.
_STDIN_SCRIPT = re.compile(r"-|/dev/stdin|/dev/fd/\d+|/proc/[^/]+/fd/\d+")
# Команды, исполняющие в текущей оболочке файл-аргумент: со скриптом _STDIN_SCRIPT — вход или процесс-подстановку.
_SOURCES = {"source", "."}
# Оболочки, исполняющие строку после `-c`.
_C_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_PIP = re.compile(r"pip\d*(?:\.\d+)*$")
# Суффиксы сборок: `t` — без GIL (`python3.13t`), `d` — отладочная, `m` — pymalloc (`python3.6m`).
_PYTHON = re.compile(r"(?:python|pypy)\d*(?:\.\d+)*[dmt]*$|py$")
# Флаги интерпретатора Python со значением.
_PYTHON_VALUE_FLAGS = {"-W", "-X"}
_PYTHON_LONG_VALUE_FLAGS = {"--check-hash-based-pycs"}
# Суффиксы исполняемых файлов Windows: `npm.cmd` — это `npm`.
_WIN_EXT = re.compile(r"\.(?:exe|cmd|bat|ps1)$", re.IGNORECASE)

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
                      "--root-prefix", "--rc-file", "--channel-priority", "--cert", "--clone"}
# Общие флаги conda, mamba и micromamba со значением перед подкомандой (`micromamba -r /x install y`; micromamba
# src/umamba.cpp: общие опции — и у корня).
_CONDA_GLOBAL_VALUE_FLAGS = {"-r", "--root-prefix", "--rc-file", "--log-level"}
_CONDA_RUN_VALUE_FLAGS = {"-n", "--name", "-p", "--prefix", "--cwd", "-r", "--root-prefix", "-a", "--attach"}
_DOTNET_ADD_VALUE_FLAGS = {"-v", "--version", "-f", "--framework", "-s", "--source", "--package-directory",
                           "--project"}
_DOTNET_TOOL_VALUE_FLAGS = {"--tool-path", "--version", "--add-source", "--configfile", "--framework", "-a",
                            "--arch", "--tool-manifest", "-v", "--verbosity"}
# Флаги `dart pub` со значением перед подкомандой (`dart pub --help`: `-C, --directory=<dir>`; ещё `-v`,
# `--trace`, `--[no-]color` без значения).
_PUB_GLOBAL_VALUE_FLAGS = {"-C", "--directory"}
_PUB_VALUE_FLAGS = {"-C", "--directory", "--git-url", "--git-ref", "--git-path", "--hosted-url", "--path", "--sdk",
                    "-s", "--source", "-x", "--executable"}
_SWIFT_PACKAGE_VALUE_FLAGS = {"--package-path", "--scratch-path", "--build-path", "--cache-path", "--config-path",
                              "--security-path", "--swift-sdks-path", "--toolset", "--pkg-config-path", "-j",
                              "--jobs", "-c", "--configuration"}
_SWIFT_ADD_VALUE_FLAGS = {"--exact", "--revision", "--branch", "--from", "--up-to-next-minor-from", "--to",
                          "--type"}
_NO_VALUE_FLAGS = frozenset()
# Глобальные флаги менеджера перед подкомандой.
_GLOBAL_FLAGS = {"npm": _NPM_VALUE_FLAGS, "pnpm": _PNPM_VALUE_FLAGS, "yarn": _YARN_VALUE_FLAGS,
                 "bun": _BUN_VALUE_FLAGS, "deno": _DENO_VALUE_FLAGS,
                 "uv": _UV_VALUE_FLAGS, "poetry": _POETRY_VALUE_FLAGS, "cargo": _CARGO_VALUE_FLAGS,
                 "composer": _COMPOSER_VALUE_FLAGS, "bundle": _BUNDLE_VALUE_FLAGS, "go": _GO_VALUE_FLAGS,
                 "pipenv": _PIPENV_VALUE_FLAGS, "pipx": _NO_VALUE_FLAGS, "dotnet": _NO_VALUE_FLAGS,
                 "dart": _NO_VALUE_FLAGS, "flutter": {"-d", "--device-id"},
                 "hatch": {"-e", "--env", "-p", "--project", "--data-dir", "--cache-dir", "--config"}}
# Флаги со значением для команды, чьё имя — подстановка (_expanded_install): менеджер неизвестен, наборы
# менеджеров JavaScript и pip.
_EXPANDED_VALUE_FLAGS = _NPM_VALUE_FLAGS | _PNPM_VALUE_FLAGS | _YARN_VALUE_FLAGS | _UV_PIP_VALUE_FLAGS
# Подкоманда `run`, исполняющая следующую команду в окружении проекта: флаги со значением перед ней.
_RUNNERS = {"uv": _UV_RUN_VALUE_FLAGS, "poetry": _NO_VALUE_FLAGS, "pipenv": _NO_VALUE_FLAGS,
            "conda": _CONDA_RUN_VALUE_FLAGS, "mamba": _CONDA_RUN_VALUE_FLAGS,
            "micromamba": _CONDA_RUN_VALUE_FLAGS, "pdm": _NO_VALUE_FLAGS, "rye": _NO_VALUE_FLAGS,
            "hatch": _NO_VALUE_FLAGS, "pixi": {"--manifest-path", "-e", "--environment"}}
_CONDAS = {"conda", "mamba", "micromamba"}
_NPX = {"npx", "bunx", "pnpx"}
# Пробный прогон без установки; у apt — и его синонимы.
_DRY_RUN = "--dry-run"
# Менеджеры, где `--dry-run` — пробный прогон или ошибка разбора флагов: установки нет. yarn 1 незнакомый флаг
# пропускает и ставит пакет (проверено на 1.22), так же могут вести себя разборщики mix, choco, nuget, winget,
# cpan, cpanm, port, scoop — у них и у неизвестных `--dry-run` пробным прогоном не считается.
_DRY_RUN_MANAGERS = {"pnpm", "bun", "deno", "uv", "poetry", "cargo", "composer", "gem", "bundle", "go", "pipx",
                     "pipenv", "conda", "mamba", "micromamba", "dotnet", "dart", "flutter", "swift", "brew", "dnf",
                     "dnf5", "microdnf", "yum", "zypper", "pacman", "yay", "paru", "apk", "snap", "flatpak", "nix",
                     "nix-env", "pdm", "rye", "pixi", "cabal", "stack", "opam", "luarocks", "vcpkg", "conan"}
_APT_SIMULATE = {"-s", "--simulate", "--just-print", "--dry-run", "--recon", "--no-act"}
# Булевы флаги apt и apt-get без установки, длинные имена без `--` в нижнем регистре → буква флага: пробный прогон
# `-s` и только скачивание `-d`/`--download-only` (apt-private/private-cmndline.cc).
_APT_NO_INSTALL_LONG = {**{f[2:]: "s" for f in _APT_SIMULATE if f.startswith("--")}, "download-only": "d"}
_APTS = {"apt", "apt-get", "aptitude"}

# Локальные архивы пакетов.
_ARCHIVES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".gem", ".conda", ".deb",
             ".rpm", ".apk", ".snap", ".flatpak", ".nupkg", ".rock", ".rockspec", ".ez")
# Протоколы локального пакета: путь (`file:`) и пакет своего workspace (`workspace:`, `link:`, `portal:` у
# pnpm и yarn).
_LOCAL_PROTOCOLS = ("file:", "workspace:", "link:", "portal:")
# Именованное требование на локальный пакет: `name@file:///x`, `name @ file:x`, `name[extra] @ file:x`,
# `@org/x@workspace:*`. Имя — с начала слова, без `:`, `/` (кроме scope `@org/`), `#`: у URL со схемой
# (`https://e.com/x.tgz?a=@file:`) и спецификаций git (`github:u/x#@file:`) `@file:` — часть адреса, не признак.
_NAMED_LOCAL = re.compile(r"(?:@[^@/:#\s]+/)?[^@/:#\s]+\s*@\s*(?:file|workspace|link|portal):")
# Сколько пар «флаг значение» перед подкомандой по очереди склеивает _loosened.
_LOOSE_FLAGS = 4
# Глаголы установки после имени-подстановки (`$M install x`).
_INSTALL_VERBS = {"install", "i", "add", "require"}


# Склейка коротких флагов одним словом: `-qr`, `-euo`, `+eo`.
_GLUED = re.compile(r"[-+][A-Za-z]{2,}")


def _takes_value(flag, value_flags):
    """Следующее слово — значение флага: флаг из value_flags или склейка коротких флагов, где первая буква флага со
    значением — последняя (`-qr` при `-r`). Как у getopt, такая буква забирает остаток слова значением: `-uroot`,
    `-tvendor` следующего слова не берут."""
    if flag in value_flags:
        return True
    if len(flag) < 3 or flag[0] not in "-+" or not flag[1].isalpha():
        return False
    for j in range(1, len(flag)):
        if not flag[j].isalpha():
            return False
        if flag[0] + flag[j] in value_flags:
            return j == len(flag) - 1
    return False


def _basename(word):
    """Имя команды без каталога и суффикса Windows, в нижнем регистре: файловые системы macOS и Windows регистр
    не различают (`NPM install x` там — npm)."""
    name = re.split(r"[/\\]", word)[-1]
    return _WIN_EXT.sub("", name).lower()


def _part_of(word, text):
    """Часть text слова word с атрибутами слова: у слова depcheck — признак внешнего раскрытия (строка `su -c"…"`,
    `--command=…` несёт раскрытие своего слова)."""
    if type(word) is str:
        return text
    out = type(word)(text)
    out.__dict__.update(word.__dict__)
    return out


def _joined(words, text):
    """Строка text, склеенная из слов words: с признаком внешнего раскрытия depcheck (expands), если он есть у
    какого-то из них (строку разбирает оболочка: `ssh host "…"`, `cmd /c …`)."""
    for word in words:
        if getattr(word, "expands", False):
            return _part_of(word, text)
    return text


def _replaced(word, old, new):
    """Слово word с заменой old на new (`{}` у `find -exec`) и в его записи для повторного разбора (shell)."""
    out = _part_of(word, word.replace(old, new))
    if getattr(word, "shell", None):
        out.shell = word.shell.replace(old, new)
    return out


def _shell_join(words):
    """Строка команды из слов-аргументов words для повторного разбора: слово с внешним раскрытием depcheck — его
    записью shell (раскрытия остаются раскрытиями), без записи — в кавычках с заглушкой `$_` за ними (раскрытие
    видно, значение неизвестно); прочие — в кавычках shlex."""
    out = []
    for word in words:
        shell = getattr(word, "shell", None)
        if shell:
            out.append(shell)
        elif getattr(word, "expands", False):
            out.append(shlex.quote(word) + '"$_"')
        else:
            out.append(shlex.quote(word))
    return " ".join(out)


# Длинные флаги su и runuser без значения (util-linux 2.42, `su --help`): getopt_long принимает однозначное
# сокращение длинного флага (`su --comm` — `--command`, `su --s` — ошибка «двусмысленный параметр»), и сокращение
# сверяется со всеми длинными флагами, не только со значением.
_SU_PLAIN_LONG = frozenset({"--preserve-environment", "--login", "--fast", "--pty", "--no-pty", "--help", "--version"})


def _long_flag(flag, names):
    """Полное имя длинного флага flag по getopt_long: имя из names или его однозначное сокращение; иначе flag как
    есть (неизвестный флаг или неоднозначное сокращение — ошибка разбора программы)."""
    if flag in names or not flag.startswith("--") or flag == "--":
        return flag
    found = [name for name in names if name.startswith(flag)]
    return found[0] if len(found) == 1 else flag


def _c_wrapper(words, value_flags):
    """Слова words после `su`/`runuser` — как их разбирает getopt с перестановкой: флаги и их значения (value_flags)
    стоят где угодно до `--`, склейка коротких флагов забирает значением остаток слова (`-lc'…'`). С
    `-c`/`--command`/`--session-command` — команда `sh -c '…'`; иначе у runuser с `-u`/`--user` команда — слова
    не-флаги; без того и другого первое слово-не-флаг `-` — вход, следующее — пользователь, команда — программа
    `-s`/`--shell` или `sh` с остальными словами аргументами (`sh -c '…'` при `-c` среди них); без аргументов —
    программа одна: оболочка читает команды из stdin.

    Слова снимаются с начала words, хвост за `--` остаётся в очереди: цепочка обёрток разбирается за один
    проход."""
    script, shell, user, rest = None, "sh", False, []
    names = _SU_PLAIN_LONG | {f for f in value_flags if f.startswith("--")}
    while words:
        word, lead = words.popleft()
        if word == "--":
            break
        if word.startswith("--"):
            flag, eq, value = word.partition("=")
            flag = _long_flag(flag, names)
            value = _part_of(word, value)
            if flag in value_flags and not eq:
                value = words.popleft()[0] if words else None
        elif word.startswith("-") and len(word) > 1:
            flag = value = None
            for j in range(1, len(word)):
                if "-" + word[j] in value_flags:
                    flag = "-" + word[j]
                    value = _part_of(word, word[j + 1:]) or (words.popleft()[0] if words else None)
                    break
        else:
            rest.append((word, lead))
            continue
        if flag in _C_FLAGS + ("--session-command",) and value is not None:
            script = value
        elif flag in ("-s", "--shell") and value:
            shell = value
        elif flag in ("-u", "--user") and flag in value_flags:
            user = True
    words.extendleft(reversed(rest))
    if script is not None:
        words.clear()
        words.extend([("sh", "sh"), ("-c", "-c"), (script, script)])
    elif not user:
        if words and words[0][0] == "-":
            words.popleft()
        if words:
            words.popleft()
        words.appendleft((shell, shell))


def _is_local(word, pip):
    """Слово — локальный путь, архив или пакет своего workspace, а не пакет; URL со схемой, кроме `file:`, —
    пакет."""
    if word.startswith(_LOCAL_PROTOCOLS) or _NAMED_LOCAL.match(word):
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


def _editable_values(args):
    """Значения `-e`: `-e x`, `--editable x`, `--editable=x`, склейка `-ex` и `-qe x`."""
    for i, a in enumerate(args):
        following = args[i + 1] if i + 1 < len(args) else None
        if a == "--editable":
            yield following
        elif a.startswith("--editable="):
            yield a.partition("=")[2]
        elif a.startswith("-") and not a.startswith("--"):
            for j in range(1, len(a)):
                if a[j] == "e":
                    yield a[j + 1:] or following
                    break
                if not a[j].isalpha():
                    break


def _vcs_editable(args):
    """`-e git+https://…` — пакет из репозитория; `-e .` и `-e mypkg` — локальный каталог."""
    return any(value and _VCS_OR_URL.match(value) and not _is_local(value, True) for value in _editable_values(args))


def _pip_add(args, value_flags: Set[str] = _PIP_VALUE_FLAGS):
    """Требования pip называют пакет, кроме самого установщика и его спутников по имени из индекса: `pip @ URL` —
    пакет из чужого источника под их именем."""
    if _vcs_editable(args):
        return True
    return any(_PIP_NAME_END.split(w, 1)[0].strip().lower() not in _PIP_BOOTSTRAP or "@" in w or "://" in w
               for w in _positionals(_join_at(args), value_flags, pip=True))


def _with_packages(args):
    """Значения `--with`/`-w` у `uv tool install` — пакеты окружения инструмента (через запятую)."""
    values = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--with", "-w")]
    values += [a.partition("=")[2] for a in args if a.startswith("--with=")]
    return any(p and not _is_local(p, True) for v in values for p in v.split(","))


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


def _python_target(w):
    """Что запускает `python …` по словам w: ("m", слова с модуля), ("c", None) — код `-c`, ("script", None) — скрипт
    или stdin `-`, (None, None) — до конца слов одни флаги. Короткие флаги склеиваются (`-Im pip`, `-Impip`);
    значение `-W`/`-X` — остаток слова или следующее слово."""
    i = 1
    while i < len(w) and w[i].startswith("-"):
        a = w[i]
        if a == "-":
            return "script", None
        if a.startswith("--"):
            i += 2 if a in _PYTHON_LONG_VALUE_FLAGS else 1
            continue
        step = 1
        for j, letter in enumerate(a[1:], 1):
            rest = a[j + 1:]
            if letter == "m":
                module = [rest, *w[i + 1:]] if rest else w[i + 1:]
                # `-m pip.__main__` запускает тот же pip.
                if module and module[0].endswith(".__main__"):
                    module = [module[0][:-len(".__main__")], *module[1:]]
                return "m", module
            if letter == "c":
                return "c", None
            if "-" + letter in _PYTHON_VALUE_FLAGS:
                step = 1 if rest else 2
                break
        i += step
    return ("script", None) if i < len(w) else (None, None)


def _python_module(w):
    """Слова `python -m <модуль> …` с модуля; None, если модуль не запускается (_python_target)."""
    kind, module = _python_target(w)
    return module if kind == "m" else None


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


def _npm_dry_run(args):
    """Пробный прогон npm по разбору флагов nopt: действует последний из `--dry-run` и `--no-dry-run` (`no-` в
    любом регистре, каждый повтор обращает; имя сокращается до `--dr`). Слово `true` или `false` следом или
    после `=` — значение флага; иное значение после `=` — отдельное слово, флаг — истина. `--` завершает флаги."""
    dry = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            break
        if not a.startswith("--"):
            continue
        name, eq, value = a[2:].partition("=")
        negate = False
        while name[:3].lower() == "no-":
            negate, name = not negate, name[3:]
        if len(name) < 2 or not _DRY_RUN[2:].startswith(name):
            continue
        if not eq:
            value = args[i] if i < len(args) else ""
            i += value in ("true", "false")
        dry = (value != "false") != negate
    return dry


def _dry_run(w, masked=False):
    """Слова команды w (с именем) — пробный прогон: у npm — по _npm_dry_run, у apt, apt-get и aptitude его
    разбирает _apt; у pip и _DRY_RUN_MANAGERS — слово `--dry-run` среди слов до `--` (за ним — аргументы, не флаги
    менеджера: `gem install x -- --dry-run`; у brew ещё `-n` — _brew_dry_run в _brew); у прочих `--dry-run`
    пробным прогоном не считается. При masked `--dry-run` или `--` сразу за флагом со значением этого менеджера
    (_dry_value_flags) — значение флага."""
    if w[0] == "npm":
        return _npm_dry_run(w[1:])
    if w[0] in _APTS or w[0] not in _DRY_RUN_MANAGERS and not _PIP.match(w[0]):
        return False
    flags = _dry_value_flags(w[0]) if masked else _NO_VALUE_FLAGS
    for i, a in enumerate(w[1:], 1):
        if a in (_DRY_RUN, "--") and not _takes_value(w[i - 1], flags):
            return a == _DRY_RUN
    return False


# Подкоманды npm, ставящие пакет, после deref npm (lib/utils/cmd-list.js: псевдонимы и однозначные сокращения
# имён команд и псевдонимов, camelCase — через `-`): `install` и `install-test`; `link` с пакетом ставит его из
# реестра в глобальный каталог, если его там нет, и связывает в проект (lib/commands/link.js, missingArgsFromTree).
_NPM_INSTALL = {"add", "i", "in", "ins", "inst", "insta", "instal", "install", "install-t", "install-te",
                "install-tes", "install-test", "isnt", "isnta", "isntal", "isntall", "it", "lin", "link", "ln"}
_NPM_CAMEL = re.compile(r"[A-Z]")


def _npm_command(sub):
    """Подкоманда npm, как её читает deref npm: заглавная буква — `-` и строчная (`installTest` — `install-test`)."""
    return _NPM_CAMEL.sub(lambda m: "-" + m[0].lower(), sub)


# `gem`: флаги, которые RubyGems снимает со слов где угодно до `--` (config_file.rb, handle_arguments).
_GEM_DROPPED = {"--backtrace", "--traceback", "--debug"}


def _gem_sub_args(w):
    """Подкоманда gem в нижнем регистре и слова после неё, как их читает RubyGems: слова за `--` — аргументы сборки
    (gem_runner.rb, extract_build_args), `--backtrace`, `--traceback`, `--debug` сняты, `-C каталог` первым словом
    пропущен (command_manager.rb, process_args); иной флаг первым словом — ошибка: (None, [])."""
    args = w[1:]
    if "--" in args:
        args = args[:args.index("--")]
    args = [a for a in args if a not in _GEM_DROPPED]
    if args[:1] == ["-C"]:
        args = args[2:]
    if not args or args[0].startswith("-"):
        return None, []
    return args[0].lower(), args[1:]


def _gem_file(args):
    """`-g`/`--file [FILE]` у `gem install`: ставятся зависимости файла (Gemfile), названные пакеты не ставятся
    (commands/install_command.rb, install_from_gemdeps)."""
    return any(a in ("-g", "--file") or a.startswith("--file=") or a.startswith("-g") and not a.startswith("--")
               for a in args)


# Флаги `yarn workspaces foreach` со значением (yarnpkg.com/cli/workspaces/foreach; `--since` значение берёт только
# через `=`).
_YARN_FOREACH_VALUE_FLAGS = {"--from", "--include", "--exclude", "-j", "--jobs"}


def _yarn_foreach(args):
    """Команда yarn, которую `yarn workspaces foreach` с флагами args запускает в каждом workspace: слова с первого
    позиционного; None — пробный прогон `-n`/`--dry-run` или команды нет."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-n", "--dry-run"):
            return None
        i += 2 if _takes_value(args[i], _YARN_FOREACH_VALUE_FLAGS) else 1
    return args[i:] or None


def _npm_subcommand(w):
    """Слова npm с подкоманды, как _subcommand; `true` или `false` за флагом без `=` — значение флага: nopt берёт
    его и у булева флага, и у флага, которого npm не знает."""
    i = 1
    while i < len(w) and w[i].startswith("-"):
        boolean = "=" not in w[i] and w[i + 1:i + 2] in (["true"], ["false"])
        i += 2 if boolean or _takes_value(w[i], _NPM_VALUE_FLAGS) else 1
    return [w[0], *w[i:]]


def _loosened(w):
    """Варианты слов менеджера w, где флаги перед подкомандой без `=` берут следующее слово-не-флаг значением
    (`--weird val` → `--weird=val`): k-й вариант склеивает первые k таких пар, k ≤ _LOOSE_FLAGS. `--` и слово-не-флаг
    не за флагом завершают флаги; `+toolchain` cargo пропускается."""
    out = list(w)
    i = 2 if w[0] == "cargo" and w[1:2] and w[1].startswith("+") else 1
    for _ in range(_LOOSE_FLAGS):
        while i + 1 < len(out) and out[i] != "--" and out[i].startswith("-") and (
                "=" in out[i] or out[i + 1].startswith("-")):
            i += 1
        if i + 1 >= len(out) or out[i] == "--" or not out[i].startswith("-"):
            return
        out[i:i + 2] = [out[i] + "=" + out[i + 1]]
        i += 1
        yield list(out)


# Менеджеры без подкоманды: операция — флагом (`pacman -S`, `nix-env -i`) или выражением (`R -e`).
_NO_SUBCOMMAND = {"pacman", "yay", "paru", "nix-env", "cpan", "cpanm", "r", "rscript"}


def _has_subcommand(name):
    """Менеджер с подкомандой, чьи флаги перед ней пропускает разбор (_subcommand, _sub_args)."""
    return (name in _GLOBAL_FLAGS or name in _OTHER_MANAGERS or bool(_PIP.match(name))) and name not in _NO_SUBCOMMAND


# Системные менеджеры и редкие языковые менеджеры: обработчик получает слова команды с именем.
# Установка по манифесту без названного пакета (`vcpkg install`, `conan install .`, `cpanm --installdeps .`),
# обновление, поиск, список и удаление — не добавление.

_APT_VALUE_FLAGS = {"-o", "--option", "-c", "--config-file", "-t", "--target-release", "--default-release",
                    "-a", "--host-architecture", "-P", "--build-profiles", "--solver", "--planner", "--comment",
                    "-S", "--snapshot", "--with-source", "--cli-version"}
_APTITUDE_VALUE_FLAGS = _APT_VALUE_FLAGS | {"-F", "--display-format", "-w", "--width", "--sort"}
# Флаги уровня apt (IntLevel): `-q`, `--quiet`, `--silent`.
_APT_LEVEL_LONG = {"quiet", "silent"}
# Флаги `brew install` со значением; короткие флаги `brew install` значения не берут.
_BREW_VALUE_FLAGS = {"--cc", "--env", "--bottle-arch"}
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
                    "--experimental-features", "--expr"}
# У nix-env `-E`/`--expr` — флаг без значения: аргументы — выражения Nix (руководство nix-env --install).
_NIX_ENV_VALUE_FLAGS = _NIX_VALUE_FLAGS - {"--expr"}
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


def _aptitude_simulates(args, flags):
    """Среди флагов aptitude — пробный прогон или только скачивание: `-s`, `-d` (и в склейке `-qs`), `--simulate` и
    синонимы, `--download-only`. Остаток склейки после флага со значением (`-tsid`) — значение."""
    for a in args:
        if a in _APT_SIMULATE or a == "--download-only":
            return True
        if a.startswith("-") and not a.startswith("--"):
            for letter in a[1:]:
                if letter in "sd":
                    return True
                if "-" + letter in flags:
                    break
    return False


# Пробелы isspace локали C, которые strtol пропускает перед числом.
_C_SPACE = r"[ \t\n\v\f\r]*"
# Целое C (strtol с основанием 0): десятичное, восьмеричное с ведущим `0`, шестнадцатеричное с `0x`.
_C_INT = re.compile(_C_SPACE + r"([+-]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)")
# Целое C по основанию 10 (strtol с основанием 10).
_C_DEC = re.compile(_C_SPACE + r"[+-]?[0-9]+")
_APT_FALSE = {"no", "false", "without", "off", "disable"}
_APT_TRUE = {"yes", "true", "with", "on", "enable"}


def _apt_bool(text):
    """Булево значение apt (StringToBool): True, False или None — не булево. Пустая строка — False."""
    m = _C_INT.fullmatch(text)
    if m or not text:
        digits = m[2] if m else "0"
        # Восьмеричное `0…` читается по основанию 10: значения 0 и 1 у них совпадают, прочие — не булевы.
        value = int(digits, 16 if digits[1:2] in ("x", "X") else 10)
        value = -value if m and m[1] == "-" else value
        if value in (0, 1):
            return bool(value)
    low = text.lower()
    return False if low in _APT_FALSE else True if low in _APT_TRUE else None


def _apt_sense(argument, certain, prefix):
    """Значение булева флага apt (HandleOpt): (значение, съедено ли argument). argument — остаток слова или
    следующее слово без `-`, certain — он после `=`, prefix — слово перед первым `-` имени (`no` в
    `--no-simulate`) или None. Значение None — ошибка разбора: apt не выполняется."""
    if argument is not None:
        value = _apt_bool(argument)
        if value is not None:
            return value, True
        if certain:
            return None, False
    if prefix is not None:
        return _apt_bool(prefix), False
    return True, False


def _apt_level(argument, certain):
    """Съедает ли флаг уровня apt (IntLevel) argument: целое strtol по основанию 10 до конца строки — да; иначе
    уровень растёт без значения. argument — как у _apt_sense. None — ошибка разбора: после `=` не целое."""
    if argument is not None and _C_DEC.fullmatch(argument):
        return True
    return None if certain else False


def _apt_parse(args, flags):
    """Разбор флагов apt и apt-get по CommandLine apt: (установки нет, args без слов-значений булевых флагов и
    флагов уровня). Установки нет при пробном прогоне — последнем значении `-s`, `--simulate` и синонимов (имя без
    учёта регистра) — или только скачивании — последнем значении `-d`, `--download-only` (_APT_NO_INSTALL_LONG).
    Значение — после `=`, остаток склейки (`-s0`, `-q2`), следующее слово без `-` (`--simulate no`, `-q 2`) или
    слово перед `-` имени (`--no-simulate`); не булево после `=` или перед `-`, не целое после `=` у `-q` —
    ошибка, установки нет (True). Значения флагов из flags остаются в args. `--` завершает флаги."""
    state = {"s": False, "d": False}
    eaten_words = set()
    i = 0
    while i < len(args):
        a = args[i]
        following = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else None
        i += 1
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, value = a[2:].partition("=")
            low = name.lower()
            argument = value if eq else following
            prefix = None
            if low in _APT_LEVEL_LONG:
                eaten = _apt_level(argument, bool(eq))
                if eaten is None:
                    return True, args
            else:
                key = _APT_NO_INSTALL_LONG.get(low)
                if key is None:
                    prefix, dash, rest = low.partition("-")
                    key = (rest if rest in ("s", "d") else _APT_NO_INSTALL_LONG.get(rest)) if dash else None
                    if key is None:
                        i += not eq and a in flags and following is not None
                        continue
                value, eaten = _apt_sense(argument, bool(eq), prefix)
                if value is None:
                    return True, args
                state[key] = value
            if eaten and not eq:
                eaten_words.add(i)
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for j, letter in enumerate(a[1:], 1):
                rest = a[j + 1:]
                certain = rest.startswith("=")
                argument = rest[1:] if certain else rest or following
                if letter in ("s", "d", "q"):
                    if letter != "q":
                        value, eaten = _apt_sense(argument, certain, None)
                        error = value is None
                        state[letter] = value
                    else:
                        eaten = _apt_level(argument, certain)
                        error = eaten is None
                    if error:
                        return True, args
                    if eaten:
                        if not rest:
                            eaten_words.add(i)
                            i += 1
                        break
                elif "-" + letter in flags:
                    i += not rest and following is not None
                    break
                elif certain or rest and _apt_bool(rest) is not None:
                    break
    return state["s"] or state["d"], [a for k, a in enumerate(args) if k not in eaten_words]


def _apt(w):
    if w[0] == "aptitude":
        flags = _APTITUDE_VALUE_FLAGS
        simulates = _aptitude_simulates(w[1:], flags)
    else:
        flags = _APT_VALUE_FLAGS
        simulates, args = _apt_parse(w[1:], flags)
        w = [w[0], *args]
    if simulates:
        return False
    sub, args = _sub_args(w, flags)
    # `satisfy` ставит пакеты, удовлетворяющие строкам зависимостей (`apt satisfy 'jq (>= 1)'`); у apt и apt-get
    # пакеты, названные `upgrade`, `full-upgrade`, `dist-upgrade`, ставятся, как у `install`, и неустановленные
    # (apt-private/private-install.cc, DoCacheManipulationFromCommandLine: действие по умолчанию — MOD_INSTALL).
    verbs = ("install", "satisfy") if w[0] == "aptitude" else (
        "install", "satisfy", "upgrade", "full-upgrade", "dist-upgrade")
    return sub in verbs and _has(args, flags)


def _brew_dry_run(args):
    """Пробный прогон `brew install`: `-n` (и в склейке `-vn`) до `--`; `--dry-run` проверяет _dry_run."""
    for a in args:
        if a == "--":
            return False
        if _GLUED.fullmatch(a) and a[0] == "-" and "n" in a or a == "-n":
            return True
    return False


def _brew(w):
    sub, args = _sub_args(w)
    # `brew cask install` — старая форма `brew install --cask`.
    if sub == "cask" and args[:1] == ["install"]:
        sub, args = "install", args[1:]
    return sub == "install" and not _brew_dry_run(args) and _has(args, _BREW_VALUE_FLAGS)


def _dnf(w):
    sub, args = _sub_args(w, _DNF_VALUE_FLAGS)
    if sub == "group" and args[:1] == ["install"]:
        sub, args = "install", args[1:]
    if sub == "swap":
        # `dnf swap <удаляемый> <ставящийся>`.
        return len(list(_positionals(args, _DNF_VALUE_FLAGS))) > 1
    # `install-n`, `install-na`, `install-nevra` — install с формой имени (dnf/cli/commands/install.py, nevra_forms).
    return sub in ("install", "in", "groupinstall", "localinstall", "install-n", "install-na",
                   "install-nevra") and _has(args, _DNF_VALUE_FLAGS)


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


# Флаги `choco upgrade`, с которыми неустановленный пакет не ставится (docs.chocolatey.org, choco upgrade: «If you
# do not have a package installed, upgrade will install it»), в нижнем регистре.
_CHOCO_FAIL_NOT_INSTALLED = {"--failonnotinstalled", "--fail-on-not-installed"}


def _choco(w):
    """`choco install` и `choco upgrade` с названным пакетом; `upgrade` без `--failonnotinstalled` ставит и
    неустановленный, `upgrade all` — обновление установленных."""
    sub, args = _sub_args(w, _CHOCO_VALUE_FLAGS)
    args = _lower_flags(args)
    if sub == "upgrade":
        return not _CHOCO_FAIL_NOT_INSTALLED & set(args) and any(
            p.lower() != "all" and not p.lower().endswith(".config") for p in _positionals(args, _CHOCO_VALUE_FLAGS))
    return sub == "install" and _named(args, _CHOCO_VALUE_FLAGS)


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


def _nix_installables(args, value_flags=_NIX_VALUE_FLAGS):
    """Есть позиционное, кроме локальных: путь, `path:`, `.#attr`."""
    return any(not p.startswith(("path:", "git+file:")) for p in _positionals(args, value_flags))


def _nix_env(w):
    args = _drop_pairs(w[1:], _NIX_PAIR_FLAGS)
    install, skip = False, False
    for a in args:
        if skip:
            skip = False
        elif a == "--install":
            install = True
        elif a.startswith("--"):
            skip = a in _NIX_ENV_VALUE_FLAGS
        elif a.startswith("-") and len(a) > 1:
            install = install or "i" in a[1:]
            skip = _takes_value(a, _NIX_ENV_VALUE_FLAGS)
    return install and not _nix_local_file(args) and _nix_installables(args, _NIX_ENV_VALUE_FLAGS)


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
    "vcpkg": _vcpkg, "conan": _conan, "r": _r, "rscript": _r,
}
# Флаги со значением по менеджерам _DRY_RUN_MANAGERS для _dry_run при сомнении: `--dry-run` за таким флагом — его
# значение. У pip — _PIP_VALUE_FLAGS; у прочих `--dry-run` не пробный прогон и без флага перед ним.
_DRY_VALUE_FLAGS = {
    "pnpm": _PNPM_VALUE_FLAGS, "bun": _BUN_VALUE_FLAGS, "deno": _DENO_VALUE_FLAGS,
    "uv": _UV_PIP_VALUE_FLAGS | _UV_ADD_VALUE_FLAGS | _UV_TOOL_VALUE_FLAGS, "cargo": _CARGO_INSTALL_VALUE_FLAGS,
    "poetry": _POETRY_VALUE_FLAGS, "bundle": _BUNDLE_VALUE_FLAGS, "gem": _GEM_VALUE_FLAGS,
    "composer": _COMPOSER_VALUE_FLAGS, "go": _GO_BUILD_VALUE_FLAGS, "pipx": _PIPX_VALUE_FLAGS,
    "pipenv": _PIPENV_VALUE_FLAGS, "dotnet": _DOTNET_ADD_VALUE_FLAGS | _DOTNET_TOOL_VALUE_FLAGS,
    "dart": _PUB_VALUE_FLAGS, "flutter": _PUB_VALUE_FLAGS, "swift": _SWIFT_PACKAGE_VALUE_FLAGS | _SWIFT_ADD_VALUE_FLAGS,
    "brew": _BREW_VALUE_FLAGS, "zypper": _ZYPPER_VALUE_FLAGS, "apk": _APK_VALUE_FLAGS, "snap": _SNAP_VALUE_FLAGS,
    "flatpak": _FLATPAK_VALUE_FLAGS, "nix-env": _NIX_ENV_VALUE_FLAGS, "nix": _NIX_VALUE_FLAGS,
    "pdm": _PDM_ADD_VALUE_FLAGS, "rye": _RYE_VALUE_FLAGS, "pixi": _PIXI_VALUE_FLAGS,
    "cabal": _CABAL_VALUE_FLAGS, "stack": _STACK_VALUE_FLAGS, "opam": _OPAM_VALUE_FLAGS,
    "luarocks": _LUAROCKS_VALUE_FLAGS, "vcpkg": _VCPKG_VALUE_FLAGS, "conan": _CONAN_VALUE_FLAGS,
    **dict.fromkeys(_CONDAS, _CONDA_VALUE_FLAGS),
    **dict.fromkeys(("dnf", "dnf5", "microdnf", "yum"), _DNF_VALUE_FLAGS),
    **dict.fromkeys(("pacman", "yay", "paru"), _PACMAN_VALUE_FLAGS),
}


def _dry_value_flags(name):
    return _PIP_VALUE_FLAGS if _PIP.match(name) else _DRY_VALUE_FLAGS.get(name, _NO_VALUE_FLAGS)


def _inline_script(words):
    """Строка, которую команда исполняет как команду: `sh -c '…'`, `fish --command '…'`, `eval …`; None, если такой
    нет или оболочка её только разбирает (_noexec). Встроенная eval принимает `--` концом флагов (bash 5.3,
    no_options): строка — слова за ним."""
    if not words:
        return None
    name = _basename(words[0])
    if name == "eval":
        return " ".join(words[2:] if words[1:2] == ["--"] else words[1:])
    if name not in _C_SHELLS:
        return None
    args = words[1:]
    has_c = noexec = False
    i = 0
    while i < len(args):
        a = args[i]
        noexec = _noexec(a, args[i + 1:i + 2], noexec)
        if a == "--command" or a.startswith("--command="):
            # `fish --command '…'`, `fish --command=…`.
            if "=" in a:
                return None if noexec else _part_of(a, a.partition("=")[2])
            has_c = True
        elif a.startswith("--"):
            if a in _SHELL_VALUE_FLAGS:
                i += 1
        elif a.startswith(("-", "+")) and len(a) > 1:
            has_c = has_c or "c" in a[1:]
            if _takes_value(a, _SHELL_VALUE_FLAGS):
                i += 1
        else:
            return a if has_c and not noexec else None
        i += 1
    return None


def _noexec(flag, following, noexec):
    """Режим «только разбор» оболочки после флага flag; noexec — режим до него. `-n` (и в склейке `-xn`),
    `-o noexec` (following — следующее слово) и `--no-execute` fish его ставят, `+n` и `+o noexec` снимают: режим —
    последнее значение по порядку флагов."""
    if flag == "--no-execute":
        return True
    if flag[:1] not in ("-", "+") or flag.startswith("--") or len(flag) < 2:
        return noexec
    if "n" in flag[1:] or flag.endswith("o") and following == ["noexec"]:
        return flag[0] == "-"
    return noexec


# Флаги sh-подобных оболочек со значением.
_SHELL_VALUE_FLAGS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
# Флаги ssh со значением.
_SSH_VALUE_FLAGS = {"-i", "-p", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-E", "-S", "-W", "-b", "-c", "-m",
                    "-O", "-Q", "-w", "-B", "-e", "-I"}


def _ssh_command(args):
    """Слова удалённой команды ssh из его аргументов args. OpenSSH разбирает флаги и до имени хоста, и после него
    (ssh.c перезапускает getopt за хостом); `--` кончает флаги, первое слово-не-флаг за хостом начинает команду."""
    i, host = 0, False
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            if host:
                break
            host, i = True, i + 1
            continue
        if a.startswith("-") and len(a) > 1:
            i += 2 if _takes_value(a, _SSH_VALUE_FLAGS) else 1
            continue
        if host:
            break
        host, i = True, i + 1
    return args[i:]


def _heredoc_runs(words):
    """Тело heredoc, вход конвейера и процесс-подстановка `<(…)` скриптом этой команды — команды: оболочка без
    скрипта и без `-c` (или с `-s`) или со скриптом _STDIN_SCRIPT, ssh без удалённой команды, `source` и `.` со
    скриптом _STDIN_SCRIPT (процесс-подстановка словом — `/dev/fd/63`, depcheck._pairs)."""
    if not words:
        return False
    name, args = _basename(words[0]), words[1:]
    if name in _SOURCES:
        # `source [-p путь] [--] файл` (bash 5.3).
        if args[:1] == ["-p"]:
            args = args[2:]
        args = args[1:] if args[:1] == ["--"] else args
        return bool(args) and bool(_STDIN_SCRIPT.fullmatch(args[0]))
    if name not in _SHELLS:
        return False
    if name == "ssh":
        return not _ssh_command(args)
    # Флаги — до первого позиционного слова или `--`: первый из `-s` и `-c` решает; режим «только разбор»
    # (_noexec) после всех флагов — без исполнения.
    runs = None
    noexec = False
    i = 0
    while i < len(args) and args[i] != "--":
        a = args[i]
        noexec = _noexec(a, args[i + 1:i + 2], noexec)
        if a.startswith("--"):
            if a in _SHELL_VALUE_FLAGS:
                i += 1
        elif a.startswith(("-", "+")) and len(a) > 1:
            if runs is None and a[0] == "-" and "s" in a[1:]:
                runs = True
            elif runs is None and a[0] == "-" and "c" in a[1:]:
                runs = False
            if _takes_value(a, _SHELL_VALUE_FLAGS):
                i += 1
        else:
            break
        i += 1
    if noexec:
        return False
    if runs is not None:
        return runs
    rest = args[i + 1:] if args[i:i + 1] == ["--"] else args[i:]
    # Первое позиционное — скрипт; _STDIN_SCRIPT — снова stdin или процесс-подстановка.
    return not rest or bool(_STDIN_SCRIPT.fullmatch(rest[0]))


# Глобальные опции git со значением следующим словом (git.c, handle_options).
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix",
                    "--attr-source"}
# `--exec` у `git rebase` и его однозначные сокращения: git принимает префикс длинной опции, `--e` неоднозначен
# (`--empty`).
_GIT_REBASE_EXEC = {"--ex", "--exe", "--exec"}
# Короткие опции `git rebase` со значением, кроме `-x`: склейка за ними — значение.
_GIT_REBASE_VALUE_LETTERS = frozenset("sXC")


def _git_runs(args):
    """Команды, которые git запускает из своих слов args: `submodule [флаги] foreach [флаги] <команда>` (одно слово
    — строка оболочки, несколько — команда со словами), `bisect run <команда>`, `rebase -x/--exec <строка>` (и
    сокращения `--ex`, `--exe`, склейка `-x…`, `-ix …`), псевдоним `-c alias.<имя>='!…'`, вызванный подкомандой
    (имя без учёта регистра, как в git; слова за ним — аргументы строки)."""
    aliases = {}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in _GIT_VALUE_FLAGS:
            if args[i] == "-c" and i + 1 < len(args):
                key, _, value = args[i + 1].partition("=")
                section, _, alias = key.partition(".")
                if section.lower() == "alias" and value.startswith("!"):
                    aliases[alias.lower()] = _part_of(args[i + 1], value[1:])
            i += 2
        else:
            i += 1
    if i >= len(args):
        return []
    sub, rest = args[i], args[i + 1:]
    if sub.lower() in aliases:
        # Строка псевдонима — строка оболочки, слова за ним — её аргументы "$@" (git 2.x: `git -c alias.z='!echo' z
        # "$x"` печатает значение $x, не исполняя его).
        alias = aliases[sub.lower()]
        return [_part_of(alias, " ".join([alias, _shell_join(rest)]).strip())]
    if sub == "submodule":
        rest = _after_flags(rest, _NO_VALUE_FLAGS)
        if rest[:1] != ["foreach"]:
            return []
        rest = _after_flags(rest[1:], _NO_VALUE_FLAGS)
        return rest if len(rest) == 1 else [_shell_join(rest)] if rest else []
    if sub == "bisect":
        return [_shell_join(rest[1:])] if rest[:1] == ["run"] and rest[1:] else []
    if sub != "rebase":
        return []
    out = []
    for k, a in enumerate(rest):
        following = rest[k + 1] if k + 1 < len(rest) else ""
        if a == "--":
            break
        name, eq, value = a.partition("=")
        if name in _GIT_REBASE_EXEC:
            out.append(_part_of(a, value) if eq else following)
        elif a.startswith("-") and not a.startswith("--"):
            for j in range(1, len(a)):
                if a[j] == "x":
                    out.append(_part_of(a, a[j + 1:]) or following)
                    break
                if a[j] in _GIT_REBASE_VALUE_LETTERS:
                    break
    return out


def _expanded(word):
    """Слово — подстановка или переменная целиком или частью (`$X`, `${V:-add}`, `` `…` ``, заглушка `$_`
    depcheck на месте длинного раскрытия): его слова известны только при исполнении. None — слова нет."""
    return word is not None and ("$" in word or "`" in word)


# Флаги справки менеджеров, у которых `-h` — не справка или справкой не задокументирован: у winget `-h` —
# `--silent` (справка — `-?`, `--help`), у nuget справка — `--help`, `-help`, `-?` (имена флагов без учёта
# регистра); у port, nix, nix-env, scoop, vcpkg — только `--help`.
_HELP_FLAGS = {"winget": ("--help", "-?"), "nuget": ("--help", "-help", "-?"), "port": ("--help",),
               "nix": ("--help",), "nix-env": ("--help",), "scoop": ("--help",), "vcpkg": ("--help",)}


def _asks_help(name, args):
    """Среди слов до `--` — флаг справки менеджера name (`--help`, `-h`; _HELP_FLAGS): менеджер печатает справку и
    ничего не ставит, какую бы подкоманду ни дала подстановка (`uv $c --help`)."""
    flags = _HELP_FLAGS.get(name, ("--help", "-h"))
    for a in args:
        if a == "--":
            return False
        if (a.lower() if name == "nuget" else a) in flags:
            return True
    return False


def _expanded_install(words):
    """Имя команды — подстановка или переменная (`$M`, `$(which npm)`, заглушка `$_` depcheck, `` `which npm` ``), за
    ним глагол установки и слово-не-флаг."""
    return (len(words) > 2 and _expanded(words[0]) and words[1] in _INSTALL_VERBS
            and _has(words[2:], _EXPANDED_VALUE_FLAGS))


def _cut_script(words):
    """Строка, которую команда исполняет, может стоять за пределом слов: `eval` или оболочка, у которой до предела
    одни флаги (`-c` и строка или скрипт — дальше) или `-c` без строки."""
    name = _basename(words[0]) if words else None
    if name == "eval":
        return True
    return name in _C_SHELLS and _inline_script(words) is None and not _after_flags(words[1:], _SHELL_VALUE_FLAGS)


# Имена, с которыми _is_add может найти добавление: менеджеры и запускатели (`npx`, `corepack`, `python -m`);
# ещё _PIP и _PYTHON — шаблонами (_installer).
_INSTALLERS = set(_GLOBAL_FLAGS) | set(_OTHER_MANAGERS) | set(_RUNNERS) | _CONDAS | _NPX | {"gem", "swift", "corepack"}


def _installer(word):
    """Слово — имя менеджера или запускателя, у которого _is_add может найти добавление."""
    name = _basename(word)
    return name in _INSTALLERS or bool(_PIP.match(name) or _PYTHON.match(name))
