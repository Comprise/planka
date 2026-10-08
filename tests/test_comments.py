import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import comments  # noqa: E402
import common  # noqa: E402


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

    def test_unknown_ext_is_empty(self):
        self.assertEqual(comments.comment_lines("// x\n# y\n", "bin"), [])
        self.assertEqual(comments.comment_lines("", "go"), [])

    def test_code_after_closed_string_is_scanned(self):
        self.assertEqual(comments.comment_lines('s = """a\n# not\n"""  # yes\n', "py"), ["# yes"])
        self.assertEqual(comments.comment_lines('x = 1 /* a */ y = 2 // b\n', "c"), ["/* a */ y = 2 // b"])

    def test_second_block_after_closed_block(self):
        self.assertEqual(comments.comment_lines("/* a */ /* b\nmid\n*/\nint x;\n", "c"),
                         ["/* a */ /* b", "mid", "*/"])

    def test_docstring_line_reported_once(self):
        src = 'def f():\n    """Doc."""  # tail\n    """Open # x\n    more\n    """\n'
        self.assertEqual(comments.comment_lines(src, "py"), ['"""Doc."""  # tail', '"""Open # x', "more", '"""'])

    def test_multiline_strings_are_data(self):
        cases = {
            "js": ("const s = `a\n// not\n`; // yes\n", "// yes"),
            "ts": ("const s = `${a}\n/* not */\n`; // yes\n", "// yes"),
            "go": ("s := `a\n// not\n` // yes\n", "// yes"),
            "lua": ("s = [[\n-- not\n]] -- yes\n", "-- yes"),
            "rs": ('let s = "a\n// not\n"; // yes\n', "// yes"),
            "kt": ('val s = """\n// not\n""" // yes\n', "// yes"),
            "java": ('String s = """\n// not\n"""; // yes\n', "// yes"),
            "toml": ("s = '''\n# not\n''' # yes\n", "# yes"),
            "nix": ("s = ''\n# not ''${x}\n''; # yes\n", "# yes"),
            "cpp": ('auto s = R"x(\n// not )"\n)x"; // yes\n', "// yes"),
            "cs": ('var s = @"a ""q""\n// not\n"; // yes\n', "// yes"),
        }
        for ext, (src, expected) in cases.items():
            self.assertEqual(comments.comment_lines(src, ext), [expected], ext)

    def test_rust_raw_string_spans_lines(self):
        self.assertEqual(comments.comment_lines('let s = r#"a\n// not "\n"#; // yes\n', "rs"), ["// yes"])

    def test_apostrophe_is_not_a_string(self):
        self.assertEqual(comments.comment_lines("fn f<'a>(x: &'a str) // it's a comment\n", "rs"),
                         ["// it's a comment"])
        self.assertEqual(comments.comment_lines("let c = '/'; // c\n", "rs"), ["// c"])
        self.assertEqual(comments.comment_lines("key: don't # it's here\nq: 'it''s # not'\n", "yaml"),
                         ["# it's here"])
        self.assertEqual(comments.comment_lines("<p>Don't</p><!-- it's a note -->\n", "html"),
                         ["<!-- it's a note -->"])
        self.assertEqual(comments.comment_lines("<p>Don't</p><!-- it's a note -->\n", "vue"),
                         ["<!-- it's a note -->"])
        self.assertEqual(comments.comment_lines("f x' = x' -- it's\n", "hs"), ["-- it's"])

    def test_more_syntaxes(self):
        self.assertEqual(comments.comment_lines("; top\nk = v ; tail\nurl = a;b\n# h\n", "ini"),
                         ["; top", "; tail", "# h"])
        self.assertEqual(comments.comment_lines("; c\n", "cfg"), ["; c"])
        for ext in ("sql", "tf", "nix"):
            self.assertEqual(comments.comment_lines("/* a\nb */\nx\n", ext), ["/* a", "b */"], ext)
        self.assertEqual(comments.comment_lines("x = 1 // c\n", "tf"), ["// c"])
        self.assertEqual(comments.comment_lines("<?php\n#[Attr]\n$x = 1; # c\n", "php"), ["# c"])
        self.assertEqual(comments.comment_lines("<# a\nb #>\n$x = 1 # c\n", "ps1"), ["<# a", "b #>", "# c"])
        self.assertEqual(comments.comment_lines("#= a\nb =#\nx = 1 # c\n", "jl"), ["#= a", "b =#", "# c"])
        self.assertEqual(comments.comment_lines("x = <<EOT\n# not\n  EOT\n# c\n", "tf"), ["# c"])
        self.assertEqual(comments.comment_lines('const s = \\\\ // not\n// c\n', "zig"), ["// c"])

    def test_styles(self):
        src = 'a { b: url(http://x/y); c: "/* no */"; } /* c */\n'
        self.assertEqual(comments.comment_lines(src, "css"), ["/* c */"])
        self.assertEqual(comments.comment_lines("a { b: 1 } // not css\n", "css"), [])
        src = '// one\n$u: url(http://y); // two\n$s: "// no";\n/* b\n   c */\n'
        for ext in ("scss", "sass", "less"):
            self.assertEqual(comments.comment_lines(src, ext), ["// one", "// two", "/* b", "c */"], ext)

    def test_json_manifests(self):
        src = '{\n  "a": "// no /* no */" // yes\n  /* b */\n}\n'
        for name in ("package.json", "composer.json"):
            self.assertEqual(comments.comment_lines(src, name), [], name)
        for name in ("tsconfig.json", "jsconfig.json", "deno.json"):
            self.assertEqual(comments.comment_lines(src, name), ["// yes", "/* b */"], name)
        self.assertEqual(comments.comment_lines('module x // m\nreplace a => "// no"\n', "go.mod"), ["// m"])

    def test_shebang_only_on_first_line(self):
        self.assertEqual(comments.comment_lines("#!/bin/sh\n#!not shebang\n", "sh"), ["#!not shebang"])

    def test_line_numbers(self):
        self.assertEqual(comments._comments("x = 1\n# a\n\n/* no */\n'\"\"\"'\n# b\n", "py"),
                         [(2, "# a"), (6, "# b")])


