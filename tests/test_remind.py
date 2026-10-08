import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import helpers
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

    def run_part(self, *args, environ=None):
        """remind.py с аргументами args подпроцессом, как его зовёт hooks.json; environ — добавка к окружению."""
        hook_input = self.env.hook_input("UserPromptSubmit", prompt="привет")
        return subprocess.run([sys.executable, str(PLANKA_DIR / "remind.py"), *args], input=json.dumps(hook_input),
                              capture_output=True, text=True, encoding="utf-8", env=self.env.environ(**(environ or {})),
                              timeout=30)

    def all_parts(self, environ=None):
        """Склейка additionalContext всех частей ядра; проверяет, что предупреждений нет."""
        contexts = []
        for k in range(1, remind.PARTS + 1):
            r = self.run_part(str(k), environ=environ)
            self.assertEqual(messages(r), [])
            contexts.append(output(r)["hookSpecificOutput"]["additionalContext"])
        return "\n\n".join(contexts)

    def expected_core(self):
        return PHILOSOPHY.replace("{RULES}", str(self.env.root / "rules")) \
            .replace("{COMMENT_LANG}", "ru").replace("{DOC_LANG}", "ru")

    def test_parts_cover_philosophy(self):
        no_docs = f"Проект без документации: предложи автору инициализацию по {self.env.root / 'rules'}/docs.md."
        parts = remind.split_core(self.expected_core(), remind.PARTS)
        contexts = []
        for k in range(1, remind.PARTS + 1):
            r = self.run_part(str(k))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(messages(r), [])
            out = output(r)["hookSpecificOutput"]
            self.assertEqual(out["hookEventName"], "UserPromptSubmit")
            contexts.append(out["additionalContext"])
        self.assertEqual(contexts[0], parts[0].rstrip() + "\n\n" + no_docs)
        for k in range(2, remind.PARTS + 1):
            self.assertEqual(contexts[k - 1],
                             remind.CONTINUATION.format(k=k, n=remind.PARTS) + "\n\n" + parts[k - 1].rstrip())
        self.assertEqual("".join(parts), self.expected_core())
        self.assertTrue(all(p.strip() for p in parts))

    def test_split_core_balanced_by_sections(self):
        text = "# T\n\n## A\n\n" + "a" * 50 + "\n\n## B\n\n" + "b" * 10 + "\n\n## C\n\n" + "c" * 40 + "\n"
        self.assertEqual(remind.split_core(text, 2),
                         ["# T\n\n## A\n\n" + "a" * 50 + "\n\n",
                          "## B\n\n" + "b" * 10 + "\n\n## C\n\n" + "c" * 40 + "\n"])

    def test_split_core_fewer_sections_than_parts(self):
        self.assertEqual(remind.split_core("# T\n\n## A\n", 3), ["# T\n\n", "## A\n", ""])

    def test_no_argument_is_first_part(self):
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit", prompt="привет"))
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("# Философия работы"))
        self.assertTrue((self.env.data / "state" / "sess-1.snap.json").exists())

    def test_later_part_takes_no_snapshot_and_no_docs_line(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.run_part("2")
        self.assertEqual(messages(r), [])
        ctx = output(r)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith(remind.CONTINUATION.format(k=2, n=remind.PARTS)))
        self.assertNotIn("Проект без документации", ctx)
        self.assertFalse((self.env.data / "state").exists())

    def test_bad_part_number_warns(self):
        for arg in ("0", str(remind.PARTS + 1), "x"):
            r = self.run_part(arg)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(messages(r), [f"planka: неверный номер части ядра: {arg!r}"], arg)
            self.assertNotIn("hookSpecificOutput", json.loads(r.stdout))

    def test_part_over_limit_warned_and_emitted(self):
        msgs, out = self.run_in_process(mock.patch.object(remind, "CONTEXT_LIMIT", 10))
        self.assertTrue(out["additionalContext"].startswith("# Философия работы"))
        self.assertEqual(msgs, ["planka: часть 1 ядра длиннее 10 символов: Claude Code отдаст агенту только её начало"])

    def test_barrier(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit"), PLANKA_JUDGE="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        # Барьер до снимка: внутри судьи хук не пишет состояние.
        self.assertFalse((self.env.data / "state").exists())

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
        ctx = self.all_parts()
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
        snap = snapshot.load(self.env.data / "state", "sess-1")
        self.assertEqual(snap["prompt_id"], "p-1")
        self.assertEqual(snap["mode"], "walk")
        self.assertEqual(self.walk_paths(snap), ["a.py"])

    @staticmethod
    def walk_paths(snap):
        """Пути файлов снимка walk."""
        return sorted((f"{rel}/" if rel else "") + os.fsdecode(name)
                      for rel, block in snap["dirs"].items() for name in snapshot._records(block))

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
        self.assertIn("Язык комментариев: ru; язык документации: ru.", self.all_parts())
        ctx = self.all_parts({"CLAUDE_PLUGIN_OPTION_COMMENT_LANG": "en", "CLAUDE_PLUGIN_OPTION_DOC_LANG": "en"})
        self.assertIn("Язык комментариев: en; язык документации: en.", ctx)

    def run_in_process(self, *patches, **environ):
        """remind.main в этом процессе (helpers.run_in_process) под подменами patches; строки systemMessage и
        hookSpecificOutput ответа."""
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            reply = helpers.run_in_process(self.env, remind.main, self.env.hook_input("UserPromptSubmit", prompt="x"),
                                           **environ)
        return reply.get("systemMessage", "").splitlines(), reply.get("hookSpecificOutput")

    def test_tree_outside_git_not_limited_by_file_count(self):
        for i in range(3):
            (self.env.project / f"f{i}").write_text("x", encoding="utf-8")
        msgs, _ = self.run_in_process(mock.patch.object(snapshot, "MAX_FILES", 2))
        self.assertEqual(msgs, [])
        snap = snapshot.load(self.env.data / "state", "sess-1")
        self.assertEqual(self.walk_paths(snap), ["f0", "f1", "f2"])

    def test_snapshot_timeout_warned_once_per_session(self):
        (self.env.project / "a.py").write_text("x", encoding="utf-8")
        patch = mock.patch.object(remind, "SNAPSHOT_BUDGET", -1)
        first, _ = self.run_in_process(patch)
        second, out = self.run_in_process(patch)
        self.assertEqual(first, ["planka: снимок не уложился в срок, сверка документации не проверяется"])
        self.assertEqual(second, [])
        self.assertIn("# Философия работы", out["additionalContext"])
        self.assertFalse((self.env.data / "state" / "sess-1.snap.json").exists())

    def test_failed_snapshot_not_repeated_for_same_root(self):
        capture = mock.Mock(side_effect=snapshot.TooManyFiles("изменённых и неотслеживаемых файлов больше 2"))
        patch = mock.patch.object(snapshot, "capture", capture)
        first, _ = self.run_in_process(patch)
        second, out = self.run_in_process(patch)
        self.assertEqual(first, ["planka: изменённых и неотслеживаемых файлов больше 2, "
                                 "сверка документации не проверяется"])
        self.assertEqual(second, [])
        self.assertIn("# Философия работы", out["additionalContext"])
        self.assertEqual(capture.call_count, 1)
        failed = json.loads((self.env.data / "state" / "sess-1.snapfail.json").read_text(encoding="utf-8"))
        self.assertEqual(failed, {str(self.env.project): "изменённых и неотслеживаемых файлов больше 2"})

    def test_failed_snapshot_retried_for_other_root(self):
        other = self.env.data / "other"
        other.mkdir()
        capture = mock.Mock(side_effect=TimeoutError("снимок не уложился в срок"))
        with mock.patch.object(snapshot, "capture", capture):
            self.run_in_process()
            msgs, _ = self.run_in_process(CLAUDE_PROJECT_DIR=str(other))
        self.assertEqual(msgs, ["planka: снимок не уложился в срок, сверка документации не проверяется"])
        self.assertEqual([c.args[0] for c in capture.call_args_list], [self.env.project, other])

    def test_unchecked_snapshot_kept_as_base(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        self.prompt()
        (self.env.project / "b.py").write_text("x\n", encoding="utf-8")
        r = self.env.run("remind.py", self.env.hook_input("UserPromptSubmit", prompt="x", prompt_id="p-2"))
        self.assertEqual(messages(r), [])
        snap = snapshot.load(self.env.data / "state", "sess-1")
        self.assertEqual((snap["prompt_id"], self.walk_paths(snap)), ("p-2", ["a.py"]))

    def test_checked_snapshot_replaced(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        self.prompt()
        snapshot.mark_checked(self.env.data / "state", "sess-1", "p-1")
        (self.env.project / "b.py").write_text("x\n", encoding="utf-8")
        self.env.run("remind.py", self.env.hook_input("UserPromptSubmit", prompt="x", prompt_id="p-2"))
        snap = snapshot.load(self.env.data / "state", "sess-1")
        self.assertEqual((snap["prompt_id"], snap["checked"], self.walk_paths(snap)), ("p-2", False, ["a.py", "b.py"]))

    def test_walk_timeout_warns(self):
        (self.env.project / "a.py").write_text("x", encoding="utf-8")
        timeout = TimeoutError("снимок не уложился в срок")
        msgs, _ = self.run_in_process(mock.patch.object(snapshot, "_walk_dirs", side_effect=timeout))
        self.assertEqual(msgs, ["planka: снимок не уложился в срок, сверка документации не проверяется"])
        self.assertFalse((self.env.data / "state" / "sess-1.snap.json").exists())

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
        # git status, который дожил до конца, оставляет метку: срок 1 с прерывает его на sleep 5.
        finished = self.env.data / "status-finished"
        (bin_dir / "git").write_text(f'#!/bin/sh\ncase " $* " in *" status "*) sleep 5; : > "{finished}";; esac\n'
                                     f'exec {real_git} "$@"\n', encoding="utf-8")
        (bin_dir / "git").chmod(0o755)
        path = f"{bin_dir}:{self.env.environ()['PATH']}"
        msgs, out = self.run_in_process(mock.patch.object(remind, "SNAPSHOT_BUDGET", 1), PATH=path)
        self.assertFalse(finished.exists())
        self.assertIn("# Философия работы", out["additionalContext"])
        self.assertEqual(msgs, ["planka: git не уложился в срок снимка, сверка документации не проверяется"])

    def test_null_session_id_snapshot_written(self):
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        hook_input = self.env.hook_input("UserPromptSubmit", prompt="x", session_id=None)
        reply = helpers.run_in_process(self.env, remind.main, hook_input)
        self.assertNotIn("systemMessage", reply)
        self.assertIsNotNone(snapshot.load(self.env.data / "state", ""))

    def test_snapshot_pack_counts_in_budget(self):
        # Обход уложился, упаковка блоков walk — уже за сроком: снимок не пишется, неудача запоминается.
        (self.env.project / "a.py").write_text("x\n", encoding="utf-8")
        real = snapshot.capture

        def slow_capture(root, deadline):
            snap = real(root, deadline)
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
            return snap
        msgs, out = self.run_in_process(mock.patch.object(remind, "SNAPSHOT_BUDGET", 0.3),
                                        mock.patch.object(snapshot, "capture", slow_capture))
        self.assertEqual(msgs, ["planka: снимок не уложился в срок, сверка документации не проверяется"])
        self.assertIn("# Философия работы", out["additionalContext"])
        self.assertFalse((self.env.data / "state" / "sess-1.snap.json").exists())
        self.assertIn(str(self.env.project), json.loads(
            (self.env.data / "state" / "sess-1.snapfail.json").read_text(encoding="utf-8")))

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

