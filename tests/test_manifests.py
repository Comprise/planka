import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.helpers import assert_linear

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
import manifest_watch  # noqa: E402
import manifests  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "manifests"
# Подключение файла: равно только подключению, не строке `include <путь>`.
INC = manifests.Include

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
    # Пакет не из реестра — имя с источником: `my-fork @ github:user/repo`.
    "npm-local/package.json": {"lodash", "my-fork @ github:user/repo", "react", "@types/node", "@scope/tool",
                               "react-dom", "fsevents"},
    "composer-docs/composer.json": {"monolog/monolog", "symfony/console", "phpunit/phpunit"},
    "pyproject-uv-bff/pyproject.toml": {"fastapi", "httpx", "pydantic", "pydantic-settings",
                                        "prometheus-client", "pytest", "strawberry-graphql", "uvicorn",
                                        "ruff", "pytest-asyncio"},
    "pyproject-poetry-fetchartifact/pyproject.toml": {"aiohttp", "mypy", "pylint", "black", "pytest",
                                                      "pytest-asyncio", "isort", "pytest-aiohttp",
                                                      "pytest-cov"},
    # poetry-core и hatchling в `build-system.requires` — стандартные бэкенды сборки, не зависимости.
    "pyproject-poetry-tcat/pyproject.toml": {"bleak", "pytest", "cryptography", "pyreadline3"},
    # Файлы uv export и pip-compile проверяются, как любой файл требований: заголовок генератора и `# via` не
    # снимают проверку.
    "requirements-uv-export/requirements.txt": set("""
        annotated-types asgiref certifi cffi charset-normalizer colorama cryptography deprecation django
        django-filter django-ipware django-polymorphic django-simple-history django-structlog django-waffle
        djangorestframework execnet factory-boy faker idna iniconfig jwcrypto packaging pika pluggy
        prometheus-client psycopg2-binary pycparser pydantic pydantic-core pygments pytest pytest-django
        pytest-xdist python-ipware python-keycloak requests requests-toolbelt sqlparse structlog
        typing-extensions typing-inspection tzdata urllib3""".split()),
    "requirements-pip-compile/requirements.txt": set("""
        alabaster babel breathe certifi charset-normalizer click docutils idna imagesize importlib-metadata
        jinja2 markdown-it-py markupsafe mdurl packaging pygments pyyaml readthedocs-cli requests rich
        snowballstemmer sphinx sphinx-rtd-theme sphinxcontrib-applehelp sphinxcontrib-devhelp
        sphinxcontrib-htmlhelp sphinxcontrib-jquery sphinxcontrib-jsmath sphinxcontrib-qthelp
        sphinxcontrib-serializinghtml tomli urllib3 zipp""".split()),
    "requirements-esp-idf/requirements/core.txt": set("""
        click construct cryptography esp-coredump esp-idf-diag esp-idf-kconfig esp-idf-monitor
        esp-idf-nvs-partition-gen esp-idf-panic-decoder esp-idf-size esptool freertos-gdb
        idf-component-manager packaging psutil pyclang pyelftools pyparsing pyserial rich rich-click
        setuptools tree-sitter tree-sitter-c""".split()),
    "requirements-pip-docs/requirements.txt": {"pytest", "pytest-cov", "beautifulsoup4", "docopt", "keyring",
                                               "coverage", "mopidy-dirble", "requests",
                                               "index https://example.com/simple",
                                               # Источник архива — каталог URL, версия в имени файла.
                                               "wxpython-phoenix @ http://wxpython.org/Phoenix/snapshot-builds/",
                                               "myproject @ git+https://git.example.com/MyProject",
                                               "urllib3 @ https://github.com/urllib3/urllib3/archive/refs/tags/",
                                               "fooproject", "rejected", "green",
                                               # `-r` и `-c` подключают файлы, которые pip ставит, а проверка не
                                               # видит (`-c` — тоже: его `-r` ставит пакеты, опции меняют индекс).
                                               INC("other-requirements.txt"), INC("constraints.txt")},
    "cargo-libgit-rs/Cargo.toml": {"autocfg"},
    "cargo-libgit-sys/Cargo.toml": {"libz-sys", "autocfg", "make-cmd"},
    # Источник не из crates.io — имя с источником (`git`, `registry`).
    "cargo-book/Cargo.toml": {"rand", "time", "regex @ git+https://github.com/rust-lang/regex.git",
                              "some-crate @ registry:my-registry", "foo",
                              "foo @ git+https://github.com/example/project.git", "foo-core @ registry:custom", "serde",
                              "libz-sys", "winhttp", "openssl", "mio", "tempdir", "cc"},
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
    "pyproject-uv-workspace/pyproject.toml": {"tqdm @ git+https://github.com/tqdm/tqdm"},
    "gemfile-path/Gemfile": {"rails", "puma", "debug", "rspec-rails @ git+https://github.com/rspec/rspec-rails"},
    "gomod-replace-local/go.mod": {"github.com/go-chi/chi/v5", "google.golang.org/grpc"},
    # Зависимости `[project.optional-dependencies]` — имена; meson-python и wheel — бэкенды сборки.
    "pyproject-gyp-next/pyproject.toml": {"packaging", "setuptools", "pytest", "ruff"},
    "pyproject-pandas/pyproject.toml": set("""
        meson cython numpy versioneer python-dateutil tzdata hypothesis pytest pytest-xdist pyarrow bottleneck
        numba numexpr scipy xarray fsspec s3fs gcsfs odfpy openpyxl python-calamine pyxlsb xlrd xlsxwriter
        pyiceberg tables pyreadstat sqlalchemy psycopg2 adbc-driver-postgresql pymysql adbc-driver-sqlite
        beautifulsoup4 html5lib lxml matplotlib jinja2 tabulate pyqt5 qtpy zstandard pytz fastparquet""".split()),
    # Директива `tool` называет команду модуля из `require`; сама не объявляет зависимость.
    "gomod-tool/go.mod": {"example.com/fork/net", "golang.org/x/net", "golang.org/x/tools", "golang.org/x/text",
                          "github.com/golang/mock", "honnef.co/go/tools"},
    # Шаблон cookiecutter: строки Jinja `{%- … %}` — не требования; `-r` — подключение.
    "requirements-cookiecutter-django/requirements/local.txt": {
        INC("base.txt"), "werkzeug", "ipdb", "psycopg", "watchfiles", "mypy", "django-stubs", "pytest",
        "pytest-sugar", "djangorestframework-stubs", "sphinx", "sphinx-autobuild", "flake8", "flake8-isort",
        "coverage", "black", "djlint", "pylint-django", "pylint-celery", "pre-commit", "factory-boy",
        "django-debug-toolbar", "django-extensions", "django-coverage-plugin", "pytest-django"},
    # Замена каталогом с версией касается только этой версии: x/text v0.13.0 при require v0.14.0 — из сети;
    # `..` — каталог.
    "gomod-replace-versioned/go.mod": {"golang.org/x/text", "golang.org/x/sync", "example.com/fork/sync"},
    # git_source Bundler (github, gist, bitbucket, gitlab), ключ опции строкой, plugin — гем, eval_gemfile —
    # подключение; `"path" =>` — местный.
    "gemfile-bundler-dsl/Gemfile": {"bundler-graph", "rails", "rack @ github:rack/rack",
                                    "nokogiri @ git+https://github.com/sparklemotion/nokogiri.git",
                                    "redis @ git+https://github.com/redis/redis-rb.git", "the_gist @ gist:4815162342",
                                    "bb_gem @ bitbucket:mybitbucketuser/bb_gem", "gl_gem @ gitlab:mygroup/gl_gem",
                                    INC("Gemfile.local")},
    # Замена и ограничение uv пакета не добавляют; с URL — меняют его источник.
    "pyproject-uv-overrides/pyproject.toml": {"werkzeug", "pydantic", "pydantic-core @ https://example.com/wheels/",
                                              "anyio @ git+https://github.com/agronholm/anyio"},
    "pyproject-pdm-overrides/pyproject.toml": {"django", "requests", "pytz @ https://mypypi.org/packages/"},
    # Список ограничений poetry: источник каждого элемента, `path` — местный.
    "pyproject-poetry-multiple/pyproject.toml": {"foo", "example @ https://example.com/", "example @ index:pypi",
                                                 "local"},
    # packageExtensions pnpm дописывает зависимости в чужие пакеты — они ставятся.
    "npm-pnpm-extensions/package.json": {"react-redux", "react", "react-dom", "cookie-parser", "fsevents"},
    # Пакет в репозитории `package` — источник этого пакета (dist и source).
    "composer-package-repo/composer.json": {"smarty/smarty", "smarty/smarty @ https://www.smarty.net/files/",
                                            "smarty/smarty @ http://smarty-php.googlecode.com/svn/"},
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
    "gomod-replace-versioned/go.mod": "example.com/root/tools",
    "pyproject-uv-overrides/pyproject.toml": "project",
    "pyproject-pdm-overrides/pyproject.toml": "pdm-app",
    "pyproject-poetry-multiple/pyproject.toml": "poetry-multi",
    "npm-pnpm-extensions/package.json": "pnpm-app",
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
        # Фикстура — файл, который git не игнорирует: кэши инструментов (.ruff_cache) в каталоге корпуса — не образцы.
        listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "."],
                                cwd=FIXTURES, capture_output=True, check=True).stdout
        files = {name for name in os.fsdecode(listed).split("\0") if name}
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
        # Добавление одной зависимости в настоящий манифест видно ровно одним именем.
        cases = {
            "npm-cc-harness/package.json": ('"zustand": "^4.5.5"', '"zustand": "^4.5.5",\n    "left-pad": "1.3.0"',
                                            ["left-pad"]),
            "pyproject-uv-bff/pyproject.toml": ('"uvicorn>=0.41.0",', '"uvicorn>=0.41.0",\n    "Left_Pad>=1",',
                                                ["left-pad"]),
            "pyproject-poetry-fetchartifact/pyproject.toml": ('aiohttp = "^3.8.4"', 'aiohttp = "^3.8.4"\nrich = "*"',
                                                              ["rich"]),
            "requirements-esp-idf/requirements/core.txt": ("\nrich\n", "\nrich\nleft_pad\n", ["left-pad"]),
            "requirements-pip-compile/requirements.txt": ("sphinx==", "left-pad==1.0\nsphinx==", ["left-pad"]),
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

    def test_folded(self):
        # Без учёта регистра (файловая система без учёта регистра) — тот же вид, но только базовое имя из
        # постоянных имён видов: имена requirements и каталоги — как написаны, проза в `Requirements/` — не манифест.
        cases = {"/p/PACKAGE.JSON": "package.json", "cargo.toml": "cargo", "gemfile": "gemfile",
                 "/p/REQUIREMENTS/Gems.RB": "gemfile", "GO.MOD": "gomod", "notes.txt": None,
                 "Requirements-Dev.TXT": None, "/p/Requirements/base.txt": None, "ci/Requirements/notes.txt": None,
                 "requirements-dev.txt": "requirements", "/p/requirements/Base.TXT": None}
        for path, expected in cases.items():
            with self.subTest(path):
                self.assertEqual(manifests.kind(path, fold=True), expected)
                if expected is not None and path != path.lower():
                    self.assertIsNone(manifests.kind(path))

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
        # Старый текст не разобрать и версии в git нет — сравнение с пустым: битый манифест и затем зависимость не
        # проходят в два шага.
        self.assertEqual(_added("package.json", "{oops", self.OLD), ["react", "vite"])

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
            ('dependencies = ["requests>=2",', 'dependencies = ["requests>=2", "pkg @ https://x/pkg.zip",',
             "pkg @ https://x/"),
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
            ('pytest = "^8"', 'pytest = "^8"\nevil = { git = "https://x/evil.git" }',
             ["evil @ git+https://x/evil.git"]),
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
        self.assertEqual(_added("pyproject.toml", self.OLD, text), ["gitpkg @ git+https://x/gitpkg"])

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
        self.assertEqual(_added("pyproject.toml", "[x", self.OLD), ["click", "pydantic", "pytest", "requests", "ruff"])


class RequirementsTest(unittest.TestCase):
    OLD = "# deps\nrequests==2.31\nDjango>=4 ; python_version > '3.8'\n-r base.txt\n"

    def test_added(self):
        cases = [
            ("flask", ["flask"]),
            ("Flask_Login[extra]>=1", ["flask-login"]),
            ("numpy ; sys_platform == 'linux'", ["numpy"]),
            ("pkg@https://x/pkg.zip", ["pkg @ https://x/"]),
            ("-e git+https://x/repo.git@v1#egg=Evil_Pkg&subdirectory=sub", ["evil-pkg @ git+https://x/repo.git"]),
            ("git+https://x/repo.git#subdirectory=sub&egg=evil4", ["evil4 @ git+https://x/repo.git"]),
            ("--editable=git+https://x/repo.git#egg=evil2", ["evil2 @ git+https://x/repo.git"]),
            ("git+https://x/repo.git#egg=evil3", ["evil3 @ git+https://x/repo.git"]),
            ("https://x/files/Some_Pkg-1.0-py3-none-any.whl",
             ["some-pkg @ https://x/files/"]),
            ("a \\\n  >= 1", ["a"]),
            # Подключение файла, которого проверка не видит (`-c` тоже ставит пакеты своих `-r`), и строка с
            # переменной окружения: значение подставит pip.
            ("-r other.txt", [INC("other.txt")]),
            ("-c constraints.txt", [INC("constraints.txt")]),
            ("-c \\\n  constraints", [INC("constraints")]),
            ("${PKG}", ["${PKG}"]),
            ("--extra-index-url https://${PRIVATE_INDEX_TOKEN}@pypi.example.com/simple",
             ["--extra-index-url https://${PRIVATE_INDEX_TOKEN}@pypi.example.com/simple"]),
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
            "-r base.txt\n--pre\n--require-hashes\n",
            "requests==2.31\n# flask\n   # numpy\n\n",
            "requests\nDJANGO\n",
            "-e .\n-e ./sub\n./dist/x-1.0-py3-none-any.whl\n/abs/y.tar.gz\n../z\nfile:///tmp/q\n~/w\n",
            "https://x/archive/master.zip\n",
            "requests\ndjango\n#egg=fake\n",
            "requests \\\n",
            "requests==2.31#notcomment\ndjango\n",
            # Колесо по `file:` и архив по относительному пути — локальные.
            "requests\nfile:///tmp/w/Local_Pkg-1.0-py3-none-any.whl\nlibs/pkg-1.0.tar.gz\nlibs\\pkg2-1.0.tar.gz\n",
            # Продолжение строки с переводом CRLF: `--pre` — опция, `django` — значение-позиционный аргумент, pip его
            # не ставит.
            "requests\r\n--pre \\\r\n  django\r\n",
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

    def test_generated_header_checked_like_any(self):
        # Заголовок генератора и `# via` агент впишет сам вместе с пакетом: файл проверяется по содержимому.
        for text in [self.PIP_COMPILE, self.UV, self.PDM, self.NO_HEADER]:
            with self.subTest(text):
                self.assertEqual(manifests.names("requirements", text), frozenset({"idna", "requests"}))
                self.assertEqual(_added("requirements.txt", text, text + "urllib3==2.2\n    # via requests\n"),
                                 ["urllib3"])
                self.assertEqual(_added("requirements.txt", None, text), ["idna", "requests"])
        plain = "idna==3.7\nrequests==2.32.3\n"
        self.assertEqual(_added("requirements.txt", plain, self.PIP_COMPILE), [])
        self.assertEqual(_added("requirements.txt", plain, "# This file is @generated by PDM.\n" + plain + "flask\n"),
                         ["flask"])

    def test_header_removed_names_known(self):
        plain = "idna==3.7\nrequests==2.32.3\n"
        self.assertEqual(_added("requirements.txt", self.PIP_COMPILE, plain), [])
        self.assertEqual(_added("requirements.txt", self.PIP_COMPILE, plain + "flask\n"), ["flask"])

    def test_new_file(self):
        self.assertEqual(_added("/p/requirements/dev.txt", None, "pytest\nblack\n"), ["black", "pytest"])

    def test_lines_as_pip_reads_them(self):
        # Ожидания сверены с pip 26.2 (req_file: join_lines, ignore_comments, break_args_options, optparse).
        cases = {
            # `\` склеивает строки без пробела; строка-комментарий не склеивается со следующей.
            "django\\\n-evil==1\n": ["django-evil"],
            "# note \\\nevil\n": ["evil"],
            "django  # see \\\nevil\n": ["django"],
            "git+https://x/\\\nevil.git#egg=evil\n": ["evil @ git+https://x/evil.git"],
            # Сокращения длинных опций, склейка короткой со значением, несколько опций в строке.
            "--edit git+https://x/e.git#egg=evil\n": ["evil @ git+https://x/e.git"],
            "--extra https://evil/simple\n": ["index https://evil/simple"],
            "-egit+https://x/e.git#egg=evil2\n": ["evil2 @ git+https://x/e.git"],
            "--pre -e git+https://x/e.git#egg=evil3\n": ["evil3 @ git+https://x/e.git"],
            "--pre --extra-index-url https://evil/simple\n": ["index https://evil/simple"],
            "--trusted-host evil -i https://evil/simple\n": ["index https://evil/simple"],
            "--requirement=extra.list\n": [INC("extra.list")],
            # pip берёт первый `-r`, без него — первый `-c`, и первый `-f`.
            "-r a.txt -r b.txt\n": [INC("a.txt")],
            "-c a.txt -r b.txt\n": [INC("b.txt")],
            "-f https://evil/links -f https://other/links\n": ["index https://evil/links"],
            # Индекс в строке требования и при `--no-index` pip не берёт; позиционный аргумент опций не ставит.
            "django --index-url https://evil/simple\n": ["django"],
            "--no-index --extra-index-url https://evil/simple\n": [],
            "--pre evil\n": [],
            # Строку, которую pip не разберёт, он отвергает с файлом целиком.
            "--req x.txt\n": [],
            "django --foo\n": [],
            "--extra-index-url \"unbalanced\n": [],
            # Слова опций — как у shlex.split: кавычки, `\` вне кавычек и в "…" перед `"` и `\`; `--` кончает опции.
            "-r 'a b.txt'\n": [INC("a b.txt")],
            "-r a\\ b.txt\n": [INC("a b.txt")],
            "-r \"a\\\"b\\x.txt\"\n": [INC('a"b\\x.txt')],
            "-r a'b'\"c\"\n": [INC("abc")],
            "-r ''\n": [INC("")],
            "-r 'unbalanced\n": [],
            "-r x\\ \n": [],
            "-- -r a.txt\n": [],
            "-ra.txt\n": [INC("a.txt")],
            "--pre=1 -r a.txt\n": [],
            "--no-index --index-url=https://evil/simple\n": [],
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(_added("requirements.txt", "", text), expected)

    def test_env_variable_ref_same_source(self):
        # Другая ссылка или фрагмент того же источника с переменной окружения — не новое имя; другой источник —
        # новое; `${…}` в ссылке — строка как есть.
        old = "git+https://${GITHUB_TOKEN}@github.com/org/repo.git@v1.0#egg=pkg\n"
        self.assertEqual(_added("requirements.txt", old, old.replace("v1.0", "v1.1")), [])
        self.assertEqual(_added("requirements.txt", "-e " + old, "-e " + old.replace("v1.0", "v1.1")), [])
        self.assertEqual(_added("requirements.txt", old, old.replace("org/repo", "evil/repo")),
                         ["git+https://${GITHUB_TOKEN}@github.com/evil/repo.git"])
        ref = "git+https://github.com/org/repo.git@${REF}#egg=pkg\n"
        self.assertEqual(_added("requirements.txt", old, ref), [ref.strip()])
        self.assertEqual(_added("requirements.txt", ref, ref.replace("REF", "OTHER")),
                         [ref.strip().replace("REF", "OTHER")])

    def test_env_variable_archive_keeps_package(self):
        # У архива с `${…}` в URL каталог — источник, а пакет — из имени файла: колесо — имя пакета, другой архив —
        # файл целиком. Другая версия того же колеса в том же каталоге — не новое имя, другой пакет — новое.
        wheels = "https://${PYPI_TOKEN}@pypi.corp/wheels/"
        old = wheels + "internal_lib-1.0-py3-none-any.whl\n"
        self.assertEqual(_added("requirements.txt", old, old + wheels + "evil_pkg-6.6-py3-none-any.whl\n"),
                         ["evil-pkg @ " + wheels])
        self.assertEqual(_added("requirements.txt", old, old.replace("1.0", "1.1")), [])
        sdist = wheels + "internal_lib-1.0.tar.gz#sha256=ab\n"
        self.assertEqual(_added("requirements.txt", sdist, sdist + wheels + "evil-6.6.tar.gz\n"),
                         [wheels + "evil-6.6.tar.gz"])
        self.assertEqual(_added("requirements.txt", sdist, wheels + "internal_lib-1.0.tar.gz#sha256=cd\n"), [])
        named = wheels + "${NAME}-1.0-py3-none-any.whl"
        self.assertEqual(_added("requirements.txt", old, old + named + "\n"), [named])

    def test_linear_on_options(self):
        # Слова опций и их разбор — один проход: 1 МиБ одиночных `-` и одно длинное значение `-i`.
        for small, large in [(" -" * 131_072, " -" * 524_288), ("-i " + "x" * 262_144, "-i " + "x" * 1_048_576),
                             ("-r '" + "x" * 262_144 + "'", "-r '" + "x" * 1_048_576 + "'"),
                             ("-r " + "\\x" * 65_536, "-r " + "\\x" * 262_144)]:
            with self.subTest(small[:8]):
                assert_linear(self, lambda: manifests.names("requirements", small),
                              lambda: manifests.names("requirements", large))

    def test_linear_on_spaces(self):
        # Комментарий ищется без отката по пробелам.
        small, large = "a" * 250_000 + "[" + " " * 25_000, "a" * 1_000_000 + "[" + " " * 100_000
        assert_linear(self, lambda: manifests.names("requirements", small),
                      lambda: manifests.names("requirements", large))


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
            ('tempfile = "3"', 'tempfile = "3"\nx = { git = "https://x/x" }', ["x @ git+https://x/x"]),
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
            # Замена каталога модулем делает модуль внешним, модуль-замена — тоже имя.
            ("replace github.com/a/one => ../one", "replace github.com/a/one => github.com/evil/one v1.0.0",
             ["github.com/a/one", "github.com/evil/one"]),
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

    def test_versioned_local_replace(self):
        # Замена каталогом с версией касается только этой версии (go проверен с GOPROXY=off): при другой версии в
        # require модуль ставится из сети.
        base = "module m\n\ngo 1.22\n\nrequire evil.com/x v1.2.0\n"
        self.assertEqual(_added("go.mod", "", base + "replace evil.com/x v1.0.0 => ./x\n"), ["evil.com/x"])
        self.assertEqual(_added("go.mod", "", base + "replace evil.com/x v1.2.0 => ./x\n"), [])
        self.assertEqual(_added("go.mod", "", base + "replace (\n\tevil.com/x v1.0.0 => ./x\n)\n"), ["evil.com/x"])

    def test_directory_forms(self):
        # modfile.IsDirectoryPath: `.`, `..`, буква диска без разделителя.
        for target in (".", "..", "C:", "c:x", ".\\x", "\\x"):
            with self.subTest(target):
                text = f"module m\nrequire example.com/root v0.0.0\nreplace example.com/root => {target}\n"
                self.assertEqual(manifests.names("gomod", text), frozenset())

    def test_replace_to_module_is_package(self):
        new = self.OLD.replace("go 1.22", "go 1.22\nrequire example.com/x v1\nreplace example.com/x => example.com/y v1")
        self.assertEqual(_added("go.mod", self.OLD, new), ["example.com/x", "example.com/y"])

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
        ]
        for line in cases:
            with self.subTest(line):
                self.assertEqual(_added("Gemfile", self.OLD, self.OLD + line + "\n"), ["pg"])
        line = 'gem "pg", git: "https://github.com/ged/ruby-pg"'
        self.assertEqual(_added("Gemfile", self.OLD, self.OLD + line + "\n"),
                         ["pg @ git+https://github.com/ged/ruby-pg"])

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

    def test_nested_source_blocks(self):
        # Источник гема — ближайший блок `path`, `git`, `github`, `source` на любой глубине; `path` — местный.
        cases = {
            'source "https://gems.example" do\n  path "x" do\n    gem "local"\n  end\n  gem "a"\nend\n':
                {"a @ https://gems.example"},
            'source "https://gems.example" do\n  git "https://g/r" do\n    gem "b"\n  end\nend\n':
                {"b @ git+https://g/r"},
            'group :dev do\n  source "https://gems.example" do\n    gem "c"\n  end\n  gem "d"\nend\n':
                {"c @ https://gems.example", "d"},
            'source "https://gems.example" do\n  group :dev do\n    path "x" do\n      gem "local"\n    end\n'
            '    gem "e"\n  end\nend\ngem "rails"\n': {"e @ https://gems.example", "rails"},
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(manifests.names("gemfile", text), frozenset(expected))

    def test_sources_inside_path_block(self):
        # `source` без блока глобален и внутри `path … do`; явный источник гема важнее местного блока.
        cases = {
            'path "x" do\n  source "https://evil"\n  gem "l"\nend\n': {"index https://evil"},
            'path "x" do\n  gem "l", git: "https://evil/l"\nend\n': {"l @ git+https://evil/l"},
            'path "x" do\n  gem "l", source: "https://evil"\nend\n': {"l @ https://evil"},
            'path "x" do\n  gem "a"; gem "l", github: "evil/l"\nend\n': {"l @ github:evil/l"},
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(manifests.names("gemfile", text), frozenset(expected))

    def test_linear_on_long_line(self):
        # Строка до размера манифеста: значение в кавычках, пробелы между частями оператора, хвост `do`.
        cases = {
            "quotes": lambda n: 'path "' + 'a"' * n,
            "spaces after do": lambda n: 'path "x" do' + " " * n + "x",
            "spaces after block word": lambda n: "path" + " " * n + "x",
            "spaces after gem": lambda n: "gem" + " " * n + "x",
            "spaces after source": lambda n: "source" + " " * n + "x",
            "spaces after source value": lambda n: 'source "x"' + " " * n + "x",
            "spaces after any do": lambda n: "x do" + " " * n + "x",
            "source statements": lambda n: "source 'x'; " * n + "gem 'a'",
            "spaces after source statement": lambda n: "gem 'a'; source 'x'" + " " * n + "x",
        }
        for name, make in cases.items():
            with self.subTest(name):
                small, large = make(2000), make(8000)
                assert_linear(self, lambda: manifests.names("gemfile", small),
                              lambda: manifests.names("gemfile", large))

    def test_never_unparseable(self):
        self.assertEqual(manifests.names("gemfile", "gem (\n"), frozenset())

    def test_parens_plugin_include(self):
        # `(gem …)` — тот же вызов; `plugin` Bundler::Plugin::DSL ставит как gem; eval_gemfile подключает файл.
        cases = {
            '(gem "evil")\n': {"evil"},
            '((gem("evil")))\n': {"evil"},
            'plugin "bundler-evil"\n': {"bundler-evil"},
            'plugin "bundler-evil", git: "https://evil/p"\n': {"bundler-evil @ git+https://evil/p"},
            'eval_gemfile "extra.rb"\n': {INC("extra.rb")},
            'eval_gemfile("extra.rb")\n': {INC("extra.rb")},
            "eval_gemfile File.join(__dir__, 'x')\n": set(),
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(manifests.names("gemfile", text), frozenset(expected))


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


class SessionStartTtlTest(unittest.TestCase):
    """Начало сессии (manifest_watch.mark_start) переживает common.prune_state в сессии дольше STATE_TTL."""

    def test_start_survives_prune_in_long_session(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict("os.environ", {"CLAUDE_PLUGIN_DATA": data}):
            state = pathlib.Path(data) / "state"
            state.mkdir()
            path = state / "s.start.json"
            path.write_text(json.dumps({"start": 5}), encoding="utf-8")
            old = time.time() - common.STATE_TTL - 60
            os.utime(path, (old, old))
            manifest_watch.mark_start("s")
            common.prune_state(state)
            self.assertEqual(manifest_watch.session_start("s"), 5)


def _call_with_timeout(test, fn, *args):
    """{"value": результат} или {"error": исключение} вызова fn(*args) в потоке; не вернулся за 5 с — провал теста,
    а не зависший прогон."""
    result = {}

    def run():
        try:
            result["value"] = fn(*args)
        except Exception as e:
            result["error"] = e
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    test.assertFalse(thread.is_alive(), "чтение FIFO зависло")
    return result


def _release_fifo(path):
    """Писатель отпускает читателя, если разбор всё же открыл FIFO path в блокирующем режиме."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


# Патч автора: package.json получает left-pad, setup.cfg — cfg-dep, requirements.txt — Requests_Foo; строки README —
# не манифест.
AUTHOR_PATCH = """\
diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1,3 +1,4 @@
 {
-  "dependencies": {"react": "^18"}
+  "dependencies": {"react": "^18",
+                   "left-pad": "^1.0.0"}
 }
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,2 @@
--- not-a-header
+++ also-not-a-header
diff --git a/setup.cfg b/setup.cfg
--- a/setup.cfg
+++ b/setup.cfg
@@ -1,3 +1,3 @@
 [options]
-install_requires = old-dep
+install_requires = cfg-dep
diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -0,0 +1 @@
+Requests_Foo==1.0
"""


class OldPatchTest(unittest.TestCase):
    """`git apply` патча, не менявшегося с начала сессии (ctime файла раньше начала): имена его изменённых строк
    в манифестах — работа до сессии (manifest_watch.restored_names)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.patch = os.path.join(self.root, "author.patch")
        with open(self.patch, "w", encoding="utf-8") as f:
            f.write(AUTHOR_PATCH)
        self.after = int(os.stat(self.patch).st_ctime) + 2
        self.deadline = time.monotonic() + 30

    def names(self, command, start=None):
        return manifest_watch.restored_names(self.root, command, self.after if start is None else start,
                                             self.deadline)

    def test_names_of_changed_manifest_lines(self):
        known = self.names("git apply author.patch")
        self.assertIn("left-pad", known["npm"])
        self.assertIn("react", known["npm"])
        self.assertIn("requests-foo", known["pypi"])
        # Блоки README и setup.cfg короче чисел своих заголовков `@@`: следующий заголовок файла всё равно узнаётся.
        self.assertIn("cfg-dep", known["pypi"])
        # Строки README — не манифест; строка `--- …` внутри блока — содержимое, не заголовок.
        self.assertNotIn("not-a-header", known.get("npm", []))
        self.assertNotIn("also-not-a-header", known.get("pypi", []))

    def test_forms(self):
        for command in ("git apply -p1 --exclude x --directory . author.patch", "git apply < author.patch",
                        "git apply - < author.patch", f"git apply {self.patch}", "git apply --3way -- author.patch",
                        "cd . && git apply -v author.patch"):
            with self.subTest(command):
                self.assertIn("left-pad", self.names(command).get("npm", []))

    def test_not_old_patch(self):
        cases = {
            # Патч изменён после начала сессии.
            "git apply author.patch": int(os.stat(self.patch).st_ctime) - 1,
            # Патч из heredoc, конвейера, без файла — текст агента.
            "git apply <<EOF\n" + AUTHOR_PATCH + "EOF": None,
            "cat author.patch | git apply": None,
            "git apply missing.patch": None,
            # Значение флага — не патч.
            "git apply --exclude author.patch": None,
            "git am author.patch": None,
        }
        for command, start in cases.items():
            with self.subTest(command):
                self.assertEqual(self.names(command, start), {})

    def test_no_session_start_reads_nothing(self):
        # Без начала сессии патч не читается: возраст не с чем сравнить.
        with mock.patch.object(manifest_watch, "_old_patch_names") as read:
            self.assertEqual(manifest_watch.restored_names(self.root, "git apply author.patch", None, self.deadline),
                             {})
        read.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "нет FIFO")
    def test_fifo_and_directory_not_read(self):
        fifo = os.path.join(self.root, "pipe.patch")
        os.mkfifo(fifo)
        self.addCleanup(_release_fifo, fifo)
        os.mkdir(os.path.join(self.root, "dir.patch"))
        result = _call_with_timeout(self, self.names, "git apply pipe.patch dir.patch")
        self.assertEqual(result.get("value"), {}, result)

    def test_large_patch_not_read(self):
        with open(self.patch, "a", encoding="utf-8") as f:
            f.write("x" * manifest_watch.MAX_MANIFEST_BYTES)
        self.assertEqual(self.names("git apply author.patch", int(time.time()) + 2), {})

    def test_relative_to_cwd(self):
        sub = os.path.join(self.root, "sub")
        os.mkdir(sub)
        known = manifest_watch.restored_names(self.root, "git apply ../author.patch", self.after, self.deadline,
                                              cwd=sub)
        self.assertIn("left-pad", known["npm"])


class OldPatchGitTest(unittest.TestCase):
    """Сравнение после `git apply` в настоящем репозитории: патч автора до сессии не блокируется, патч,
    записанный в сессии, — блокируется."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        with open(os.path.join(self.root, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"dependencies": {"react": "^18"}}\n')
        _git("init", "-q", cwd=self.root)
        _git("add", ".", cwd=self.root)
        _git("commit", "-qm", "i", cwd=self.root)
        with open(os.path.join(self.root, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"dependencies": {"react": "^18", "left-pad": "^1.0.0"}}\n')
        diff = subprocess.run(["git", "diff"], cwd=self.root, check=True, capture_output=True).stdout
        _git("checkout", "--", "package.json", cwd=self.root)
        self.patch = os.path.join(self.root, "author.patch")
        with open(self.patch, "wb") as f:
            f.write(diff)
        # Неотслеживаемый патч исключён из снимка: в .git/info/exclude.
        with open(os.path.join(self.root, ".git", "info", "exclude"), "a", encoding="utf-8") as f:
            f.write("author.patch\n")

    def added(self, start):
        deadline = time.monotonic() + 30
        command = "git apply author.patch"
        entry = manifest_watch.take(self.root, deadline)
        known = manifest_watch.restored_names(self.root, command, start, deadline)
        if known:
            entry["known"] = known
        _git("apply", "author.patch", cwd=self.root)
        return manifest_watch.compare(entry, deadline)[0]

    def test_patch_before_session_passes(self):
        self.assertEqual(self.added(int(os.stat(self.patch).st_ctime) + 2), {})

    def test_patch_written_in_session_blocked(self):
        self.assertEqual(self.added(int(os.stat(self.patch).st_ctime) - 1), {"package.json": ["left-pad"]})


class SourceTest(unittest.TestCase):
    """Источник пакета не из реестра по умолчанию — имя с источником (`имя @ источник`) или `index <url>`:
    смена источника существующего имени — новое имя, другая ссылка (коммит, тег) того же источника — нет."""

    def assert_added(self, path, old, cases):
        for new, expected in cases.items():
            with self.subTest(new):
                self.assertEqual(_added(path, old, new), expected)

    def test_npm_specs(self):
        old = '{"dependencies": {"left-pad": "^1", "a": "github:o/a#v1"}}'
        dep = '{"dependencies": {"left-pad": "%s", "a": "github:o/a#v1"}}'
        self.assert_added("package.json", old, {
            dep % "github:evil/evil": ["left-pad @ github:evil/evil"],
            dep % "evil/left-pad#v2": ["left-pad @ evil/left-pad"],
            dep % "git+https://x/evil.git#main": ["left-pad @ git+https://x/evil.git"],
            dep % "https://x/evil.tgz": ["left-pad @ https://x/"],
            dep % "jsr:@evil/pad": ["left-pad @ jsr:@evil/pad"],
            dep % "git@github.com:evil/pad.git": ["left-pad @ git@github.com:evil/pad.git"],
            # Реестр по умолчанию: версия, тег, каталог pnpm.
            dep % "^2": [],
            dep % "latest": [],
            dep % "catalog:": [],
            '{"dependencies": {"left-pad": "^1", "a": "github:o/a#v2"}}': [],
        })

    def test_npm_overrides(self):
        old = '{"dependencies": {"left-pad": "^1"}}'
        base = '{"dependencies": {"left-pad": "^1"}, %s}'
        self.assert_added("package.json", old, {
            base % '"overrides": {"left-pad": "npm:evil-pad@1"}': ["evil-pad"],
            base % '"overrides": {"react": {"left-pad": "github:evil/pad"}}': ["left-pad @ github:evil/pad"],
            base % '"overrides": {"left-pad@1": {".": "npm:evil-pad@1"}}': ["evil-pad"],
            base % '"resolutions": {"**/left-pad": "npm:evil-pad@1"}': ["evil-pad"],
            base % '"resolutions": {"a/@s/left-pad": "https://x/e.tgz"}': ["@s/left-pad @ https://x/"],
            base % '"pnpm": {"overrides": {"a>left-pad@<2": "npm:evil-pad"}}': ["evil-pad"],
            # Версия, ссылка на версию зависимости и местный пакет — не новый источник.
            base % '"overrides": {"left-pad": "1.3.0", "x": "$left-pad", "y": "file:../y"}': [],
        })

    def test_go_replace_module(self):
        old = "module m\n\ngo 1.22\n\nrequire golang.org/x/text v0.14.0\n"
        self.assert_added("go.mod", old, {
            old + "\nreplace golang.org/x/text => github.com/evil/text v0.1.0\n": ["github.com/evil/text"],
            old + "\nreplace (\n\tgolang.org/x/text v0.14.0 => github.com/evil/text v0.1.0\n)\n":
                ["github.com/evil/text"],
            # Замена версией того же модуля и каталогом — не новый источник.
            old + "\nreplace golang.org/x/text => golang.org/x/text v0.15.0\n": [],
            old + "\nreplace golang.org/x/text => ../text\n": [],
        })

    def test_cargo_sources(self):
        old = '[package]\nname = "app"\n\n[dependencies]\nserde = "1"\nx = { git = "https://x/x", rev = "a" }\n'
        self.assert_added("Cargo.toml", old, {
            old + '\n[patch.crates-io]\nserde = { git = "https://github.com/evil/serde" }\n':
                ["serde @ git+https://github.com/evil/serde"],
            old + '\n[patch."https://github.com/rust-lang/crates.io-index"]\nserde = { git = "https://e/s", '
                  'branch = "b" }\n': ["serde @ git+https://e/s"],
            old + '\n[patch.crates-io]\nserde = { path = "../serde" }\n': [],
            old.replace('serde = "1"', 'serde = { version = "1", registry = "evil" }'): ["serde @ registry:evil"],
            old + '\n[replace]\n"serde:1.0.0" = { git = "https://e/serde" }\n': ["serde @ git+https://e/serde"],
            old.replace('rev = "a"', 'rev = "b"'): [],
        })

    def test_requirements_sources(self):
        old = "requests\n"
        self.assert_added("requirements.txt", old, {
            "--extra-index-url https://evil.example/simple\nrequests\n": ["index https://evil.example/simple"],
            "-i https://evil/simple/\nrequests\n": ["index https://evil/simple"],
            "--index-url=https://evil/simple\nrequests\n": ["index https://evil/simple"],
            "-f https://evil/links\nrequests\n": ["index https://evil/links"],
            "requests @ git+https://github.com/evil/requests@v2\n": ["requests @ git+https://github.com/evil/requests"],
            # Каталог колёс и индекс PyPI по умолчанию — не новый источник.
            "--find-links ./wheels\nrequests\n": [],
            "--index-url https://pypi.org/simple\nrequests\n": [],
        })

    def test_pyproject_sources(self):
        old = '[project]\nname = "app"\ndependencies = ["requests"]\n'
        self.assert_added("pyproject.toml", old, {
            old.replace('"requests"', '"requests @ https://evil/r.whl"'): ["requests @ https://evil/"],
            old + '\n[tool.uv.sources]\nrequests = { git = "https://evil/r", tag = "v1" }\n':
                ["requests @ git+https://evil/r"],
            old + '\n[tool.uv.sources]\nrequests = { index = "evil" }\n': ["requests @ index:evil"],
            old + '\n[[tool.uv.index]]\nname = "evil"\nurl = "https://evil/simple"\n': ["index https://evil/simple"],
            old + '\n[tool.uv]\nextra-index-url = ["https://evil/simple"]\n': ["index https://evil/simple"],
            old + '\n[[tool.poetry.source]]\nname = "evil"\nurl = "https://evil/simple"\n':
                ["index https://evil/simple"],
            old + '\n[[tool.pdm.source]]\nname = "evil"\nurl = "https://evil/simple"\n':
                ["index https://evil/simple"],
            '[tool.poetry.dependencies]\nrequests = { git = "https://evil/r" }\n': ["requests @ git+https://evil/r"],
        })

    def test_gemfile_sources(self):
        old = GemfileTest.OLD
        self.assert_added("Gemfile", old, {
            old.replace('"~> 7.1"', '"~> 7.1", git: "https://github.com/evil/rails"'):
                ["rails @ git+https://github.com/evil/rails"],
            old.replace('"~> 7.1"', '"~> 7.1", :git => "https://github.com/evil/rails"'):
                ["rails @ git+https://github.com/evil/rails"],
            old.replace('"~> 7.1"', '"~> 7.1", github: "evil/rails"'): ["rails @ github:evil/rails"],
            old.replace('"~> 7.1"', '"~> 7.1", source: "https://evil"'): ["rails @ https://evil"],
            old.replace('gem "rails", "~> 7.1"\n', 'git "https://github.com/evil/rails" do\n  gem "rails"\nend\n'):
                ["rails @ git+https://github.com/evil/rails"],
            old.replace('gem "rails", "~> 7.1"\n', 'source "https://evil" do\n  gem "rails"\nend\n'):
                ["rails @ https://evil"],
            old + 'source "https://evil"\n': ["index https://evil"],
            # Ключ строкой (Bundler::Dsl.normalize_hash) и git_source gist, bitbucket, gitlab.
            old.replace('"~> 7.1"', '"~> 7.1", "git" => "https://evil/rails"'): ["rails @ git+https://evil/rails"],
            old.replace('"~> 7.1"', '"~> 7.1", "github" => "evil/rails"'): ["rails @ github:evil/rails"],
            old.replace('"~> 7.1"', '"~> 7.1", bitbucket: "evil/rails"'): ["rails @ bitbucket:evil/rails"],
            old.replace('"~> 7.1"', '"~> 7.1", gitlab: "evil/rails"'): ["rails @ gitlab:evil/rails"],
            old.replace('"~> 7.1"', '"~> 7.1", :gist => "abc"'): ["rails @ gist:abc"],
            old + 'gem "local", "path" => "../local"\n': [],
        })

    def test_override_sources(self):
        # Замены и ограничения пакета не добавляют; URL в них меняет источник пакета графа (uv 0.12 качает его
        # и для constraint-dependencies; pdm — «Override the resolved package versions»).
        old = '[project]\nname = "app"\ndependencies = ["urllib3"]\n'
        self.assert_added("pyproject.toml", old, {
            old + '\n[tool.uv]\noverride-dependencies = '
                  '["urllib3 @ https://evil/u/urllib3-9-py3-none-any.whl", "new"]\n':
                ["urllib3 @ https://evil/u/"],
            old + '\n[tool.uv]\nconstraint-dependencies = ["urllib3 @ git+https://evil/u", "urllib3<2"]\n':
                ["urllib3 @ git+https://evil/u"],
            old + '\n[tool.uv]\noverride-dependencies = ["urllib3 @ file:///tmp/u.whl"]\n': [],
            old + '\n[tool.pdm.resolution.overrides]\nurllib3 = "https://evil/urllib3.whl"\nidna = "3.7"\n':
                ["urllib3 @ https://evil/"],
            old + '\n[tool.pdm.resolution.overrides]\nurllib3 = "file:///tmp/urllib3.whl"\n': [],
        })
        poetry = '[tool.poetry]\nname = "app"\n[tool.poetry.dependencies]\nfoo = "^1"\n'
        self.assert_added("pyproject.toml", poetry, {
            poetry.replace('foo = "^1"', 'foo = [{ version = "^1", python = "<3.8" }, { git = "https://evil/foo", '
                                         'python = ">=3.8" }]'): ["foo @ git+https://evil/foo"],
            poetry.replace('foo = "^1"', 'foo = [{ version = "^1", python = "<3.8" }, { version = "^2" }]'): [],
        })

    def test_pnpm_package_extensions(self):
        old = '{"dependencies": {"react": "^18"}}'
        self.assert_added("package.json", old, {
            '{"dependencies": {"react": "^18"}, "pnpm": {"packageExtensions": {"react": {"dependencies": '
            '{"evil-pkg": "^1"}, "peerDependencies": {"react": "*"}}}}}': ["evil-pkg"],
            '{"dependencies": {"react": "^18"}, "pnpm": {"packageExtensions": {"react": {"optionalDependencies": '
            '{"x": "github:evil/x"}}}}}': ["x @ github:evil/x"],
        })

    def test_composer_package_repository(self):
        old = '{"require": {"monolog/monolog": "^3"}}'
        repo = '{"require": {"monolog/monolog": "^3"}, "repositories": [{"type": "package", "package": %s}]}'
        self.assert_added("composer.json", old, {
            repo % '{"name": "Monolog/Monolog", "version": "3.0.0", '
                   '"dist": {"url": "https://evil/m.zip", "type": "zip"}}':
                ["monolog/monolog @ https://evil/"],
            repo % '[{"name": "monolog/monolog", "version": "3.0.0", "source": {"url": "https://evil/m.git", '
                   '"type": "git", "reference": "x"}}]': ["monolog/monolog @ https://evil/m.git"],
        })

    def test_archive_url_source_is_directory(self):
        # Источник архива — каталог URL: другая версия файла в том же каталоге — не новое имя, другой хост или
        # каталог — новое.
        req = "pkg @ https://h/d/pkg-1.0-py3-none-any.whl\n"
        self.assert_added("requirements.txt", req, {
            "pkg @ https://h/d/pkg-2.0-py3-none-any.whl\n": [],
            "https://h/d/pkg-2.0-py3-none-any.whl\n": [],
            "pkg @ https://h/d/pkg-2.0.tar.gz?token=1#sha256=00\n": [],
            "pkg @ https://evil/d/pkg-1.0-py3-none-any.whl\n": ["pkg @ https://evil/d/"],
            "pkg @ https://h/e/pkg-1.0-py3-none-any.whl\n": ["pkg @ https://h/e/"],
        })
        npm = '{"dependencies": {"x": "%s"}}'
        self.assert_added("package.json", npm % "https://h/x-1.0.tgz", {
            npm % "https://h/x-2.0.tgz": [],
            npm % "https://evil/x-1.0.tgz": ["x @ https://evil/"],
        })
        uv = '[project]\nname = "app"\ndependencies = ["x"]\n\n[tool.uv.sources]\nx = { url = "%s" }\n'
        self.assert_added("pyproject.toml", uv % "https://h/x-1.0.zip", {
            uv % "https://h/x-2.0.zip": [],
            uv % "https://evil/x-1.0.zip": ["x @ https://evil/"],
        })

    def test_composer_repositories(self):
        old = '{"require": {"monolog/monolog": "^3"}}'
        self.assert_added("composer.json", old, {
            '{"require": {"monolog/monolog": "^3"}, "repositories": [{"type": "vcs", '
            '"url": "https://github.com/evil/monolog"}]}': ["index https://github.com/evil/monolog"],
            '{"require": {"monolog/monolog": "^3"}, "repositories": {"e": {"type": "composer", '
            '"url": "https://evil"}}}': ["index https://evil"],
            '{"require": {"monolog/monolog": "^3"}, "repositories": [{"type": "path", "url": "../x"}, '
            '{"packagist.org": false}]}': [],
        })


class StatementsTest(unittest.TestCase):
    def test_gemfile_statements_on_one_line(self):
        self.assertEqual(manifests.names("gemfile", 'gem "rails"; gem "evil"\n'), frozenset({"rails", "evil"}))
        self.assertEqual(manifests.names("gemfile", "group :test do; gem 'evil'; end\n"), frozenset({"evil"}))
        self.assertEqual(manifests.names("gemfile", 'gem "a;b"\n'), frozenset())

    def test_gemfile_global_source_per_statement(self):
        for text in ["source 'https://evil'; gem 'a'\n", "gem 'a'; source 'https://evil'\n",
                     "gem 'a'; source('https://evil') # x\n"]:
            with self.subTest(text):
                self.assertEqual(manifests.names("gemfile", text), frozenset({"a", "index https://evil"}))

    def test_hatch_envs(self):
        text = ('[tool.hatch.envs.default]\ndependencies = ["pytest"]\n\n'
                '[tool.hatch.envs.lint]\nextra-dependencies = ["Ruff>=0.5"]\n')
        self.assertEqual(manifests.names("pyproject", text), frozenset({"pytest", "ruff"}))


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class OwnNameTrustTest(unittest.TestCase):
    """Имя пакета манифеста — пакет проекта, только если манифест в HEAD или член workspace корня (`workspaces`
    package.json, `[tool.uv.workspace]` pyproject.toml); имя свежего манифеста вне workspace — внешнее."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        _write(self.root, "package.json", '{"name": "app", "dependencies": {"left-pad": "^1"}}')
        _write(self.root, "tools/fake/package.json", '{"name": "evil-pkg"}')

    def names(self, kind="package.json"):
        return manifest_watch.project_names(self.root, kind, time.monotonic() + 30)

    def test_fresh_manifest_name_not_project_package(self):
        self.assertNotIn("evil-pkg", self.names())
        self.assertIn("left-pad", self.names())

    def test_workspace_member_name_is_project_package(self):
        for workspaces in ('["tools/*"]', '{"packages": ["tools/**"]}', '["./tools/fake/"]'):
            with self.subTest(workspaces):
                _write(self.root, "package.json", '{"name": "app", "workspaces": %s}' % workspaces)
                self.assertIn("evil-pkg", self.names())
        for workspaces in ('["tools/*", "!tools/fake"]', '["other/*"]', '["tools"]'):
            with self.subTest(workspaces):
                _write(self.root, "package.json", '{"name": "app", "workspaces": %s}' % workspaces)
                self.assertNotIn("evil-pkg", self.names())

    def test_uv_workspace_member(self):
        _write(self.root, "pyproject.toml", '[project]\nname = "app"\n\n[tool.uv.workspace]\nmembers = ["libs/*"]\n'
                                            'exclude = ["libs/skip"]\n')
        _write(self.root, "libs/a/pyproject.toml", '[project]\nname = "Lib_A"\n')
        _write(self.root, "libs/skip/pyproject.toml", '[project]\nname = "skipped"\n')
        _write(self.root, "other/pyproject.toml", '[project]\nname = "other"\n')
        names = self.names("pyproject")
        self.assertIn("lib-a", names)
        self.assertNotIn("skipped", names)
        self.assertNotIn("other", names)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_committed_manifest_name_is_project_package(self):
        _git("init", "-q", cwd=self.root)
        self.assertNotIn("evil-pkg", self.names())
        _git("add", ".", cwd=self.root)
        _git("commit", "-qm", "i", cwd=self.root)
        self.assertIn("evil-pkg", self.names())

    def test_compare_fresh_manifest_name_is_new(self):
        os.remove(os.path.join(self.root, "tools/fake/package.json"))
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "tools/fake/package.json", '{"name": "evil-pkg"}')
        _write(self.root, "package.json", '{"name": "app", "dependencies": {"left-pad": "^1", "evil-pkg": "^1"}}')
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30)[0], {"package.json": ["evil-pkg"]})
        # Свежий манифест в снимке перед следующей командой — тоже не пакет проекта.
        _write(self.root, "package.json", '{"name": "app", "dependencies": {"left-pad": "^1"}}')
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "package.json", '{"name": "app", "dependencies": {"left-pad": "^1", "evil-pkg": "^1"}}')
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30)[0], {"package.json": ["evil-pkg"]})

    def test_compare_workspace_member_not_new(self):
        _write(self.root, "package.json", '{"name": "app", "workspaces": ["tools/*"], "dependencies": {}}')
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "package.json", '{"name": "app", "workspaces": ["tools/*"], '
                                          '"dependencies": {"evil-pkg": "*"}}')
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30)[0], {})


