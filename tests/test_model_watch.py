"""model_watch: модель сессии из SessionStart и PostModelSwitch в состояние; judge_model берёт её первой."""
import json
import os
import sys
import unittest
from unittest import mock

from tests.helpers import PLANKA_DIR, Env, messages

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402


class ModelWatchTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def state_file(self, session="sess-1"):
        return self.env.data / "state" / f"{session}.model.json"

    def stored(self, session="sess-1"):
        p = self.state_file(session)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def run_hook(self, event, extra_env=None, **fields):
        r = self.env.run("model_watch.py", self.env.hook_input(event, **fields), **(extra_env or {}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r

    def start(self, **fields):
        return self.run_hook("SessionStart", source="startup", **fields)

    def switch(self, to_model, **fields):
        return self.run_hook("PostModelSwitch", from_model="claude-sonnet-5", to_model=to_model,
                             requested_model=None, source="command", **fields)

    def judge_model(self, session="sess-1", transcript=None):
        common._reset()
        data = {"session_id": session, "transcript_path": str(transcript or self.env.transcript)}
        with mock.patch.dict(os.environ, self.env.environ(), clear=True):
            return common.judge_model(data)

    def test_session_start_model_stored_silently(self):
        r = self.start(model="claude-opus-5")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.stored(), {"model": "claude-opus-5"})

    def test_post_model_switch_overrides(self):
        self.start(model="claude-sonnet-5")
        r = self.switch("claude-opus-5")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.stored(), {"model": "claude-opus-5"})

    def test_session_start_without_model_keeps_known(self):
        # После /clear и при восстановлении сессии Claude Code опускает model.
        self.switch("claude-opus-5")
        for fields in ({}, {"model": None}, {"model": ""}, {"model": 7}):
            r = self.run_hook("SessionStart", source="clear", **fields)
            self.assertEqual(r.stdout, "")
            self.assertEqual(self.stored(), {"model": "claude-opus-5"}, fields)

    def test_session_start_without_model_writes_nothing(self):
        r = self.run_hook("SessionStart", source="resume")
        self.assertEqual(r.stdout, "")
        self.assertIsNone(self.stored())

    def test_switch_without_usable_model_warns_and_keeps(self):
        self.start(model="claude-opus-5")
        for bad in (None, "", 7, "<synthetic>"):
            r = self.switch(bad)
            self.assertEqual(messages(r), ["planka: PostModelSwitch без to_model, модель сессии не обновлена"])
            self.assertEqual(self.stored(), {"model": "claude-opus-5"}, bad)

    def test_service_model_not_stored(self):
        r = self.start(model="<synthetic>")
        self.assertEqual(r.stdout, "")
        self.assertIsNone(self.stored())

    def test_barrier(self):
        r = self.run_hook("SessionStart", {"PLANKA_JUDGE": "1"}, source="startup", model="claude-haiku-5")
        self.assertEqual(r.stdout, "")
        self.assertIsNone(self.stored())
        r = self.run_hook("PostModelSwitch", {"PLANKA_JUDGE": "1"}, to_model="claude-haiku-5")
        self.assertEqual(r.stdout, "")
        self.assertFalse((self.env.data / "state").exists())

    def test_broken_state_overwritten(self):
        state = self.env.data / "state"
        state.mkdir()
        for broken in ("не json", "[1]", '{"model": 7}'):
            self.state_file().write_text(broken, encoding="utf-8")
            r = self.switch("claude-opus-5")
            self.assertEqual(r.stdout, "")
            self.assertEqual(self.stored(), {"model": "claude-opus-5"}, broken)

    def test_unsafe_session_id(self):
        self.start(model="claude-opus-5", session_id="../x/y")
        self.assertEqual(self.stored(".._x_y"), {"model": "claude-opus-5"})
        self.assertEqual(self.judge_model("../x/y"), "claude-opus-5")

    def test_bad_input_silent(self):
        for raw in ("", "не json", "[1]"):
            r = self.env.run("model_watch.py", raw)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), raw)
        self.assertFalse((self.env.data / "state").exists())

    def test_write_failure_is_warning(self):
        (self.env.data / "state").write_text("не каталог", encoding="utf-8")
        r = self.start(model="claude-opus-5")
        lines = messages(r)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("planka: модель сессии не записана: "), lines)

    def test_judge_takes_stored_model_over_transcript(self):
        # Транскрипт Env отвечает моделью claude-test-model; событие сессии — модель новее.
        self.assertEqual(self.judge_model(), "claude-test-model")
        self.switch("claude-opus-5")
        self.assertEqual(self.judge_model(), "claude-opus-5")
        self.assertEqual(common._messages, [])
        # Состояние другой сессии не подменяет модель этой.
        self.assertEqual(self.judge_model("sess-2"), "claude-test-model")

    def test_judge_falls_back_to_transcript_on_broken_state(self):
        state = self.env.data / "state"
        state.mkdir()
        for broken in ("не json", "[1]", '{"model": 7}', '{"model": "<synthetic>"}', '{"model": ""}'):
            self.state_file().write_text(broken, encoding="utf-8")
            self.assertEqual(self.judge_model(), "claude-test-model", broken)

    def test_judge_stored_model_without_transcript(self):
        self.start(model="claude-opus-5")
        missing = self.env.data / "нет.jsonl"
        self.assertEqual(self.judge_model(transcript=missing), "claude-opus-5")
        self.assertEqual(common._messages, [])

    def test_explicit_setting_still_wins(self):
        self.start(model="claude-opus-5")
        common._reset()
        with mock.patch.dict(os.environ, self.env.environ(CLAUDE_PLUGIN_OPTION_JUDGE_MODEL="haiku"), clear=True):
            self.assertEqual(common.judge_model({"session_id": "sess-1"}), "haiku")

    def test_pruned_with_state(self):
        self.start(model="claude-opus-5")
        old = common.time.time() - common.STATE_TTL - 10
        os.utime(self.state_file(), (old, old))
        common.prune_state(self.env.data / "state")
        self.assertFalse(self.state_file().exists())


if __name__ == "__main__":
    unittest.main()
