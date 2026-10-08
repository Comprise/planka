"""Git-код плагина под чужим глобальным git-конфигом пользователя.

tests/__init__.py изолирует процесс тестов от git-настроек машины. Здесь враждебный конфиг передаётся
явно — GIT_CONFIG_GLOBAL на временный файл — только вызовам плагина: snapshot.capture,
snapshot.changed_since, comments.extract, common.project_root, guard_memory.is_memory_path (git check-ignore)
и хукам remind.py и judge_stop.py подпроцессом. Git-команды подготовки репозитория идут под изолированным
конфигом процесса.

Настройки из HOSTILE не должны менять результат: плагин отключает их флагами вызова git или читает
вывод, на который они не действуют. core.excludesFile законно меняет результат — плагин его уважает
(ExcludesFileTest).
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import Env, PLANKA_DIR

sys.path.insert(0, str(PLANKA_DIR))
import comments  # noqa: E402
import common  # noqa: E402
import guard_memory  # noqa: E402
import snapshot  # noqa: E402

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
OLD = "# old z\n" + "z = 1\n" * 5

# Скрипт, который git зовёт как внешний diff, пейджер и монитор файловой системы: печатает строки,
# похожие на вывод diff, — разбор плагина не должен их увидеть, — и завершается с ошибкой: монитор
# со сбоем git обходит полным сканированием. Монитор, который отвечает успехом и врёт, обманывает и сам git.
JUNK = """#!/bin/sh
echo 'diff --git a/junk.py b/junk.py'
echo '+++ b/junk.py'
echo '@@ -1 +1,500 @@'
exit 1
"""

# Файл атрибутов core.attributesFile: файлы .py — бинарные для diff, у a.py — textconv-драйвер junk.
ATTRIBUTES = "*.py -diff\na.py diff=junk\n"

# Настройка git → значение; "{junk}" — путь к скрипту JUNK, "{attrs}" — к файлу ATTRIBUTES.
HOSTILE = {
    "color.ui": "always",
    "color.diff": "always",
    "color.status": "always",
    "diff.noprefix": "true",
    "diff.mnemonicPrefix": "true",
    "diff.external": "{junk}",
    "diff.renames": "copies",
    "diff.context": "7",
    "diff.relative": "true",
    "diff.ignoreSubmodules": "none",
    "core.quotePath": "true",
    "core.pager": "{junk}",
    "core.fsmonitor": "{junk}",
    "core.attributesFile": "{attrs}",
    "diff.junk.textconv": "{junk}",
    "status.showUntrackedFiles": "no",
    "status.relativePaths": "false",
    "status.renames": "copies",
    "status.branch": "false",
    "commit.gpgsign": "true",
    "log.showSignature": "true",
}

# Сливает близкие ханки git diff -U0 с контекстом между ними; comments._added_lines отменяет это
# флагом --inter-hunk-context=0.
INTER_HUNK = {"diff.interHunkContext": "10"}

# Выключает поиск переименований в git diff; comments._added_lines включает его флагом --find-renames.
NO_RENAMES = {"diff.renames": "false"}

# Ожидаемый результат HostileCase.run_scenario под изолированным конфигом.
EXPECTED_DIRTY = ["b.py", "pre.py", "same.py"]
EXPECTED_CHANGED = [("a.py", True), ("b.py", True), ("moved.py", True), ("new.py", True), ("old.py", False),
                    ("pre.py", True), ("sp ace.py", True), ("ü.py", True)]
EXPECTED_COMMENTS = ["a.py: # c1", "a.py: # c2", "b.py: # b changed", "moved.py: # m", "new.py: # n", "pre.py: # pre2",
                     "sp ace.py: # s", "ü.py: # u"]


def git(*args, cwd):
    """Git подготовки репозитория — под изолированным конфигом процесса тестов."""
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def write_config(path, settings, junk):
    """Глобальный git-конфиг path из settings; значение "{junk}" — путь junk, "{attrs}" — файл ATTRIBUTES
    рядом с junk."""
    attrs = junk.parent / "attributes"
    attrs.write_text(ATTRIBUTES, encoding="utf-8")
    path.write_text("", encoding="utf-8")
    for key, value in settings.items():
        subprocess.run(["git", "config", "--file", str(path), key, value.format(junk=junk, attrs=attrs)],
                       check=True)
    return path


@unittest.skipUnless(shutil.which("git"), "нет git")
class HostileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.junk = self.dir / "junk.sh"
        self.junk.write_text(JUNK, encoding="utf-8")
        self.junk.chmod(0o755)
        self.count = 0
        common._reset()

    def tearDown(self):
        common._reset()
        self.tmp.cleanup()

    def config(self, settings):
        self.count += 1
        return write_config(self.dir / f"gitconfig{self.count}", settings, self.junk)

    def repo(self):
        """Репозиторий с коммитом: a.py с комментарием «# keep» в строке 5, b.py, ü.py, old.py с комментарием
        «# old z»; .gitattributes проекта делает b.py и ü.py бинарными для diff."""
        self.count += 1
        root = self.dir / f"repo{self.count}"
        root.mkdir()
        git("init", "-q", cwd=root)
        write(root, "a.py", "x = 1\n" * 4 + "# keep\n" + "x = 1\n" * 25)
        write(root, "b.py", "x = 1\n")
        write(root, "ü.py", "y = 1\n")
        write(root, "old.py", OLD)
        write(root, ".gitattributes", "b.py binary\nü.py -diff\n")
        git("add", ".", cwd=root)
        git("commit", "-qm", "i", cwd=root)
        return root

    def run_scenario(self, cfg):
        """(грязные пути снимка, changed_since, строки comments.extract, вершина ли
        репозитория project_root из подкаталога) реплики:
        на старте b.py изменён, pre.py и same.py не отслеживаются; за реплику — комментарии в строках 3 и 7
        a.py вокруг неизменной «# keep», правки b.py, pre.py, ü.py, новые new.py и «sp ace.py»,
        old.py переименован в moved.py коммитом и в moved.py дописан «# m». cfg — глобальный конфиг вызовов плагина."""
        root = self.repo()
        write(root, "b.py", "x = 2\n")
        write(root, "pre.py", "# pre\n")
        write(root, "same.py", "# same\n")
        with hostile(cfg):
            snap = snapshot.capture(root)
        lines = (root / "a.py").read_text(encoding="utf-8").splitlines()
        lines[2], lines[6] = "# c1", "# c2"
        write(root, "a.py", "\n".join(lines) + "\n")
        write(root, "b.py", "# b changed\n")
        write(root, "pre.py", "# pre2\n")
        write(root, "ü.py", "# u\n")
        write(root, "new.py", "# n\n")
        write(root, "sp ace.py", "# s\n")
        git("mv", "old.py", "moved.py", cwd=root)
        git("commit", "-qm", "mv", cwd=root)
        write(root, "moved.py", OLD + "# m\n")
        (root / "pkg").mkdir()
        with hostile(cfg):
            changed = snapshot.changed_since(root, snap)
            lines, _, _, _ = comments.extract(
                root, [p for p, _ in changed], snap["head"], snap["sub_heads"])
            top = common.project_root(root / "pkg")
        return sorted(snap["dirty"]), changed, lines, top == root


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def hostile(cfg):
    """Глобальный git-конфиг cfg на время вызовов плагина в этом процессе."""
    return mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)})


