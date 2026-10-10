import codecs
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
    # Оператор на нескольких строках: опции гема (`path:`) на строках после запятой — его опции. Ожидание сверено с
    # вычислением этого Gemfile в Ruby 3.4 заглушкой DSL Bundler (обе ветви `if next?`).
    "gemfile-gitlab/Gemfile": set("""
        CFPropertyList RedCloth acme-client addressable akismet amazing_print apnotic apollo_upload_server
        app_store_connect arr-pm asciidoctor asciidoctor-include-ext asciidoctor-kroki asciidoctor-plantuml async
        atlassian-jwt attr_encrypted aws-actionmailer-ses aws-sdk-cloudformation aws-sdk-core aws-sdk-s3
        axe-core-rspec babosa base32 base64 batch-loader bcrypt benchmark-ips benchmark-memory benchmark-swap
        bootsnap browser bullet capybara capybara-screenshot carrierwave charlock_holmes circuitbox
        click_house-client commonmarker concurrent-ruby connection_pool countries coverband creole css_parser
        cssbundling-rails cvss-suite database_cleaner-active_record debug declarative_policy derailed_benchmarks
        devfile device_detector devise devise-two-factor diffy doorkeeper doorkeeper-device_authorization_grant
        doorkeeper-openid_connect drb duo_api ed25519 elasticsearch-api elasticsearch-model elasticsearch-rails
        email_reply_trimmer email_spec factory_bot_rails faraday faraday-multipart faraday-retry faraday-typhoeus
        faraday_middleware-aws-sigv4 fast_blank ffaker ffi flipper flipper-active_record
        flipper-active_support_cache_store fog-aliyun fog-aws fog-core fog-google fog-local fugit gdk-toogle gettext
        gettext_i18n_rails git gitaly gitlab-chronic gitlab-cloud-connector gitlab-crystalball gitlab-dangerfiles
        gitlab-experiment gitlab-fog-azure-rm gitlab-glaz gitlab-glfm-markdown gitlab-grape-openapi gitlab-kas-grpc
        gitlab-labkit gitlab-license gitlab-mail_room gitlab-markup gitlab-net-dns gitlab-orbit-proto
        gitlab-rspec-metrics-exporter gitlab-sdk gitlab-secret_detection gitlab-security_report_schemas
        gitlab-styles gitlab_chronic_duration gitlab_omniauth-ldap gitlab_quality-test_tooling gitlab_query_language
        gon google-apis-androidpublisher_v3 google-apis-cloudbilling_v1 google-apis-cloudresourcemanager_v1
        google-apis-compute_v1 google-apis-container_v1 google-apis-container_v1beta1 google-apis-core
        google-apis-iam_v1 google-apis-serviceusage_v1 google-apis-sqladmin_v1beta4 google-apis-storage_v1
        google-cloud-artifact_registry-v1 google-cloud-compute-v1 google-cloud-storage google-protobuf googleauth
        gpgme grape grape-entity grape-path-helpers grape-swagger grape-swagger-entity grape_logging graphlyte
        graphql graphql-docs grpc grpc-tools gssapi guard-rspec gvltools haml_lint hamlit hashdiff hashie
        health_check html-pipeline html2text httparty i18n_data icalendar invisible_captcha io-event ipaddress
        jira-ruby js-routes js_regex json json_schemer jsonb_accessor jwt kaminari keela knapsack kramdown
        kubeclient lefthook letter_opener_web license_finder licensee listen lockbox logger lograge loofah lookbook
        lru_redux mail marcel memory_profiler mini_magick minitest multi_json nats-pure net-http net-ldap net-ntp
        net-protocol nkf nokogiri oauth2 octokit ohai oj oj-introspect omniauth omniauth-alicloud
        omniauth-atlassian-oauth2 omniauth-auth0 omniauth-azure-activedirectory-v2 omniauth-github
        omniauth-google-oauth2 omniauth-oauth2-generic omniauth-saml omniauth-shibboleth-redux
        omniauth_openid_connect openid_connect openssl opentelemetry-exporter-otlp
        opentelemetry-instrumentation-action_pack opentelemetry-instrumentation-action_view
        opentelemetry-instrumentation-active_job opentelemetry-instrumentation-active_record
        opentelemetry-instrumentation-active_support opentelemetry-instrumentation-aws_sdk
        opentelemetry-instrumentation-concurrent_ruby opentelemetry-instrumentation-ethon
        opentelemetry-instrumentation-excon opentelemetry-instrumentation-faraday
        opentelemetry-instrumentation-grape opentelemetry-instrumentation-graphql opentelemetry-instrumentation-http
        opentelemetry-instrumentation-http_client opentelemetry-instrumentation-net_http
        opentelemetry-instrumentation-pg opentelemetry-instrumentation-rack opentelemetry-instrumentation-rails
        opentelemetry-instrumentation-rake opentelemetry-instrumentation-redis opentelemetry-instrumentation-sidekiq
        opentelemetry-sdk org-ruby os pact paper_trail parallel parser parslet peek pg pg_query png_quantizator
        prawn prawn-svg premailer-rails prometheus-client-mmap pry-byebug pry-rails pry-shell puma rack rack-attack
        rack-cors rack-oauth2 rack-proxy rack-timeout rails rails-controller-testing rails-i18n rainbow rbtrace re2
        recaptcha redis redis-actionpack redis-client redis-cluster-client redis-clustering request_store
        resolv-replace responders retriable rexml rouge rqrcode rspec-benchmark rspec-parameterized rspec-rails
        rspec_junit_formatter rspec_profiling rubocop ruby-lsp ruby-lsp-rails ruby-lsp-rspec ruby-magic
        ruby-progressbar ruby-saml rubyzip sanitize sd_notify seed-fu selenium-webdriver semver_dialects
        sentry-rails sentry-ruby sentry-sidekiq shoulda-matchers sidekiq-cron sigdump simple_po_parser simplecov
        simplecov-cobertura simplecov-lcov slack-messenger snowplow-tracker solargraph solargraph-rspec spamcheck
        spring spring-commands-rspec sprite-factory sprockets sprockets-rails ssh_data stackprof
        state_machines-activerecord state_machines-rspec sys-filesystem tanuki_emoji telesignenterprise terser
        test-prof test_file_finder thrift timfel-krb5-auth toml-rb truncato tty-prompt typhoeus undercover
        unicode-emoji uri valid_email validates_hostname version_sorter view_component vite_rails vite_ruby vmstat
        warning webauthn webmock webrick wikicloth yajl-ruby yard zeitwerk zlib zstd-ruby""".split()),
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
            # `--no-index` строки опций снимает индексы всего файла — до и после себя, но не `-f`; в строке требования
            # и в строке `-r` pip опций не применяет (handle_line, _parse_and_recurse).
            "--no-index\n--extra-index-url https://evil/simple\n": [],
            "-i https://evil/simple\n--no-index\n": [],
            "-f https://evil/links\n--no-index\n": ["index https://evil/links"],
            "django --no-index\n-i https://evil/simple\n": ["django", "index https://evil/simple"],
            "-r x.txt --no-index\n-i https://evil/simple\n": [INC("x.txt"), "index https://evil/simple"],
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


