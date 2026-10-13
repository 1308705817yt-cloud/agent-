def run(state):
    update = {"completed": True}
    return {**state, **update}


def stable(value):
    return value + 10