class HostileGitConfigTest(HostileCase):
    def test_baseline(self):
        self.assertEqual(self.run_scenario(os.devnull),
                         (EXPECTED_DIRTY, EXPECTED_CHANGED, EXPECTED_COMMENTS, True))

    def test_each_setting_keeps_result(self):
        expected = (EXPECTED_DIRTY, EXPECTED_CHANGED, EXPECTED_COMMENTS, True)
        for key, value in HOSTILE.items():
            with self.subTest(setting=key):
                self.assertEqual(self.run_scenario(self.config({key: value})), expected)

    def test_all_settings_together_keep_result(self):
        self.assertEqual(self.run_scenario(self.config(HOSTILE)),
                         (EXPECTED_DIRTY, EXPECTED_CHANGED, EXPECTED_COMMENTS, True))

    def test_inter_hunk_context_keeps_comments(self):
        """Под diff.interHunkContext неизменная «# keep» между правками в строки комментариев не попадает."""
        self.assertEqual(self.run_scenario(self.config(INTER_HUNK))[2], EXPECTED_COMMENTS)

    def test_renames_off_keeps_comments(self):
        """Под diff.renames=false переименованный moved.py сравнивается со старым путём, а не целиком."""
        self.assertEqual(self.run_scenario(self.config(NO_RENAMES))[2], EXPECTED_COMMENTS)


# Ожидаемый результат HostileCase.memory_scenario под изолированным конфигом: путь → память ли он.
EXPECTED_MEMORY = {"tracked.md": False, "new.md": False, "ign/a.md": True, "deep/er/x.md": False,
                   "sp ace.md": False, "ü.md": False}


def memory_paths(root, rels=tuple(EXPECTED_MEMORY)):
    """{rel: память ли root/rel} при каталоге cowork — самом репозитории root, он же проект: память только
    то, что исключил git. Каталог настроек и managed — несуществующие каталоги рядом с root."""
    env = {"CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": str(root), "CLAUDE_CONFIG_DIR": str(root.parent / "cfg"),
           "CLAUDE_CODE_REMOTE_MEMORY_DIR": ""}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(guard_memory, "managed_dir", return_value=str(root.parent / "managed")):
        return {rel: guard_memory.is_memory_path(str(root / rel), str(root)) for rel in rels}


