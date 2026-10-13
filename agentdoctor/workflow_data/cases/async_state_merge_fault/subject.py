from workflow import graph
async def run(state):
    return await graph.execute(state)


def stable(value):
    return value + 10
