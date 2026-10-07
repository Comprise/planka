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

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import common  # noqa: E402
import snapshot  # noqa: E402


def walk_files(root, deadline=None):
    """Файлы снимка capture вне git."""
    snap = snapshot.capture(root, deadline)
    assert snap["mode"] == "walk", snap["mode"]
    return snap["files"]


class WalkCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_relpaths_and_ignored_dirs(self):
        self.write("a.py"); self.write("pkg/b.go"); self.write(".git/HEAD"); self.write("node_modules/x.js")
        self.write("build/out"); self.write(".superpowers/x")
        files = walk_files(self.root)
        self.assertEqual(sorted(files), [".superpowers/x", "a.py", "pkg/b.go"])
        self.assertEqual(len(files["a.py"]), 2)

    def test_gitignore_names_and_exts(self):
        self.write(".gitignore", "secrets/\n*.log\n# comment\nbin\n!keep\nfoo/*.tmp\n")
        self.write("secrets/k"); self.write("run.log"); self.write("bin/tool"); self.write("ok.txt")
        names, exts = snapshot.ignore_rules(self.root)
        self.assertEqual(names, {"secrets", "bin"})
        self.assertEqual(exts, {"log"})
        self.assertEqual(sorted(walk_files(self.root)), [".gitignore", "ok.txt"])

    def test_gitignore_multi_dot_ext(self):
        self.write(".gitignore", "*.min.js\n")
        self.write("a.min.js"); self.write("a.js")
        self.assertEqual(sorted(walk_files(self.root)), [".gitignore", "a.js"])

    def test_nested_gitignore_anchored_name(self):
        self.write("sub/.gitignore", "/cache\n")
        self.write("sub/cache/x"); self.write("cache/y"); self.write("sub/other/z")
        self.assertEqual(sorted(walk_files(self.root)),
                         ["cache/y", "sub/.gitignore", "sub/other/z"])

    def test_nested_gitignore_ext_only_in_own_subtree(self):
        self.write("sub/.gitignore", "*.log\n")
        self.write("sub/a.log"); self.write("sub/deep/b.log"); self.write("a.log")
        self.assertEqual(sorted(walk_files(self.root)), ["a.log", "sub/.gitignore"])

    def test_root_rules_inherited_into_subdirs(self):
        self.write(".gitignore", "*.log\nsecrets\n")
        self.write("sub/.gitignore", "/cache\n")
        self.write("sub/a.log"); self.write("sub/secrets/k"); self.write("sub/cache/x"); self.write("sub/ok")
        self.assertEqual(sorted(walk_files(self.root)), [".gitignore", "sub/.gitignore", "sub/ok"])

    def test_no_gitignore(self):
        self.assertEqual(snapshot.ignore_rules(self.root), (set(), set()))

    def test_symlink_not_followed(self):
        self.write("real/f")
        os.symlink(self.root / "real", self.root / "link")
        self.assertEqual(sorted(walk_files(self.root)), ["real/f"])

    def test_walk_deadline_passed(self):
        self.write("a")
        with self.assertRaises(TimeoutError):
            snapshot.capture(self.root, time.monotonic() - 1)

    def test_non_utf8_name(self):
        name = b"bad\xff.py"
        try:
            with open(os.path.join(os.fsencode(self.root), name), "w") as f:
                f.write("x")
        except OSError:
            self.skipTest("ФС не принимает имя не в UTF-8")
        self.assertEqual(list(walk_files(self.root)), [os.fsdecode(name)])

    def test_no_heads_outside_git(self):
        snap = snapshot.capture(self.root)
        self.assertEqual((snap["head"], snap["sub_heads"]), (None, {}))

    def test_stat_files_deadline(self):
        self.write("a")
        with self.assertRaises(TimeoutError):
            snapshot._stat_files(self.root, ["a"], time.monotonic() - 1)

    def test_stat_files_checks_deadline_periodically(self):
        paths = [f"missing{i}" for i in range(snapshot.STAT_CHECK_EVERY * 2)]
        calls = []
        real = snapshot._remaining
        with mock.patch.object(snapshot, "_remaining", side_effect=lambda d: calls.append(d) or real(d)):
            self.assertEqual(snapshot._stat_files(self.root, paths, time.monotonic() + 60), {})
        self.assertEqual(len(calls), 2)


