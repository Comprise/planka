"""Изоляция процесса тестов от git-настроек и окружения машины."""
import os
import tempfile

# Глобальный и системный конфиг git не читаются: gpgsign, excludesFile и алиасы не влияют на снимок и коммиты.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
for _name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
              "CLAUDE_PROJECT_DIR"):
    os.environ.pop(_name, None)
# Поиск репозитория вверх по дереву останавливается перед tempdir: внешний репозиторий не находится.
os.environ["GIT_CEILING_DIRECTORIES"] = tempfile.gettempdir()
