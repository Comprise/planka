"""Разбор манифестов пакетов: объявленные внешние зависимости, они же вместе с транзитивными и имя
самого пакета манифеста.

Имена — внешние пакеты из реестра или VCS; локальные (путь, workspace) не входят. Имена PyPI
нормализованы по PEP 503, Composer и crates.io — без регистра, crates.io — с `_` как `-`; npm, Go
и RubyGems — как записаны. Пакет не из реестра по умолчанию (git, URL, другой реестр) — имя с источником
`имя @ источник`, источник для всех пакетов (индекс, репозиторий) — `index <url>`: смена источника имени — новое
имя, другая ссылка того же источника (коммит, тег, ветка) — нет. Подключение файла, который ставится вместе с
манифестом (`-r`, `-c`, `eval_gemfile`), — Include `include <путь>`, не равное строке с тем же текстом; строка
requirements с переменной окружения `${…}` — сама строка, у её URL — без ссылки и фрагмента, где нет `${…}`, у
архива — колесо `имя @ каталог`, другой архив — URL с именем файла.
"""
import json
import re
import tomllib

# Ошибки разбора JSON и TOML (TOMLDecodeError — подкласс ValueError); RecursionError — на глубокой вложенности.
_PARSE_ERRORS = (ValueError, RecursionError)

_REQUIREMENTS_NAME = re.compile(r"requirements.*\.(?:txt|in)\Z")
_REQUIREMENTS_DIR_FILE = re.compile(r".*\.(?:txt|in)\Z")
_KINDS = {
    "package.json": "package.json",
    "composer.json": "composer.json",
    "pyproject.toml": "pyproject",
    "Cargo.toml": "cargo",
    "go.mod": "gomod",
    "Gemfile": "gemfile",
    "gems.rb": "gemfile",
}


_FOLDED_KINDS = {name.casefold(): value for name, value in _KINDS.items()}


def kind(path, fold=False):
    """Вид манифеста по имени файла или None.

    requirements — `requirements*.txt`, `requirements*.in` и `*.txt`, `*.in` в каталоге
    `requirements/`; разделитель пути — `/` или `\\`. Регистр имени значим; fold — без учёта регистра только
    базового имени и только у имён _KINDS (файловая система без учёта регистра: `PACKAGE.JSON` — тот же
    package.json); имена requirements и каталоги — как написаны.
    """
    parts = re.split(r"[\\/]", path)
    base = parts[-1]
    if base in _KINDS:
        return _KINDS[base]
    if fold and base.casefold() in _FOLDED_KINDS:
        return _FOLDED_KINDS[base.casefold()]
    if _REQUIREMENTS_NAME.match(base):
        return "requirements"
    if len(parts) > 1 and parts[-2] == "requirements" and _REQUIREMENTS_DIR_FILE.match(base):
        return "requirements"
    return None


def _pep503(name):
    return re.sub(r"[-_.]+", "-", name).lower()


# Начало требования PEP 508: имя; хвостовые `.`, `_`, `-` отрезаются отдельно, без отката регулярки.
_PEP508_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _sourced(name, source):
    """Имя пакета из источника не по умолчанию."""
    return f"{name} @ {source}"


def _index(url):
    return f"index {url.rstrip('/')}"


# Ссылка VCS в конце URL (`git+https://host/repo.git@v1`): коммит, тег или ветка того же источника.
_VCS_REF = re.compile(r"@[^/@]*\Z")


# Имя файла архива пакета в конце пути URL: колесо, sdist, tarball npm.
_ARCHIVE = re.compile(r"[^/]*\.(?:whl|zip|tar|tgz|tbz2|txz|tar\.(?:gz|bz2|xz))\Z", re.IGNORECASE)


# Схема VCS-URL pip: `git+…`, `hg+…`, `svn+…`, `bzr+…`.
_VCS = re.compile(r"[A-Za-z]+\+")


def _url_source(url):
    """URL пакета без фрагмента (`#egg=`, `#sha256=`); у VCS (`git+…`) — без ссылки `@…`, у архива — каталог
    без имени файла и запроса: версия пакета живёт в имени файла, как ссылка у VCS."""
    url = url.split("#", 1)[0].strip()
    if _VCS.match(url):
        return _VCS_REF.sub("", url)
    path = url.split("?", 1)[0]
    if _URL.match(path) and _ARCHIVE.search(path) and path.count("/") > 2:
        return _ARCHIVE.sub("", path)
    return url


def _pep508_name(spec):
    """Нормализованное имя требования PEP 508 или None: нет имени, ссылка `name @ file:` — локальная. Ссылка
    `name @ url` — имя с источником (_sourced)."""
    if not isinstance(spec, str):
        return None
    m = _PEP508_NAME.match(spec)
    if not m:
        return None
    name = m.group(1).rstrip("._-")
    rest = spec[m.end():].lstrip()
    if rest.startswith("["):
        rest = rest[rest.find("]") + 1:].lstrip() if "]" in rest else ""
    if rest.startswith("@"):
        url = rest[1:].strip().split(";", 1)[0].split()
        if not url or url[0].startswith("file:"):
            return None
        return _sourced(_pep503(name), _url_source(url[0]))
    return _pep503(name)


