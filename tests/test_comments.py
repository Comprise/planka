import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import comments  # noqa: E402


class CommentLinesTest(unittest.TestCase):
    def test_c_family(self):
        src = "x := 1 // trailing\n// line one\n/* block\n   two */\ns := \"// not\"\n"
        self.assertEqual(comments.comment_lines(src, "go"),
                         ["// trailing", "// line one", "/* block", "two */"])

    def test_hash_and_docstring(self):
        src = "#!/usr/bin/env python3\n# note\ndef f():\n    \"\"\"Doc line.\"\"\"\n    s = '#'\n    return 1  # tail\n"
        self.assertEqual(comments.comment_lines(src, "py"),
                         ["# note", '"""Doc line."""', "# tail"])

    def test_shell_hash_expansions_are_code(self):
        src = "echo $#\necho ${#a[@]}\necho $# # c\n"
        for ext in ("sh", "bash", "zsh"):
            self.assertEqual(comments.comment_lines(src, ext), ["# c"])

    def test_sql_and_html(self):
        self.assertEqual(comments.comment_lines("select 1 -- c\n", "sql"), ["-- c"])
        self.assertEqual(comments.comment_lines("<a>\n<!-- hidden -->\n", "html"), ["<!-- hidden -->"])

    def test_unknown_ext_is_empty(self):
        self.assertEqual(comments.comment_lines("// x\n# y\n", "bin"), [])
        self.assertEqual(comments.comment_lines("", "go"), [])


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def test_non_git_whole_files(self):
        self.write("a.go", "// one\nx := 1\n")
        self.write("b.py", "# two\n")
        self.write("c.bin", "// ignored\n")
        lines, truncated = comments.extract(self.root, ["a.go", "b.py", "c.bin", "missing.go"])
        self.assertEqual(lines, ["a.go: // one", "b.py: # two"])
        self.assertFalse(truncated)

    def test_truncation(self):
        self.write("a.py", "".join(f"# line {i}\n" for i in range(500)))
        lines, truncated = comments.extract(self.root, ["a.py"])
        self.assertEqual(len(lines), comments.MAX_LINES)
        self.assertTrue(truncated)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_added_lines_only(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("a.go", "// old\nx := 1\n")
        subprocess.run(["git", "-C", str(self.root), "add", "a.go"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "init"], check=True)
        self.write("a.go", "// old\nx := 1\n// new\n")
        self.write("u.py", "# untracked\n")
        lines, _ = comments.extract(self.root, ["a.go", "u.py"])
        self.assertEqual(lines, ["a.go: // new", "u.py: # untracked"])


if __name__ == "__main__":
    unittest.main()
