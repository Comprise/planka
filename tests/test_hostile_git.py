"""Git-код плагина под чужим глобальным git-конфигом пользователя.

tests/__init__.py изолирует процесс тестов от git-настроек машины. Здесь враждебный конфиг передаётся
явно — GIT_CONFIG_GLOBAL на временный файл — только вызовам плагина: snapshot.capture,
snapshot.changed_since, comments.extract, common.project_root (git rev-parse, git ls-files под домашним
каталогом-репозиторием), guard_memory.is_memory_path (git check-ignore),
manifest_watch.list_manifests (git ls-files), manifest_watch.head_names (git cat-file --batch версий ref и сторон
конфликта индекса, для имени с возвратом каретки — git cat-file blob),
manifest_watch.restored_names, manifest_watch.project_names и manifest_watch.compare (git ls-files, git ls-tree,
git cat-file --batch)
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
import time
import unittest
from unittest import mock

from tests.helpers import Env, PLANKA_DIR

sys.path.insert(0, str(PLANKA_DIR))
import comments  # noqa: E402
import common  # noqa: E402
import guard_memory  # noqa: E402
import manifest_watch  # noqa: E402
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


# Ожидаемый результат HostileRootTest.root_scenario: каталог запуска → корень относительно дома.
EXPECTED_ROOTS = {"Projects/app": "Projects/app", ".config/ü vim": "."}


class HostileRootTest(HostileCase):
    """common.project_root с CLAUDE_PROJECT_DIR под домашним каталогом-репозиторием (git ls-files -z --cached) под
    враждебным конфигом: проект без отслеживаемых файлов — сам каталог, каталог с отслеживаемыми — вершина."""

    def root_scenario(self, cfg):
        home = self.repo()
        write(home, ".config/ü vim/init.lua", "x\n")
        git("add", ".", cwd=home)
        git("commit", "-qm", "c", cwd=home)
        write(home, "Projects/app/a.py", "x = 1\n")
        found = {}
        for rel in ("Projects/app", ".config/ü vim"):
            with hostile(cfg), mock.patch.dict(os.environ, {"HOME": str(home), "CLAUDE_PROJECT_DIR": str(home / rel)}):
                found[rel] = os.path.relpath(common.project_root(str(home / rel)).resolve(), home.resolve())
        return found

    def test_baseline(self):
        self.assertEqual(self.root_scenario(os.devnull), EXPECTED_ROOTS)

    def test_each_setting_keeps_result(self):
        for key, value in HOSTILE.items():
            with self.subTest(setting=key):
                self.assertEqual(self.root_scenario(self.config({key: value})), EXPECTED_ROOTS)

    def test_all_settings_together_keep_result(self):
        self.assertEqual(self.root_scenario(self.config(HOSTILE)), EXPECTED_ROOTS)


# Ожидаемый результат HostileManifestTest.manifest_scenario под изолированным конфигом: режим, манифесты
# проекта, имена версий из HEAD и сторон конфликта, имена ref команды, имена проекта реестра PyPI и новые имена
# после правки.
# Версия package.json из HEAD объединена с версией из MERGE_HEAD (lodash) незавершённого слияния; у
# cf/requirements.txt — версия HEAD (attrs) и стороны конфликта индекса :1:, :2:, :3: (base, ours, theirs).
# Имена ref команды — из манифестов и setup.py их деревьев. Имена проекта: манифесты рабочего дерева и манифесты и
# setup.py версии HEAD (legacydep).
EXPECTED_MANIFESTS = ("git", ["cf/requirements.txt", "new/requirements.txt", "nl/requirements\r.txt",
                              "package.json", "sp ace/Cargo.toml", "ü/requirements.txt"],
                      {"package.json": ["lodash", "react"], "ü/requirements.txt": ["rich"],
                       "new/requirements.txt": None, "nl/requirements\r.txt": ["httpx"],
                       "cf/requirements.txt": ["attrs", "base", "ours", "theirs"]},
                      {"npm": ["lodash", "react"], "pypi": ["attrs", "click", "flask", "legacydep", "rich"],
                       "crates.io": ["serde"]},
                      ["attrs", "flask", "httpx", "legacydep", "rich"],
                      ({"new/requirements.txt": ["evilpkg"]}, []))


class HostileManifestTest(HostileCase):
    """Манифесты проекта (git ls-files), их версии в HEAD (git cat-file) и имена ref, откуда команда возвращает
    файлы (git cat-file и git ls-tree restored_names), под враждебным конфигом."""

    def manifest_scenario(self, cfg):
        root = self.repo()
        write(root, ".gitignore", "ign/\n")
        # Драйвер junk из HOSTILE для манифеста: версия из HEAD читается без textconv.
        write(root, ".gitattributes", "package.json diff=junk\nsetup.py diff=junk\ncf/requirements.txt diff=junk\n")
        # Файл _LEGACY версии HEAD: его имена читает git ls-tree и git cat-file --batch дерева HEAD.
        write(root, "setup.py", "setup(install_requires=['legacydep'])\n")
        write(root, "cf/requirements.txt", "attrs\n")
        write(root, "package.json", '{"dependencies": {"react": "^18"}}\n')
        write(root, "ü/requirements.txt", "rich\n")
        write(root, "sp ace/Cargo.toml", "[dependencies]\nserde = \"1\"\n")
        # Имя с возвратом каретки (имя с переводом строки не манифест): версия HEAD — отдельным `git cat-file blob`,
        # под драйвером junk тоже без textconv.
        write(root, "nl/.gitattributes", "* diff=junk\n")
        write(root, "nl/requirements\r.txt", "httpx\n")
        git("add", ".", cwd=root)
        git("commit", "-qm", "m", cwd=root)
        git("checkout", "-qb", "feat", cwd=root)
        write(root, "package.json", '{"dependencies": {"react": "^18", "lodash": "^4"}}\n')
        git("commit", "-qam", "feat", cwd=root)
        git("checkout", "-q", "-", cwd=root)
        # stash с отслеживаемым и неотслеживаемым манифестом: имена ref читаются тем же cat-file --batch.
        write(root, "ü/requirements.txt", "rich\nflask\n")
        write(root, "st/requirements.txt", "click\n")
        git("stash", "-q", "-u", cwd=root)
        # Незавершённое слияние feat: git пишет MERGE_HEAD файлом, update-ref псевдоссылку не создаёт.
        sha = subprocess.run([*GIT, "rev-parse", "feat"], cwd=root, check=True, capture_output=True, text=True)
        (root / ".git" / "MERGE_HEAD").write_text(sha.stdout, encoding="utf-8")
        write(root, "package.json", '{"dependencies": {"react": "^18", "axios": "^1"}}\n')
        write(root, "new/requirements.txt", "flask\n")
        write(root, "ign/requirements.txt", "flask\n")
        write(root, "tests/fixtures/x/package.json", "{}\n")
        # Конфликт cf/requirements.txt: стороны :1:, :2:, :3: в индексе в форме `git ls-files -s`.
        stages = []
        for stage, name in ((1, "base"), (2, "ours"), (3, "theirs")):
            blob = subprocess.run([*GIT, "hash-object", "-w", "--stdin"], cwd=root, input=f"{name}\n", check=True,
                                  capture_output=True, text=True).stdout.strip()
            stages.append(f"100644 {blob} {stage}\tcf/requirements.txt\n")
        git("rm", "-q", "--cached", "cf/requirements.txt", cwd=root)
        subprocess.run([*GIT, "update-index", "--index-info"], cwd=root, input="".join(stages), check=True,
                       capture_output=True, text=True)
        with hostile(cfg):
            mode, found = manifest_watch.list_manifests(root, time.monotonic() + 30)
            heads = {}
            for rel in ("package.json", "ü/requirements.txt", "new/requirements.txt", "nl/requirements\r.txt",
                        "cf/requirements.txt"):
                names = manifest_watch.head_names(str(root / rel), manifest_watch.watched_kind(rel), 10)
                heads[rel] = None if names is None else sorted(names)
            # Начало сессии позже коммитов: ref — работа до сессии.
            restored = manifest_watch.restored_names(root, "git checkout feat -- package.json; git stash pop",
                                                     int(time.time()) + 100, time.monotonic() + 30)
            project = sorted(manifest_watch.project_names(root, "requirements", time.monotonic() + 30))
            entry = manifest_watch.take(root, time.monotonic() + 30)
        # Перенос из setup.py версии HEAD и новое имя: сравнение читает дерево HEAD.
        write(root, "new/requirements.txt", "flask\nlegacydep\nevilpkg\n")
        with hostile(cfg):
            compared = manifest_watch.compare(entry, time.monotonic() + 30)
        return mode, sorted(found), heads, restored, project, compared

    def test_baseline(self):
        self.assertEqual(self.manifest_scenario(os.devnull), EXPECTED_MANIFESTS)

    def test_each_setting_keeps_result(self):
        for key, value in HOSTILE.items():
            with self.subTest(setting=key):
                self.assertEqual(self.manifest_scenario(self.config({key: value})), EXPECTED_MANIFESTS)

    def test_all_settings_together_keep_result(self):
        self.assertEqual(self.manifest_scenario(self.config(HOSTILE)), EXPECTED_MANIFESTS)


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
                {"type": "text", "text": "Комментарии в a.py."}]}})), encoding="utf-8")
        rec = env.data / "rec.txt"
        # Сообщение без заявки «готово»: судью зовёт фильтр документации.
        r = env.run("judge_stop.py", env.hook_input("Stop", last_assistant_message="Комментарии в a.py.",
                                                    stop_hook_active=False),
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
