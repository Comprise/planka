import json
import sys
import unittest

from tests.helpers import Env, PLANKA_DIR, hook_input

sys.path.insert(0, str(PLANKA_DIR))
import judge_stop  # noqa: E402

OPTIONS_MSG = """Есть два подхода:

1. Заплатка — три строки (рекомендую).
2. Перестроить владение — снимает причину.

Какой берём?"""

PLAIN_MSG = "Смотрю, что сломалось."


class FilterTest(unittest.TestCase):
    def test_filter(self):
        self.assertTrue(judge_stop.looks_like_options(OPTIONS_MSG))
        self.assertTrue(judge_stop.looks_like_options("Options:\n- A (Recommended)\n- B"))
        self.assertFalse(judge_stop.looks_like_options(PLAIN_MSG))
        self.assertFalse(judge_stop.looks_like_options("рекомендую перезапустить"))
        self.assertFalse(judge_stop.looks_like_options("- один пункт\nи вариант в прозе"))


class StopHookTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def stop(self, msg, **extra):
        return self.env.run("judge_stop.py",
                            hook_input("Stop", last_assistant_message=msg, stop_hook_active=False),
                            **extra)

    def test_plain_message_passes_without_judge(self):
        rec = self.env.data / "rec.txt"
        r = self.stop(PLAIN_MSG, PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertFalse(rec.exists())

    def test_options_ok_passes(self):
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="ok")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        log = self.env.log_lines()
        self.assertEqual(log[-1]["verdict"], "ok")

    def test_options_denied_blocks_with_reason(self):
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="deny", PLANKA_STUB_REASON="нет варианта с причиной")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("нет варианта с причиной", out["reason"])
        self.assertIn("Решения 4", out["reason"])
        self.assertTrue(out["reason"].startswith("planka: "))

    def test_judge_gets_message_and_rubric(self):
        rec = self.env.data / "rec.txt"
        self.stop(OPTIONS_MSG, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("## Решения", text)
        self.assertNotIn("## Поведение", text)
        self.assertIn("<content>\n" + OPTIONS_MSG, text)

    def test_budget_exhausted_passes_with_warning(self):
        for _ in range(2):
            r = self.stop(OPTIONS_MSG, PLANKA_STUB="deny")
            self.assertIn("block", r.stdout)
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")
        self.assertIn("лимит отказов", r.stderr)

    def test_judge_failure_passes_with_warning(self):
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="garbage")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_unusable_data_dir_passes_without_block(self):
        blocker = self.env.data / "file"
        blocker.write_text("", encoding="utf-8")
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="deny", CLAUDE_PLUGIN_DATA=str(blocker / "sub"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka: внутренняя ошибка", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_barrier_and_garbage(self):
        self.assertEqual(self.stop(OPTIONS_MSG, PLANKA_OFF="1").stdout, "")
        r = self.env.run("judge_stop.py", "garbage")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class MissingRubricTest(unittest.TestCase):
    def test_without_section_is_logged(self):
        env = Env(philosophy="# X\n\n## Планы\n\n1. a\n")
        try:
            r = env.run("judge_stop.py", hook_input("Stop", last_assistant_message=OPTIONS_MSG))
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
            self.assertEqual(r.stderr.count("planka:"), 1)
            last = env.log_lines()[-1]
            self.assertEqual(last["verdict"], "skipped")
            self.assertEqual(last["error"], "нет раздела рубрики")
        finally:
            env.close()


DONE_MSG = "Готово: тесты зелёные, 82/82."
BOTH_MSG = OPTIONS_MSG + "\n\nПервый вариант уже сделан."


class DoneFilterTest(unittest.TestCase):
    def test_filter(self):
        self.assertTrue(judge_stop.claims_done(DONE_MSG))
        self.assertTrue(judge_stop.claims_done("Fixed, all tests passing."))
        self.assertTrue(judge_stop.claims_done("Исправлено."))
        self.assertFalse(judge_stop.claims_done("Готовлю план."))
        self.assertFalse(judge_stop.claims_done("Я готов обсудить"))
        self.assertFalse(judge_stop.claims_done("Готов к работе"))
        self.assertFalse(judge_stop.claims_done("Смотрю, что сломалось."))
        self.assertFalse(judge_stop.claims_done(""))
        self.assertFalse(judge_stop.claims_done(None))


class DoneHookTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def stop(self, msg, **extra):
        return self.env.run("judge_stop.py",
                            hook_input("Stop", last_assistant_message=msg, stop_hook_active=False), **extra)

    def test_done_judged_with_verification_module(self):
        rec = self.env.data / "rec.txt"
        r = self.stop(DONE_MSG, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("# Доказательство", text)
        self.assertNotIn("## Решения", text)
        self.assertIn("команда-доказательство", text)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["done"])

    def test_done_denied_blocks(self):
        r = self.stop(DONE_MSG, PLANKA_STUB="deny", PLANKA_STUB_REASON="нет команды-доказательства")
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("нет команды-доказательства", out["reason"])
        self.assertTrue(out["reason"].startswith("planka: "))

    def test_both_filters_one_call(self):
        rec = self.env.data / "rec.txt"
        r = self.stop(BOTH_MSG, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("## Решения", text)
        self.assertIn("# Доказательство", text)
        self.assertIn("самый правильный", text)
        self.assertIn("команда-доказательство", text)
        self.assertEqual(text.count("\n<content>\n"), 1)
        self.assertEqual(text.count("\n</content>\n"), 1)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["options", "done"])

    def test_done_without_module_skips(self):
        (self.env.root / "rules" / "verification.md").unlink()
        r = self.stop(DONE_MSG)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["done"])
        self.assertEqual(r.stderr.count("planka:"), 1)


if __name__ == "__main__":
    unittest.main()
