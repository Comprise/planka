import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.helpers import Env, PLANKA_DIR

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402


class BarrierTest(unittest.TestCase):
    def test_judge_env_only(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLANKA_")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(common.barrier_active())
            with mock.patch.dict(os.environ, {"PLANKA_JUDGE": "1"}, clear=False):
                self.assertTrue(common.barrier_active())
            with mock.patch.dict(os.environ, {"PLANKA_OFF": "1"}, clear=False):
                self.assertFalse(common.barrier_active())


class ReadInputTest(unittest.TestCase):
    def test_empty_and_garbage(self):
        with mock.patch("sys.stdin", new=io.StringIO("")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=io.StringIO("not json")):
            self.assertIsNone(common.read_input())
        with mock.patch("sys.stdin", new=io.StringIO('{"a":1}')):
            self.assertEqual(common.read_input(), {"a": 1})
        with mock.patch("sys.stdin", new=io.StringIO('[1]')):
            self.assertIsNone(common.read_input())


class PhilosophyTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_sections_in_order(self):
        text = common.philosophy_sections("Планы", "Решения")
        self.assertTrue(text.startswith("## Планы"))
        self.assertIn("## Решения", text)
        self.assertLess(text.index("## Планы"), text.index("## Решения"))
        self.assertNotIn("## Поведение", text)
        self.assertIn("Правило планов два.", text)

    def test_missing_section_is_none(self):
        common._reset()
        self.assertIsNone(common.philosophy_sections("Решения", "Нет такого"))
        self.assertTrue(any(m.startswith("planka:") for m in common._messages))

    def test_missing_file_is_none(self):
        (self.env.root / "philosophy.md").unlink()
        common._reset()
        self.assertIsNone(common.philosophy_text())
        self.assertTrue(any(m.startswith("planka:") for m in common._messages))


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_placeholder_replaced_with_rules_dir(self):
        text = common.philosophy_text()
        self.assertNotIn("{RULES}", text)
        self.assertIn(str(self.env.root / "rules"), text)

    def test_rule_texts_in_order(self):
        text = common.rule_texts("planning", "verification")
        self.assertTrue(text.startswith("# Планирование"))
        self.assertIn("# Доказательство", text)
        self.assertLess(text.index("# Планирование"), text.index("# Доказательство"))

    def test_missing_rule_is_none(self):
        common._reset()
        self.assertIsNone(common.rule_texts("verification", "nope"))
        self.assertTrue(any(m.startswith("planka:") for m in common._messages))

    def test_non_utf8_rule_is_none(self):
        (self.env.root / "rules" / "verification.md").write_bytes(b"\xff\xfe")
        common._reset()
        self.assertIsNone(common.rule_texts("verification"))
        self.assertTrue(any(m.startswith("planka:") for m in common._messages))

    def test_rubric_combines(self):
        text = common.rubric(("Решения", "Планы"), ("planning",))
        self.assertTrue(text.startswith("## Решения"))
        self.assertIn("## Планы", text)
        self.assertIn("# Планирование", text)
        self.assertLess(text.index("## Планы"), text.index("# Планирование"))

    def test_rubric_sections_only_and_modules_only(self):
        self.assertTrue(common.rubric(("Решения",), ()).startswith("## Решения"))
        self.assertTrue(common.rubric((), ("verification",)).startswith("# Доказательство"))

    def test_rubric_none_when_part_missing(self):
        common._reset()
        self.assertIsNone(common.rubric(("Решения",), ("nope",)))
        self.assertIsNone(common.rubric(("Нет такого",), ("planning",)))

    def test_rules_dir_missing_is_none(self):
        import shutil
        shutil.rmtree(self.env.root / "rules")
        common._reset()
        self.assertIsNone(common.rule_texts("planning"))
        self.assertTrue(any(m.startswith("planka:") for m in common._messages))
        self.assertIn(str(self.env.root / "rules"), common.philosophy_text())


class RunJudgeTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def judge(self, **extra):
        with mock.patch.dict(os.environ, {**self.base, **extra}, clear=True):
            return common.run_judge("SYS", "USER", timeout=3)

    def test_ok(self):
        v = self.judge(PLANKA_STUB="ok")
        self.assertTrue(v.ok)
        self.assertIsNone(v.error)

    def test_deny(self):
        v = self.judge(PLANKA_STUB="deny", PLANKA_STUB_REASON="нет правильного варианта")
        self.assertFalse(v.ok)
        self.assertEqual(v.violated, ["Решения 4"])
        self.assertEqual(v.reason, "нет правильного варианта")

    def test_long_reason_is_capped(self):
        v = self.judge(PLANKA_STUB="deny", PLANKA_STUB_REASON="ы" * 3000)
        self.assertEqual(len(v.reason), 2000)
        self.assertTrue(v.reason.endswith("ы…"))

    def test_flags_stdin_and_env(self):
        rec = self.env.data / "rec.txt"
        self.judge(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec), CLAUDE_PLUGIN_OPTION_JUDGE_MODEL="haiku")
        text = rec.read_text(encoding="utf-8")
        argv = text.split("ARGV\n", 1)[1].split("\nSTDIN\n", 1)[0].split("\n")
        self.assertEqual(argv[:9], ["-p", "--setting-sources", "", "--strict-mcp-config",
                                    "--no-session-persistence", "--output-format", "json",
                                    "--tools", ""])
        for whole in ["--json-schema", "--model", "haiku", "--system-prompt", "SYS"]:
            self.assertIn(whole, argv)
        self.assertNotIn("--bare", argv)
        self.assertIn("STDIN\nUSER", text)
        self.assertIn("ENV PLANKA_JUDGE=1", text)
        self.assertIn(f"CWD {self.env.data.resolve()}", text.splitlines())

    def test_default_model(self):
        rec = self.env.data / "rec.txt"
        self.judge(PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        argv = rec.read_text(encoding="utf-8").split("ARGV\n", 1)[1].split("\nSTDIN\n", 1)[0].split("\n")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_non_executable_binary_is_error(self):
        fake = self.env.root / "bin"
        fake.mkdir()
        (fake / "claude").write_text("#!/bin/sh\n")
        v = self.judge(PATH=str(fake))
        self.assertTrue(v.ok)
        self.assertIn("claude", v.error)

    def test_timeout_is_error(self):
        v = self.judge(PLANKA_STUB="hang")
        self.assertTrue(v.ok)
        self.assertIn("таймаут", v.error)

    def test_garbage_is_error(self):
        v = self.judge(PLANKA_STUB="garbage")
        self.assertTrue(v.ok)
        self.assertIsNotNone(v.error)

    def test_not_logged_in_is_error(self):
        v = self.judge(PLANKA_STUB="notlogged")
        self.assertTrue(v.ok)
        self.assertIn("Not logged in", v.error)

    def test_missing_binary_is_error(self):
        v = self.judge(PATH="/nonexistent")
        self.assertTrue(v.ok)
        self.assertIn("claude", v.error)


class DenyBudgetTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_two_denies_then_exhausted(self):
        self.assertFalse(common.deny_budget_exhausted("s", "p", "tool"))
        self.assertFalse(common.deny_budget_exhausted("s", "p", "tool"))
        self.assertTrue(common.deny_budget_exhausted("s", "p", "tool"))
        self.assertFalse(common.deny_budget_exhausted("s", "p", "stop"))
        self.assertFalse(common.deny_budget_exhausted("s", "p2", "tool"))

    def test_counter_survives_sequential_writes(self):
        for _ in range(5):
            common.deny_budget_exhausted("s", "p", "tool")
        state = json.loads((self.env.data / "state" / "s.json").read_text())
        self.assertEqual(state["p:tool"], 5)
        self.assertEqual([p.name for p in (self.env.data / "state").iterdir()], ["s.json"])

    def test_stale_state_is_pruned(self):
        state = self.env.data / "state"
        state.mkdir()
        stale, fresh = state / "old.json", state / "new.json"
        for p in (stale, fresh):
            p.write_text("{}", encoding="utf-8")
        week_ago = time.time() - 8 * 86400
        os.utime(stale, (week_ago, week_ago))
        common.deny_budget_exhausted("s", "p", "tool")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue((state / "s.json").exists())

    def test_prune_state_covers_all_suffixes(self):
        state = self.env.data / "state"
        state.mkdir()
        old = time.time() - 8 * 86400
        for name in ("x.json", "y.snap.json", "z.warned.json"):
            p = state / name
            p.write_text("{}", encoding="utf-8")
            os.utime(p, (old, old))
        (state / "fresh.snap.json").write_text("{}", encoding="utf-8")
        common.prune_state(state)
        self.assertEqual([p.name for p in state.iterdir()], ["fresh.snap.json"])

    def test_session_id_is_sanitized(self):
        common.deny_budget_exhausted("../../x/y", "p", "tool")
        common.deny_budget_exhausted("", "p", "tool")
        names = sorted(p.name for p in (self.env.data / "state").iterdir())
        self.assertEqual(names, [".._.._x_y.json", "unknown.json"])


def run_captured(main):
    """run_hook с перехваченными stdout и stderr: (stdout, stderr)."""
    with mock.patch("sys.stdout", new=io.StringIO()) as out, \
         mock.patch("sys.stderr", new=io.StringIO()) as err:
        common.run_hook(main)
    return out.getvalue(), err.getvalue()


class RunHookTest(unittest.TestCase):
    def test_exception_is_system_message(self):
        def main():
            raise PermissionError("нет доступа")
        out, err = run_captured(main)
        self.assertEqual(err, "")
        msg = json.loads(out)
        self.assertEqual(list(msg), ["systemMessage"])
        self.assertTrue(msg["systemMessage"].startswith("planka: внутренняя ошибка: PermissionError"))

    def test_exception_keeps_emitted_output(self):
        def main():
            common.emit(common.block_output("r"))
            raise ValueError("x")
        out, _ = run_captured(main)
        msg = json.loads(out)
        self.assertEqual(msg["decision"], "block")
        self.assertIn("planka: внутренняя ошибка: ValueError", msg["systemMessage"])

    def test_normal_main_returns(self):
        calls = []
        out, err = run_captured(lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_warnings_only(self):
        def main():
            common.warn("раз")
            common.warn("два")
        out, err = run_captured(main)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"systemMessage": "planka: раз\nplanka: два"})

    def test_merge_with_each_output(self):
        for build in (common.deny_output, common.block_output, common.context_output):
            def main():
                common.warn("внимание")
                common.emit(build("текст"))
            out, err = run_captured(main)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out), {**build("текст"), "systemMessage": "planka: внимание"})

    def test_output_without_warnings(self):
        out, _ = run_captured(lambda: common.emit(common.block_output("r")))
        self.assertEqual(json.loads(out), {"decision": "block", "reason": "r"})

    def test_state_reset_between_runs(self):
        def first():
            common.warn("старое")
            common.emit(common.block_output("r"))
        run_captured(first)
        out, _ = run_captured(lambda: None)
        self.assertEqual(out, "")

    def test_warn_does_not_write_stderr(self):
        common._reset()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            common.warn("x")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(common._messages, ["planka: x"])


