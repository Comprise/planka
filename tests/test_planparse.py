import pathlib
import sys
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "planka"
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

    def test_tasks_without_wave_are_wave_one(self):
        tasks = planparse.parse_plan(PLAN_EN)
        self.assertEqual([t.wave for t in tasks], [1, 1])


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


if __name__ == "__main__":
    unittest.main()