@unittest.skipUnless(hasattr(os, "mkfifo"), "нет FIFO")
class FifoManifestTest(unittest.TestCase):
    """FIFO под именем манифеста и ссылка на него не вешают хук: не обычный файл не читается."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        _write(self.root, "requirements.txt", "requests\n")
        self.fifo = os.path.join(self.root, "requirements-dev.txt")
        os.mkfifo(self.fifo)
        os.makedirs(os.path.join(self.root, "sub"))
        os.symlink(self.fifo, os.path.join(self.root, "sub", "requirements-dev.txt"))
        self.addCleanup(_release_fifo, self.fifo)

    def call(self, fn, *args):
        return _call_with_timeout(self, fn, *args)

    def test_project_names(self):
        result = self.call(manifest_watch.project_names, self.root, "requirements", time.monotonic() + 30)
        self.assertEqual(result.get("value"), frozenset({"requests"}), result)

    def test_edit_texts(self):
        for path in (self.fifo, os.path.join(self.root, "sub", "requirements-dev.txt")):
            with self.subTest(path):
                result = self.call(manifest_watch.edit_texts, "Write", {"content": "flask\n"}, path)
                self.assertIsInstance(result.get("error"), manifest_watch.Unavailable, result)



class EditTargetTest(unittest.TestCase):
    """Правка файловым инструментом по имени в другом регистре и по жёсткой ссылке правит манифест."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        _write(self.root, "package.json", '{"dependencies": {"react": "^18"}}\n')
        self.manifest = os.path.join(self.root, "package.json")

    def targets(self, rel):
        return manifest_watch.edit_targets(os.path.join(self.root, rel), lambda: self.root)

    def check(self, rel, content):
        """Новые имена Write content по пути rel: {путь от корня: имена} по каждому манифесту edit_targets."""
        targets, problem = self.targets(rel)
        self.assertIsNone(problem)
        return {os.path.relpath(path, self.root): manifest_watch.check_edit("Write", {"content": content}, path, kind,
                                                                            root=lambda: self.root)
                for path, kind in targets}

    def test_hard_link_is_manifest(self):
        os.link(self.manifest, os.path.join(self.root, "notes.json"))
        self.assertEqual(self.targets("notes.json"), ([(self.manifest, "package.json")], None))
        self.assertEqual(self.check("notes.json", '{"dependencies": {"react": "^18", "evil": "1"}}'),
                         {"package.json": ["evil"]})

    def test_hard_link_with_manifest_name_checked_by_each_kind(self):
        # Ссылка с именем манифеста другого вида: файл — и package.json, и sub/Gemfile, каждый по своему виду.
        os.makedirs(os.path.join(self.root, "sub"))
        os.link(self.manifest, os.path.join(self.root, "sub", "Gemfile"))
        targets, problem = self.targets("sub/Gemfile")
        self.assertIsNone(problem)
        self.assertEqual(sorted(targets), sorted([(os.path.join(self.root, "sub", "Gemfile"), "gemfile"),
                                                  (self.manifest, "package.json")]))
        self.assertEqual(self.check("sub/Gemfile", '{"dependencies": {"react": "^18", "evil": "1"}}'),
                         {"package.json": ["evil"], "sub/Gemfile": []})

    def test_hard_link_in_foreign_dir(self):
        # Ссылка под FOREIGN_DIRS сама не манифест, но правит манифест проекта.
        os.makedirs(os.path.join(self.root, "fixtures"))
        os.link(self.manifest, os.path.join(self.root, "fixtures", "package.json"))
        self.assertEqual(self.targets("fixtures/package.json"), ([(self.manifest, "package.json")], None))
        self.assertEqual(self.check("fixtures/package.json", '{"dependencies": {"react": "^18", "evil": "1"}}'),
                         {"package.json": ["evil"]})

    def test_plain_file_not_manifest(self):
        _write(self.root, "notes.json", "{}")
        os.link(os.path.join(self.root, "notes.json"), os.path.join(self.root, "notes2.json"))
        self.assertEqual(self.targets("notes.json"), ([], None))
        self.assertEqual(self.targets("absent.json"), ([], None))

    def test_single_link_not_listed(self):
        # Одна ссылка или не обычный файл — перечень манифестов не нужен.
        _write(self.root, "notes.json", "{}")
        os.mkfifo(os.path.join(self.root, "pipe.json"))
        with mock.patch.object(manifest_watch, "list_manifests", side_effect=AssertionError("перечень")):
            self.assertEqual(self.targets("notes.json"), ([], None))
            self.assertEqual(self.targets("pipe.json"), ([], None))
            self.assertEqual(self.targets("package.json"), ([(self.manifest, "package.json")], None))

    def test_hard_link_unlisted_unavailable(self):
        os.link(self.manifest, os.path.join(self.root, "notes.json"))
        with mock.patch.object(manifest_watch, "MAX_MANIFESTS", 0):
            targets, problem = self.targets("notes.json")
            self.assertEqual(targets, [])
            self.assertRegex(problem, "^жёсткая ссылка не сверена с манифестами проекта: манифестов больше 0$")
            # Манифест по имени проверяется и без перечня.
            self.assertEqual(self.targets("package.json")[0], [(self.manifest, "package.json")])

    def test_other_case_on_case_insensitive_fs(self):
        # Файловая система без учёта регистра: запись `PACKAGE.JSON` — тот же файл, что package.json (здесь —
        # жёсткая ссылка), путь проверки — каноническая запись каталога.
        os.link(self.manifest, os.path.join(self.root, "PACKAGE.JSON"))
        with mock.patch.object(manifest_watch.common, "case_insensitive", return_value=True):
            self.assertEqual(self.targets("PACKAGE.JSON"), ([(self.manifest, "package.json")], None))
            # Нового файла ещё нет: он и будет манифестом.
            self.assertEqual(self.targets("sub/GEMFILE"),
                             ([(os.path.join(self.root, "sub", "GEMFILE"), "gemfile")], None))
            self.assertEqual(self.targets("tests/fixtures/GEMFILE"), ([], None))
            # Регистр сворачивается только у имён видов: проза в `Requirements/` — не манифест.
            self.assertEqual(self.targets("ci/Requirements/notes.txt"), ([], None))
            self.assertEqual(self.targets("Requirements.TXT"), ([], None))

    def test_other_case_components_on_case_insensitive_fs(self):
        # Файловая система без учёта регистра: имя в другом регистре находит ту же запись каталога, а realpath регистр
        # не правит. Здесь запись в другом регистре — ссылка (по одной за раз: на такой файловой системе двух записей,
        # различных только регистром, не бывает), а realpath ссылки не разрешает. Каталоги и имена requirements
        # сверяются по настоящим записям: `REQUIREMENTS/base.txt` — манифест requirements/base.txt, а
        # `requirements/notes.txt` в настоящем каталоге `Docs/Requirements` — нет.
        _write(self.root, "requirements.txt", "django\n")
        _write(self.root, "requirements/base.txt", "django\n")
        _write(self.root, "Docs/Requirements/notes.txt", "prose\n")
        top, base = os.path.join(self.root, "requirements.txt"), os.path.join(self.root, "requirements", "base.txt")
        cases = [("REQUIREMENTS.TXT", "requirements.txt", "REQUIREMENTS.TXT", [(top, "requirements")]),
                 ("Requirements.txt", "requirements.txt", "Requirements.txt", [(top, "requirements")]),
                 ("REQUIREMENTS", "requirements", "REQUIREMENTS/base.txt", [(base, "requirements")]),
                 ("requirements/BASE.TXT", "base.txt", "requirements/BASE.TXT", [(base, "requirements")]),
                 ("Docs/requirements", "Requirements", "Docs/requirements/notes.txt", [])]
        with mock.patch.object(manifest_watch.common, "case_insensitive", return_value=True), \
                mock.patch("os.path.realpath", os.path.abspath):
            for alias, target, rel, expected in cases:
                with self.subTest(rel):
                    link = os.path.join(self.root, alias)
                    os.symlink(target, link)
                    try:
                        self.assertEqual(self.targets(rel), (expected, None))
                        if expected:
                            self.assertEqual(self.check(rel, "django\nevil\n"),
                                             {os.path.relpath(expected[0][0], self.root): ["evil"]})
                    finally:
                        os.unlink(link)

    def test_other_case_through_link_on_case_insensitive_fs(self):
        # Регистр сворачивается и у разрешённого пути: ссылка `notes.txt` с именем, не похожим на манифест, ведёт в
        # файл `Package.json`, который на такой файловой системе — package.json; по самому имени path вида нет.
        target = os.path.join(self.root, "Package.json")
        _write(self.root, "Package.json", "{}")
        os.symlink("Package.json", os.path.join(self.root, "notes.txt"))
        with mock.patch.object(manifest_watch.common, "case_insensitive", return_value=True):
            self.assertEqual(self.targets("notes.txt"), ([(target, "package.json")], None))

    def test_other_case_on_case_sensitive_fs(self):
        _write(self.root, "GEMFILE", "gem 'x'\n")
        self.assertEqual(self.targets("GEMFILE"), ([], None))
        self.assertEqual(self.targets("cargo.toml"), ([], None))


