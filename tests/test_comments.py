import codecs
import errno
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
from tests import helpers  # noqa: E402


FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "comments"
# Номера строк комментариев каждого файла корпуса; путь — относительно FIXTURES, синтаксис — по имени файла.
CORPUS = {
    "cpython-makefile/Makefile": [1, 8, 9, 29, 32, 34, 36, 40],
    "create-vite-react/App.jsx": [1],
    "create-vite-vue/HelloWorld.vue": [1],
    "esp-idf-ci/idf_ci.toml": [1, 10, 26],
    "esp-idf-gitlab/pre_check.yml": [1, 3, 4, 10, 11, 22, 34, 44, 54],
    "ffmpeg-doc/libswscale.html": [1, 4],
    "flutter-gradle-continuation/flutter.gradle": [1, 2, 3, 4, 6, 7, 8],
    "flutter-gradle/gradle.dart": [1],
    "flutter-issue-form/04_performance_others.yml": [1],
    "flutter-resolve-deps/resolve_dependencies.gradle.kts": [1, *range(4, 17)],
    "fzf-tmux/fzf-tmux.sh": [1, 7, 8, 9, 12, 22, 37, 42],
    "go-template-lex/lex.go": [1, 11, 31, 38],
    "groovy-spec-slashy/SyntaxTest.groovy": [1, 3, 6, 8, 11, 13, 19, 21, 26],
    "grpc-gateway-ci/ci.yml": [1, 6, 7, 14, 21, 22, 26],
    "kernel-make/Makefile": [1],
    "kernel-rustdoc/rustdoc_test_gen.rs": [*range(1, 10)],
    "kernel-unifdef/unifdef.c": [1, 8, *range(10, 21), 22, 23, 59, 64, 65, 66],
    "lib-pq/20-config.sql": [1, 12, 13],
    "mbedtls-readthedocs/readthedocs.yaml": [*range(1, 5), 6, 9, 14, 27, 32, 37],
    "mermaid-zenuml-render/render.js": [1],
    "mldsa-hol-light/hol_light.yml": [1, 2, *range(10, 14), 21],
    "moby-dockerfile/Dockerfile": [1, 2, 9, 10, 13, 17, 21, 24, *range(31, 35), *range(37, 44), 48, 51, 52, 58,
        *range(61, 65)],
    "moby-swagger/swagger.yaml": [1],
    "nimble-log2smtest/log2smtest.rb": [1],
    "npm-install/install.js": [1, 6, 7, 8, 10, 15, 16, 46, 49, 52],
    "openssh-findssl/findssl.sh": [*range(1, 7), 10, 11, 12, 15, 16, 17, 24, 25, 26, 33],
    "perl-cpan-distribution/Distribution.pl": [1, 26, 33, 46, 52, 53],
    "perl-encode-kr/2022_KR.pl": [1, 5, 6, 7, 8],
    "perl-mime-header/Header.pl": [1, 3, 12, 24, 26, 35, 52],
    "perl-module-load/Load.pl": [1, 8],
    "perl-proxysubs/ProxySubs.pl": [1],
    "pygments-lisp/lisp.py": [1, 18],
    "python-shlex/shlex.py": [1, 2, *range(4, 10), 20, 59, 61, 63],
    "wordpress-twentytwelve/custom-header.php": [1, *range(3, 10), 13, 18, 22, 28, 32, *range(44, 49), 70,
        *range(84, 91)],
    "wordpress-twentytwelve/header.php": [1, *range(3, 12), *range(13, 20), 21, 28, 29, 30, 31, 55, 60],
    "react-virtual/index.tsx": [1, *range(9, 14), *range(22, 44), *range(45, 55)],
    "ruby-rjit/insn_compiler.rb": [1, 4, 7, 21, 48, 52, 53],
    "sveltekit-sverdle/page.svelte": [1, 79],
    "rubygems-specification/specification.rb": [1, 4, 6, 8, 12, *range(31, 35)],
    "ts-dedent/index.ts": [1, 8, 14, 25, 32, 35, 39, 43],
}


class CorpusTest(unittest.TestCase):
    """Корпус настоящих входов разборщика: выдержки публичных проектов, источник — первой строкой файла."""

    def test_every_fixture_has_expectation(self):
        files = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file()}
        self.assertEqual(files, set(CORPUS))

    def test_corpus(self):
        for rel, expected in CORPUS.items():
            with self.subTest(rel):
                text = (FIXTURES / rel).read_text(encoding="utf-8")
                found = comments._comments(text, comments._ext(rel))
                self.assertEqual([n for n, _ in found], expected)
                lines = text.split("\n")
                # Строка комментария — хвост своей строки: разбор не сдвигает начало комментария внутрь кода.
                for n, c in found:
                    self.assertTrue(lines[n - 1].rstrip().endswith(c), (n, c))
                self.assertIn("источник:", found[0][1])

    def test_corpus_through_extract(self):
        # Вне git файл берётся целиком: extract отдаёт те же строки с путём.
        for rel in CORPUS:
            with self.subTest(rel):
                text = (FIXTURES / rel).read_text(encoding="utf-8")
                expected = [f"{rel}: {c}" for _, c in comments._comments(text, comments._ext(rel))]
                self.assertEqual(comments.extract(str(FIXTURES), [rel], None), (expected, False, [], []))

class CommentLinesTest(unittest.TestCase):
    def test_c_family(self):
        src = "x := 1 // trailing\n// line one\n/* block\n   two */\ns := \"// not\"\n"
        self.assertEqual(helpers.comment_lines(src, "go"),
                         ["// trailing", "// line one", "/* block", "two */"])

    def test_hash_and_docstring(self):
        src = "#!/usr/bin/env python3\n# note\ndef f():\n    \"\"\"Doc line.\"\"\"\n    s = '#'\n    return 1  # tail\n"
        self.assertEqual(helpers.comment_lines(src, "py"),
                         ["# note", '"""Doc line."""', "# tail"])

    def test_shell_hash_expansions_are_code(self):
        src = "echo $#\necho ${#a[@]}\necho $# # c\n"
        for ext in ("sh", "bash", "zsh"):
            self.assertEqual(helpers.comment_lines(src, ext), ["# c"])

    def test_shell_hash_inside_word_is_code(self):
        self.assertEqual(helpers.comment_lines("x=${var#prefix}\ny=a#b # c\n", "sh"), ["# c"])

    def test_triple_quoted_string_is_not_docstring(self):
        src = 'q = """SELECT"""\nsql = """\n# not a comment\n"""\n# real\ndef f():\n    r"""Doc."""\n'
        self.assertEqual(helpers.comment_lines(src, "py"), ["# real", 'r"""Doc."""'])

    def test_triple_quote_inside_comment(self):
        src = 'x = 1  # see the """ quoting\ny = 2\n# real comment\n'
        self.assertEqual(helpers.comment_lines(src, "py"), ['# see the """ quoting', "# real comment"])

    def test_backticks_and_raw_strings_are_strings(self):
        self.assertEqual(helpers.comment_lines("const u = `https://e.com/x`; // c\n", "ts"), ["// c"])
        self.assertEqual(helpers.comment_lines('let s = r#"a " // not"#; // yes\n', "rs"), ["// yes"])

    def test_yaml_hash_inside_word_is_value(self):
        self.assertEqual(helpers.comment_lines("url: http://a/b#frag\nk: 1 # c\n", "yaml"), ["# c"])

    def test_make_recipe_and_dockerfile_hash_inside_word(self):
        # Строка рецепта Make уходит в shell; в Dockerfile «#» внутри слова — значение. Формы — Makefile ядра
        # Linux 7.2 (filechk_version.h), Dockerfile moby 28.5 (ARG DELVE_SUPPORTED).
        src = ("define filechk_version.h\n\techo '#define KERNEL_VERSION(a,b,c) (((a) << 16) +  \\\n"
               "\t((c) > 255 ? 255 : (c)))';  \\\n\techo \\#define LINUX_VERSION_MAJOR $(VERSION)\nendef\n"
               "x := 1 # c1\nall:\n\t@echo hi # c2\n")
        for name in ("Makefile", "GNUmakefile", "rules.mk"):
            self.assertEqual(helpers.comment_lines(src, comments._ext(name)), ["# c1", "# c2"], name)
        src = "ARG DELVE_SUPPORTED=${TARGETPLATFORM#linux/amd64}\nRUN make # c1\n# c2\n"
        self.assertEqual(helpers.comment_lines(src, comments._ext("Dockerfile")), ["# c1", "# c2"])

    def test_make_recipe_prefixes_and_hash_outside_recipe(self):
        # Make снимает со строки рецепта префиксы «@», «-», «+» и отдаёт shell «# текст»; вне рецепта «#» — комментарий
        # и внутри слова.
        src = "all:\n\t@# Clean the apidoc\n\t@echo hi\n\t-@# ignore errors note\n\t+ @ #c3\n\t@echo a#b\n"
        self.assertEqual(comments._comments(src, "makefile"),
                         [(2, "# Clean the apidoc"), (4, "# ignore errors note"), (5, "#c3")])
        self.assertEqual(comments._comments("CFLAGS = -O2#opt level\n", "makefile"), [(1, "#opt level")])
        # Пробел между префиксами: «- @# c».
        self.assertEqual(comments._comments("all:\n\t- @# c\n", "makefile"), [(2, "# c")])

    def test_shell_heredoc_body_is_data(self):
        src = "cat <<EOF > s.md\n# Heading\nEOF\n# real\n"
        self.assertEqual(helpers.comment_lines(src, "sh"), ["# real"])

    def test_shell_heredoc_inside_open_backtick_is_data(self):
        # `…` не закрыта до конца строки: тело heredoc в ней — данные (depcheck.heredocs).
        src = "x=`cat <<EOF\nbody # not a comment\nEOF\n`\n# real\n"
        self.assertEqual(helpers.comment_lines(src, "sh"), ["# real"])
        # Тело heredoc из `…` bash читает раньше тела heredoc строки: тело B, затем тело A.
        src = "cat <<A; x=`cat <<B\nbodyB\nB\n`\nbodyA\nA\necho \"x=[$x]\"\n# real\n"
        self.assertEqual(helpers.comment_lines(src, "sh"), ["# real"])

    def test_block_comments_lua_haskell(self):
        self.assertEqual(helpers.comment_lines("--[[ block\nline two\n]]\nx = 1 -- tail\n", "lua"),
                         ["--[[ block", "line two", "]]", "-- tail"])
        self.assertEqual(helpers.comment_lines("{- block\nline -}\nx = 1 -- tail\n", "hs"),
                         ["{- block", "line -}", "-- tail"])

    def test_vue_script_comments(self):
        src = "<template>\n<!-- html -->\n</template>\n<script>\n// js comment\n</script>\n"
        self.assertEqual(helpers.comment_lines(src, "vue"), ["<!-- html -->", "// js comment"])

    def test_vue_and_svelte_blocks(self):
        # Однофайловый компонент: вне «<script>» и «<style>» — разметка, «//» в тексте — не комментарий; «<script>» —
        # JS или TS по lang, «<style>» — CSS или препроцессор по lang; «</script» закрывает блок и внутри строки.
        src = ("<template>\n  <a href=x>see http://example.com</a> /* not */\n  <!-- c1 -->\n</template>\n"
               "<script setup lang=\"ts\" generic=\"T extends Record<string, unknown>\">\n"
               "const re = /\\/\\//; // c2\nlet t: T // c3\n</script>\n"
               "<style lang=\"scss\">\n.a { b: url(http://x) } // c4\n</style>\n<style>\n.b {} // not\n</style>\n")
        for ext in ("vue", "svelte"):
            self.assertEqual(comments._comments(src, ext),
                             [(3, "<!-- c1 -->"), (6, "// c2"), (7, "// c3"), (10, "// c4")], ext)
        self.assertEqual(helpers.comment_lines("<p>a // b</p>\n<script>let s = '</script>'; // c\n", "svelte"), [])
        self.assertEqual(helpers.comment_lines("<!-- <script> -->\n<p>a // b</p>\n", "vue"), ["<!-- <script> -->"])
        self.assertEqual(helpers.comment_lines("<SCRIPT>// a\n</Script>// b\n<script\n", "vue"), ["// a"])

    def test_vue_and_svelte_markup_code(self):
        # Код разметки — «{…}» Svelte (и в теге) и «{{…}}» Vue; «{/if}», «{:else}» Svelte, одиночная «{» Vue и пустой
        # элемент HTML без закрытия («<br>») разбор кода не сбивают.
        src = ("<br>\n{#if a}<p on:click={() => { // c1\n}}>x // y {/if}</p>\n{:else}\n<p>{a /* c2 */} // z</p>\n"
               "{/if} // w\n")
        self.assertEqual(comments._comments(src, "svelte"), [(2, "// c1"), (5, "/* c2 */} // z</p>")])
        src = "<p>{ a // x</p>\n<p>{{ a /* c1 */ }} // y</p>\n<p :a=\"{b}\">{{ '}}' }} http://e.com</p>\n"
        self.assertEqual(comments._comments(src, "vue"), [(2, "/* c1 */ }} // y</p>")])

    def test_vue_directive_and_svelte_attribute_code(self):
        # Значение директивы Vue («v-», «:», «@», «#», «.») — выражение JavaScript до первой такой же кавычки, с
        # раскрытыми сущностями HTML; атрибут Svelte в кавычках — текст с кодом «{…}». Прочие атрибуты — данные.
        self.assertEqual(comments._comments('<p @click="a() // c">x</p>\n', "vue"), [(1, "// c")])
        self.assertEqual(comments._comments('<p :x="[\n 1, // c1\n 2 /* c2 */]" title="a // b">\n', "vue"),
                         [(2, "// c1"), (3, "/* c2 */]")])
        src = '<p v-if="a &amp;&amp; \'//\' /* c */" .p=\'f("//")\' #s="{ a }">\n'
        self.assertEqual(comments._comments(src, "vue"), [(1, "/* c */")])
        self.assertEqual(comments._comments('<p :x="a &#47;&#47; c">\n', "vue"), [(1, "// c")])
        # «#» — слот, «.» — привязка свойства: их значение — тоже выражение.
        self.assertEqual(comments._comments('<p #s="{ a } /* c */">\n', "vue"), [(1, "/* c */")])
        self.assertEqual(comments._comments('<p .p="a // c">\n', "vue"), [(1, "// c")])
        self.assertEqual(comments._comments('<p title="a // b" href=\'//e.com\'>x</p>\n', "vue"), [])
        src = '<p on:click="{() => {\n a() // c\n}}" title="{a} // b" class="x // y">\n'
        self.assertEqual(comments._comments(src, "svelte"), [(2, "// c")])

    def test_sfc_tag_opens_with_ascii_letter(self):
        # Тег разметки компонента открывает «<» и буква ASCII (токенизатор HTML): атрибут-директива за именем тега
        # не делает тег текстом; «<» перед пробелом, цифрой и не-ASCII буквой — текст.
        self.assertEqual(helpers.comment_lines("<p :x=\"'{{'\">a // b</p>\n", "vue"), [])
        self.assertEqual(helpers.comment_lines("<p>a < b, 1 <2 \"// x\" <ф \"// y\"</p>\n", "vue"), [])

    def test_sql_and_html(self):
        self.assertEqual(helpers.comment_lines("select 1 -- c\n", "sql"), ["-- c"])
        self.assertEqual(helpers.comment_lines("<a>\n<!-- hidden -->\n", "html"), ["<!-- hidden -->"])

    def test_slash_families(self):
        for ext in ("php", "groovy", "gradle", "proto", "sol", "zig"):
            # Код PHP — в блоке «<?php»: вне него HTML.
            src = "<?php x = 1 // c\n" if ext == "php" else "x = 1 // c\n"
            self.assertEqual(helpers.comment_lines(src, ext), ["// c"], ext)

    def test_language_variants(self):
        for ext in ("mjs", "cjs", "mts", "cts", "cxx", "hh", "hxx"):
            self.assertEqual(helpers.comment_lines("x = 1 // c\n", ext), ["// c"], ext)
        self.assertEqual(helpers.comment_lines('# c\ndef f():\n    """Doc."""\n', "pyi"), ["# c", '"""Doc."""'])
        self.assertEqual(helpers.comment_lines("set(X 1) # c\n", "cmake"), ["# c"])

    def test_hash_families(self):
        for ext in ("tf", "nix", "r", "jl", "ex", "exs"):
            self.assertEqual(helpers.comment_lines("x = 1 # c\n", ext), ["# c"], ext)

    def test_unknown_ext_is_empty(self):
        self.assertEqual(helpers.comment_lines("// x\n# y\n", "bin"), [])
        self.assertEqual(helpers.comment_lines("", "go"), [])

    def test_code_after_closed_string_is_scanned(self):
        self.assertEqual(helpers.comment_lines('s = """a\n# not\n"""  # yes\n', "py"), ["# yes"])
        self.assertEqual(helpers.comment_lines('x = 1 /* a */ y = 2 // b\n', "c"), ["/* a */ y = 2 // b"])

    def test_second_block_after_closed_block(self):
        self.assertEqual(helpers.comment_lines("/* a */ /* b\nmid\n*/\nint x;\n", "c"),
                         ["/* a */ /* b", "mid", "*/"])

    def test_docstring_line_reported_once(self):
        src = 'def f():\n    """Doc."""  # tail\n    """Open # x\n    more\n    """\n'
        self.assertEqual(helpers.comment_lines(src, "py"), ['"""Doc."""  # tail', '"""Open # x', "more", '"""'])

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
            self.assertEqual(helpers.comment_lines(src, ext), [expected], ext)

    def test_rust_raw_string_spans_lines(self):
        self.assertEqual(helpers.comment_lines('let s = r#"a\n// not "\n"#; // yes\n', "rs"), ["// yes"])

    def test_apostrophe_is_not_a_string(self):
        self.assertEqual(helpers.comment_lines("fn f<'a>(x: &'a str) // it's a comment\n", "rs"),
                         ["// it's a comment"])
        self.assertEqual(helpers.comment_lines("let c = '/'; // c\n", "rs"), ["// c"])
        self.assertEqual(helpers.comment_lines("key: don't # it's here\nq: 'it''s # not'\n", "yaml"),
                         ["# it's here"])
        self.assertEqual(helpers.comment_lines("<p>Don't</p><!-- it's a note -->\n", "html"),
                         ["<!-- it's a note -->"])
        self.assertEqual(helpers.comment_lines("<p>Don't</p><!-- it's a note -->\n", "vue"),
                         ["<!-- it's a note -->"])
        self.assertEqual(helpers.comment_lines("f x' = x' -- it's\n", "hs"), ["-- it's"])

    def test_more_syntaxes(self):
        self.assertEqual(helpers.comment_lines("; top\nk = v ; tail\nurl = a;b\n# h\n", "ini"),
                         ["; top", "; tail", "# h"])
        self.assertEqual(helpers.comment_lines("; c\n", "cfg"), ["; c"])
        for ext in ("sql", "tf", "nix"):
            self.assertEqual(helpers.comment_lines("/* a\nb */\nx\n", ext), ["/* a", "b */"], ext)
        self.assertEqual(helpers.comment_lines("x = 1 // c\n", "tf"), ["// c"])
        self.assertEqual(helpers.comment_lines("<?php\n#[Attr]\n$x = 1; # c\n", "php"), ["# c"])
        self.assertEqual(helpers.comment_lines("<# a\nb #>\n$x = 1 # c\n", "ps1"), ["<# a", "b #>", "# c"])
        self.assertEqual(helpers.comment_lines("#= a\nb =#\nx = 1 # c\n", "jl"), ["#= a", "b =#", "# c"])
        self.assertEqual(helpers.comment_lines("x = <<EOT\n# not\n  EOT\n# c\n", "tf"), ["# c"])
        self.assertEqual(helpers.comment_lines('const s = \\\\ // not\n// c\n', "zig"), ["// c"])

    def test_styles(self):
        src = 'a { b: url(http://x/y); c: "/* no */"; } /* c */\n'
        self.assertEqual(helpers.comment_lines(src, "css"), ["/* c */"])
        self.assertEqual(helpers.comment_lines("a { b: 1 } // not css\n", "css"), [])
        src = '// one\n$u: url(http://y); // two\n$s: "// no";\n/* b\n   c */\n'
        for ext in ("scss", "sass", "less"):
            self.assertEqual(helpers.comment_lines(src, ext), ["// one", "// two", "/* b", "c */"], ext)

    def test_json_manifests(self):
        src = '{\n  "a": "// no /* no */" // yes\n  /* b */\n}\n'
        for name in ("package.json", "composer.json"):
            self.assertEqual(helpers.comment_lines(src, name), [], name)
        for name in ("tsconfig.json", "jsconfig.json", "deno.json"):
            self.assertEqual(helpers.comment_lines(src, name), ["// yes", "/* b */"], name)
        self.assertEqual(helpers.comment_lines('module x // m\nreplace a => "// no"\n', "go.mod"), ["// m"])

    def test_shebang_only_on_first_line(self):
        self.assertEqual(helpers.comment_lines("#!/bin/sh\n#!not shebang\n", "sh"), ["#!not shebang"])

    def test_line_numbers(self):
        self.assertEqual(comments._comments("x = 1\n# a\n\n/* no */\n'\"\"\"'\n# b\n", "py"),
                         [(2, "# a"), (6, "# b")])