def _json_object(text):
    try:
        data = json.loads(text)
    except _PARSE_ERRORS:
        return None
    return data if isinstance(data, dict) else None


def _toml(text):
    try:
        return tomllib.loads(text)
    except _PARSE_ERRORS:
        return None


def _table(data, *keys):
    """Вложенная таблица по ключам; не таблица на пути — пустой словарь."""
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


# Спецификаторы npm, ссылающиеся на локальный пакет.
_NPM_LOCAL = re.compile(r"(?:workspace:|file:|link:|portal:|\.{0,2}/|~/)")
# Спецификатор не из реестра по умолчанию: со схемой (`github:`, `git+https:`, `https:`, `jsr:`), адрес git
# `git@host:repo` или `user/repo` GitHub; `catalog:` pnpm — версия из реестра.
_NPM_SOURCE = re.compile(r"(?!catalog:)[A-Za-z][A-Za-z0-9+.-]*:|[^\s/@:#]+@[^\s/:#]+:|[^\s/@:#][^\s/:#]*/\S")


def _npm_spec(name, spec):
    """Имя, которое ставит спецификатор spec пакета name; None — местный пакет или ссылка на версию
    (`$имя` в overrides). Псевдоним `npm:настоящее@версия` ставит настоящий пакет; спецификатор не из реестра —
    имя с источником без ссылки `#…`."""
    if not isinstance(spec, str):
        return name
    spec = spec.strip()
    if _NPM_LOCAL.match(spec) or spec.startswith("$"):
        return None
    if spec.startswith("npm:"):
        real = spec[4:]
        at = real.find("@", 1)
        return real if at < 0 else real[:at]
    if _NPM_SOURCE.match(spec):
        return _sourced(name, _url_source(spec) if re.match(r"https?://", spec) else spec.split("#", 1)[0])
    return name


def _override_target(key):
    """Пакет ключа overrides npm (`имя`, `имя@версия`), resolutions yarn (`**/a/имя`) и pnpm.overrides
    (`a>имя@версия`)."""
    parts = key.rsplit(">", 1)[-1].split("/")
    name = "/".join(parts[-2:]) if len(parts) > 1 and parts[-2].startswith("@") else parts[-1]
    at = name.find("@", 1)
    return name if at < 0 else name[:at]


def _npm_overrides(table, names):
    """Имена overrides (вложенных по пакетам) и resolutions, которые ставят другой пакет или источник: псевдоним
    `npm:` и спецификатор не из реестра. Версия только закрепляет уже стоящий пакет — не имя. Обход стеком:
    вложенность не ограничена."""
    stack = [(table, None)]
    while stack:
        table, parent = stack.pop()
        for key, spec in table.items():
            target = parent if key == "." else _override_target(key)
            if isinstance(spec, dict):
                stack.append((spec, target))
            elif isinstance(spec, str) and target:
                name = _npm_spec(target, spec)
                if name is not None and name != target:
                    names.add(name)


def _npm(text):
    data = _json_object(text)
    if data is None:
        return None
    names = set()
    # `pnpm.packageExtensions` дописывает зависимости в чужие пакеты (@pnpm/types, PackageExtension): они ставятся.
    extensions = [ext for ext in _table(data, "pnpm", "packageExtensions").values() if isinstance(ext, dict)]
    for table in [data, *extensions]:
        for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for name, spec in _table(table, section).items():
                name = _npm_spec(name, spec)
                if name is not None:
                    names.add(name)
    for table in (_table(data, "overrides"), _table(data, "resolutions"), _table(data, "pnpm", "overrides")):
        _npm_overrides(table, names)
    return frozenset(names)


# Платформенные пакеты Composer (PlatformRepository::PLATFORM_PACKAGE_REGEX): не ставятся из реестра.
_COMPOSER_PLATFORM = re.compile(
    r"(?:php(?:-64bit|-ipv6|-zts|-debug)?|hhvm|(?:ext|lib)-[a-z0-9](?:[_.-]?[a-z0-9]+)*"
    r"|composer(?:-(?:plugin|runtime)-api)?)", re.IGNORECASE)


def _composer(text):
    data = _json_object(text)
    if data is None:
        return None
    names = {name.lower() for section in ("require", "require-dev") for name in _table(data, section)
             if not _COMPOSER_PLATFORM.fullmatch(name)}
    # Репозитории — источник для всех пакетов; `path` — каталог проекта. Репозиторий `package` описывает пакет
    # (одну версию или список) прямо в манифесте: его `dist.url` и `source.url` — источник этого пакета.
    repositories = data.get("repositories")
    for repo in repositories.values() if isinstance(repositories, dict) else _list(repositories):
        if not isinstance(repo, dict) or repo.get("type") == "path":
            continue
        if isinstance(repo.get("url"), str):
            names.add(_index(repo["url"]))
        if repo.get("type") == "package":
            package = repo.get("package")
            for version in package if isinstance(package, list) else [package]:
                if not isinstance(version, dict):
                    continue
                name = version.get("name")
                for key in ("dist", "source"):
                    url = _table(version, key).get("url")
                    if isinstance(url, str):
                        source = _url_source(url)
                        names.add(_sourced(name.lower(), source) if isinstance(name, str) else _index(source))
    return frozenset(names)