class IncludeTest(unittest.TestCase):
    """Подключение файла (`-r`, `-c`, eval_gemfile) — новое имя, если файл не проверяется сам: не манифест из
    перечня проекта (list_manifests) по пути от каталога манифеста."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        for rel in ("requirements-dev.txt", "requirements/constraints.txt", "requirements/base.txt",
                    "requirements.txt", "engines/Gemfile", "vendor/requirements.txt"):
            _write(self.root, rel, "")

    def added(self, rel, content):
        path = os.path.join(self.root, rel)
        return manifest_watch.check_edit("Write", {"content": content}, path, manifest_watch.watched_kind(rel),
                                         root=lambda: self.root)

    def test_includes(self):
        cases = [
            ("requirements.txt", "-r requirements-dev.txt\n", []),
            ("requirements.txt", "-c requirements/constraints.txt\n", []),
            ("requirements/dev.txt", "-r base.txt\n", []),
            ("requirements/dev.txt", "-r ../requirements.txt\n", []),
            ("requirements.txt", "-r extra.list\n", [INC("extra.list")]),
            ("requirements.txt", "-c constraints.txt\n", [INC("constraints.txt")]),
            # Каталог FOREIGN_DIRS, вне корня и URL — файл не проверяется.
            ("requirements.txt", "-r vendor/requirements.txt\n", [INC("vendor/requirements.txt")]),
            ("requirements.txt", "-r ../out/requirements.txt\n", [INC("../out/requirements.txt")]),
            ("requirements.txt", "-r https://evil/requirements.txt\n", [INC("https://evil/requirements.txt")]),
            ("Gemfile", 'eval_gemfile "engines/Gemfile"\n', []),
            ("Gemfile", 'eval_gemfile "Gemfile.local"\n', [INC("Gemfile.local")]),
            # Файла нет — он не в перечне манифестов проекта.
            ("requirements.txt", "-r requirements-absent.txt\n", [INC("requirements-absent.txt")]),
        ]
        for rel, content, expected in cases:
            with self.subTest(rel=rel, content=content):
                self.assertEqual(self.added(rel, content), expected)

    def test_package_named_include_not_dropped(self):
        # Пакет `include` с источником — имя пакета, не подключение, даже если текст как у подключения манифеста.
        name = "include @ git+https://evil.example/requirements.txt"
        for rel, content in [("requirements.txt", name + "\n"),
                             ("pyproject.toml", f'[project]\nname = "app"\ndependencies = ["{name}"]\n')]:
            with self.subTest(rel):
                self.assertEqual(self.added(rel, content), [name])
                self.assertNotIsInstance(self.added(rel, content)[0], manifests.Include)
        self.assertEqual(self.added("requirements.txt", "include ${X}/requirements.txt\n"),
                         ["include ${X}/requirements.txt"])
        self.assertNotEqual(INC("x"), "include x")
        self.assertNotIn("include x", {INC("x")})

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_ignored_include_not_checked(self):
        # Файл, исключённый git, не в перечне манифестов: его подключение — новое имя и после команды.
        _git("init", "-q", cwd=self.root)
        _write(self.root, ".gitignore", "requirements-local.txt\n")
        _write(self.root, "requirements.txt", "requests\n")
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "requirements-local.txt", "evilpkg\n")
        self.assertEqual(self.added("requirements.txt", "requests\n-r requirements-local.txt\n"),
                         [INC("requirements-local.txt")])
        _write(self.root, "requirements.txt", "requests\n-r requirements-local.txt\n")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": [INC("requirements-local.txt")]}, [], None))

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_include_named_by_target_from_project_root(self):
        # Подключение — путь цели от корня проекта, разрешённый от каталога манифеста: `-r base.txt` в
        # requirements/dev.txt — requirements/base.txt и не маскирует `-r base.txt` корневого файла, исключённого git;
        # `-r ../extra.list` в requirements/dev.txt — тот же extra.list, что `-r extra.list` в корне.
        _git("init", "-q", cwd=self.root)
        _write(self.root, ".gitignore", "/base.txt\n")
        _write(self.root, "requirements/dev.txt", "-r base.txt\n-r ../extra.list\npytest\n")
        _write(self.root, "requirements.txt", "requests\n")
        _git("add", "-A", cwd=self.root)
        _git("-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "i", cwd=self.root)
        entry = json.loads(json.dumps(manifest_watch.take(self.root, time.monotonic() + 30)))
        self.assertIn({"include": "requirements/base.txt"}, entry["files"]["requirements/dev.txt"][3])
        _write(self.root, "base.txt", "evilpkg\n")

        def project():
            return manifest_watch.project_names(self.root, "requirements", time.monotonic() + 30)
        path = os.path.join(self.root, "requirements.txt")
        for content, expected in [("requests\n-r base.txt\n", [INC("base.txt")]),
                                  ("requests\n-r ./sub/../base.txt\n", [INC("base.txt")]),
                                  ("requests\n-r extra.list\n", [])]:
            with self.subTest(content):
                self.assertEqual(manifest_watch.check_edit("Write", {"content": content}, path, "requirements",
                                                           project, root=lambda: self.root), expected)
        _write(self.root, "requirements.txt", "requests\n-r base.txt\n-r extra.list\n")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": [INC("base.txt")]}, [], None))

    def test_snapshot_keeps_include_type(self):
        # Подключение в снимке остаётся подключением: прежнее `-r extra.list` после команды не новое.
        _write(self.root, "requirements.txt", "django\n-r extra.list\n")
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        entry = json.loads(json.dumps(entry))
        self.assertIn({"include": "extra.list"}, entry["files"]["requirements.txt"][3])
        _write(self.root, "requirements.txt", "django\n-r extra.list\nflask\n")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": ["flask"]}, [], None))

    def test_symlink_out_of_project(self):
        # Ссылка-манифест проекта на файл вне его — в перечне, снимок читает файл по ссылке: подключение ссылки
        # проверяется само; тот же файл по пути вне проекта — не в перечне.
        with tempfile.TemporaryDirectory() as out:
            out = os.path.realpath(out)
            _write(out, "requirements.txt", "evil\n")
            os.symlink(os.path.join(out, "requirements.txt"), os.path.join(self.root, "requirements-x.txt"))
            self.assertEqual(self.added("requirements.txt", "-r requirements-x.txt\n"), [])
            os.unlink(os.path.join(self.root, "requirements-x.txt"))
            target = os.path.join(out, "requirements.txt")
            self.assertEqual(self.added("requirements.txt", f"-r {target}\n"), [INC(target)])

    def test_compare_drops_checked_include(self):
        _write(self.root, "requirements.txt", "django\n")
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "requirements.txt", "django\n-r requirements-dev.txt\n-r extra.list\n")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": [INC("extra.list")]}, [], None))


class BrokenOldSideTest(unittest.TestCase):
    """Старая сторона, которую не разобрать и которой нет в версиях git, — пустая: битый манифест и затем
    зависимость не проходят в два шага."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_edit_two_steps(self):
        with self.assertRaises(manifest_watch.Unavailable):
            manifest_watch.edit_names("package.json", None, "{", lambda: None)
        self.assertEqual(manifest_watch.edit_names("package.json", "{", '{"dependencies": {"evil": "1"}}',
                                                   lambda: None), ["evil"])
        # Версия git есть — сравнение с ней.
        self.assertEqual(manifest_watch.edit_names("package.json", "{", '{"dependencies": {"evil": "1"}}',
                                                   lambda: frozenset({"evil"})), [])

    def test_command_two_steps(self):
        path = os.path.join(self.root, "package.json")
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "package.json", "{")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30), ({}, ["package.json"], None))
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "package.json", '{"dependencies": {"evil": "1"}}')
        self.assertTrue(os.path.exists(path))
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30), ({"package.json": ["evil"]}, [], None))


