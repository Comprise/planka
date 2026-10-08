import pathlib
import sys
import unittest

from tests.helpers import assert_linear

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import manifest_watch  # noqa: E402
import manifests  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "manifests"

# Ожидаемые имена каждого манифеста корпуса; путь — относительно FIXTURES.
CORPUS = {
    "npm-cc-harness/package.json": set("""
        @monaco-editor/react @radix-ui/react-dialog @radix-ui/react-dropdown-menu @radix-ui/react-icons
        @radix-ui/react-label @radix-ui/react-select @radix-ui/react-separator @radix-ui/react-slot
        @radix-ui/react-tabs @radix-ui/react-toast @radix-ui/react-tooltip @types/cors @types/express
        @types/react @types/react-dom @typescript-eslint/eslint-plugin @typescript-eslint/parser
        @vitejs/plugin-react ajv autoprefixer chokidar class-variance-authority clsx concurrently cors
        date-fns electron electron-builder eslint eslint-plugin-react-hooks eslint-plugin-react-refresh
        express highlight.js i18next i18next-browser-languagedetector lucide-react mermaid postcss react
        react-dom react-i18next react-markdown react-router-dom reactflow recharts rehype-highlight
        rehype-raw remark-gfm tailwind-merge tailwindcss tailwindcss-animate tsx typescript vite
        vite-plugin-electron vite-plugin-electron-renderer wait-on zustand""".split()),
    "npm-local/package.json": {"lodash", "my-fork", "react", "@types/node", "@scope/tool", "react-dom",
                               "fsevents"},
    "composer-docs/composer.json": {"monolog/monolog", "symfony/console", "phpunit/phpunit"},
    "pyproject-uv-bff/pyproject.toml": {"fastapi", "httpx", "pydantic", "pydantic-settings",
                                        "prometheus-client", "pytest", "strawberry-graphql", "uvicorn",
                                        "ruff", "pytest-asyncio"},
    "pyproject-poetry-fetchartifact/pyproject.toml": {"aiohttp", "mypy", "pylint", "black", "pytest",
                                                      "pytest-asyncio", "isort", "pytest-aiohttp",
                                                      "pytest-cov"},
    # poetry-core и hatchling в `build-system.requires` — стандартные бэкенды сборки, не зависимости.
    "pyproject-poetry-tcat/pyproject.toml": {"bleak", "pytest", "cryptography", "pyreadline3"},
    # Сгенерированные файлы (uv export, pip-compile) перечисляют транзитивные пакеты — не объявление.
    "requirements-uv-export/requirements.txt": set(),
    "requirements-pip-compile/requirements.txt": set(),
    "requirements-esp-idf/requirements/core.txt": set("""
        click construct cryptography esp-coredump esp-idf-diag esp-idf-kconfig esp-idf-monitor
        esp-idf-nvs-partition-gen esp-idf-panic-decoder esp-idf-size esptool freertos-gdb
        idf-component-manager packaging psutil pyclang pyelftools pyparsing pyserial rich rich-click
        setuptools tree-sitter tree-sitter-c""".split()),
    "requirements-pip-docs/requirements.txt": {"pytest", "pytest-cov", "beautifulsoup4", "docopt", "keyring",
                                               "coverage", "mopidy-dirble", "wxpython-phoenix", "myproject",
                                               "urllib3", "requests", "fooproject", "rejected", "green"},
    "cargo-libgit-rs/Cargo.toml": {"autocfg"},
    "cargo-libgit-sys/Cargo.toml": {"libz-sys", "autocfg", "make-cmd"},
    "cargo-book/Cargo.toml": {"rand", "time", "regex", "some-crate", "foo", "foo-core", "serde", "libz-sys",
                              "winhttp", "openssl", "mio", "tempdir", "cc"},
    # `// indirect` — транзитивные, их дописывает `go mod tidy`; в имена не входят.
    "gomod-grpc/go.mod": set("""
        cloud.google.com/go/auth cloud.google.com/go/compute/metadata github.com/cespare/xxhash/v2
        github.com/cncf/xds/go github.com/envoyproxy/go-control-plane github.com/envoyproxy/go-control-plane/envoy
        github.com/golang/glog github.com/golang/protobuf github.com/google/go-cmp github.com/google/uuid
        github.com/spiffe/go-spiffe/v2 go.opentelemetry.io/contrib/detectors/gcp go.opentelemetry.io/otel
        go.opentelemetry.io/otel/metric go.opentelemetry.io/otel/sdk go.opentelemetry.io/otel/sdk/metric
        go.opentelemetry.io/otel/trace golang.org/x/net golang.org/x/oauth2 golang.org/x/sync golang.org/x/sys
        gonum.org/v1/gonum google.golang.org/genproto/googleapis/rpc google.golang.org/protobuf""".split()),
    "gomod-wintun/go.mod": {"golang.org/x/sys"},
    # metric и trace заменены каталогами репозитория (`replace … => ./trace`) — местные.
    "gomod-otel/go.mod": {"github.com/cespare/xxhash/v2", "github.com/go-logr/logr", "github.com/go-logr/stdr",
                          "github.com/google/go-cmp", "github.com/stretchr/testify", "go.opentelemetry.io/auto/sdk"},
    "gemfile-grpc-gateway/Gemfile": {"just-the-docs", "github-pages", "jekyll-optional-front-matter",
                                     "jekyll-default-layout", "jekyll-titles-from-headings",
                                     "jekyll-readme-index", "jekyll-relative-links", "jekyll-include-cache"},
    "gemfile-cmock/Gemfile": {"bundler", "rake", "minitest", "require_all", "constructor", "diy"},
    # Местные зависимости: `{ workspace = true }` и `{ path = … }` в `tool.uv.sources`, `path:` и блок
    # `path … do` Gemfile, `replace` на путь в go.mod.
    "pyproject-uv-workspace/pyproject.toml": {"tqdm"},
    "gemfile-path/Gemfile": {"rails", "puma", "debug", "rspec-rails"},
    "gomod-replace-local/go.mod": {"github.com/go-chi/chi/v5", "google.golang.org/grpc"},
    # Зависимости `[project.optional-dependencies]` — имена; meson-python и wheel — бэкенды сборки.
    "pyproject-gyp-next/pyproject.toml": {"packaging", "setuptools", "pytest", "ruff"},
    "pyproject-pandas/pyproject.toml": set("""
        meson cython numpy versioneer python-dateutil tzdata hypothesis pytest pytest-xdist pyarrow bottleneck
        numba numexpr scipy xarray fsspec s3fs gcsfs odfpy openpyxl python-calamine pyxlsb xlrd xlsxwriter
        pyiceberg tables pyreadstat sqlalchemy psycopg2 adbc-driver-postgresql pymysql adbc-driver-sqlite
        beautifulsoup4 html5lib lxml matplotlib jinja2 tabulate pyqt5 qtpy zstandard pytz fastparquet""".split()),
    # Директива `tool` называет команду модуля из `require`; сама не объявляет зависимость.
    "gomod-tool/go.mod": {"golang.org/x/net", "golang.org/x/tools", "golang.org/x/text", "github.com/golang/mock",
                          "honnef.co/go/tools"},
}