# Бэкенды сборки из руководства PyPA (packaging.python.org, «Choosing a build backend» и бэкенды
# расширений) и wheel, который ставят рядом с setuptools: в `build-system.requires` — не новая зависимость.
_BUILD_BACKENDS = frozenset({"setuptools", "wheel", "hatchling", "flit-core", "pdm-backend", "poetry-core",
                             "uv-build", "maturin", "scikit-build-core", "meson-python"})


# Индекс PyPI по умолчанию: не новый источник.
_PYPI_INDEXES = frozenset({"https://pypi.org/simple", "https://pypi.python.org/simple"})


def _pypi_index(url):
    """index <url> индекса или каталога ссылок PyPI; None — индекс по умолчанию или не URL (каталог колёс)."""
    if not isinstance(url, str) or not _URL.match(url) or url.startswith("file:"):
        return None
    return None if url.rstrip("/") in _PYPI_INDEXES else _index(url)


def _table_source(spec):
    """Источник таблицы зависимости uv, poetry (`git`, `url`, `index`, `source`); None — реестр по умолчанию."""
    if not isinstance(spec, dict):
        return None
    if isinstance(spec.get("git"), str):
        return "git+" + _url_source(spec["git"])
    if isinstance(spec.get("url"), str):
        return _url_source(spec["url"])
    for key in ("index", "source"):
        if isinstance(spec.get(key), str):
            return f"index:{spec[key]}"
    return None


def _pyproject(text):
    data = _toml(text)
    if data is None:
        return None
    build = {_pep508_name(spec) for spec in _list(_table(data, "build-system").get("requires"))}
    specs = []
    project = _table(data, "project")
    specs += _list(project.get("dependencies"))
    for group in _table(project, "optional-dependencies").values():
        specs += _list(group)
    # Элементы-таблицы `{include-group = …}` ссылаются на группу того же файла.
    for group in _table(data, "dependency-groups").values():
        specs += _list(group)
    uv = _table(data, "tool", "uv")
    specs += _list(uv.get("dev-dependencies"))
    for group in _table(data, "tool", "pdm", "dev-dependencies").values():
        specs += _list(group)
    for env in _table(data, "tool", "hatch", "envs").values():
        if isinstance(env, dict):
            specs += _list(env.get("dependencies")) + _list(env.get("extra-dependencies"))
    names = {_pep508_name(spec) for spec in specs} | (build - _BUILD_BACKENDS)
    # Ограничения и замены uv (`override-dependencies`, `constraint-dependencies`) и замены pdm
    # (`[tool.pdm.resolution.overrides]`) пакета не добавляют, но требование с URL меняет источник пакета графа.
    for spec in _list(uv.get("override-dependencies")) + _list(uv.get("constraint-dependencies")):
        name = _pep508_name(spec)
        if name is not None and " @ " in name:
            names.add(name)
    for name, spec in _table(data, "tool", "pdm", "resolution", "overrides").items():
        if isinstance(spec, str) and _URL.match(spec) and not spec.startswith("file:"):
            names.add(_sourced(_pep503(name), _url_source(spec)))
    poetry = _table(data, "tool", "poetry")
    tables = [_table(poetry, "dependencies"), _table(poetry, "dev-dependencies")]
    tables += [_table(group, "dependencies") for group in _table(poetry, "group").values()]
    for table in tables:
        for name, spec in table.items():
            if name.lower() == "python":
                continue
            # Список таблиц — ограничения по маркерам (poetry, «Multiple constraints dependencies»): у каждой свой
            # источник, `path` — местный.
            for one in spec if isinstance(spec, list) else [spec]:
                if not (isinstance(one, dict) and "path" in one):
                    source = _table_source(one)
                    names.add(_pep503(name) if source is None else _sourced(_pep503(name), source))
    # Ссылка проекта на свои extras (`app[cli]`) — не внешний пакет.
    for own in (project.get("name"), poetry.get("name")):
        if isinstance(own, str):
            names.discard(_pep503(own))
    # Источник uv — член workspace или каталог; список источников с маркерами — местный, если местный хоть один,
    # иначе каждый источник — имя с источником.
    for name, source in _table(uv, "sources").items():
        sources = source if isinstance(source, list) else [source]
        if any(isinstance(s, dict) and (s.get("workspace") is True or "path" in s) for s in sources):
            names.discard(_pep503(name))
        elif _pep503(name) in names:
            remote = {_table_source(s) for s in sources} - {None}
            if remote:
                names.discard(_pep503(name))
                names.update(_sourced(_pep503(name), s) for s in remote)
    indexes = [index.get("url") for index in _list(uv.get("index")) + _list(poetry.get("source"))
               + _list(_table(data, "tool", "pdm").get("source")) if isinstance(index, dict)]
    indexes += [uv.get("index-url"), *_list(uv.get("extra-index-url")), *_list(uv.get("find-links"))]
    names.update(_pypi_index(url) for url in indexes)
    names.discard(None)
    return frozenset(names)


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_EGG = re.compile(r"[#&]egg=([A-Za-z0-9][A-Za-z0-9._-]*)")
# Комментарий файла требований (pip, req_file.COMMENT_RE `(^|\s+)#.*$`): `#` в начале строки или после пробела;
# строка, которая начинается с него после пробелов, — строка-комментарий. Без отката по пробелам: время линейно.
_REQ_COMMENT = re.compile(r"(?<!\S)#")
_REQ_COMMENT_LINE = re.compile(r"\s*#")
# Переменная окружения, которую pip подставляет в строку требований (req_file.ENV_VAR_RE).
_REQ_ENV = re.compile(r"\$\{[A-Z0-9_]+\}")


