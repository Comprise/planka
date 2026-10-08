import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
PLANKA_DIR = REPO / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import planparse  # noqa: E402
from tests.helpers import assert_linear  # noqa: E402

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
                         [(1, "1", "Скелет"), (1, "2", "Общий модуль"), (2, "3", "Хук")])
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
        self.assertEqual(planparse.shared_files(tasks), [("b/two.py", 1, ["1", "2"])])


class SharedFilesTest(unittest.TestCase):
    def test_conflict_within_wave_only(self):
        tasks = planparse.parse_plan(PLAN_RU)
        conflicts = planparse.shared_files(tasks)
        self.assertEqual(conflicts, [("b/two.py", 1, ["1", "2"])])

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
        self.assertEqual([(t.number, t.files) for t in tasks], [("5", ["e.py"])])
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


class FilesFormsTest(unittest.TestCase):
    def test_files_as_list_item(self):
        plan = "## Волна 1\n### Задача 1: A\n- **Файлы:** `a.py`\n### Задача 2: B\n- **Файлы:** `a.py`\n"
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("a.py", 1, ["1", "2"])])

    def test_blank_line_inside_list(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:**\n- `a.py`\n\n- `b.py`\n"
                "### Задача 2: B\n**Файлы:**\n- `b.py`\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("b.py", 1, ["1", "2"])])

    def test_dot_slash_is_same_file(self):
        plan = "## Волна 1\n### Задача 1: A\nФайлы: `./a.py`\n### Задача 2: B\nФайлы: `a.py`\n"
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("a.py", 1, ["1", "2"])])


class FenceTest(unittest.TestCase):
    def test_fenced_block_is_not_structure(self):
        plan = ("### Задача 1: Настоящая\n**Файлы:**\n- Создать: `a.py`\n\n"
                "```markdown\n### Задача 2: Пример в блоке\n**Файлы:**\n- Создать: `a.py`\n```\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual([(t.number, t.files) for t in tasks], [("1", ["a.py"])])

    def test_fence_closes_only_with_same_char_and_length(self):
        plan = ("### Задача 1: A\n**Файлы:**\n- Создать: `a.py`\n"
                "````markdown\n```\n### Задача 2: B\nFiles: `a.py`\n```\n````\n"
                "~~~\n### Задача 3: C\nFiles: `a.py`\n~~~\n")
        self.assertEqual([t.number for t in planparse.parse_plan(plan)], ["1"])

    def test_fence_nested_in_list_item(self):
        plan = ("### Задача 1: A\n**Файлы:**\n- Создать: `a.py`\n\n1. Шаг\n\n"
                "    ```markdown\n    Files: `z.py`\n    ```\n")
        self.assertEqual([(t.number, t.files) for t in planparse.parse_plan(plan)], [("1", ["a.py"])])

    def test_repo_plan_is_disjoint(self):
        text = (REPO / "tests" / "fixtures" / "plan-waves.md").read_text(encoding="utf-8")
        tasks = planparse.parse_plan(text)
        self.assertEqual([(t.wave, t.number) for t in tasks],
                         [(1, "1"), (1, "2"), (1, "3"), (2, "4"), (2, "5"), (2, "6"), (3, "7")])
        self.assertEqual(planparse.shared_files(tasks), [])


def _structure(plan):
    return [(t.wave, t.number, t.files) for t in planparse.parse_plan(plan) or []]


class HeadingScopeTest(unittest.TestCase):
    def test_other_heading_closes_task(self):
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n### Схождение волны 1\nФайлы: `b.py`\n"
                "### Задача 2: B\nФайлы: `b.py`\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [])
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"]), (1, "2", ["b.py"])])

    def test_heading_not_deeper_than_wave_closes_wave(self):
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n## Итог\nФайлы: `a.py`\n"
                "### Задача 2: B\nФайлы: `a.py`\n")
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"]), (None, "2", ["a.py"])])
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [])

    def test_deeper_heading_keeps_wave(self):
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n#### Шаги\n### Задача 2: B\nФайлы: `a.py`\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("a.py", 1, ["1", "2"])])