class LanguageSyntaxTest(unittest.TestCase):
    """Каждый язык: маркер комментария внутри литерала — код, настоящий комментарий, блок."""

    def check(self, ext, src, expected):
        self.assertEqual(helpers.comment_lines(src, ext), expected, ext)

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
        # Модуль Perl — синтаксис Perl: оператор-кавычка прячет «#».
        self.check("pm", "my @w = qw(a #b); # c\n", ["# c"])

    def test_syntax_table_entries(self):
        # Каждая запись таблицы: литерал или блок прячет маркер, без записи вывод другой.
        cases = (("sql", "select '--x' -- c\n", ["-- c"]),
                 ("proto", "x = '/* no */' /* c */\n", ["/* c */"]), ("sol", "x = '/* no */' /* c */\n", ["/* c */"]),
                 ("java", "char q = '\"'; s = \"//x\"; // c\n", ["// c"]),
                 ("zig", "const q = '\"'; const s = \"//x\"; // c\n", ["// c"]),
                 ("lua", "s = '--x' -- c\n", ["-- c"]), ("xml", "<a/><!-- c -->\n", ["<!-- c -->"]),
                 ("m", "x = 1; // c\n", ["// c"]), ("mm", "x = 1; // c\n", ["// c"]),
                 ("svelte", "<p>Don't</p><!-- c -->\n<script>// d\n", ["<!-- c -->", "// d"]),
                 ("kt", 'val s = """\n// not\n""" // c\n', ["// c"]), ("erl", 'S = """\n% not\n""". % c\n', ["% c"]),
                 ("cs", 'var s = """\n// not\n"""; // c\n', ["// c"]),
                 ("dart", "var s = '''\n// not\n'''; // c\n", ["// c"]),
                 ("groovy", "def s = '''\n// not\n''' // c\n", ["// c"]),
                 ("swift", 'let s = """\n// not\n""" // c\n', ["// c"]),
                 # Escape строк: обратной косой нет в литеральных строках toml, ps1, go, go.mod; есть в php.
                 ("toml", "k = ['C:\\', '# x'] # c\n", ["# c"]), ("ps1", "$p = @('C:\\', '# x') # c\n", ["# c"]),
                 ("go", "s := []string{`C:\\`, `// x`} // c\n", ["// c"]),
                 ("go.mod", "replace a => `C:\\` `// x` // c\n", ["// c"]),
                 ("php", "<?php $s = 'it\\'s # x'; # c\n", ["# c"]),
                 # Escape в символьном литерале: «'\"'» не открывает строку.
                 ("c", "char q = '\\\"'; s = \"//x\"; // c\n", ["// c"]),
                 # «-» в имени heredoc Terraform.
                 ("tf", "x = <<EOT-1\n# not\nEOT-1\n# c\n", ["# c"]))
        for ext, src, expected in cases:
            self.check(ext, src, expected)

    def test_small_branches(self):
        # «?» после слова — метод-предикат Ruby, а не символьный литерал.
        self.check("rb", "ok = a.empty?# c\n", ["# c"])
        # Глубина вложенных блоков переносится через строки.
        self.check("rs", "/* a /* b\n*/ still\n*/\nlet x = 1; // c\n", ["/* a /* b", "*/ still", "*/", "// c"])
        # Escape «''\» в строке Nix берёт и следующий знак.
        self.check("nix", "x = ''a ''\\'' b''; # c\ny = ''z''; # d\n", ["# c", "# d"])
        # Длинная строка Lua закрывается скобками с тем же числом «=».
        self.check("lua", "s = [==[\n]] -- not\n]==] -- yes", ["-- yes"])
        # Голое REM — комментарий.
        self.check("vb", "REM\nx = 1\n", ["REM"])
        self.check("bat", "REM\n", ["REM"])
        # Dollar-slashy Groovy: «$/» — escape, «$/$» не закрывает литерал.
        self.check("groovy", "def u = $/a $/$ // b/$ // c\n", ["// c"])
        # ~S в @doc — без escape: «\"» закрывает строку.
        self.check("ex", '@doc ~S"C:\\"\nx = "# not"\n', ['@doc ~S"C:\\"'])
        # POD из одной строки «=cut» и «=cutx» — не конец блока.
        self.check("pl", "=cut\nmy $x; # c\n", ["=cut", "# c"])
        self.check("pl", "=pod\n=cutx\nmy $x; # not\n=cut\n", ["=pod", "=cutx", "my $x; # not", "=cut"])

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
            first, second = src.split("\n")[:2]
            self.assertEqual(comments._comments(src, ext), [(1, first), (2, second)], ext)

    def test_c_block_does_not_nest(self):
        self.check_lines("c", "/* a /* b */\nint x; // c\n", ["/* a /* b */", "// c"])
        self.check_lines("groovy", "/* a /* b */\nint x // c\n", ["/* a /* b */", "// c"])

    def check_lines(self, ext, src, expected):
        self.assertEqual(helpers.comment_lines(src, ext), expected, ext)

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

    def test_php_template_html_outside_blocks(self):
        # Вне «<?php … ?>», «<?= … ?>», «<? … ?>» — HTML: «#» и «//» в тексте — данные, «<!-- -->» — комментарий;
        # «?>» кончает строчный комментарий; «<style>» и «<script>» — CSS и JavaScript, блоки PHP в них — код PHP.
        cases = (("<style>#masthead { color: #fff; }</style>\n", []),
                 ("<p>Order #12, see https://example.com</p>\n", []),
                 ("<?php echo 1; // c ?> <p>#not</p>\n<?= $a /* d */ ?> // no\n",
                  [(1, "// c ?> <p>#not</p>"), (2, "/* d */ ?> // no")]),
                 ("<? if ($a): # c ?>x<? endif ?>\n<?xml version='1.0' ?><!-- e -->\n",
                  [(1, "# c ?>x<? endif ?>"), (2, "<!-- e -->")]),
                 ("<?php $s = '?>'; /* ?> */ ?>#x\n", [(1, "/* ?> */ ?>#x")]),
                 ("<script>\nvar a = <?php echo $x; // p ?>; // j\n</script> // no\n"
                  "<script type=\"text/html\">// t\n</script>\n", [(2, "// p ?>; // j"), (2, "// j")]),
                 ('<style id="<?php echo $i ?>">a { b: #<?php echo $c ?>; } /* s */\n<?php // q\n?></style>#x\n',
                  [(1, "/* s */"), (2, "// q")]),
                 ("<script\n src='a>b'><?php\n// p\n?>/* j */</script>\n<script>\n// u\n",
                  [(3, "// p"), (4, "/* j */"), (6, "// u")]))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "php"), expected, src)

    def test_php_block_in_script_is_an_operand(self):
        # Блок PHP в «<script>» и «<style>» выводит значение: для разбора JS и CSS он — имя: «/<?php ?>/g» —
        # регулярка, «/<?php ?>* x *<?php ?>/» — не комментарий.
        cases = (("<script>\nvar re = /<?php echo $pattern; ?>/g;\nvar x = 1; // c\n</script>\n", [(3, "// c")]),
                 ("<script>\nvar w = <?= $a ?>/<?= $b ?>; // c\n</script>\n", [(2, "// c")]),
                 ("<style>\na { b: c }\n/<?php ?>* not *<?php ?>/\n</style>\n", []),
                 # Имени нет в тексте комментария; строка из одного блока PHP — не строка комментария JS.
                 ("<script>\n/* a\n<?php echo 1 ?>\n b <?= $c ?>x */ // c\n</script>\n",
                  [(2, "/* a"), (4, "b x */ // c")]))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "php"), expected, src)

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
        self.assertEqual(helpers.comment_lines('const a = "a \\" // not"; // yes\n', "js"), ["// yes"])
        self.assertEqual(helpers.comment_lines("s = 'it\\'s # x' # c\n", "py"), ["# c"])

    def test_char_literal_quote_does_not_open_string(self):
        cases = {"c": "char q = '\"'; s = \"//x\"; // c\n",
                 "go": "q := '\"'; s := \"//x\" // c\n",
                 "rs": "let q = '\"'; let s = \"//x\"; // c\n"}
        for ext, src in cases.items():
            self.assertEqual(helpers.comment_lines(src, ext), ["// c"], ext)

    def test_csharp_verbatim_interpolated_string(self):
        self.assertEqual(helpers.comment_lines('var s = @$"C:\\{d}\\"; var t = "// x"; // c\n', "cs"), ["// c"])

    def test_cpp_prefixed_raw_string(self):
        self.assertEqual(helpers.comment_lines('auto s = u8R"(a " // not)"; // yes\n', "cpp"), ["// yes"])
        # Закрытие — с разделителем: «)"» без него строку не закрывает.
        self.assertEqual(comments._comments('R"x(\n)" // not\n)x"; // yes', "cpp"), [(3, "// yes")])

    def test_rust_byte_raw_string(self):
        self.assertEqual(helpers.comment_lines('let s = br"a\\"; // c\nlet t = 1;\n', "rs"), ["// c"])

    def test_hash_after_dollar_or_brace_is_code(self):
        self.assertEqual(helpers.comment_lines("my $n = $#a; # c\n", "pl"), ["# c"])
        self.assertEqual(helpers.comment_lines("t:\n\techo $${#PATH} # c\n", "makefile"), ["# c"])
        # Вне рецепта «#» в вызове функции GNU make буквален: «{#» — код.
        self.assertEqual(helpers.comment_lines("N := $(shell echo $${#A}) # c\n", "makefile"), ["# c"])

    def test_shell_ansi_c_string_escapes_quote(self):
        # «$'…'» bash — строка с escape обратной косой: «\'» её не закрывает; «'…'» — без escape.
        for ext in ("sh", "bash", "zsh"):
            self.assertEqual(helpers.comment_lines("echo $'a\\'b # x' # c\necho 'a\\' # d\n", ext), ["# c", "# d"],
                             ext)

    def test_nested_string_in_substitution(self):
        # Строка внутри подстановки строки не закрывает внешнюю: стек подстановок до закрывающей кавычки.
        cases = (("kt", 'val u = "${uri("https://x")}" // c\n', "// c"),
                 ("kt", 'val u = """${"""//"""}""" // c\n', "// c"),
                 ("gradle", 'url = "${uri("https://x")}" // c\n', "// c"),
                 ("swift", 'let s = "\\(f("http://x"))" // c\n', "// c"),
                 ("cs", 'var s = $"{F("http://x")}"; // c\n', "// c"),
                 ("cs", 'var s = $@"{F(@"a"" // b")}"; // c\n', "// c"),
                 ("dart", "var s = '${m['//']}'; // c\n", "// c"),
                 ("ex", 's = "#{f("a # b")}" # c\n', "# c"),
                 ("sh", 'r="$(echo "a # b")" # c\n', "# c"),
                 ("sh", 'r="`echo "a # b"`" # c\n', "# c"),
                 ("sh", 'r="${x:-"a # b"}" # c\n', "# c"),
                 ("py", 'x = f"{d["//#"]}"  # c\n', "# c"),
                 ("py", 'x = rf"\\{d["#"]}"  # c\n', "# c"),
                 # Сырая f-строка: «\\"» и «\\\\» кавычку не закрывают (токенизатор Python), «\\{» открывает
                 # подстановку. Форма — pygments 2.19, lexers/lisp.py.
                 ("py", 'z = rf"a\\"#{y}"  # c\n', "# c"),
                 ("py", "z = rf'[\\'#]{y}'  # c\n", "# c"),
                 ("py", 'z = rf"\\\\" + "#"  # c\n', "# c"),
                 # Обратная косая в коде подстановки shell берёт следующий знак: «\\"» — не строка.
                 ("sh", 'q="${x/\\"/ # z}" # c\n', "# c"),
                 ("sh", 't="${x//[\\"]/ # y}" # c\n', "# c"),
                 ("sh", 'y="$(echo \\"a\\")" # c\n', "# c"),
                 ("tf", 'x = "${f("#")}" # c\n', "# c"))
        for ext, src, expected in cases:
            self.assertEqual(helpers.comment_lines(src, ext), [expected], (ext, src))
        # Без подстановки — обычная строка: «{{» f-строки и «$${» Terraform — текст, «"» без «$» C#, строка Scala
        # без интерполятора, сырая строка Dart.
        cases = (("py", 'x = f"{{" + "#" # c\n', "# c"), ("tf", 'x = "$${f("#")}" # c\n', '#")}" # c'),
                 ("cs", 'var s = "{F("http://x")}"; // c\n', '//x")}"; // c'),
                 ("scala", 'val u = "${uri("https://x")}" // c\n', '//x")}" // c'),
                 ("dart", "var s = r'${m['//']}'; // c\n", "//']}'; // c"))
        for ext, src, expected in cases:
            self.assertEqual(helpers.comment_lines(src, ext), [expected], (ext, src))

    def test_python_format_spec_is_text(self):
        # «:» f-строки на нулевой глубине скобок кода подстановки открывает спецификацию формата — текст до «}»,
        # в нём «{…}» — вложенная подстановка; «:» в «[…]», «(…)», «{…}» кода — не формат.
        cases = (('a = f"{x:\'>10}"  # c\n', "# c"),
                 ("a = f'{x:\"^10}' + \"#\"  # c\n", "# c"),
                 ('a = f"{x:\'>{w}}" + \'#\'  # c\n', "# c"),
                 ('a = f"{x!r:\'<{w:\'>3}}"  # c\n', "# c"),
                 ('a = rf"{x:\'>10}"  # c\n', "# c"),
                 ('a = f"{x[1:2]!r:>3} {d[\'#\']} {(lambda y: \'#\')(1)} {({1: \'#\'})}"  # c\n', "# c"),
                 ('a = f"""{x:\'>10}"""  # c\n', "# c"))
        for src, expected in cases:
            self.assertEqual(helpers.comment_lines(src, "py"), [expected], src)

    def test_hash_right_after_brace_is_comment(self):
        # «{» — не префикс выражения: «#» сразу за ней открывает комментарий.
        cases = {"py": "d = {# c\n 1: 2}\n", "rb": "[1].each {# c\n |x| x }\n", "pl": "my %h = (a => {# c\n});\n",
                 "r": "f <- function() {# c\n}\n", "jl": "d = Dict{# c\n}\n", "ex": "m = %{# c\n}\n",
                 "nix": "{# c\n}\n", "ps1": "if ($a) {# c\n}\n", "tf": "a = {# c\n}\n", "toml": "a = {# c\n}\n",
                 "cmake": "if(A) {# c\n"}
        for ext, src in cases.items():
            self.assertEqual(helpers.comment_lines(src, ext), ["# c"], ext)
        # Сразу после «${» — код: «${#x}» — переменная PowerShell.
        self.assertEqual(helpers.comment_lines("$a = ${#x} # c\n", "ps1"), ["# c"])

    def test_escaped_hash_is_code(self):
        self.assertEqual(helpers.comment_lines("x := a\\#b # c\n", "makefile"), ["# c"])
        self.assertEqual(helpers.comment_lines("set(X a\\#b) # c\n", "cmake"), ["# c"])

    def test_crlf_heredoc_terminator(self):
        self.assertEqual(helpers.comment_lines("cat <<EOF > s.md\r\n# body\r\nEOF\r\n# real\r\n", "sh"), ["# real"])

    def test_dash_heredoc_with_tab_indented_terminator(self):
        self.assertEqual(helpers.comment_lines("cat <<-EOF\n\t# body\n\tEOF\n# real\n", "sh"), ["# real"])

    def test_shebang_in_hash_comment_languages(self):
        # Shebang первой строки — не комментарий и в php, ps1, nim, nix: интерпретатор пропускает строку.
        for ext, src in (("php", "#!/usr/bin/env php\n<?php\n$x = 1; # c\n"),
                         ("ps1", "#!/usr/bin/env pwsh\n$x = 1 # c\n"),
                         ("nim", "#!/usr/bin/env -S nim r\nlet x = 1 # c\n"),
                         ("nix", "#!/usr/bin/env nix-instantiate --eval\n1 # c\n")):
            self.assertEqual(helpers.comment_lines(src, ext), ["# c"], ext)

    def test_bom_before_shebang(self):
        self.assertEqual(helpers.comment_lines("\ufeff#!/usr/bin/env python3\n# c\n", "py"), ["# c"])
        self.assertEqual(helpers.comment_lines("\ufeff# c\n", "py"), ["# c"])

    def test_ruby_heredoc_body_is_data(self):
        src = "sql = <<~SQL\n  # not\n  SQL\nq = <<-'EOS'.strip # tail\n# not\n    EOS\nt = <<EOF\n# not\nEOF\n# real\n"
        self.assertEqual(helpers.comment_lines(src, "rb"), ["# tail", "# real"])
        self.assertEqual(helpers.comment_lines("execute <<~SQL\n  # not\nSQL\n# real\n", "rakefile"), ["# real"])
        self.assertEqual(helpers.comment_lines('print <<"EOT";\n# not\nEOT\n# real\n', "pl"), ["# real"])

    def test_shift_is_not_ruby_heredoc(self):
        src = "x = 1<<BITS # a\nclass << self # b\n  arr << ITEM # c\nend\n"
        self.assertEqual(helpers.comment_lines(src, "rb"), ["# a", "# b", "# c"])

    def test_shift_after_closer_is_not_heredoc(self):
        for src in ("a[0]<<X # a\n# b\nX\n", "h{1}<<X # a\n# b\nX\n", "f(1)<<X # a\n# b\nX\n"):
            self.assertEqual(helpers.comment_lines(src, "rb"), ["# a", "# b"], src)

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
            self.assertEqual(helpers.comment_lines(src, "pl"), ["# real"], src)

    def test_perl_long_block_filehandle(self):
        # Блок-дескриптор узнаётся любой длины и вложенности; скобки перед ним не мешают.
        name = "a" * 300
        for src in ("print {$self->{" + name + "}} <<EOF;\n# not\nEOF\n# c\n",
                    "f({}); print {$h{x}{" + name + "}} <<EOF;\n# not\nEOF\n# c\n"):
            self.assertEqual(helpers.comment_lines(src, "pl"), ["# c"], src[:40])
        self.assertEqual(helpers.comment_lines("f({" + name + "}} <<EOF); # c1\n# c2\nEOF\n", "pl"), ["# c1", "# c2"])

    def test_perl_heredoc_real_corpus_forms(self):
        # Идентификатор с ведущим «_», «::» в имени дескриптора, «<<» вплотную после функции вывода.
        for src in ("print <<_EOUSAGE_ ;\n# not\n_EOUSAGE_\n# real\n", "print <<_EOVERS;\n# not\n_EOVERS\n# real\n",
                    "print {$DB::OUT} <<EOP;\n# not\nEOP\n# real\n", "die<<EOF;\n# not\nEOF\n# real\n",
                    "warn<<EOF;\n# not\nEOF\n# real\n", "print<<EOT;\n# not\nEOT\n# real\n",
                    "printf<<EOT, 1;\n# not\nEOT\n# real\n", "say<<EOT;\n# not\nEOT\n# real\n",
                    "print CSS<<EOF;\n# not\nEOF\n# real\n", "print STDERR<<EOF;\n# not\nEOF\n# real\n"):
            self.assertEqual(helpers.comment_lines(src, "pl"), ["# real"], src)

    def test_perl_tight_shift_is_not_heredoc(self):
        # Вплотную после прочего слова, переменной или константы вне print — сдвиг; в Ruby print<<EOT — сдвиг.
        for src, ext in (("x = 1<<EOF; # c1\n# c2\nEOF\n", "pl"), ("$print<<EOF; # c1\n# c2\nEOF\n", "pl"),
                         ("foo CSS<<EOF; # c1\n# c2\nEOF\n", "pl"), ("$h->print<<EOF; # c1\n# c2\nEOF\n", "pl"),
                         ("reprint<<EOF; # c1\n# c2\nEOF\n", "pl"), ("print<<EOF # c1\n# c2\nEOF\n", "rb"),
                         ("print CSS<<EOF # c1\n# c2\nEOF\n", "rb")):
            self.assertEqual(helpers.comment_lines(src, ext), ["# c1", "# c2"], src)
        # Ruby: за методом через пробел — heredoc и со строчным идентификатором; Perl: за print — тоже.
        self.assertEqual(helpers.comment_lines("puts <<_eof # c1\n# c2\n_eof\n", "rb"), ["# c1"])
        self.assertEqual(helpers.comment_lines("print <<_ # c1\n# c2\n_\n", "pl"), ["# c1"])

    def test_ruby_heredoc_by_word_before(self):
        # Лексер Ruby: «<<» вплотную за словом — сдвиг, кроме ключевых слов, за которыми начинается выражение
        # (return, if, and…); через пробел за методом и константой — heredoc, за локальной переменной, числом и
        # self, nil, true, false, end — сдвиг и добавление при любом идентификаторе. Ожидания сверены с Prism.
        cases = (("puts <<eof\n# a\neof\n# c\n", ["# c"]), ("return<<eos\n# a\neos\n# c\n", ["# c"]),
                 ("x if<<eos\n# a\neos\n# c\n", ["# c"]), ("x.y <<-eof\n# a\neof\n# c\n", ["# c"]),
                 ("Foo <<eof\n# a\neof\n# c\n", ["# c"]), ("x = 1\nx.x <<eof\n# a\neof\n# c\n", ["# c"]),
                 ("self <<EOF # c1\n# c2\nEOF\n", ["# c1", "# c2"]),
                 ("x = 1\nx <<~EOF # c1\n# c2\nEOF\n", ["# c1", "# c2"]),
                 ("x = 1\nx <<'EOF' # c1\n# c2\nEOF\n", ["# c1", "# c2"]),
                 ("1 <<eof # c1\n# c2\neof\n", ["# c1", "# c2"]),
                 ("x.return<<EOS # c1\n# c2\nEOS\n", ["# c1", "# c2"]))
        for src, expected in cases:
            self.assertEqual(helpers.comment_lines(src, "rb"), expected, src)

    def test_perl_builtins_decide_slash_and_heredoc(self):
        # За встроенной функцией Perl, ждущей аргумент, «/» — регулярка и через пробел (и многострочная), «<<» —
        # heredoc и вплотную; за функцией без аргументов (time, wantarray) и __LINE__ — деление и сдвиг.
        # Ожидания сверены с perl -MO=Deparse.
        cases = (("my @a = reverse / #/; # c\n", ["# c"]), ("my $t = time /2; # c\n", ["# c"]),
                 ("my $x = getppid /2; # c\n", ["# c"]), ("my $h = shift / 2;\n# c/;\n", []),
                 ("print <<eof;\n# not\neof\n# c\n", ["# c"]), ("my $s = lc<<EOS;\n# not\nEOS\n# c\n", ["# c"]),
                 ("return<<EOS;\n# not\nEOS\n# c\n", ["# c"]),
                 ("my $n = time <<EOF; # c1\n# c2\nEOF\n", ["# c1", "# c2"]),
                 ("my $n = __LINE__ <<EOF; # c1\n# c2\nEOF\n", ["# c1", "# c2"]),
                 ("$o->length / #/; # c\n", ["#/; # c"]))
        for src, expected in cases:
            self.assertEqual(helpers.comment_lines(src, "pl"), expected, src)

    def test_perl_hash_element_before_shift(self):
        # «}» закрывает элемент хеша, а не «{$дескриптор}» после print.
        self.assertEqual(comments._comments("my $x = $h{$k} <<FLAGS; # c1\n# c2\nFLAGS\n", "pl"),
                         [(1, "# c1"), (2, "# c2")])
        self.assertEqual(comments._comments("print $h{$k} <<FLAGS; # c1\n# c2\nFLAGS\n", "pl"),
                         [(1, "# c1"), (2, "# c2")])

    def test_shift_after_quoted_term_is_not_heredoc(self):
        for src in ('puts "a" <<EOF # c1\n# c2\nEOF\n', "puts 'a' <<EOF # c1\n# c2\nEOF\n",
                    "puts `a` <<EOF # c1\n# c2\nEOF\n"):
            self.assertEqual(helpers.comment_lines(src, "rb"), ["# c1", "# c2"], src)

    def test_lowercase_heredoc_after_operator_or_line_start(self):
        # Строчный идентификатор без «-», «~» и кавычек — heredoc там, где добавление невозможно.
        self.assertEqual(helpers.comment_lines("x = <<eof\n# not\neof\n# real\n", "rb"), ["# real"])
        self.assertEqual(helpers.comment_lines("  <<'eof'\n# not\neof\n# real\n", "rb"), ["# real"])
        self.assertEqual(helpers.comment_lines("puts <<'eof'\n# not\neof\n# real\n", "rb"), ["# real"])
        self.assertEqual(helpers.comment_lines("puts <<`eof`\n# not\neof\n# real\n", "rb"), ["# real"])

    def test_perl_filehandle_branch_needs_print_and_dollar(self):
        self.assertEqual(helpers.comment_lines("print STDOUT <<eof;\n# not\neof\n# real\n", "pl"), ["# real"])
        self.assertEqual(helpers.comment_lines("print STDERR <<eof;\n# not\neof\n# real\n", "pl"), ["# real"])
        for src in ("foo STDOUT <<eof; # c1\n# c2\neof\n", "foo STDERR <<eof; # c1\n# c2\neof\n",
                    "print @a <<EOF; # c1\n# c2\nEOF\n"):
            self.assertEqual(helpers.comment_lines(src, "pl"), ["# c1", "# c2"], src)

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
            self.assertEqual(helpers.comment_lines(src, "rb"), ["# real"], src)

    def test_unterminated_heredoc_is_rolled_back(self):
        self.assertEqual(comments._comments("x = <<EOF\n# c2\n# c3\n", "rb"), [(2, "# c2"), (3, "# c3")])
        # Откат касается только незакрытого: закрытый heredoc остаётся данными.
        src = "a = <<ONE\n# not\nONE\nb = <<TWO\n# c5\n"
        self.assertEqual(comments._comments(src, "rb"), [(5, "# c5")])
        self.assertEqual(comments._comments("x = <<EOT\n# c2\n", "tf"), [(2, "# c2")])

    def test_indented_terminator_of_plain_heredoc_is_not_terminator(self):
        src = "x = <<ID\n  ID\n# not\nID\n# real\n"
        self.assertEqual(helpers.comment_lines(src, "rb"), ["# real"])