# Имя самого пакета манифеста корпуса (manifests.own_name); не перечисленные — None.
OWN = {
    "npm-cc-harness/package.json": "cc-harness",
    "npm-local/package.json": "app",
    "composer-docs/composer.json": "acme/app",
    "pyproject-uv-bff/pyproject.toml": "bff",
    "pyproject-uv-workspace/pyproject.toml": "albatross",
    "pyproject-poetry-fetchartifact/pyproject.toml": "fetchartifact",
    "pyproject-poetry-tcat/pyproject.toml": "tcat-ble-client",
    "cargo-libgit-rs/Cargo.toml": "libgit",
    "cargo-libgit-sys/Cargo.toml": "libgit-sys",
    "cargo-book/Cargo.toml": "hello-world",
    "gomod-grpc/go.mod": "google.golang.org/grpc",
    "gomod-otel/go.mod": "go.opentelemetry.io/otel",
    "gomod-wintun/go.mod": "golang.zx2c4.com/wintun",
    "gomod-replace-local/go.mod": "example.com/monorepo/api",
    "pyproject-gyp-next/pyproject.toml": "gyp-next",
    "pyproject-pandas/pyproject.toml": "pandas",
    "gomod-tool/go.mod": "example.com/my/thing",
}


# Ожидаемые известные имена (manifest_watch._known) файлов PyPI без проверки (manifest_watch._LEGACY) корпуса;
# путь — относительно FIXTURES.
LEGACY_CORPUS = {
    "legacy-requests/setup.py": set("""
        certifi chardet charset-normalizer idna pysocks pytest pytest-cov pytest-httpbin pytest-mock pytest-xdist
        urllib3""".split()),
    "legacy-pytest/setup.cfg": set("""
        argcomplete attrs colorama exceptiongroup hypothesis importlib-metadata iniconfig mock nose packaging pluggy
        pygments requests setuptools setuptools-scm tomli xmlschema""".split()),
    "legacy-pipenv/Pipfile": set("""
        atomicwrites build click colorama exceptiongroup gunicorn importlib-metadata invoke myst-parser parse pipenv
        pre-commit pypiserver pytest-cov pytz pyyaml semver sphinx sphinx-click sphinxcontrib-spelling stdeb tomli
        twine typing-extensions waitress zipp""".split()),
}


class CorpusTest(unittest.TestCase):
    def test_every_fixture_has_expectation(self):
        files = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file()}
        self.assertEqual(files, set(CORPUS) | set(LEGACY_CORPUS))

    def test_corpus(self):
        for rel, expected in CORPUS.items():
            with self.subTest(rel):
                path = FIXTURES / rel
                text = path.read_text(encoding="utf-8")
                kind = manifests.kind(str(path))
                self.assertIsNotNone(kind)
                self.assertEqual(manifests.names(kind, text), frozenset(expected))
                self.assertEqual(_added(str(path), None, text), sorted(expected))
                self.assertEqual(_added(str(path), text, text), [])
                self.assertEqual(manifests.own_name(kind, text), OWN.get(rel))

    def test_corpus_one_added_line(self):
        # Добавление одной зависимости в настоящий манифест видно ровно одним именем; в сгенерированный файл
        # требований — ни одним: его пакеты транзитивные.
        cases = {
            "npm-cc-harness/package.json": ('"zustand": "^4.5.5"', '"zustand": "^4.5.5",\n    "left-pad": "1.3.0"',
                                            ["left-pad"]),
            "pyproject-uv-bff/pyproject.toml": ('"uvicorn>=0.41.0",', '"uvicorn>=0.41.0",\n    "Left_Pad>=1",',
                                                ["left-pad"]),
            "pyproject-poetry-fetchartifact/pyproject.toml": ('aiohttp = "^3.8.4"', 'aiohttp = "^3.8.4"\nrich = "*"',
                                                              ["rich"]),
            "requirements-esp-idf/requirements/core.txt": ("\nrich\n", "\nrich\nleft_pad\n", ["left-pad"]),
            "requirements-pip-compile/requirements.txt": ("sphinx==", "left-pad==1.0\nsphinx==", []),
            "cargo-libgit-sys/Cargo.toml": ('libz-sys = "1.1.19"', 'libz-sys = "1.1.19"\nserde = "1"', ["serde"]),
            "gomod-grpc/go.mod": ("\tgonum.org/v1/gonum v0.17.0",
                                  "\tgonum.org/v1/gonum v0.17.0\n\tgithub.com/evil/pkg v1.0.0", ["github.com/evil/pkg"]),
            "gemfile-cmock/Gemfile": ('gem "diy"', 'gem "diy"\ngem "nokogiri", "~> 1.16"', ["nokogiri"]),
            "pyproject-gyp-next/pyproject.toml": ('dev = ["pytest", "ruff"]', 'dev = ["pytest", "ruff", "mypy"]',
                                                  ["mypy"]),
            "gomod-tool/go.mod": ("\thonnef.co/go/tools v0.6.1",
                                  "\thonnef.co/go/tools v0.6.1\n\tgithub.com/evil/pkg v1.0.0", ["github.com/evil/pkg"]),
        }
        for rel, (old, new, expected) in cases.items():
            with self.subTest(rel):
                path = FIXTURES / rel
                text = path.read_text(encoding="utf-8")
                self.assertIn(old, text)
                self.assertEqual(_added(str(path), text, text.replace(old, new, 1)), expected)


class KindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "package.json": "package.json",
            "/p/web/package.json": "package.json",
            "composer.json": "composer.json",
            "pyproject.toml": "pyproject",
            "requirements.txt": "requirements",
            "requirements-dev.txt": "requirements",
            "requirements_test.txt": "requirements",
            "/p/requirements/base.txt": "requirements",
            "/p/requirements.in": "requirements",
            "/p/requirements/dev.in": "requirements",
            "C:\\p\\requirements\\base.txt": "requirements",
            "C:\\p\\Cargo.toml": "cargo",
            "Cargo.toml": "cargo",
            "go.mod": "gomod",
            "Gemfile": "gemfile",
            "gems.rb": "gemfile",
        }
        for path, expected in cases.items():
            with self.subTest(path):
                self.assertEqual(manifests.kind(path), expected)

    def test_not_manifests(self):
        for path in ["package-lock.json", "composer.lock", "Cargo.lock", "go.sum", "Gemfile.lock",
                     "poetry.lock", "uv.lock", "notes.txt", "/p/docs/requirements.md", "constraints.txt",
                     "tsconfig.json", "/p/package.json/x", "setup.py", "setup.cfg", "/p/requirements/README.md",
                     "my-requirements.txt", "go.mod.bak", "cargo.toml", "gemfile", "/p/docs/notes.txt", "src/x.in",
                     "C:\\p\\docs\\notes.txt", "", "requirements.txt\n", "/p/requirements/base.txt\n",
                     "requirements.in\n"]:
            with self.subTest(path):
                self.assertIsNone(manifests.kind(path))


def _added(path, old, new):
    """Новые имена правки манифеста path рабочим путём хука (manifest_watch.edit_names) без версии в HEAD и
    без других манифестов проекта; old None — файла не было. Не манифест — []; не разобран — None."""
    kind = manifests.kind(path)
    if kind is None:
        return []
    try:
        return manifest_watch.edit_names(kind, old, new, lambda: None)
    except manifest_watch.Unavailable:
        return None


class PackageJsonTest(unittest.TestCase):
    OLD = '{"name": "a", "scripts": {"t": "x"}, "dependencies": {"react": "^18"}, "devDependencies": {"vite": "5"}}'

    def test_added_in_each_section(self):
        for section in ["dependencies", "devDependencies", "optionalDependencies", "peerDependencies"]:
            with self.subTest(section):
                new = '{"name": "a", "dependencies": {"react": "^18"}, "devDependencies": {"vite": "5"}, "%s": {"x": "1"}}'
                self.assertEqual(_added("package.json", self.OLD, new % section), ["x"])

    def test_version_change_removal_reorder_scripts(self):
        for new in [
            '{"name": "a", "dependencies": {"react": "^19"}, "devDependencies": {"vite": "6"}}',
            '{"name": "a", "dependencies": {}}',
            '{"devDependencies": {"vite": "5"}, "dependencies": {"react": "^18"}, "name": "b"}',
            '{"name": "a", "scripts": {"t": "npm i lodash"}, "dependencies": {"react": "^18"}, "devDependencies": {"vite": "5"}, "overrides": {"lodash": "4"}, "bundleDependencies": ["lodash"]}',
            # Перенос между разделами — не добавление.
            '{"name": "a", "dependencies": {"react": "^18", "vite": "5"}}',
        ]:
            with self.subTest(new):
                self.assertEqual(_added("package.json", self.OLD, new), [])

    def test_local_specs_not_packages(self):
        new = '{"dependencies": {"react": "^18", "a": "workspace:^", "b": "file:../b", "c": "link:../c", "d": "portal:../d", "e": "./e", "f": "/abs/f", "g": "~/g"}}'
        self.assertEqual(_added("package.json", self.OLD, new), [])

    def test_local_to_registry_is_added(self):
        old = '{"dependencies": {"a": "file:../a"}}'
        self.assertEqual(_added("package.json", old, '{"dependencies": {"a": "^1"}}'), ["a"])

    def test_alias_resolves_to_real_package(self):
        self.assertEqual(_added("package.json", self.OLD, '{"dependencies": {"react": "npm:evil@1"}}'), ["evil"])
        self.assertEqual(_added("package.json", self.OLD, '{"dependencies": {"r2": "npm:react@^18"}}'), [])
        self.assertEqual(_added("package.json", None, '{"dependencies": {"x": "npm:@s/p"}}'), ["@s/p"])

    def test_broken(self):
        self.assertIsNone(_added("package.json", self.OLD, '{"dependencies": {"x": '))
        self.assertIsNone(_added("package.json", self.OLD, "[1, 2]"))
        self.assertIsNone(manifests.names("package.json", "[" * 200000))
        # Старый текст не разобрать — что добавлено, неизвестно.
        self.assertIsNone(_added("package.json", "{oops", self.OLD))

    def test_odd_sections_ignored(self):
        self.assertEqual(manifests.names("package.json", '{"dependencies": ["x"], "devDependencies": null}'),
                         frozenset())
        self.assertEqual(manifests.names("package.json", '{"dependencies": {"x": 1}}'), frozenset({"x"}))

    def test_bom(self):
        self.assertEqual(manifests.names("package.json", '\ufeff{"dependencies": {"x": "1"}}'), frozenset({"x"}))

    def test_new_file(self):
        self.assertEqual(_added("/p/package.json", None, '{"dependencies": {"b": "1", "a": "1"}}'), ["a", "b"])


class ComposerTest(unittest.TestCase):
    OLD = '{"require": {"php": ">=8.1", "monolog/monolog": "^3"}, "require-dev": {"phpunit/phpunit": "^10"}}'

    def test_added(self):
        new = '{"require": {"php": ">=8.1", "monolog/monolog": "^3", "guzzlehttp/guzzle": "^7"}, "require-dev": {"phpunit/phpunit": "^10", "Mockery/Mockery": "1"}}'
        self.assertEqual(_added("composer.json", self.OLD, new), ["guzzlehttp/guzzle", "mockery/mockery"])

    def test_platform_packages_not_added(self):
        new = '{"require": {"php": ">=8.2", "php-64bit": "*", "hhvm": "*", "ext-json": "*", "lib-icu": "*", "composer-plugin-api": "^2", "composer-runtime-api": "^2", "composer": "^2", "monolog/monolog": "^3"}, "require-dev": {"phpunit/phpunit": "^10"}}'
        self.assertEqual(_added("composer.json", self.OLD, new), [])

    def test_version_change_removal_case_metadata(self):
        for new in [
            '{"require": {"php": ">=8.3", "monolog/monolog": "^4"}, "require-dev": {"phpunit/phpunit": "^11"}}',
            '{"require": {}}',
            '{"name": "x/y", "require": {"Monolog/Monolog": "^3"}, "scripts": {"t": "composer require evil/x"}, "require-dev": {"phpunit/phpunit": "^10"}}',
            '{"require": {"monolog/monolog": "^3"}, "suggest": {"evil/x": "why"}, "conflict": {"a/b": "1"}, "replace": {"c/d": "*"}, "provide": {"e/f": "1"}}',
        ]:
            with self.subTest(new):
                self.assertEqual(_added("composer.json", self.OLD, new), [])

    def test_broken(self):
        self.assertIsNone(_added("composer.json", self.OLD, '{"require": }'))


