import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.helpers import Env, PHILOSOPHY, PLANKA_DIR, messages, output

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
import remind  # noqa: E402
import snapshot  # noqa: E402


class RemindTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_emits_whole_philosophy(self):
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit", prompt="привет"))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = output(r)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        expected = PHILOSOPHY.replace("{RULES}", str(self.env.root / "rules")) \
            .replace("{COMMENT_LANG}", "ru").replace("{DOC_LANG}", "ru")
        no_docs = f"Проект без документации: предложи автору инициализацию по {self.env.root / 'rules'}/docs.md."
        self.assertEqual(ctx, expected + "\n\n" + no_docs)

    def test_barrier(self):
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit"), PLANKA_JUDGE="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_missing_file_warns_and_emits_nothing(self):
        (self.env.root / "philosophy.md").unlink()
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit"))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))

    def test_non_utf8_file_warns_and_emits_nothing(self):
        (self.env.root / "philosophy.md").write_bytes(b"# \xff\xfe\n")
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit"))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))
        self.assertNotIn("Traceback", r.stderr)

    def test_garbage_stdin(self):
        r = self.env.run("remind.py", "not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_rules_dir_substituted(self):
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit", prompt="привет"))
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("{RULES}", ctx)
        self.assertIn(str(self.env.root / "rules"), ctx)

    def prompt(self, **extra):
        return self.env.run("remind.py", self.env.hook_input("UserPromptSubmit", prompt="x", cwd=str(self.env.project)), **extra)

    def test_no_claude_md_line(self):
        r = self.prompt()
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.endswith(f"Проект без документации: предложи автору инициализацию по {self.env.root / 'rules'}/docs.md."))

    def test_claude_md_present_no_line(self):
        (self.env.project / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
        ctx = output(self.prompt())["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Проект без документации", ctx)

    def test_snapshot_written(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        self.prompt()
        snap = json.loads((self.env.data / "state" / "sess-1.snap.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["prompt_id"], "p-1")
        self.assertEqual(snap["mode"], "walk")
        self.assertEqual(sorted(snap["files"]), ["a.py"])

    def git(self, *args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(self.env.project), *args],
                       check=True, capture_output=True)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_snapshot_ignores_tree_size(self):
        for i in range(5):
            (self.env.project / f"f{i}.py").write_text("x", encoding="utf-8")
        self.git("init", "-q"); self.git("add", "."); self.git("commit", "-qm", "i")
        (self.env.project / "f0.py").write_text("y", encoding="utf-8")
        msgs, _ = self.run_in_process(mock.patch.object(snapshot, "MAX_FILES", 2))
        self.assertEqual(msgs, [])
        snap = json.loads((self.env.data / "state" / "sess-1.snap.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["mode"], "git")
        self.assertEqual(list(snap["dirty"]), ["f0.py"])
        self.assertEqual(snap["head"], snap["repos"][""])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_too_many_dirty_warns(self):
        self.git("init", "-q")
        for i in range(3):
            (self.env.project / f"f{i}").write_text("x", encoding="utf-8")
        msgs, _ = self.run_in_process(mock.patch.object(snapshot, "MAX_FILES", 2))
        self.assertEqual(msgs, ["planka: изменённых и неотслеживаемых файлов больше 2, "
                                "сверка документации не проверяется"])
        self.assertFalse((self.env.data / "state" / "sess-1.snap.json").exists())

    def test_languages_default_ru_without_warning(self):
        r1 = self.prompt()
        self.assertEqual(messages(r1), [])
        self.assertIn("Язык комментариев: ru; язык документации: ru.",
                      output(r1)["hookSpecificOutput"]["additionalContext"])
        r2 = self.prompt(CLAUDE_PLUGIN_OPTION_COMMENT_LANG="en", CLAUDE_PLUGIN_OPTION_DOC_LANG="en")
        ctx = output(r2)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Язык комментариев: en; язык документации: en.", ctx)

    def run_in_process(self, *patches):
        """remind.main через common.run_hook в этом процессе; возвращает строки systemMessage и ответ."""
        raw = json.dumps(self.env.hook_input("UserPromptSubmit", prompt="x", cwd=str(self.env.project)))
        stdin = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")), encoding="utf-8")
        stdout_bytes = io.BytesIO()
        out = io.TextIOWrapper(stdout_bytes, encoding="utf-8", write_through=True)
        with mock.patch.dict(os.environ, self.env.environ(), clear=True), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", new=out):
            for p in patches:
                p.start()
            try:
                common.run_hook(remind.main)
            finally:
                for p in patches:
                    p.stop()
        reply = json.loads(stdout_bytes.getvalue().decode("utf-8"))
        return reply.get("systemMessage", "").splitlines(), reply.get("hookSpecificOutput")

    def test_too_many_files_warns(self):
        for i in range(3):
            (self.env.project / f"f{i}").write_text("x", encoding="utf-8")
        msgs, _ = self.run_in_process(mock.patch.object(snapshot, "MAX_FILES", 2))
        self.assertEqual(msgs, ["planka: дерево больше 2 файлов, сверка документации не проверяется"])
        self.assertFalse((self.env.data / "state" / "sess-1.snap.json").exists())

    def test_too_many_files_warned_once_per_session(self):
        for i in range(3):
            (self.env.project / f"f{i}").write_text("x", encoding="utf-8")
        patch = mock.patch.object(snapshot, "MAX_FILES", 2)
        first, _ = self.run_in_process(patch)
        second, out = self.run_in_process(patch)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertIn("# Философия работы", out["additionalContext"])

    def test_no_cwd_no_snapshot_no_docs_line(self):
        hook_input = self.env.hook_input("UserPromptSubmit", prompt="x")
        del hook_input["cwd"]
        r = self.env.run("remind.py", hook_input)
        self.assertEqual(messages(r), [])
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Проект без документации", ctx)
        self.assertFalse((self.env.data / "state").exists())

    def test_project_root_failure_keeps_reminder(self):
        msgs, out = self.run_in_process(mock.patch.object(common, "project_root", side_effect=OSError("сбой")))
        self.assertIn("# Философия работы", out["additionalContext"])
        self.assertEqual(msgs, ["planka: корень проекта не определён: OSError('сбой')"])

    def test_reminder_survives_unwritable_state(self):
        (self.env.data / "state").write_text("файл вместо каталога", encoding="utf-8")
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.prompt()
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("# Философия работы"))
        self.assertTrue(ctx.endswith(f"Проект без документации: предложи автору инициализацию по {self.env.root / 'rules'}/docs.md."))
        msgs = messages(r)
        self.assertEqual(len(msgs), 1, msgs)
        self.assertTrue(msgs[0].startswith("planka: снимок дерева не записан: "), msgs)
        self.assertNotIn("Traceback", r.stderr)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_slow_git_keeps_reminder(self):
        real_git = shutil.which("git")
        bin_dir = self.env.data / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(f'#!/bin/sh\ncase " $* " in *" status "*) exec sleep 5;; esac\nexec {real_git} "$@"\n',
                                     encoding="utf-8")
        (bin_dir / "git").chmod(0o755)
        path = f"{bin_dir}:{self.env.environ()['PATH']}"
        started = time.monotonic()
        msgs, out = self.run_in_process(mock.patch.object(remind, "SNAPSHOT_BUDGET", 1),
                                        mock.patch.dict(os.environ, {"PATH": path}))
        self.assertLess(time.monotonic() - started, 4)
        self.assertIn("# Философия работы", out["additionalContext"])
        self.assertTrue(any("снимок дерева не записан" in m for m in msgs), msgs)

    def test_reminder_survives_snapshot_error(self):
        msgs, out = self.run_in_process(mock.patch.object(snapshot, "capture", side_effect=ValueError("сбой")))
        self.assertIn("# Философия работы", out["additionalContext"])
        self.assertEqual(msgs, ["planka: снимок дерева не записан: ValueError('сбой')"])

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
