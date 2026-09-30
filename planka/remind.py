"""UserPromptSubmit: подмешивает philosophy.md целиком в контекст на каждую реплику автора."""
import common


def main():
    if common.barrier_active():
        return
    if common.read_input() is None:
        return
    text = common.philosophy_text()
    if text is None:
        return
    common.emit(common.context_output(text))


if __name__ == "__main__":
    common.run_hook(main)
