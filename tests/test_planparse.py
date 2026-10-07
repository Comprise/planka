import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
PLANKA_DIR = REPO / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import planparse  # noqa: E402

PLAN_RU = """# План

## Волна 1

### Задача 1: Скелет
**Файлы:**
- Создать: `a/one.py`
- Создать: `b/two.py:10-20`

### Задача 2: Общий модуль
Файлы: `c/three.py`, `b/two.py`

## Волна 2

### Задача 3: Хук
**Files:**
- Modify: `a/one.py`
- Test: `tests/test_one.py`
"""

PLAN_EN = """### Task 1: Alpha
**Files:**
- Create: `x.py`

### Task 2: Beta
**Files:**
- Create: `x.py`
"""


class ParseTest(unittest.TestCase):
    def test_parse_ru(self):
        tasks = planparse.parse_plan(PLAN_RU)
        self.assertEqual([(t.wave, t.number, t.title) for t in tasks],
                         [(1, 1, "Скелет"), (1, 2, "Общий модуль"), (2, 3, "Хук")])
        self.assertEqual(tasks[0].files, ["a/one.py", "b/two.py"])
        self.assertEqual(tasks[1].files, ["c/three.py", "b/two.py"])
        self.assertEqual(tasks[2].files, ["a/one.py", "tests/test_one.py"])

    def test_no_structure_is_none(self):
        self.assertIsNone(planparse.parse_plan("Просто текст без задач."))
        self.assertIsNone(planparse.parse_plan("### Задача 1: Без файлов\nделаем"))

    def test_tasks_without_wave_have_no_wave(self):
        tasks = planparse.parse_plan(PLAN_EN)
        self.assertEqual([t.wave for t in tasks], [None, None])

    def test_plan_without_waves_has_no_conflicts(self):
        self.assertEqual(planparse.shared_files(planparse.parse_plan(PLAN_EN)), [])

    def test_task_before_first_wave_is_outside(self):
        tasks = planparse.parse_plan("### Task 0: Prep\nFiles: `x.py`\n" + PLAN_RU)
        self.assertEqual(tasks[0].wave, None)
        self.assertEqual(planparse.shared_files(tasks), [("b/two.py", 1, [1, 2])])


class SharedFilesTest(unittest.TestCase):
    def test_conflict_within_wave_only(self):
        tasks = planparse.parse_plan(PLAN_RU)
        conflicts = planparse.shared_files(tasks)
        self.assertEqual(conflicts, [("b/two.py", 1, [1, 2])])

    def test_format(self):
        text = planparse.format_conflicts([("b/two.py", 1, [1, 2]), ("x.py", 1, [1, 2, 3])])
        self.assertEqual(text, "файл b/two.py принадлежит задачам 1 и 2 волны 1\n"
                               "файл x.py принадлежит задачам 1, 2 и 3 волны 1")

    def test_no_conflict(self):
        tasks = planparse.parse_plan(PLAN_RU.replace("`c/three.py`, `b/two.py`", "`c/three.py`"))
        self.assertEqual(planparse.shared_files(tasks), [])


class FilesLineTest(unittest.TestCase):
    def test_none_marker_is_no_files(self):
        plan = ("### Задача 1: A\nФайлы: нет\n### Задача 2: B\nFiles: none\n"
                "### Задача 3: C\n**Файлы:** нет\n### Задача 4: D\n**Файлы:**\n- —\n"
                "### Задача 5: E\nФайлы: `e.py`\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual([(t.number, t.files) for t in tasks], [(5, ["e.py"])])
        self.assertEqual(planparse.shared_files(tasks), [])
        self.assertIsNone(planparse.parse_plan("### Задача 1: A\nФайлы: нет\n### Задача 2: B\nFiles: -\n"))

    def test_several_backticked_paths_on_one_item(self):
        plan = "### Task 1: A\n**Files:**\n- Test: `a.py`, `b.py:3-9`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "b.py"])

    def test_colon_outside_bold(self):
        plan = ("### Задача 1: A\n**Файлы**:\n- Создать: `a.py`\n"
                "### Task 2: B\n**Files**: `b.py`, `c.py`\n")
        self.assertEqual([t.files for t in planparse.parse_plan(plan)], [["a.py"], ["b.py", "c.py"]])


def _files(line, head="**Files:**\n"):
    tasks = planparse.parse_plan(f"### Task 1: A\n{head}{line}\n")
    return tasks[0].files if tasks else []


def _two_tasks(line1, line2):
    return planparse.parse_plan(f"### Task 1: A\n**Files:**\n{line1}\n"
                                f"### Task 2: B\n**Files:**\n{line2}\n")


class PathExtractionTest(unittest.TestCase):
    def test_backticks_in_description_are_not_paths(self):
        tasks = _two_tasks("- Modify: `common.py` (add `run_hook`)",
                           "- Modify: `judge_tool.py` (call `run_hook`)")
        self.assertEqual([t.files for t in tasks], [["common.py"], ["judge_tool.py"]])
        self.assertEqual(planparse.shared_files(tasks), [])

    def test_commas_in_description_are_not_paths(self):
        tasks = _two_tasks("- Modify: a.py (tests, docs)", "- Modify: b.py (tests, docs)")
        self.assertEqual([t.files for t in tasks], [["a.py"], ["b.py"]])
        self.assertEqual(planparse.shared_files(tasks), [])

    def test_leading_backticked_run_only(self):
        self.assertEqual(_files("- Test: `a.py`, `b.py:3-9`"), ["a.py", "b.py"])
        self.assertEqual(_files("- Modify: `a.py`, `b.py` (add `run_hook`)"), ["a.py", "b.py"])
        self.assertEqual(_files("- Modify: add `x` first"), [])

    def test_unquoted_paths(self):
        self.assertEqual(_files("- Modify: a.py, b.py"), ["a.py", "b.py"])
        self.assertEqual(_files("- Modify: a.py, b (see notes)"), ["a.py", "b"])
        self.assertEqual(_files("- Modify: some words here"), [])

    def test_no_files_markers(self):
        for head in ["Files: empty", "Файлы: нет.", "Файлы: *нет*", "Файлы: нет (только чтение)",
                     "Files: none — read-only"]:
            self.assertEqual(_files("", head=head), [], head)


class FenceTest(unittest.TestCase):
    def test_fenced_block_is_not_structure(self):
        plan = ("### Задача 1: Настоящая\n**Файлы:**\n- Создать: `a.py`\n\n"
                "```markdown\n### Задача 2: Пример в блоке\n**Файлы:**\n- Создать: `a.py`\n```\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual([(t.number, t.files) for t in tasks], [(1, ["a.py"])])

    def test_fence_closes_only_with_same_char_and_length(self):
        plan = ("### Задача 1: A\n**Файлы:**\n- Создать: `a.py`\n"
                "````markdown\n```\n### Задача 2: B\nFiles: `a.py`\n```\n````\n"
                "~~~\n### Задача 3: C\nFiles: `a.py`\n~~~\n")
        self.assertEqual([t.number for t in planparse.parse_plan(plan)], [1])

    def test_fence_nested_in_list_item(self):
        plan = ("### Задача 1: A\n**Файлы:**\n- Создать: `a.py`\n\n1. Шаг\n\n"
                "    ```markdown\n    Files: `z.py`\n    ```\n")
        self.assertEqual([(t.number, t.files) for t in planparse.parse_plan(plan)], [(1, ["a.py"])])

    def test_repo_plan_is_disjoint(self):
        text = (REPO / "tests" / "fixtures" / "plan-waves.md").read_text(encoding="utf-8")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(text)), [])


if __name__ == "__main__":
    unittest.main()