class PyprojectTest(unittest.TestCase):
    OLD = """\
[build-system]
requires = ["hatchling"]

[project]
name = "My_App"
version = "1"
dependencies = ["requests>=2", "Pydantic[email]~=2.0 ; python_version >= '3.11'"]

[project.optional-dependencies]
cli = ["click"]
self = ["my-app[cli]"]

[dependency-groups]
dev = ["pytest", {include-group = "lint"}]
lint = ["ruff"]

[tool.ruff]
line-length = 88
"""

    def test_added_each_section(self):
        cases = [
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "httpx",', "httpx"),
            ('cli = ["click"]', 'cli = ["click", "rich>=13"]', "rich"),
            ('lint = ["ruff"]', 'lint = ["ruff", "mypy"]', "mypy"),
            ('requires = ["hatchling"]', 'requires = ["hatchling", "hatch-vcs"]', "hatch-vcs"),
            ('requires = ["hatchling"]', 'requires = ["setuptools", "cython"]', "cython"),
            ("[tool.ruff]", '[tool.uv]\ndev-dependencies = ["coverage"]\n\n[tool.ruff]', "coverage"),
            ("[tool.ruff]", '[tool.pdm.dev-dependencies]\ntest = ["hypothesis"]\n\n[tool.ruff]', "hypothesis"),
            ('cli = ["click"]', 'cli = ["click"]\nnew = ["Zope.Interface"]', "zope-interface"),
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "pkg @ https://x/pkg.zip",', "pkg"),
        ]
        for old, new, name in cases:
            with self.subTest(name):
                self.assertIn(old, self.OLD)
                self.assertEqual(_added("pyproject.toml", self.OLD, self.OLD.replace(old, new, 1)), [name])

    def test_not_added(self):
        cases = [
            ('"requests>=2"', '"requests>=3"'),
            ('"requests>=2", ', ""),
            ('"requests>=2", "Pydantic[email]~=2.0 ; python_version >= \'3.11\'"',
             '"pydantic[email]~=2.0", "requests"'),
            ('"requests>=2"', '"Requests_>=2"'.replace("_", ".")),
            ("line-length = 88", 'line-length = 100\nextend-select = ["httpx"]'),
            ('version = "1"', 'version = "2"\ndescription = "uses httpx"'),
            ('dev = ["pytest", {include-group = "lint"}]', 'dev = ["pytest", {include-group = "other"}]'),
            ('self = ["my-app[cli]"]', 'self = ["my-app[cli]", "My.App[x]"]'),
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "loc @ file:///tmp/loc",'),
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "${VAR}", "",'),
        ]
        for old, new in cases:
            with self.subTest(new):
                self.assertIn(old, self.OLD)
                self.assertEqual(_added("pyproject.toml", self.OLD, self.OLD.replace(old, new, 1)), [])

    POETRY = """\
[tool.poetry]
name = "app"

[tool.poetry.dependencies]
python = "^3.11"
requests = "^2"
local = { path = "../local", develop = true }

[tool.poetry.dev-dependencies]
pytest = "^8"

[tool.poetry.group.lint.dependencies]
ruff = "*"
"""

    def test_own_extras_not_package(self):
        self.assertNotIn("my-app", manifests.names("pyproject", self.OLD))

    def test_poetry(self):
        self.assertEqual(manifests.names("pyproject", self.POETRY), frozenset({"requests", "pytest", "ruff"}))
        cases = [
            ('requests = "^2"', 'requests = "^2"\nFlask_Login = { version = "^0.6", extras = ["x"] }', ["flask-login"]),
            ('ruff = "*"', 'ruff = "*"\n\n[tool.poetry.group.docs.dependencies]\nmkdocs = "*"', ["mkdocs"]),
            ('pytest = "^8"', 'pytest = "^8"\nevil = { git = "https://x/evil.git" }', ["evil"]),
            ('requests = "^2"', 'requests = "^3"', []),
            ('python = "^3.11"', 'python = "^3.12"', []),
            ('python = "^3.11"', 'Python = "^3.11"', []),
            # Ссылка на свои extras по имени `tool.poetry.name`.
            ('ruff = "*"', 'ruff = "*"\n\n[dependency-groups]\ndev = ["App[test]"]', []),
            ('local = { path = "../local", develop = true }', 'local = { path = "../local2" }\nother = { path = "../o" }',
             []),
        ]
        for old, new, expected in cases:
            with self.subTest(new):
                self.assertEqual(_added("pyproject.toml", self.POETRY, self.POETRY.replace(old, new, 1)), expected)

    def test_uv_sources_local_not_package(self):
        cases = [
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "member",',
             '[tool.uv.sources]\nmember = { workspace = true }'),
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "Lib_A",',
             '[tool.uv.sources]\nlib-a = { path = "../lib-a", editable = true }'),
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "both",',
             '[tool.uv.sources]\nboth = [{ path = "../b", marker = "sys_platform == \'linux\'" }, { index = "x" }]'),
        ]
        for old, new, sources in cases:
            with self.subTest(sources):
                text = self.OLD.replace(old, new, 1).replace("[tool.ruff]", sources + "\n\n[tool.ruff]")
                self.assertEqual(_added("pyproject.toml", self.OLD, text), [])

    def test_standard_build_backends_not_packages(self):
        for spec in ("setuptools>=61", "wheel", "hatchling", "uv_build>=0.8,<0.9", "uv-build", "poetry-core>=1",
                     "flit_core >=3.2,<4", "pdm-backend", "scikit-build-core>=0.10", "maturin>=1,<2",
                     "meson-python"):
            with self.subTest(spec):
                text = f'[build-system]\nrequires = ["{spec}"]\n'
                self.assertEqual(manifests.names("pyproject", text), frozenset())
        # Бэкенд в зависимостях проекта и прочие пакеты в requires — по-прежнему имена.
        text = '[build-system]\nrequires = ["hatchling", "hatch-vcs", "cython"]\n[project]\ndependencies = ["wheel"]\n'
        self.assertEqual(manifests.names("pyproject", text), frozenset({"hatch-vcs", "cython", "wheel"}))

    def test_uv_sources_remote_is_package(self):
        text = self.OLD.replace('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "gitpkg",', 1)
        text = text.replace("[tool.ruff]", '[tool.uv.sources]\ngitpkg = { git = "https://x/gitpkg" }\n\n[tool.ruff]')
        self.assertEqual(_added("pyproject.toml", self.OLD, text), ["gitpkg"])

    def test_odd_shapes_ignored(self):
        text = """\
[project]
dependencies = "requests"
optional-dependencies = ["x"]
[dependency-groups]
dev = "pytest"
[tool]
poetry = 1
"""
        self.assertEqual(manifests.names("pyproject", text), frozenset())

    def test_broken(self):
        self.assertIsNone(_added("pyproject.toml", self.OLD, self.OLD + "\n[project\n"))
        self.assertIsNone(_added("pyproject.toml", self.OLD, self.OLD + '\n[project]\nname = "dup"\n'))
        self.assertIsNone(_added("pyproject.toml", "[x", self.OLD))