class HostileMemoryTest(HostileCase):
    """guard_memory._repository_file (git check-ignore) под враждебным конфигом: файл репозитория остаётся
    файлом репозитория, исключённый .gitignore — памятью."""

    def memory_scenario(self, cfg):
        root = self.repo()
        write(root, ".gitignore", "ign/\n")
        write(root, "tracked.md", "t\n")
        git("add", ".", cwd=root)
        git("commit", "-qm", "t", cwd=root)
        write(root, "new.md", "n\n")
        write(root, "ign/a.md", "a\n")
        write(root, "ü.md", "u\n")
        write(root, "sp ace.md", "s\n")
        with hostile(cfg):
            return memory_paths(root)

    def test_baseline(self):
        self.assertEqual(self.memory_scenario(os.devnull), EXPECTED_MEMORY)

    def test_each_setting_keeps_result(self):
        for key, value in HOSTILE.items():
            with self.subTest(setting=key):
                self.assertEqual(self.memory_scenario(self.config({key: value})), EXPECTED_MEMORY)

    def test_all_settings_together_keep_result(self):
        self.assertEqual(self.memory_scenario(self.config(HOSTILE)), EXPECTED_MEMORY)


class ExcludesFileTest(HostileCase):
    """core.excludesFile пользователя законно меняет результат: игнорируемый файл не попадает в изменения."""

    def test_user_excluded_file_not_changed(self):
        ignore = self.dir / "ignore"
        ignore.write_text("ignored.py\n", encoding="utf-8")
        cfg = self.config({"core.excludesFile": str(ignore)})
        found = {}
        for name, conf in (("isolated", os.devnull), ("user", cfg)):
            root = self.repo()
            with hostile(conf):
                snap = snapshot.capture(root)
            write(root, "ignored.py", "# i\n")
            with hostile(conf):
                found[name] = snapshot.changed_since(root, snap)
        self.assertEqual(found["isolated"], [("ignored.py", True)])
        self.assertEqual(found["user"], [])

    def test_user_excluded_file_is_memory(self):
        # Исключённый core.excludesFile файл в каталоге памяти, равном проекту, — память, как исключённый
        # .gitignore.
        ignore = self.dir / "ignore"
        ignore.write_text("ignored.md\n", encoding="utf-8")
        cfg = self.config({"core.excludesFile": str(ignore)})
        root = self.repo()
        write(root, "ignored.md", "i\n")
        found = {}
        for name, conf in (("isolated", os.devnull), ("user", cfg)):
            with hostile(conf):
                found[name] = memory_paths(root, ("ignored.md",))["ignored.md"]
        self.assertEqual(found, {"isolated": False, "user": True})


@unittest.skipUnless(shutil.which("git"), "нет git")
class HostileHookTest(unittest.TestCase):
    """remind.py и judge_stop.py подпроцессом: под враждебным конфигом судья получает то же, что без него."""

    def judged(self, gitconfig):
        """Блок <content> промпта судьи Stop после реплики с правкой a.py."""
        env = Env()
        self.addCleanup(env.close)
        project = env.project
        git("init", "-q", cwd=project)
        write(project, "a.py", "x = 1\n" * 4 + "# keep\n" + "x = 1\n" * 25)
        git("add", ".", cwd=project)
        git("commit", "-qm", "i", cwd=project)
        extra = {"GIT_CONFIG_GLOBAL": str(gitconfig), "CLAUDE_PROJECT_DIR": str(project)}
        r = env.run("remind.py", env.hook_input("UserPromptSubmit", prompt="x"), **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = (project / "a.py").read_text(encoding="utf-8").splitlines()
        lines[2], lines[6] = "# c1", "# c2"
        write(project, "a.py", "\n".join(lines) + "\n")
        write(project, "ü.py", "# u\n")
        # Транскрипт реплики по форме write_turn из test_judge_stop: реплика автора и ответ ассистента.
        env.transcript.write_text("".join(json.dumps(e) + "\n" for e in (
            {"type": "user", "message": {"role": "user", "content": "реплика автора"}},
            {"type": "assistant", "message": {"model": "claude-test-model", "content": [
                {"type": "text", "text": "Поправил."}]}})), encoding="utf-8")
        rec = env.data / "rec.txt"
        r = env.run("judge_stop.py", env.hook_input("Stop", last_assistant_message="Поправил.", stop_hook_active=False),
                    PLANKA_STUB="ok", PLANKA_STUB_RECORD=str(rec), **extra)
        self.assertEqual(r.stdout, "", r.stderr)
        return rec.read_text(encoding="utf-8").split("\n<content>\n", 1)[1].split("\n</content>\n", 1)[0]

    def test_hooks_under_hostile_config(self):
        with tempfile.TemporaryDirectory() as d:
            junk = pathlib.Path(d) / "junk.sh"
            junk.write_text(JUNK, encoding="utf-8")
            junk.chmod(0o755)
            cfg = write_config(pathlib.Path(d) / "gitconfig", HOSTILE, junk)
            isolated, user = self.judged(os.devnull), self.judged(cfg)
        self.assertIn("a.py: # c1", isolated)
        self.assertIn("a.py: # c2", isolated)
        self.assertIn("ü.py: # u", isolated)
        self.assertNotIn("# keep", isolated)
        self.assertNotIn("junk", isolated)
        self.assertEqual(user, isolated)