class Include(str):
    """Подключение другого файла манифестом (`-r`, `-c` requirements, `eval_gemfile` Gemfile): его содержимое
    ставится, а этот разбор его не видит. Текст — `include <путь>`; равно только подключению того же пути, не строке
    и не имени пакета с тем же текстом (`include @ git+…` — пакет)."""
    __slots__ = ()
    PREFIX = "include "

    def __new__(cls, path):
        return super().__new__(cls, cls.PREFIX + path)

    @property
    def path(self):
        return self[len(self.PREFIX):]

    def __eq__(self, other):
        return isinstance(other, Include) and str.__eq__(self, other)

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((Include, str(self)))


def _requirement_name(line):
    """Имя пакета требования (строка без опций pip) или None.

    VCS- и прочий URL — имя из `#egg=` или из имени файла колеса `.whl` с источником (_sourced); иначе не
    виден. Локальный путь (`./x`, `/x`, `~/x`, `.`, архив с разделителем пути) — не пакет.
    """
    if _URL.match(line):
        url = line.split()[0]
        egg = _EGG.search(url)
        if egg:
            return _sourced(_pep503(egg.group(1).rstrip("._-")), _url_source(url))
        path = url.split("#", 1)[0].split("?", 1)[0]
        if path.endswith(".whl") and not path.startswith("file:"):
            return _sourced(_pep503(path.rsplit("/", 1)[-1].split("-", 1)[0]), _url_source(url))
        return None
    word = line.split(None, 1)[0] if line.split() else ""
    if word.startswith((".", "/", "~")) or "/" in word.split("@", 1)[0] or "\\" in word:
        return None
    return _pep508_name(line)


def _requirement_lines(text):
    """Логические строки файла требований, как у pip (req_file.join_lines, ignore_comments): строка, оканчивающаяся
    на `\\`, кроме строки-комментария, склеивается со следующей без разделителя, `\\` с её краёв снимаются; затем
    снимается комментарий и пробелы по краям, пустые строки пропускаются."""
    joined, parts = [], []
    for line in text.splitlines():
        comment = _REQ_COMMENT_LINE.match(line)
        if line.endswith("\\") and not comment:
            parts.append(line.strip("\\"))
            continue
        if comment:
            line = " " + line
        joined.append("".join(parts) + line)
        parts = []
    if parts:
        joined.append("".join(parts))
    for line in joined:
        m = _REQ_COMMENT.search(line)
        line = (line if m is None else line[:m.start()]).strip()
        if line:
            yield line


class _OptionError(Exception):
    pass


# Слово строки опций, как у shlex.split (posix, whitespace_split, без комментариев): часть без кавычек, `\` с любым
# знаком, строка в '…' и строка в "…" (`\` в ней снимается только перед `"` и `\`); пробелы shlex — ` \t\r\n`.
# Незакрытая кавычка и `\` в конце не совпадают ни с чем: shlex отвергает такую строку. Части не пересекаются,
# совпадение без отката: время линейно.
_SHELL_PIECE = re.compile(r"""[^ \t\r\n'"\\]+|\\([\s\S])|'([^']*)'|"([^"\\]*(?:\\[\s\S][^"\\]*)*)"|([ \t\r\n]+)""")
_SHELL_DQ_ESCAPE = re.compile(r"""\\([\s\S])""")


def _shell_words(text):
    """Слова text, как их даёт shlex.split; ValueError — незакрытая кавычка или `\\` в конце, как у shlex."""
    words, word, quoted, pos = [], [], False, 0
    while pos < len(text):
        m = _SHELL_PIECE.match(text, pos)
        if m is None:
            raise ValueError("строка, которую shlex отвергает")
        pos = m.end()
        escaped, single, double, space = m.groups()
        if space is not None:
            if word or quoted:
                words.append("".join(word))
            word, quoted = [], False
        elif escaped is not None:
            word.append(escaped)
        elif single is not None:
            word.append(single)
            quoted = True
        elif double is not None:
            word.append(_SHELL_DQ_ESCAPE.sub(lambda e: e.group(1) if e.group(1) in '"\\' else e.group(0), double))
            quoted = True
        else:
            word.append(m.group())
    if word or quoted:
        words.append("".join(word))
    return words


# Опции строки файла требований pip (req_file.SUPPORTED_OPTIONS и SUPPORTED_OPTIONS_REQ, pip 26.2): имена опции —
# первое длинное имя (у optparse — dest), значение — список значений (append) или True у флага.
_OPTION_VALUES = [("-i", "--index-url", "--pypi-url"), ("--extra-index-url",), ("-c", "--constraint"),
                  ("-r", "--requirement"), ("-e", "--editable"), ("-f", "--find-links"), ("--no-binary",),
                  ("--only-binary",), ("--all-releases",), ("--only-final",), ("--trusted-host",), ("--use-feature",),
                  ("--hash",), ("-C", "--config-settings")]