class RequirementsTest(unittest.TestCase):
    OLD = "# deps\nrequests==2.31\nDjango>=4 ; python_version > '3.8'\n-r base.txt\n"

    def test_added(self):
        cases = [
            ("flask", ["flask"]),
            ("Flask_Login[extra]>=1", ["flask-login"]),
            ("numpy ; sys_platform == 'linux'", ["numpy"]),
            ("pkg@https://x/pkg.zip", ["pkg"]),
            ("-e git+https://x/repo.git@v1#egg=Evil_Pkg&subdirectory=sub", ["evil-pkg"]),
            ("git+https://x/repo.git#subdirectory=sub&egg=evil4", ["evil4"]),
            ("--editable=git+https://x/repo.git#egg=evil2", ["evil2"]),
            ("git+https://x/repo.git#egg=evil3", ["evil3"]),
            ("https://x/files/Some_Pkg-1.0-py3-none-any.whl", ["some-pkg"]),
            ("a \\\n  >= 1", ["a"]),
            ("b==1 --hash=sha256:00 \\\n    --hash=sha256:11", ["b"]),
            ("c==1  # comment with requests-two", ["c"]),
            ("d\te", ["d"]),
        ]
        for line, expected in cases:
            with self.subTest(line):
                self.assertEqual(_added("requirements.txt", self.OLD, self.OLD + line + "\n"), expected)

    def test_not_added(self):
        for new in [
            "requests==2.32\ndjango>=5\n",
            "",
            "-r base.txt\n-r other.txt\n-c constraints.txt\n--index-url https://x\n-i https://x\n--pre\n",
            "requests==2.31\n# flask\n   # numpy\n\n",
            "requests\nDJANGO\n",
            "-e .\n-e ./sub\n./dist/x-1.0-py3-none-any.whl\n/abs/y.tar.gz\n../z\nfile:///tmp/q\n~/w\n",
            "https://x/archive/master.zip\n",
            "${PKG}\n",
            "requests\ndjango\n#egg=fake\n",
            "requests \\\n",
            # Продолжение строки опции: `constraints` — значение `-c`, не пакет.
            "requests\n-c \\\n  constraints\n",
            "requests==2.31#notcomment\ndjango\n",
            # Колесо по `file:` и архив по относительному пути — локальные.
            "requests\nfile:///tmp/w/Local_Pkg-1.0-py3-none-any.whl\nlibs/pkg-1.0.tar.gz\nlibs\\pkg2-1.0.tar.gz\n",
            # Продолжение строки с переводом CRLF.
            "requests\r\n-c \\\r\n  constraints\r\n",
        ]:
            with self.subTest(new):
                self.assertEqual(_added("requirements.txt", self.OLD, new), [])

    def test_never_unparseable(self):
        self.assertEqual(manifests.names("requirements", "\x00garbage ][\n"), frozenset())

    PIP_COMPILE = ("#\n# This file is autogenerated by pip-compile with Python 3.11\n# by the following command:\n#\n"
                   "#    pip-compile requirements.in\n#\nidna==3.7\n    # via requests\nrequests==2.32.3\n"
                   "    # via -r requirements.in\n")
    UV = ("# This file was autogenerated by uv via the following command:\n#    uv pip compile requirements.in "
          "-o requirements.txt\nidna==3.7\n    # via requests\nrequests==2.32.3\n    # via -r requirements.in\n")
    PDM = "# This file is @generated by PDM.\n# Please do not edit it manually.\n\nidna==3.7\nrequests==2.32.3\n"
    NO_HEADER = "idna==3.7\n    # via requests\nrequests==2.32.3  # via -r requirements.in\n"

    def test_generated_files_not_declarations(self):
        for text in [self.PIP_COMPILE, self.UV, self.PDM, self.NO_HEADER]:
            with self.subTest(text):
                self.assertEqual(manifests.names("requirements", text), frozenset())
                # Перегенерация с новым транзитивным пакетом — не добавление.
                self.assertEqual(_added("requirements.txt", text, text + "urllib3==2.2\n    # via requests\n"), [])
                self.assertEqual(_added("requirements.txt", None, text), [])

    def test_header_removed_names_known(self):
        # Снятый заголовок не делает добавлением пакеты, которые уже были в файле.
        plain = "idna==3.7\nrequests==2.32.3\n"
        self.assertEqual(_added("requirements.txt", self.PIP_COMPILE, plain), [])
        self.assertEqual(_added("requirements.txt", self.PIP_COMPILE, plain + "flask\n"), ["flask"])

    def test_header_only_at_top(self):
        # Строка заголовка ниже первого требования — обычный комментарий.
        text = "flask\n# This file is autogenerated by pip-compile\n"
        self.assertEqual(manifests.names("requirements", text), frozenset({"flask"}))

    def test_new_file(self):
        self.assertEqual(_added("/p/requirements/dev.txt", None, "pytest\nblack\n"), ["black", "pytest"])


