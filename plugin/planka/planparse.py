"""Детерминированный разбор плана: волны, задачи, владение файлами."""
import dataclasses
import re

# Заголовок ATX: уровень — число `#`; остаток строки разбирает _atx_text, закрывающие `#` отбрасываются.
_ATX = re.compile(r"^\s{0,3}(#{1,6})(.*)$")
# Подчёркивание setext: `===` — уровень 1, `---` — уровень 2.
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
# Уровень строки целиком жирным (`**Волна 1**`): глубже любого ATX.
_BOLD_LEVEL = 7
_WAVE = re.compile(r"^(?:Волна|Wave)\s+(\d+)(?!\d)", re.IGNORECASE)
# Волна строкой жирным: за номером — разделитель или конец.
_WAVE_BOLD = re.compile(r"^(?:Волна|Wave)\s+(\d+)\s*(?:[:.—–(-].*)?$", re.IGNORECASE)
_TASK = re.compile(r"^(?:Задача|Task)\s+(\d+(?:\.\d+)*)(?:\s*\(([^()]*)\))?\s*(?:[:.—–-]\s*(.*))?$",
                   re.IGNORECASE)
_FILES_HEAD = re.compile(r"^\**\s*(?:Файлы|Файл|Files|File)\s*\**\s*:\s*\**\s*(.*)$", re.IGNORECASE)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_ITEM_MARK = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_PREFIX = re.compile(r"^\**\s*(?:Create|Created|Modify|Modified|Test|Tests|Delete|Deleted|Update|Updated|Edit|"
                     r"Создать|Изменить|Тест|Тесты|Удалить|Обновить)\s*\**\s*:\s*\**\s*", re.IGNORECASE)
