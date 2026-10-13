from pathlib import Path


def run(value):
    # Deterministic stand-in for a responder; this fixture does not call an LLM.
    prompt = Path(__file__).with_name("prompt.txt").read_text(encoding="utf-8")
    if "Return only a bare integer." in prompt:
        return str(value)
    return {"answer": value}


def stable(value):
    return value + 10