class CargoTest(unittest.TestCase):
    OLD = """\
[package]
name = "app"
version = "0.1.0"

[dependencies]
serde = { version = "1", features = ["derive"] }
local = { path = "../local" }

[dev-dependencies]
tempfile = "3"
"""

    def test_added(self):
        cases = [
            ('tempfile = "3"', 'tempfile = "3"\nproptest = "1"', ["proptest"]),
            ("[dev-dependencies]", '[build-dependencies]\ncc = "1"\n\n[dev-dependencies]', ["cc"]),
            ("[dev-dependencies]", '[dev_dependencies]\nold-style = "1"\n\n[dev-dependencies]', ["old-style"]),
            ("[dev-dependencies]", '[target.\'cfg(unix)\'.dependencies]\nnix = "0.29"\n\n[dev-dependencies]', ["nix"]),
            ("[dev-dependencies]", '[target.wasm32-unknown-unknown.build-dependencies]\nwb = "1"\n\n[dev-dependencies]',
             ["wb"]),
            ('tempfile = "3"', 'tempfile = "3"\nx = { git = "https://x/x" }', ["x"]),
            ('serde = { version = "1", features = ["derive"] }',
             'serde = { version = "1", features = ["derive"] }\nsj = { version = "1", package = "serde_json" }',
             ["serde-json"]),
            ("[dependencies]", '[workspace.dependencies]\nanyhow = "1"\n\n[dependencies]', ["anyhow"]),
            ("[dependencies]", "[dependencies.tokio]\nversion = \"1\"\n\n[dependencies]", ["tokio"]),
        ]
        for old, new, expected in cases:
            with self.subTest(new):
                self.assertIn(old, self.OLD)
                self.assertEqual(_added("Cargo.toml", self.OLD, self.OLD.replace(old, new, 1)), expected)

    def test_not_added(self):
        cases = [
            ('tempfile = "3"', 'tempfile = "3.10"'),
            ('tempfile = "3"', ""),
            ('tempfile = "3"', 'Tempfile = "3"'),
            ('serde = { version = "1", features = ["derive"] }', 'serde_alias = { version = "1", package = "serde" }'),
            ('tempfile = "3"', 'tempfile = "3"\nlocal2 = { path = "../l2" }\nws = { workspace = true }\nws2.workspace = true'),
            ('version = "0.1.0"', 'version = "0.2.0"\ndescription = "serde_json"'),
            ("[dev-dependencies]", '[features]\nextra = ["dep:evil"]\n\n[dev-dependencies]'),
        ]
        for old, new in cases:
            with self.subTest(new):
                self.assertIn(old, self.OLD)
                self.assertEqual(_added("Cargo.toml", self.OLD, self.OLD.replace(old, new, 1)), [])

    def test_broken(self):
        self.assertIsNone(_added("Cargo.toml", self.OLD, self.OLD + "\n[dependencies]\n"))
        self.assertIsNone(_added("Cargo.toml", self.OLD, "serde = "))


class GoModTest(unittest.TestCase):
    OLD = """\
module example.com/app

go 1.22

require github.com/a/one v1.0.0

require (
\tgithub.com/b/two v1.2.0
\tgolang.org/x/text v0.14.0 // indirect
)

replace github.com/a/one => ../one
"""

    def test_added(self):
        cases = [
            ("require github.com/a/one v1.0.0", "require github.com/a/one v1.0.0\nrequire github.com/c/three v0.1.0",
             ["github.com/c/three"]),
            ("\tgithub.com/b/two v1.2.0", "\tgithub.com/b/two v1.2.0\n\tgithub.com/d/four v2.0.0+incompatible",
             ["github.com/d/four"]),
            ("\tgolang.org/x/text v0.14.0 // indirect", "\tgolang.org/x/text v0.14.0 // indirect\n\tgolang.org/x/net v0.1.0",
             ["golang.org/x/net"]),
            # Комментарий, лишь начинающийся с `indirect`, — не пометка.
            ("go 1.22", "go 1.22\nrequire github.com/g/h v1 // indirectly needed", ["github.com/g/h"]),
            ("go 1.22", 'go 1.22\n\nrequire "github.com/q/quoted" v1.0.0', ["github.com/q/quoted"]),
            ("go 1.22", "go 1.22\nrequire(\n\tgithub.com/f/nospace v1\n)", ["github.com/f/nospace"]),
            # Замена каталога модулем делает модуль внешним.
            ("replace github.com/a/one => ../one", "replace github.com/a/one => github.com/evil/one v1.0.0",
             ["github.com/a/one"]),
        ]
        for old, new, expected in cases:
            with self.subTest(new):
                self.assertIn(old, self.OLD)
                self.assertEqual(_added("go.mod", self.OLD, self.OLD.replace(old, new, 1)), expected)

    def test_not_added(self):
        cases = [
            ("v1.2.0", "v1.3.0"),
            ("\tgithub.com/b/two v1.2.0\n", ""),
            (" // indirect", ""),
            ("go 1.22", "go 1.23\ntoolchain go1.23.1"),
            ("go 1.22", "go 1.22\n// require github.com/z/commented v1\nexclude github.com/x/ex v1.0.0\nretract v0.1.0"),
            ("go 1.22", "go 1.22\ntool golang.org/x/tools/cmd/stringer"),
            ("go 1.22", "go 1.22\nexclude (\n\tgithub.com/x/ex v1.0.0\n)"),
            # Транзитивные дописывают `go mod tidy` и `go get -u`.
            ("\tgolang.org/x/text v0.14.0 // indirect",
             "\tgolang.org/x/text v0.14.0 // indirect\n\tgolang.org/x/net v0.1.0 // indirect\n"
             "\tgithub.com/y/z v1.0.0 //indirect; for tests"),
            ("go 1.22", "go 1.22\nrequire github.com/k/single v1.0.0 // indirect"),
            # Транзитивная, ставшая прямой (`go mod tidy` после импорта), уже была в графе модулей.
            (" // indirect", ""),
        ]
        for old, new in cases:
            with self.subTest(new):
                self.assertIn(old, self.OLD)
                self.assertEqual(_added("go.mod", self.OLD, self.OLD.replace(old, new, 1)), [])

    def test_local_replace_not_package(self):
        cases = [
            ("go 1.22", "go 1.22\nrequire example.com/local v0.0.0\nreplace example.com/local => ../local"),
            ("go 1.22", "go 1.22\nrequire example.com/local v1.0.0\nreplace example.com/local v1.0.0 => ./local"),
            ("go 1.22", "go 1.22\nrequire example.com/local v1\nreplace (\n\texample.com/local => /abs/local\n)"),
            ("go 1.22", "go 1.22\nrequire example.com/local v1\nreplace example.com/local => ..\\local"),
        ]
        for old, new in cases:
            with self.subTest(new):
                self.assertEqual(_added("go.mod", self.OLD, self.OLD.replace(old, new, 1)), [])

    def test_replace_to_module_is_package(self):
        new = self.OLD.replace("go 1.22", "go 1.22\nrequire example.com/x v1\nreplace example.com/x => example.com/y v1")
        self.assertEqual(_added("go.mod", self.OLD, new), ["example.com/x"])

    def test_indirect_not_in_names(self):
        # github.com/a/one заменён каталогом `../one` — местный.
        self.assertEqual(manifests.names("gomod", self.OLD), frozenset({"github.com/b/two"}))

    def test_unclosed_block_reads_to_end(self):
        self.assertEqual(manifests.names("gomod", "require (\n\tgithub.com/a/b v1\n"), frozenset({"github.com/a/b"}))


