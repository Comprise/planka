import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import snapshot  # noqa: E402


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_scan_relpaths_and_ignored_dirs(self):
        self.write("a.py"); self.write("pkg/b.go"); self.write(".git/HEAD"); self.write("node_modules/x.js")
        self.write("build/out"); self.write(".superpowers/x")
        files = snapshot.scan(self.root)
        self.assertEqual(sorted(files), [".superpowers/x", "a.py", "pkg/b.go"])
        self.assertEqual(len(files["a.py"]), 2)

    def test_gitignore_names_and_exts(self):
        self.write(".gitignore", "secrets/\n*.log\n# comment\nbin\n!keep\nfoo/*.tmp\n")
        self.write("secrets/k"); self.write("run.log"); self.write("bin/tool"); self.write("ok.txt")
        names, exts = snapshot.ignore_rules(self.root)
        self.assertEqual(names, {"secrets", "bin"})
        self.assertEqual(exts, {"log"})
        self.assertEqual(sorted(snapshot.scan(self.root)), [".gitignore", "ok.txt"])

    def test_gitignore_multi_dot_ext(self):
        self.write(".gitignore", "*.min.js\n")
        self.write("a.min.js"); self.write("a.js")
        self.assertEqual(sorted(snapshot.scan(self.root)), [".gitignore", "a.js"])

    def test_nested_gitignore_anchored_name(self):
        self.write("sub/.gitignore", "/cache\n")
        self.write("sub/cache/x"); self.write("cache/y"); self.write("sub/other/z")
        self.assertEqual(sorted(snapshot.scan(self.root)),
                         ["cache/y", "sub/.gitignore", "sub/other/z"])

    def test_nested_gitignore_ext_only_in_own_subtree(self):
        self.write("sub/.gitignore", "*.log\n")
        self.write("sub/a.log"); self.write("sub/deep/b.log"); self.write("a.log")
        self.assertEqual(sorted(snapshot.scan(self.root)), ["a.log", "sub/.gitignore"])

    def test_root_rules_inherited_into_subdirs(self):
        self.write(".gitignore", "*.log\nsecrets\n")
        self.write("sub/.gitignore", "/cache\n")
        self.write("sub/a.log"); self.write("sub/secrets/k"); self.write("sub/cache/x"); self.write("sub/ok")
        self.assertEqual(sorted(snapshot.scan(self.root)), [".gitignore", "sub/.gitignore", "sub/ok"])

    def test_no_gitignore(self):
        self.assertEqual(snapshot.ignore_rules(self.root), (set(), set()))

    def test_symlink_not_followed(self):
        self.write("real/f")
        os.symlink(self.root / "real", self.root / "link")
        self.assertEqual(sorted(snapshot.scan(self.root)), ["real/f"])

    def test_too_many_files_is_none(self):
        old = snapshot.MAX_FILES
        snapshot.MAX_FILES = 2
        try:
            self.write("a"); self.write("b"); self.write("c")
            self.assertIsNone(snapshot.scan(self.root))
        finally:
            snapshot.MAX_FILES = old


@unittest.skipUnless(shutil.which("git"), "нет git")
class GitScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def test_git_rules_are_exact(self):
        self.write(".gitignore", "*.log\n!keep.log\nbuild-*/\n")
        self.write("a.log"); self.write("keep.log"); self.write("build-x/out"); self.write("src/a.py")
        self.write(".git/info/exclude", "local/\n"); self.write("local/x")
        self.write(".superpowers/x")
        self.assertEqual(sorted(snapshot.scan(self.root)), [".gitignore", ".superpowers/x", "keep.log", "src/a.py"])

    def test_tracked_and_untracked_with_odd_names(self):
        self.write("dir/a b.py"); self.write("кириллица.go")
        subprocess.run(["git", "-C", str(self.root), "add", "dir/a b.py"], check=True)
        self.assertEqual(sorted(snapshot.scan(self.root)), ["dir/a b.py", "кириллица.go"])

    def test_deleted_tracked_file_absent(self):
        self.write("gone.py")
        subprocess.run(["git", "-C", str(self.root), "add", "gone.py"], check=True)
        (self.root / "gone.py").unlink()
        self.assertEqual(snapshot.scan(self.root), {})

    def test_too_many_files_is_none(self):
        old = snapshot.MAX_FILES
        snapshot.MAX_FILES = 2
        try:
            self.write("a"); self.write("b"); self.write("c")
            self.assertIsNone(snapshot.scan(self.root))
        finally:
            snapshot.MAX_FILES = old

    def test_submodule_files_included(self):
        lib = pathlib.Path(self.tmp.name + "-lib")
        subprocess.run(["git", "init", "-q", str(lib)], check=True)
        try:
            (lib / "f.py").write_text("x", encoding="utf-8")
            git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
            subprocess.run([*git, "-C", str(lib), "add", "f.py"], check=True)
            subprocess.run([*git, "-C", str(lib), "commit", "-qm", "i"], check=True)
            subprocess.run([*git, "-C", str(self.root), "submodule", "add", "-q", str(lib), "sub"],
                           check=True, capture_output=True)
            (self.root / "sub" / "new.py").write_text("y", encoding="utf-8")
            self.assertEqual(sorted(snapshot.scan(self.root)), [".gitmodules", "sub/f.py", "sub/new.py"])
            subprocess.run([*git, "-C", str(self.root), "commit", "-qm", "s"], check=True)
            clone = pathlib.Path(self.tmp.name + "-clone")
            subprocess.run([*git, "clone", "-q", str(self.root), str(clone)], check=True, capture_output=True)
            try:
                self.assertEqual(sorted(snapshot.scan(clone)), [".gitmodules"])
            finally:
                shutil.rmtree(clone)
        finally:
            shutil.rmtree(lib)


class SaveLoadDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        files = {"a.py": [1, 2], "b/c.go": [3, 4]}
        snapshot.save(self.state, "sess/1", "p-1", self.root, files)
        got = snapshot.load(self.state, "sess/1")
        self.assertEqual(got["prompt_id"], "p-1")
        self.assertEqual(got["root"], str(self.root))
        self.assertEqual(got["files"], files)
        self.assertEqual([p.name for p in self.state.iterdir()], ["sess_1.snap.json"])

    def test_load_missing_or_garbage(self):
        self.assertIsNone(snapshot.load(self.state, "none"))
        (self.state / "bad.snap.json").write_text("{", encoding="utf-8")
        self.assertIsNone(snapshot.load(self.state, "bad"))

    def test_load_non_utf8_garbage(self):
        (self.state / "bin.snap.json").write_bytes(b"\xff\xfe\x00garbage")
        self.assertIsNone(snapshot.load(self.state, "bin"))

    def test_diff(self):
        old = {"a": [1, 1], "b": [1, 1], "c": [1, 1]}
        new = {"a": [1, 1], "b": [2, 1], "d": [1, 1]}
        self.assertEqual(snapshot.diff(old, new), ["b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
