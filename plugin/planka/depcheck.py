"""Детерминированный разбор команды Bash: добавляет ли она пакет в проект."""
import bisect
import re
import shlex
from collections import deque
from collections.abc import Set
from itertools import chain, islice

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
_C_FLAGS = ("-c", "--command")
# Флаг `-x`/`--exec` watch: команда запускается без `sh -c`. По optstring watch `+bCcefd::ghq:n:prs:twvx` буква
# со значением (`n`, `q`, `s`, необязательным — `d`) забирает остаток склейки: `-xn5` — `-x`, `-n5x` и `-dx` — нет.
_WATCH_EXEC = re.compile(r"^(?:--exec$|-[bCcefghprtwv]*x)")
# Оператор перенаправления в начале слова: `2>&1`, `>log`, `&>/dev/null`, `<<EOF`; без цели в слове
# (`>`, `2>`) цель — следующее слово.
_REDIRECT = re.compile(r"^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:&>>|&>|>>|>&|>\||>|<<<|<<-|<<|<&|<>|<)")
# Команды, которым тело heredoc, строка here-string и вход конвейера передаются как команды, если у них нет
# скрипта и `-c`.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ssh"}
# Скрипт-аргумент, который читает stdin: оболочка с ним — тоже без скрипта.
_STDIN_SCRIPTS = {"-", "/dev/stdin"}
# Команды, исполняющие в текущей оболочке файл процесс-подстановки `<(…)` или stdin со скриптом из _STDIN_SCRIPTS.
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
# Глубина разбора вложенных команд: `sh -c`, `eval`, подстановка в двойных кавычках.
_MAX_DEPTH = 4
# Обёрток, снимаемых с одного сегмента (_command_info); слова за последней остаются как есть.
_MAX_WRAPPERS = 16

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
# `gem install`: псевдоним `i` (ALIAS_COMMANDS RubyGems) и однозначные сокращения имени (CommandManager, от `ins`:
# `in` — ещё и `info`).
_GEM_INSTALL = {"i", "ins", "inst", "insta", "instal", "install"}
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
                 "dart": _NO_VALUE_FLAGS, "flutter": _NO_VALUE_FLAGS,
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
# Пакеты, без которых окружение conda не создаётся: `conda create -n x python=3.11` нового пакета не вносит.
_CONDA_BASE = {"python", "pip"}
# Подкоманды, исполняющие следующую команду или программу пакета (`npm exec pnpm add x`, `bundle exec gem install
# x`), как npx.
_EXECS = {"npm": {"exec", "x"}, "pnpm": {"dlx", "exec"}, "yarn": {"dlx", "exec"}, "bun": {"x"},
          "bundle": {"exec"}}
# Запускатели пакета npm (`npx pnpm add x`): флаги со значением перед пакетом.
_NPX_VALUE_FLAGS = {"-p", "--package", "-c", "--call", "--cache", "--userconfig"}
_NPX = {"npx", "bunx", "pnpx"}
# Версия в спецификации пакета: `pnpm@9`, `@scope/x@1`.
_VERSION = re.compile(r"(?<=.)@[^/]*$")
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
# Длинные имена пробного прогона apt и apt-get без `--`, в нижнем регистре.
_APT_SIMULATE_LONG = {f[2:] for f in _APT_SIMULATE if f.startswith("--")}
_APTS = {"apt", "apt-get", "aptitude"}
# Псевдонимы `npm install`.
_NPM_INSTALL = {"install", "i", "add", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal",
                "isntall", "it", "install-test"}

# Локальные архивы пакетов.
_ARCHIVES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".gem", ".conda", ".deb",
             ".rpm", ".apk", ".snap", ".flatpak", ".nupkg", ".rock", ".rockspec", ".ez")
