"""SessionStart и PostModelSwitch: модель сессии в state/<session>.model.json — для судьи (common.judge_model).

Документированные источники модели сессии: поле model входа SessionStart (Claude Code опускает его, например
после /clear и при восстановлении сессии) и to_model входа PostModelSwitch (любая смена модели, в том числе
восстановление модели при возобновлении сессии). Агенту хук ничего не выводит: stdout PostModelSwitch и
SessionStart Claude Code добавляет в контекст агента.
"""
import common

EVENT_FIELDS = {"SessionStart": "model", "PostModelSwitch": "to_model"}


def store(session_id, model):
    """Записывает model как модель сессии session_id; прежнее значение заменяется."""
    state_dir = common.data_dir() / "state"
    state_dir.mkdir(exist_ok=True)
    with common.state_lock(state_dir):
        common.atomic_write_json(common.session_model_path(state_dir, session_id), {"model": model})
    common.prune_state(state_dir)


def main():
    if common.barrier_active():
        return
    data = common.read_input()
    if data is None:
        return
    event = data.get("hook_event_name")
    field = EVENT_FIELDS.get(event)
    if field is None:
        return
    model = data.get(field)
    if not common.usable_model(model):
        # SessionStart без model — обычный случай: известная модель сессии остаётся.
        if event == "PostModelSwitch":
            common.warn("PostModelSwitch без to_model, модель сессии не обновлена")
        return
    session_id = data.get("session_id")
    try:
        store(session_id if isinstance(session_id, str) else "", model)
    except OSError as e:
        common.warn(f"модель сессии не записана: {e}")


if __name__ == "__main__":
    common.run_hook(main)
