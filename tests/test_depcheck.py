import pathlib
import sys
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "planka"
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
            "go build ./...",
            "go get",
            "git add -A",
            "npm run build",
            "cargo build",
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


class FalseDenyTest(unittest.TestCase):
    def test_not_adds(self):
        for cmd in [
            "pip install -e .[dev]",
            "pip install -e .",
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