_OPTION_FLAGS = ["--no-index", "--prefer-binary", "--require-hashes", "--no-require-hashes", "--pre"]
# Имя опции → (dest, берёт ли значение).
_OPTIONS = {name: (next(n for n in names if n.startswith("--")), True) for names in _OPTION_VALUES for name in names}
_OPTIONS.update({name: (name, False) for name in _OPTION_FLAGS})


def _long_option(opt):
    """Длинная опция по имени или однозначному сокращению (optparse._match_abbrev); _OptionError — нет такой или
    сокращение неоднозначно."""
    if opt in _OPTIONS:
        return opt
    found = [name for name in _OPTIONS if name.startswith("--") and name.startswith(opt)]
    if len(found) != 1:
        raise _OptionError(opt)
    return found[0]


def _parse_options(args):
    """{dest: [значения] или True} слов args, как их разбирает optparse pip (OptionParser._process_args): `--`
    кончает опции; длинная — по имени или сокращению, значение — после `=` или следующим словом, у флага `=` —
    ошибка; короткие склеиваются (`-e<значение>`), опция со значением берёт остаток слова или следующее слово;
    прочие слова — позиционные, они не значат. Один проход по словам: время линейно. _OptionError — строка, которую
    optparse отвергает."""
    opts, i = {}, 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            dest, takes = _OPTIONS[_long_option(name)]
            if not takes:
                if eq:
                    raise _OptionError(name)
                opts[dest] = True
                continue
            if not eq:
                if i == len(args):
                    raise _OptionError(name)
                value = args[i]
                i += 1
            opts.setdefault(dest, []).append(value)
        elif arg.startswith("-") and len(arg) > 1:
            for k in range(1, len(arg)):
                option = _OPTIONS.get("-" + arg[k])
                if option is None:
                    raise _OptionError(arg)
                dest, takes = option
                if not takes:
                    opts[dest] = True
                    continue
                if k + 1 < len(arg):
                    value = arg[k + 1:]
                elif i < len(args):
                    value = args[i]
                    i += 1
                else:
                    raise _OptionError(arg)
                opts.setdefault(dest, []).append(value)
                break
    return opts


def _env_word(word):
    """Слово строки с `${…}`: у слова-URL — без ссылки VCS `@…` и фрагмента `#…` (_url_source), если в снимаемой
    части нет `${`; у архива — его пакет: колесо — `имя @ каталог`, как у _requirement_name, другой архив — URL с
    именем файла, без запроса и фрагмента (имя пакета sdist из имени файла точно не выделить)."""
    if not _URL.match(word):
        return word
    source = _url_source(word)
    if not word.startswith(source) or "${" in word[len(source):]:
        return word
    path = word.split("#", 1)[0].split("?", 1)[0]
    if path == source or _VCS.match(word):
        return source
    if path.endswith(".whl") and not path.startswith("file:"):
        return _sourced(_pep503(path.rsplit("/", 1)[-1].split("-", 1)[0]), source)
    return path


def _env_line(line):
    """Строка с переменной окружения `${…}` как имя: слова через пробел, каждое — _env_word. Другая ссылка того же
    источника и другая версия того же колеса в том же каталоге — не новое имя."""
    return " ".join(_env_word(word) for word in line.split(" "))


def _requirement_line(line):
    """Имена логической строки файла требований, как её читает pip (req_file.break_args_options, get_line_parser,
    handle_line): до первого слова на `-` (слова — через пробел) — требование, остальное — опции: слова shlex
    (_shell_words) и разбор optparse (_parse_options). Требование — его имя (_requirement_name); без него — первое
    `-e`. Строка без требования: первое `-r`, иначе первое `-c` (файл ограничений тоже ставит пакеты своих `-r` и
    меняет индекс), — Include; иначе индексы `-i`, `--extra-index-url` (без `--no-index`) и первый `-f` —
    `index <url>` (_pypi_index). Строку с переменной окружения `${…}` pip подставляет при чтении: имя — сама строка
    (_env_line). Опции, которые pip не разберёт, — пусто: pip отвергнет файл целиком."""
    if _REQ_ENV.search(line):
        return {_env_line(line)}
    words = line.split(" ")
    split = next((i for i, word in enumerate(words) if word.startswith("-")), len(words))
    requirement = " ".join(words[:split])
    try:
        opts = _parse_options(_shell_words(" ".join(words[split:])))
    except (ValueError, _OptionError):
        return set()
    requirement = requirement or opts.get("--editable", [""])[0]
    if requirement:
        return {_requirement_name(requirement)}
    if "--requirement" in opts or "--constraint" in opts:
        return {Include((opts.get("--requirement") or opts["--constraint"])[0])}
    urls = [] if opts.get("--no-index") else opts.get("--index-url", [])[-1:] + opts.get("--extra-index-url", [])
    return {_pypi_index(url) for url in urls + opts.get("--find-links", [])[:1]}


def _requirements(text):
    """Имена файла требований. Заголовок генератора и аннотации `# via` проверку не снимают: их впишет и агент."""
    names = set()
    for line in _requirement_lines(text):
        names.update(_requirement_line(line))
    names.discard(None)
    return frozenset(names)