class LanguageSyntaxTest(unittest.TestCase):
    """Каждый язык: маркер комментария внутри литерала — код, настоящий комментарий, блок."""

    def check(self, ext, src, expected):
        self.assertEqual(comments.comment_lines(src, ext), expected, ext)

    def test_erlang(self):
        self.check("erl", 'io:format("100%~n"), A = \'50%\', % c\n%% doc\n', ["% c", "%% doc"])
        # «$%» и «$"» — символьные литералы.
        self.check("erl", 'X = $%, Y = $", Z = "a%b". % c\n', ["% c"])
        self.check("erl", 'S = """\n% not\n""". % c\n', ["% c"])

    def test_clojure(self):
        self.check("clj", '(def s "a;b") ; c\n;; doc\n', ["; c", ";; doc"])
        # «\;» и «\"» — символьные литералы, строка многострочная, «#"…"» — регулярное выражение.
        self.check("clj", '(str \\; \\" #"a;\\"b") ; c\n', ["; c"])
        self.check("clj", '(defn f\n  "Doc ; not\n  more"\n  [x]) ; c\n', ["; c"])
        # «#_» убирает форму из чтения, это код, а не текст комментария.
        self.check("clj", "#_(foo) (bar) ; c\n", ["; c"])

    def test_fsharp(self):
        self.check("fs", 'let s = "// not" // c\n(* a\n (* inner *) b\n*)\nlet x = 1\n',
                   ["// c", "(* a", "(* inner *) b", "*)"])
        # «(*)» — оператор умножения, символ «'"'», verbatim и тройные строки.
        self.check("fs", "let m = (*) 2 3 // c\nlet q = '\"' // d\n", ["// c", "// d"])
        self.check("fs", 'let v = @"a ""//"" b" // c\nlet t = """\n// not\n""" // d\n', ["// c", "// d"])

    def test_visual_basic(self):
        self.check("vb", 'Dim s = "it\'s ""REM"" x" \' c\nREM block\nx = 1 : rem tail\n',
                   ["' c", "REM block", "rem tail"])
        # «Remove» и «REM» внутри строки — код.
        self.check("vb", 'Remove(x)\ny = "REM"\n', [])

    def test_nim(self):
        self.check("nim", 'let s = "# not" # c\nlet c = \'#\' # d\n', ["# c", "# d"])
        self.check("nim", '#[ a\n #[ inner ]# b\n]#\nlet x = 1\n', ["#[ a", "#[ inner ]# b", "]#"])
        self.check("nim", '##[ doc\nmore ]##\nlet x = 1 ## d\n', ["##[ doc", "more ]##", "## d"])
        # Сырые строки: «r"a\"» закрывается второй кавычкой, удвоенная кавычка — escape.
        self.check("nim", 'let r = r"a\\" # c\nlet g = fmt"x""#""y" # d\nlet t = """\n# not\n""" # e\n',
                   ["# c", "# d", "# e"])

    def test_emacs_lisp(self):
        self.check("el", '(setq s "a;b") ; c\n;;; Commentary\n', ["; c", ";;; Commentary"])
        self.check("el", '(list ?; ?\\" ?\\;) ; c\n', ["; c"])
        self.check("el", '(defun f ()\n  "Doc ; not\nmore"\n  1) ; c\n', ["; c"])

    def test_vim(self):
        self.check("vim", '" top\n  :" colon\nlet s = "a \\" b" " tail\necho \'it\'\'s "\' " c\n',
                   ['" top', '" colon', '" tail', '" c'])
        # Строка с закрывающей кавычкой — литерал; регистр «"a» после слова — код.
        self.check("vim", 'echo "hi"\nnormal! x"ay\n', [])
        # vim9script: «#» после пробела — комментарий, «#{» — словарь, «#» внутри слова — автозагрузка.
        self.check("vim", "var d = #{a: 1} # c\ncall foo#bar()\n# top\n", ["# c", "# top"])

    def test_batch(self):
        for ext in ("bat", "cmd"):
            self.check(ext, '@echo off\nREM c\n@rem at\n:: colons\necho a & rem tail\n',
                       ["REM c", "rem at", ":: colons", "rem tail"])
            # REM внутри строки, в чужом слове и как аргумент — код; метка «:x» — не комментарий.
            self.check(ext, 'echo "a & rem b"\necho rem x\nset remark=1\n:label\necho a::b\n', [])


    def test_language_variants(self):
        for ext in ("cljs", "edn"):
            self.check(ext, '{:a "x;y"} \\; ; c\n', ["; c"])
        for ext in ("fsx", "fsi"):
            self.check(ext, 'let s = "// not" // c\n(* a (* b *) *)\n', ["// c", "(* a (* b *) *)"])
        self.check("vbs", 'x = "\'REM" \' c\nREM d\n', ["' c", "REM d"])

    def test_common_lisp(self):
        # «#\;» — символьный литерал, «#| |#» вкладываются.
        self.check("lisp", '(format t "a;b" #\\;) ; c\n#| a\n#| b |#\nstill |#\n(x)\n',
                   ["; c", "#| a", "#| b |#", "still |#"])