class CompareLostListTest(unittest.TestCase):
    """Смена режима за команду и переполнение списка манифестов: пути снимка всё равно сверяются, причина — третьим
    элементом и при найденных именах."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        _write(self.root, "requirements.txt", "django\n")
        self.entry = manifest_watch.take(self.root, time.monotonic() + 30)

    def compare(self):
        return manifest_watch.compare(self.entry, time.monotonic() + 30)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_mode_change(self):
        _write(self.root, "requirements.txt", "django\nevil\n")
        _git("init", "-q", cwd=self.root)
        self.assertEqual(self.compare(), ({"requirements.txt": ["evil"]}, [],
                                          "за команду корень проекта стал git-репозиторием"))

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_mode_change_without_names(self):
        _git("init", "-q", cwd=self.root)
        self.assertEqual(self.compare(), ({}, [], "за команду корень проекта стал git-репозиторием"))

    def test_overflow(self):
        _write(self.root, "requirements.txt", "django\nevil\n")
        _write(self.root, "a/requirements.txt", "x\n")
        with mock.patch.object(manifest_watch, "MAX_MANIFESTS", 1):
            self.assertEqual(self.compare(), ({"requirements.txt": ["evil"]}, [], "манифестов больше 1"))
            _write(self.root, "requirements.txt", "django\n")
            self.assertEqual(self.compare(), ({}, [], "манифестов больше 1"))

    def test_walk_overflow(self):
        _write(self.root, "requirements.txt", "django\nevil\n")
        with mock.patch.object(manifest_watch, "MAX_WALK_FILES", 0):
            self.assertEqual(self.compare(), ({"requirements.txt": ["evil"]}, [], "вне git больше 0 файлов"))