class OutputsTest(unittest.TestCase):
    def test_formats(self):
        d = common.deny_output("r")
        self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(d["hookSpecificOutput"]["permissionDecisionReason"], "r")
        self.assertEqual(common.block_output("r"), {"decision": "block", "reason": "r"})
        c = common.context_output("t")
        self.assertEqual(c["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(c["hookSpecificOutput"]["additionalContext"], "t")


class LogTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()
        self.log = self.env.data / "judge.log"

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_log_line(self):
        common.log_event("tool", "s", verdict="ok", reason="")
        lines = self.env.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["hook"], "tool")
        self.assertEqual(lines[0]["session_id"], "s")
        self.assertIn("ts", lines[0])
        self.assertNotIn("content_len", lines[0])
        self.assertNotIn("content_sha256", lines[0])

    def test_content_is_hashed(self):
        text = "секрет // comment"
        common.log_event("stop", "s", verdict="ok", content=text)
        entry = self.env.log_lines()[0]
        self.assertNotIn("content", entry)
        self.assertEqual(entry["content_len"], len(text))
        self.assertEqual(entry["content_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertNotIn("секрет", self.log.read_text(encoding="utf-8"))

    def test_rotation_at_threshold(self):
        self.assertEqual(common.LOG_MAX_BYTES, 1_048_576)
        (self.env.data / "judge.log.1").write_text("прежний\n", encoding="utf-8")
        old = "x" * (common.LOG_MAX_BYTES - 1) + "\n"
        self.log.write_text(old, encoding="utf-8")
        common.log_event("tool", "s", verdict="ok")
        self.assertEqual((self.env.data / "judge.log.1").read_text(encoding="utf-8"), old)
        self.assertEqual(len(self.env.log_lines()), 1)

    def test_no_rotation_below_threshold(self):
        old = "x" * (common.LOG_MAX_BYTES - 2) + "\n"
        self.log.write_text(old, encoding="utf-8")
        common.log_event("tool", "s", verdict="ok")
        self.assertFalse((self.env.data / "judge.log.1").exists())
        self.assertTrue(self.log.read_text(encoding="utf-8").startswith(old))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.base = self.env.environ()

    def tearDown(self):
        self.env.close()

    def test_settings_unset(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": None, "doc_lang": None})

    def test_settings_set(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en+ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "ru"}, clear=True):
            self.assertEqual(common.settings(), {"comment_lang": "en+ru", "doc_lang": "ru"})

    def test_substitute_all_placeholders(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "en"}, clear=True):
            out = common.substitute("a {RULES} b {COMMENT_LANG} c {DOC_LANG}")
            self.assertEqual(out, f"a {self.env.root / 'rules'} b en c en")

    def test_substitute_unset_is_marker(self):
        with mock.patch.dict(os.environ, self.base, clear=True):
            self.assertEqual(common.substitute("{COMMENT_LANG}/{DOC_LANG}"), "не задан/не задан")

    def test_philosophy_and_rules_substituted(self):
        with mock.patch.dict(os.environ, {**self.base, "CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "ru",
                                          "CLAUDE_PLUGIN_OPTION_DOC_LANG": "ru"}, clear=True):
            self.assertIn("Язык комментариев: ru; язык документации: ru.", common.philosophy_text())
            self.assertIn("Язык: ru.", common.rule_texts("comments"))
            self.assertNotIn("{COMMENT_LANG}", common.rule_texts("comments"))


class ProjectRootTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.patch = mock.patch.dict(os.environ, self.env.environ(), clear=True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.close()

    def test_project_dir_wins_over_cwd(self):
        sub = self.env.project / "a" / "b"
        sub.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.env.project)}):
            self.assertEqual(common.project_root(str(sub)), self.env.project)

    def test_non_git_is_cwd(self):
        sub = self.env.project / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(common.project_root(str(sub)), sub)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_toplevel(self):
        subprocess.run(["git", "init", "-q", str(self.env.project)], check=True)
        sub = self.env.project / "pkg"
        sub.mkdir()
        self.assertEqual(common.project_root(str(sub)).resolve(), self.env.project.resolve())

    def test_missing_dir_is_path(self):
        self.assertEqual(common.project_root("/nonexistent/x"), pathlib.Path("/nonexistent/x"))


