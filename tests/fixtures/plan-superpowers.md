<!-- Образец корпуса: форма плана — skills/writing-plans/SKILL.md плагина superpowers 6.4.2 («Plan Document Header», «Task Structure»); содержимое сгенерировано. Волн нет. -->
# Export Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `notes export` command that writes all notes to a single Markdown or JSON file.

**Architecture:** A new `exporters` module holds one function per format; the CLI parses `--format` and `--out` and calls it. Storage is read through the existing `NoteStore` interface, unchanged.

**Tech Stack:** Python 3.12, argparse, pytest.

**Spec:** `docs/specs/2026-09-14-export-design.md`

## Global Constraints

- Python 3.12 standard library only; no new dependencies.
- Output files are UTF-8 with `\n` line endings on every platform.
- Existing commands keep their exit codes and output byte for byte.

## Review Focus

1. Empty store — export writes a valid empty document, not an error.
2. Note title with a newline or a backtick — Markdown output stays parseable.
3. `--out` pointing to an existing file — overwritten only with `--force`.
4. Non-ASCII titles — written as UTF-8, not escaped.
5. `--out -` — output goes to stdout.

---

### Task 1: Markdown and JSON exporters

**Files:**
- Create: `src/notes/exporters.py`
- Test: `tests/test_exporters.py`

**Interfaces:**
- Consumes: `NoteStore.all() -> list[Note]` from `src/notes/store.py` (unchanged).
- Produces: `export_markdown(notes: list[Note]) -> str`, `export_json(notes: list[Note]) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_markdown_empty_store():
    assert export_markdown([]) == "# Notes\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_exporters.py::test_markdown_empty_store -v`
Expected: FAIL with "cannot import name 'export_markdown'"

- [ ] **Step 3: Implement `export_markdown(notes)` in `src/notes/exporters.py`**

Titles become `## ` headings; a newline inside a title is replaced by a space.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_exporters.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/notes/exporters.py tests/test_exporters.py
git commit -m "feat: markdown and json exporters"
```

### Task 2: `export` subcommand

**Files:**
- Modify: `src/notes/cli.py:41-88`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `export_markdown`, `export_json` (Task 1).
- Produces: `notes export --format {md,json} --out PATH [--force]`.

- [ ] **Step 1: Write the failing test**

```python
def test_export_refuses_existing_file(tmp_path):
    out = tmp_path / "n.md"
    out.write_text("x")
    assert main(["export", "--out", str(out)]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::test_export_refuses_existing_file -v`
Expected: FAIL with "invalid choice: 'export'"

- [ ] **Step 3: Add the subparser in `src/notes/cli.py`**

The handler writes to stdout when `--out` is `-`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

### Task 3: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/cli.md`

Example of the section to add:

````markdown
### Task 9: not a real task

**Files:**
- Create: `src/notes/exporters.py`
````

- [ ] **Step 1: Add the `export` section to `docs/cli.md`**
- [ ] **Step 2: Commit**