GIT_ENV = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always"]


@unittest.skipUnless(shutil.which("git"), "нет git")
class GitCaptureTest(unittest.TestCase):
    """Грязные пути и HEAD снимка capture в git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def dirty(self):
        snap = snapshot.capture(self.root)
        self.assertEqual(snap["mode"], "git")
        return snap["dirty"]

    def test_git_rules_are_exact(self):
        self.write(".gitignore", "*.log\n!keep.log\nbuild-*/\n")
        self.write("a.log"); self.write("keep.log"); self.write("build-x/out"); self.write("src/a.py")
        self.write(".git/info/exclude", "local/\n"); self.write("local/x")
        self.write(".superpowers/x")
        self.assertEqual(sorted(self.dirty()), [".gitignore", ".superpowers/x", "keep.log", "src/a.py"])

    def test_tracked_and_untracked_with_odd_names(self):
        self.write("dir/a b.py"); self.write("кириллица.go")
        subprocess.run(["git", "-C", str(self.root), "add", "dir/a b.py"], check=True)
        self.assertEqual(sorted(self.dirty()), ["dir/a b.py", "кириллица.go"])

    def test_deleted_tracked_file_not_a_file(self):
        self.write("gone.py")
        subprocess.run(["git", "-C", str(self.root), "add", "gone.py"], check=True)
        (self.root / "gone.py").unlink()
        self.assertEqual(self.dirty(), {"gone.py": None})

    def test_head_empty_repo_and_after_commit(self):
        self.assertIsNone(snapshot.capture(self.root)["head"])
        self.write("f")
        subprocess.run([*GIT_ENV, "-C", str(self.root), "add", "f"], check=True)
        subprocess.run([*GIT_ENV, "-C", str(self.root), "commit", "-qm", "i"], check=True)
        expected = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        self.assertEqual(snapshot.capture(self.root)["head"], expected)

    def test_symlinks_not_files(self):
        self.write("real")
        os.symlink(self.root / "real", self.root / "link")
        os.symlink(self.root / "nowhere", self.root / "dangling")
        subprocess.run(["git", "-C", str(self.root), "add", "link"], check=True)
        self.assertEqual(sorted(p for p, st in self.dirty().items() if st is not None), ["real"])

    def test_nested_repo_not_submodule_files_included(self):
        self.write("top.py")
        inner = self.root / "nested"
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        self.write("nested/a.py"); self.write("nested/deep/b.py"); self.write("nested/.gitignore", "*.log\n")
        self.write("nested/x.log")
        self.assertEqual(sorted(self.dirty()),
                         ["nested/.gitignore", "nested/a.py", "nested/deep/b.py", "top.py"])

    def test_stat_phase_obeys_deadline(self):
        self.write("a")
        real = snapshot._git_state
        with mock.patch.object(snapshot, "_git_state", side_effect=lambda r, d: real(r, None)):
            with self.assertRaises(TimeoutError):
                snapshot.capture(self.root, time.monotonic() - 1)

    def test_submodule_stages_listed_once(self):
        entries = b"".join(f"160000 {'0' * 40} {stage}\tsub\0".encode() for stage in (1, 2, 3))
        with mock.patch.object(snapshot, "_git", return_value=entries + b"100644 " + b"0" * 40 + b" 0\tf.py\0"):
            self.assertEqual(snapshot._gitlinks(self.root, None), ["sub"])

    def test_nested_submodule_heads(self):
        base = pathlib.Path(self.tmp.name + "-libs")
        base.mkdir()
        try:
            def repo(path):
                subprocess.run(["git", "init", "-q", str(path)], check=True)

            def commit(path, *add):
                subprocess.run([*GIT_ENV, "-C", str(path), "add", *add], check=True, capture_output=True)
                subprocess.run([*GIT_ENV, "-C", str(path), "commit", "-qm", "i"], check=True, capture_output=True)

            def rev(path):
                return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                                      text=True, check=True).stdout.strip()

            inner, mid = base / "inner", base / "mid"
            repo(inner); (inner / "i.py").write_text("x", encoding="utf-8"); commit(inner, "i.py")
            repo(mid)
            subprocess.run([*GIT_ENV, "-C", str(mid), "submodule", "add", "-q", str(inner), "in"],
                           check=True, capture_output=True)
            commit(mid, ".")
            subprocess.run([*GIT_ENV, "-C", str(self.root), "submodule", "add", "-q", str(mid), "m"],
                           check=True, capture_output=True)
            subprocess.run([*GIT_ENV, "-C", str(self.root / "m"), "submodule", "update", "--init", "-q"],
                           check=True, capture_output=True)
            snap = snapshot.capture(self.root)
            self.assertEqual(snap["sub_heads"], {"m": rev(mid), "m/in": rev(inner)})
            (self.root / "m" / "in" / "i.py").write_text("changed", encoding="utf-8")
            self.assertEqual(snapshot.changed_since(self.root, snap), [("m/in/i.py", True)])
        finally:
            shutil.rmtree(base)

    def test_submodule_files_included(self):
        lib = pathlib.Path(self.tmp.name + "-lib")
        subprocess.run(["git", "init", "-q", str(lib)], check=True)
        try:
            (lib / "f.py").write_text("x", encoding="utf-8")
            subprocess.run([*GIT_ENV, "-C", str(lib), "add", "f.py"], check=True)
            subprocess.run([*GIT_ENV, "-C", str(lib), "commit", "-qm", "i"], check=True)
            subprocess.run([*GIT_ENV, "-C", str(self.root), "submodule", "add", "-q", str(lib), "sub"],
                           check=True, capture_output=True)
            (self.root / "sub" / "new.py").write_text("y", encoding="utf-8")
            snap = snapshot.capture(self.root)
            self.assertIn("sub/new.py", snap["dirty"])
            self.assertNotIn("sub/f.py", snap["dirty"])
            lib_head = subprocess.run(["git", "-C", str(lib), "rev-parse", "HEAD"], capture_output=True,
                                      text=True, check=True).stdout.strip()
            self.assertEqual(snap["sub_heads"], {"sub": lib_head})
        finally:
            shutil.rmtree(lib)


@unittest.skipUnless(shutil.which("git"), "нет git")
class ChangedSinceGitTest(unittest.TestCase):
    """capture на старте реплики, правки «за реплику», changed_since на Stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "repo"
        self.git("init", "-q", str(self.root), cwd=self.tmp.name)
        self.write("a.py"); self.write("b.py"); self.write("docs/x.md")
        self.git("add", "."); self.git("commit", "-qm", "i")
        common._reset()

    def tearDown(self):
        common._reset()
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        subprocess.run([*GIT_ENV, *args], cwd=cwd or self.root, check=True, capture_output=True)

    def write(self, rel, text="x", root=None):
        p = (root or self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def roundtrip(self):
        """Снимок через store/load, как его прочтёт judge_stop."""
        state = pathlib.Path(self.tmp.name) / "state"
        state.mkdir(exist_ok=True)
        snapshot.store(state, "s", "p", self.root, snapshot.capture(self.root))
        return snapshot.load(state, "s")

    def test_clean_tree_no_changes(self):
        snap = self.roundtrip()
        self.assertEqual(snap["mode"], "git")
        self.assertEqual(snap["dirty"], {})
        self.assertEqual(snapshot.changed_since(self.root, snap), [])

    def test_large_tree_not_limited(self):
        with mock.patch.object(snapshot, "MAX_FILES", 1):
            snap = self.roundtrip()
            self.write("a.py", "changed")
            self.assertEqual(snapshot.changed_since(self.root, snap), [("a.py", True)])

    def test_commit_during_turn(self):
        snap = self.roundtrip()
        self.write("a.py", "changed"); self.write("new.py")
        self.git("add", "."); self.git("commit", "-qm", "c")
        self.assertEqual(snapshot.changed_since(self.root, snap), [("a.py", True), ("new.py", True)])

    def test_dirty_at_start_counted_only_if_edited(self):
        self.write("a.py", "before turn"); self.write("old.py", "untracked before turn")
        snap = self.roundtrip()
        self.assertEqual(sorted(snap["dirty"]), ["a.py", "old.py"])
        self.assertEqual(snapshot.changed_since(self.root, snap), [])
        self.git("add", "."); self.git("commit", "-qm", "c")
        self.assertEqual(snapshot.changed_since(self.root, snap), [])
        self.write("a.py", "edited in turn")
        self.assertEqual(snapshot.changed_since(self.root, snap), [("a.py", True)])

    def test_untracked_new_and_deleted(self):
        snap = self.roundtrip()
        self.write("pkg/new.go"); (self.root / "b.py").unlink()
        self.write(".gitignore", "*.log\n"); self.write("x.log")
        self.assertEqual(snapshot.changed_since(self.root, snap),
                         [(".gitignore", True), ("b.py", False), ("pkg/new.go", True)])

    def test_deleted_dirty_file(self):
        self.write("tmp.py")
        snap = self.roundtrip()
        (self.root / "tmp.py").unlink()
        self.assertEqual(snapshot.changed_since(self.root, snap), [("tmp.py", False)])

    def test_rename_gives_both_paths(self):
        snap = self.roundtrip()
        self.git("mv", "a.py", "c.py")
        self.assertEqual(snapshot.changed_since(self.root, snap), [("a.py", False), ("c.py", True)])

    def test_symlink_not_reported(self):
        snap = self.roundtrip()
        os.symlink(self.root / "a.py", self.root / "link.py")
        self.assertEqual(snapshot.changed_since(self.root, snap), [])

    def test_non_utf8_name(self):
        name = b"bad\xff.py"
        try:
            with open(os.path.join(os.fsencode(self.root), name), "w") as f:
                f.write("x")
        except OSError:
            self.skipTest("ФС не принимает имя не в UTF-8")
        snap = self.roundtrip()
        self.assertEqual(list(snap["dirty"]), [os.fsdecode(name)])
        self.assertEqual(snapshot.changed_since(self.root, snap), [])
        with open(os.path.join(os.fsencode(self.root), name), "w") as f:
            f.write("longer")
        self.assertEqual(snapshot.changed_since(self.root, snap), [(os.fsdecode(name), True)])

    def test_unborn_repo_then_commit(self):
        other = pathlib.Path(self.tmp.name) / "fresh"
        self.git("init", "-q", str(other), cwd=self.tmp.name)
        self.write("staged.py", root=other); self.write("keep.py", root=other)
        self.git("add", "staged.py", cwd=other)
        snap = snapshot.capture(other)
        self.assertIsNone(snap["head"])
        self.write("made.py", root=other)
        self.git("add", ".", cwd=other); self.git("commit", "-qm", "i", cwd=other)
        self.assertEqual(snapshot.changed_since(other, snap), [("made.py", True)])

    def make_submodule(self):
        lib = pathlib.Path(self.tmp.name) / "lib"
        self.git("init", "-q", str(lib), cwd=self.tmp.name)
        self.write("l.py", root=lib); self.write("m.py", root=lib)
        self.git("add", ".", cwd=lib); self.git("commit", "-qm", "i", cwd=lib)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sub")
        self.git("commit", "-qm", "s")

    def test_submodule_edit_and_commit(self):
        self.make_submodule()
        snap = self.roundtrip()
        self.assertEqual(set(snap["repos"]), {"", "sub"})
        self.assertEqual(snap["sub_heads"], {"sub": snap["repos"]["sub"]})
        self.write("sub/l.py", "changed")
        self.git("commit", "-qam", "c", cwd=self.root / "sub")
        self.write("sub/m.py", "dirty"); self.write("sub/n.py")
        self.assertEqual(snapshot.changed_since(self.root, snap),
                         [("sub/l.py", True), ("sub/m.py", True), ("sub/n.py", True)])

    def test_uninitialized_submodule_skipped(self):
        self.make_submodule()
        clone = pathlib.Path(self.tmp.name) / "clone"
        self.git("clone", "-q", str(self.root), str(clone), cwd=self.tmp.name)
        snap = snapshot.capture(clone)
        self.assertEqual(set(snap["repos"]), {""})
        self.assertEqual(snapshot.changed_since(clone, snap), [])

    def test_nested_repo_not_submodule(self):
        nested = self.root / "nested"
        self.git("init", "-q", str(nested))
        self.write("nested/old.py"); self.write("nested/x.log"); self.write("nested/.gitignore", "*.log\n")
        snap = self.roundtrip()
        self.assertIn("nested", snap["repos"])
        self.assertEqual(sorted(snap["dirty"]), ["nested/.gitignore", "nested/old.py"])
        self.write("nested/new.py"); self.write("nested/y.log")
        self.assertEqual(snapshot.changed_since(self.root, snap), [("nested/new.py", True)])

    def test_new_nested_repo_all_files(self):
        snap = self.roundtrip()
        self.write("vendor/k.py")
        self.assertEqual(snapshot.changed_since(self.root, snap), [("vendor/k.py", True)])
        self.git("init", "-q", str(self.root / "vendor"))
        self.git("add", ".", cwd=self.root / "vendor")
        self.assertEqual(snapshot.changed_since(self.root, snap), [("vendor/k.py", True)])

    def test_vanished_repo_is_undetermined(self):
        self.git("init", "-q", str(self.root / "nested"))
        self.write("nested/a.py")
        snap = self.roundtrip()
        shutil.rmtree(self.root / "nested")
        self.assertIsNone(snapshot.changed_since(self.root, snap))
        self.assertEqual(common._messages,
                         ["planka: сверка документации не проверена: за реплику пропал репозиторий nested"])

    def test_git_removed_is_undetermined(self):
        snap = self.roundtrip()
        shutil.rmtree(self.root / ".git")
        self.assertIsNone(snapshot.changed_since(self.root, snap))
        self.assertEqual(common._messages, ["planka: сверка документации не проверена: за реплику корень "
                                            "проекта перестал быть git-репозиторием"])

    def test_too_many_dirty(self):
        self.write("u1"); self.write("u2")
        with mock.patch.object(snapshot, "MAX_FILES", 1):
            with self.assertRaises(snapshot.TooManyFiles):
                snapshot.capture(self.root)

    def test_deadline(self):
        snap = snapshot.capture(self.root)
        with self.assertRaises(TimeoutError):
            snapshot.capture(self.root, time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            snapshot.changed_since(self.root, snap, time.monotonic() - 1)

    def test_one_ls_files_per_repo(self):
        self.make_submodule()
        calls = []
        real = snapshot._git

        def spy(root, deadline, *args):
            calls.append((os.path.basename(str(root)), args[0] if args[0] != "--no-optional-locks" else args[1]))
            return real(root, deadline, *args)

        with mock.patch.object(snapshot, "_git", side_effect=spy):
            snapshot.capture(self.root)
        self.assertEqual(sorted(calls), [("repo", "ls-files"), ("repo", "status"),
                                         ("sub", "ls-files"), ("sub", "status")])


class ChangedSinceWalkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "a.py").write_text("x", encoding="utf-8")
        common._reset()

    def tearDown(self):
        common._reset()
        self.tmp.cleanup()

    def test_walk_changes(self):
        snap = snapshot.capture(self.root)
        self.assertEqual(snap["mode"], "walk")
        (self.root / "a.py").write_text("longer", encoding="utf-8")
        (self.root / "b.py").write_text("x", encoding="utf-8")
        self.assertEqual(snapshot.changed_since(self.root, snap), [("a.py", True), ("b.py", True)])
        (self.root / "a.py").unlink()
        self.assertEqual(snapshot.changed_since(self.root, snap), [("a.py", False), ("b.py", True)])

    def test_walk_too_many(self):
        with mock.patch.object(snapshot, "MAX_FILES", 0):
            with self.assertRaises(snapshot.TooManyFiles):
                snapshot.capture(self.root)

    @unittest.skipUnless(shutil.which("git"), "нет git")
    def test_git_init_during_turn_is_undetermined(self):
        snap = snapshot.capture(self.root)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.assertIsNone(snapshot.changed_since(self.root, snap))
        self.assertEqual(common._messages, ["planka: сверка документации не проверена: за реплику корень "
                                            "проекта стал git-репозиторием"])


class StoreLoadDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def walk(files, head=None):
        return {"mode": "walk", "head": head, "sub_heads": {}, "files": files}

    def test_roundtrip(self):
        files = {"a.py": [1, 2], "b/c.go": [3, 4]}
        snapshot.store(self.state, "sess/1", "p-1", self.root, self.walk(files))
        got = snapshot.load(self.state, "sess/1")
        self.assertEqual(got["prompt_id"], "p-1")
        self.assertEqual(got["root"], str(self.root))
        self.assertEqual(got["files"], files)
        self.assertEqual([p.name for p in self.state.iterdir()], ["sess_1.snap.json"])

    def test_roundtrip_non_utf8_name(self):
        files = {"bad\udcff.py": [1, 2]}
        snapshot.store(self.state, "s", "p", self.root, self.walk(files, "abc"))
        got = snapshot.load(self.state, "s")
        self.assertEqual(got["files"], files)
        self.assertEqual(got["head"], "abc")

    def test_snapshot_without_mode_rejected(self):
        snapshot.store(self.state, "old", "p", self.root, {"head": None, "sub_heads": {}, "files": {}})
        self.assertIsNone(snapshot.load(self.state, "old"))

    def test_store_load_modes(self):
        snapshot.store(self.state, "g", "p", self.root, {"mode": "git", "head": None, "sub_heads": {},
                                                         "repos": {"": None}, "dirty": {"bad\udcff.py": None}})
        got = snapshot.load(self.state, "g")
        self.assertEqual((got["prompt_id"], got["root"], got["dirty"]), ("p", str(self.root), {"bad\udcff.py": None}))
        snapshot.store(self.state, "x", "p", self.root, {"mode": "git", "files": {}})
        self.assertIsNone(snapshot.load(self.state, "x"))
        snapshot.store(self.state, "y", "p", self.root, {"mode": "other", "files": {}})
        self.assertIsNone(snapshot.load(self.state, "y"))

    def test_load_missing_or_garbage(self):
        self.assertIsNone(snapshot.load(self.state, "none"))
        (self.state / "bad.snap.json").write_text("{", encoding="utf-8")
        self.assertIsNone(snapshot.load(self.state, "bad"))

    def test_load_non_utf8_garbage(self):
        (self.state / "bin.snap.json").write_bytes(b"\xff\xfe\x00garbage")
        self.assertIsNone(snapshot.load(self.state, "bin"))

    def test_diff(self):
        old = {"a": [1, 1], "b": [1, 1], "c": [1, 1]}
        new = {"a": [1, 1], "b": [2, 1], "d": [1, 1]}
        self.assertEqual(snapshot.diff(old, new), ["b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
