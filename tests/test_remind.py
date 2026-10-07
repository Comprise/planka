import json
import os
import time
import unittest

from tests.helpers import Env, hook_input, messages, output


class RemindTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_emits_whole_philosophy(self):
        r = self.env.run("remind.py", hook_input("UserPromptSubmit", prompt="привет"))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = output(r)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertTrue(ctx.startswith("# Философия работы"))
        self.assertIn("## Планы", ctx)

    def test_barrier(self):
        r = self.env.run("remind.py", hook_input("UserPromptSubmit"), PLANKA_JUDGE="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_missing_file_warns_and_emits_nothing(self):
        (self.env.root / "philosophy.md").unlink()
        r = self.env.run("remind.py", hook_input("UserPromptSubmit"))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))

    def test_non_utf8_file_warns_and_emits_nothing(self):
        (self.env.root / "philosophy.md").write_bytes(b"# \xff\xfe\n")
        r = self.env.run("remind.py", hook_input("UserPromptSubmit"))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))
        self.assertNotIn("Traceback", r.stderr)

    def test_garbage_stdin(self):
        r = self.env.run("remind.py", "not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_rules_dir_substituted(self):
        r = self.env.run("remind.py", hook_input("UserPromptSubmit", prompt="привет"))
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("{RULES}", ctx)
        self.assertIn(str(self.env.root / "rules"), ctx)

    def prompt(self, **extra):
        return self.env.run("remind.py", hook_input("UserPromptSubmit", prompt="x", cwd=str(self.env.project)), **extra)

    def test_no_claude_md_line(self):
        r = self.prompt()
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.endswith("Проект без документации: предложи автору инициализацию по rules/docs.md."))

    def test_claude_md_present_no_line(self):
        (self.env.project / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
        ctx = output(self.prompt())["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Проект без документации", ctx)

    def test_snapshot_written(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        self.prompt()
        snap = json.loads((self.env.data / "state" / "sess-1.snap.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["prompt_id"], "p-1")
        self.assertEqual(sorted(snap["files"]), ["a.py"])

    def test_language_warning_once(self):
        r1 = self.prompt()
        r2 = self.prompt()
        self.assertIn("planka: задайте comment_lang и doc_lang: claude plugin configure planka@planka --values-stdin", messages(r1))
        self.assertNotIn("comment_lang", "\n".join(messages(r2)))
        r3 = self.prompt(CLAUDE_PLUGIN_OPTION_COMMENT_LANG="en", CLAUDE_PLUGIN_OPTION_DOC_LANG="en")
        ctx = output(r3)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Язык комментариев: en; язык документации: en.", ctx)

    def test_too_many_files_warns(self):
        for i in range(3):
            (self.env.project / f"f{i}").write_text("x", encoding="utf-8")
        r = self.prompt(PLANKA_TEST_MAX_FILES="2")
        self.assertIn("50000", "\n".join(messages(r)))
        self.assertFalse((self.env.data / "state" / "sess-1.snap.json").exists())

    def test_reminder_survives_unwritable_state(self):
        (self.env.data / "state").write_text("файл вместо каталога", encoding="utf-8")
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.prompt()
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("# Философия работы"))
        self.assertTrue(ctx.endswith("Проект без документации: предложи автору инициализацию по rules/docs.md."))
        self.assertIn("planka:", "\n".join(messages(r)))
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("внутренняя ошибка", "\n".join(messages(r)))

    def test_reminder_survives_bad_test_limit(self):
        r = self.prompt(PLANKA_TEST_MAX_FILES="не число")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("# Философия работы", output(r)["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("внутренняя ошибка", "\n".join(messages(r)))

    def test_both_labels_when_state_unwritable(self):
        (self.env.data / "state").write_text("файл вместо каталога", encoding="utf-8")
        r = self.prompt()
        self.assertIn("снимок дерева не записан:", "\n".join(messages(r)))
        self.assertIn("предупреждение о языке не записано:", "\n".join(messages(r)))

    def test_bad_test_limit_only_snapshot_label(self):
        r = self.prompt(PLANKA_TEST_MAX_FILES="не число")
        self.assertIn("снимок дерева не записан:", "\n".join(messages(r)))
        self.assertNotIn("предупреждение о языке не записано", "\n".join(messages(r)))

    def test_snapshot_prunes_stale_state(self):
        state = self.env.data / "state"
        state.mkdir()
        stale = state / "old.snap.json"
        stale.write_text("{}", encoding="utf-8")
        week_ago = time.time() - 8 * 86400
        os.utime(stale, (week_ago, week_ago))
        self.prompt()
        self.assertFalse(stale.exists())
        self.assertTrue((state / "sess-1.snap.json").exists())


if __name__ == "__main__":
    unittest.main()