# Протоколы локального пакета: путь (`file:`) и пакет своего workspace (`workspace:`, `link:`, `portal:` у
# pnpm и yarn).
_LOCAL_PROTOCOLS = ("file:", "workspace:", "link:", "portal:")
# Именованное требование на локальный пакет: `name@file:///x`, `name @ file:x`, `@org/x@workspace:*`.
_NAMED_LOCAL = re.compile(r"@\s*(?:file|workspace|link|portal):")
# Сколько первых символов сегмента _command разбирает на слова: пакет, названный дальше, не виден — команду
# установки без пакета в длинном сегменте отдаёт _doubt.
_WORDS_LIMIT = 4096
# Сколько пар «флаг значение» перед подкомандой по очереди склеивает _loosened.
_LOOSE_FLAGS = 4
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
# Глаголы установки после имени-подстановки (`$M install x`).
_INSTALL_VERBS = {"install", "i", "add", "require"}
# Глаголы установки за именем менеджера в словах неизвестной программы (_launches_install; флаг за менеджером —
# тоже: `pacman -S`, `npm -g i`): `go get`, `pipx inject`, `dnf in`, `bun a`, `apt satisfy`.
_LAUNCH_VERBS = _INSTALL_VERBS | {"get", "in", "inject", "a", "satisfy"}
# Сколько пар «менеджер, глагол или флаг» в словах неизвестной программы разбирает _launches_install; больше —
# сомнение.
_MAX_LAUNCH_PAIRS = 16
# Программы, чьи слова — данные, а не команда: текст, поиск, справка, git (команды, которые git запускает сам, —
# _git_runs).
_DATA_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "git", "gh", "cat", "sed", "awk", "head",
                  "tail", "jq", "wc", "ls", "diff", "man", "tldr", "help", "info", "apropos", "which", "type",
                  "whereis", "sort", "uniq", "tee", "less", "more", "test", "[", "[[", "true", "false", ":", "read",
                  "export", "declare", "local", "set", "unset", "alias"}


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
        elif _basename(word) in _WRAPPERS and wrappers < _MAX_WRAPPERS:
            wrappers += 1
            name = _basename(word)
            xargs = xargs or name == "xargs"
            value_flags, operands = _WRAPPERS[name]
            words.popleft()
            if name in _C_WRAPPERS:
                _c_wrapper(words, value_flags)
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
                if _takes_value(flag, value_flags) and words:
                    words.popleft()
            for _ in range(min(operands, len(words))):
                words.popleft()
            if name == "flock" and words and words[0][0] in _C_FLAGS:
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
    wrapped = bool(words) and _basename(words[0][0]) in _WRAPPERS
    return out, marker, xargs, cut, wrapped


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
    while words:
        word, lead = words.popleft()
        if word == "--":
            break
        if word.startswith("--"):
            flag, eq, value = word.partition("=")
            if flag in value_flags and not eq:
                value = words.popleft()[0] if words else None
        elif word.startswith("-") and len(word) > 1:
            flag = value = None
            for j in range(1, len(word)):
                if "-" + word[j] in value_flags:
                    flag = "-" + word[j]
                    value = word[j + 1:] or (words.popleft()[0] if words else None)
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
    if word.startswith(_LOCAL_PROTOCOLS) or _NAMED_LOCAL.search(word):
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
    if mode == _COMPUTED and depth and _expanded_install(words):
        return True
    w = [_basename(words[0]), *words[1:]]
    name = w[0]
    if name == "corepack" or name in _NPX:
        # `corepack <менеджер>[@версия] …`, `npx [флаги] <пакет>[@версия] …`: исполняется программа пакета.
        rest = w[1:] if name == "corepack" else _after_flags(w[1:], _NPX_VALUE_FLAGS)
        return bool(rest) and _is_add([_VERSION.sub("", rest[0]), *rest[1:]], depth + 1, mode)
    if _PYTHON.match(name):
        module = _python_module(w)
        return module is not None and _is_add(module, depth + 1, mode)
    if mode == _LOOSE and _has_subcommand(name) and any(_is_add(v, depth) for v in _loosened(w)):
        return True
    if name in _OTHER_MANAGERS:
        sub, args = _sub_args(w)
        if mode == _COMPUTED and _has_subcommand(name) and _expanded(sub) and not _asks_help(name, args):
            return True
        if name in _RUNNERS and sub == "run":
            return _is_add(_after_flags(args, _RUNNERS[name]), depth + 1, mode)
        return not _dry_run(w, mode == _MASKED) and _OTHER_MANAGERS[name](w)
    dry = _dry_run(w, mode == _MASKED)
    # `cargo +nightly install …`: выбор toolchain rustup перед подкомандой.
    if name == "cargo" and w[1:2] and w[1].startswith("+"):
        w = [w[0], *w[2:]]
    if name == "npm":
        w = _npm_subcommand(w)
    elif name in _GLOBAL_FLAGS:
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
    if (mode == _COMPUTED and _expanded(sub) and (_has_subcommand(name) or name in _CONDAS or name == "gem")
            and not _asks_help(name, args)):
        return True
    if name in _RUNNERS and sub == "run":
        rest = _after_flags(args, _RUNNERS[name])
        # `hatch run env:cmd` — команда cmd в окружении env.
        if name == "hatch" and rest:
            rest = [rest[0].partition(":")[2] or rest[0], *rest[1:]]
        return _is_add(rest, depth + 1, mode)
    if sub in _EXECS.get(name, ()):
        rest = _after_flags(args, _NPX_VALUE_FLAGS | _GLOBAL_FLAGS[name])
        return bool(rest) and _is_add([_VERSION.sub("", rest[0]), *rest[1:]], depth + 1, mode)
    if dry:
        return False
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
        # `--workspace` берёт пакеты только из workspace проекта.
        return sub in ("add", "install", "i") and "--workspace" not in args and _has(args, _PNPM_VALUE_FLAGS)
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
            return _pip_add(args[1:], _UV_TOOL_VALUE_FLAGS) or _with_packages(args[1:])
        return False
    if name == "pipx":
        if sub == "install":
            return _pip_add(args, _PIPX_VALUE_FLAGS)
        if sub == "inject":
            # Первое позиционное — имя окружения pipx.
            return len(list(_positionals(_join_at(args), _PIPX_VALUE_FLAGS, pip=True))) > 1
        if sub == "runpip":
            # `pipx runpip <окружение> <аргументы pip>` — pip внутри окружения.
            rest = _after_flags(args, _PIPX_VALUE_FLAGS)
            return bool(rest) and _is_add(["pip", *rest[1:]], depth + 1, mode)
        return False
    if name == "pipenv":
        return sub == "install" and _pip_add(args, _PIPENV_VALUE_FLAGS)
    if name in _CONDAS:
        if sub == "create":
            # Спецификация `канал::имя=версия`; окружение с одним интерпретатором пакетов не вносит.
            return any(re.split(r"[=<>!~\[ ]", p, maxsplit=1)[0].rpartition("::")[2].lower() not in _CONDA_BASE
                       for p in _positionals(args, _CONDA_VALUE_FLAGS))
        return sub == "install" and _has(args, _CONDA_VALUE_FLAGS)
    if name in ("cargo", "bundle") and _flag_value(args, "--path") is not None:
        # `--path` — локальный пакет под названным именем.
        return False
    if name == "cargo":
        if sub == "install":
            return _has(args, _CARGO_INSTALL_VALUE_FLAGS) or _git_source(args)
        return sub == "add" and (_has(args, _CARGO_VALUE_FLAGS) or _git_source(args))
    if name in _ADD_FLAGS:
        return sub == "add" and _has(args, _ADD_FLAGS[name])
    if name == "gem":
        return sub in _GEM_INSTALL and _has(args, _GEM_VALUE_FLAGS)
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