class ExpressionLiteralTest(unittest.TestCase):
    """Регулярные выражения JS и TS, slashy-строки Groovy, разметка JSX: «/» и «<» после токена, за которым
    начинается выражение, — литерал или тег; после значения — деление и сравнение."""

    def test_js_regex_literals(self):
        for ext in ("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"):
            self.assertEqual(comments._comments("let r = /\\/\\//;\n", ext), [], ext)
            self.assertEqual(comments._comments("let r = /a\\/*/;\nx = 1;\n// c\n", ext), [(3, "// c")], ext)
            # «/» в классе не закрывает регулярку: без классов «"/; y = "» — строка, а «// no» — комментарий.
            self.assertEqual(comments._comments('re = /[/]"/; y = "// no"\n', ext), [], ext)
            self.assertEqual(comments._comments("return /x/.test(s); // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("s.replace(/\\/+/g, '/'); // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("x = c ? /a/ : /b\\//i // c\n", ext), [(1, "// c")], ext)

    def test_js_division_stays_division(self):
        for src in ("x = a / b // c\n", "y = x[i] / 2 // c\n", "z = f() / 3 // c\n", ")/2 // c\n",
                    "n = i++ / 2 // c\n", "q = this.return / 2 // c\n", "w = 1 /2/ 3 // c\n"):
            self.assertEqual(helpers.comment_lines(src, "js"), ["// c"], src)

    def test_division_continues_previous_line(self):
        # Начало строки — начало выражения, только если им кончилась прошлая строка кода.
        src = "s = a[u] // c1\n// c2\n/ b.c // c3\n/ d; // c4\nx = (\n/a\\//.test(y)) // c5\n"
        self.assertEqual(helpers.comment_lines(src, "js"), ["// c1", "// c2", "// c3", "// c4", "// c5"])
        self.assertEqual(helpers.comment_lines("f() /* c1 */\n/ 2 // c2\n", "ts"), ["/* c1 */", "// c2"])

    def test_unclosed_regex_ends_at_line_end(self):
        # Мнимая регулярка без закрытия прячет только остаток своей строки.
        self.assertEqual(comments._comments("x = /[a // b\n// c\n", "js"), [(2, "// c")])

    def test_regex_after_statement_parenthesis(self):
        # «)» условия if, while, for, with — конец заголовка оператора: «/» за ним — регулярка (так acorn); «)»
        # вызова и группы — конец значения, «/» — деление.
        for ext in ("js", "ts", "jsx", "tsx"):
            cases = (("if (x) /\\/\\//.test(y) // c\n", [(1, "// c")]),
                     ("while (f(x)) /\\/\\//.exec(s) // c\n", [(1, "// c")]),
                     ("for (;;) /a\\//.test(s) // c\n", [(1, "// c")]),
                     ("if (a)\n  /\\/\\//.test(y) // c\n", [(2, "// c")]),
                     ("if (a) b = f(x) /2/ 1 // c\n", [(1, "// c")]),
                     ("x.if (a) /2/ 1 // c\n", [(1, "// c")]),
                     ("z = (a) /2/ 1 // c\n", [(1, "// c")]))
            for src, expected in cases:
                self.assertEqual(comments._comments(src, ext), expected, (ext, src))

    def test_regex_after_block_comment(self):
        # Блочный комментарий прозрачен: решает токен перед ним. «`» за регуляркой — её знак; за делением —
        # многострочная шаблонная строка до «`» последней строки, она прячет «// d».
        for ext in ("js", "ts"):
            cases = (("x = /* c */ /`/\n// d\nz = `\n", [(1, "/* c */ /`/"), (2, "// d")]),
                     ("x = /* c\n*/ /`/\n// d\nz = `\n", [(1, "/* c"), (2, "*/ /`/"), (3, "// d")]),
                     ("x = a /* c\n*/ /`/\n// d\nz = `\n", [(1, "/* c"), (2, "*/ /`/")]),
                     ("x = /* c */\n/`/\n// d\nz = `\n", [(1, "/* c */"), (3, "// d")]),
                     ("x = /a/ /`/\n// d\nz = `\n", []),
                     ("x = /a/ /2/ 1 // c\n", [(1, "// c")]))
            for src, expected in cases:
                self.assertEqual(comments._comments(src, ext), expected, (ext, src))

    def test_js_increment_and_division_before_slash(self):
        # «++» и «--» не меняют ответ токена перед собой, в начале строки — префикс; «/» деления — начало выражения.
        # «`» за делением открыл бы шаблонную строку до «`» последней строки и спрятал «// d». Ожидания сверены с
        # парсером TypeScript.
        for ext in ("js", "ts"):
            cases = (("x = ++/`/\n", 1), ("a +++/`/\n", 1), ("f(a)\n++/`/\n", 2), ("x = a / /`/\n", 1),
                     ("x = a /\n/`/\n", 2), ("x = a ++\n/2/`\n", None), ("x = a++ /2/`\n", None))
            for src, line in cases:
                n = src.count("\n")
                expected = [(n + 1, "// d")] if line else []
                self.assertEqual(comments._comments(src + "// d\nz = `\n", ext), expected, (ext, src))

    def test_groovy_multiline_slashy_strings(self):
        # Slashy-строка Groovy многострочна; без закрытия до конца файла лексер Groovy читает «/» как деление.
        # Ожидания сверены с лексером Groovy 4 (org.apache.groovy.parser.antlr4.GroovyLexer).
        for ext in ("groovy", "gradle"):
            cases = (("def u = /a\nb // c/\n// d\n", [(3, "// d")]),
                     ("def u = /a\n// c\n", []),
                     ("def u = /a\\/\n// b/ // c\n", [(2, "// c")]),
                     ("def u = /a\\\\/ // b/ // c\n", [(1, "// c")]),
                     ("def u = /a ${b /* x */ / 2} // c/ // d\n", [(1, "/* x */ / 2} // c/ // d")]),
                     ("def u = /a ${'}'} b/ // c\n", [(1, "// c")]),
                     ("def u = /a/ /2/ 1 // c\n", [(1, "// c")]),
                     ("def u = a / /b // c/\n", []),
                     ("def u = {1} /2/ 1 // c\n", [(1, "// c")]),
                     ("def u = {1} /b // c/\n", [(1, "// c/")]),
                     ("def u = /a/ /b // c/\n", [(1, "// c/")]),
                     ("def u = (a\n/b // c/)\n", [(2, "// c/)")]),
                     ("def u = this /2/ 1 // c\n", [(1, "// c")]),
                     ("def u = null /2/ 1 // c\n", [(1, "// c")]),
                     ("def u = a++ /2/ 1 // c\n", [(1, "// c")]),
                     ("def u = a.in /b // c/\n", []),
                     ("assert /b // c/\n", []),
                     ("def u = a $/b/$ // c\n", [(1, "// c")]),
                     ("def u = a$/b/$ // c\n", [(1, "// c")]),
                     ("def u = a$/ 2 // c/$\n", [(1, "// c/$")]),
                     ("def u = a $/b\n// c\n", [(2, "// c")]),
                     ("def u = a /* c */ /2/ 1 // d\n", [(1, "/* c */ /2/ 1 // d")]),
                     ("def u = (a\n/2/ 1) // c\n", [(2, "// c")]),
                     ("def u = (a // b\n/2/ 1) // c\n", [(1, "// b"), (2, "// c")]),
                     ("def u = [a\n/2/ 1] // c\n", [(2, "// c")]),
                     ("def u = a\n/b // c/\n", []),
                     ("def u = ({\n/b // c/ })\n", []),
                     ("try (a\n/b // c/) {}\n", []))
            for src, expected in cases:
                self.assertEqual(comments._comments(src, ext), expected, (ext, src))

    def test_groovy_string_line_continuation(self):
        # «\» в конце строки продолжает строку Groovy в кавычках на следующей; «\\» — escape самой косой.
        for ext in ("groovy", "gradle"):
            cases = (('x = "a \\\nhttps://b \\\n" // c\n', [(3, "// c")]),
                     ("x = 'a \\\nhttps://b' // c\n", [(2, "// c")]),
                     ('x = "a \\\\\nhttps://b" // c\n', [(2, '//b" // c')]),
                     # «"""» без закрытия — «""» и строка «" "», «\» за ней — продолжение кода, не строки.
                     ('x = """ "a \\\nb //c"\n', [(2, '//c"')]))
            for src, expected in cases:
                self.assertEqual(comments._comments(src, ext), expected, (ext, src))

    def test_unclosed_openings_reported_for_rollback(self):
        # Slashy-строка без закрытия — открытие для отката, запрещённое — деление; ожидание второй части s{…}{…}
        # к концу файла ничего не прячет и в откат не идёт.
        groovy, perl = comments._SYNTAX["groovy"], comments._SYNTAX["pl"]
        self.assertEqual(comments._parse("x = /a\n", groovy, None, frozenset())[1], [(None, "ctx", (1, 4))])
        self.assertEqual(comments._parse("x = /a\n", groovy, None, frozenset({(1, 4)}))[1], [])
        self.assertEqual(comments._parse("$s =~ s{a}\n", perl, None, frozenset())[1], [])

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
            self.assertEqual(comments._comments(src, ext), [(4, "/* jsx */}"), (10, "// after")], ext)

    def test_jsx_tag_comment_in_attributes(self):
        src = "x = <div // c\n  id='a'\n>text // t</div>\n"
        self.assertEqual(comments._comments(src, "jsx"), [(1, "// c")])

    def test_comparison_and_generics_are_not_tags(self):
        for src in ("if (a <b) f() // c\n", "const x: Array<string> = [] // c\n",
                    "const f = <T,>(x: T) => x // c\n", "const g = <T extends X>(x: T) => x // c\n",
                    "useState<string>(null) // c\n"):
            self.assertEqual(helpers.comment_lines(src, "tsx"), ["// c"], src)

    def test_plain_ts_has_no_jsx(self):
        self.assertEqual(helpers.comment_lines("const f = <T>(x: T) => x // c\n", "ts"), ["// c"])

    def test_multiline_opening_tag(self):
        src = 'return (\n  <a\n    href="x"\n  >\n    see http://x.y\n  </a>\n);\n// c\n'
        self.assertEqual(comments._comments(src, "jsx"), [(8, "// c")])

    def test_markup_branches(self):
        # Счётчик скобок кода в разметке, фрагмент «<>» и вне элемента, и внутри него.
        self.assertEqual(comments._comments("const a = <div>{fn({a: 1}) // c\n}</div>;", "jsx"), [(1, "// c")])
        self.assertEqual(comments._comments("const a = <>see http://x.y</>; // yes", "jsx"), [(1, "// yes")])
        self.assertEqual(comments._comments("const a = <div><>see http://x.y</></div>; // yes", "jsx"),
                         [(1, "// yes")])

    def test_shift_is_not_tag(self):
        # «<<» перед именем в конце строки — сдвиг, а не тег JSX.
        src = "const m = 1 <<SHIFT\n// one\nconst x = a > b\n// two\n</div>\n// three\n"
        self.assertEqual(comments._comments(src, "js"), [(2, "// one"), (4, "// two"), (6, "// three")])

    def test_block_comment_before_regex_line(self):
        # Строка кода, кончающаяся блочным комментарием, ждёт выражение: «/» следующей строки — регулярка.
        self.assertEqual(comments._comments("const r = /* pattern */\n/\\/\\//.test(s);", "js"),
                         [(1, "/* pattern */")])

    def test_template_substitutions(self):
        # Шаблон внутри подстановки, комментарий внутри подстановки, текст шаблона после неё.
        src = ('const html = `\n  <ul>${items.map(i => `<li>${i}</li>`).join("")}</ul>\n`;\n// real comment one\n'
               "function f() { return 1; } // real two\n")
        self.assertEqual(comments._comments(src, "js"), [(4, "// real comment one"), (5, "// real two")])
        src = 'const page = `\n  ${rows.map(r => `<td>${r}</td>`).join("")}\n  Docs: https://example.com/docs\n`;\n'
        self.assertEqual(comments._comments(src, "js"), [])
        for ext in ("js", "ts", "jsx", "tsx", "mjs", "cjs", "mts", "cts"):
            self.assertEqual(comments._comments("const s = `a ${ x // y\n} d`;\n", ext), [(1, "// y")], ext)
            self.assertEqual(comments._comments("const s = `${p}/*`; // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("x = `${ {a: 1}.a } // not`; // c\n", ext), [(1, "// c")], ext)
            self.assertEqual(comments._comments("x = `\\${ // not`; // c\n", ext), [(1, "// c")], ext)
        self.assertEqual(comments._comments("const a = <p>{`${x}`} // t</p>; // c\n", "jsx"), [(1, "// c")])

    def test_unclosed_template_is_rolled_back(self):
        # Шаблонная строка без закрытия до конца файла — однострочная: следующие строки — код.
        self.assertEqual(comments._comments("x = `a\n// c\n", "js"), [(2, "// c")])

    def test_unclosed_tag_is_rolled_back(self):
        # Мнимый тег без закрытия до конца файла — не тег: разбор повторяется, и комментарии за ним видны.
        src = "type F = <T>(x: T) => T;\n// c\nconst a = <p>t // t</p>;\n"
        self.assertEqual(comments._comments(src, "tsx"), [(2, "// c")])


class RubyPerlElixirLiteralTest(unittest.TestCase):
    """Регулярные выражения и литералы с разделителями Ruby, Perl, Elixir: «#» в них — не комментарий."""

    def test_perl_regex_and_quote_operators(self):
        src = "$line =~ s/#.*$//;    # strip comments\nmy @f = split /#/, $s;\n"
        self.assertEqual(comments._comments(src, "pl"), [(1, "# strip comments")])
        src = ("my @w = qw(a #b c); # c1\nmy $r = qr{#\\d+}x; # c2\n$s =~ tr/#/ /; # c3\n$s =~ s{#}{ }g; # c4\n"
               "my $q = q#not#; # c5\nmy $t = qq (#(a)#); # c6\n$s =~ m<#>; # c7\n")
        self.assertEqual(helpers.comment_lines(src, "pl"), [f"# c{i}" for i in range(1, 8)])

    def test_perl_division_and_barewords_are_code(self):
        src = ("my $x = $a / $b; # c1\nmy %h = (s => 1, y => 2); # c2\nprint $h{s}; # c3\n$x = $n /2; # c4\n"
               "$x = 10 /2; # c5\n$o->s(1); # c6\nmy $d = $a // 0; # c7\n")
        self.assertEqual(helpers.comment_lines(src, "pl"), [f"# c{i}" for i in range(1, 8)])

    def test_ruby_regex_and_percent_literals(self):
        self.assertEqual(comments._comments('s = line.sub(/#.*/, "")\nparts = s.split(/#/)', "rb"), [])
        src = ("w = %w(a #b) # c1\nr = %r{#\\d+#{x}}x # c2\nx = a % b # c3\ny = a / 2 # c4\nn = 10 /2 # c5\n"
               "puts /#/ # c6\nr = /#{x}/ # c7\n")
        self.assertEqual(helpers.comment_lines(src, "rb"), [f"# c{i}" for i in range(1, 8)])

    def test_elixir_sigils(self):
        src = '~r/#\\d+/ # c1\nx = ~w(a #b) # c2\ns = ~S"""\n# not\n""" # c3\nl = ~w(\n#a\n) # c4\n'
        self.assertEqual(helpers.comment_lines(src, "ex"), ["# c1", "# c2", "# c3", "# c4"])

    def test_perl_multiline_quote_operators(self):
        # Многострочные операторы-кавычки; «#» в многострочной регулярке — комментарий режима /x; вторая часть
        # s{…}{…}e в скобках — код.
        src = ("my @w = qw(\n  a #b\n); # c1\n$s =~ s{\n  a # c2\n}{b}x; # c3\n$s =~ s{a}{\n  f(1); # c4\n}e; # c5\n"
               "my $q = q[\n# not\n]; # c6\n$s =~ tr/a\n#/b/; # c7\n$t =~\n  /^(?:  # c8\n    a\n  )$/x; # c9\n")
        self.assertEqual(helpers.comment_lines(src, "pl"), [f"# c{i}" for i in range(1, 10)])

    def test_perl_special_variables_backticks_and_delimiters(self):
        # «$"» — переменная, а не строка; `…` — строка; «m,…,» и «m$…$» — операторы-кавычки.
        src = ("local $\" = ')(';\nmy $x = \"a\"; # c1\nmy $o = `echo \"\n# not\n\"`; # c2\n"
               "if (m,/, ) { f(\"x\"); } # c3\nmy @a = $t =~ m$a\"b$g; # c4\nmy $r = $h{a} / 2; # c5\n")
        self.assertEqual(helpers.comment_lines(src, "pl"), [f"# c{i}" for i in range(1, 6)])

    def test_data_section_and_quoted_heredoc_ids(self):
        # После __END__ — данные, POD в них — документация; идентификатор heredoc в кавычках — любые знаки.
        self.assertEqual(helpers.comment_lines("print 1; # c1\n__END__\ndon't # not\n=pod\n\nDoc\n\n=cut\n", "pl"),
                         ["# c1", "=pod", "Doc", "=cut"])
        self.assertEqual(helpers.comment_lines("x = 1 # c1\n__END__\nit's # not\n", "rb"), ["# c1"])
        self.assertEqual(helpers.comment_lines("print <<'----END----';\n'# not\n----END----\n# c\n", "pl"), ["# c"])
        self.assertEqual(helpers.comment_lines('x = <<~"END OF TEXT"\n  "# not\n  END OF TEXT\n# c\n', "rb"), ["# c"])

    def test_ruby_interpolation_and_line_start(self):
        # Кавычки внутри «#{…}» не закрывают строку; строка Ruby начинает выражение: «/» в её начале — регулярка.
        src = ('s = "a #{h["k"]} b" # c1\nt = "#{x ? "\\"" : \'"\'}" # c2\ncmd = `echo #{"a"}` # c3\n'
               "if x\n  /a:/ =~ y # c4\nend\nr = %r{\n  a # c5\n}x # c6\nw = %w(\n  a #b\n) # c7\n")
        self.assertEqual(helpers.comment_lines(src, "rb"), [f"# c{i}" for i in range(1, 8)])

    def test_ruby_slash_after_local_variable_is_division(self):
        # После локальной переменной Ruby «/» и «%» — деление и остаток; после метода — литерал-аргумент.
        src = ("a = 4\nx = a /b # c1\ndef f(n, m)\n  n /2 # c2\nend\n[1].each { |k| k /m # c3\n}\n"
               "v ||= 1\nv /2 # c4\nputs /#/ # c5\n")
        self.assertEqual(comments._comments(src, "rb"),
                         [(2, "# c1"), (4, "# c2"), (6, "# c3"), (9, "# c4"), (10, "# c5")])
        self.assertEqual(comments._comments("x = a /b # c\n", "rb"), [])

    def test_multiline_regex_without_x_flag_has_no_comments(self):
        # «#» в многострочной регулярке — комментарий только с флагом x после закрытия литерала.
        cases = (("pl", "$r = qr{\n  a #b\n};\n", []), ("pl", "$r = qr{\n  a #b\n}x;\n", [(2, "#b")]),
                 ("pl", "$r = qr{\n  a #b\n}i; # c\n", [(3, "# c")]),
                 ("pl", "$s =~ s/\n a #b\n/c/; # c\n", [(3, "# c")]),
                 ("pl", "$s =~ s/\n a #b\n/c/x;\n", [(2, "#b")]), ("pl", "$s =~ s{\n a #b\n}{c};\n", []),
                 ("pl", "$s =~ s{\n a #b\n} {\n c\n}gx;\n", [(2, "#b")]),
                 ("pl", "$s =~ m{\n a #b\n  c #d\n}; # e\n", [(4, "# e")]),
                 ("rb", "r = %r{\n  a #b\n}\n", []), ("rb", "r = %r{\n  a #b\n}xi\n", [(2, "#b")]),
                 ("rb", "r =\n/a #b\n/ # c\n", [(3, "# c")]), ("ex", "r = ~r/\n  a #b\n/\n", []),
                 ("ex", "r = ~r/\n  a #b\n/x\n", [(2, "#b")]),
                 # Строка, закрывшая регулярку с флагом x и открывшая новую, к новой не относится.
                 ("pl", "$r = qr{\n  a #b\n  c #d }x; $s = qr{\n  e\n}; # f\n",
                  [(2, "#b"), (3, "#d }x; $s = qr{"), (5, "# f")]))
        for ext, src, expected in cases:
            self.assertEqual(comments._comments(src, ext), expected, src)

    def test_regex_after_term_words_symbols_and_spaced_heredoc(self):
        # «/» после split, grep, if и подобных — регулярка и вплотную, и перед пробелом и «=»; «:/» Ruby — символ;
        # «<< "ID"» Perl — heredoc. Источники форм — Perl core (CPAN/Distribution.pm, B/Deparse.pm,
        # ExtUtils/Constant/ProxySubs.pm), Ruby 3.4 (ruby_vm/rjit/insn_compiler.rb).
        cases = (("pl", "my ($p, $a) = split /=/, $plugin, 2; # c1\nmy $x = $a / 2; # c2\n", ["# c1", "# c2"]),
                 ("pl", "@names = split/\\s+/, $val; # c1\nmy $x = $a / 2; # c2\n", ["# c1", "# c2"]),
                 ("pl", "print /=#/; # c1\n$n /= 2; # c2\n", ["# c1", "# c2"]),
                 ("pl", "my @w = split / /, $s; # c1\nmy $x = $a / 2; # c2\n", ["# c1", "# c2"]),
                 ("pl", "$_ = uc $_ unless /=/; # c1\nmy $x = $a / 2; # c2\n", ["# c1", "# c2"]),
                 ("pl", "print $xs_fh $e ? <<\"EXPLODE\" : << \"DONT\";\na\nEXPLODE\n#ifndef X\nDONT\n# c1\n",
                  ["# c1"]),
                 ("rb", "register(Integer, :/, :jit_div) # c1\nregister(Integer, :%, :jit_mod) # c2\nx = a / 2 # c3\n",
                  ["# c1", "# c2", "# c3"]),
                 ("rb", "x << \"a\" # c1\n# c2\n", ["# c1", "# c2"]),
                 ("rb", "x << \"EOS\" # c1\n# c2\nEOS\n# c3\n", ["# c1", "# c2", "# c3"]))
        for ext, src, expected in cases:
            self.assertEqual(helpers.comment_lines(src, ext), expected, src)

    def test_ruby_slash_after_keywords(self):
        # За return, else и подобными «/» — регулярка и через пробел; за self, nil — деление; not — как метод.
        # Ожидания сверены с Prism.
        cases = (("return / #/ if x # c\n", [(1, "# c")]), ("self /a # c\n", [(1, "# c")]),
                 ("x = foo? / 2 # c\n", [(1, "# c")]), ("foo?<<EOF # c1\n# c2\nEOF\n", [(1, "# c1"), (2, "# c2")]),
                 ("nil /a # c\n", [(1, "# c")]), ("not / 2 # c/\n", [(1, "# c/")]), ("not /#/ # c\n", [(1, "# c")]))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "rb"), expected, src)

    def test_ruby_slash_operand_forms(self):
        # Метод после «.» с именем локальной переменной или слова из _TERM_WORDS — не переменная и не начало
        # выражения; «::/» — не символ; «/=» после слова — деление с присваиванием и вплотную к знаку;
        # «a, b = …» вводит обе переменные.
        cases = (("a = 4\nx = obj.a /#/ # c\n", [(2, "# c")]),
                 ("x = a.then / 2 # c1\ny = 1 / 2 # c2\n", [(1, "# c1"), (2, "# c2")]),
                 ("v = t ? a ::/#/ # c\n", [(1, "# c")]),
                 ("foo /=#/ # c\n", [(1, "#/ # c")]), ("x = obj.foo /=#/ # c\n", [(1, "#/ # c")]),
                 ("n = 1\nn /= 2 # c\n", [(2, "# c")]),
                 ("a, b = 4, 2\nx = a /b # c\n", [(2, "# c")]))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "rb"), expected, src)

    def test_flags_after_literal_are_not_quote_operators(self):
        # Буквы за закрывающим разделителем — флаги: «s,» после «/…/» — не оператор s с разделителем «,».
        src = ("for my $feep (grep /^\\$pw_/s, @EXPORT_OK) { # c1\nmy @a = m!a!s, 1; # c2\n"
               "$x = qr{\n  a\n}s, 1; # c3\n$s =~ s{a}{b}s, 1; # c4\n")
        self.assertEqual(helpers.comment_lines(src, "pl"), [f"# c{i}" for i in range(1, 5)])
        self.assertEqual(helpers.comment_lines("r = %r{\n  a\n}s # c1\n", "rb"), ["# c1"])

    def test_perl_second_part_on_later_line(self):
        # Вторая часть s{…}{…}, tr{…}{…} в скобках — и на следующих строках, за пробелами и комментариями (perlop,
        # «Gory details of parsing quoted constructs»): она строка или код (e), её флаги решают про /x первой.
        cases = (("$s =~ s{\n a #b\n}\n{c};\n# d\n", [(5, "# d")]),
                 ("$s =~ s{\n a #b\n}\n{c}x;\n", [(2, "#b")]),
                 ("$s =~ s{a} # c\n\n  { #not}x; # e\n", [(1, "# c"), (3, "# e")]),
                 ("$s =~ s{a}\n  # c\n{ #not};\n# d\n", [(2, "# c"), (4, "# d")]),
                 ("$s =~ s{a}\n  # c\n{\n f() # d\n}e;\n", [(2, "# c"), (4, "# d")]),
                 ("$s =~ s{\n a #b\n} # c\n {\n f() # d\n }ex; # e\n", [(2, "#b"), (3, "# c"), (5, "# d"), (6, "# e")]),
                 ("$s =~ tr{a}\n{ #not};\n# c\n", [(3, "# c")]))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "pl"), expected, src)

    def test_perl_substitution_replacement_code_only_with_e_flag(self):
        # Вторая часть s{…}{…} — код с флагом e, иначе строка; на первой строке и на следующих.
        cases = (("$s =~ s{a}{ # c\n  f()\n}e;\n", [(1, "# c")]),
                 ("$s =~ s{a}{ # c\n  f() # d\n}g; # e\n", [(3, "# e")]),
                 ("$s =~ s{a}{\n  f() # d\n  {x}\n}ge; # e\n", [(2, "# d"), (4, "# e")]),
                 ("$s =~ s{a}{\n  'x' # d\n};\n", []))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "pl"), expected, src)

    def test_nested_substitution_code_within_budget(self):
        # Вложенные вторые части s{…}{…}e разбираются кодом, пока повторный разбор укладывается в бюджет 4·len(text);
        # глубже _MAX_CODE_DEPTH — не разбираются (предел рекурсии Python).
        def nest(depth, pad=""):
            return pad + "$s =~ s{a}{\n" * depth + "f() # d\n" + "}e;\n" * depth

        self.assertEqual(comments._comments(nest(6), "pl"), [(7, "# d")])
        pad = "# " + "x" * 200000 + "\n"
        self.assertEqual(comments._comments(nest(60, pad), "pl"), [(1, pad.strip()), (62, "# d")])
        self.assertEqual(comments._comments(nest(500, pad), "pl"), [(1, pad.strip())])
        self.assertEqual(comments._comments(nest(500), "pl"), [])
        # Глубина меньше _MAX_CODE_DEPTH, бюджет исчерпан: повторный разбор ~8,5·60² знаков больше 4·len(text).
        self.assertEqual(comments._comments(nest(60), "pl"), [])

    def test_unclosed_literal_ends_with_line(self):
        # Незакрытая в строке регулярка прячет только остаток своей строки.
        self.assertEqual(comments._comments("x = split /a # b\n# c\n", "pl"), [(2, "# c")])
        self.assertEqual(comments._comments("x = %w(a # b\n# c\n", "rb"), [(2, "# c")])


    def test_ruby_begin_inside_string_is_data(self):
        # Строка «"…"» Ruby многострочна: «=begin» с первой колонки внутри неё — текст строки.
        src = 'x = "abc\n=begin not a comment\n"\ny = 1 # real\nz = 2\n=end\n'
        self.assertEqual(comments._comments(src, "rb"), [(4, "# real")])
        src = "x = 'abc\n=begin not\n'\ny = 1 # real\n"
        self.assertEqual(comments._comments(src, "rb"), [(4, "# real")])

    def test_perl_pod_only_where_statement_starts(self):
        # perl (toke.c, PL_expect == XSTATE): «=слово» с первой колонки — POD, только где ждётся новый оператор:
        # в начале файла, после «;», «{», «}» и метки; посреди оператора — присваивание (perl -MO=Deparse, 5.42).
        self.assertEqual(comments._comments("my $x\n=shift; # c\nprint $x;\n", "pl"), [(2, "# c")])
        self.assertEqual(comments._comments("my $x = 1 # c\n=foo;\n", "pl"), [(1, "# c")])
        for head in ("", "my $x = 1;\n", "sub f {\n", "sub f { 1 }\n", "FOO:\n", "my $x = 1; # c\n\n",
                     "print <<EOT;\nbody\nEOT\n", "=pod\n\n=cut\n"):
            src = head + "=head1 NAME\n\nx\n\n=cut\n"
            found = [c for _, c in comments._comments(src, "pl")]
            self.assertEqual(found[-3:], ["=head1 NAME", "x", "=cut"], head)

    def test_perl_filetest_s_is_not_substitution(self):
        # «-s» и знак не слова за ним — файловый тест (perl -MO=Deparse: «-s($f)» — «-s $f»); «-m(a)», «-q(a)»,
        # «-y(a)(b)» — минус и оператор-кавычка; «--s/a/b/» — декремент и замена.
        self.assertEqual(comments._comments("my $n = -s($f); # size\nmy $m = 1; # two\n", "pl"),
                         [(1, "# size"), (2, "# two")])
        self.assertEqual(comments._comments('my $n = -s ("a") + -e("b"); # size\n', "pl"), [(1, "# size")])
        self.assertEqual(comments._comments("my $n = -q(# a); # c\n", "pl"), [(1, "# c")])
        self.assertEqual(comments._comments("$i--s{a # b}{c}; # c\n", "pl"), [(1, "# c")])


