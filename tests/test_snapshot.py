import json
import os
import pathlib
import sys
import tempfile
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "planka"
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
        self.assertEqual(sorted(files), ["a.py", "pkg/b.go"])
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