def _aptitude_simulates(args, flags):
    """Среди флагов aptitude — пробный прогон: `-s` (и в склейке `-qs`), `--simulate` и синонимы. Остаток склейки
    после флага со значением (`-tsid`) — значение."""
    for a in args:
        if a in _APT_SIMULATE:
            return True
        if a.startswith("-") and not a.startswith("--"):
            for letter in a[1:]:
                if letter == "s":
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
    """Разбор флагов apt и apt-get по CommandLine apt: (пробный прогон, args без слов-значений булевых флагов и
    флагов уровня). Пробный прогон — последнее значение `-s`, `--simulate` и синонимов (имя без учёта регистра).
    Значение — после `=`, остаток склейки (`-s0`, `-q2`), следующее слово без `-` (`--simulate no`, `-q 2`) или
    слово перед `-` имени (`--no-simulate`); не булево после `=` или перед `-`, не целое после `=` у `-q` —
    ошибка, установки нет (True). Значения флагов из flags остаются в args. `--` завершает флаги."""
    simulate = False
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
                if low not in _APT_SIMULATE_LONG:
                    prefix, dash, rest = low.partition("-")
                    if not dash or rest not in _APT_SIMULATE_LONG and rest != "s":
                        i += not eq and a in flags and following is not None
                        continue
                simulate, eaten = _apt_sense(argument, bool(eq), prefix)
                if simulate is None:
                    return True, args
            if eaten and not eq:
                eaten_words.add(i)
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for j, letter in enumerate(a[1:], 1):
                rest = a[j + 1:]
                certain = rest.startswith("=")
                argument = rest[1:] if certain else rest or following
                if letter in ("s", "q"):
                    if letter == "s":
                        simulate, eaten = _apt_sense(argument, certain, None)
                        error = simulate is None
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
    return simulate, [a for k, a in enumerate(args) if k not in eaten_words]


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
    # `satisfy` ставит пакеты, удовлетворяющие строкам зависимостей (`apt satisfy 'jq (>= 1)'`).
    return sub in ("install", "satisfy") and _has(args, flags)


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
    "vcpkg": _vcpkg, "conan": _conan, "r": _r, "rscript": _r,
}

