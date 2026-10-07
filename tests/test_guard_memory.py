import json
import os
import re
import sys
import unittest

from tests.helpers import PHILOSOPHY, PLANKA_DIR, Env, messages, output

sys.path.insert(0, str(PLANKA_DIR))
import guard_memory  # noqa: E402

MEM_PHILOSOPHY = PHILOSOPHY.replace("## Модули", "## Границы\n\n- Правило памяти ядра.\n\n## Модули")


class MemoryHookTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(philosophy=MEM_PHILOSOPHY)
        self.home = self.env.root.parent / "home"
        self.config = self.home / ".claude"
        self.memdir = self.config / "projects" / "-home-u-proj" / "memory"
        self.memdir.mkdir(parents=True)
        self.rec = self.env.data / "rec.txt"

    def tearDown(self):
        self.env.close()

    def run_tool(self, tool, tool_input, *, transcript=None, prompt_id=None, **extra):
        # Свой prompt_id на вызов: отказы в цикле не упираются в лимит.
        self.calls = getattr(self, "calls", 0) + 1
        env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": "", "PLANKA_STUB_RECORD": str(self.rec)}
        env.update(extra)
        fields = {"tool_name": tool, "tool_input": tool_input, "tool_use_id": "t1",
                  "prompt_id": prompt_id or f"p-{self.calls}"}
        if transcript is not None:
            fields["transcript_path"] = transcript
        return self.env.run("guard_memory.py", self.env.hook_input("PreToolUse", **fields), **env)

    def write_mem(self, path=None, content="Тесты: make test.", **extra):
        return self.run_tool("Write", {"file_path": str(path or self.memdir / "MEMORY.md"), "content": content},
                             **extra)

    def judged(self):
        return self.rec.read_text(encoding="utf-8") if self.rec.exists() else None

    def test_memory_write_ok(self):
        r = self.write_mem(PLANKA_STUB="ok")
        self.assertEqual(r.stdout, "", r.stderr)
        text = self.judged()
        self.assertIn("Правило памяти ядра", text)
        self.assertIn("Правило модуля памяти", text)
        self.assertNotIn("## Решения", text)
        self.assertIn("Тесты: make test.", text)
        self.assertIn(str(self.memdir / "MEMORY.md"), text)
        line = self.env.log_lines()[-1]
        self.assertEqual((line["hook"], line["verdict"], line["tool"]), ("memory", "ok", "Write"))
        self.assertIn("content_sha256", line)
        self.assertNotIn("Тесты", json.dumps(line, ensure_ascii=False))

    def test_memory_write_deny(self):
        r = self.write_mem(PLANKA_STUB="deny", PLANKA_STUB_REASON="автор не согласился")
        out = output(r)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertTrue(out["permissionDecisionReason"].startswith("planka: автор не согласился"))
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "deny")

    def test_non_memory_paths_pass_silently(self):
        for path in (self.env.project / "MEMORY.md", self.config / "projects" / "x" / "notes.md",
                     self.config / "settings.json", self.config / "rules"):
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", path)
        r = self.run_tool("Read", {"file_path": str(self.memdir / "MEMORY.md")}, PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")
        self.assertIsNone(self.judged())
        self.assertEqual(self.env.log_lines(), [])

    def test_user_claude_md_and_rules(self):
        for path in (self.config / "CLAUDE.md", self.config / "rules" / "style.md",
                     self.config / "rules" / "sub" / "x.md"):
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
            self.assertIsNotNone(self.judged())

    def test_symlinked_config_files(self):
        # Форма dotfiles: CLAUDE.md — ссылка на файл, rules/ и memory/ — ссылки на каталоги вне настроек.
        dot = self.env.root.parent / "dot"
        (dot / "rules").mkdir(parents=True)
        (dot / "mem").mkdir()
        (dot / "CLAUDE.md").write_text("", encoding="utf-8")
        (self.config / "CLAUDE.md").symlink_to(dot / "CLAUDE.md")
        (self.config / "rules").symlink_to(dot / "rules")
        linked = self.config / "projects" / "-home-u-other"
        linked.mkdir()
        (linked / "memory").symlink_to(dot / "mem")
        for path in (self.config / "CLAUDE.md", dot / "CLAUDE.md", self.config / "rules" / "a.md",
                     dot / "rules" / "a.md", linked / "memory" / "MEMORY.md", dot / "mem" / "MEMORY.md"):
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
            self.assertIsNotNone(self.judged(), path)
        self.rec.unlink(missing_ok=True)
        for path in (dot / "notes.md", dot / "rules"):
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", path)
        self.assertIsNone(self.judged())

    def test_write_path_link_outside_config(self):
        # Ссылка вне каталога настроек ведёт внутрь memory/ и rules/: путь записи сравнивается и разрешённым.
        (self.config / "rules").mkdir()
        for name, target in (("memlink", self.memdir), ("rulelink", self.config / "rules")):
            (self.env.project / name).symlink_to(target)
        for path in (self.env.project / "memlink" / "MEMORY.md", self.env.project / "rulelink" / "a.md"):
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
            self.assertIsNotNone(self.judged(), path)

    def test_config_dir_is_symlink(self):
        # CLAUDE_CONFIG_DIR — ссылка на настоящий каталог: судится запись и через ссылку, и прямо в каталог.
        real = self.env.root.parent / "realcfg"
        (real / "rules").mkdir(parents=True)
        (real / "projects" / "p" / "memory").mkdir(parents=True)
        link = self.env.root.parent / "linkcfg"
        link.symlink_to(real)
        for base in (link, real):
            for rel in ("CLAUDE.md", "rules/a.md", "projects/p/memory/MEMORY.md"):
                self.rec.unlink(missing_ok=True)
                r = self.write_mem(base / rel, PLANKA_STUB="deny", CLAUDE_CONFIG_DIR=str(link))
                self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", (base, rel))
        r = self.write_mem(real / "settings.json", PLANKA_STUB="deny", CLAUDE_CONFIG_DIR=str(link))
        self.assertEqual(r.stdout, "")

    def test_symlinked_project_dir(self):
        # projects/<проект> — ссылка: запись прямо в memory/ цели — память, остальное в цели — нет.
        elsewhere = self.env.root.parent / "elsewhere"
        (elsewhere / "memory").mkdir(parents=True)
        (self.config / "projects" / "-home-u-lnk").symlink_to(elsewhere)
        for path in (elsewhere / "memory" / "x.md", self.config / "projects" / "-home-u-lnk" / "memory" / "x.md"):
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
        self.rec.unlink(missing_ok=True)
        r = self.write_mem(elsewhere / "notes.md", PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")
        self.assertIsNone(self.judged())

    def test_symlinked_rule_file(self):
        # Ссылка на файл внутри rules/: путь записи через ссылку — память.
        dot = self.env.root.parent / "dot"
        dot.mkdir()
        (dot / "style.md").write_text("", encoding="utf-8")
        (self.config / "rules").mkdir()
        (self.config / "rules" / "style.md").symlink_to(dot / "style.md")
        r = self.write_mem(self.config / "rules" / "style.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_edit_tools(self):
        target = str(self.memdir / "topic.md")
        cases = [("Edit", {"file_path": target, "old_string": "a", "new_string": "новый факт"}),
                 ("MultiEdit", {"file_path": target, "edits": [{"old_string": "a", "new_string": "новый факт"}]}),
                 ("NotebookEdit", {"notebook_path": str(self.memdir / "n.ipynb"), "new_source": "новый факт"})]
        for tool, ti in cases:
            self.rec.unlink(missing_ok=True)
            r = self.run_tool(tool, ti, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", tool)
            self.assertIn("новый факт", self.judged(), tool)

    def test_tilde_and_relative_paths(self):
        r = self.write_mem("~/.claude/projects/p/memory/MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        rel = os.path.relpath(self.memdir / "MEMORY.md", self.env.project)
        r = self.write_mem(rel, PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        r = self.write_mem("memory/MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")

    def test_claude_config_dir(self):
        other = self.env.root.parent / "cfg"
        r = self.write_mem(other / "projects" / "p" / "memory" / "MEMORY.md", PLANKA_STUB="deny",
                           CLAUDE_CONFIG_DIR=str(other))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        r = self.write_mem(PLANKA_STUB="deny", CLAUDE_CONFIG_DIR=str(other))
        self.assertEqual(r.stdout, "")

    def test_auto_memory_directory_setting(self):
        custom = self.env.root.parent / "mymem"
        (self.config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(custom)}),
                                                   encoding="utf-8")
        r = self.write_mem(custom / "MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        (self.config / "settings.json").write_text("{broken", encoding="utf-8")
        r = self.write_mem(custom / "MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")
        r = self.write_mem(PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_mcp_memory_tools(self):
        for tool in ("mcp__memory__create_entities", "mcp__serena__write_memory",
                     "mcp__plugin_x_openmemory__addMemories"):
            self.rec.unlink(missing_ok=True)
            r = self.run_tool(tool, {"entities": [{"name": "факт MCP"}]}, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", tool)
            self.assertIn("факт MCP", self.judged())
            self.assertIn(tool, self.judged())
        for tool in ("mcp__memory__read_graph", "mcp__serena__list_memories", "mcp__github__create_issue",
                     "mcp__memory", "Write_memory"):
            r = self.run_tool(tool, {}, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", tool)

    def test_barrier(self):
        r = self.write_mem(PLANKA_STUB="deny", PLANKA_JUDGE="1")
        self.assertEqual(r.stdout, "")
        self.assertIsNone(self.judged())
        self.assertEqual(self.env.log_lines(), [])

    def test_budget(self):
        for _ in range(2):
            r = self.write_mem(PLANKA_STUB="deny", prompt_id="same")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.rec.unlink()
        r = self.write_mem(PLANKA_STUB="deny", prompt_id="same")
        self.assertIsNone(output(r))
        self.assertEqual(messages(r), ["planka: лимит отказов, пропущено без проверки"])
        self.assertIsNone(self.judged())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "budget")

    def test_judge_failure_passes(self):
        r = self.write_mem(PLANKA_STUB="garbage")
        self.assertIsNone(output(r))
        self.assertEqual(len(messages(r)), 1)
        self.assertTrue(messages(r)[0].startswith("planka: судья пропущен: ответ судьи не JSON"))
        line = self.env.log_lines()[-1]
        self.assertEqual((line["verdict"], line["error"]), ("skipped", "ответ судьи не JSON"))

    def test_missing_rubric_passes(self):
        (self.env.root / "rules" / "memory.md").unlink()
        r = self.write_mem(PLANKA_STUB="deny")
        self.assertIsNone(output(r))
        self.assertTrue(any("memory.md" in m for m in messages(r)))
        self.assertIsNone(self.judged())

    def test_judge_gets_author_turn_and_agent_messages(self):
        t = self.env.data / "t.jsonl"
        entries = [
            {"type": "user", "message": {"role": "user", "content": "старая реплика"}},
            {"type": "assistant", "message": {"model": "m", "content": [
                {"type": "text", "text": "Сохранить в память: тесты через make test?"}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "да, сохрани"}]}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "служебная вставка"}},
            {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "реплика субагента"}},
            {"type": "assistant", "message": {"model": "m", "content": [
                {"type": "text", "text": "Записываю факт."},
                {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "q1", "content": "ответ: подтверждаю"}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x9", "content": "вывод другого инструмента"}]}},
        ]
        t.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries) + "битая строка\n",
                     encoding="utf-8")
        self.write_mem(PLANKA_STUB="ok", transcript=str(t))
        text = self.judged()
        self.assertIn("да, сохрани", text)
        self.assertIn("Сохранить в память: тесты через make test?", text)
        self.assertIn("Записываю факт.", text)
        self.assertIn("ответ: подтверждаю", text)
        for absent in ("старая реплика", "служебная вставка", "реплика субагента", "вывод другого инструмента"):
            self.assertNotIn(absent, text)

    def test_long_write_is_clipped(self):
        self.write_mem(content="я" * (guard_memory.MAX_FIELD + 500), PLANKA_STUB="ok")
        text = self.judged()
        self.assertIn("… обрезано", text)
        self.assertNotIn("я" * (guard_memory.MAX_FIELD + 1), text)


class MemoryMcpNameTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(guard_memory._words("addMemories_now"), ["add", "memories", "now"])
        self.assertFalse(guard_memory.is_memory_mcp("mcp__memory__address_lookup"))

    def test_memory_word_is_whole_word(self):
        for tool in ("mcp__memory__create_entities", "mcp__serena__write_memory", "mcp__x__save_MEMORY",
                     "mcp__plugin_x_openmemory__addMemories", "mcp__memory-bank__write_file",
                     "mcp__x__memorise_fact", "mcp__x__remember", "mcp__a__b__memories_add"):
            self.assertTrue(guard_memory.is_memory_mcp(tool), tool)
        for tool in ("mcp__redis_memorystore__set_key", "mcp__x__memorandum_create", "mcp__openmemory__add",
                     "mcp__memory__read_graph", "mcp__memory", "Write_memory"):
            self.assertFalse(guard_memory.is_memory_mcp(tool), tool)

    # Корпус имён MCP-инструментов: имя → судится ли как запись в память. Серверы memory (официальный граф
    # знаний), serena, mem0, openmemory, basic-memory; ложные — настройки и метрики памяти машины.
    CORPUS = {
        "mcp__memory__create_entities": True, "mcp__memory__create_relations": True,
        "mcp__memory__add_observations": True, "mcp__memory__delete_entities": True,
        "mcp__memory__delete_observations": True, "mcp__memory__delete_relations": True,
        "mcp__memory__read_graph": False, "mcp__memory__search_nodes": False, "mcp__memory__open_nodes": False,
        "mcp__serena__write_memory": True, "mcp__serena__edit_memory": True, "mcp__serena__delete_memory": True,
        "mcp__serena__rename_memory": True, "mcp__serena__read_memory": False, "mcp__serena__list_memories": False,
        "mcp__mem0__add_memory": True, "mcp__mem0__add_memories": True, "mcp__mem0__update_memory": True,
        "mcp__mem0__delete_memory": True, "mcp__mem0__delete_all_memories": True,
        "mcp__mem0__search_memories": False, "mcp__mem0__get_all_memories": False,
        "mcp__openmemory__add_memories": True, "mcp__openmemory__delete_all_memories": True,
        "mcp__openmemory__list_memories": False, "mcp__openmemory__search_memory": False,
        "mcp__basic-memory__write_note": True, "mcp__basic-memory__delete_note": True,
        "mcp__basic-memory__move_note": True,
        "mcp__basic-memory__read_note": False,
        "mcp__x__save_to_memory": True, "mcp__x__create_memory_entry": True, "mcp__x__memory_add": True,
        "mcp__x__memory_store": True, "mcp__x__storeMemory": True, "mcp__x__rememberFact": True,
        "mcp__x__memory_usage_set_alert": False, "mcp__x__memory_stats_get": False,
        "mcp__x__get_memory_usage": False, "mcp__x__memory_limit_update": False,
        "mcp__redis_memorystore__set_key": False, "mcp__gcp__memorystore_create_instance": False,
        # Глагол перед словом памяти, которое определяет другое существительное: имя судится как запись.
        "mcp__x__set_memory_limit": True, "mcp__x__clear_memory_cache": True,
    }

    def test_corpus(self):
        for tool, expected in self.CORPUS.items():
            with self.subTest(tool):
                self.assertEqual(guard_memory.is_memory_mcp(tool), expected)

    def test_hook_matcher_passes_every_memory_tool(self):
        # Matcher хука — регулярное выражение JS без флагов, ищется в имени инструмента; такой шаблон
        # Python re понимает так же.
        hooks = json.loads((PLANKA_DIR.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        matcher = next(h["matcher"] for h in hooks["hooks"]["PreToolUse"]
                       if any("guard_memory.py" in c["command"] for c in h["hooks"]))
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "mcp__memory__create_entities",
                     "mcp__x__save_MEMORY", "mcp__x__Memorise_fact", "mcp__x__remember", "mcp__x__REMEMBER_this"):
            self.assertRegex(tool, matcher)
        for tool in ("WriteFile", "Bash", "mcp__github__create_issue"):
            self.assertIsNone(re.search(matcher, tool), tool)


if __name__ == "__main__":
    unittest.main()