class DocPathTest(unittest.TestCase):
    def test_doc_paths(self):
        for p in ["README.md", "README", "README.rst", "LICENSE", "CLAUDE.md", "internal/x/CLAUDE.md",
                  "context/a.md", "context/deferred/INDEX.md", "docs/en/x.md", "notes.md"]:
            self.assertTrue(common.is_doc_path(p), p)

    def test_code_paths(self):
        for p in ["main.go", "a/b.py", "Makefile", "docs.py", "context.go", "readme_test.go", "x.toml"]:
            self.assertFalse(common.is_doc_path(p), p)


class PathKindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "README.md": "doc", "README": "doc", "LICENSE": "doc", "docs/en/x.md": "doc",
            "internal/CLAUDE.md": "doc", "context/notes.txt": "doc",
            "context/x.go": "code", "docs/docs.go": "code", "READMEParser.java": "code",
            "LICENSE_check.py": "code",
            "a.mjs": "code", "a.cjs": "code", "a.mts": "code", "a.cts": "code", "a.pyi": "code",
            "a.cxx": "code", "a.hh": "code", "a.hxx": "code", "CMakeLists.txt": "code", "m/x.cmake": "code",
            "pkg/x.go": "code", "a/b.py": "code", "Makefile": "code", "Dockerfile": "code",
            "sub/Justfile": "code", "makefile": "code", "GNUmakefile": "code", "Rakefile": "code", "Gemfile": "code", "x.toml": "code",
            "src/App.TSX": "code", "a.php": "code", "infra/main.tf": "code",
            "a.json": "other", "img.png": "other", "LICENSE-third-party.txt": "doc",
            "notes.txt": "other", "bin/tool": "other", "go.sum": "other",
        }
        for path, kind in cases.items():
            self.assertEqual(common.path_kind(path), kind, path)

    def test_code_exts_cover_comment_families(self):
        import comments
        known = comments._C_FAMILY | comments._HASH | comments._DASH | comments._HTML
        self.assertLessEqual(known, common.CODE_EXTS)
        for ext in ("php", "r", "jl", "ex", "exs", "erl", "clj", "fs", "vb", "nim", "zig", "sol",
                    "proto", "gradle", "groovy", "tf", "nix", "el", "vim", "bat", "cmd"):
            self.assertIn(ext, common.CODE_EXTS)
        self.assertEqual(common.CODE_NAMES, {"Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "Dockerfile",
                                             "Justfile", "Rakefile", "Gemfile"})


class WarnOnceTest(unittest.TestCase):
    def test_once_per_session_and_key(self):
        env = Env()
        try:
            common._reset()
            with mock.patch.dict(os.environ, env.environ(), clear=True):
                self.assertTrue(common.warn_once("s", "lang", "раз"))
                self.assertFalse(common.warn_once("s", "lang", "раз"))
                self.assertTrue(common.warn_once("s", "other", "два"))
                self.assertTrue(common.warn_once("s2", "lang", "три"))
            self.assertEqual(common._messages, ["planka: раз", "planka: два", "planka: три"])
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