class RequirementsEncodingTest(unittest.TestCase):
    """Файл требований декодируется, как у pip 26.2 (req_file._decode_req_file): BOM UTF-8, UTF-16, UTF-32, объявление
    PEP 263 в строке на `#` из первых двух, UTF-8, кодировка локали; имена — ещё и текста в UTF-8, как его читают
    setuptools и hatch-requirements-txt. Образец формы — `pip freeze > requirements.txt` в Windows PowerShell 5:
    UTF-16 LE с BOM."""

    CASES = {
        "utf-16": ("requests\nevil-pkg==1.0\n".encode("utf-16"), {"requests", "evil-pkg"}),
        "utf-16-be": (codecs.BOM_UTF16_BE + "evil\n".encode("utf-16-be"), {"evil"}),
        "utf-32": ("evil\n".encode("utf-32"), {"evil"}),
        "utf-8-sig": ("evil\n".encode("utf-8-sig"), {"evil"}),
        "utf-7": (b"# -*- coding: utf-7 -*-\n+AGUAdgBpAGw-\n", {"evil"}),
        "second line": (b"\n# vim: set fileencoding=utf-7 :\n+AGUAdgBpAGw-\n", {"evil"}),
        # Объявление в третьей строке или с отступом pip не читает.
        "third line": (b"\n\n# coding: utf-7\n+AGUAdgBpAGw-\n", set()),
        "indented": (b"  # coding: utf-7\n+AGUAdgBpAGw-\n", set()),
        "latin-1": ("# coding: latin-1\nevil # caf\xe9\n".encode("latin-1"), {"evil"}),
        "unknown codec": (b"# coding: nosuch\nevil\n", {"evil"}),
        # pip читает UTF-16 LE, setuptools — UTF-8.
        "utf-16-le declared": (b"# coding: utf-16-le\nevil\n\n", {"evil"}),
    }

    def test_decode_as_pip(self):
        for name, (data, expected) in self.CASES.items():
            with self.subTest(name):
                text = manifests.decode("requirements", data)
                self.assertEqual(manifests.names("requirements", text), frozenset(expected))
                self.assertEqual(manifests.known_names("requirements", text), frozenset(expected))

    def test_locale_fallback(self):
        # Не UTF-8 без объявления — кодировка локали (utils.compat.get_locale_encoding).
        data = "evil # \u0441\n".encode("cp1251")
        with mock.patch("locale.getencoding", return_value="cp1251"):
            self.assertEqual(manifests.decode("requirements", data), "evil # \u0441\n")
        self.assertEqual(manifests.names("requirements", manifests.decode("requirements", data)), {"evil"})

    def test_other_kinds_utf8(self):
        # npm, uv, go отвергают UTF-16; BOM UTF-8 npm и uv принимают (npm 11.16, uv 0.12): разбор его снимает.
        data = '{"dependencies": {"evil": "1"}}'.encode("utf-16")
        self.assertIsNone(manifests.names("package.json", manifests.decode("package.json", data)))
        data = '{"dependencies": {"evil": "1"}}'.encode("utf-8-sig")
        self.assertEqual(manifests.names("package.json", manifests.decode("package.json", data)), {"evil"})

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.path = os.path.join(self.root, "requirements.txt")

    def write(self, data):
        with open(self.path, "wb") as f:
            f.write(data)

    def check(self, tool, tool_input):
        return manifest_watch.check_edit(tool, tool_input, self.path, "requirements", root=lambda: self.root)

    def test_command(self):
        self.write(b"requests\n")
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        self.write("requests\nevil-pkg==1.0\n".encode("utf-16"))
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": ["evil-pkg"]}, [], None))

    def test_edit(self):
        # Edit файла в UTF-16 ложится на текст pip; Write с объявлением PEP 263 pip прочтёт в объявленной кодировке.
        self.write("requests\n".encode("utf-16"))
        self.assertEqual(self.check("Edit", {"old_string": "requests", "new_string": "requests\nevil-pkg"}),
                         ["evil-pkg"])
        self.assertEqual(self.check("Write", {"content": "# -*- coding: utf-7 -*-\nrequests\n+AGUAdgBpAGw-\n"}),
                         ["evil"])
        self.write("# coding: latin-1\nrequests # caf\xe9\n".encode("latin-1"))
        for old in ("caf\xe9", "caf\ufffd"):
            with self.subTest(old):
                self.assertEqual(self.check("Edit", {"old_string": old, "new_string": "x\nevil"}), ["evil"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_versions(self):
        # Версия HEAD и дерево HEAD декодируются так же: прежнее имя файла в UTF-16 не новое.
        _git("init", "-q", cwd=self.root)
        self.write("requests\nflask\n".encode("utf-16"))
        _git("add", ".", cwd=self.root)
        _git("commit", "-qm", "i", cwd=self.root)
        self.assertEqual(manifest_watch.head_names(self.path, "requirements", 30, self.root), {"requests", "flask"})
        self.assertIn("flask", manifest_watch.project_names(self.root, "requirements", time.monotonic() + 30))
        self.write("requests\n".encode("utf-16"))
        self.assertEqual(self.check("Write", {"content": "requests\nflask\n"}), [])


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


    def test_statement_over_lines(self):
        # Оператор продолжается после строки, которая кончается `,`, `\\`, открытой скобкой, `=>` или `ключ:`, через
        # пустые строки и строки-комментарии: опции гема на строке-продолжении — его опции. Формы сверены с Ruby 3.4
        # (вычисление заглушкой DSL); образец — gemfile-gitlab.
        cases = {
            'gem "rails",\n  git: "https://evil/a"\n': {"rails @ git+https://evil/a"},
            'gem "b",\n\n  # comment\n  git: "https://evil/b"\n': {"b @ git+https://evil/b"},
            'gem "c", git:\n  "https://evil/c"\n': {"c @ git+https://evil/c"},
            'gem "d", "git" =>\n  "https://evil/d"\n': {"d @ git+https://evil/d"},
            'gem "f", \\\n  git: "https://evil/f"\n': {"f @ git+https://evil/f"},
            'gem "g", {\n  git: "https://evil/g" }\n': {"g @ git+https://evil/g"},
            'gem "h", [\n  "1" ].first, git: "https://evil/h"\n': {"h @ git+https://evil/h"},
            'gem(\n  "i",\n  github: "evil/i"\n)\n': {"i @ github:evil/i"},
            'source(\n  "https://evil.example/src"\n)\n': {"index https://evil.example/src"},
            'eval_gemfile(\n  "extra.rb"\n)\n': {INC("extra.rb")},
            # Местный гем: `path:` на строке-продолжении.
            'gem "e",\n  path: "../e"\n': set(),
            'gem(\n  "e",\n  path: "../e"\n)\n': set(),
            'gem "a", require: false\ngem "b",\n  path: "../b"\n': {"a"},
            # Гем с начала строки внутри цепочки — свой оператор: блок `{ … }`, оператор после `;`.
            '[1].each {\n  gem "x",\n    git: "https://evil/x"\n}\ngem "y"\n': {"x @ git+https://evil/x", "y"},
            'gem "a", "~> 1",\n  require: false; gem "b"\n': {"a", "b"},
            'group :development,\n      :test do\n  gem "a"\nend\ngem "b"\n': {"a", "b"},
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(manifests.names("gemfile", text), frozenset(expected))
        old = 'source "https://rubygems.org"\ngem "rails"\n'
        self.assertEqual(_added("Gemfile", old, old.replace('"rails"', '"rails",\n  git: "https://evil.example/r"')),
                         ["rails @ git+https://evil.example/r"])

    def test_linear_on_long_statement(self):
        # Цепочка строк-продолжений до размера манифеста: опция гема ищется двоичным поиском.
        cases = {
            "gems": lambda n: 'gem "a",\n' * n,
            "labels": lambda n: 'gem "a", git:\n' * n,
            "sources": lambda n: "source(\n" * n,
            "path at end": lambda n: 'gem "a",\n' * n + 'path: "x"\n',
        }
        for name, make in cases.items():
            with self.subTest(name):
                small, large = make(4000), make(16000)
                assert_linear(self, lambda: manifests.names("gemfile", small),
                              lambda: manifests.names("gemfile", large))


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
    """Имя пакета манифеста — пакет проекта, только если манифест в HEAD (кроме npm и PyPI) или член npm workspace
    корня (`workspaces` package.json); имя свежего манифеста вне workspace — внешнее. Член uv workspace — не пакет
    проекта по имени: uv ставит его из каталога только по источнику `workspace = true` (UvWorkspaceSourceTest)."""

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

    def test_uv_workspace_member_name_not_project_package(self):
        _write(self.root, "pyproject.toml", '[project]\nname = "app"\n\n[tool.uv.workspace]\nmembers = ["libs/*"]\n'
                                            'exclude = ["libs/skip"]\n')
        _write(self.root, "libs/a/pyproject.toml", '[project]\nname = "Lib_A"\n')
        _write(self.root, "libs/skip/pyproject.toml", '[project]\nname = "skipped"\n')
        _write(self.root, "other/pyproject.toml", '[project]\nname = "other"\n')
        self.assertFalse({"lib-a", "skipped", "other"} & self.names("pyproject"))

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_committed_manifest_name(self):
        # Имя пакета манифеста HEAD — пакет проекта у cargo; у npm и PyPI — нет: вне связи workspace npm и pip ставят
        # пакет с этим именем из реестра.
        _write(self.root, "crates/fake/Cargo.toml", '[package]\nname = "evil-crate"\n')
        _write(self.root, "libs/fake/pyproject.toml", '[project]\nname = "evil-py"\n')
        _git("init", "-q", cwd=self.root)
        _git("add", ".", cwd=self.root)
        _git("commit", "-qm", "i", cwd=self.root)
        self.assertNotIn("evil-pkg", self.names())
        self.assertNotIn("evil-py", self.names("requirements"))
        self.assertIn("evil-crate", self.names("cargo"))
        # Член npm workspace — пакет проекта и в HEAD.
        _write(self.root, "package.json", '{"name": "app", "workspaces": ["tools/*"]}')
        self.assertIn("evil-pkg", self.names())

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_committed_name_new_outside_workspace(self):
        # Имя пакета из HEAD не прикрывает ту же зависимость в package.json, который npm с каталогом не свяжет.
        _git("init", "-q", cwd=self.root)
        _git("add", ".", cwd=self.root)
        _git("commit", "-qm", "i", cwd=self.root)
        dep = '{"name": "app", "dependencies": {"left-pad": "^1", "evil-pkg": "^1"}}'
        for workspaces, expected in [(None, ["evil-pkg"]), ('["other/*"]', ["evil-pkg"]), ('["tools/*"]', [])]:
            with self.subTest(workspaces=workspaces):
                text = dep if workspaces is None else dep.replace('"app",', '"app", "workspaces": %s,' % workspaces)
                head = text.replace(', "evil-pkg": "^1"', "")
                _write(self.root, "package.json", head)
                self.assertEqual(manifest_watch.check_edit(
                    "Write", {"content": text}, os.path.join(self.root, "package.json"), "package.json",
                    lambda: manifest_watch.project_names(self.root, "package.json", time.monotonic() + 30),
                    root=lambda: self.root), expected)
                entry = manifest_watch.take(self.root, time.monotonic() + 30)
                _write(self.root, "package.json", text)
                self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30)[0],
                                 {"package.json": expected} if expected else {})

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


class UvWorkspaceSourceTest(unittest.TestCase):
    """uv ставит члена workspace из каталога только в pyproject.toml workspace с источником `workspace = true`:
    своим или корня, если член не задал источник этого имени сам (docs.astral.sh/uv, «Workspaces»). Проверено
    `uv lock --offline` (uv 0.13.0): член app без источника — запрос mylib к реестру; источник корня — mylib из
    packages/mylib; свой источник члена `path` для mylib заменяет источник корня; pyproject вне `members` и в
    `exclude` — mylib из реестра. Файл требований pip ставит из PyPI."""

    ROOT = ('[project]\nname = "root"\n\n[tool.uv.workspace]\nmembers = ["packages/*"]\nexclude = ["packages/skip"]\n'
            '%s')
    APP = '[project]\nname = "app"\ndependencies = ["mylib"]\n%s'
    WS = '\n[tool.uv.sources]\nmylib = { workspace = true }\n'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        _write(self.root, "pyproject.toml", self.ROOT % "")
        _write(self.root, "packages/mylib/pyproject.toml", '[project]\nname = "mylib"\n')

    def project(self, kind):
        return manifest_watch.project_names(self.root, kind, time.monotonic() + 30)

    def edit(self, rel, kind, content):
        return manifest_watch.check_edit("Write", {"content": content}, os.path.join(self.root, rel), kind,
                                         lambda: self.project(kind), root=lambda: self.root)

    def command(self, rel, content):
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, rel, content)
        try:
            return manifest_watch.compare(entry, time.monotonic() + 30)[0].get(rel, [])
        finally:
            os.remove(os.path.join(self.root, rel))

    def check(self, rel, kind, content, expected):
        with self.subTest(rel=rel, content=content):
            self.assertEqual(self.edit(rel, kind, content), expected)
            self.assertEqual(self.command(rel, content), expected)

    def test_member_without_source_is_external(self):
        self.check("packages/app/pyproject.toml", "pyproject", self.APP % "", ["mylib"])
        self.check("requirements.txt", "requirements", "mylib\n", ["mylib"])
        self.check("tools/x/pyproject.toml", "pyproject", self.APP % "", ["mylib"])

    def test_own_source(self):
        self.check("packages/app/pyproject.toml", "pyproject", self.APP % self.WS, [])

    def test_root_source_inherited_by_member(self):
        _write(self.root, "pyproject.toml", self.ROOT % self.WS)
        self.check("packages/app/pyproject.toml", "pyproject", self.APP % "", [])
        # Свой источник члена с другим именем источник корня не отменяет.
        self.check("packages/app/pyproject.toml", "pyproject",
                   self.APP % '\n[tool.uv.sources]\nother = { path = "../other" }\n', [])
        # Свой источник того же имени заменяет источник корня.
        self.check("packages/app/pyproject.toml", "pyproject",
                   self.APP % '\n[tool.uv.sources]\nmylib = { index = "evil" }\n', ["mylib @ index:evil"])
        # Не член (вне `members`, в `exclude`) и файл требований источник корня не наследуют.
        self.check("tools/x/pyproject.toml", "pyproject", self.APP % "", ["mylib"])
        self.check("packages/skip/pyproject.toml", "pyproject", self.APP % "", ["mylib"])
        self.check("requirements.txt", "requirements", "mylib\n", ["mylib"])


