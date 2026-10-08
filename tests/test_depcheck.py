import pathlib
import sys
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import depcheck  # noqa: E402


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
            'echo "$(echo "$(npm install x)")"',
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
            "node index.js", "npm run build", "npm test", "npm ci", "npm ls", "npm outdated", "npm audit",
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
            "poetry lock", "bundle install", "bundle exec rspec", "composer install", "dotnet build",
            "mix test", "stack build", "cabal build", "opam env", "luarocks list", "Rscript analysis.R",
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
        self.assertEqual(depcheck.dependency_add("git commit -m \"$(cat <<'EOF'\nnpm install x\nEOF\n)\"\nnpm install x"),
                         "npm install x")


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

    def test_uv_cert_value_is_not_package(self):
        self.assertIsNone(depcheck.dependency_add("uv pip install --cert ca -r r.txt"))
        for cmd in ["uv --cert ca pip install -r r.txt", "uv add --cert ca -r r.txt",
                    "uv tool install --cert ca .", "uv run --cert ca script.py"]:
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
            "python -W ignore -m pip install x",
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
        # README: на слова разбираются первые 4096 символов сегмента.
        self.assertEqual(depcheck._WORDS_LIMIT, 4096)
        head = "npm install "
        inside = head + " " * (4096 - len(head) - len("left-pad")) + "left-pad"
        beyond = head + " " * (4096 - len(head)) + "left-pad"
        self.assertIsNotNone(depcheck.dependency_add(inside))
        self.assertIsNone(depcheck.dependency_add(beyond))