_CARGO_SECTIONS = ("dependencies", "dev-dependencies", "dev_dependencies", "build-dependencies",
                   "build_dependencies")


def _crate(name):
    return name.lower().replace("_", "-")


def _cargo_source(spec):
    """Источник зависимости Cargo не из crates.io (`git`, `registry`, `registry-index`); None — crates.io."""
    if isinstance(spec.get("git"), str):
        return "git+" + _url_source(spec["git"])
    if isinstance(spec.get("registry"), str):
        return f"registry:{spec['registry']}"
    if isinstance(spec.get("registry-index"), str):
        return _url_source(spec["registry-index"])
    return None


def _cargo(text):
    data = _toml(text)
    if data is None:
        return None
    tables = [_table(data, "workspace", "dependencies")]
    for scope in [data, *_table(data, "target").values()]:
        tables += [_table(scope, section) for section in _CARGO_SECTIONS]
    # `[patch.<источник>]` и `[replace]` (ключ `имя:версия`) подменяют источник крейта во всём графе.
    tables += [patch for patch in _table(data, "patch").values() if isinstance(patch, dict)]
    tables.append({key.split(":", 1)[0]: spec for key, spec in _table(data, "replace").items()})
    names = set()
    for table in tables:
        for key, spec in table.items():
            source = None
            if isinstance(spec, dict):
                # Зависимость по пути и унаследованная из workspace объявлены не здесь.
                if "path" in spec or spec.get("workspace") is True:
                    continue
                # `package = "настоящее"` — ключ лишь локальное имя крейта.
                if isinstance(spec.get("package"), str):
                    key = spec["package"]
                source = _cargo_source(spec)
            names.add(_crate(key) if source is None else _sourced(_crate(key), source))
    return frozenset(names)


_GO_COMMENT = re.compile(r"//.*")
# Пометка транзитивной зависимости: комментарий `// indirect` или `// indirect; …`.
_GO_INDIRECT = re.compile(r"//\s*indirect\s*(?:;|$)")
_GO_BLOCK_START = re.compile(r"([a-z]+)\s*\(\s*$")
_GO_DIRECTIVE = re.compile(r"(require|replace|module)\s+(.*)")


def _go_directory(path):
    """Правая часть `replace` — каталог (modfile.IsDirectoryPath): `.`, `..`, начало `./`, `.\\`, `../`, `..\\`, `/`,
    `\\` или буква диска с `:`."""
    return (path in (".", "..") or path.startswith(("./", ".\\", "../", "..\\", "/", "\\"))
            or len(path) >= 2 and path[0].isascii() and path[0].isalpha() and path[1] == ":")


def _go_path(token):
    return token.strip("\"`")


def _go_replace(spec):
    """((модуль, версия левой части или None), заменённый каталогом, или None; модуль-замена из другого пути или None)
    директивы `replace <модуль> [версия] => <путь или модуль> [версия]`."""
    left, arrow, right = spec.partition("=>")
    old, new = left.split(), right.split()
    if not (arrow and old and new):
        return None, None
    version = _go_path(old[1]) if len(old) > 1 else None
    old, new = _go_path(old[0]), _go_path(new[0])
    if _go_directory(new):
        return (old, version), None
    return None, (new if new != old else None)


def _gomod(text, indirect=False):
    """Пути модулей директив `require`; помеченные `// indirect` — только при indirect. Модуль,
    заменённый каталогом (`replace x => ../x`), — местный, не входит, если у замены нет версии или она — версия из
    `require` этого модуля (замена `x v1 => ./x` другой версии не касается); модуль-замена из другого пути
    (`replace x => evil/x v1`) — входит.

    Незакрытый блок читается до конца файла.
    """
    names, replaced, versions = set(), [], {}
    block = None

    def require(spec):
        words = spec.split()
        path = _go_path(words[0])
        versions.setdefault(path, set()).update(_go_path(w) for w in words[1:2])
        return path
    for line in text.splitlines():
        skip = not indirect and _GO_INDIRECT.search(line)
        line = _GO_COMMENT.sub("", line).strip()
        if not line:
            continue
        if block is not None:
            if line.startswith(")"):
                block = None
            elif block == "require":
                path = require(line)
                if not skip:
                    names.add(path)
            elif block == "replace":
                replaced.append(_go_replace(line))
            continue
        m = _GO_BLOCK_START.match(line)
        if m:
            block = m.group(1)
            continue
        m = _GO_DIRECTIVE.match(line)
        if m and m.group(1) == "require" and m.group(2).split():
            path = require(m.group(2))
            if not skip:
                names.add(path)
        elif m and m.group(1) == "replace":
            replaced.append(_go_replace(m.group(2)))
    local = {old for (old, version), _ in filter(lambda r: r[0], replaced)
             if version is None or version in versions.get(old, ())}
    names = (names - local) | {new for _, new in replaced}
    names.discard("")
    names.discard(None)
    return frozenset(names)


def _gomod_module(text):
    for line in text.splitlines():
        m = _GO_DIRECTIVE.match(_GO_COMMENT.sub("", line).strip())
        if m and m.group(1) == "module" and m.group(2).split():
            return _go_path(m.group(2).split()[0])
    return None


