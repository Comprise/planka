"""Детерминированный разбор плана: волны, задачи, владение файлами."""
import dataclasses
import re

_WAVE = re.compile(r"^#+\s*(?:Волна|Wave)\s+(\d+)", re.IGNORECASE)
_TASK = re.compile(r"^#+\s*(?:Задача|Task)\s+(\d+)\s*[:.]\s*(.*)$", re.IGNORECASE)
_FILES_HEAD = re.compile(r"^\**\s*(?:Файлы|Files)\s*\**\s*:\s*\**\s*(.*)$", re.IGNORECASE)
_ITEM = re.compile(r"^\s*[-*]\s+(.*)$")
_PREFIX = re.compile(r"^(?:Create|Modify|Test|Delete|Создать|Изменить|Тест|Удалить)\s*:\s*",
                     re.IGNORECASE)
_BACKTICK = re.compile(r"`([^`]+)`")
_NO_FILES = {"", "нет", "none", "—", "-"}
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


@dataclasses.dataclass
class PlanTask:
    wave: int
    number: int
    title: str
    files: list


def _paths(fragment):
    """Пути из фрагмента строки: все в обратных кавычках, иначе части через запятую; без префикса
    и «:строки». Часть без кавычек вида «нет»/«none»/«—» путём не считается."""
    quoted = _BACKTICK.findall(fragment)
    out = []
    for raw in quoted or fragment.split(","):
        raw = _PREFIX.sub("", raw.strip())
        path = re.sub(r":\d+(?:-\d+)?$", "", raw).strip()
        if path and (quoted or path.lower() not in _NO_FILES):
            out.append(path)
    return out


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
