"""Разбор манифестов пакетов: объявленные внешние зависимости, они же вместе с транзитивными и имя
самого пакета манифеста.

Имена — внешние пакеты из реестра или VCS; локальные (путь, workspace) не входят. Имена PyPI
нормализованы по PEP 503, Composer и crates.io — без регистра, crates.io — с `_` как `-`; npm, Go
и RubyGems — как записаны.
"""
import json
import re
import tomllib

# Ошибки разбора JSON и TOML (TOMLDecodeError — подкласс ValueError); RecursionError — на глубокой вложенности.
_PARSE_ERRORS = (ValueError, RecursionError)

_REQUIREMENTS_NAME = re.compile(r"requirements.*\.(?:txt|in)$")
_REQUIREMENTS_DIR_FILE = re.compile(r".*\.(?:txt|in)$")
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


def _pep508_name(spec):
    """Нормализованное имя требования PEP 508 или None: нет имени, ссылка `name @ file:` — локальная."""
    if not isinstance(spec, str):
        return None
    m = _PEP508_NAME.match(spec)
    if not m:
        return None
    name = m.group(1).rstrip("._-")
    rest = spec[m.end():].lstrip()
    if rest.startswith("@") and rest[1:].lstrip().startswith("file:"):
        return None
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


def _npm(text):
    data = _json_object(text)
    if data is None:
        return None
    names = set()
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in _table(data, section).items():
            if isinstance(spec, str):
                if _NPM_LOCAL.match(spec):
                    continue
                # Псевдоним `npm:настоящее@версия` ставит настоящий пакет.
                if spec.startswith("npm:"):
                    real = spec[4:]
                    at = real.find("@", 1)
                    name = real if at < 0 else real[:at]
            names.add(name)
    return frozenset(names)


# Платформенные пакеты Composer (PlatformRepository::PLATFORM_PACKAGE_REGEX): не ставятся из реестра.
_COMPOSER_PLATFORM = re.compile(
    r"(?:php(?:-64bit|-ipv6|-zts|-debug)?|hhvm|(?:ext|lib)-[a-z0-9](?:[_.-]?[a-z0-9]+)*"
    r"|composer(?:-(?:plugin|runtime)-api)?)", re.IGNORECASE)


def _composer(text):
    data = _json_object(text)
    if data is None:
        return None
    return frozenset(name.lower() for section in ("require", "require-dev") for name in _table(data, section)
                     if not _COMPOSER_PLATFORM.fullmatch(name))


# Бэкенды сборки из руководства PyPA (packaging.python.org, «Choosing a build backend» и бэкенды
# расширений) и wheel, который ставят рядом с setuptools: в `build-system.requires` — не новая зависимость.
_BUILD_BACKENDS = frozenset({"setuptools", "wheel", "hatchling", "flit-core", "pdm-backend", "poetry-core",
                             "uv-build", "maturin", "scikit-build-core", "meson-python"})


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
    specs += _list(_table(data, "tool", "uv").get("dev-dependencies"))
    for group in _table(data, "tool", "pdm", "dev-dependencies").values():
        specs += _list(group)
    names = {_pep508_name(spec) for spec in specs} | (build - _BUILD_BACKENDS)
    poetry = _table(data, "tool", "poetry")
    tables = [_table(poetry, "dependencies"), _table(poetry, "dev-dependencies")]
    tables += [_table(group, "dependencies") for group in _table(poetry, "group").values()]
    for table in tables:
        for name, spec in table.items():
            if name.lower() != "python" and not (isinstance(spec, dict) and "path" in spec):
                names.add(_pep503(name))
    # Ссылка проекта на свои extras (`app[cli]`) — не внешний пакет.
    for own in (project.get("name"), poetry.get("name")):
        if isinstance(own, str):
            names.discard(_pep503(own))
    # Источник uv — член workspace или каталог; список источников с маркерами — местный, если местный хоть один.
    for name, source in _table(data, "tool", "uv", "sources").items():
        sources = source if isinstance(source, list) else [source]
        if any(isinstance(s, dict) and (s.get("workspace") is True or "path" in s) for s in sources):
            names.discard(_pep503(name))
    names.discard(None)
    return frozenset(names)


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_EGG = re.compile(r"[#&]egg=([A-Za-z0-9][A-Za-z0-9._-]*)")
# Комментарий файла требований: `#` в начале строки или после пробела.
_REQ_COMMENT = re.compile(r"(?:^|\s)#.*")
_EDITABLE = re.compile(r"(?:-e|--editable)(?:\s+|=)(.*)")


def _requirement_name(line):
    """Имя пакета строки требований без комментария или None.

    VCS- и прочий URL — имя из `#egg=` или из имени файла колеса `.whl`; иначе не виден. Локальный
    путь (`./x`, `/x`, `~/x`, `.`, архив с разделителем пути) — не пакет. Опции, `-r`, `-c` — None.
    """
    m = _EDITABLE.match(line)
    if m:
        line = m.group(1).strip()
    elif line.startswith("-"):
        return None
    if _URL.match(line):
        egg = _EGG.search(line)
        if egg:
            return _pep503(egg.group(1).rstrip("._-"))
        path = line.split("#", 1)[0].split("?", 1)[0]
        if path.endswith(".whl") and not path.startswith("file:"):
            return _pep503(path.rsplit("/", 1)[-1].split("-", 1)[0])
        return None
    word = line.split(None, 1)[0] if line.split() else ""
    if word.startswith((".", "/", "~")) or "/" in word.split("@", 1)[0] or "\\" in word:
        return None
    return _pep508_name(line)