class NestedAndDocTest(unittest.TestCase):
    def test_nested_blocks(self):
        cases = {"rs": "/* a /* b */\nstill */\nlet x = 1;\n",
                 "swift": "/* a /* b */\nstill */\nlet x = 1\n",
                 "kt": "/* a /* b */\nstill */\nval x = 1\n",
                 "scala": "/* a /* b */\nstill */\nval x = 1\n",
                 "dart": "/* a /* b */\nstill */\nvar x = 1;\n",
                 "hs": "{- a {- b -}\nstill -}\nx = 1\n",
                 "jl": "#= a #= b =#\nstill =#\nx = 1\n"}
        for ext, src in cases.items():
            lines = comments.comment_lines(src, ext)
            self.assertEqual(len(lines), 2, ext)
            self.assertTrue(lines[1].startswith("still"), ext)

    def test_c_block_does_not_nest(self):
        self.check_lines("c", "/* a /* b */\nint x; // c\n", ["/* a /* b */", "// c"])
        self.check_lines("groovy", "/* a /* b */\nint x // c\n", ["/* a /* b */", "// c"])

    def check_lines(self, ext, src, expected):
        self.assertEqual(comments.comment_lines(src, ext), expected, ext)

    def test_nested_close_and_open_on_one_line(self):
        line = "/* a */ x(); /* b /* c */ d */ y(); // e"
        self.check_lines("rs", line + "\n", [line])
        self.check_lines("rs", "let a = 1; /* b /* c */ d */\nlet x = 1;\n", ["/* b /* c */ d */"])

    def test_ruby_begin_end(self):
        src = "x = 1\n=begin\n# inside\ntext\n=end\ny = 2 # c\n"
        self.check_lines("rb", src, ["=begin", "# inside", "text", "=end", "# c"])
        # «=begin» не с начала строки — не блок.
        self.check_lines("rb", "x = 1\n  =begin\n", [])

    def test_perl_pod(self):
        src = "my $x = 1;\n=pod\n\nDoc # x\n\n=cut\nmy $y = 2; # c\n"
        self.check_lines("pl", src, ["=pod", "Doc # x", "=cut", "# c"])
        self.check_lines("pl", "=head1 NAME\n\nfoo\n=cut\n", ["=head1 NAME", "foo", "=cut"])
        # «= 5» и «=~» в начале строки — продолжение выражения.
        self.check_lines("pl", "my $x\n= 5; # c\n", ["# c"])

    def test_php_heredoc(self):
        src = ("<?php\n$s = <<<EOT\n// not\n# not\nEOT;\n$t = <<<'NOW'\n/* not */\n  NOW;\n"
               "$u = <<<\"Q\"\n// not\n    Q . 'x'; // c\n")
        self.check_lines("php", src, ["// c"])
        # Без терминатора до конца файла — не heredoc.
        self.check_lines("php", "<?php\n$s = <<<EOT\n// c\n", ["// c"])

    def test_elixir_doc_attributes(self):
        src = ('@moduledoc """\nModule # doc\n"""\n@doc "One line"\ndef f, do: 1 # c\n'
               '@typedoc ~S"""\nT\n"""\ns = """\n# not\n"""\n@doc false\n')
        self.check_lines("ex", src, ['@moduledoc """', "Module # doc", '"""', '@doc "One line"', "# c",
                                     '@typedoc ~S"""', "T", '"""'])

    def test_question_mark_char_literal(self):
        self.check_lines("ex", "x = ?#\ny = ?\" <> \"#\" # c\n", ["# c"])
        self.check_lines("rb", "x = ?# \ny = c ? 1 : 2 # c\nz = valid? # d\n", ["# c", "# d"])

    def test_swift_raw_strings(self):
        self.check_lines("swift", 'let s = #"a " // not"#; // c\nlet t = ##"x"#y"##\n', ["// c"])
        self.check_lines("swift", 'let s = #"""\n// not\n"""#\nlet x = 1 // c\n', ["// c"])

    def test_perl_heredoc_into_braced_filehandle(self):
        for src in ("print{$fh} <<END;\n# not\nEND\n# real\n", "print {$self->{fh}} <<END;\n# not\nEND\n# real\n",
                    "print {*STDOUT} <<END;\n# not\nEND\n# real\n", "printf{$fh}<<END;\n# not\nEND\n# real\n"):
            self.check_lines("pl", src, ["# real"])
        # Элемент хеша перед «<<» без print — сдвиг.
        self.check_lines("pl", "my $x = $h->{$k} <<B; # c1\n# c2\nB\n", ["# c1", "# c2"])