# `gem 'имя'`, `gem("имя")`, `(gem "имя")` с буквальным именем; `plugin` — гем плагина Bundler, его ставит
# Bundler::Plugin::DSL как `gem`. Интерполяция и переменные не совпадают.
_GEM = re.compile(r"""\s*(?:\(\s*)*(?:gem|plugin)\s*(?:\(\s*)?(['"])([A-Za-z0-9._-]+)\1""")
# Подключение файла: `eval_gemfile "путь"` с буквальным путём.
_GEM_EVAL = re.compile(r"""\s*(?:\(\s*)*eval_gemfile\s*(?:\(\s*)?(['"])([^'"]*)\1""")
# Местный гем: опция `path` в строке `gem` — `path:`, `:path =>`, `"path" =>` (Bundler::Dsl.normalize_hash
# приводит ключи к строкам).
_GEM_PATH = re.compile(r"""(?:\bpath:|:path\s*=>|(['"])path\1\s*=>)""")
# Источник гема опцией с буквальной строкой: `git`, `source` и встроенные git_source Bundler (`github`, `gist`,
# `bitbucket`, `gitlab`; Bundler::Dsl.add_git_sources) ключом `git:`, `:git =>` или `"git" =>`.
_GEM_SOURCE_KEYS = "git|github|gist|bitbucket|gitlab|source"
_GEM_SOURCE = re.compile(rf"""(?:\b({_GEM_SOURCE_KEYS}):|:({_GEM_SOURCE_KEYS})\s*=>|(['"])({_GEM_SOURCE_KEYS})\3\s*=>)"""
                         r"""\s*(['"])([^'"]*)\5""")
# Блок `path|git|github|source "значение" do … end`: гемы в нём — из каталога или источника. Значение — до первой
# своей кавычки.
_GEM_BLOCK = re.compile(
    r"""\s*(path|git|github|source)\s*(?:\(\s*)?(?:'([^']*)'|"([^"]*)").*\bdo\s*(?:\|[^|]*\|\s*)?(?:#.*)?$""")
# Источник для всех гемов: оператор `source "url"` без блока (_ruby_statements).
_GEM_GLOBAL_SOURCE = re.compile(r"""\s*source\s*(?:\(\s*)?(['"])([^'"]*)\1\s*(?:\)\s*)?$""")
# Источник RubyGems по умолчанию: не новый источник.
_RUBYGEMS = frozenset({"https://rubygems.org", "http://rubygems.org"})
_RUBY_BLOCK_OPEN = re.compile(r"\bdo\s*(?:\|[^|]*\|\s*)?(?:#.*)?$")
# Ключевое слово в начале строки, которое открывает блок до `end`; условие-модификатор стоит после выражения.
_RUBY_KEYWORD_OPEN = re.compile(r"\s*(?:if|unless|case|begin|while|until|for|def|class|module)\b")
# Блок в одну строку: `if x then y end`.
_RUBY_LINE_END = re.compile(r"\bend\s*(?:#.*)?$")
_RUBY_BLOCK_END = re.compile(r"\s*end\b")
_RUBY_SPECIAL = re.compile(r"""['";#]""")


def _ruby_statements(line):
    """Операторы строки Ruby до комментария: границы — `;` и `#` вне строки в кавычках."""
    out, start, quote = [], 0, None
    for m in _RUBY_SPECIAL.finditer(line):
        c = m.group()
        if quote is not None:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == ";":
            out.append(line[start:m.start()])
            start = m.end()
        else:
            return [*out, line[start:m.start()]]
    return [*out, line[start:]]


def _gem_source(kind, value):
    """Источник гема по виду опции или блока (`git`, `source`, git_source `github`, `gist`, `bitbucket`, `gitlab`) и
    значению."""
    if kind == "git":
        return "git+" + _url_source(value)
    if kind == "source":
        return value.rstrip("/")
    return f"{kind}:{value}"


# Метка местного блока `path … do` в стеке источников _gemfile.
_GEM_LOCAL = object()