# Менеджеры, чьё имя с глаголом установки в словах неизвестной программы — сомнение (_launches_install); у R
# установка — выражением `-e`, не глаголом.
_PAIR_MANAGERS = (set(_GLOBAL_FLAGS) | set(_OTHER_MANAGERS) | set(_RUNNERS) | _CONDAS | {"gem"}) - {"r", "rscript"}
# Программы, разбор которых детектор знает: менеджеры, запускатели, обёртки, оболочки, `eval`, `source`, команды
# _launched и программы-данные; у прочих _launches_install ищет менеджер с глаголом установки в словах.
_KNOWN_PROGRAMS = (_PAIR_MANAGERS | {"r", "rscript", "corepack", "eval", "find", "trap", "cmd", "pwsh", "powershell",
                                     "nix-shell", "mise", "rtx"}
                   | _NPX | set(_WRAPPERS) | _SHELLS | _C_SHELLS | _SOURCES | _DATA_PROGRAMS)
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
    "flatpak": _FLATPAK_VALUE_FLAGS, "nix-env": _NIX_VALUE_FLAGS, "nix": _NIX_VALUE_FLAGS,
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
    нет или оболочка её только разбирает (_noexec)."""
    if not words:
        return None
    name = _basename(words[0])
    if name == "eval":
        return " ".join(words[1:])
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
                return None if noexec else a.partition("=")[2]
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
    """Тело heredoc этой команды — команды: оболочка без скрипта и без `-c` (или с `-s`), ssh без
    удалённой команды, `source` и `.` со скриптом `-` или `/dev/stdin`."""
    if not words:
        return False
    name, args = _basename(words[0]), words[1:]
    if name in _SOURCES:
        args = args[1:] if args[:1] == ["--"] else args
        return args[:1] in ([s] for s in _STDIN_SCRIPTS)
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
    # Первое позиционное — скрипт; `-` и `/dev/stdin` — снова stdin.
    return not rest or rest[0] in _STDIN_SCRIPTS


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
        if not any(_heredoc_runs(_command(s)[0]) for s in segments[logical_start:]):
            pending = deque(opened)
            if opened and cur[1] and carry is None:
                # Конвейер продолжается за телом: приёмник — на следующих строках.
                carry = (logical_start, [])
        if carry is not None and not cur[1]:
            if any(_heredoc_runs(_command(s)[0]) for s in segments[carry[0]:]):
                scripts.append("\n".join(carry[1]))
            carry = None
        opened = []
        logical_start = len(segments)
    if body:
        bodies.append("\n".join(body))
    flush()
    if carry is not None and any(_heredoc_runs(_command(s)[0]) for s in segments[carry[0]:]):
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
    name = _basename(words[0])
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
    name, args = _basename(words[0]), words[1:]
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
                _heredoc_runs(words) or _basename(words[0]) in _SOURCES and len(words) == 1)
        if not runs[target]:
            continue
        if _heredoc_cat(segments[source]) and _heredoc_runs(_command(segments[target])[0]):
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
    script = _inline_script(words)
    if script is not None:
        out.append(script)
    if _heredoc_runs(words):
        out.extend(_herestrings(segment))
    if words:
        out.extend(_launched(_basename(words[0]), words[1:]))
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
                    aliases[alias.lower()] = value[1:]
            i += 2
        else:
            i += 1
    if i >= len(args):
        return []
    sub, rest = args[i], args[i + 1:]
    if sub.lower() in aliases:
        return [" ".join([aliases[sub.lower()], shlex.join(rest)]).strip()]
    if sub == "submodule":
        rest = _after_flags(rest, _NO_VALUE_FLAGS)
        if rest[:1] != ["foreach"]:
            return []
        rest = _after_flags(rest[1:], _NO_VALUE_FLAGS)
        return rest if len(rest) == 1 else [shlex.join(rest)] if rest else []
    if sub == "bisect":
        return [shlex.join(rest[1:])] if rest[:1] == ["run"] and rest[1:] else []
    if sub != "rebase":
        return []
    out = []
    for k, a in enumerate(rest):
        following = rest[k + 1] if k + 1 < len(rest) else ""
        if a == "--":
            break
        name, eq, value = a.partition("=")
        if name in _GIT_REBASE_EXEC:
            out.append(value if eq else following)
        elif a.startswith("-") and not a.startswith("--"):
            for j in range(1, len(a)):
                if a[j] == "x":
                    out.append(a[j + 1:] or following)
                    break
                if a[j] in _GIT_REBASE_VALUE_LETTERS:
                    break
    return out


def _launched(name, args):
    """Команды, которые name запускает из своих аргументов args, строками для разбора."""
    if name == "git":
        return _git_runs(args)
    if name == "ssh":
        command = _ssh_command(args)
        return [" ".join(command)] if command else []
    if name == "trap":
        rest = _after_flags(args, _NO_VALUE_FLAGS)
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
        name = _basename(word)
        if word == DEP_OK_MARKER:
            return False
        if (_ENV_ASSIGN.match(word) or word.startswith(("-", "+")) or word in _KEYWORDS or name in _WRAPPERS
                or name in _C_SHELLS or name in _NPX or name in ("eval", "corepack") or _PYTHON.match(name)):
            i += 1
        elif name in _RUNNERS and words[i + 1:i + 2] == ["run"]:
            i += 2
        else:
            break
    return i < n and _is_add([_VERSION.sub("", words[i]), *words[i + 1:]])


def _soup_add(text):
    """Текст глубже _MAX_DEPTH ставит пакет: кавычки и `\\` сняты, команды разделены по `;`, `&`, `|`, скобкам,
    обратной кавычке и переводу строки, каждая проверена _flat_add."""
    return any(_flat_add(_SOUP_QUOTES.sub("", piece).split()) for piece in _SOUP_SPLIT.split(text))


def _expanded(word):
    """Слово — подстановка или переменная целиком или частью (`$X`, `${V:-add}`, `` `…` ``, заглушка `$` _parse на
    месте `$(…)`): его слова известны только при исполнении. None — слова нет."""
    return word is not None and ("$" in word or "`" in word)


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
    return bool(flat) and (_is_add(flat) or _expanded_install(flat) or _launches_install(flat))


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
    """Имя команды — подстановка или переменная (`$M`, `$(which npm)`, заглушка `$` _parse, `` `which npm` ``), за
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
    if cut == _WHY_BRACES or cut and words and (_installer(words[0]) or _cut_script(words)) and not (
            _PYTHON.match(_basename(words[0])) and _python_target(words)[0] in ("c", "script")):
        return cut
    if _is_add(words, mode=_MASKED):
        return _WHY_DRY
    if _is_add(words, mode=_LOOSE):
        return _WHY_FLAG
    if _is_add(words, mode=_DEEP):
        return _WHY_DEPTH
    if _expanded_install(words) or _expanded_name_install(words):
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
    name = _basename(words[0]) if words else None
    script = _inline_script(words) if name == "eval" or name in _C_SHELLS else None
    texts = ([script] if script is not None else []) + (_herestrings(segment) if _heredoc_runs(words) else [])
    return _WHY_COMPUTED if any(_computed(t) for t in texts) else None


def _launches_install(words):
    """Программа, неизвестная детектору (не менеджер, не обёртка, не оболочка, не запускатель из _launched и не
    программа-данные _DATA_PROGRAMS), получает словами менеджер пакетов и за ним глагол установки или флаг
    (`direnv exec . npm install x`, `docker exec c npm --weird v i x`), и слова с менеджера по его же правилам
    разбора — добавление пакета или сомнение (_is_add, _doubt): программа может запустить их командой. Установка
    без пакета (`docker exec web npm install`) и пробный прогон — не сомнение."""
    if len(words) < 3:
        return False
    name = _basename(words[0])
    # Слово с `=` в кавычках (`'A=1' npm i x`) bash ищет как программу и не находит.
    if name in _KNOWN_PROGRAMS or "=" in name or _PIP.match(name) or _PYTHON.match(name):
        return False
    pairs = 0
    for k in range(1, len(words) - 1):
        head, following = _basename(words[k]), words[k + 1]
        if not (head in _PAIR_MANAGERS or _PIP.match(head)) or not (
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
