import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import PHILOSOPHY, PLANKA_DIR, STUB_DIR, Env, assert_linear, messages, output

sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
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

    def run_tool(self, tool, tool_input, *, transcript=None, prompt_id=None, cwd=None, proc_cwd=None, **extra):
        # Свой prompt_id на вызов: отказы в цикле не упираются в лимит. proc_cwd — текущий каталог процесса
        # хука; без него процесс наследует каталог процесса тестов.
        self.calls = getattr(self, "calls", 0) + 1
        env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": "", "PLANKA_STUB_RECORD": str(self.rec)}
        env.update(extra)
        fields = {"tool_name": tool, "tool_input": tool_input, "tool_use_id": "t1",
                  "prompt_id": prompt_id or f"p-{self.calls}"}
        if transcript is not None:
            fields["transcript_path"] = transcript
        if cwd is not None:
            fields["cwd"] = cwd
        hook_input = self.env.hook_input("PreToolUse", **fields)
        if proc_cwd is None:
            return self.env.run("guard_memory.py", hook_input, **env)
        return subprocess.run([sys.executable, str(PLANKA_DIR / "guard_memory.py")], input=json.dumps(hook_input),
                              capture_output=True, text=True, encoding="utf-8", env=self.env.environ(**env),
                              cwd=proc_cwd, timeout=30)

    def write_mem(self, path=None, content="Тесты: make test.", *, cwd=None, **extra):
        return self.run_tool("Write", {"file_path": str(path or self.memdir / "MEMORY.md"), "content": content},
                             cwd=cwd, **extra)

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
        self.assertTrue(out["permissionDecisionReason"].startswith("planka: Мой дорогой друг, автор не согласился"))
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
                 ("MultiEdit", {"file_path": target, "edits": [{"old_string": "b", "new_string": "первый"},
                                                          {"old_string": "a", "new_string": "новый факт"}]}),
                 ("NotebookEdit", {"notebook_path": str(self.memdir / "n.ipynb"), "new_source": "новый факт"})]
        for tool, ti in cases:
            self.rec.unlink(missing_ok=True)
            r = self.run_tool(tool, ti, PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", tool)
            self.assertIn("новый факт", self.judged(), tool)
            if tool == "MultiEdit":
                # Судья видит новые строки всех правок по порядку.
                self.assertIn("первый\n---\nновый факт", self.judged())

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

    def test_agent_memory(self):
        # Память субагента: memory: user — <настройки>/agent-memory/, memory: local —
        # <проект>/.claude/agent-memory-local/ (Claude Code исключает её из git); memory: project —
        # <проект>/.claude/agent-memory/, отслеживаемый файл репозитория, не судится.
        proj = self.env.root.parent / "launch"
        for path, extra in ((self.config / "agent-memory" / "reviewer" / "MEMORY.md", {}),
                            (self.env.project / ".claude" / "agent-memory-local" / "reviewer" / "MEMORY.md", {}),
                            (proj / ".claude" / "agent-memory-local" / "reviewer" / "MEMORY.md",
                             {"CLAUDE_PROJECT_DIR": str(proj)})):
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(path, PLANKA_STUB="deny", **extra)
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
            self.assertIsNotNone(self.judged(), path)
        self.rec.unlink(missing_ok=True)
        for path in (self.env.project / ".claude" / "agent-memory" / "reviewer" / "MEMORY.md",
                     self.config / "agent-memory", self.config / "agent-memory-x" / "a.md"):
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", path)
        self.assertIsNone(self.judged())

    def test_remote_memory_dir(self):
        # Облачная сессия: CLAUDE_CODE_REMOTE_MEMORY_DIR переносит agent-memory/ пользователя, автопамять и
        # agent-memory-local/ проектов в свой каталог; без переменной эти места не память.
        remote = self.env.root.parent / "remote"
        paths = (remote / "agent-memory" / "r" / "M.md",
                 remote / "projects" / "p" / "agent-memory-local" / "r" / "M.md",
                 remote / "projects" / "p" / "memory" / "MEMORY.md")
        for path in paths:
            r = self.write_mem(path, PLANKA_STUB="deny", CLAUDE_CODE_REMOTE_MEMORY_DIR=str(remote))
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", path)
        r = self.write_mem(remote / "projects" / "p" / "other" / "x.md", PLANKA_STUB="deny",
                           CLAUDE_CODE_REMOTE_MEMORY_DIR=str(remote))
        self.assertEqual(r.stdout, "")

    def test_remote_project_dir_symlink(self):
        # projects/<проект> под CLAUDE_CODE_REMOTE_MEMORY_DIR — символьная ссылка: memory/ и
        # agent-memory-local/ судятся и при записи через ссылку, и при записи прямо в её цель.
        remote = self.env.root.parent / "remote"
        target = self.env.root.parent / "stored"
        (remote / "projects").mkdir(parents=True)
        target.mkdir()
        (remote / "projects" / "p").symlink_to(target)
        for name in ("memory", "agent-memory-local"):
            for base in (remote / "projects" / "p", target):
                path = base / name / "r" / "M.md"
                r = self.write_mem(path, PLANKA_STUB="deny", CLAUDE_CODE_REMOTE_MEMORY_DIR=str(remote))
                self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
        r = self.write_mem(target / "other" / "x.md", PLANKA_STUB="deny", CLAUDE_CODE_REMOTE_MEMORY_DIR=str(remote))
        self.assertEqual(r.stdout, "")

    def test_cowork_memory_path_override(self):
        # CLAUDE_COWORK_MEMORY_PATH_OVERRIDE целиком заменяет каталог автопамяти; относительный путь Claude Code
        # отвергает.
        custom = self.env.root.parent / "cowork"
        r = self.write_mem(custom / "MEMORY.md", PLANKA_STUB="deny", CLAUDE_COWORK_MEMORY_PATH_OVERRIDE=str(custom))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        r = self.write_mem(custom / "MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")
        # Относительное значение не разрешается и от текущего каталога процесса хука.
        r = self.write_mem(self.env.project / "cowork" / "MEMORY.md", PLANKA_STUB="deny",
                           CLAUDE_COWORK_MEMORY_PATH_OVERRIDE="cowork", cwd=str(self.env.project),
                           proc_cwd=str(self.env.project))
        self.assertEqual(r.stdout, "")

    def test_auto_memory_directory_project_settings(self):
        # autoMemoryDirectory из .claude/settings.json и .claude/settings.local.json проекта: проект —
        # CLAUDE_PROJECT_DIR, без него cwd входа; относительное значение Claude Code отвергает.
        launch = self.env.root.parent / "launch"
        for base, name, extra in ((self.env.project, "settings.local.json", {}),
                                  (self.env.project, "settings.json", {}),
                                  (launch, "settings.local.json", {"CLAUDE_PROJECT_DIR": str(launch)})):
            custom = self.env.root.parent / f"mem-{base.name}-{name}"
            (base / ".claude").mkdir(parents=True, exist_ok=True)
            (base / ".claude" / name).write_text(json.dumps({"autoMemoryDirectory": str(custom)}), encoding="utf-8")
            r = self.write_mem(custom / "MEMORY.md", PLANKA_STUB="deny", **extra)
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", (base, name))
            (base / ".claude" / name).unlink()
        (self.env.project / ".claude" / "settings.json").write_text(
            json.dumps({"autoMemoryDirectory": "relmem"}), encoding="utf-8")
        r = self.write_mem(self.env.project / "relmem" / "MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_in_repository(self):
        # autoMemoryDirectory, равный проекту: файл, не исключённый git проекта, — файл репозитория, не память
        # (память — вне репозитория); значение, равное каталогу проекта, не делает памятью весь проект.
        # Исключённый git файл в таком каталоге — память. Каталог памяти строго внутри проекта — память всегда,
        # git не спрашивается: автор назначил его местом памяти.
        subprocess.run(["git", "init", "-q", str(self.env.project)], check=True)
        (self.env.project / ".gitignore").write_text("mem/\n", encoding="utf-8")
        settings = self.env.project / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"autoMemoryDirectory": str(self.env.project)}), encoding="utf-8")
        for path in (self.env.project / "src" / "notes.txt", self.env.project / "MEMORY.md"):
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", path)
        self.assertIsNone(self.judged())
        r = self.write_mem(self.env.project / "mem" / "MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        for value in (self.env.project / "notes", self.env.project / "mem"):
            self.rec.unlink(missing_ok=True)
            settings.write_text(json.dumps({"autoMemoryDirectory": str(value)}), encoding="utf-8")
            r = self.write_mem(value / "MEMORY.md", PLANKA_STUB="deny")
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", value)
            self.assertIsNotNone(self.judged(), value)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_above_project(self):
        # autoMemoryDirectory — родитель проекта, он же корень репозитория: файл проекта, не исключённый git, —
        # файл репозитория, не память; файл того же репозитория и каталога памяти вне проекта — память.
        parent = self.env.project.parent
        subprocess.run(["git", "init", "-q", str(parent)], check=True)
        (self.config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(parent)}),
                                                   encoding="utf-8")
        r = self.write_mem(self.env.project / "src" / "notes.md", PLANKA_STUB="deny")
        self.assertEqual(r.stdout, "")
        self.assertIsNone(self.judged())
        r = self.write_mem(parent / "other" / "fact.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_in_home_project(self):
        # Claude Code запущен из самого домашнего каталога, он — git-репозиторий (dotfiles), autoMemoryDirectory
        # — ~/notes/mem, git его не исключает: каталог памяти строго внутри проекта — память.
        subprocess.run(["git", "init", "-q", str(self.home)], check=True)
        (self.config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/notes/mem"}),
                                                   encoding="utf-8")
        r = self.write_mem(self.home / "notes" / "mem" / "fact.md", PLANKA_STUB="deny",
                           CLAUDE_PROJECT_DIR=str(self.home), cwd=str(self.home))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNotNone(self.judged())

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_nested_repository(self):
        # autoMemoryDirectory равен проекту, в проекте — отдельный репозиторий .mem (память под git для
        # синхронизации): его файлы не файлы рабочего дерева проекта, запись — память, исключил ли проект
        # каталог или нет; git проекта о неисключённом отвечает 1.
        subprocess.run(["git", "init", "-q", str(self.env.project)], check=True)
        mem = self.env.project / ".mem"
        subprocess.run(["git", "init", "-q", str(mem)], check=True)
        settings = self.env.project / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"autoMemoryDirectory": str(self.env.project)}), encoding="utf-8")
        for ignore in (".mem/\n", ""):
            (self.env.project / ".gitignore").write_text(ignore, encoding="utf-8")
            for path in (mem / "fact.md", mem / "sub" / "fact.md"):
                self.rec.unlink(missing_ok=True)
                r = self.write_mem(path, PLANKA_STUB="deny")
                self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", (ignore, path))
                self.assertIsNotNone(self.judged(), (ignore, path))

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_nested_worktree(self):
        # Вложенный репозиторий с .git-файлом (git worktree add внутри проекта): его файлы тоже не рабочее
        # дерево проекта, запись — память.
        project = self.env.project
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                        "-q", "--allow-empty", "-m", "init"], check=True)
        wt = project / "wt"
        subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", "-b", "side", str(wt)], check=True)
        self.assertTrue((wt / ".git").is_file())
        settings = project / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"autoMemoryDirectory": str(project)}), encoding="utf-8")
        r = self.write_mem(wt / "fact.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNotNone(self.judged())

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_several_memory_directories_all_must_contain_project(self):
        # autoMemoryDirectory — проект, cowork — mem/ внутри него: запись в mem/ попадает в оба каталога, и
        # проект содержит лишь первый; mem/ — каталог памяти строго внутри проекта, запись — память, хотя
        # другой совпавший каталог содержит проект.
        project = self.env.project
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        settings = project / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"autoMemoryDirectory": str(project)}), encoding="utf-8")
        r = self.write_mem(project / "mem" / "x.md", PLANKA_STUB="deny",
                           CLAUDE_COWORK_MEMORY_PATH_OVERRIDE=str(project / "mem"))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNotNone(self.judged())

    def test_auto_memory_directory_in_project_without_git(self):
        # autoMemoryDirectory равен проекту, проект не репозиторий: git отвечает 128, запись — память.
        settings = self.env.project / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"autoMemoryDirectory": str(self.env.project)}), encoding="utf-8")
        r = self.write_mem(self.env.project / "notes" / "MEMORY.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNotNone(self.judged())

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_in_foreign_repository(self):
        # Домашний каталог — git-репозиторий (dotfiles), autoMemoryDirectory — ~/notes/mem вне проекта:
        # запись туда — память, хотя git её не исключает. Исключение «файл репозитория» — только для
        # каталога памяти внутри проекта.
        subprocess.run(["git", "init", "-q", str(self.home)], check=True)
        (self.config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/notes/mem"}),
                                                   encoding="utf-8")
        r = self.write_mem(self.home / "notes" / "mem" / "fact.md", PLANKA_STUB="deny")
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNotNone(self.judged())

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_project_under_dotfiles_home(self):
        # Домашний каталог — репозиторий dotfiles, проект ~/Projects/app без своего git — каталог запуска.
        # Репозиторий дома — не репозиторий проекта (common.project_root): файл проекта в каталоге памяти,
        # равном проекту или содержащем его, — память, хотя git дома его не исключает. Каталога проекта нет —
        # так же.
        commit(self.home, ".bashrc")
        app = self.home / "Projects" / "app"
        settings = app / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        for value in (str(app), "~"):
            settings.write_text(json.dumps({"autoMemoryDirectory": value}), encoding="utf-8")
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(app / "notes.md", PLANKA_STUB="deny", CLAUDE_PROJECT_DIR=str(app), cwd=str(app))
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", value)
            self.assertIsNotNone(self.judged(), value)
        gone = self.home / "Projects" / "gone"
        (self.config / "settings.json").write_text(
            json.dumps({"autoMemoryDirectory": str(self.home / "Projects")}), encoding="utf-8")
        self.rec.unlink(missing_ok=True)
        r = self.write_mem(gone / "notes.md", PLANKA_STUB="deny", CLAUDE_PROJECT_DIR=str(gone), cwd=str(app))
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_auto_memory_directory_monorepo_subproject(self):
        # Каталог запуска — новый или удалённый подкаталог монорепозитория, autoMemoryDirectory — монорепозиторий:
        # файл подкаталога, не исключённый git, — файл репозитория, не память.
        mono = self.env.project
        commit(mono, "package.json", "packages/old/package.json")
        (self.config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(mono)}),
                                                   encoding="utf-8")
        (mono / "packages" / "new").mkdir()
        for sub in (mono / "packages" / "new", mono / "packages" / "gone"):
            r = self.write_mem(sub / "notes.md", PLANKA_STUB="deny", CLAUDE_PROJECT_DIR=str(sub), cwd=str(mono))
            self.assertEqual(r.stdout, "", sub)
        self.assertIsNone(self.judged())

    def test_non_utf8_locale_cyrillic_paths(self):
        # Локаль C с PYTHONUTF8=0: кодировка файловой системы ascii, пути входа — байты UTF-8 (как в
        # make test-hostile). Кириллица в cwd и в пути цели не роняет хук.
        env = self.env.environ(LC_ALL="C", PYTHONUTF8="0")
        fsenc = subprocess.run([sys.executable, "-c", "import sys; print(sys.getfilesystemencoding())"],
                               env=env, capture_output=True, text=True).stdout.strip()
        if fsenc != "ascii":
            self.skipTest(f"кодировка файловой системы {fsenc}: локаль C здесь не ascii")
        cwd = self.env.project / "проект"
        cwd.mkdir()
        for path in (self.env.root.parent / "чужое" / "x.py", "заметки/a.md"):
            r = self.write_mem(path, PLANKA_STUB="deny", cwd=str(cwd), LC_ALL="C")
            self.assertEqual((r.stdout, r.stderr), ("", ""), path)
        memory = self.config / "projects" / "-проект" / "memory" / "MEMORY.md"
        r = self.write_mem(memory, PLANKA_STUB="deny", cwd=str(cwd), LC_ALL="C")
        self.assertEqual(messages(r), [])
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(str(memory), self.judged())
        # Кириллица в autoMemoryDirectory (settings.json — текст UTF-8) и в путях окружения: запись в каталог
        # памяти — отказ без сообщений, запись в файл проекта — пустой ответ.
        notes = self.env.root.parent / "заметки"
        (self.config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(notes)},
                                                              ensure_ascii=False), encoding="utf-8")
        places = ((notes / "MEMORY.md", {}),
                  (self.env.root.parent / "кворк" / "MEMORY.md",
                   {"CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": str(self.env.root.parent / "кворк")}),
                  (self.env.root.parent / "удалённо" / "agent-memory" / "r" / "M.md",
                   {"CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.env.root.parent / "удалённо")}),
                  (self.env.root.parent / "настройки" / "CLAUDE.md",
                   {"CLAUDE_CONFIG_DIR": str(self.env.root.parent / "настройки")}))
        for path, extra in places:
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(path, PLANKA_STUB="deny", LC_ALL="C", **extra)
            self.assertEqual(messages(r), [], path)
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
            self.assertIn(str(path), self.judged())
        r = self.write_mem(self.env.project / "src" / "a.py", PLANKA_STUB="deny", LC_ALL="C")
        self.assertEqual((r.stdout, r.stderr), ("", ""))

    def test_unusable_auto_memory_directory_skipped(self):
        # Значение autoMemoryDirectory, которое не путь файловой системы (NUL, одиночный суррогат), и файл
        # настроек не с объектом JSON пропускаются: остальные места памяти судятся, хук не падает.
        for settings in ({"autoMemoryDirectory": "/tmp/a\0b"}, {"autoMemoryDirectory": "/tmp/\ud800x"}, []):
            (self.config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            r = self.write_mem(PLANKA_STUB="deny")
            self.assertEqual(messages(r), [], settings)
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", settings)
            r = self.write_mem(self.env.project / "src" / "a.py", PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", settings)

    def test_nul_in_path_is_not_memory(self):
        # Путь с NUL файловый инструмент не откроет: записи нет, судить нечего.
        for path, cwd in ((str(self.memdir / "MEM\0ORY.md"), None), ("MEMORY.md", f"{self.memdir}\0x")):
            r = self.write_mem(path, PLANKA_STUB="deny", cwd=cwd)
            self.assertEqual(r.stdout, "", (path, cwd))
        self.assertIsNone(self.judged())

    def test_relative_auto_memory_directory_ignored(self):
        # Ни от проекта, ни от текущего каталога процесса хука (его наследует подпроцесс теста).
        (self.config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "relmem"}), encoding="utf-8")
        for path in (self.env.project / "relmem" / "MEMORY.md", Path.cwd() / "relmem" / "MEMORY.md"):
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", path)

    def test_rules_non_md_and_nested_memory_not_judged(self):
        # rules/ — только *.md; memory/ — только прямо в projects/<проект>/.
        (self.config / "rules").mkdir()
        for path in (self.config / "rules" / "x.txt", self.config / "projects" / "p" / "memory",
                     self.config / "projects" / "p" / "sub" / "memory" / "a.md",
                     self.config / "projects" / "memory" / "a.md"):
            r = self.write_mem(path, PLANKA_STUB="deny")
            self.assertEqual(r.stdout, "", path)
        self.assertIsNone(self.judged())

    def assert_judged(self, paths, **extra):
        for path in paths:
            self.rec.unlink(missing_ok=True)
            r = self.write_mem(path, PLANKA_STUB="deny", **extra)
            self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny", path)
            self.assertIsNotNone(self.judged(), path)

    def assert_not_judged(self, paths, **extra):
        self.rec.unlink(missing_ok=True)
        for path in paths:
            r = self.write_mem(path, PLANKA_STUB="deny", **extra)
            self.assertEqual(r.stdout, "", path)
        self.assertIsNone(self.judged())

    def test_dotdot_after_link_resolved_lexically(self):
        # Claude Code схлопывает «..» в пути файлового инструмента лексически (path.resolve) до записи и до хука:
        # sub/rl/../CLAUDE.md пишется в sub/CLAUDE.md проекта, а не в каталог настроек над целью ссылки rl.
        (self.config / "rules").mkdir()
        (self.env.project / "sub").mkdir()
        (self.env.project / "sub" / "rl").symlink_to(self.config / "rules")
        self.assert_not_judged([f"{self.env.project}/sub/rl/../CLAUDE.md"])
        self.assert_judged([f"{self.env.project}/sub/rl/../rl/a.md"])

    def test_user_memory_imports(self):
        # Файл, который пользовательская память подключает импортом @путь, — память: путь от файла с импортом,
        # ~/ — от домашнего каталога, абсолютный; импорты в коде, комментарии HTML и почтовые адреса не
        # импорты. Глубина — четыре перехода от корня.
        shared = self.home / "shared"
        absolute = self.env.root.parent / "abs"
        (self.config / "notes").mkdir()
        (self.config / "rules").mkdir()
        (self.config / "CLAUDE.md").write_text(
            "См. @notes/style.md и @~/shared/team.md,\n"
            f"абсолютный @{absolute}/x.md, с пробелом @Design\\ Docs/api.md#раздел.\n"
            "- *@emph.md*\n"
            "`@span.md` и ``@span2.md``\n"
            "```\n@fenced.md\n```\n"
            "<!-- @comment.md -->\n"
            "почта a@mail.md\n", encoding="utf-8")
        chain = [self.config / "notes" / "style.md"] + [self.config / "notes" / f"d{i}.md" for i in range(2, 6)]
        for cur, nxt in zip(chain, chain[1:]):
            cur.write_text(f"@{nxt.name}\n", encoding="utf-8")
        (self.config / "rules" / "r.md").write_text("@../from-rule.md\n", encoding="utf-8")
        self.assert_judged(chain[:4] + [shared / "team.md", absolute / "x.md", self.config / "Design Docs" / "api.md",
                                        self.config / "emph.md", self.config / "from-rule.md"])
        self.assert_not_judged([chain[4], self.config / "span.md", self.config / "span2.md",
                                self.config / "fenced.md", self.config / "comment.md", self.config / "mail.md",
                                self.config / "notes.md"])

    def test_claude_local_md(self):
        # CLAUDE.local.md — личные инструкции проекта, Claude Code читает его из проекта, каталогов над ним и
        # подкаталогов: память, если git проекта его исключает или git нет; файл репозитория — не память.
        # Его импорты — так же; импорты проектного CLAUDE.md — документация.
        proj = self.env.project
        (proj / "CLAUDE.local.md").write_text("@priv/notes.md\n", encoding="utf-8")
        (proj / "CLAUDE.md").write_text("@docs/x.md\n", encoding="utf-8")
        self.assert_judged([proj / "CLAUDE.local.md", proj.parent / "CLAUDE.local.md",
                            proj / "sub" / "CLAUDE.local.md", proj / "priv" / "notes.md"])
        self.assert_not_judged([proj / "docs" / "x.md", proj / "CLAUDE.md",
                                self.env.root.parent / "elsewhere" / "CLAUDE.local.md"])

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_claude_local_md_in_repository(self):
        proj = self.env.project
        subprocess.run(["git", "init", "-q", str(proj)], check=True)
        (proj / ".gitignore").write_text("CLAUDE.local.md\npriv/\n", encoding="utf-8")
        (proj / "CLAUDE.local.md").write_text("@priv/notes.md\n@docs/y.md\n", encoding="utf-8")
        self.assert_judged([proj / "CLAUDE.local.md", proj / "priv" / "notes.md"])
        (proj / ".gitignore").write_text("", encoding="utf-8")
        self.assert_not_judged([proj / "CLAUDE.local.md", proj / "docs" / "y.md"])

    def test_case_insensitive_file_system(self):
        # На файловой системе без учёта регистра ~/.claude/claude.md — тот же CLAUDE.md. Linux различает регистр:
        # такую систему подменяет ссылка .CLAUDE на .claude — каталог находится и по имени с другим регистром.
        (self.config / "rules").mkdir()
        paths = [self.config / "claude.md", self.config / "Rules" / "a.md",
                 self.config / "projects" / "-home-u-proj" / "Memory" / "M.md"]
        self.assert_not_judged(paths)
        (self.home / ".CLAUDE").symlink_to(self.config)
        self.assert_judged(paths)

    def test_deny_survives_log_failure(self):
        (self.env.data / "judge.log").mkdir()
        r = self.write_mem(PLANKA_STUB="deny")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(output(r)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(any("внутренняя ошибка" in m for m in messages(r)))

    def test_budget_exhausted_while_judging(self):
        # Параллельный вызов дошёл до предела, пока судья думал: отказ не выдаётся, в журнале — budget.
        wrap = self.env.data / "wrap"
        wrap.mkdir()
        state = self.env.data / "state" / "sess-1.json"
        script = wrap / "claude"
        script.write_text("#!/bin/sh\n"
                          f"printf '%s' '{json.dumps({'race:memory': common.MAX_DENIES})}' > '{state}'\n"
                          f'exec \'{STUB_DIR / "claude"}\' "$@"\n', encoding="utf-8")
        script.chmod(0o755)
        r = self.write_mem(PLANKA_STUB="deny", prompt_id="race", PATH=f"{wrap}:{self.env.environ()['PATH']}")
        self.assertIsNone(output(r), r.stdout)
        self.assertEqual(messages(r), ["planka: лимит отказов, пропущено без проверки"])
        self.assertIsNotNone(self.judged())
        self.assertEqual(self.env.log_lines()[-1]["verdict"], "budget")

    def test_notebook_delete_label(self):
        r = self.run_tool("NotebookEdit", {"notebook_path": str(self.memdir / "n.ipynb"), "edit_mode": "delete",
                                           "cell_id": "c1"}, PLANKA_STUB="ok")
        self.assertEqual(r.stdout, "", r.stderr)
        self.assertIn("(удаление ячейки)", self.judged())

    def test_judge_model_from_transcript(self):
        self.write_mem(PLANKA_STUB="ok")
        self.assertIn("ARGV\n", self.judged())
        args = self.judged().split("STDIN\n", 1)[0].splitlines()
        self.assertIn("claude-test-model", args[args.index("--model") + 1:args.index("--model") + 2])

    def test_agent_messages_clipped_keep_tail(self):
        t = self.env.data / "t.jsonl"
        long = "НАЧАЛО" + "я" * guard_memory.MAX_FIELD + "ХВОСТ"
        entries = [{"type": "user", "message": {"role": "user", "content": "да"}},
                   {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": long}]}}]
        t.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
        self.write_mem(PLANKA_STUB="ok", transcript=str(t))
        text = self.judged()
        self.assertIn("ХВОСТ", text)
        self.assertNotIn("НАЧАЛО", text)

    def test_message_before_author_clipped_keep_tail(self):
        # Длинное сообщение агента перед репликой автора: судье — его конец, ближайший к реплике.
        t = self.env.data / "t.jsonl"
        long = "НАЧАЛО" + "я" * guard_memory.MAX_FIELD + "ХВОСТ"
        entries = [{"type": "user", "message": {"role": "user", "content": "запомни команду тестов"}},
                   {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": long}]}},
                   {"type": "user", "message": {"role": "user", "content": "да"}},
                   {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "Пишу."}]}}]
        t.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
        self.write_mem(PLANKA_STUB="ok", transcript=str(t))
        text = self.judged()
        self.assertIn("ХВОСТ", text)
        self.assertNotIn("НАЧАЛО", text)
        self.assertIn("Пишу.", text)

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
        self.assertIn("Прежние реплики автора, от старых к новым:\nстарая реплика\n", text)
        for absent in ("служебная вставка", "реплика субагента", "вывод другого инструмента"):
            self.assertNotIn(absent, text)

    def test_long_write_is_clipped(self):
        self.write_mem(content="я" * (guard_memory.MAX_FIELD + 500), PLANKA_STUB="ok")
        text = self.judged()
        self.assertIn("… обрезано", text)
        self.assertNotIn("я" * (guard_memory.MAX_FIELD + 1), text)


class ManagedSettingsTest(unittest.TestCase):
    """autoMemoryDirectory из managed-settings.json и managed-settings.d/*.json: путь каталога managed
    подменяется в процессе — системный каталог тест не пишет."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.managed = self.root / "managed"
        (self.managed / "managed-settings.d").mkdir(parents=True)
        self.project = self.root / "project"
        self.project.mkdir()
        patches = [mock.patch.object(guard_memory, "managed_dir", return_value=str(self.managed)),
                   mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "cfg")})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_settings_fifo_not_opened(self):
        # Открытие FIFO повесило бы хук: читает поток, чтобы тест падал, а не висел.
        (self.project / ".claude").mkdir()
        os.mkfifo(self.project / ".claude" / "settings.local.json")
        result = []
        reader = threading.Thread(target=lambda: result.append(
            guard_memory._auto_memory_overrides(str(self.root / "cfg"), str(self.project))), daemon=True)
        reader.start()
        reader.join(2)
        hung = reader.is_alive()
        if hung:
            # Отпустить читателя, чтобы поток не остался висеть.
            os.close(os.open(self.project / ".claude" / "settings.local.json", os.O_WRONLY))
            reader.join(2)
        self.assertFalse(hung, "_auto_memory_overrides повис на FIFO")
        self.assertEqual(result, [[]])

    def test_managed_file_and_drop_in(self):
        for name in ("managed-settings.json", "managed-settings.d/10-mem.json"):
            custom = self.root / f"mem-{name.replace('/', '-')}"
            (self.managed / name).write_text(json.dumps({"autoMemoryDirectory": str(custom)}), encoding="utf-8")
            self.assertTrue(guard_memory.is_memory_path(str(custom / "MEMORY.md"), str(self.project)), name)
            (self.managed / name).unlink()
            self.assertFalse(guard_memory.is_memory_path(str(custom / "MEMORY.md"), str(self.project)), name)


class RepositoryFileTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_check_ignore_gets_named_timeout(self):
        # Срок вызова — CHECK_IGNORE_TIMEOUT: его сумму с судьёй проверяет TimeoutsTest в test_contract.
        # Каталога проекта нет — git спрашивается из ближайшего существующего предка под корнем репозитория.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(os.path.realpath(tmp)) / "repo"
            commit(repo, "a.txt")
            with mock.patch.object(guard_memory.subprocess, "run", wraps=subprocess.run) as run:
                self.assertTrue(guard_memory._repository_file(str(repo / "gone" / "x.md"), str(repo / "gone")))
        self.assertEqual(run.call_args.kwargs["timeout"], guard_memory.CHECK_IGNORE_TIMEOUT)
        self.assertEqual(run.call_args.args[0][:4], ["git", "-C", str(repo), "check-ignore"])
        self.assertEqual(run.call_args.kwargs["env"]["GIT_CEILING_DIRECTORIES"].split(os.pathsep)[0],
                         str(repo.parent))

    def test_missing_root_is_not_repository_file(self):
        with mock.patch.object(guard_memory.subprocess, "run", wraps=subprocess.run) as run:
            self.assertFalse(guard_memory._repository_file("/nonexistent/x.md", "/nonexistent"))
        self.assertNotIn("check-ignore", [a for c in run.call_args_list for a in c.args[0]])


def commit(repo, *files):
    """git init repo (если нет) и коммит файлов files с содержимым «x»."""
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for rel in files:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-f", "--", *files], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"],
                   check=True)


@unittest.skipUnless(shutil.which("git"), "нет git")
class OverrideSymlinkTest(unittest.TestCase):
    """Каталог памяти и проект, заданные символьной ссылкой и её целью, — один и тот же каталог."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.proj = self.root / "proj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        (self.proj / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.proj), "add", "a.txt"], check=True)
        self.link = self.root / "plink"
        self.link.symlink_to(self.proj)
        (self.root / "cfg").mkdir()

    def is_memory(self, path, project, override):
        env = {"CLAUDE_CONFIG_DIR": str(self.root / "cfg"), "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": str(override)}
        with mock.patch.dict(os.environ, env):
            return guard_memory.is_memory_path(str(path), str(project))

    def test_override_link_to_project(self):
        # Ссылка на проект в роли каталога памяти — каталог, содержащий проект: отслеживаемый файл
        # репозитория — не память, как бы ни был записан путь.
        for path in (self.link / "a.txt", self.proj / "a.txt"):
            self.assertFalse(self.is_memory(path, self.proj, self.link), path)

    def test_project_given_by_link(self):
        # Проект задан ссылкой, каталог памяти — её цель.
        for path in (self.link / "a.txt", self.proj / "a.txt"):
            self.assertFalse(self.is_memory(path, self.link, self.proj), path)

    def test_nested_repository_through_link(self):
        # Вложенный репозиторий проекта виден и по пути через ссылку: запись в него — память.
        subprocess.run(["git", "init", "-q", str(self.proj / ".mem")], check=True)
        self.assertTrue(self.is_memory(self.link / ".mem" / "fact.md", self.proj, self.link))

    def test_directory_beside_project_is_memory(self):
        other = self.root / "other"
        other.mkdir()
        self.assertTrue(self.is_memory(other / "x.md", self.proj, other))


class ImportParseTest(unittest.TestCase):
    """Разбор импортов @путь памяти (guard_memory._imports) и пределы обхода импортов."""

    # Корпус: строки из примеров документации Claude Code о памяти (code.claude.com/docs/en/memory) → пути
    # импортов от каталога /m. Лишний путь стоит лишнего вызова судьи, пропущенный — записи в память без него.
    CORPUS = {
        "See @README for project overview and @package.json for available npm commands for this project.":
            {"/m/README", "/m/package.json"},
        "- git workflow @docs/git-instructions.md": {"/m/docs/git-instructions.md"},
        "- @~/.claude/my-project-instructions.md": {"/h/.claude/my-project-instructions.md"},
        "- API conventions @Design\\ Docs/api-conventions.md": {"/m/Design Docs/api-conventions.md"},
        "writing `@README` keeps the text literal": set(),
        "@AGENTS.md": {"/m/AGENTS.md"},
        # Ссылка с якорем, абсолютный путь и путь вверх; почта и «@» без пути — не импорт.
        "@./a.md#part @/etc/x.md @../up.md": {"/m/a.md", "/etc/x.md", "/up.md"},
        "mail me@host.md, @ alone, @/ root, @@x, @#tag": set(),
        # Пунктуация и разметка у пути: путь и с хвостом, и без него.
        "See @README.": {"/m/README.", "/m/README"},
        "**@bold.md**": {"/m/bold.md**", "/m/bold.md"},
        "```md\n@fenced.md\n```\n@after.md": {"/m/after.md"},
        "~~~\n@open.md": set(),
        # Строка из серии кавычек и текста с кавычкой в остатке — код в строке (marked), не ограждение блока.
        "```npm test```\n@notes/team.md": {"/m/notes/team.md"},
        "Run ```npm test``` first.\n@a.md": {"/m/a.md"},
        "```js `x`\n@a.md": {"/m/a.md"},
        "```js\n@fenced.md\n```\n@after.md": {"/m/after.md"},
        # «<!-->» и «<!--->» — закрытые комментарии (marked): импорт за ними разбирается.
        "<!--> @a.md": {"/m/a.md"},
        "<!---> @a.md": {"/m/a.md"},
        "<!-- @hidden.md -->\n@shown.md": {"/m/shown.md"},
    }

    def test_corpus(self):
        with mock.patch.dict(os.environ, {"HOME": "/h"}):
            for text, expected in self.CORPUS.items():
                with self.subTest(text):
                    self.assertEqual(guard_memory._imports(text, "/m"), expected)

    def test_strip_code_edges(self):
        # Незакрытый комментарий и серия кавычек без пары той же длины — текст: импорты за ними разбираются.
        cases = {
            "<!-- open @a.md": "<!-- open @a.md",
            "<!-- x --> @a.md <!-- y": "  @a.md <!-- y",
            # «<!-->» и «<!--->» закрыты сразу (marked: `<!--(?:-?>|…)`), «-->» после них — текст.
            "<!-->@a.md": " @a.md",
            "<!--->@a.md-->": " @a.md-->",
            "``code ` inside`` @a.md": "  @a.md",
            "` one `` two @a.md": "` one `` two @a.md",
            "t ```` x `` y ` z ```` `a` ``": "t     ``",
            # Комментарий снимается раньше кода в строке.
            "`<!--` @a.md -->": "` ",
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(guard_memory._strip_code(text), expected)

    def test_strip_code_linear(self):
        # Файл памяти — до MAX_IMPORT_FILE байт. Время разбора на вчетверо большем входе: незакрытые комментарии —
        # около 4 раз (предел 8), серии кавычек разной длины без пары — около 2,5 раза, потому что серий вдвое
        # больше (предел 6).
        def runs(size):
            text, length = "", 1
            while len(text) < size:
                text, length = text + "`" * length + "a", length + 1
            return text

        for make, size, ratio in ((lambda n: "<!--" * (n // 4), 4000, 8), (runs, 20000, 6)):
            small, large = make(size), make(4 * size)
            with self.subTest(small[:10]):
                assert_linear(self, lambda: guard_memory._strip_code(small), lambda: guard_memory._strip_code(large),
                              ratio)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.config = self.root / "cfg"
        (self.config / "rules").mkdir(parents=True)
        self.project = self.root / "proj"
        self.project.mkdir()
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config), "HOME": str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        common._messages.clear()
        self.addCleanup(common._messages.clear)

    def imported(self):
        return guard_memory._imported_files(str(self.project))

    def test_oversized_file_imports_not_followed(self):
        # Файл больше предела Claude Code не читает: он сам — импорт, его импорты — нет.
        (self.config / "CLAUDE.md").write_text("@big.md\n", encoding="utf-8")
        (self.config / "big.md").write_text("@next.md\n" + "x" * 64, encoding="utf-8")
        with mock.patch.object(guard_memory, "MAX_IMPORT_FILE", 32):
            found = self.imported()
        self.assertIn(str(self.config / "big.md"), found)
        self.assertNotIn(str(self.config / "next.md"), found)

    def test_file_limit_warns(self):
        (self.config / "CLAUDE.md").write_text("@a.md\n", encoding="utf-8")
        (self.config / "a.md").write_text("@b.md\n", encoding="utf-8")
        (self.config / "b.md").write_text("@c.md\n", encoding="utf-8")
        with mock.patch.object(guard_memory, "MAX_IMPORT_FILES", 2):
            found = self.imported()
        self.assertIn(str(self.config / "b.md"), found)
        self.assertNotIn(str(self.config / "c.md"), found)
        self.assertEqual(len(common._messages), 1)
        self.assertIn("импорты памяти", common._messages[0])

    def test_byte_limit_warns(self):
        (self.config / "CLAUDE.md").write_text("@a.md\n", encoding="utf-8")
        (self.config / "a.md").write_text("@b.md\n" + "x" * 20, encoding="utf-8")
        (self.config / "b.md").write_text("@c.md\n", encoding="utf-8")
        with mock.patch.object(guard_memory, "MAX_IMPORT_BYTES", 20):
            found = self.imported()
        self.assertIn(str(self.config / "a.md"), found)
        self.assertNotIn(str(self.config / "b.md"), found)
        self.assertEqual(len(common._messages), 1)
        self.assertIn("импорты памяти", common._messages[0])

    def test_cycles_and_special_files(self):
        # Взаимный импорт и цикл ссылок rules/ не зацикливают обход; FIFO не читается (иначе хук ждал бы писателя).
        (self.config / "CLAUDE.md").write_text("@a.md @fifo\n", encoding="utf-8")
        (self.config / "a.md").write_text("@CLAUDE.md\n", encoding="utf-8")
        (self.config / "rules" / "loop").symlink_to(self.config / "rules")
        (self.config / "rules" / "r.md").write_text("@from-rule.md\n", encoding="utf-8")
        os.mkfifo(self.config / "fifo")
        found = self.imported()
        for name in ("a.md", "fifo", "CLAUDE.md", "rules/from-rule.md"):
            self.assertIn(str(self.config / name), found, name)
        self.assertEqual(common._messages, [])

    def test_case_insensitive_probe(self):
        # Каталог находится и по имени с обращённым регистром — тот же файл: файловая система без учёта регистра.
        # Linux различает регистр, такую систему подменяет ссылка.
        (self.root / "Data").mkdir()
        self.assertFalse(common.case_insensitive(str(self.root / "Data" / "new" / "x.md")))
        (self.root / "dATA").symlink_to(self.root / "Data")
        self.assertTrue(common.case_insensitive(str(self.root / "Data" / "new" / "x.md")))


class ManagedDirTest(unittest.TestCase):
    def test_platform_paths(self):
        for platform, expected in (("linux", "/etc/claude-code"),
                                   ("darwin", "/Library/Application Support/ClaudeCode")):
            with mock.patch.object(sys, "platform", platform):
                self.assertEqual(guard_memory.managed_dir(), expected, platform)


class MemoryMcpNameTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(guard_memory._words("addMemories_now"), ["add", "memories", "now"])
        self.assertEqual(guard_memory._words("saveLTMemory2x"), ["save", "lt", "memory", "2", "x"])
        self.assertEqual(guard_memory._words("SAVEMemory"), ["save", "memory"])
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
        # Аббревиатура перед словом памяти и цифра после него — границы слов.
        "mcp__x__saveLTMemory": True, "mcp__x__SAVEMemory": True, "mcp__x__storeMemory2": True,
        "mcp__x__getLTMemory": False,
        # Глагол через два слова до слова памяти — вне окна _writes_memory (add, to, ai, memory).
        "mcp__x__addToAIMemory": False,
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
                     "mcp__x__save_MEMORY", "mcp__x__Memorise_fact", "mcp__x__remember", "mcp__x__REMEMBER_this",
                     "mcp__x__saveLTMemory", "mcp__x__addToAIMemory", "mcp__x__SAVEMemory", "mcp__x__storeMemory2"):
            self.assertRegex(tool, matcher)
        for tool in ("WriteFile", "Bash", "mcp__github__create_issue"):
            self.assertIsNone(re.search(matcher, tool), tool)