# Заголовок сгенерированного файла в начальном блоке комментариев: pip-compile, `uv pip compile`,
# `uv export` («autogenerated by …»), `pdm export` («@generated by PDM»).
_GENERATED_HEADER = re.compile(r"#.*(?:autogenerated by|@generated by)", re.IGNORECASE)
# Аннотация происхождения pip-compile и uv: `    # via requests`, `pkg==1  # via -r req.in`.
_VIA = re.compile(r"(?:^|\s)#\s*via\b", re.MULTILINE)


def _generated(text):
    """Файл требований сгенерирован из другого объявления и перечисляет транзитивные пакеты."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            break
        if _GENERATED_HEADER.match(line):
            return True
    return bool(_VIA.search(text))


def _requirements(text, generated_ok=False):
    """Имена файла требований; сгенерированный файл — пусто, если не generated_ok."""
    if not generated_ok and _generated(text):
        return frozenset()
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


def _cargo(text):
    data = _toml(text)
    if data is None:
        return None
    tables = [_table(data, "workspace", "dependencies")]
    for scope in [data, *_table(data, "target").values()]:
        tables += [_table(scope, section) for section in _CARGO_SECTIONS]
    names = set()
    for table in tables:
        for key, spec in table.items():
            if isinstance(spec, dict):
                # Зависимость по пути и унаследованная из workspace объявлены не здесь.
                if "path" in spec or spec.get("workspace") is True:
                    continue
                # `package = "настоящее"` — ключ лишь локальное имя крейта.
                if isinstance(spec.get("package"), str):
                    key = spec["package"]
            names.add(_crate(key))
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


def _go_local_replace(spec):
    """Путь модуля, заменённого каталогом, из `replace <модуль> [версия] => <путь>`; иначе None."""
    left, arrow, right = spec.partition("=>")
    old, new = left.split(), right.split()
    if arrow and old and new and _GO_LOCAL_PATH.match(_go_path(new[0])):
        return _go_path(old[0])
    return None


def _gomod(text, indirect=False):
    """Пути модулей директив `require`; помеченные `// indirect` — только при indirect. Модуль,
    заменённый каталогом (`replace x => ../x`), — местный, не входит.

    Незакрытый блок читается до конца файла.
    """
    names, local = set(), set()
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
                local.add(_go_local_replace(line))
            continue
        m = _GO_BLOCK_START.match(line)
        if m:
            block = m.group(1)
            continue
        m = _GO_DIRECTIVE.match(line)
        if m and m.group(1) == "require" and not skip:
            names.add(_go_path(m.group(2).split(None, 1)[0]))
        elif m and m.group(1) == "replace":
            local.add(_go_local_replace(m.group(2)))
    names.discard("")
    return frozenset(names - local)


def _gomod_module(text):
    for line in text.splitlines():
        m = _GO_DIRECTIVE.match(_GO_COMMENT.sub("", line).strip())
        if m and m.group(1) == "module" and m.group(2).split():
            return _go_path(m.group(2).split()[0])
    return None


# `gem 'имя'` и `gem("имя")` с буквальным именем; интерполяция и переменные не совпадают.
_GEM = re.compile(r"""\s*gem\s*\(?\s*(['"])([A-Za-z0-9._-]+)\1""")
# Местный гем: опция `path:` или `:path =>` в строке `gem`.
_GEM_PATH = re.compile(r"(?:\bpath:|:path\s*=>)")
# Блок `path "каталог" do … end`: гемы в нём — из каталога.
_GEM_PATH_BLOCK = re.compile(r"""\s*path\s*\(?\s*['"].*\bdo\s*(?:\|[^|]*\|)?\s*(?:#.*)?$""")
_RUBY_BLOCK_OPEN = re.compile(r"\bdo\s*(?:\|[^|]*\|)?\s*(?:#.*)?$")
_RUBY_BLOCK_END = re.compile(r"\s*end\b")


def _gemfile(text):
    """Гемы строк `gem` с буквальным именем, кроме местных: с `path:` и внутри блока `path … do`.
    Вложенность блоков считается по `do` в конце строки и `end` в начале; незакрытый блок `path` идёт
    до конца файла."""
    names = set()
    depth = 0
    for line in text.splitlines():
        if depth:
            if _RUBY_BLOCK_OPEN.search(line):
                depth += 1
            elif _RUBY_BLOCK_END.match(line):
                depth -= 1
            continue
        if _GEM_PATH_BLOCK.match(line):
            depth = 1
            continue
        m = _GEM.match(line)
        if m and not _GEM_PATH.search(line, m.end()):
            names.add(m.group(2))
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


# Старая сторона сравнения: всё, что уже было в файле, — и транзитивное go.mod, и пакеты сгенерированного
# файла требований.
_KNOWN = {
    "requirements": lambda text: _requirements(text, generated_ok=True),
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
    не входят: `// indirect` go.mod и весь сгенерированный файл требований (`_generated`).
    """
    parser = _PARSERS.get(kind)
    if parser is None:
        return None
    return parser(text.removeprefix("\ufeff"))


def known_names(kind, text):
    """names вместе с транзитивными: `// indirect` go.mod и пакеты сгенерированного файла требований.
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