class TaskHeadingFormsTest(unittest.TestCase):
    def test_separators_and_bold(self):
        plan = ("## Волна 1\n### Задача 1 — A\nФайлы: `a.py`\n### Задача 2 (backend): B\nФайлы: `a.py`\n"
                "**Задача 3: C**\nФайлы: `a.py`\n### Task 4 - D\nFiles: `a.py`\n### Задача 5.\nФайлы: `a.py`\n"
                "**Задача 6:** F\nФайлы: `a.py`\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual([(t.number, t.title) for t in tasks],
                         [("1", "A"), ("2", "B"), ("3", "C"), ("4", "D"), ("5", ""), ("6", "F")])
        self.assertEqual(planparse.shared_files(tasks), [("a.py", 1, ["1", "2", "3", "4", "5", "6"])])

    def test_dotted_numbers_kept(self):
        plan = "## Волна 1\n### Задача 1.1: A\nФайлы: `a.py`\n### Задача 1.2. B\nФайлы: `a.py`\n"
        conflicts = planparse.shared_files(planparse.parse_plan(plan))
        self.assertEqual(planparse.format_conflicts(conflicts), "файл a.py принадлежит задачам 1.1 и 1.2 волны 1")

    def test_number_without_separator_is_not_task(self):
        plan = "## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n### Задача 2 описание\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"])])

    def test_bold_and_setext_waves(self):
        plan = ("**Волна 1**\n\n### Задача 1: A\nФайлы: `a.py`\n\nВолна 2\n-------\n\n"
                "### Задача 2: B\nФайлы: `a.py`\n\nWave 3\n======\n### Задача 3: C\nФайлы: `a.py`\n")
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"]), (2, "2", ["a.py"]), (3, "3", ["a.py"])])

    def test_dash_underline_of_other_text_is_rule(self):
        plan = "## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n---\n### Задача 2: B\nФайлы: `a.py`\nТекст\n---\n"
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("a.py", 1, ["1", "2"])])


class FilesLineFormsTest(unittest.TestCase):
    def test_item_markers_and_prefixes(self):
        plan = ("### Задача 1: A\n**Файлы:**\n1. `a.py`\n2) `b.py`\n+ `c.py`\n- **Создать:** `d.py`\n"
                "- Modified: `e.py`\n- **Изменить**: `f.py`\n")
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"])

    def test_singular_head(self):
        plan = "### Задача 1: A\nФайл: `a.py`\n### Задача 2: B\n**File:** `b.py`\n"
        self.assertEqual([t.files for t in planparse.parse_plan(plan)], [["a.py"], ["b.py"]])

    def test_step_after_blank_line_is_not_file(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:**\n- `a.py`\n\n- `make test` проходит\n"
                "### Задача 2: B\n**Файлы:**\n- `b.py`\n\n- `make test` проходит\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [])

    def test_path_or_prefixed_item_after_blank_line_continues(self):
        plan = "### Задача 1: A\n**Файлы:**\n- `a.py`\n\n- `b.py`\n\n- Создать: `c.py`\n\n- sub/d.py\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "b.py", "c.py", "sub/d.py"])

    def test_spans_after_note_in_parens(self):
        self.assertEqual(_files("- `x.py` (новый), `a.py`"), ["x.py", "a.py"])
        self.assertEqual(_files("- Создать: `x.py` (новый) `a.py` (тест)"), ["x.py", "a.py"])

    def test_span_with_space_is_not_path(self):
        self.assertEqual(_files("- `make test`, `a.py`"), ["a.py"])

    def test_html_comment_is_not_structure(self):
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n<!--\n### Задача 2: B\nФайлы: `a.py`\n-->\n"
                "### Задача 3: C\nФайлы: `c.py` <!-- `a.py` -->\n")
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"]), (1, "3", ["c.py"])])