class MultilineStringTest(unittest.TestCase):
    """Многострочные строки Ruby, Perl, Julia, PowerShell, блочные скаляры YAML."""

    def test_strings_span_lines(self):
        cases = (("rb", 's = "line one\n# inside string\nend"\n# c\n', [(4, "# c")]),
                 ("rb", "s = 'a\n# not\n' # c\n", [(3, "# c")]),
                 ("pl", "my $s = 'a\n# not\n'; # c\n", [(3, "# c")]),
                 ("pl", 'my $s = "a\n# not\n"; # c\n', [(3, "# c")]),
                 ("jl", 's = "a\n# not\n" # c\n', [(3, "# c")]),
                 ("ps1", '$s = "a\n# not\n" # c\n', [(3, "# c")]),
                 ("ps1", "$s = 'a\n# not\n' # c\n", [(3, "# c")]),
                 # Here-string PowerShell; «`"» — escape кавычки.
                 ("ps1", '$h = @"\n"# not\n"@ # c\n', [(3, "# c")]),
                 ("ps1", "$h = @'\n'# not\n'@ # c\n", [(3, "# c")]),
                 ("ps1", '$e = "a`"b # not" # c\n', [(1, "# c")]))
        for ext, src, expected in cases:
            self.assertEqual(comments._comments(src, ext), expected, (ext, src))

    def test_php_strings_span_lines(self):
        # Строки PHP «'…'», «"…"», «`…`» многострочны: «?>» и «<?php» внутри них — текст строки, а не граница блока
        # PHP (лексер PHP 8: T_CONSTANT_ENCAPSED_STRING и T_ENCAPSED_AND_WHITESPACE берут перевод строки).
        cases = (("<?php\nclass Feed {\n    function xml() {\n        $xml = '<?xml version=\"1.0\"?>\n"
                  "<rss version=\"2.0\">';\n        // c1\n        return $xml; // c2\n    }\n}\n",
                  [(6, "// c1"), (7, "// c2")]),
                 ("<?php\n$tpl = '<div>\n<?php echo $x; ?>\n</div>'; // c1\n// c2\n", [(4, "// c1"), (5, "// c2")]),
                 ('<?php\n$s = "\n?>\n// not\n"; // c\n', [(5, "// c")]),
                 ("<?php\n$s = `ls\n?> # not\n`; # c\n", [(4, "# c")]))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "php"), expected, src)

    def test_unclosed_multiline_string_is_rolled_back(self):
        # Строка без закрытия до конца файла — однострочная: следующие строки — код.
        self.assertEqual(comments._comments('x = "unclosed\n# c\n', "rb"), [(2, "# c")])
        self.assertEqual(comments._comments('a = "x"\nb = "y\n# c\n', "pl"), [(3, "# c")])
        self.assertEqual(comments._comments('def f():\n    """Open\nx = 1  # c\n', "py"), [(3, "# c")])
        self.assertEqual(comments._comments("x = ~w(a\n# c\n", "ex"), [(2, "# c")])

    def test_yaml_block_scalars(self):
        self.assertEqual(comments._comments("value: |\n  # heading\n  text\nk: v # real", "yaml"), [(4, "# real")])
        src = ("steps:\n  - text: >-  # c1\n      # not\n    name: x # c2\n  - |\n    # not\n  - y # c3\n"
               "k: !!str &a |\n  # not\n\n  # not\n# c4\n")
        self.assertEqual(helpers.comment_lines(src, "yaml"), ["# c1", "# c2", "# c3", "# c4"])
        # «|» и «>» внутри значения — не индикатор.
        self.assertEqual(helpers.comment_lines("a: x | y\n  # c1\nb: x >\n  # c2\n", "yaml"), ["# c1", "# c2"])

    def test_yaml_script_block_scalars_are_shell(self):
        # Блочный скаляр под ключом скрипта — shell: «#» в начале слова вне кавычек — комментарий.
        src = ("jobs:\n  b:\n    steps:\n      - run: |\n          # Устанавливаем зависимости\n"
               "          pip install x  # trailing\n")
        self.assertEqual(comments._comments(src, "yml"), [(5, "# Устанавливаем зависимости"), (6, "# trailing")])
        cases = (
            # GitLab: элемент списка «- |» под script, в том числе список без отступа.
            ("t:\n  script:\n    - |\n      # c1\n      make\n    - echo # c2\n  after_script:\n  - >-\n    # c3\n",
             [(4, "# c1"), (6, "# c2"), (9, "# c3")]),
            # Ansible: модуль с пространством имён.
            ("- name: x\n  ansible.builtin.shell: |\n    # c1\n    ls\n  args:\n    chdir: /\n", [(3, "# c1")]),
            # Heredoc и кавычки shell внутри скрипта.
            ("run: |\n  cat <<EOF\n  # data\n  EOF\n  echo '#x' \"# y\" # c1\n# c2\n", [(5, "# c1"), (6, "# c2")]),
            # Скаляр после вложенного отображения в том же списке — под своим ключом.
            ("script:\n  - name: a\n  - |\n    # c1\nvalues:\n  - |\n    # heading\n", [(4, "# c1")]),
            # Индикатор на строке после ключа.
            ("run:\n  | # c\n  echo 1 # d\nk: v # e\n", [(2, "# c"), (3, "# d"), (4, "# e")]),
            ("steps:\n  - run:\n      >-\n      # c1\n  - k: v # c2\n", [(4, "# c1"), (5, "# c2")]),
        )
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "yaml"), expected, src)

    def test_yaml_prose_block_scalars_are_data(self):
        # Скаляр под прочим ключом — данные: заголовки Markdown формы issue и описаний OpenAPI.
        src = ("body:\n  - type: markdown\n    attributes:\n      value: |\n        ### Before you start\n"
               "        # Read docs\n  - type: textarea\n    id: what # c1\ninfo:\n  description: |\n    # Errors\n")
        self.assertEqual(comments._comments(src, "yaml"), [(8, "# c1")])

    def test_github_script_input_is_javascript(self):
        # Вход «script» actions/github-script — JavaScript, вход «script» прочих действий — shell. Форма —
        # flutter, .github/workflows/release-tracker.yml.
        src = ("steps:\n  - uses: \"actions/github-script@v7\" # c0\n    with:\n      script: |\n"
               "        const body = `\n        # Release ${tag}\n        `; // c1\n"
               "  - name: x\n    with:\n      script: |\n        # c2\n    uses: ./setup\n")
        self.assertEqual(comments._comments(src, "yml"), [(2, "# c0"), (7, "// c1"), (11, "# c2")])
        self.assertEqual(comments._comments("- uses: actions/github-script\n  with:\n    script: |\n      # a\n",
                                            "yml"), [])
        # «script» шага github-script не под «with:» — shell.
        self.assertEqual(comments._comments("- uses: actions/github-script\n  env:\n    script: |\n      # a\n",
                                            "yml"), [(4, "# a")])

    def test_github_script_uses_after_with(self):
        # Порядок ключей шага не важен: «uses» после «with:» решает язык входа «script» так же; без «uses» до конца
        # шага — shell. Строки вывода — по порядку номеров.
        cases = (("- with:\n    script: |\n      // a\n  uses: actions/github-script@v7\n", [(3, "// a")]),
                 ("- with:\n    script: |\n      // a\n      # b\n  # c\n  uses: actions/github-script@v7\n",
                  [(3, "// a"), (5, "# c")]),
                 ("- with:\n    script: |\n      # a\n  uses: actions/checkout@v4\n", [(3, "# a")]),
                 ("- with:\n    script: |\n      # a\n- uses: actions/github-script@v7\n", [(3, "# a")]),
                 ("steps:\n  - with:\n      script: |\n        # a\n", [(4, "# a")]),
                 ("a:\n  - with:\n      script: |\n        # a\nb:\n  uses: actions/github-script@v7\n", [(4, "# a")]))
        for src, expected in cases:
            self.assertEqual(comments._comments(src, "yml"), expected, src)

    def test_yaml_key_stack_keeps_one_key_per_column(self):
        keys, uses = [], {}
        for line in ("a:", "  b:", "c:", "d:"):
            comments._yaml_key(line, keys, uses)
        self.assertEqual(keys, [(0, "d")])


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

    def test_max_bytes_counts_utf8_bytes(self):
        body = "# " + "ж" * 500
        self.write("a.py", f"{body}\n" * 40)
        lines, truncated, _, _ = comments.extract(self.root, ["a.py"])
        self.assertTrue(truncated)
        self.assertEqual(len(lines), comments.MAX_BYTES // len(f"a.py: {body}".encode("utf-8")))

    def test_utf16_and_utf32_by_bom(self):
        text = "x = 1\n# c\n"
        for name, bom, codec in (("le.py", codecs.BOM_UTF16_LE, "utf-16-le"),
                                 ("be.py", codecs.BOM_UTF16_BE, "utf-16-be"),
                                 ("l4.py", codecs.BOM_UTF32_LE, "utf-32-le")):
            (self.root / name).write_bytes(bom + text.encode(codec))
        self.assertEqual(comments.extract(self.root, ["le.py", "be.py", "l4.py"])[0],
                         ["le.py: # c", "be.py: # c", "l4.py: # c"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_utf16_added_lines_by_git_line_numbers(self):
        # «Ċ» (U+010A) несёт байт 0x0A в UTF-16: git видит в строке два перевода, номера строк сдвигаются.
        self.git("init", "-q")
        old = "x = 'Ċ'\n# old\n"
        for name, bom, codec in (("le.py", codecs.BOM_UTF16_LE, "utf-16-le"),
                                 ("be.py", codecs.BOM_UTF16_BE, "utf-16-be")):
            (self.root / name).write_bytes(bom + old.encode(codec))
            self.commit(name)
            (self.root / name).write_bytes(bom + (old + "# new\n").encode(codec))
        self.assertEqual(comments.extract(self.root, ["le.py", "be.py"])[0], ["le.py: # new", "be.py: # new"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_utf16_change_after_newline_byte_inside_line(self):
        # «上» (U+4E0A) и «Њ» (U+040A) несут байт 0x0A: git делит строку текста на две, правка после знака — во
        # второй git-строке той же строки текста.
        self.git("init", "-q")
        for name, bom, codec, old, new in (
                ("u.ps1", codecs.BOM_UTF16_LE, "utf-16-le", "$x = 1  # 上 old\n", "$x = 1  # 上 new comment\n"),
                ("b.py", codecs.BOM_UTF16_BE, "utf-16-be", "x = 1\ny = 2  # Њ old\n", "x = 1\ny = 2  # Њ new\n")):
            (self.root / name).write_bytes(bom + old.encode(codec))
            self.commit(name)
            (self.root / name).write_bytes(bom + new.encode(codec))
        self.assertEqual(comments.extract(self.root, ["u.ps1", "b.py"])[0],
                         ["u.ps1: # 上 new comment", "b.py: # Њ new"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_utf16_change_in_last_line_without_final_newline(self):
        # Последняя строка без завершающего перевода строки содержит знак с байтом 0x0A («Њ»): номер за ней
        # считается по числу этих байтов, а не прибавкой единицы.
        self.git("init", "-q")
        for name, bom, codec in (("l.py", codecs.BOM_UTF16_LE, "utf-16-le"),
                                 ("b.py", codecs.BOM_UTF16_BE, "utf-16-be")):
            (self.root / name).write_bytes(bom + "x = 1\ny = 2  # Њ old".encode(codec))
            self.commit(name)
            (self.root / name).write_bytes(bom + "x = 1\ny = 2  # Њ new".encode(codec))
        self.assertEqual(comments.extract(self.root, ["l.py", "b.py"])[0], ["l.py: # Њ new", "b.py: # Њ new"])

    def test_no_lines_after_byte_truncation(self):
        self.write("a.py", ("# " + "x" * 1000 + "\n") * 20)
        self.write("b.py", "# s\n")
        lines, truncated, _, _ = comments.extract(self.root, ["a.py", "b.py"])
        self.assertTrue(truncated)
        self.assertEqual([l for l in lines if l.startswith("b.py")], [])

    def test_deadline_passed_files_without_check(self):
        common._reset()
        self.addCleanup(common._reset)
        self.write("a.py", "# a\n")
        self.write("b.bin", "x\n")
        lines, truncated, unknown, late = comments.extract(self.root, ["a.py", "b.bin"], None, None,
                                                           time.monotonic() - 1)
        self.assertEqual((lines, truncated, unknown, late), ([], False, ["b.bin"], ["a.py"]))
        self.assertEqual(common._messages,
                         ["planka: строки комментариев не извлечены в срок, файлов кода без проверки: 1"])

    def test_git_timeout_files_without_check(self):
        common._reset()
        self.addCleanup(common._reset)
        self.write("a.py", "# a\n")
        with mock.patch.object(comments.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            lines, _, unknown, late = comments.extract(self.root, ["a.py"], "HEAD", None, time.monotonic() + 30)
        self.assertEqual((lines, unknown, late), ([], [], ["a.py"]))
        self.assertEqual(common._messages,
                         ["planka: строки комментариев не извлечены в срок, файлов кода без проверки: 1"])

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
    def test_root_below_repository_is_outside_git(self):
        # Корень проекта в каталоге чужого репозитория: git не поднимается выше корня, файл — целиком.
        self.git("init", "-q")
        self.write("sub/a.py", "# old\n")
        self.commit("sub/a.py")
        self.write("sub/a.py", "# old\n# new\n")
        lines, _, _, _ = comments.extract(self.root / "sub", ["a.py"])
        self.assertEqual(lines, ["a.py: # old", "a.py: # new"])

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
    def test_rename_pass_names_no_paths(self):
        # Пару переименованию ищет git diff всего дерева: удалённые пути не в командной строке, их число не
        # ограничено.
        self.git("init", "-q")
        body = "# old one\n" + "x = 1\n" * 10 + "# old two\n"
        self.write("a.py", body)
        for i in range(1001):
            self.write(f"g/{i}.py", f"v = {i}\n")
        self.commit("a.py", "g")
        self.git("rm", "-q", "-r", "g")
        self.git("mv", "a.py", "b.py")
        self.write("b.py", body + "# новое\n")
        with mock.patch.object(comments, "_git", wraps=comments._git) as git:
            self.assertEqual(comments.extract(self.root, ["b.py"])[0], ["b.py: # новое"])
        renames = [c.args for c in git.call_args_list if "--diff-filter=R" in c.args]
        self.assertEqual(len(renames), 1)
        self.assertEqual(renames[0][-1], "--")

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_paths_beyond_command_line_limit(self):
        # Пути сверх предела командной строки: execve отвечает E2BIG — git diff идёт частями, а не падает.
        self.git("init", "-q")
        names = [f"{'d' * 100}/f{i:04}.py" for i in range(300)]
        for n in names:
            self.write(n, "x = 1\n# old\n")
        self.commit("d" * 100)
        self.write(names[7], "x = 1\n# old\n# new\n")
        run, limit = subprocess.run, 32_767

        def limited(args, **kwargs):
            if sum(len(os.fsencode(str(a))) + 1 for a in args) > limit:
                raise OSError(errno.E2BIG, "Argument list too long")
            return run(args, **kwargs)

        with mock.patch.object(comments.subprocess, "run", side_effect=limited):
            lines, truncated, _, _ = comments.extract(self.root, names)
        self.assertEqual((lines, truncated), ([f"{names[7]}: # new"], False))

    def test_batches_within_limit(self):
        with mock.patch.object(comments, "MAX_ARG_BYTES", 10):
            self.assertEqual(list(comments._batches(["aaaa", "bbbb", "c", "dddddddddddd", "e"])),
                             [["aaaa", "bbbb"], ["c"], ["dddddddddddd"], ["e"]])

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
    def assert_linear(self, cases):
        """Разбор каждого входа make(4 * n) не дольше восьми разборов make(n) и 5 мс (helpers.assert_linear)."""
        for make, ext, n in cases:
            small, large = make(n), make(4 * n)
            helpers.assert_linear(self, lambda: comments._comments(small, ext, time.monotonic() + 30),
                                  lambda: comments._comments(large, ext, time.monotonic() + 30), msg=(ext, make(2)))

    def test_unclosed_quotes_and_backslashes(self):
        self.assertEqual(helpers.comment_lines('"\\' * 20000 + " // c", "js"), ["// c"])
        self.assertEqual(helpers.comment_lines("'\\" * 20000, "js"), [])
        self.assertEqual(helpers.comment_lines('"' + "\\a" * 20000 + '" // c', "js"), ["// c"])
        self.assert_linear(((lambda k: '"\\' * k + " // c", "js", 5000), (lambda k: "'\\" * k, "js", 2500),
                            (lambda k: '"' + "\\a" * k + '" // c', "js", 5000)))

    def test_many_triple_quotes_on_one_line(self):
        self.assertEqual(helpers.comment_lines('x = """a""" ' * 250000, "py"), [])
        self.assert_linear(((lambda k: 'x = """a""" ' * k, "py", 1500),))

    def test_long_prefix_before_many_heredoc_openers(self):
        # Решение «heredoc или сдвиг» смотрит только хвост строки перед «<<», а не весь префикс.
        cases = ((lambda k: 'my $d = "' + "ab" * k + '"; print $fh <<EOF;\nbody\nEOF\n', "pl", []),
                 (lambda k: "x" * (100 * k) + " <<b " * k, "rb", []),
                 (lambda k: "print {$" + "f" * (100 * k) + "} <<B " * k + "\n# not\nB\n", "pl", []),
                 (lambda k: " " * (10 * k) + "x <<b " * k, "rb", []),
                 # Первые тройные кавычки — docstring: строка идёт в вывод целиком.
                 (lambda k: " " * (10 * k) + '"""""" ' * k, "py", [(1, ('"""""" ' * 2000).strip())]))
        for make, ext, expected in cases:
            self.assertEqual(comments._comments(make(2000), ext, time.monotonic() + 30), expected, ext)
        self.assert_linear((make, ext, 100) for make, ext, _ in cases)

    def test_new_language_constructs_are_linear(self):
        # Вложенные блоки, символьные литералы, REM, «"» Vim, heredoc PHP, @doc Elixir, блок-дескриптор Perl.
        self.assert_linear((
            (lambda k: "/* " * k + "*/ " * k + "\n", "rs", 10000), (lambda k: "(* " + "(*)" * k + "\n", "fs", 20000),
            (lambda k: "/* " + "*/ /*" * k + "\n", "kt", 2000), (lambda k: "#[" + " #[ ]#" * k + "\n", "nim", 10000),
            (lambda k: "$" * k, "erl", 8000), (lambda k: "\\" * k, "clj", 8000), (lambda k: "?" * k, "el", 6000),
            (lambda k: "?#" * k, "rb", 3000), (lambda k: "remx " * k, "bat", 2500),
            (lambda k: " " * (5 * k) + "a ::" * k, "cmd", 1500), (lambda k: "x rem" * k, "vb", 2500),
            (lambda k: ' "a' * k, "vim", 20000), (lambda k: 'x"' * k + ' "', "vim", 3000),
            (lambda k: "<?php " + "<<<A " * k, "php", 500), (lambda k: "@doc " * k, "ex", 2500),
            (lambda k: "{" * (5 * k) + "} <<B" * k, "pl", 200), (lambda k: "print " + "{$a->{b}}<<B " * k, "pl", 400),
            (lambda k: "#" * k + '"', "swift", 50000), (lambda k: 'r"' * k, "nim", 4000),
            # Регулярки, slashy-строки, теги JSX: незакрытые литералы, классы, имена тегов, вложенность.
            (lambda k: "(/[" * k, "js", 50000), (lambda k: "=/" * k, "ts", 5000), (lambda k: "(/\\" * k, "js", 5000),
            (lambda k: " " * (3 * k) + "/a/" * k, "js", 10000), (lambda k: "return" * k + "/", "js", 50000),
            (lambda k: "(/" * k, "groovy", 5000), (lambda k: "=$/" * k, "gradle", 50000),
            (lambda k: "$/" + "$$/" * k, "groovy", 600), (lambda k: "(<a>" * k, "jsx", 1000),
            (lambda k: "=<" * k, "tsx", 2000), (lambda k: "(<" + "a" * k + " " * k + "=" * (k // 100), "tsx", 50000),
            (lambda k: "<a " * k, "jsx", 1500),
            (lambda k: "x = <p>" + "<b>{" * k + "\n" + "}</b>" * k + "</p>\n", "jsx", 600),
            (lambda k: "(<T,>" * k, "tsx", 2000), (lambda k: "(<a>\n" * k + "// c\n" * k, "js", 500),
            # Слово перед «/» и «$» просматривается не дальше _KEYWORD_MAX знаков.
            (lambda k: "$" * k, "groovy", 1200), (lambda k: "a" * (2 * k) + " /" * k, "js", 2000),
            # Шаблонные строки JS, «<<» перед тегом JSX.
            (lambda k: "`${" * k, "js", 1500), (lambda k: "x`" + "${`" * k, "ts", 1500),
            (lambda k: "`" + "${a}" * k + "`", "js", 5000), (lambda k: "x <<" * k, "jsx", 5000),
            # Литералы Ruby, Perl, Elixir: незакрытые разделители, скобки, «/» после слова.
            (lambda k: "s{" * k, "pl", 10000), (lambda k: "q(" * k, "pl", 10000),
            (lambda k: "s " * k + "{", "pl", 10000), (lambda k: "s/a/" * k, "pl", 2000),
            (lambda k: "a /" * k, "rb", 2500), (lambda k: "x = %w(" * k, "rb", 10000),
            (lambda k: "~r/" * k, "ex", 5000), (lambda k: ' "a' * k, "rb", 5000),
            # Специальные переменные, вложенные подстановки Ruby, многострочные операторы-кавычки и heredoc.
            (lambda k: '$"' * k, "pl", 10000), (lambda k: '"#{' * k, "rb", 5000), (lambda k: "s{a}{" * k, "pl", 5000),
            (lambda k: "qr{" * k + "\n", "pl", 10000), (lambda k: "%r{\n" + "a # b\n" * k + "}", "rb", 2000),
            (lambda k: "x = <<'a <<\"b " * k, "pl", 2000),
            # Блочные скаляры YAML: длинный отступ, теги перед индикатором.
            (lambda k: "a: |\n" + "  " * k + "\n", "yaml", 50000), (lambda k: "k: !a " * k + "|", "yaml", 10000),
            # Скрипты в блочных скалярах YAML, стек ключей; локальные переменные Ruby; флаги x и e после литерала.
            (lambda k: "run: |\n" + "  echo a # c\n" * k, "yaml", 2000),
            (lambda k: "a:\n  b:\n    c:\n- |\n  # c\n" * k, "yaml", 1000),
            (lambda k: "script:\n" + "  - a: 1\n  - |\n    # c\n" * k, "yaml", 1000),
            (lambda k: "".join(" " * d + "- uses: x\n" for d in range(200)) + (" " * 200 + "- k: v\n") * k, "yaml",
             2000),
            (lambda k: ("".join(" " * d + f"k{d}:\n" for d in range(100)) + " |\n  # c\n") * k, "yaml", 100),
            (lambda k: "a = 1; " * k + "a /b # c", "rb", 5000), (lambda k: "do |" * k, "rb", 5000),
            (lambda k: "def f(" * k, "rb", 5000), (lambda k: "a, " * k + "= 1", "rb", 5000),
            (lambda k: "qr{\n a #b\n}\n" * k, "pl", 1000), (lambda k: "s{a}{\n" * k + "}e\n" * k, "pl", 500),
            # Стек скобок JS и Groovy, «++» перед «/», блочные комментарии перед «/», slashy-строки с подстановками,
            # продолжение строки Groovy, тройная кавычка без закрытия.
            (lambda k: "(" * k + ")" * k + " /a/", "js", 5000), (lambda k: "if (a) /b/ " * k, "ts", 2000),
            (lambda k: "x" + " ++" * k + " /`/", "js", 5000), (lambda k: "x = ++" * k + "/a/", "js", 3000),
            (lambda k: "/* */ " * k + "/a/", "js", 3000), (lambda k: "(\n" * k + "/a/\n" * k, "groovy", 1000),
            (lambda k: "x = /a ${" * k + "}/" * k, "groovy", 2000), (lambda k: "+" * k + "/a/", "groovy", 20000),
            (lambda k: 'x = "a \\\n' * k, "gradle", 2000), (lambda k: '"""' + ' ""' * k + "\n", "groovy", 5000),
            (lambda k: "x = a $/" * k + "\n", "groovy", 5000),
            # Вторая часть s{…}{…} на следующих строках, длинный блок-дескриптор, слова перед «<<» и «/».
            (lambda k: "s{a}\n" * k + "{b}\n", "pl", 2000), (lambda k: "s{a} # c\n\n" * k, "pl", 2000),
            (lambda k: "f({}) " * k + "print {$x->{" + "a" * k + "}} <<B\n# c\nB\n", "pl", 2000),
            (lambda k: "print {$a->{b}} <<B " * k + "\nB\n", "pl", 500), (lambda k: "lc<<A " * k, "pl", 2000),
            (lambda k: "a?" * k + " /b", "rb", 5000), (lambda k: "x = 1\n" + "x <<eof " * k, "rb", 3000),
            # Входы «script» шагов без uses.
            (lambda k: "- with:\n    script: |\n      # a\n" * k + "  uses: actions/github-script@v7\n", "yml",
             1000),
            (lambda k: "".join(" " * d + "- with:\n" + " " * d + "    script: |\n" + " " * d + "      # a\n"
                               for d in range(k)), "yml", 100)))

    def test_many_closed_substitutions_on_one_line(self):
        # Пуст ли хвост строки за первой частью s{…}{…} — без копии хвоста; квадратичность видна от десятков тысяч
        # операторов в строке.
        self.assertEqual(comments._comments("s{a}{b} " * 20000 + "# c\n", "pl"), [(1, "# c")])
        self.assert_linear(((lambda k: "s{a}{b} " * k, "pl", 20000),))

    def test_string_substitutions_are_linear(self):
        # Подстановки строк: незакрытые, вложенные до глубины k, строки и символьные литералы в коде подстановки,
        # спецификации формата Python, «\» в коде подстановки shell; «$'…'» shell.
        self.assert_linear((
            (lambda k: '"${' * k, "kt", 3000), (lambda k: '"${' * k + '}"' * k + " // c", "kt", 3000),
            (lambda k: '"$(' * k, "sh", 3000), (lambda k: '"`' * k, "sh", 3000), (lambda k: "$'\\" * k, "sh", 3000),
            (lambda k: 'f"{' * k, "py", 3000), (lambda k: 'f"{x:\'{(' * k, "py", 3000),
            (lambda k: '"${\\' * k, "sh", 3000), (lambda k: '"\\(' * k, "swift", 3000),
            (lambda k: '$"{' * k, "cs", 3000), (lambda k: '$@"{' * k, "cs", 3000),
            (lambda k: "'${" * k + '"${' * k, "dart", 3000),
            (lambda k: '"#{' * k, "ex", 3000), (lambda k: '"${' * k, "tf", 3000),
            (lambda k: 'x = """${\n' * k, "kt", 2000), (lambda k: '"${"a" ' * k, "kt", 3000),
            (lambda k: '"${\'"\'' * k, "kt", 3000), (lambda k: 'x = "${\\\n' * k, "gradle", 2000)))

    def test_php_templates_are_linear(self):
        # Блоки PHP в HTML, в теге и содержимом «<script>», «<style>»; «?>» после строчного комментария.
        self.assert_linear((
            (lambda k: "<?php ?>" * k, "php", 3000), (lambda k: "<script>a<?php ?>" + "b<?php ?>" * k, "php", 3000),
            (lambda k: "<script " + "'" * k, "php", 3000), (lambda k: "<style " + "<?php ?>" * k, "php", 3000),
            (lambda k: "<?php // a ?>" * k, "php", 3000), (lambda k: "<script>\n" + "a\n<?php\n?>\n" * k, "php", 1000),
            (lambda k: "<?" * k + "\n<!--\n" * k, "php", 2000), (lambda k: "<script>" * k, "php", 3000)))

    def test_groovy_unclosed_quote_before_escaped_quotes(self):
        # За незакрытой «"» или «'» строки Groovy каждая следующая такая же кавычка экранирована: хвост строки не
        # просматривается заново.
        self.assertEqual(helpers.comment_lines('x = "' + '\\"' * 2000 + " // c\n", "groovy"), ["// c"])
        self.assert_linear(((lambda k: 'x = "' + '\\"' * k + "\n", "groovy", 1000),
                            (lambda k: "x = '" + "\\'" * k + "\n", "gradle", 1000)))

    def test_single_file_components_are_linear(self):
        # Блоки «<script>», «<style>» и комментарии разметки ищутся одним проходом; теги без «>» и блоки без закрытия.
        self.assert_linear((
            (lambda k: "<script" * k, "vue", 5000), (lambda k: '<script a="' * k, "vue", 5000),
            (lambda k: "<!--" * k, "svelte", 5000), (lambda k: "<script></script>" * k, "svelte", 1000),
            (lambda k: "<style lang=scss>a{}</style><!-- c -->\n" * k, "vue", 500),
            (lambda k: "{" * k, "svelte", 5000), (lambda k: "{{" * k, "vue", 5000),
            (lambda k: "<a " * k, "svelte", 2000), (lambda k: "{/" * k, "svelte", 5000),
            (lambda k: "<br>{a}\n" * k, "svelte", 2000),
            # Значения директив Vue и строки атрибутов Svelte с кодом.
            (lambda k: "<p " + ':a="x" ' * k + ">\n", "vue", 2000),
            (lambda k: '<p :a="' + "f(\n" * k + '">\n', "vue", 2000),
            (lambda k: "<p " + " " * (10 * k) + ':a="x" ' * k + ">\n", "vue", 1000),
            (lambda k: '<p a="' + "{" * k + '">\n', "svelte", 2000), (lambda k: '<p a="{x}\n' * k, "svelte", 500)))

    def test_deadline_during_parse_files_without_check(self):
        common._reset()
        self.addCleanup(common._reset)
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

    def test_deadline_inside_one_long_line(self):
        common._reset()
        self.addCleanup(common._reset)
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
