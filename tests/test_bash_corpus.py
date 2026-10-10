import json
import pathlib
import sys
import unittest

PLANKA_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "planka"
sys.path.insert(0, str(PLANKA_DIR))
import depcheck  # noqa: E402

# Корпус команд Bash из транскриптов Claude Code автора (tests/tools/build_corpus.py): обезличен, без секретов;
# add и doubt — ожидаемые вердикты depcheck; у строки 2409 ожидается doubt (решение автора о ложном отказе — README,
# «Известные ограничения»). Команды только читаются как строки, не исполняются.
CORPUS = pathlib.Path(__file__).parent / "fixtures" / "bash-transcripts.jsonl"


def _plain(value):
    """Вердикт в виде, в каком он лежит в JSON (кортеж становится списком)."""
    return json.loads(json.dumps(value, ensure_ascii=False))


class BashCorpusTest(unittest.TestCase):
    def test_verdicts_match_recorded(self):
        with open(CORPUS, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(rows)
        for number, row in enumerate(rows, 1):
            command = row["command"]
            add = depcheck.dependency_add(command)
            doubt = None if add else depcheck.dependency_doubt(command)
            got = {"add": _plain(add), "doubt": _plain(doubt)}
            want = {"add": row["add"], "doubt": row["doubt"]}
            if got != want:
                with self.subTest(line=number, command=command):
                    self.fail("записано %r, получено %r" % (want, got))


if __name__ == "__main__":
    unittest.main()