class GemfileTest(unittest.TestCase):
    OLD = """\
source "https://rubygems.org"
ruby "3.3.0"
gem "rails", "~> 7.1"
group :test do
  gem 'rspec'
end
"""

    def test_added(self):
        cases = [
            'gem "pg"',
            "gem 'pg', '~> 1.5', require: false",
            "  gem('pg')",
            'gem "pg" # db',
            'gem "pg", git: "https://github.com/ged/ruby-pg"',
        ]
        for line in cases:
            with self.subTest(line):
                self.assertEqual(_added("Gemfile", self.OLD, self.OLD + line + "\n"), ["pg"])

    def test_not_added(self):
        for new in [
            self.OLD.replace("~> 7.1", "~> 7.2"),
            self.OLD.replace("gem 'rspec'\n", ""),
            self.OLD + "# gem 'evil'\n",
            self.OLD + 'gem "#{name}-ext"\n',
            self.OLD + "gem name\n",
            self.OLD + "gemspec\n",
            self.OLD + "source 'https://gems.example.com' do\nend\n",
            self.OLD.replace('ruby "3.3.0"', 'ruby "3.3.1"'),
            self.OLD + "gems = %w[a b]\n",
            # Местные гемы: `path:`, `:path =>` и блок `path … do`.
            self.OLD + "gem 'local', path: '../local'\n",
            self.OLD + "gem \"local\", :path => \"vendor/local\", require: false\n",
            self.OLD + "path '../engines' do\n  gem 'a'\n  group :x do\n    gem 'b'\n  end\n  gem 'c'\nend\n",
        ]:
            with self.subTest(new):
                self.assertEqual(_added("Gemfile", self.OLD, new), [])

    def test_after_path_block_is_package(self):
        new = self.OLD + "path '../engines' do\n  gem 'a'\nend\ngem 'pg'\n"
        self.assertEqual(_added("Gemfile", self.OLD, new), ["pg"])

    def test_keyword_blocks_inside_path_block(self):
        # `end` условия и цикла внутри блока `path … do` закрывает их, не сам блок.
        cases = [
            'path "engines" do\n  if ENV["X"]\n    gem "a"\n  end\n  gem "local_b"\nend\ngem "rails"\n',
            'path "engines" do\n  unless ENV["X"]\n    gem "a"\n  else\n    gem "c"\n  end\n  gem "local_b"\nend\n'
            'gem "rails"\n',
            'path "engines" do\n  case RUBY_ENGINE\n  when "jruby"\n    gem "a"\n  end\n  begin\n    gem "c"\n  end\n'
            '  gem "local_b"\nend\ngem "rails"\n',
            'path "engines" do\n  while false\n  end\n  until true\n  end\n  for x in [] do\n  end\n'
            '  gem "local_b"\nend\ngem "rails"\n',
            # Условие-модификатор и условие в одну строку блок не открывают.
            'path "engines" do\n  gem "a" if ENV["X"]\n  if ENV["Y"] then gem "c" end\nend\ngem "rails"\n',
        ]
        for text in cases:
            with self.subTest(text):
                self.assertEqual(manifests.names("gemfile", text), frozenset({"rails"}))

    def test_never_unparseable(self):
        self.assertEqual(manifests.names("gemfile", "gem (\n"), frozenset())


class KnownNamesTest(unittest.TestCase):
    """Старая сторона сравнения (manifests.known_names): объявленные вместе с транзитивными."""

    def test_known_includes_transitive(self):
        self.assertEqual(manifests.known_names("gomod", GoModTest.OLD),
                         frozenset({"github.com/b/two", "golang.org/x/text"}))
        self.assertEqual(manifests.known_names("requirements", RequirementsTest.PIP_COMPILE),
                         frozenset({"idna", "requests"}))
        self.assertEqual(manifests.known_names("package.json", PackageJsonTest.OLD), frozenset({"react", "vite"}))
        self.assertIsNone(manifests.known_names("package.json", "{oops"))
        self.assertIsNone(manifests.known_names("unknown", "x"))

    def test_known_excludes_local(self):
        text = "require example.com/l v1 // indirect\nreplace example.com/l => ../l\n"
        self.assertEqual(manifests.known_names("gomod", text), frozenset())


class RegistryTest(unittest.TestCase):
    def test_registry_of_each_kind(self):
        self.assertEqual(manifests.registry("pyproject"), manifests.registry("requirements"))
        others = [manifests.registry(k) for k in ("package.json", "composer.json", "cargo", "gomod", "gemfile",
                                                  "pyproject")]
        self.assertEqual(len(set(others)), len(others))
        self.assertIsNone(manifests.registry("unknown"))


class OwnNameTest(unittest.TestCase):
    def test_own_names(self):
        cases = [
            ("package.json", '{"name": "@acme/utils", "dependencies": {}}', "@acme/utils"),
            ("composer.json", '{"name": "Acme/Utils"}', "acme/utils"),
            ("pyproject", '[project]\nname = "Acme_Utils"\n', "acme-utils"),
            ("pyproject", '[tool.poetry]\nname = "acme.poetry"\n', "acme-poetry"),
            ("cargo", '[package]\nname = "acme_utils"\n', "acme-utils"),
            ("gomod", "// c\nmodule \"example.com/acme/utils\" // m\n\ngo 1.22\n", "example.com/acme/utils"),
        ]
        for kind, text, expected in cases:
            with self.subTest(kind=kind, text=text):
                self.assertEqual(manifests.own_name(kind, text), expected)

    def test_no_own_name(self):
        for kind, text in [("package.json", "{}"), ("package.json", '{"name": 5}'), ("package.json", "{oops"),
                           ("pyproject", "[x"), ("cargo", "[workspace]\n"), ("gomod", "go 1.22\n"),
                           ("requirements", "flask\n"), ("gemfile", "gem 'x'\n"), ("unknown", "x")]:
            with self.subTest(kind=kind, text=text):
                self.assertIsNone(manifests.own_name(kind, text))


