"""Разбор манифестов пакетов: объявленные внешние зависимости, они же вместе с транзитивными и имя
самого пакета манифеста.

Имена — внешние пакеты из реестра или VCS; локальные (путь, workspace) не входят. Имена PyPI
нормализованы по PEP 503, Composer и crates.io — без регистра, crates.io — с `_` как `-`; npm, Go
и RubyGems — как записаны. Пакет не из реестра по умолчанию (git, URL, другой реестр) — имя с источником
`имя @ источник`, источник для всех пакетов (индекс, репозиторий) — `index <url>`: смена источника имени — новое
имя, другая ссылка того же источника (коммит, тег, ветка) — нет.
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


def kind(path):
    """Вид манифеста по имени файла или None.

    requirements — `requirements*.txt`, `requirements*.in` и `*.txt`, `*.in` в каталоге
    `requirements/`; разделитель пути — `/` или `\\`. Регистр имени значим.
    """
    parts = re.split(r"[\\/]", path)
    base = parts[-1]
    if base in _KINDS:
        return _KINDS[base]
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


def _url_source(url):
    """URL пакета без фрагмента (`#egg=`, `#sha256=`); у VCS (`git+…`) — без ссылки `@…`, у архива — каталог
    без имени файла и запроса: версия пакета живёт в имени файла, как ссылка у VCS."""
    url = url.split("#", 1)[0].strip()
    if re.match(r"[A-Za-z]+\+", url):
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
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in _table(data, section).items():
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
    # Репозитории — источник для всех пакетов; `path` — каталог проекта.
    repositories = data.get("repositories")
    for repo in repositories.values() if isinstance(repositories, dict) else _list(repositories):
        if isinstance(repo, dict) and repo.get("type") != "path" and isinstance(repo.get("url"), str):
            names.add(_index(repo["url"]))
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
    poetry = _table(data, "tool", "poetry")
    tables = [_table(poetry, "dependencies"), _table(poetry, "dev-dependencies")]
    tables += [_table(group, "dependencies") for group in _table(poetry, "group").values()]
    for table in tables:
        for name, spec in table.items():
            if name.lower() != "python" and not (isinstance(spec, dict) and "path" in spec):
                source = _table_source(spec)
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
# Комментарий файла требований: `#` в начале строки или после пробела.
_REQ_COMMENT = re.compile(r"(?:^|\s)#.*")
_EDITABLE = re.compile(r"(?:-e|--editable)(?:\s+|=)(.*)")
# Индекс и каталог ссылок: `-i URL`, `-iURL`, `--index-url URL`, `--extra-index-url=URL`, `-f`, `--find-links`.
_REQ_INDEX = re.compile(r"(?:-[if]\s*=?|--(?:extra-)?index-url(?:\s*=\s*|\s+)|--find-links(?:\s*=\s*|\s+))(\S+)")


def _requirement_name(line):
    """Имя пакета строки требований без комментария или None.

    VCS- и прочий URL — имя из `#egg=` или из имени файла колеса `.whl` с источником (_sourced); иначе не
    виден. Индекс и каталог ссылок по URL — `index <url>`. Локальный путь (`./x`, `/x`, `~/x`, `.`, архив с
    разделителем пути) — не пакет. Прочие опции, `-r`, `-c` — None.
    """
    m = _EDITABLE.match(line)
    if m:
        line = m.group(1).strip()
    elif line.startswith("-"):
        m = _REQ_INDEX.match(line)
        return _pypi_index(m.group(1)) if m else None
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


def _requirements(text):
    """Имена файла требований. Заголовок генератора и аннотации `# via` проверку не снимают: их впишет и агент."""
    names = set()
    # Строка, оканчивающаяся на `\`, продолжается следующей.
    for line in re.sub(r"\\\r?\n", " ", text).splitlines():
        line = _REQ_COMMENT.sub("", line).strip()
        if line:
            names.add(_requirement_name(line))
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
# Правая часть `replace` — каталог: `./`, `../`, абсолютный путь, в том числе Windows.
_GO_LOCAL_PATH = re.compile(r"(?:\.{1,2}[\\/]|/|[A-Za-z]:[\\/]|\\)")


def _go_path(token):
    return token.strip("\"`")


def _go_replace(spec):
    """(модуль, заменённый каталогом, или None; модуль-замена из другого пути или None) директивы
    `replace <модуль> [версия] => <путь или модуль> [версия]`."""
    left, arrow, right = spec.partition("=>")
    old, new = left.split(), right.split()
    if not (arrow and old and new):
        return None, None
    old, new = _go_path(old[0]), _go_path(new[0])
    if _GO_LOCAL_PATH.match(new):
        return old, None
    return None, (new if new != old else None)


def _gomod(text, indirect=False):
    """Пути модулей директив `require`; помеченные `// indirect` — только при indirect. Модуль,
    заменённый каталогом (`replace x => ../x`), — местный, не входит; модуль-замена из другого пути
    (`replace x => evil/x v1`) — входит.

    Незакрытый блок читается до конца файла.
    """
    names, replaced = set(), []
    block = None
    for line in text.splitlines():
        skip = not indirect and _GO_INDIRECT.search(line)
        line = _GO_COMMENT.sub("", line).strip()
        if not line:
            continue
        if block is not None:
            if line.startswith(")"):
                block = None
            elif block == "require" and not skip:
                names.add(_go_path(line.split(None, 1)[0]))
            elif block == "replace":
                replaced.append(_go_replace(line))
            continue
        m = _GO_BLOCK_START.match(line)
        if m:
            block = m.group(1)
            continue
        m = _GO_DIRECTIVE.match(line)
        if m and m.group(1) == "require" and not skip:
            names.add(_go_path(m.group(2).split(None, 1)[0]))
        elif m and m.group(1) == "replace":
            replaced.append(_go_replace(m.group(2)))
    local = {old for old, _ in replaced}
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


# `gem 'имя'` и `gem("имя")` с буквальным именем; интерполяция и переменные не совпадают.
_GEM = re.compile(r"""\s*gem\s*(?:\(\s*)?(['"])([A-Za-z0-9._-]+)\1""")
# Местный гем: опция `path:` или `:path =>` в строке `gem`.
_GEM_PATH = re.compile(r"(?:\bpath:|:path\s*=>)")
# Источник гема опцией: `git:`, `github:`, `source:` или `:git =>` и т.п. с буквальной строкой.
_GEM_SOURCE = re.compile(r"""(?:\b(git|github|source):|:(git|github|source)\s*=>)\s*(['"])([^'"]*)\3""")
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
    """Источник гема по виду опции или блока (`git`, `github`, `source`) и значению."""
    if kind == "git":
        return "git+" + _url_source(value)
    if kind == "github":
        return "github:" + value
    return value.rstrip("/")


# Метка местного блока `path … do` в стеке источников _gemfile.
_GEM_LOCAL = object()


def _gemfile(text):
    """Гемы операторов `gem` с буквальным именем, кроме местных: с `path:` и внутри блока `path … do` без своей
    опции источника; гем с опцией или в блоке `git`, `github`, `source` — имя с источником, опция важнее блока;
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
            m = _GEM.match(statement)
            if not m or _GEM_PATH.search(statement, m.end()):
                continue
            option = _GEM_SOURCE.search(statement, m.end())
            if option:
                source = _gem_source(option.group(1) or option.group(2), option.group(4))
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
