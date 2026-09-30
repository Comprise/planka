"""Детерминированный разбор плана: волны, задачи, владение файлами."""
import dataclasses
import re

_WAVE = re.compile(r"^#+\s*(?:Волна|Wave)\s+(\d+)", re.IGNORECASE)
_TASK = re.compile(r"^#+\s*(?:Задача|Task)\s+(\d+)\s*[:.]\s*(.*)$", re.IGNORECASE)
_FILES_HEAD = re.compile(r"^\**\s*(?:Файлы|Files)\s*\**\s*:\s*\**\s*(.*)$", re.IGNORECASE)
_ITEM = re.compile(r"^\s*[-*]\s+(.*)$")
_PREFIX = re.compile(r"^(?:Create|Modify|Test|Delete|Создать|Изменить|Тест|Удалить)\s*:\s*",
                     re.IGNORECASE)
_NO_FILES = {"", "нет", "none", "empty", "—", "-"}
_LEADING_SPAN = re.compile(r"[\s,]*`([^`]+)`")
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


@dataclasses.dataclass
class PlanTask:
    wave: int
    number: int
    title: str
    files: list


def _lines_suffix_stripped(path):
    return re.sub(r":\d+(?:-\d+)?$", "", path).strip()


def _paths(fragment):
    """Пути строки файлов: ведущая серия `…` через пробелы и запятые, а без кавычек — токены
    без пробелов через запятую; описание и маркер «нет»/«none»/«empty»/«—» путём не считаются."""
    text = _PREFIX.sub("", fragment.strip())
    if "`" in text:
        out, pos = [], 0
        while m := _LEADING_SPAN.match(text, pos):
            out.append(_lines_suffix_stripped(m.group(1)))
            pos = m.end()
        return [p for p in out if p]
    text = _TRAILING_PAREN.sub("", text).strip().strip("*_").strip().rstrip(".;").strip()
    first = text.split()[0].lower().strip(".,;:*_") if text else ""
    if first in _NO_FILES:
        return []
    parts = [p.strip() for p in text.split(",")]
    if not all(p and not any(c.isspace() for c in p) for p in parts):
        return []
    return [_lines_suffix_stripped(p) for p in parts]


def parse_plan(text):
    tasks = []
    wave = 1
    current = None
    lines = text.splitlines()
    fence = None
    i = 0
    while i < len(lines):
        line = lines[i]
        # Содержимое fenced-блока — пример, а не структура плана; блок закрывает
        # строка из тех же символов не короче открывающей и без info-строки.
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            i += 1
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not m.group(2).strip():
                fence = None
            i += 1
            continue
        m = _WAVE.match(line)
        if m:
            wave = int(m.group(1))
            current = None
            i += 1
            continue
        m = _TASK.match(line)
        if m:
            current = PlanTask(wave=wave, number=int(m.group(1)), title=m.group(2).strip(), files=[])
            tasks.append(current)
            i += 1
            continue
        m = _FILES_HEAD.match(line.strip()) if current is not None else None
        if m:
            current.files.extend(_paths(m.group(1)))
            i += 1
            while i < len(lines):
                item = _ITEM.match(lines[i])
                if not item:
                    break
                current.files.extend(_paths(item.group(1)))
                i += 1
            continue
        i += 1
    tasks = [t for t in tasks if t.files]
    return tasks or None


def shared_files(tasks):
    owners = {}
    for t in tasks:
        for f in dict.fromkeys(t.files):
            owners.setdefault((t.wave, f), []).append(t.number)
    return [(f, wave, nums) for (wave, f), nums in sorted(owners.items()) if len(nums) > 1]


def _join(nums):
    nums = [str(n) for n in nums]
    return nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " и " + nums[-1]


def format_conflicts(conflicts):
    return "\n".join(f"файл {f} принадлежит задачам {_join(nums)} волны {wave}"
                     for f, wave, nums in conflicts)