class OwnershipNoiseTest(unittest.TestCase):
    def test_repeated_task_number_is_one_owner(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:** `a.py`\n### Задача 2: B\n**Файлы:** `b.py`\n"
                "### Итог волны\n**Задача 1** — сделано\n**Файлы:** `a.py`\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [])
        plan += "### Задача 2: B\n**Файлы:** `a.py`\n"
        conflicts = planparse.shared_files(planparse.parse_plan(plan))
        self.assertEqual(conflicts, [("a.py", 1, ["1", "2"])])
        self.assertEqual(planparse.format_conflicts(conflicts), "файл a.py принадлежит задачам 1 и 2 волны 1")

    def test_described_path_item_under_files_is_not_owned(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:** `a.py`\n- `common.py` — только читать\n"
                "- `pytest` зелёный\n- `x.py` (новый)\n- `y.py`, `z.py`\n- Изменить: `w.py` — тесты\n"
                "### Задача 2: B\n**Файлы:** `common.py`\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual(tasks[0].files, ["a.py", "x.py", "y.py", "z.py", "w.py"])
        self.assertEqual(planparse.shared_files(tasks), [])

    def test_described_item_does_not_end_list(self):
        plan = "### Задача 1: A\n**Файлы:**\n- `a.py`\n- `common.py` — только читать\n- `b.py`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "b.py"])


class ReadOnlyNoteTest(unittest.TestCase):
    def test_read_only_item_is_not_owned(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:**\n- Изменить: `a.py`\n- `common.py` (только чтение)\n"
                "### Задача 2: B\n**Файлы:**\n- Изменить: `b.py`\n- `common.py` (только чтение)\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual([t.files for t in tasks], [["a.py"], ["b.py"]])
        self.assertEqual(planparse.shared_files(tasks), [])

    def test_read_note_forms(self):
        for note in ["(только чтение)", "(Только для чтения)", "(только читать)", "(чтение)", "(читать)",
                     "(не трогать)", "(не менять)", "(read-only)", "(readonly)", "(Read only)", "(read)",
                     "(do not modify)", "(don't edit)", "(*read-only*)", "(только чтение: нужен `X`)",
                     "(read-only, for the interface)", "(без изменений)", "(Без изменений)", "(reference)",
                     "(reference only)", "(только импорт)", "(для справки)", "(только для справки)",
                     "(справка)", "(справочно)", "(контекст)", "(no changes)", "(no change)", "(unchanged)",
                     "(read access)", "(reading)", "(imports only)", "(import only)", "(import)", "(context only)",
                     "(не изменяется)", "(без изменения)"]:
            self.assertEqual(_files(f"- `a.py`\n- `common.py` {note}"), ["a.py"], note)
            self.assertEqual(_files(f"- `a.py`\n- common.py {note}"), ["a.py"], note)

    def test_note_that_only_starts_like_marker_is_owned(self):
        for note in ["(чтение конфига и запись)", "(новый)", "(readme)", "(read and write)",
                     "(reference implementation)", "(контекст запроса)", "(добавить `run_hook`)",
                     "(import path)", "(без изменений в API)"]:
            self.assertEqual(_files(f"- `common.py` {note}"), ["common.py"], note)

    def test_note_applies_to_preceding_path(self):
        self.assertEqual(_files("- `x.py`, `common.py` (только чтение), `y.py`"), ["x.py", "y.py"])
        self.assertEqual(_files("- `common.py` (read-only), `x.py` (новый)"), ["x.py"])
        self.assertEqual(_files("- x.py, common.py (read-only)"), ["x.py"])

    def test_inline_files_line(self):
        self.assertEqual(_files("", head="**Файлы:** `a.py`, `common.py` (только чтение)"), ["a.py"])
        self.assertEqual(_files("", head="**Файлы:** `a.py`, `common.py` — только чтение"), ["a.py"])
        self.assertEqual(_files("", head="Files: `a.py`, `common.py` - read-only"), ["a.py"])
        self.assertEqual(_files("", head="Files: `a.py` — новый модуль"), ["a.py"])

    def test_prefix_keeps_ownership(self):
        self.assertEqual(_files("- Изменить: `common.py` (только чтение)"), ["common.py"])
        self.assertEqual(_files("- Modify: `a.py`, `common.py` (read-only)"), ["a.py", "common.py"])

    def test_read_only_item_continues_list_after_blank(self):
        self.assertEqual(_files("- `a.py`\n\n- `common.py` (read-only)\n\n- `b.py`"), ["a.py", "b.py"])


class ReadNoteConflictTest(unittest.TestCase):
    def test_reference_note_gives_no_conflict(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:**\n- `a.py`\n- `common.py` (reference)\n"
                "### Задача 2: B\n**Файлы:**\n- `b.py`\n- `common.py` (reference)\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [])

    def test_unknown_note_keeps_conflict(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:**\n- `common.py` (добавить `run_hook`)\n"
                "### Задача 2: B\n**Файлы:**\n- `common.py` (добавить `stop_hook`)\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("common.py", 1, ["1", "2"])])

    def test_second_files_line_with_sentence_is_description(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:** `a.py`\n\nФайл: `cfg.yaml` читается при старте.\n"
                "### Задача 2: B\n**Файлы:** `cfg.yaml`\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual([t.files for t in tasks], [["a.py"], ["cfg.yaml"]])
        self.assertEqual(planparse.shared_files(tasks), [])

    def test_second_files_line_with_paths_or_dash_note_is_owned(self):
        plan = "### Задача 1: A\n**Файлы:** `a.py`\n\nФайлы: `b.py`, `c.py` — новые.\nФайл: `d.py`.\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "b.py", "c.py", "d.py"])

    def test_first_files_line_with_sentence_keeps_paths(self):
        self.assertEqual(_files("", head="Файлы: `a.py` читается при старте."), ["a.py"])

    def test_connector_continues_series(self):
        for head in ("Файлы: `a.py` и `b.py`", "Files: `a.py` and `b.py`", "Файлы: `a.py` или `b.py`",
                     "Files: `a.py` or `b.py`", "Files: `a.py` + `b.py`", "Files: `a.py`, and `b.py`"):
            self.assertEqual(_files("", head=head), ["a.py", "b.py"], head)

    def test_repeated_files_line_with_connector_or_note_is_owned(self):
        plan = ("## Волна 1\n### Задача 1: A\n**Файлы:** `a.py`\n\n**Files:** `x.py` and `y.py`\n"
                "### Задача 2: B\n**Файлы:** `x.py`\n")
        tasks = planparse.parse_plan(plan)
        self.assertEqual(tasks[0].files, ["a.py", "x.py", "y.py"])
        self.assertEqual(planparse.shared_files(tasks), [("x.py", 1, ["1", "2"])])
        plan = "### Задача 1: A\n**Файлы:** `a.py`\n\n**Files:** `t.py` (new) for tests\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "t.py"])
        plan = "### Задача 1: A\n**Файлы:** `a.py`\n\nFiles: `t.py` and `u.py` are read at startup.\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py"])

    def test_files_line_state_is_per_task(self):
        plan = ("### Задача 1: A\nФайлы: `a.py`\n### Задача 2: B\nФайл: `b.py` правится при старте.\n")
        self.assertEqual(_structure(plan), [(None, "1", ["a.py"]), (None, "2", ["b.py"])])

    def test_first_files_line_with_sentence_in_each_task_gives_conflict(self):
        plan = ("## Wave 1\n### Task 1\n**Files:** `a.py`\n### Task 2\n**Files:** `shared.py` is created here.\n"
                "### Task 3\n**Files:** `shared.py` gets a new function.\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("shared.py", 1, ["2", "3"])])


class RobustnessTest(unittest.TestCase):
    def test_bom_at_start_keeps_wave(self):
        plan = "\ufeff" + PLAN_RU.split("\n", 2)[2]
        self.assertTrue(plan.startswith("\ufeff## Волна 1"))
        self.assertEqual([t.wave for t in planparse.parse_plan(plan)], [1, 1, 2])
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("b/two.py", 1, ["1", "2"])])

    def test_adversarial_input_is_linear(self):
        # Пункт списка файлов при множителе k: большой вход (k=4) вчетверо длиннее малого (k=1).
        items = [lambda k: "- " + "a/" * 2000 * k + " x",
                 lambda k: "- " + "a." * 5000 * k + " (",
                 lambda k: "- " + "a.py" + " " * 7500 * k + "x",
                 lambda k: "- `a.py`" + " " * 7500 * k + "x",
                 lambda k: "- x.py" + " " * 7500 * k + "(a",
                 lambda k: "- " + "(" * 5000 * k,
                 lambda k: "- `c.py` (read" + " " * 7500 * k + "x)",
                 lambda k: "- `a.py` — read" + " " * 7500 * k + "x",
                 lambda k: "- Create" + " " * 7500 * k + "x"]
        plans = [lambda k, item=item: f"### Task 1: A\n**Files:**\n{item(k)}\n" for item in items]
        plans += [lambda k, item=item: f"### Task 1: A\nFiles: {item(k)[2:]}\n" for item in items]
        plans += [lambda k: "# a" + " " * 7500 * k + "b\n",
                  lambda k: "### Task 1: A\nFiles: `a.py` " + "<!---->" * 37500 * k + "\n",
                  lambda k: "### Task 1: A\nFiles" + " " * 7500 * k + "x\n"]
        for plan in plans:
            small, large = plan(1), plan(4)
            assert_linear(self, lambda: planparse.parse_plan(small), lambda: planparse.parse_plan(large),
                          msg=large[:40])


class ParserBranchesTest(unittest.TestCase):
    def test_item_after_blank_line_ends_list_even_if_next_is_path(self):
        plan = "### Задача 1: A\n**Файлы:**\n- `a.py`\n\n- `make test` проходит\n- `b.py`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py"])

    def test_non_path_item_without_blank_line_keeps_list(self):
        plan = "### Задача 1: A\n**Файлы:**\n- `a.py`\n\n- `b.py`\n- `make test` проходит\n- `c.py`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "b.py", "c.py"])

    def test_fence_not_closed_by_info_line(self):
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n```markdown\n```python\n"
                "### Задача 2: B\nФайлы: `a.py`\n```\n")
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"])])

    def test_fence_not_closed_by_other_char(self):
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n```\n~~~\n### Задача 2: B\nФайлы: `a.py`\n```\n")
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"])])
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n~~~\n```\n### Задача 2: B\nФайлы: `a.py`\n~~~\n")
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"])])

    def test_fence_ends_files_list(self):
        plan = "### Задача 1: A\n**Файлы:**\n- `a.py`\n```\ncode\n```\n- `b.py`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py"])

    def test_comment_end_is_cut_whole(self):
        plan = "### Задача 1: A\nФайлы: `c.py` <!-- x --> `d.py`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["c.py", "d.py"])
        plan = "### Задача 1: A\n<!--\nтекст\n--> Файлы: `b.py`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["b.py"])

    def test_indented_hash_line_is_not_heading(self):
        plan = "## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n    ### Задача 2: B\nФайлы: `b.py`\n"
        self.assertEqual(_structure(plan), [(1, "1", ["a.py", "b.py"])])
        plan = "## Волна 1\n   ### Задача 1: A\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"])])

    def test_closing_hashes_dropped_from_title(self):
        tasks = planparse.parse_plan("## Волна 1 ##\n### Задача 1: A ###\nФайлы: `a.py`\n")
        self.assertEqual([(t.wave, t.title) for t in tasks], [(1, "A")])
        tasks = planparse.parse_plan("### Задача 1: C#\nФайлы: `a.py`\n")
        self.assertEqual(tasks[0].title, "C#")

    def test_heading_without_space_after_hashes(self):
        plan = "##Волна 1\n###Задача 1: A\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"])])

    def test_setext_underline_indent(self):
        plan = "### Задача 1: A\nФайлы: `a.py`\n   Волна 2\n   ---\n### Задача 2: B\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(None, "1", ["a.py"]), (2, "2", ["a.py"])])
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\nВолна 2\n    ---\n"
                "### Задача 2: B\nФайлы: `a.py`\n")
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"]), (1, "2", ["a.py"])])

    def test_keywords_ignore_case(self):
        plan = "## wave 1\n### task 1: A\nФайлы: `a.py`\n## ВОЛНА 2\n### задача 2: B\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(1, "1", ["a.py"]), (2, "2", ["a.py"])])

    def test_bold_forms_ignore_case(self):
        plan = "**wave 2: Name**\n**task 1:** A\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(2, "1", ["a.py"])])

    def test_files_head_after_blank_line_resets_blank(self):
        plan = "### Задача 1: A\n**Файлы:**\n- `a.py`\n\nФайлы: `b.py` — новый.\n- запустить тесты\n- `c.py`\n"
        self.assertEqual(planparse.parse_plan(plan)[0].files, ["a.py", "b.py", "c.py"])

    def test_bold_wave_needs_separator(self):
        plan = "**Волна 1 описание**\n### Задача 1: A\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(None, "1", ["a.py"])])
        plan = "**Wave 2: Name**\n### Задача 1: A\nФайлы: `a.py`\n"
        self.assertEqual(_structure(plan), [(2, "1", ["a.py"])])

    def test_list_item_above_equals_underline_is_not_heading(self):
        plan = ("## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n- пункт\n=====\n"
                "### Задача 2: B\nФайлы: `a.py`\n")
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("a.py", 1, ["1", "2"])])

    def test_plain_task_line_is_not_heading(self):
        plan = "### Задача 1: A\nФайлы: `a.py`\n\nЗадача 2: B\nФайлы: `b.py`\n"
        self.assertEqual(_structure(plan), [(None, "1", ["a.py", "b.py"])])

    def test_prefix_with_note(self):
        self.assertEqual(_files("- Create (temp only, not committed): `$ROOT/t.sh` (v3, below), fixture repos"),
                         ["$ROOT/t.sh"])
        self.assertEqual(_files("- `a.py`\n\n- Modify (only if a GREEN gate fails): `s.md`"), ["a.py", "s.md"])
        self.assertEqual(_files("- **Modify (optional):** `s.md`"), ["s.md"])

    def test_bold_wave_kept_under_task_subheading(self):
        plan = "**Волна 1**\n\n### Задача 1: A\nФайлы: `a.py`\n\n#### Шаги\n\n1. x\n\n### Задача 2: B\nФайлы: `a.py`\n"
        self.assertEqual(planparse.shared_files(planparse.parse_plan(plan)), [("a.py", 1, ["1", "2"])])

    def test_bold_wave_closed_by_heading_not_deeper_than_first_task(self):
        tail = "### Задача 3: C\nФайлы: `a.py`\n"
        plan = "**Волна 1**\n### Задача 1: A\nФайлы: `a.py`\n#### Шаги\n### Задача 2: B\nФайлы: `a.py`\n"
        for closing in ("### Итог\n", "## Итог\n"):
            self.assertEqual(_structure(plan + closing + tail),
                             [(1, "1", ["a.py"]), (1, "2", ["a.py"]), (None, "3", ["a.py"])], closing)
        self.assertEqual(_structure("**Волна 1**\n#### Цель\n" + tail), [(None, "3", ["a.py"])])
        self.assertEqual(_structure("**Волна 1**\n**Задача 1:** A\nФайлы: `a.py`\n#### Шаги\n" + tail),
                         [(1, "1", ["a.py"]), (None, "3", ["a.py"])])

    def test_setext_level(self):
        body = "### Задача 1: A\nФайлы: `a.py`\n## Итог\n### Задача 2: B\nФайлы: `a.py`\n"
        self.assertEqual(_structure("Волна 1\n=======\n" + body), [(1, "1", ["a.py"]), (1, "2", ["a.py"])])
        self.assertEqual(_structure("Волна 1\n-------\n" + body), [(1, "1", ["a.py"]), (None, "2", ["a.py"])])

    def test_underscore_bold_headings(self):
        plan = "__Волна 1__\n__Задача 1:__ A\nФайлы: `a.py`\n## __Wave 2__\n### __Task 2: B__\nФайлы: `a.py`\n"
        tasks = planparse.parse_plan(plan)
        self.assertEqual([(t.wave, t.number, t.title) for t in tasks], [(1, "1", "A"), (2, "2", "B")])

    def test_seven_hashes_are_not_heading(self):
        plan = "## Волна 1\n### Задача 1: A\nФайлы: `a.py`\n####### Задача 2: B\nФайл: `b.py`\n"
        self.assertEqual(_structure(plan), [(1, "1", ["a.py", "b.py"])])

    def test_read_note_with_dash_explanation(self):
        self.assertEqual(_files("- `a.py`\n- `c.py` (read-only — for the interface)"), ["a.py"])
        self.assertEqual(_files("- `a.py`\n- `c.py` (read-only - for the interface)"), ["a.py"])

    def test_unquoted_paths_drop_final_dot(self):
        self.assertEqual(_files("", head="Files: a.py, b.py."), ["a.py", "b.py"])

    def test_unquoted_note_before_final_dot(self):
        self.assertEqual(_files("", head="Files: a.py, b.py (new)."), ["a.py", "b.py"])
        self.assertEqual(_files("", head="Files: a.py, b.py (read-only)."), ["a.py"])
        self.assertEqual(_files("- a.py, b.py (read-only);"), ["a.py"])

    def test_directory_item_without_dot_is_path(self):
        self.assertEqual(_files("- `a.py`\n\n- docs/"), ["a.py", "docs/"])

    def test_note_applies_only_to_adjacent_path(self):
        self.assertEqual(_files("", head="Files: `a.py` (новый) (только чтение)"), ["a.py"])
        self.assertEqual(_files("- `a.py`, `make test` (read-only)"), ["a.py"])
        self.assertEqual(_files("", head="Files: `a.py`, `make test` - read-only"), ["a.py"])
        self.assertEqual(_files("- `a.py` (новый), `b.py` (read-only)"), ["a.py"])


# Корпус настоящих по форме планов: (волна, номер, файлы) каждой задачи и конфликты владения.
CORPUS = {
    "plan-waves.md": (
        [(1, "1", [".claude-plugin/plugin.json", "hooks/hooks.json", "philosophy.md", "Makefile", ".gitignore"]),
         (1, "2", ["planka/common.py", "planka/prompts.py", "tests/stub/claude", "tests/helpers.py",
                   "tests/__init__.py", "tests/test_common.py", "tests/test_prompts.py"]),
         (1, "3", ["planka/planparse.py", "tests/test_planparse.py"]),
         (2, "4", ["planka/remind.py", "tests/test_remind.py"]),
         (2, "5", ["planka/judge_stop.py", "tests/test_judge_stop.py"]),
         (2, "6", ["planka/judge_tool.py", "tests/test_judge_tool.py"]),
         (3, "7", ["README.md"])],
        []),
    "plan-superpowers.md": (
        [(None, "1", ["src/notes/exporters.py", "tests/test_exporters.py"]),
         (None, "2", ["src/notes/cli.py", "tests/test_cli.py"]),
         (None, "3", ["README.md", "docs/cli.md"])],
        []),
    "plan-superpowers-waves.md": (
        [(1, "1", ["src/api/limiter/bucket.py", "tests/limiter/test_bucket.py"]),
         (1, "2", ["src/api/limiter/store.py", "tests/limiter/test_store.py"]),
         (2, "3", ["src/api/limiter/middleware.py", "src/api/app.py", "tests/limiter/test_middleware.py"]),
         (2, "4", ["docs/settings.md", "README.md"]),
         (2, "5", ["docs/headers.md", "README.md"]),
         (3, "6", ["$EVAL_ROOT/load-fixture.sh", "docs/load-eval.md", "src/api/limiter/bucket.py"]),
         (3, "7", ["src/api/limiter/bucket.py", "tests/limiter/test_bucket.py"])],
        [("README.md", 2, ["4", "5"]), ("src/api/limiter/bucket.py", 3, ["6", "7"])]),
    "plan-canonical.md": (
        [(1, "1", ["planka/cache.py", "tests/test_cache.py"]),
         (1, "2", ["planka/cache_store.py", "tests/test_cache_store.py"]),
         (2, "4", ["planka/common.py", "tests/test_common.py"]),
         (2, "5", ["README.md", "context/architecture.md"])],
        []),
}


class CorpusTest(unittest.TestCase):
    def test_every_plan_has_expectation(self):
        names = sorted(p.name for p in (REPO / "tests" / "fixtures").glob("plan-*.md"))
        self.assertEqual(names, sorted(CORPUS))

    def test_corpus(self):
        for name, expected in CORPUS.items():
            with self.subTest(name):
                text = (REPO / "tests" / "fixtures" / name).read_text(encoding="utf-8")
                structure, conflicts = expected
                self.assertEqual(_structure(text), structure)
                self.assertEqual(planparse.shared_files(planparse.parse_plan(text)), conflicts)