class NpmWorkspaceLinkTest(unittest.TestCase):
    """Зависимость на члена npm workspace npm связывает с каталогом члена в корневом package.json всегда, в члене —
    если версия члена подходит к диапазону, иначе ставит пакет из реестра (@npmcli/arborist 9.7: Node.#loadDepType,
    buildIdealTree #nodeFromSpec, dep-valid.js): такое имя — `имя @ registry`, имя члена его не покрывает. Проверено
    `npm install --dry-run --offline` (npm 11.16): член b с `"leftpadx": "^2.0.0"` при члене leftpadx 1.2.0 и с
    `latest` — запрос к реестру (ENOTCACHED), с `^1.0.0` — связь; корень с `^2.0.0` — связь."""

    DEP = '{"name": "b", "version": "1.0.0", "dependencies": {"leftpadx": "%s"}}'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        _write(self.root, "package.json", '{"name": "root", "workspaces": ["packages/*"]}')
        _write(self.root, "packages/leftpadx/package.json", '{"name": "leftpadx", "version": "1.2.0"}')
        _write(self.root, "packages/b/package.json", '{"name": "b", "version": "1.0.0"}')

    def edit(self, rel, content):
        return manifest_watch.check_edit(
            "Write", {"content": content}, os.path.join(self.root, rel), "package.json",
            lambda: manifest_watch.project_names(self.root, "package.json", time.monotonic() + 30),
            root=lambda: self.root)

    def test_satisfies_as_node_semver(self):
        # Ожидания сняты с semver 7.8.1 и npm-package-arg npm 11.16 по правилу dep-valid.js; формы вне
        # поддержанных (`>=`, `||`, пробел после `v`, пререлиз, `v` и нули в версии члена) — не подходят: отказ
        # дешевле. Края спецификатора npm-package-arg обрезает, пробел после `^`, `~`, `~>`, `=` убирает node-semver;
        # `\x1c` и `\u200b` — не пробел JavaScript: npm-package-arg отвергает спецификатор (EINVALIDTAGNAME).
        cases = {
            ("1.2.3", ""): True, ("1.2.3-beta.1", "*"): True, ("1.2.3", "x"): True, ("1.2.3", "1"): True,
            ("1.2.3", "1.2"): True, ("1.2.3", "1.2.x"): True, ("1.2.3", "=v1.2.3"): True, ("1.2.3", "1.2.3"): True,
            ("1.2.4", "1.2.3"): False, ("1.9.9", "^1.2"): True, ("2.0.0", "^1.2.3"): False,
            ("1.2.3+build.1", "^1.2.3"): True, ("0.1.5", "^0.1"): True, ("0.2.0", "^0.1.5"): False,
            ("0.0.3", "^0.0.3"): True, ("0.0.4", "^0.0.3"): False, ("0.0.4", "^0.0"): True, ("0.1.0", "^0.0.x"): False,
            ("0.9.0", "^0"): True, ("1.0.0", "^0.x"): False, ("1.3.0", "~1.2.3"): False, ("1.2.9", "~>1.2.3"): True,
            ("1.9.0", "~1"): True, ("1.2.3", "^v1.2.0"): True, ("10.0.0", "1.x.3"): False, ("1.2.3", "^01.2.3"): True,
            ("1.2.3", "latest"): False, ("1.2.3", ">=1.0.0"): False, ("1.2.3", "1.2.3 || 2.0.0"): False,
            ("1.2.3", " ^1.2.3"): True, ("1.2.3", "^1.2.3\n"): True, ("1.2.3", "\u3000^ 1.2.3 "): True,
            ("1.2.3", "^ 1.2.3"): True, ("1.3.0", "~ 1.2.3"): False, ("1.2.9", "~> 1.2.3"): True,
            ("1.2.3", "= 1.2.3"): True, ("1.2.3", " * "): True, ("1.2.3", " "): True, ("1.2.3", "^ \t1"): True,
            ("1.2.3", "\x1c^1.2.3"): False, ("1.2.3", "v 1.2.3"): False, ("1.2.3", "\u200b^1.2.3"): False,
            ("1.2.3-beta.1", "1.2.3-beta.1"): False, ("v1.2.3", "^1"): False,
            ("1.2", "*"): True, ("1.2", "^1"): False, (None, "^1"): False, (None, "*"): True,
            ("9007199254740993.0.0", "^9007199254740993.0.0"): False, ("9007199254740993.0.0", "x"): False,
        }
        for (version, spec), expected in cases.items():
            with self.subTest(version=version, spec=spec):
                self.assertIs(manifests._npm_satisfies(version, spec), expected)

    def test_names_by_place(self):
        members = {"leftpadx": "1.2.0"}
        for place, spec, expected in [
            ("root", "^2.0.0", set()), ("root", "npm:leftpadx@^9", set()),
            ("member", "^1.0.0", set()), ("member", "*", set()), ("member", "^2.0.0", {"leftpadx @ registry"}),
            ("member", "latest", {"leftpadx @ registry"}), ("member", "npm:leftpadx@^1", {"leftpadx @ registry"}),
            ("member", "file:../leftpadx", set()),
            ("member", "github:evil/leftpadx", {"leftpadx @ github:evil/leftpadx"}),
            (None, "^1.0.0", {"leftpadx @ registry"}),
        ]:
            with self.subTest(place=place, spec=spec):
                text = self.DEP % spec
                self.assertEqual(manifests.names("package.json", text, manifests.NpmWorkspace(members, place, members)),
                                 frozenset(expected))
        # Псевдоним другого ключа и overrides ставят член из реестра и в корне.
        text = '{"dependencies": {"y": "npm:leftpadx@^1"}, "overrides": {"z": "npm:leftpadx@1"}}'
        self.assertEqual(manifests.names("package.json", text, manifests.NpmWorkspace(members, "root", members)),
                         {"leftpadx @ registry"})
        self.assertEqual(manifests.names("package.json", self.DEP % "^2.0.0"), {"leftpadx"})

    def test_edit(self):
        root = '{"name": "root", "workspaces": ["packages/*"], "dependencies": {"leftpadx": "%s"}}'
        for rel, content, expected in [
            ("packages/b/package.json", self.DEP % "^2.0.0", ["leftpadx @ registry"]),
            ("packages/b/package.json", self.DEP % "latest", ["leftpadx @ registry"]),
            ("packages/b/package.json", self.DEP % "^1.0.0", []),
            ("packages/b/package.json", self.DEP % "*", []),
            ("package.json", root % "^2.0.0", []),
            ("tools/x/package.json", self.DEP % "^1.0.0", ["leftpadx @ registry"]),
        ]:
            with self.subTest(rel=rel, content=content):
                self.assertEqual(self.edit(rel, content), expected)
        # Уже взятый из реестра член — не новое имя; связанный — не прикрывает реестр в другом манифесте.
        _write(self.root, "packages/b/package.json", self.DEP % "^2.0.0")
        self.assertEqual(self.edit("packages/b/package.json", (self.DEP % "^2.0.0").replace("1.0.0", "1.0.1")), [])
        _write(self.root, "packages/b/package.json", self.DEP % "^1.0.0")
        _write(self.root, "packages/c/package.json", '{"name": "c"}')
        self.assertEqual(self.edit("packages/c/package.json", self.DEP.replace('"b"', '"c"') % "^2.0.0"),
                         ["leftpadx @ registry"])

    def test_edit_moves_dependents_to_registry(self):
        # Правка версии члена или шаблонов корня переводит зависимость другого члена со связи на реестр.
        _write(self.root, "packages/b/package.json", self.DEP % "^1.0.0")
        self.assertEqual(self.edit("packages/leftpadx/package.json", '{"name": "leftpadx", "version": "1.3.0"}'), [])
        self.assertEqual(self.edit("packages/leftpadx/package.json", '{"name": "leftpadx", "version": "2.0.0"}'),
                         ["leftpadx @ registry"])
        self.assertEqual(self.edit("package.json", '{"name": "root", "workspaces": ["packages/b"]}'),
                         ["leftpadx @ registry"])
        self.assertEqual(self.edit("package.json", '{"name": "root"}'), ["leftpadx @ registry"])

    def test_compare(self):
        entry = json.loads(json.dumps(manifest_watch.take(self.root, time.monotonic() + 30)))
        _write(self.root, "packages/b/package.json", self.DEP % "^1.0.0")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30), ({}, [], None))
        _write(self.root, "packages/b/package.json", self.DEP % "^2.0.0")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"packages/b/package.json": ["leftpadx @ registry"]}, [], None))
        # Версия члена меняется командой: зависимость неизменённого b уходит на реестр.
        _write(self.root, "packages/b/package.json", self.DEP % "^1.0.0")
        entry = json.loads(json.dumps(manifest_watch.take(self.root, time.monotonic() + 30)))
        self.assertEqual(entry["npm"], {"": {"leftpadx": "1.2.0", "b": "1.0.0"}})
        _write(self.root, "packages/leftpadx/package.json", '{"name": "leftpadx", "version": "2.0.0"}')
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"packages/b/package.json": ["leftpadx @ registry"]}, [], None))
        _write(self.root, "packages/leftpadx/package.json", '{"name": "leftpadx", "version": "1.2.0"}')
        _write(self.root, "package.json", '{"name": "root"}')
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"packages/b/package.json": ["leftpadx @ registry"]}, [], None))

    def commit(self):
        _git("init", "-q", cwd=self.root)
        _git("add", ".", cwd=self.root)
        _git("commit", "-qm", "i", cwd=self.root)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_restored_member_text_not_new(self):
        # Версия HEAD разбирается с npm workspace HEAD: возврат закоммиченного текста члена — командой или Write — не
        # новое имя.
        registry = self.DEP % "^2.0.0"
        _write(self.root, "packages/b/package.json", registry)
        self.commit()
        _write(self.root, "packages/b/package.json", self.DEP % "^1.0.0")
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _git("checkout", "--", "packages/b/package.json", cwd=self.root)
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30), ({}, [], None))
        _write(self.root, "packages/b/package.json", self.DEP % "^1.0.0")
        self.assertEqual(self.edit("packages/b/package.json", registry), [])
        self.assertIn("leftpadx @ registry",
                      manifest_watch.project_names(self.root, "package.json", time.monotonic() + 30))

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_head_workspace_not_working_tree(self):
        # В HEAD зависимость b связана с членом 2.0.0; команда, сменившая версию члена на 1.2.0, переводит её на
        # реестр, и версия HEAD, разобранная с членами HEAD, а не рабочего дерева, это имя не прикрывает.
        _write(self.root, "packages/leftpadx/package.json", '{"name": "leftpadx", "version": "2.0.0"}')
        _write(self.root, "packages/b/package.json", self.DEP % "^2.0.0")
        self.commit()
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "packages/leftpadx/package.json", '{"name": "leftpadx", "version": "1.2.0"}')
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"packages/b/package.json": ["leftpadx @ registry"]}, [], None))
        self.assertEqual(manifest_watch.head_names(os.path.join(self.root, "packages/b/package.json"),
                                                   "package.json", 30, self.root), frozenset())

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_ref_and_index_versions_use_their_workspace(self):
        # Дерево ref команды и сторона конфликта индекса разбираются с членами своей версии.
        self.commit()
        _git("checkout", "-qb", "feat", cwd=self.root)
        _write(self.root, "packages/b/package.json", self.DEP % "^2.0.0")
        _git("commit", "-qam", "feat", cwd=self.root)
        _git("checkout", "-q", "-", cwd=self.root)
        known = manifest_watch.restored_names(self.root, "git checkout feat -- packages/b/package.json",
                                              int(time.time()) + 100, time.monotonic() + 30)
        self.assertIn("leftpadx @ registry", known["npm"])
        self.assertNotIn("leftpadx", known["npm"])
        # Сторона :3: файла b — с зависимостью на реестр; корень и член — стадии 0.
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.root, input=self.DEP % "^2.0.0",
                              check=True, capture_output=True, text=True).stdout.strip()
        _git("rm", "-q", "--cached", "packages/b/package.json", cwd=self.root)
        subprocess.run(["git", "update-index", "--index-info"], cwd=self.root, check=True, capture_output=True,
                       input=f"100644 {blob} 3\tpackages/b/package.json\n", text=True)
        self.assertEqual(manifest_watch.head_names(os.path.join(self.root, "packages/b/package.json"),
                                                   "package.json", 30, self.root), {"leftpadx @ registry"})