_NO_FILES = {"", "нет", "none", "empty", "—", "-"}
# Серия путей в обратных кавычках: между путями — пробелы, запятые и пометки в скобках.
_SEP = re.compile(r"[\s,;]*")
_NOTE = re.compile(r"\(([^()]*)\)")
_SPAN = re.compile(r"`([^`]+)`")
_DASH = re.compile(r"\s*[—–-]\s*")
# Пометка чтения: текст пометки начинается с неё, дальше конец, знак `,;:.` или тире через пробел;
# «чтение конфига», «read-write», «readme», «reference implementation» пометкой не считаются.
# Непонятная пометка («новый», «добавить X») путь владением оставляет.
_READ_NOTE = re.compile(
    r"[\s*_]*(?:только\s+(?:для\s+)?чтени[еяю]|только\s+читать|чтение|читать|"
    r"не\s+(?:трогать|менять|изменять|править)|без\s+изменени[йя]|не\s+изменя(?:ется|ются)|"
    r"только\s+импорт|только\s+для\s+справки|для\s+справки|справочно|справка|контекст|"
    r"read[\s-]?only|read(?:ing|\s+access)?|(?:no|without)\s+changes?|unchanged|"
    r"reference(?:\s+only)?|context(?:\s+only)?|imports?\s+only|import|"
    r"(?:do\s+not|don['’]t)\s+(?:modify|edit|touch|change))"
    r"[\s*_]*(?:$|[,;:.]|\s+[—–-])", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
# Пункт целиком из путей: `…` или слова с `.` или `/` через пробелы, запятые и пометки в скобках; `…` с
# пробелом путём не станет в _paths. _is_whole_path разбирает пункт за время, линейное по его длине.
_PATH_WORD = re.compile(r"[^`\s,;()]+")
_TAIL_NOTE = re.compile(r"\s*\([^()]*\)")
_SEP_RUN = re.compile(r"(?:[\s,;]+|\([^()]*\))+")
# Связка серии путей: «и»/«and»/«или»/«or»/`+`/`&` перед следующим путём в кавычках.
_CONN = re.compile(r"(?:и|and|или|or|[+&])\s*(?=`)", re.IGNORECASE)


@dataclasses.dataclass
class PlanTask:
    # None — задача до первого заголовка «Волна N»/«Wave N» или после заголовка, закрывшего волну.
    wave: int | None
    # Номер как в плане: «2», «1.1».
    number: str
    title: str
    files: list


def _path_word_end(text, pos):
    """Конец пути `…` или слова с `.`/`/`, начатого в pos; None, если пути там нет."""
    if span := _SPAN.match(text, pos):
        return span.end()
    word = _PATH_WORD.match(text, pos)
    if word and ("." in word.group() or "/" in word.group()):
        return word.end()
    return None


def _is_whole_path(item):
    """Пункт целиком из путей: пути через пробелы, запятые и пометки в скобках, в конце — одна пометка."""
    pos = _path_word_end(item, 0)
    if pos is None:
        return False
    while True:
        sep = _SEP_RUN.match(item, pos)
        nxt = _path_word_end(item, sep.end()) if sep else None
        if nxt is None:
            note = _TAIL_NOTE.match(item, pos)
            return all(c.isspace() or c in ".,;" for c in item[note.end() if note else pos:])
        pos = nxt


def _atx_text(rest):
    """Текст заголовка ATX: без пробелов по краям и закрывающих `#`, отделённых пробелом."""
    text = rest.strip()
    bare = text.rstrip("#")
    if bare != text and (not bare or bare[-1].isspace()):
        return bare.rstrip()
    return text


def _trailing_note(text):
    """(текст без пометки в скобках в конце, текст пометки) или (text, None)."""
    body = text.rstrip()
    start = body.rfind("(")
    if body.endswith(")") and start >= 0 and ")" not in body[start + 1:-1]:
        return body[:start], body[start + 1:-1]
    return text, None


def _lines_suffix_stripped(path):
    """Путь без суффикса строк `:10-20` и ведущих `./`."""
    path = re.sub(r":\d+(?:-\d+)?$", "", path).strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _split(fragment):
    """(пути, остаток) строки файлов; остаток — текст после серии путей в кавычках, иначе пустой.

    Пути строки файлов: ведущая серия `…` через пробелы, запятые и пометки в скобках, а без
    кавычек — токены без пробелов через запятую; описание, путь с пробелом и маркер
    «нет»/«none»/«empty»/«—» путём не считаются.

    Без префикса владения путь с пометкой чтения — в скобках сразу за ним или после тире в конце
    серии — не владение; пометка относится только к пути перед ней.
    """
    stripped = fragment.strip()
    check_read = not _PREFIX.match(stripped)
    text = _PREFIX.sub("", stripped)
    if "`" in text:
        # last — индекс в out пути, к которому относится следующая пометка.
        out, pos, last = [], 0, None
        while True:
            pos = _SEP.match(text, pos).end()
            if conn := _CONN.match(text, pos):
                pos = conn.end()
            if note := _NOTE.match(text, pos):
                if check_read and last is not None and _READ_NOTE.match(note.group(1)):
                    out[last] = ""
                last, pos = None, note.end()
            elif span := _SPAN.match(text, pos):
                last = None
                if not any(c.isspace() for c in span.group(1)):
                    out.append(_lines_suffix_stripped(span.group(1)))
                    last = len(out) - 1
                pos = span.end()
            else:
                break
        dash = _DASH.match(text, pos)
        if check_read and last is not None and dash and _READ_NOTE.match(text, dash.end()):
            out[last] = ""
        return [p for p in out if p], text[pos:]
    text, note = _trailing_note(text)
    read_last = check_read and note is not None and _READ_NOTE.match(note)
    text = text.strip().strip("*_").strip().rstrip(".;").strip()
    first = text.split()[0].lower().strip(".,;:*_") if text else ""
    if first in _NO_FILES:
        return [], ""
    parts = [p.strip() for p in text.split(",")]
    if not all(p and not any(c.isspace() for c in p) for p in parts):
        return [], ""
    if read_last:
        parts.pop()
    return [_lines_suffix_stripped(p) for p in parts], ""


def _paths(fragment):
    """Пути строки файлов; см. _split."""
    return _split(fragment)[0]


def _continues_description(tail):
    """Остаток после путей — законченное предложение, а не конец строки, не пояснение после тире и не
    незаконченный хвост («for tests»)."""
    tail = tail.strip()
    body = tail.rstrip(".!?;,").strip()
    return bool(body) and tail[-1] in ".!?" and not _DASH.match(body)


def _structure_lines(text):
    """Строки плана с вырезанными HTML-комментариями `<!-- -->`; строка fenced-блока — None.

    Содержимое fenced-блока — пример, а не структура плана; блок закрывает строка из тех же
    символов не короче открывающей и без info-строки.
    """
    fence = None
    in_comment = False
    for line in text.splitlines():
        if in_comment:
            end = line.find("-->")
            if end < 0:
                yield ""
                continue
            line = line[end + 3:]
            in_comment = False
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            yield None
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = None
            yield None
            continue
        while (start := line.find("<!--")) >= 0:
            end = line.find("-->", start + 4)
            if end < 0:
                line = line[:start]
                in_comment = True
                break
            line = line[:start] + line[end + 3:]
        yield line


def _unbold(text):
    return text.replace("**", "").replace("__", "").strip()


def _heading(lines, i):
    """Заголовок, начатый строкой i: (уровень, текст, число строк) или None.

    ATX — любой; setext с `===` — любой, с `---` — только волна или задача (иначе это
    горизонтальная черта); строка жирным — только волна или задача.
    """
    line = lines[i]
    m = _ATX.match(line)
    if m:
        return len(m.group(1)), _unbold(_atx_text(m.group(2))), 1
    stripped = line.strip()
    if not stripped or _ITEM.match(line):
        return None
    nxt = lines[i + 1] if i + 1 < len(lines) else None
    u = _SETEXT.match(nxt) if nxt else None
    text = _unbold(stripped)
    if u and (u.group(1)[0] == "=" or _WAVE.match(text) or _TASK.match(text)):
        return (1 if u.group(1)[0] == "=" else 2), text, 2
    if stripped.startswith(("**", "__")) and (_WAVE_BOLD.match(text) or _TASK.match(text)):
        return _BOLD_LEVEL, text, 1
    return None


def parse_plan(text):
    """Задачи плана с файлами; None, если ни у одной задачи нет файлов.

    Любой заголовок закрывает текущую задачу; заголовок не волны и не задачи уровнем не глубже
    заголовка волны закрывает волну.
    """
    tasks = []
    wave, wave_level = None, None
    current = None
    collecting, blank, head_seen = False, False, False
    lines = list(_structure_lines(text.removeprefix("\ufeff")))
    i = 0
    while i < len(lines):
        line = lines[i]
        if line is None:
            collecting = False
            i += 1
            continue
        heading = _heading(lines, i)
        if heading:
            level, title, size = heading
            i += size
            current, collecting, head_seen = None, False, False
            if m := _WAVE.match(title):
                wave, wave_level = int(m.group(1)), level
            elif m := _TASK.match(title):
                current = PlanTask(wave=wave, number=m.group(1),
                                   title=(m.group(3) or m.group(2) or "").strip(), files=[])
                tasks.append(current)
            elif wave_level is not None and level <= wave_level:
                wave, wave_level = None, None
            continue
        if collecting:
            if not line.strip():
                blank = True
                i += 1
                continue
            item = _ITEM.match(line)
            # Пункт без префикса — владение, только если он целиком из путей; иной пункт сразу под списком
            # пропускается, после пустой строки — заканчивает список.
            owned = item and (_PREFIX.match(item.group(1).strip()) or _is_whole_path(item.group(1).strip()))
            if owned or item and not blank:
                if owned:
                    current.files.extend(_paths(item.group(1)))
                blank = False
                i += 1
                continue
            collecting = False
        # Строка файлов — отдельной строкой или пунктом списка (`- **Файлы:** …`).
        m = _FILES_HEAD.match(_ITEM_MARK.sub("", line.strip(), 1)) if current is not None else None
        if m:
            paths, tail = _split(m.group(1))
            # Повторная строка файлов, за путями которой идёт предложение, — описание задачи.
            if not (head_seen and _continues_description(tail)):
                current.files.extend(paths)
                collecting, blank = True, False
            head_seen = True
        i += 1
    tasks = [t for t in tasks if t.files]
    return tasks or None


def shared_files(tasks):
    """Файлы, принадлежащие задачам с разными номерами одной волны; номера — без повторов, в порядке плана.
    Задачи вне волны не участвуют; задача, повторённая с тем же номером, — одна задача."""
    owners = {}
    for t in tasks:
        if t.wave is None:
            continue
        for f in t.files:
            owners.setdefault((t.wave, f), {})[t.number] = None
    return [(f, wave, list(nums)) for (wave, f), nums in sorted(owners.items()) if len(nums) > 1]


def _join(nums):
    nums = [str(n) for n in nums]
    return nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " и " + nums[-1]


def format_conflicts(conflicts):
    return "\n".join(f"файл {f} принадлежит задачам {_join(nums)} волны {wave}"
                     for f, wave, nums in conflicts)
