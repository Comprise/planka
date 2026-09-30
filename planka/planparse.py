"""Детерминированный разбор плана: волны, задачи, владение файлами."""
import dataclasses
import re

_WAVE = re.compile(r"^#+\s*(?:Волна|Wave)\s+(\d+)", re.IGNORECASE)
_TASK = re.compile(r"^#+\s*(?:Задача|Task)\s+(\d+)\s*[:.]\s*(.*)$", re.IGNORECASE)
_FILES_HEAD = re.compile(r"^\**\s*(?:Файлы|Files)\s*:\**\s*(.*)$", re.IGNORECASE)
_ITEM = re.compile(r"^\s*[-*]\s+(.*)$")
_PREFIX = re.compile(r"^(?:Create|Modify|Test|Delete|Создать|Изменить|Тест|Удалить)\s*:\s*",
                     re.IGNORECASE)
_BACKTICK = re.compile(r"`([^`]+)`")


@dataclasses.dataclass
class PlanTask:
    wave: int
    number: int
    title: str
    files: list


def _path(fragment):
    """Путь из фрагмента строки: из обратных кавычек, если есть; без префикса и «:строки»."""
    m = _BACKTICK.search(fragment)
    raw = m.group(1) if m else _PREFIX.sub("", fragment.strip())
    raw = _PREFIX.sub("", raw.strip())
    return re.sub(r":\d+(?:-\d+)?$", "", raw).strip()


def parse_plan(text):
    tasks = []
    wave = 1
    current = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
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
            inline = m.group(1).strip()
            if inline:
                current.files.extend(p for p in (_path(f) for f in inline.split(",")) if p)
            i += 1
            while i < len(lines):
                item = _ITEM.match(lines[i])
                if not item:
                    break
                p = _path(item.group(1))
                if p:
                    current.files.append(p)
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