class NestedWorkspaceTest(unittest.TestCase):
    """Корень workspace в подкаталоге проекта: npm ищет его из каталога члена ближайшим предком, чей package.json с
    `workspaces` включает каталог члена (@npmcli/config loadLocalPrefix), uv — первым предком с pyproject.toml, если
    его `[tool.uv.workspace]` включает член (uv-workspace find_workspace). Проверено `npm install --offline` (npm
    11.16: член web/packages/app связан с web/packages/ui) и `uv lock --offline -v` (uv 0.13.0: из каталога члена под
    промежуточным pyproject.toml с `[project]`, без него, с workspace без члена — «No workspace root found»)."""

    APP = '{"name": "@acme/app", "version": "1.0.0", "dependencies": {"@acme/ui": "%s"}}'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        _write(self.root, "web/package.json", '{"name": "web", "private": true, "workspaces": ["packages/*"]}')
        _write(self.root, "web/packages/ui/package.json", '{"name": "@acme/ui", "version": "1.0.0"}')
        _write(self.root, "web/packages/app/package.json", '{"name": "@acme/app", "version": "1.0.0"}')

    def check(self, rel, kind, content, expected):
        with self.subTest(rel=rel, content=content):
            path = os.path.join(self.root, rel)
            self.assertEqual(manifest_watch.check_edit(
                "Write", {"content": content}, path, kind,
                lambda: manifest_watch.project_names(self.root, kind, time.monotonic() + 30),
                root=lambda: self.root), expected)
            saved = manifest_watch._text(path, kind)
            entry = manifest_watch.take(self.root, time.monotonic() + 30)
            _write(self.root, rel, content)
            try:
                self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30)[0].get(rel, []), expected)
            finally:
                if saved is None:
                    os.remove(path)
                else:
                    _write(self.root, rel, saved)

    def test_npm_member_of_nested_root(self):
        self.check("web/packages/app/package.json", "package.json", self.APP % "^1.0.0", [])
        self.check("web/packages/app/package.json", "package.json", self.APP % "^2.0.0", ["@acme/ui @ registry"])
        # Корень web связывает члена в своём package.json; package.json вне workspace web берёт имя из реестра.
        self.check("web/package.json", "package.json",
                   '{"name": "web", "workspaces": ["packages/*"], "dependencies": {"@acme/ui": "^2"}}', [])
        self.check("api/package.json", "package.json", self.APP.replace("app", "api") % "^1.0.0",
                   ["@acme/ui @ registry"])
        self.check("package.json", "package.json", self.APP.replace("app", "top") % "^1.0.0", ["@acme/ui @ registry"])

    def test_npm_skips_ancestor_without_member(self):
        # Предок с package.json без `workspaces` или с шаблонами без члена npm пропускает и ищет корень выше.
        _write(self.root, "web/packages/package.json", '{"name": "mid", "workspaces": ["other/*"]}')
        self.check("web/packages/app/package.json", "package.json", self.APP % "^1.0.0", [])
        # Ближний корень, который включает члена, решает: член другого корня — реестр.
        _write(self.root, "web/packages/package.json", '{"name": "mid", "workspaces": ["app"]}')
        self.check("web/packages/app/package.json", "package.json", self.APP % "^1.0.0", ["@acme/ui @ registry"])

    def test_npm_member_with_own_workspaces(self):
        # Член верхнего корня со своими `workspaces` — член: npm поднимается к корню, чей `workspaces` включает
        # каталог (loadLocalPrefix), и `workspaces` члена не читает. Проверено `npm install --offline` (npm 11.16):
        # сосед lib связан, @acme/ui из вложенных `workspaces` web — запрос в реестр (ENOTCACHED).
        _write(self.root, "package.json", '{"name": "top", "workspaces": ["web", "lib"]}')
        _write(self.root, "lib/package.json", '{"name": "lib", "version": "1.0.0"}')
        web = '{"name": "web", "version": "1.0.0", "workspaces": ["packages/*"], "dependencies": {"%s": "^1.0.0"}}'
        self.check("web/package.json", "package.json", web % "lib", [])
        self.check("web/package.json", "package.json", web % "@acme/ui", ["@acme/ui @ registry"])

    def test_npm_member_without_name(self):
        # Член без `name` npm называет по каталогу, под каталогом `@scope` — `@scope/каталог` (@npmcli/map-workspaces
        # getPackageName, @npmcli/name-from-folder).
        _write(self.root, "web/package.json", '{"name": "web", "workspaces": ["packages/*", "packages/@acme/*"]}')
        _write(self.root, "web/packages/ui/package.json", '{"version": "1.0.0"}')
        _write(self.root, "web/packages/@acme/kit/package.json", '{"version": "1.0.0"}')
        app = '{"name": "@acme/app", "version": "1.0.0", "dependencies": {"%s": "%s"}}'
        self.check("web/packages/app/package.json", "package.json", app % ("ui", "^1.0.0"), [])
        self.check("web/packages/app/package.json", "package.json", app % ("@acme/kit", "^1.0.0"), [])
        self.check("web/packages/app/package.json", "package.json", app % ("ui", "^2.0.0"), ["ui @ registry"])
        # Корень сужает `workspaces`: безымянный ui больше не член, зависимость app на него — из реестра.
        _write(self.root, "web/packages/app/package.json", app % ("ui", "^1.0.0"))
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "web/package.json", '{"name": "web", "workspaces": ["packages/app"]}')
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30)[0],
                         {"web/packages/app/package.json": ["ui @ registry"]})

    def test_npm_root_change_moves_dependents(self):
        # Правка шаблонов вложенного корня переводит зависимость члена со связи на реестр.
        _write(self.root, "web/packages/app/package.json", self.APP % "^1.0.0")
        narrowed = '{"name": "web", "workspaces": ["packages/app"]}'
        self.assertEqual(manifest_watch.check_edit(
            "Write", {"content": narrowed}, os.path.join(self.root, "web/package.json"), "package.json",
            lambda: manifest_watch.project_names(self.root, "package.json", time.monotonic() + 30),
            root=lambda: self.root), ["@acme/ui @ registry"])
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "web/package.json", narrowed)
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30)[0],
                         {"web/packages/app/package.json": ["@acme/ui @ registry"]})

    def test_uv_member_of_nested_root(self):
        _write(self.root, "py/pyproject.toml", '[project]\nname = "root"\n\n[tool.uv.workspace]\n'
                                               'members = ["packages/*", "mid/app"]\n\n[tool.uv.sources]\n'
                                               'alib = { workspace = true }\n')
        _write(self.root, "py/packages/alib/pyproject.toml", '[project]\nname = "alib"\n')
        app = '[project]\nname = "app"\ndependencies = ["alib"]\n'
        self.check("py/packages/app/pyproject.toml", "pyproject", app, [])
        self.check("py/mid/app/pyproject.toml", "pyproject", app, [])
        # Первый предок с pyproject.toml без workspace, включающего член, решает: не член, alib — из реестра.
        for mid in ('[project]\nname = "mid"\n', '[tool.black]\nline-length = 1\n',
                    '[project]\nname = "mid"\n\n[tool.uv.workspace]\nmembers = ["other"]\n'):
            _write(self.root, "py/mid/pyproject.toml", mid)
            self.check("py/mid/app/pyproject.toml", "pyproject", app, ["alib"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_head_version_uses_nested_root(self):
        _write(self.root, "web/packages/app/package.json", self.APP % "^2.0.0")
        _git("init", "-q", cwd=self.root)
        _git("add", ".", cwd=self.root)
        _git("commit", "-qm", "i", cwd=self.root)
        self.assertEqual(manifest_watch.head_names(os.path.join(self.root, "web/packages/app/package.json"),
                                                   "package.json", 30, self.root), {"@acme/ui @ registry"})
        _write(self.root, "web/packages/app/package.json", self.APP % "^1.0.0")
        self.check("web/packages/app/package.json", "package.json", self.APP % "^2.0.0", [])


@unittest.skipUnless(shutil.which("git"), "нет git")
class VersionNpmOnceTest(unittest.TestCase):
    """Сравнение после команды читает дерево версии HEAD для npm workspace один раз на все изменённые package.json, а
    не на каждый: иначе 500 членов или 30 членов в репозитории на 100 000 файлов не укладываются в CHECK_BUDGET."""

    def test_tree_read_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        _write(root, "package.json", '{"name": "root", "workspaces": ["packages/*"]}')
        for i in range(20):
            _write(root, f"packages/m{i}/package.json", '{"name": "m%d", "version": "1.0.0"}' % i)
        _git("init", "-q", cwd=root)
        _git("add", ".", cwd=root)
        _git("commit", "-qm", "i", cwd=root)
        entry = manifest_watch.take(root, time.monotonic() + 30)
        for i in range(20):
            _write(root, f"packages/m{i}/package.json",
                   '{"name": "m%d", "version": "1.0.0", "dependencies": {"left-pad": "1"}}' % i)
        calls = []
        git = manifest_watch._git

        def counted(cwd, timeout, *args, **kwargs):
            calls.append(args[0])
            return git(cwd, timeout, *args, **kwargs)
        with mock.patch.object(manifest_watch, "_git", counted):
            added = manifest_watch.compare(entry, time.monotonic() + 30)[0]
        self.assertEqual(added, {f"packages/m{i}/package.json": ["left-pad"] for i in range(20)})
        # Одно ls-tree дерева HEAD, общее у npm workspace и _tree_names.
        self.assertEqual(calls.count("ls-tree"), 1, calls)


class WorkspaceGlobsOnceTest(unittest.TestCase):
    """package.json корня workspace разбирается один раз на снимок, а не на каждого члена: корень на 300 КБ и 900
    членов иначе не укладываются в SNAPSHOT_BUDGET."""

    def test_root_parsed_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        _write(root, "package.json", '{"name": "root", "workspaces": ["packages/*"]}')
        for i in range(20):
            _write(root, f"packages/m{i}/package.json", '{"name": "m%d", "version": "1.0.0"}' % i)
        manifest_watch._globs.cache_clear()
        with mock.patch.object(manifests, "workspace_members", wraps=manifests.workspace_members) as parsed:
            entry = manifest_watch.take(root, time.monotonic() + 30)
        self.assertEqual(len(entry["npm"][""]), 20)
        self.assertEqual(parsed.call_count, 1, parsed.call_args_list)


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

    def test_include_read_by_includer_kind(self):
        # pip читает файл `-r`/`-c` как требования под любым именем (pip 26.2, req_file._parse_and_recurse), Bundler
        # `eval_gemfile` — как Gemfile: манифест другого вида проверяется по своему виду, его подключение — новое имя.
        _write(self.root, "Gemfile", "")
        self.assertEqual(self.added("Gemfile", "evil-pkg==1.0\n"), [])
        _write(self.root, "Gemfile", "evil-pkg==1.0\n")
        cases = [
            ("requirements.txt", "requests\n-r Gemfile\n", [INC("Gemfile"), "requests"]),
            ("requirements.txt", "-c engines/Gemfile\n", [INC("engines/Gemfile")]),
            ("Gemfile", 'eval_gemfile "requirements.txt"\n', [INC("requirements.txt")]),
            ("engines/Gemfile", 'eval_gemfile "../Gemfile"\n', []),
        ]
        for rel, content, expected in cases:
            with self.subTest(rel=rel, content=content):
                self.assertEqual(self.added(rel, content), expected)
        entry = manifest_watch.take(self.root, time.monotonic() + 30)
        _write(self.root, "requirements.txt", "-r Gemfile\n")
        self.assertEqual(manifest_watch.compare(entry, time.monotonic() + 30),
                         ({"requirements.txt": [INC("Gemfile")]}, [], None))

    def test_pyproject_dynamic_files(self):
        # Файл зависимостей `dynamic` — подключение требований: setuptools 84 (`file` в `[tool.setuptools.dynamic]`,
        # строка или список) и hatch-requirements-txt 0.4.1 (`files`, `filename`, без них requirements.txt; `filename`
        # не строкой — TypeError load_requirements_files, файла нет); поле не в `project.dynamic` бэкенд не читает.
        head = '[project]\nname = "app"\ndynamic = %s\n'
        cases = [
            ('["dependencies"]', '[tool.setuptools.dynamic]\ndependencies = {file = ["deps.list"]}\n',
             [INC("deps.list")]),
            ('["dependencies"]', '[tool.setuptools.dynamic]\ndependencies = {file = "deps.list"}\n',
             [INC("deps.list")]),
            ('["dependencies"]', '[tool.setuptools.dynamic]\ndependencies = {file = ["requirements-dev.txt"]}\n', []),
            ('["dependencies"]', '[tool.setuptools.dynamic]\ndependencies = {file = ["engines/Gemfile"]}\n',
             [INC("engines/Gemfile")]),
            ('["version"]', '[tool.setuptools.dynamic]\ndependencies = {file = ["deps.list"]}\n', []),
            ('["optional-dependencies"]', '[tool.setuptools.dynamic.optional-dependencies]\n'
             'dev = {file = ["dev.list"]}\n', [INC("dev.list")]),
            ('["dependencies"]', '[tool.hatch.metadata.hooks.requirements_txt]\nfiles = ["deps.list"]\n',
             [INC("deps.list")]),
            ('["dependencies"]', '[tool.hatch.metadata.hooks.requirements_txt]\nfilename = "deps.list"\n',
             [INC("deps.list")]),
            ('["dependencies"]', '[tool.hatch.metadata.hooks.requirements_txt]\nfilename = ["deps.list"]\n', []),
            ('["dependencies"]', '[tool.hatch.metadata.hooks.requirements_txt]\n', []),
            ('["optional-dependencies"]', '[tool.hatch.metadata.hooks.requirements_txt]\n'
             'optional-dependencies = {cli = ["cli.list"]}\n', [INC("cli.list")]),
        ]
        for dynamic, tail, expected in cases:
            with self.subTest(dynamic=dynamic, tail=tail):
                self.assertEqual(self.added("pyproject.toml", head % dynamic + tail), expected)
        # Путь — от каталога pyproject.toml.
        self.assertEqual(self.added("sub/pyproject.toml", head % '["dependencies"]' + '[tool.setuptools.dynamic]\n'
                                    'dependencies = {file = ["../requirements.txt"]}\n'), [])

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
