"""Изоляция процесса тестов от git-настроек и окружения машины."""
import os
import tempfile

# Все GIT_* машины сняты: GIT_DIR и GIT_WORK_TREE подменяют репозиторий, GIT_CONFIG_PARAMETERS (его экспортирует
# git -c, например при rebase -x) и GIT_CONFIG_COUNT/KEY_<n>/VALUE_<n> добавляют настройки поверх любого конфига.
for _name in [n for n in os.environ if n.startswith("GIT_")] + ["CLAUDE_PROJECT_DIR"]:
    os.environ.pop(_name, None)
# Глобальный и системный конфиг и системные атрибуты git не читаются: gpgsign, excludesFile и алиасы не влияют
# на снимок и коммиты.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_ATTR_NOSYSTEM"] = "1"
# ignore и attributes из XDG_CONFIG_HOME/git (без неё — из ~/.config/git) git читает и при GIT_CONFIG_GLOBAL:
# пустой временный каталог, удаляется при выходе процесса.
_xdg = tempfile.TemporaryDirectory(prefix="planka-xdg-")
os.environ["XDG_CONFIG_HOME"] = _xdg.name
# Поиск репозитория вверх по дереву останавливается перед tempdir: внешний репозиторий не находится.
os.environ["GIT_CEILING_DIRECTORIES"] = tempfile.gettempdir()