class AddedContractTest(unittest.TestCase):
    def test_sorted(self):
        self.assertEqual(_added("Gemfile", "", 'gem "b"\ngem "a"\ngem "c"\n'), ["a", "b", "c"])

    def test_unknown_kind_names(self):
        self.assertIsNone(manifests.names("unknown", "x"))

    # Входы разбора по числу записей n: около 1 МБ при n = SIZE.
    BIG = {
        "package.json": lambda n: ("{\"dependencies\": {" + ", ".join(f'"p{i}": "^1.0.{i}"' for i in range(n))
                                   + "}}"),
        "pyproject.toml": lambda n: ("[project]\ndependencies = [\n" + "".join(f'  "p{i}>=1.{i}",\n' for i in range(n))
                                     + "]\n"),
        "requirements.txt": lambda n: "".join(f"p{i}==1.{i} --hash=sha256:{'0' * 40} \\\n  # c\n" for i in range(n)),
        "Cargo.toml": lambda n: "[dependencies]\n" + "".join(f'p{i} = {{ version = "1.{i}" }}\n' for i in range(n)),
        "go.mod": lambda n: ("module m\nrequire (\n" + "".join(f"\tgithub.com/o/p{i} v1.0.{i}\n" for i in range(n))
                             + ")\n"),
        "Gemfile": lambda n: "".join(f"gem 'p{i}', '~> 1.{i}'\n" for i in range(n)),
        "composer.json": lambda n: "{\"require\": {" + ", ".join(f'"v/p{i}": "^1.{i}"' for i in range(n)) + "}}",
    }
    SIZE = {"package.json": 40000, "pyproject.toml": 50000, "requirements.txt": 15000, "Cargo.toml": 40000,
            "go.mod": 30000, "Gemfile": 50000, "composer.json": 40000}
    # Строка без переводов строки и патологический ввод регулярных выражений; n = 1 — около 1 МБ.
    PATHOLOGICAL = {
        "requirements.txt": lambda n: "a" * (1_000_000 // n) + "[" + " " * (100_000 // n),
        "Gemfile": lambda n: "gem " + "'" * (500_000 // n) + " " * (500_000 // n),
        "go.mod": lambda n: "require (" + " " * (1_000_000 // n),
    }

    def test_large_file_linear(self):
        for name, make in self.BIG.items():
            with self.subTest(name):
                small, large = make(self.SIZE[name] // 4), make(self.SIZE[name])
                self.assertGreater(len(large), 900_000)
                self.assertEqual(_added(name, large, large + "\n"), [])
                assert_linear(self, lambda: _added(name, small, small + "\n"),
                              lambda: _added(name, large, large + "\n"))
        for name, make in self.PATHOLOGICAL.items():
            with self.subTest(name + " pathological"):
                small, large = make(4), make(1)
                assert_linear(self, lambda: _added(name, None, small), lambda: _added(name, None, large))


class LegacySourcesTest(unittest.TestCase):
    """Имена файлов зависимостей PyPI без проверки (manifest_watch._known на видах _LEGACY)."""

    def test_corpus(self):
        for rel, expected in LEGACY_CORPUS.items():
            name = rel.rsplit("/", 1)[1]
            with self.subTest(rel):
                self.assertEqual(manifest_watch.source_kind(rel), name)
                self.assertIsNone(manifest_watch.watched_kind(rel))
                text = (FIXTURES / rel).read_text(encoding="utf-8")
                self.assertEqual(manifest_watch._known(name, text), frozenset(expected))

    def test_edges(self):
        cases = [
            # setup.py: требования, собранные кодом, не видны; присваивание и `+` — видны; словарь аргументов.
            ("setup.py", "from setuptools import setup\nsetup(install_requires=open('r.txt').read().splitlines())\n",
             set()),
            ("setup.py", "base = ['a']\nsetup(install_requires=base + ['b'], setup_requires=('c',))\n",
             {"a", "b", "c"}),
            ("setup.py", "kw = {'install_requires': ['a'], 'name': 'x'}\nsetup(**kw)\n", {"a"}),
            # Цикл присваиваний и глубокая цепочка имён: каждое имя разрешается один раз, глубина не ограничена.
            ("setup.py", "a = [b, 'x']\nb = [a, 'y']\nsetup(install_requires=a)\n", {"x", "y"}),
            ("setup.py", "a0 = ['deep']\n" + "".join(f"a{i} = [a{i - 1}]\n" for i in range(1, 30))
             + "setup(install_requires=a29)\n", {"deep"}),
            ("setup.py", "setup(\n", None),
            ("setup.cfg", "[options]\ninstall_requires = file: requirements.in\n", set()),
            ("setup.cfg", "[options\n", None),
            ("setup.cfg", "[metadata]\nname = x\n", set()),
            ("Pipfile", "[packages\n", None),
            ("Pipfile", '[scripts]\nstart = "python -m app"\n[pipenv]\nallow_prereleases = true\n', set()),
        ]
        for kind, text, expected in cases:
            with self.subTest(kind=kind, text=text):
                self.assertEqual(manifest_watch._known(kind, text), expected)

    def test_not_legacy(self):
        for path in ("setup.py.bak", "tests/fixtures/x/setup.py", "node_modules/x/Pipfile", "src/setup_utils.py"):
            with self.subTest(path):
                self.assertIsNone(manifest_watch.source_kind(path))

    @staticmethod
    def _fanout(refs, levels=6):
        """setup.py, где каждое из levels имён ссылается refs раз на предыдущее: дерево подстановок — refs**levels
        узлов, текст — около levels * refs ссылок."""
        lines = ["a0 = ['x0']"]
        for i in range(1, levels + 1):
            lines.append(f"a{i} = [{', '.join([f'a{i - 1}'] * refs)}, 'x{i}']")
        return "\n".join(lines) + f"\nsetup(install_requires=a{levels})\n"

    def test_setup_py_references_linear(self):
        small, large = self._fanout(4), self._fanout(16)
        self.assertEqual(manifest_watch._known("setup.py", large), frozenset(f"x{i}" for i in range(7)))
        assert_linear(self, lambda: manifest_watch._known("setup.py", small),
                      lambda: manifest_watch._known("setup.py", large))