def _gemfile(text):
    """Гемы операторов `gem` и `plugin` с буквальным именем, кроме местных: с опцией `path` и внутри блока
    `path … do` без своей опции источника; гем с опцией (_GEM_SOURCE) или в блоке `git`, `github`, `source` — имя с
    источником, опция важнее блока; `eval_gemfile "путь"` — Include;
    `source "url"` без блока, кроме RubyGems, — `index <url>` на любой глубине, в том числе внутри `path … do`: в
    Bundler он глобален. Операторы одной строки, в том числе `source`, разделяет `;`. Источник гема — ближайший
    блок источника на любой глубине (стек блоков): вложенный `path` внутри `source … do` — местный. Вложенность
    считается по `do` в конце строки, ключевому слову блока (`if`, `unless`, `case`, `begin`, `while`, `until`, `for`,
    `def`, `class`, `module`) в начале строки без `end` в конце и `end` в начале; незакрытый блок идёт до конца
    файла."""
    names = set()
    # Источник открытых блоков: прочий блок (`group`, `if`) наследует внешний; пустой стек — реестр.
    stack = []
    for line in text.splitlines():
        m = _GEM_BLOCK.match(line)
        if m:
            value = m.group(2) if m.group(2) is not None else m.group(3)
            stack.append(_GEM_LOCAL if m.group(1) == "path" else _gem_source(m.group(1), value))
            continue
        if _RUBY_BLOCK_OPEN.search(line) or (_RUBY_KEYWORD_OPEN.match(line) and not _RUBY_LINE_END.search(line)):
            stack.append(stack[-1] if stack else None)
            continue
        if _RUBY_BLOCK_END.match(line):
            if stack:
                stack.pop()
            continue
        block = stack[-1] if stack else None
        for statement in _ruby_statements(line):
            m = _GEM_GLOBAL_SOURCE.match(statement)
            if m:
                if m.group(2).rstrip("/") not in _RUBYGEMS:
                    names.add(_index(m.group(2)))
                continue
            m = _GEM_EVAL.match(statement)
            if m:
                names.add(Include(m.group(2)))
                continue
            m = _GEM.match(statement)
            if not m or _GEM_PATH.search(statement, m.end()):
                continue
            option = _GEM_SOURCE.search(statement, m.end())
            if option:
                source = _gem_source(option.group(1) or option.group(2) or option.group(4), option.group(6))
            elif block is _GEM_LOCAL:
                continue
            else:
                source = block
            names.add(m.group(2) if source is None else _sourced(m.group(2), source))
    return frozenset(names)


_PARSERS = {
    "package.json": _npm,
    "composer.json": _composer,
    "pyproject": _pyproject,
    "requirements": _requirements,
    "cargo": _cargo,
    "gomod": _gomod,
    "gemfile": _gemfile,
}


# Старая сторона сравнения: всё, что уже было в файле, вместе с транзитивным go.mod.
_KNOWN = {
    "gomod": lambda text: _gomod(text, indirect=True),
}


# Реестр пакетов вида манифеста: имена одного реестра сравнимы между видами (requirements и pyproject — PyPI).
_REGISTRY = {
    "package.json": "npm",
    "composer.json": "packagist",
    "pyproject": "pypi",
    "requirements": "pypi",
    "cargo": "crates.io",
    "gomod": "go",
    "gemfile": "rubygems",
}


def registry(kind):
    """Реестр пакетов вида kind; None — вид неизвестен."""
    return _REGISTRY.get(kind)


def names(kind, text):
    """Имена внешних зависимостей, объявленных манифестом вида kind; None — текст не разобрать или вид
    неизвестен.

    requirements, go.mod и Gemfile разбираются построчно и None не дают. Транзитивные зависимости
    не входят: `// indirect` go.mod.
    """
    parser = _PARSERS.get(kind)
    if parser is None:
        return None
    return parser(text.removeprefix("\ufeff"))


def known_names(kind, text):
    """names вместе с транзитивными: `// indirect` go.mod.
    Имя отсюда в новом тексте не новое: транзитивная, ставшая прямой, уже стоит."""
    parser = _KNOWN.get(kind) or _PARSERS.get(kind)
    if parser is None:
        return None
    return parser(text.removeprefix("\ufeff"))


def _own_json(text, normalize):
    data = _json_object(text)
    name = None if data is None else data.get("name")
    return normalize(name) if isinstance(name, str) and name else None


def _own_toml(text, *paths, normalize):
    data = _toml(text)
    if data is None:
        return None
    for path in paths:
        name = _table(data, *path[:-1]).get(path[-1])
        if isinstance(name, str) and name:
            return normalize(name)
    return None


_OWN = {
    "package.json": lambda text: _own_json(text, str),
    "composer.json": lambda text: _own_json(text, str.lower),
    "pyproject": lambda text: _own_toml(text, ("project", "name"), ("tool", "poetry", "name"), normalize=_pep503),
    "cargo": lambda text: _own_toml(text, ("package", "name"), normalize=_crate),
    "gomod": _gomod_module,
}


def own_name(kind, text):
    """Имя пакета, который объявляет сам манифест (`name` package.json и composer.json, `project.name` и
    `tool.poetry.name` pyproject, `package.name` Cargo.toml, `module` go.mod), нормализованное как names;
    None — нет имени, текст не разобран или у вида его нет (requirements, Gemfile)."""
    parser = _OWN.get(kind)
    return None if parser is None else parser(text.removeprefix("\ufeff"))


def workspace_members(kind, text):
    """Шаблоны каталогов членов workspace корневого манифеста — `workspaces` package.json (список или
    `{"packages": […]}`) и `[tool.uv.workspace]` pyproject.toml (`members`, `exclude` — с `!` в начале), по
    порядку: последний совпавший шаблон решает. Члена такого workspace менеджер ставит из каталога по имени. Пусто —
    workspace нет или текст не разобран."""
    if kind == "package.json":
        data = _json_object(text.removeprefix("\ufeff")) or {}
        workspaces = data.get("workspaces")
        patterns = _list(workspaces.get("packages") if isinstance(workspaces, dict) else workspaces)
    elif kind == "pyproject":
        workspace = _table(_toml(text.removeprefix("\ufeff")) or {}, "tool", "uv", "workspace")
        patterns = _list(workspace.get("members")) + ["!" + p for p in _list(workspace.get("exclude"))
                                                      if isinstance(p, str)]
    else:
        return []
    return [p for p in patterns if isinstance(p, str)]
