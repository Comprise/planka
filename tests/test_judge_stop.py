import hashlib
import json
import os
import sys
import unittest
from unittest import mock

from tests.helpers import Env, PLANKA_DIR, hook_input, messages, output

sys.path.insert(0, str(PLANKA_DIR))
import judge_stop  # noqa: E402
import prompts  # noqa: E402
import snapshot  # noqa: E402

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
        out = output(r)
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
            self.assertEqual(output(r)["decision"], "block")
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="deny")
        self.assertIsNone(output(r))
        self.assertIn("лимит отказов", "\n".join(messages(r)))

    def test_judge_failure_passes_with_warning(self):
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="garbage")
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")

    def test_unusable_data_dir_passes_without_block(self):
        blocker = self.env.data / "file"
        blocker.write_text("", encoding="utf-8")
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="deny", CLAUDE_PLUGIN_DATA=str(blocker / "sub"))
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(output(r))
        self.assertIn("planka: внутренняя ошибка", "\n".join(messages(r)))
        self.assertNotIn("Traceback", r.stderr)

    def test_barrier_and_garbage(self):
        self.assertEqual(self.stop(OPTIONS_MSG, PLANKA_JUDGE="1").stdout, "")
        r = self.env.run("judge_stop.py", "garbage")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class MissingRubricTest(unittest.TestCase):
    def test_without_section_is_logged(self):
        env = Env(philosophy="# X\n\n## Планы\n\n1. a\n")
        try:
            r = env.run("judge_stop.py", hook_input("Stop", last_assistant_message=OPTIONS_MSG))
            self.assertEqual(r.returncode, 0)
            self.assertIsNone(output(r))
            self.assertEqual("\n".join(messages(r)).count("planka:"), 1)
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
        out = output(r)
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
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["done"])
        self.assertEqual("\n".join(messages(r)).count("planka:"), 1)


class DocsFilterTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.project = self.env.project
        (self.project / "pkg").mkdir()

    def tearDown(self):
        self.env.close()

    def snap(self, prompt_id="p-1"):
        state = self.env.data / "state"
        state.mkdir(exist_ok=True)
        snapshot.save(state, "sess-1", prompt_id, self.project, snapshot.scan(self.project))

    def stop(self, msg, **extra):
        return self.env.run("judge_stop.py", hook_input(
            "Stop", last_assistant_message=msg, stop_hook_active=False, cwd=str(self.project)), **extra)

    def test_code_change_triggers_docs_judge(self):
        self.snap()
        (self.project / "pkg" / "a.go").write_text("// hello\nx := 1\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.stop("Поправил.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertIn("# Документация", text)
        self.assertIn("# Комментарии", text)
        self.assertIn("pkg/a.go — код", text)
        self.assertIn("pkg/a.go: // hello", text)
        self.assertIn("В корне проекта нет CLAUDE.md.", text)
        self.assertIn("локальный CLAUDE.md", text)
        last = self.env.log_lines()[-1]
        self.assertEqual(last["filters"], ["docs"])
        judged = text.split("\n<content>\n", 1)[1].split("\n</content>\n", 1)[0]
        self.assertIn("pkg/a.go: // hello", judged)
        self.assertNotIn("content", last)
        self.assertEqual(last["content_len"], len(judged))
        self.assertEqual(last["content_sha256"], hashlib.sha256(judged.encode("utf-8")).hexdigest())

    def test_code_change_without_comments_says_so(self):
        self.snap()
        (self.project / "a.py").write_text("x = 1\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        self.stop("Поправил.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("В изменённых файлах кода комментарии не добавлены.", text)
        self.assertIn("При отказе назови файл, который нужно сверить, или строку комментария, "
                      "которую нужно переписать.", text)

    def test_unknown_syntax_named_not_none_added(self):
        self.snap()
        (self.project / "pkg" / "data.erl").write_text("% x\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        self.stop("Поправил.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("Файлы без известного синтаксиса комментариев, судятся по самоотчёту: pkg/data.erl", text)
        self.assertNotIn("комментарии не добавлены", text)

    def test_absent_prompt_id_matches_empty_snapshot(self):
        self.snap(prompt_id="")
        (self.project / "a.py").write_text("x = 1\n", encoding="utf-8")
        data = hook_input("Stop", last_assistant_message="Поправил.", stop_hook_active=False, cwd=str(self.project))
        del data["prompt_id"]
        rec = self.env.data / "rec.txt"
        r = self.env.run("judge_stop.py", data, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["docs"])

    def test_cd_into_subdir_keeps_project_root(self):
        project_dir = str(self.project)
        r = self.env.run("remind.py", hook_input("UserPromptSubmit", prompt="x", cwd=str(self.project / "pkg")),
                         CLAUDE_PROJECT_DIR=project_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.project / "a.py").write_text("x = 1\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.env.run("judge_stop.py", hook_input(
            "Stop", last_assistant_message="Поправил.", stop_hook_active=False, cwd=str(self.project / "pkg")),
            CLAUDE_PROJECT_DIR=project_dir, PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertIn("- a.py — код", rec.read_text(encoding="utf-8"))
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["docs"])

    def test_docs_only_change_no_trigger(self):
        self.snap()
        (self.project / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
        (self.project / "notes.md").write_text("n\n", encoding="utf-8")
        r = self.stop("Поправил.")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_other_only_change_no_trigger(self):
        for files in (["a.json"], ["img.png"], [".claude/settings.local.json"],
                      ["a.json", "img.png", ".claude/settings.local.json", "notes.md"]):
            with self.subTest(files=files):
                self.snap()
                for rel in files:
                    path = self.project / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"{files}\n", encoding="utf-8")
                r = self.stop("Поправил.")
                self.assertEqual(r.stdout, "")
                self.assertEqual(self.env.log_lines(), [])

    def test_deleted_code_file_alone_triggers_docs_judge(self):
        (self.project / "old.go").write_text("// old\n", encoding="utf-8")
        self.snap()
        (self.project / "old.go").unlink()
        rec = self.env.data / "rec.txt"
        self.stop("Удалил.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertIn("- old.go — код, удалён", rec.read_text(encoding="utf-8"))
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["docs"])

    def test_three_kinds_and_deleted_listed(self):
        (self.project / "old.go").write_text("// old\n", encoding="utf-8")
        (self.project / "old.erl").write_text("% old\n", encoding="utf-8")
        (self.project / "gone.json").write_text("{}\n", encoding="utf-8")
        self.snap()
        (self.project / "old.go").unlink()
        (self.project / "old.erl").unlink()
        (self.project / "gone.json").unlink()
        (self.project / "a.py").write_text("# new\n", encoding="utf-8")
        (self.project / "b.erl").write_text("% x\n", encoding="utf-8")
        (self.project / "c.json").write_text("{}\n", encoding="utf-8")
        (self.project / "d.md").write_text("d\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.stop("Поправил.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        listed = text.split("Изменённые файлы за ход:\n", 1)[1].split("\n\n", 1)[0].split("\n")
        self.assertEqual(listed, ["- a.py — код", "- b.erl — код", "- c.json — прочее", "- d.md — документация",
                                  "- gone.json — прочее, удалён", "- old.erl — код, удалён",
                                  "- old.go — код, удалён"])
        self.assertIn("a.py: # new", text)
        self.assertIn("Файлы без известного синтаксиса комментариев, судятся по самоотчёту: b.erl\n", text)
        self.assertEqual(self.env.log_lines()[-1]["files"],
                         ["a.py", "b.erl", "c.json", "d.md", "gone.json", "old.erl", "old.go"])

    def test_listed_files_limited(self):
        self.snap()
        for i in range(prompts.MAX_LISTED + 3):
            (self.project / f"f{i:03}.py").write_text("x = 1\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        self.stop("Поправил.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        text = rec.read_text(encoding="utf-8")
        self.assertIn("\n- … и ещё 3\n", text)
        files = self.env.log_lines()[-1]["files"]
        self.assertEqual(files, [f"f{i:03}.py" for i in range(prompts.MAX_LISTED)])

    def test_files_logged_only_with_docs_filter(self):
        r = self.stop(OPTIONS_MSG, PLANKA_STUB="ok")
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertNotIn("files", self.env.log_lines()[-1])

    def test_changed_this_turn_triples(self):
        (self.project / "old.go").write_text("x\n", encoding="utf-8")
        self.snap()
        (self.project / "old.go").unlink()
        (self.project / "a.json").write_text("{}\n", encoding="utf-8")
        data = hook_input("Stop", cwd=str(self.project))
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(self.env.data)}):
            _, changed = judge_stop.changed_this_turn(data)
        self.assertEqual(changed, [("a.json", "other", True), ("old.go", "code", False)])

    def test_no_snapshot_no_trigger(self):
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.stop("Поправил.")
        self.assertEqual(r.stdout, "")
        self.assertEqual(messages(r), [])
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_stale_snapshot_no_trigger(self):
        self.snap(prompt_id="p-0")
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        r = self.stop("Поправил.")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.env.log_lines(), [])

    def test_denied_blocks_with_reason(self):
        self.snap()
        (self.project / "a.py").write_text("# было так, стало эдак\n", encoding="utf-8")
        r = self.stop("Поправил.", PLANKA_STUB="deny", PLANKA_STUB_REASON="сверь pkg/CLAUDE.md")
        out = output(r)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("planka: "))
        self.assertIn("сверь pkg/CLAUDE.md", out["reason"])

    def test_three_filters_one_call(self):
        self.snap()
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        rec = self.env.data / "rec.txt"
        r = self.stop(OPTIONS_MSG + "\n\nГотово.", PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec))
        self.assertEqual(r.stdout, "", r.stderr)
        text = rec.read_text(encoding="utf-8")
        self.assertEqual(text.count("\n<content>\n"), 1)
        for needle in ["## Решения", "# Доказательство", "# Документация", "# Комментарии"]:
            self.assertIn(needle, text)
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["options", "done", "docs"])

    def test_missing_docs_module_skips(self):
        self.snap()
        (self.project / "a.py").write_text("x\n", encoding="utf-8")
        (self.env.root / "rules" / "comments.md").unlink()
        r = self.stop("Поправил.")
        self.assertIsNone(output(r))
        self.assertIn("planka:", "\n".join(messages(r)))
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "skipped")
        self.assertEqual(self.env.log_lines()[-1]["filters"], ["docs"])


if __name__ == "__main__":
    unittest.main()
