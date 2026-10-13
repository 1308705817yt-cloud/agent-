def probe():
    return {"done": True}


def run(max_steps):
    attempts = 0
    done = False
    while attempts < max_steps:
        result = probe()
        attempts += 1
        done = result["done"]
    return {"attempts": attempts, "done": done}


def stable(value):
    return value + 10