class ParserEdgeTest(unittest.TestCase):
    """Края разборщика: каждый тест держит одну ветвь _token, _close или _comments."""

    def test_backslash_escaped_quote_keeps_string_open(self):
        self.assertEqual(comments.comment_lines('const a = "a \\" // not"; // yes\n', "js"), ["// yes"])
        self.assertEqual(comments.comment_lines("s = 'it\\'s # x' # c\n", "py"), ["# c"])

    def test_char_literal_quote_does_not_open_string(self):
        cases = {"c": "char q = '\"'; s = \"//x\"; // c\n",
                 "go": "q := '\"'; s := \"//x\" // c\n",
                 "rs": "let q = '\"'; let s = \"//x\"; // c\n"}
        for ext, src in cases.items():
            self.assertEqual(comments.comment_lines(src, ext), ["// c"], ext)

    def test_csharp_verbatim_interpolated_string(self):
        self.assertEqual(comments.comment_lines('var s = @$"C:\\{d}\\"; var t = "// x"; // c\n', "cs"), ["// c"])

    def test_cpp_prefixed_raw_string(self):
        self.assertEqual(comments.comment_lines('auto s = u8R"(a " // not)"; // yes\n', "cpp"), ["// yes"])

    def test_rust_byte_raw_string(self):
        self.assertEqual(comments.comment_lines('let s = br"a\\"; // c\nlet t = 1;\n', "rs"), ["// c"])

    def test_hash_after_dollar_or_brace_is_code(self):
        self.assertEqual(comments.comment_lines("my $n = $#a; # c\n", "pl"), ["# c"])
        self.assertEqual(comments.comment_lines("t:\n\techo $${#PATH} # c\n", "makefile"), ["# c"])

    def test_escaped_hash_is_code(self):
        self.assertEqual(comments.comment_lines("x := a\\#b # c\n", "makefile"), ["# c"])
        self.assertEqual(comments.comment_lines("set(X a\\#b) # c\n", "cmake"), ["# c"])

    def test_crlf_heredoc_terminator(self):
        self.assertEqual(comments.comment_lines("cat <<EOF > s.md\r\n# body\r\nEOF\r\n# real\r\n", "sh"), ["# real"])

    def test_dash_heredoc_with_tab_indented_terminator(self):
        self.assertEqual(comments.comment_lines("cat <<-EOF\n\t# body\n\tEOF\n# real\n", "sh"), ["# real"])

    def test_bom_before_shebang(self):
        self.assertEqual(comments.comment_lines("\ufeff#!/usr/bin/env python3\n# c\n", "py"), ["# c"])
        self.assertEqual(comments.comment_lines("\ufeff# c\n", "py"), ["# c"])

    def test_ruby_heredoc_body_is_data(self):
        src = "sql = <<~SQL\n  # not\n  SQL\nq = <<-'EOS'.strip # tail\n# not\n    EOS\nt = <<EOF\n# not\nEOF\n# real\n"
        self.assertEqual(comments.comment_lines(src, "rb"), ["# tail", "# real"])
        self.assertEqual(comments.comment_lines("execute <<~SQL\n  # not\nSQL\n# real\n", "rakefile"), ["# real"])
        self.assertEqual(comments.comment_lines('print <<"EOT";\n# not\nEOT\n# real\n', "pl"), ["# real"])

    def test_shift_is_not_ruby_heredoc(self):
        src = "x = 1<<BITS # a\nclass << self # b\n  arr << ITEM # c\nend\n"
        self.assertEqual(comments.comment_lines(src, "rb"), ["# a", "# b", "# c"])

    def test_shift_after_closer_is_not_heredoc(self):
        for src in ("a[0]<<X # a\n# b\nX\n", "h{1}<<X # a\n# b\nX\n", "f(1)<<X # a\n# b\nX\n"):
            self.assertEqual(comments.comment_lines(src, "rb"), ["# a", "# b"], src)

    def test_shift_after_space_is_not_heredoc(self):
        # Переменная Perl перед пробелом и строчный идентификатор после слова Ruby — сдвиг и добавление.
        self.assertEqual(comments._comments("my $s = $a <<EOF; # c5\n# c6\n", "pl"), [(1, "# c5"), (2, "# c6")])
        self.assertEqual(comments._comments("push @a, @b <<EOF; # c5\n# c6\n", "pl"), [(1, "# c5"), (2, "# c6")])
        self.assertEqual(comments._comments("a = [1]\na <<b\n# c2\nb\n", "rb"), [(3, "# c2")])
        self.assertEqual(comments._comments("a = [1]\n(a) <<EOF\n# c2\nEOF\n", "rb"), [(3, "# c2")])
        # С терминатором: откат незакрытого heredoc не маскирует решение «сдвиг».
        self.assertEqual(comments._comments("my $s = $a <<B; # c1\n# c2\nB\n", "pl"), [(1, "# c1"), (2, "# c2")])
        self.assertEqual(comments._comments("push @a, @b <<B; # c1\n# c2\nB\n", "pl"), [(1, "# c1"), (2, "# c2")])
        # Переменная Perl без print перед ней и слово, лишь кончающееся на print, — сдвиг.
        self.assertEqual(comments._comments("$fh <<EOF; # c1\n# c2\nEOF\n", "pl"), [(1, "# c1"), (2, "# c2")])
        self.assertEqual(comments._comments("reprint $fh <<EOF; # c1\n# c2\nEOF\n", "pl"), [(1, "# c1"), (2, "# c2")])
        # Ruby: «$fh» — глобальная переменная, «<<» за ней — сдвиг и после print.
        self.assertEqual(comments._comments("print $fh <<EOF # c1\n# c2\nEOF\n", "rb"), [(1, "# c1"), (2, "# c2")])

    def test_perl_heredoc_into_filehandle(self):
        # print, printf, say в дескриптор: «$fh», «{$fh}», STDOUT, STDERR — heredoc.
        for src in ("print $fh <<EOF;\n# not\nEOF\n# real\n", "printf $out <<\"EOT\", 1;\n# not\nEOT\n# real\n",
                    "say {$fh} <<~EOT;\n  # not\n  EOT\n# real\n", "print STDERR <<eof;\n# not\neof\n# real\n",
                    "if ($x) { print  $log  <<'END' }\n# not\nEND\n# real\n"):
            self.assertEqual(comments.comment_lines(src, "pl"), ["# real"], src)

    def test_perl_heredoc_real_corpus_forms(self):
        # Идентификатор с ведущим «_», «::» в имени дескриптора, «<<» вплотную после функции вывода.
        for src in ("print <<_EOUSAGE_ ;\n# not\n_EOUSAGE_\n# real\n", "print <<_EOVERS;\n# not\n_EOVERS\n# real\n",
                    "print {$DB::OUT} <<EOP;\n# not\nEOP\n# real\n", "die<<EOF;\n# not\nEOF\n# real\n",
                    "warn<<EOF;\n# not\nEOF\n# real\n", "print<<EOT;\n# not\nEOT\n# real\n",
                    "printf<<EOT, 1;\n# not\nEOT\n# real\n", "say<<EOT;\n# not\nEOT\n# real\n",
                    "print CSS<<EOF;\n# not\nEOF\n# real\n", "print STDERR<<EOF;\n# not\nEOF\n# real\n"):
            self.assertEqual(comments.comment_lines(src, "pl"), ["# real"], src)

    def test_perl_tight_shift_is_not_heredoc(self):
        # Вплотную после прочего слова, переменной или константы вне print — сдвиг; в Ruby print<<EOT — сдвиг.
        for src, ext in (("x = 1<<EOF; # c1\n# c2\nEOF\n", "pl"), ("$print<<EOF; # c1\n# c2\nEOF\n", "pl"),
                         ("foo CSS<<EOF; # c1\n# c2\nEOF\n", "pl"), ("$h->print<<EOF; # c1\n# c2\nEOF\n", "pl"),
                         ("reprint<<EOF; # c1\n# c2\nEOF\n", "pl"), ("print<<EOF # c1\n# c2\nEOF\n", "rb"),
                         ("print CSS<<EOF # c1\n# c2\nEOF\n", "rb"), ("print <<_ # c1\n# c2\n_\n", "pl")):
            self.assertEqual(comments.comment_lines(src, ext), ["# c1", "# c2"], src)
        self.assertEqual(comments.comment_lines("puts <<_eof # c1\n# c2\n_eof\n", "rb"), ["# c1", "# c2"])

    def test_perl_hash_element_before_shift(self):
        # «}» закрывает элемент хеша, а не «{$дескриптор}» после print.
        self.assertEqual(comments._comments("my $x = $h{$k} <<FLAGS; # c1\n# c2\nFLAGS\n", "pl"),
                         [(1, "# c1"), (2, "# c2")])
        self.assertEqual(comments._comments("print $h{$k} <<FLAGS; # c1\n# c2\nFLAGS\n", "pl"),
                         [(1, "# c1"), (2, "# c2")])

    def test_shift_after_quoted_term_is_not_heredoc(self):
        for src in ('puts "a" <<EOF # c1\n# c2\nEOF\n', "puts 'a' <<EOF # c1\n# c2\nEOF\n",
                    "puts `a` <<EOF # c1\n# c2\nEOF\n"):
            self.assertEqual(comments.comment_lines(src, "rb"), ["# c1", "# c2"], src)

    def test_lowercase_heredoc_after_operator_or_line_start(self):
        # Строчный идентификатор без «-», «~» и кавычек — heredoc там, где добавление невозможно.
        self.assertEqual(comments.comment_lines("x = <<eof\n# not\neof\n# real\n", "rb"), ["# real"])
        self.assertEqual(comments.comment_lines("  <<'eof'\n# not\neof\n# real\n", "rb"), ["# real"])
        self.assertEqual(comments.comment_lines("puts <<'eof'\n# not\neof\n# real\n", "rb"), ["# real"])
        self.assertEqual(comments.comment_lines("puts <<`eof`\n# not\neof\n# real\n", "rb"), ["# real"])

    def test_perl_filehandle_branch_needs_print_and_dollar(self):
        self.assertEqual(comments.comment_lines("print STDOUT <<eof;\n# not\neof\n# real\n", "pl"), ["# real"])
        self.assertEqual(comments.comment_lines("print STDERR <<eof;\n# not\neof\n# real\n", "pl"), ["# real"])
        for src in ("foo STDOUT <<eof; # c1\n# c2\neof\n", "foo STDERR <<eof; # c1\n# c2\neof\n",
                    "print @a <<EOF; # c1\n# c2\nEOF\n"):
            self.assertEqual(comments.comment_lines(src, "pl"), ["# c1", "# c2"], src)

    def test_many_unterminated_heredocs_are_all_rolled_back(self):
        # Каждый откат снимает открытие, поглотившее остаток файла: четыре подряд — пять разборов.
        src = "a = <<A\n# c2\nb = <<B\n# c4\nc = <<C\n# c6\nd = <<D\n# c8\n"
        self.assertEqual(comments._comments(src, "rb"), [(2, "# c2"), (4, "# c4"), (6, "# c6"), (8, "# c8")])
        # Все незакрытые открытия одной строки снимаются за один откат.
        many = "f(" + ", ".join(f"<<A{i}" for i in range(10)) + ")\n# c2\n"
        self.assertEqual(comments._comments(many, "rb"), [(2, "# c2")])

    def test_heredoc_after_operator_or_function_name(self):
        for src in ("x = <<EOF\n# not\nEOF\n# real\n", "foo(<<EOF)\n# not\nEOF\n# real\n",
                    "print <<EOF;\n# not\nEOF\n# real\n", "puts <<~sql\n# not\nsql\n# real\n",
                    "foo a, <<EOF\n# not\nEOF\n# real\n", "<<EOF\n# not\nEOF\n# real\n"):
            self.assertEqual(comments.comment_lines(src, "rb"), ["# real"], src)

    def test_unterminated_heredoc_is_rolled_back(self):
        self.assertEqual(comments._comments("x = <<EOF\n# c2\n# c3\n", "rb"), [(2, "# c2"), (3, "# c3")])
        # Откат касается только незакрытого: закрытый heredoc остаётся данными.
        src = "a = <<ONE\n# not\nONE\nb = <<TWO\n# c5\n"
        self.assertEqual(comments._comments(src, "rb"), [(5, "# c5")])
        self.assertEqual(comments._comments("x = <<EOT\n# c2\n", "tf"), [(2, "# c2")])

    def test_indented_terminator_of_plain_heredoc_is_not_terminator(self):
        src = "x = <<ID\n  ID\n# not\nID\n# real\n"
        self.assertEqual(comments.comment_lines(src, "rb"), ["# real"])


