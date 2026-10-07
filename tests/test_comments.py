import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
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

    def test_shell_hash_inside_word_is_code(self):
        self.assertEqual(comments.comment_lines("x=${var#prefix}\ny=a#b # c\n", "sh"), ["# c"])

    def test_triple_quoted_string_is_not_docstring(self):
        src = 'q = """SELECT"""\nsql = """\n# not a comment\n"""\n# real\ndef f():\n    r"""Doc."""\n'
        self.assertEqual(comments.comment_lines(src, "py"), ["# real", 'r"""Doc."""'])

    def test_triple_quote_inside_comment(self):
        src = 'x = 1  # see the """ quoting\ny = 2\n# real comment\n'
        self.assertEqual(comments.comment_lines(src, "py"), ['# see the """ quoting', "# real comment"])

    def test_backticks_and_raw_strings_are_strings(self):
        self.assertEqual(comments.comment_lines("const u = `https://e.com/x`; // c\n", "ts"), ["// c"])
        self.assertEqual(comments.comment_lines('let s = r#"a " // not"#; // yes\n', "rs"), ["// yes"])

    def test_yaml_hash_inside_word_is_value(self):
        self.assertEqual(comments.comment_lines("url: http://a/b#frag\nk: 1 # c\n", "yaml"), ["# c"])

    def test_shell_heredoc_body_is_data(self):
        src = "cat <<EOF > s.md\n# Heading\nEOF\n# real\n"
        self.assertEqual(comments.comment_lines(src, "sh"), ["# real"])

    def test_block_comments_lua_haskell(self):
        self.assertEqual(comments.comment_lines("--[[ block\nline two\n]]\nx = 1 -- tail\n", "lua"),
                         ["--[[ block", "line two", "]]", "-- tail"])
        self.assertEqual(comments.comment_lines("{- block\nline -}\nx = 1 -- tail\n", "hs"),
                         ["{- block", "line -}", "-- tail"])

    def test_vue_script_comments(self):
        src = "<template>\n<!-- html -->\n</template>\n<script>\n// js comment\n</script>\n"
        self.assertEqual(comments.comment_lines(src, "vue"), ["<!-- html -->", "// js comment"])

    def test_sql_and_html(self):
        self.assertEqual(comments.comment_lines("select 1 -- c\n", "sql"), ["-- c"])
        self.assertEqual(comments.comment_lines("<a>\n<!-- hidden -->\n", "html"), ["<!-- hidden -->"])

    def test_slash_families(self):
        for ext in ("php", "groovy", "gradle", "proto", "sol", "zig"):
            self.assertEqual(comments.comment_lines("x = 1 // c\n", ext), ["// c"], ext)

    def test_language_variants(self):
        for ext in ("mjs", "cjs", "mts", "cts", "cxx", "hh", "hxx"):
            self.assertEqual(comments.comment_lines("x = 1 // c\n", ext), ["// c"], ext)
        self.assertEqual(comments.comment_lines('# c\ndef f():\n    """Doc."""\n', "pyi"), ["# c", '"""Doc."""'])
        self.assertEqual(comments.comment_lines("set(X 1) # c\n", "cmake"), ["# c"])

    def test_hash_families(self):
        for ext in ("tf", "nix", "r", "jl", "ex", "exs"):
            self.assertEqual(comments.comment_lines("x = 1 # c\n", ext), ["# c"], ext)

    def test_erl_has_no_family(self):
        self.assertEqual(comments.comment_lines("% c\n", "erl"), [])

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

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t",
                        "--literal-pathspecs", *args],
                       check=True, capture_output=True)

    def commit(self, *paths):
        self.git("add", "--", *paths)
        self.git("commit", "-q", "-m", "c")

    def test_non_git_whole_files(self):
        self.write("a.go", "// one\nx := 1\n")
        self.write("b.py", "# two\n")
        self.write("c.bin", "// ignored\n")
        lines, truncated, unknown = comments.extract(self.root, ["a.go", "b.py", "c.bin", "missing.go"])
        self.assertEqual(lines, ["a.go: // one", "b.py: # two"])
        self.assertFalse(truncated)
        self.assertEqual(unknown, ["c.bin"])

    def test_unknown_syntax_listed_sorted(self):
        self.write("z.bin", "// x\n")
        self.write("a.foo", "# y\n")
        self.write("b.py", "# two\n")
        lines, truncated, unknown = comments.extract(self.root, ["z.bin", "b.py", "a.foo", "x.erl"])
        self.assertEqual(lines, ["b.py: # two"])
        self.assertEqual(unknown, ["a.foo", "x.erl", "z.bin"])

    def test_hash_by_file_name(self):
        names = ["Makefile", "CMakeLists.txt", "Dockerfile", "Justfile", "Rakefile", "Gemfile"]
        for name in names:
            self.write(f"d/{name}", "# c\n")
        lines, _, unknown = comments.extract(self.root, [f"d/{n}" for n in names])
        self.assertEqual(lines, [f"d/{n}: # c" for n in names])
        self.assertEqual(unknown, [])

    def test_truncation(self):
        self.write("a.py", "".join(f"# line {i}\n" for i in range(500)))
        lines, truncated, _ = comments.extract(self.root, ["a.py"])
        self.assertEqual(len(lines), comments.MAX_LINES)
        self.assertTrue(truncated)

    def test_empty_relpaths_no_git_calls(self):
        with mock.patch.object(comments, "_git", wraps=comments._git) as git:
            self.assertEqual(comments.extract(self.root, []), ([], False, []))
        git.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_added_lines_only(self):
        self.git("init", "-q")
        self.write("a.go", "// old\nx := 1\n")
        self.commit("a.go")
        self.write("a.go", "// old\nx := 1\n// new\n")
        self.write("u.py", "# untracked\n")
        lines, _, _ = comments.extract(self.root, ["a.go", "u.py"])
        self.assertEqual(lines, ["a.go: // new", "u.py: # untracked"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_base_commit_sees_committed_lines(self):
        self.git("init", "-q")
        self.write("a.py", "x = 1\n")
        self.commit("a.py")
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        self.write("a.py", "x = 1\n# закоммичено в ходе\n")
        self.commit("a.py")
        self.assertEqual(comments.extract(self.root, ["a.py"])[0], [])
        self.assertEqual(comments.extract(self.root, ["a.py"], base)[0], ["a.py: # закоммичено в ходе"])
        self.assertEqual(comments.extract(self.root, ["a.py"], None)[0], ["a.py: # закоммичено в ходе"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_submodule_file_added_lines_only(self):
        lib = pathlib.Path(self.tmp.name + "-lib")
        try:
            self.git("init", "-q")
            subprocess.run(["git", "init", "-q", str(lib)], check=True)
            (lib / "s.py").write_text("# old sub comment\n", encoding="utf-8")
            git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
            subprocess.run([*git, "-C", str(lib), "add", "s.py"], check=True)
            subprocess.run([*git, "-C", str(lib), "commit", "-qm", "i"], check=True)
            subprocess.run([*git, "-C", str(self.root), "submodule", "add", "-q", str(lib), "sub"],
                           check=True, capture_output=True)
            self.write("sub/s.py", "# old sub comment\n# new sub\n")
            lines, _, _ = comments.extract(self.root, ["sub/s.py"])
            self.assertEqual(lines, ["sub/s.py: # new sub"])
            sub_base = subprocess.run(["git", "-C", str(self.root / "sub"), "rev-parse", "HEAD"],
                                      capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run([*git, "-C", str(self.root / "sub"), "commit", "-qam", "turn"], check=True)
            self.assertEqual(comments.extract(self.root, ["sub/s.py"])[0], [])
            self.assertEqual(comments.extract(self.root, ["sub/s.py"], "HEAD", {"sub": sub_base})[0],
                             ["sub/s.py: # new sub"])
        finally:
            shutil.rmtree(lib, ignore_errors=True)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_two_calls_for_many_files(self):
        self.git("init", "-q")
        names = [f"f{i}.py" for i in range(5)]
        for n in names:
            self.write(n, "x = 1\n")
        self.commit(*names)
        for n in names:
            self.write(n, f"x = 1\n# {n}\n")
        self.write("u.py", "# u\n")
        with mock.patch.object(comments, "_git", wraps=comments._git) as git:
            lines, _, _ = comments.extract(self.root, names + ["u.py"])
        self.assertEqual(lines, [f"{n}: # {n}" for n in names] + ["u.py: # u"])
        self.assertEqual(git.call_count, 2)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_paths_with_spaces_unicode_and_pathspec_magic(self):
        self.git("init", "-q")
        names = ["dir/a b.go", "кириллица.py", ":x.py", "q\"t.py"]
        old = {"dir/a b.go": "// old\n", "кириллица.py": "# old\n", ":x.py": "# old\n", "q\"t.py": "# old\n"}
        for n, t in old.items():
            self.write(n, t)
        self.commit(*names)
        self.write("dir/a b.go", "// old\n++i; // inc\n")
        self.write("кириллица.py", "# old\n# новый\n")
        self.write(":x.py", "# old\n# colon\n")
        self.write("q\"t.py", "# old\n# quote\n")
        lines, _, _ = comments.extract(self.root, names)
        self.assertEqual(lines, ["dir/a b.go: // inc", "кириллица.py: # новый", ":x.py: # colon",
                                 "q\"t.py: # quote"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_user_diff_config_ignored(self):
        self.git("init", "-q")
        self.git("config", "diff.noprefix", "true")
        self.git("config", "diff.mnemonicPrefix", "true")
        self.write("a.go", "// old\n")
        self.commit("a.go")
        self.write("a.go", "// old\n// new\n")
        lines, _, _ = comments.extract(self.root, ["a.go"])
        self.assertEqual(lines, ["a.go: // new"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_without_head_whole_tracked_file(self):
        self.git("init", "-q")
        self.write("a.py", "# staged\nx = 1\n")
        self.git("add", "a.py")
        lines, _, _ = comments.extract(self.root, ["a.py"])
        self.assertEqual(lines, ["a.py: # staged"])


class UnquoteAndFramingTest(unittest.TestCase):
    def test_malformed_escape_is_literal(self):
        self.assertEqual(comments._unquote('"a\\1z.py"'), "a\\1z.py")
        self.assertEqual(comments._unquote('"caf\\303\\251.py"'), "café.py")

    def test_form_feed_inside_added_line_is_kept(self):
        text = "x = 1  // a\x0cb\n"
        self.assertEqual(comments.comment_lines(text, "go"), ["// a\x0cb"])



class MakefileNamesTest(unittest.TestCase):
    def test_all_code_names_have_hash_syntax(self):
        import common
        for name in common.CODE_NAMES:
            self.assertEqual(comments.extract("/nonexistent", [name])[2], [], name)

if __name__ == "__main__":
    unittest.main()
