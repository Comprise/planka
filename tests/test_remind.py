import json
import unittest

from tests.helpers import Env, hook_input


class RemindTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_emits_whole_philosophy(self):
        r = self.env.run("remind.py", hook_input("UserPromptSubmit", prompt="привет"))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertTrue(ctx.startswith("# Философия работы"))
        self.assertIn("## Планы", ctx)

    def test_barriers(self):
        for var in ("PLANKA_OFF", "PLANKA_JUDGE"):
            r = self.env.run("remind.py", hook_input("UserPromptSubmit"), **{var: "1"})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")

    def test_missing_file_warns_and_emits_nothing(self):
        (self.env.root / "philosophy.md").unlink()
        r = self.env.run("remind.py", hook_input("UserPromptSubmit"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)

    def test_non_utf8_file_warns_and_emits_nothing(self):
        (self.env.root / "philosophy.md").write_bytes(b"# \xff\xfe\n")
        r = self.env.run("remind.py", hook_input("UserPromptSubmit"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("planka:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_garbage_stdin(self):
        r = self.env.run("remind.py", "not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