class ExpressionLiteralTest(unittest.TestCase):
    """Регулярные выражения JS и TS, slashy-строки Groovy, разметка JSX: «/» и «<» после токена, за которым
    начинается выражение, — литерал или тег; после значения — деление и сравнение."""

    def test_js_regex_literals(self):
        for ext in ("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"):
            self.assertEqual(comments._comments("let r = /\\/\\//;\n", ext), [], ext)
            self.assertEqual(comments._comments("let r = /a\\/*/;\nx = 1;\n// c\n", ext), [(3, "// c")], ext)
            self.assertEqual(comments._comments("let r = /[/]/; // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("return /x/.test(s); // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("s.replace(/\\/+/g, '/'); // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("x = c ? /a/ : /b\\//i // c\n", ext), [(1, "// c")], ext)

    def test_js_division_stays_division(self):
        for src in ("x = a / b // c\n", "y = x[i] / 2 // c\n", "z = f() / 3 // c\n", ")/2 // c\n",
                    "n = i++ / 2 // c\n", "q = this.return / 2 // c\n", "w = 1 /2/ 3 // c\n"):
            self.assertEqual(comments.comment_lines(src, "js"), ["// c"], src)

    def test_division_continues_previous_line(self):
        # Начало строки — начало выражения, только если им кончилась прошлая строка кода.
        src = "s = a[u] // c1\n// c2\n/ b.c // c3\n/ d; // c4\nx = (\n/a\\//.test(y)) // c5\n"
        self.assertEqual(comments.comment_lines(src, "js"), ["// c1", "// c2", "// c3", "// c4", "// c5"])
        self.assertEqual(comments.comment_lines("f() /* c1 */\n/ 2 // c2\n", "ts"), ["/* c1 */", "// c2"])

    def test_unclosed_regex_ends_at_line_end(self):
        # Мнимая регулярка без закрытия прячет только остаток своей строки.
        self.assertEqual(comments._comments("x = /[a // b\n// c\n", "js"), [(2, "// c")])

    def test_groovy_slashy_strings(self):
        for ext in ("groovy", "gradle"):
            self.assertEqual(comments._comments("def u = /http:\\/\\/x/ // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("def u = /http://x/\n", ext), [], ext)
            self.assertEqual(comments._comments("def u = $/http://x/$ // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("def u = $/\nhttp://x $/ $$\n/* not */\n/$\n// c\n", ext),
                             [(5, "// c")], ext)
            self.assertEqual(comments._comments("def h = a / b // c\n", ext), [(1, "// c")], ext)

    def test_jsx_text_is_not_code(self):
        for ext in ("jsx", "tsx", "js"):
            src = "const a = <p>see http://x.y</p>; // c\n"
            self.assertEqual(comments._comments(src, ext), [(1, "// c")], ext)
            src = ("return (\n  <div className=\"a//b\">\n    see http://x.y\n    {/* jsx */}\n"
                   "    {items.map(i => <li key={i}>// {i}</li>)}\n    <br/>\n    <>frag // t</>\n  </div>\n);\n"
                   "// after\n")
            self.assertEqual(comments._comments(src, ext), [(4, "{/* jsx */}"[1:]), (10, "// after")], ext)

    def test_jsx_tag_comment_in_attributes(self):
        src = "x = <div // c\n  id='a'\n>text // t</div>\n"
        self.assertEqual(comments._comments(src, "jsx"), [(1, "// c")])

    def test_comparison_and_generics_are_not_tags(self):
        for src in ("if (a <b) f() // c\n", "const x: Array<string> = [] // c\n",
                    "const f = <T,>(x: T) => x // c\n", "const g = <T extends X>(x: T) => x // c\n",
                    "useState<string>(null) // c\n"):
            self.assertEqual(comments.comment_lines(src, "tsx"), ["// c"], src)

    def test_plain_ts_has_no_jsx(self):
        self.assertEqual(comments.comment_lines("const f = <T>(x: T) => x // c\n", "ts"), ["// c"])

    def test_unclosed_tag_is_rolled_back(self):
        # Мнимый тег без закрытия до конца файла — не тег: разбор повторяется, и комментарии за ним видны.
        src = "type F = <T>(x: T) => T;\n// c\nconst a = <p>t // t</p>;\n"
        self.assertEqual(comments._comments(src, "tsx"), [(2, "// c")])


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
        lines, truncated, unknown, late = comments.extract(self.root, ["a.go", "b.py", "c.bin", "missing.go"])
        self.assertEqual(lines, ["a.go: // one", "b.py: # two"])
        self.assertFalse(truncated)
        self.assertEqual((unknown, late), (["c.bin"], []))

    def test_unknown_syntax_listed_sorted(self):
        self.write("z.bin", "// x\n")
        self.write("a.foo", "# y\n")
        self.write("b.py", "# two\n")
        lines, truncated, unknown, _ = comments.extract(self.root, ["z.bin", "b.py", "a.foo", "x.dat"])
        self.assertEqual(lines, ["b.py: # two"])
        self.assertEqual(unknown, ["a.foo", "x.dat", "z.bin"])

    def test_manifests_by_file_name(self):
        self.write("web/tsconfig.json", "{ // ts\n}\n")
        self.write("web/package.json", '{"a": "// x"}\n')
        self.write("go.mod", "module m // g\n")
        self.write("other.json", "{}\n")
        lines, _, unknown, _ = comments.extract(self.root, ["web/tsconfig.json", "web/package.json", "go.mod",
                                                         "other.json"])
        self.assertEqual(lines, ["web/tsconfig.json: // ts", "go.mod: // g"])
        self.assertEqual(unknown, ["other.json"])

    def test_hash_by_file_name(self):
        names = ["Makefile", "CMakeLists.txt", "Dockerfile", "Justfile", "Rakefile", "Gemfile"]
        for name in names:
            self.write(f"d/{name}", "# c\n")
        lines, _, unknown, _ = comments.extract(self.root, [f"d/{n}" for n in names])
        self.assertEqual(lines, [f"d/{n}: # c" for n in names])
        self.assertEqual(unknown, [])

    def test_truncation(self):
        self.write("a.py", "".join(f"# line {i}\n" for i in range(500)))
        lines, truncated, _, _ = comments.extract(self.root, ["a.py"])
        self.assertEqual(len(lines), comments.MAX_LINES)
        self.assertTrue(truncated)

    def test_exactly_max_lines_not_truncated(self):
        self.write("a.py", "".join(f"# {i}\n" for i in range(comments.MAX_LINES)))
        lines, truncated, _, _ = comments.extract(self.root, ["a.py"])
        self.assertEqual(len(lines), comments.MAX_LINES)
        self.assertFalse(truncated)
        self.write("a.py", "".join(f"# {i}\n" for i in range(comments.MAX_LINES + 1)))
        self.assertTrue(comments.extract(self.root, ["a.py"])[1])

    def test_max_bytes(self):
        body = "# " + "x" * 1000
        self.write("a.py", f"{body}\n" * 20)
        lines, truncated, _, _ = comments.extract(self.root, ["a.py"])
        self.assertTrue(truncated)
        self.assertEqual(len(lines), comments.MAX_BYTES // len(f"a.py: {body}"))
        self.assertLessEqual(sum(len(l.encode("utf-8")) for l in lines), comments.MAX_BYTES)

    def test_no_lines_after_byte_truncation(self):
        self.write("a.py", ("# " + "x" * 1000 + "\n") * 20)
        self.write("b.py", "# s\n")
        lines, truncated, _, _ = comments.extract(self.root, ["a.py", "b.py"])
        self.assertTrue(truncated)
        self.assertEqual([l for l in lines if l.startswith("b.py")], [])

    def test_deadline_passed_files_without_check(self):
        common._reset()
        self.write("a.py", "# a\n")
        self.write("b.bin", "x\n")
        lines, truncated, unknown, late = comments.extract(self.root, ["a.py", "b.bin"], None, None,
                                                           time.monotonic() - 1)
        self.assertEqual((lines, truncated, unknown, late), ([], False, ["b.bin"], ["a.py"]))
        self.assertEqual(common._messages,
                         ["planka: строки комментариев не извлечены в срок, файлов кода без проверки: 1"])
        common._reset()

    def test_git_timeout_files_without_check(self):
        common._reset()
        self.write("a.py", "# a\n")
        with mock.patch.object(comments.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            lines, _, unknown, late = comments.extract(self.root, ["a.py"], "HEAD", None, time.monotonic() + 30)
        self.assertEqual((lines, unknown, late), ([], [], ["a.py"]))
        self.assertEqual(len(common._messages), 1)
        common._reset()

    def test_empty_relpaths_no_git_calls(self):
        with mock.patch.object(comments, "_git", wraps=comments._git) as git:
            self.assertEqual(comments.extract(self.root, []), ([], False, [], []))
        git.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_added_lines_only(self):
        self.git("init", "-q")
        self.write("a.go", "// old\nx := 1\n")
        self.commit("a.go")
        self.write("a.go", "// old\nx := 1\n// new\n")
        self.write("u.py", "# untracked\n")
        lines, _, _, _ = comments.extract(self.root, ["a.go", "u.py"])
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
            lines, _, _, _ = comments.extract(self.root, ["sub/s.py"])
            self.assertEqual(lines, ["sub/s.py: # new sub"])
            sub_base = subprocess.run(["git", "-C", str(self.root / "sub"), "rev-parse", "HEAD"],
                                      capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run([*git, "-C", str(self.root / "sub"), "commit", "-qam", "turn"], check=True)
            self.assertEqual(comments.extract(self.root, ["sub/s.py"])[0], [])
            self.assertEqual(comments.extract(self.root, ["sub/s.py"], "HEAD", {"sub": sub_base})[0],
                             ["sub/s.py: # new sub"])
        finally:
            shutil.rmtree(lib, ignore_errors=True)

    def init_repo(self, path, files):
        """Отдельный репозиторий path с коммитом files {путь: текст}; HEAD."""
        path.mkdir(parents=True, exist_ok=True)
        git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*git, "init", "-q"], check=True)
        for rel, text in files.items():
            (path / rel).write_text(text, encoding="utf-8")
        subprocess.run([*git, "add", "--", *files], check=True)
        subprocess.run([*git, "commit", "-qm", "i"], check=True)
        return subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_nested_repo_against_its_base(self):
        self.git("init", "-q")
        self.write("top.py", "x = 1\n")
        self.commit("top.py")
        nested = self.init_repo(self.root / "nested", {"n.py": "# old comment english\n"})
        inner = self.init_repo(self.root / "nested" / "inner", {"i.py": "# old inner\n"})
        self.write("nested/n.py", "# old comment english\n# новый комментарий\n")
        self.write("nested/inner/i.py", "# old inner\n# новый внутренний\n")
        bases = {"nested": nested, "nested/inner": inner}
        lines, _, _, _ = comments.extract(self.root, ["nested/n.py", "nested/inner/i.py"], "HEAD", bases)
        self.assertEqual(lines, ["nested/n.py: # новый комментарий", "nested/inner/i.py: # новый внутренний"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_nested_repo_inside_submodule_against_its_base(self):
        lib = pathlib.Path(self.tmp.name + "-lib")
        self.addCleanup(shutil.rmtree, lib, True)
        self.git("init", "-q")
        self.init_repo(lib, {"s.py": "x = 1\n"})
        subprocess.run(["git", "-c", "protocol.file.allow=always", "-C", str(self.root), "submodule", "add", "-q",
                        str(lib), "sub"], check=True, capture_output=True)
        sub = subprocess.run(["git", "-C", str(self.root / "sub"), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        inner = self.init_repo(self.root / "sub" / "inner", {"i.py": "# old inner\n"})
        self.write("sub/inner/i.py", "# old inner\n# новый\n")
        lines, _, _, _ = comments.extract(self.root, ["sub/inner/i.py"], "HEAD", {"sub": sub, "sub/inner": inner})
        self.assertEqual(lines, ["sub/inner/i.py: # новый"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_project_gitattributes_binary_still_diffed(self):
        self.git("init", "-q")
        self.write(".gitattributes", "*.py -diff\n*.go binary\n")
        self.write("a.py", "# old\nx = 1\n")
        self.write("b.go", "// old\n")
        self.commit(".gitattributes", "a.py", "b.go")
        self.write("a.py", "# old\nx = 1\n# new\n")
        self.write("b.go", "// old\n// new\n")
        self.assertEqual(comments.extract(self.root, ["a.py", "b.go"])[0], ["a.py: # new", "b.go: // new"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_renamed_file_against_old_path(self):
        self.git("init", "-q")
        body = "# old one\n" + "x = 1\n" * 10 + "# old two\n"
        self.write("a.py", body)
        self.write("keep.py", "y = 1\n")
        self.commit("a.py", "keep.py")
        (self.root / "pkg").mkdir()
        self.git("mv", "a.py", "pkg/b.py")
        self.write("pkg/b.py", body + "# новое\n")
        self.write("keep.py", "y = 1\n# k\n")
        lines, _, _, _ = comments.extract(self.root, ["keep.py", "pkg/b.py"])
        self.assertEqual(lines, ["keep.py: # k", "pkg/b.py: # новое"])
        # Переименование, закоммиченное за реплику, — против базы на старте.
        base = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        self.git("add", "-A")
        self.git("commit", "-qm", "mv")
        self.assertEqual(comments.extract(self.root, ["pkg/b.py"], base)[0], ["pkg/b.py: # новое"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_rename_sources_limit(self):
        self.git("init", "-q")
        body = "# old one\n" + "x = 1\n" * 10 + "# old two\n"
        self.write("a.py", body)
        self.commit("a.py")
        self.git("mv", "a.py", "b.py")
        self.write("b.py", body + "# новое\n")
        # Предел включительно: один удалённый путь при пределе 1 — пара находится, при пределе 0 — файл целиком.
        with mock.patch.object(comments, "MAX_RENAME_SOURCES", 1):
            self.assertEqual(comments.extract(self.root, ["b.py"])[0], ["b.py: # новое"])
        with mock.patch.object(comments, "MAX_RENAME_SOURCES", 0):
            self.assertEqual(comments.extract(self.root, ["b.py"])[0],
                             ["b.py: # old one", "b.py: # old two", "b.py: # новое"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_new_tracked_file_without_rename_is_whole(self):
        self.git("init", "-q")
        self.write("gone.py", "x = 1\n")
        self.write("a.py", "y = 2\n")
        self.commit("gone.py", "a.py")
        self.git("rm", "-q", "gone.py")
        self.write("n.py", "# one\n# two\n")
        self.git("add", "n.py")
        self.assertEqual(comments.extract(self.root, ["n.py"])[0], ["n.py: # one", "n.py: # two"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_plain_mv_untracked_target_is_whole_file(self):
        """Остаток: путь после mv без git add не отслеживается, пары с удалённым у git нет."""
        self.git("init", "-q")
        self.write("a.py", "# old\n")
        self.commit("a.py")
        os.rename(self.root / "a.py", self.root / "b.py")
        self.assertEqual(comments.extract(self.root, ["b.py"])[0], ["b.py: # old"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_added_line_looking_like_diff_header(self):
        self.git("init", "-q")
        self.write("a.c", "int a;\n" * 10)
        self.commit("a.c")
        lines = ["int a;"] * 10
        lines.insert(1, "++ i;")
        lines.insert(8, "// c")
        self.write("a.c", "\n".join(lines) + "\n")
        self.assertEqual(comments.extract(self.root, ["a.c"])[0], ["a.c: // c"])

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
            lines, _, _, _ = comments.extract(self.root, names + ["u.py"])
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
        lines, _, _, _ = comments.extract(self.root, names)
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
        lines, _, _, _ = comments.extract(self.root, ["a.go"])
        self.assertEqual(lines, ["a.go: // new"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_closing_docstring_edit_does_not_hide_later_hunk(self):
        self.git("init", "-q")
        old = 'def f():\n    """Doc\n    """\n    return 1\n\n\nx = 2\n'
        self.write("a.py", old)
        self.commit("a.py")
        self.write("a.py", old.replace('    """\n    return', '    end."""\n    return') + "# new comment\ny = 3\n")
        lines, _, _, _ = comments.extract(self.root, ["a.py"])
        self.assertEqual(lines, ['a.py: end."""', "a.py: # new comment"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_block_opened_in_added_line_closes_in_unchanged(self):
        self.git("init", "-q")
        self.write("a.c", "/* head\n   body */\nint a;\nint b;\n")
        self.commit("a.c")
        self.write("a.c", "/* new head\n   body */\nint a;\nint b;\nint c;\n")
        lines, _, _, _ = comments.extract(self.root, ["a.c"])
        self.assertEqual(lines, ["a.c: /* new head"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_added_line_inside_unchanged_block(self):
        self.git("init", "-q")
        self.write("a.go", "/*\n old\n*/\nx := 1\n")
        self.commit("a.go")
        self.write("a.go", "/*\n old\n new\n*/\nx := 1\n")
        self.assertEqual(comments.extract(self.root, ["a.go"])[0], ["a.go: new"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_form_feed_and_carriage_return_keep_line_numbers(self):
        self.git("init", "-q")
        (self.root / "a.go").write_bytes(b"a := 1 \r// old\nb := 2\r\n")
        self.commit("a.go")
        (self.root / "a.go").write_bytes(b"a := 1 \r// old\nb := 2 // a\x0cb\r\n")
        self.assertEqual(comments.extract(self.root, ["a.go"])[0], ["a.go: // a\x0cb"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_non_utf8_file_name(self):
        self.git("init", "-q")
        name = os.fsdecode(b"b\xff.py")
        try:
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as f:
                f.write("# old\n")
        except OSError:
            self.skipTest("файловая система не принимает имя не в UTF-8")
        self.commit(name)
        with open(os.path.join(self.tmp.name, name), "a", encoding="utf-8") as f:
            f.write("# new\n")
        new = os.fsdecode(b"n\xfe.py")
        with open(os.path.join(self.tmp.name, new), "w", encoding="utf-8") as f:
            f.write("# untracked\n")
        lines, truncated, _, _ = comments.extract(self.root, [name, new])
        self.assertEqual(lines, [f"{name}: # new", f"{new}: # untracked"])
        self.assertFalse(truncated)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_without_head_whole_tracked_file(self):
        self.git("init", "-q")
        self.write("a.py", "# staged\nx = 1\n")
        self.git("add", "a.py")
        lines, _, _, _ = comments.extract(self.root, ["a.py"])
        self.assertEqual(lines, ["a.py: # staged"])


class UnquoteTest(unittest.TestCase):
    def test_malformed_escape_is_literal(self):
        self.assertEqual(comments._unquote('"a\\1z.py"'), "a\\1z.py")
        self.assertEqual(comments._unquote('"caf\\303\\251.py"'), "café.py")

    def test_non_utf8_bytes_are_surrogates(self):
        self.assertEqual(comments._unquote('"b\\377\\t.py"'), os.fsdecode(b"b\xff\t.py"))


class CodeNamesTest(unittest.TestCase):
    def test_all_code_names_have_comment_syntax(self):
        for name in common.CODE_NAMES:
            self.assertEqual(comments.extract("/nonexistent", [name])[2], [], name)


class LinearParseTest(unittest.TestCase):
    def test_unclosed_quotes_and_backslashes(self):
        started = time.monotonic()
        self.assertEqual(comments.comment_lines('"\\' * 20000 + " // c", "js"), ["// c"])
        self.assertEqual(comments.comment_lines("'\\" * 20000, "js"), [])
        self.assertEqual(comments.comment_lines('"' + "\\a" * 20000 + '" // c', "js"), ["// c"])
        self.assertLess(time.monotonic() - started, 3)

    def test_many_triple_quotes_on_one_line(self):
        started = time.monotonic()
        self.assertEqual(comments.comment_lines('x = """a""" ' * 250000, "py"), [])
        self.assertLess(time.monotonic() - started, 3)

    def test_long_prefix_before_many_heredoc_openers(self):
        # Решение «heredoc или сдвиг» смотрит только хвост строки перед «<<», а не весь префикс.
        cases = (('my $d = "' + "ab" * 50000 + '"; print $fh <<EOF;\nbody\nEOF\n', "pl", []),
                 ("x" * 200000 + " <<b " * 2000, "rb", []),
                 ("print {$" + "f" * 200000 + "} <<B " * 2000 + "\n# not\nB\n", "pl", []),
                 (" " * 200000 + "x <<b " * 20000, "rb", []),
                 # Первые тройные кавычки — docstring: строка идёт в вывод целиком.
                 (" " * 200000 + '"""""" ' * 20000, "py", [(1, ('"""""" ' * 20000).strip())]))
        for text, ext, expected in cases:
            started = time.monotonic()
            self.assertEqual(comments._comments(text, ext, time.monotonic() + 30), expected, ext)
            self.assertLess(time.monotonic() - started, 1, ext)

    def test_new_language_constructs_are_linear(self):
        # Вложенные блоки, символьные литералы, REM, «"» Vim, heredoc PHP, @doc Elixir, блок-дескриптор Perl.
        cases = (("/* " * 100000 + "*/ " * 100000 + "\n", "rs"), ("(* " + "(*)" * 100000 + "\n", "fs"),
                 ("/* " + "*/ /*" * 100000 + "\n", "kt"), ("#[" + " #[ ]#" * 50000 + "\n", "nim"),
                 ("$" * 200000, "erl"), ("\\" * 200000, "clj"), ("?" * 200000, "el"), ("?#" * 100000, "rb"),
                 ("remx " * 50000, "bat"), (" " * 100000 + "a ::" * 20000, "cmd"), ("x rem" * 50000, "vb"),
                 (' "a' * 50000, "vim"), ('x"' * 50000 + ' "', "vim"), ("<<<A " * 50000, "php"),
                 ("@doc " * 50000, "ex"), ("{" * 100000 + "} <<B" * 20000, "pl"),
                 ("print " + "{$a->{b}}<<B " * 20000, "pl"), ('#' * 100000 + '"', "swift"),
                 ('r"' * 100000, "nim"),
                 # Регулярки, slashy-строки, теги JSX: незакрытые литералы, классы, имена тегов, вложенность.
                 ("(/[" * 100000, "js"), ("=/" * 100000, "ts"), ("(/\\" * 100000, "js"),
                 (" " * 100000 + "/a/" * 30000, "js"),
                 ("return" * 50000 + "/", "js"), ("(/" * 100000, "groovy"), ("=$/" * 100000, "gradle"),
                 ("$/" + "$$/" * 50000, "groovy"), ("(<a>" * 50000, "jsx"), ("=<" * 100000, "tsx"),
                 ("(<" + "a" * 100000 + " " * 100000 + "=" * 1000, "tsx"), ("<a " * 100000, "jsx"),
                 ("x = <p>" + "<b>{" * 30000 + "\n" + "}</b>" * 30000 + "</p>\n", "jsx"),
                 ("(<T,>" * 50000, "tsx"), ("(<a>\n" * 3000 + "// c\n" * 3000, "js"))
        for text, ext in cases:
            started = time.monotonic()
            comments._comments(text, ext, time.monotonic() + 30)
            self.assertLess(time.monotonic() - started, 1, ext)

    def test_deadline_during_parse_files_without_check(self):
        common._reset()
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        pathlib.Path(root, "a.py").write_text("# c\n" * 3000, encoding="utf-8")
        calls = []

        def clock():
            calls.append(1)
            return 0 if len(calls) <= 2 else 100

        with mock.patch.object(comments.time, "monotonic", side_effect=clock):
            lines, truncated, unknown, late = comments.extract(root, ["a.py"], None, None, 50)
        self.assertEqual((lines, truncated, unknown, late), ([], False, [], ["a.py"]))
        self.assertGreater(len(calls), 2)
        self.assertEqual(common._messages,
                         ["planka: строки комментариев не извлечены в срок, файлов кода без проверки: 1"])
        common._reset()

    def test_deadline_inside_one_long_line(self):
        common._reset()
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        pathlib.Path(root, "a.js").write_text('x = "a"; ' * 200000 + "// c\n", encoding="utf-8")
        ticks = iter(range(10 ** 9))
        seen = []

        def clock():
            seen.append(next(ticks))
            return seen[-1]

        # Часы идут на единицу за вызов; срок истекает, когда в строке ещё сотни тысяч позиций.
        with mock.patch.object(comments.time, "monotonic", side_effect=clock):
            lines, _, _, late = comments.extract(root, ["a.js"], None, None, 50)
        self.assertEqual((lines, late), ([], ["a.js"]))
        # Разбор остановился на сроке, а не дошёл до конца строки: проверок срока на несколько порядков
        # меньше позиций.
        self.assertLess(len(seen), 200)
        common._reset()
