from workflow import graph
async def run(items):
    return await graph.execute(items)


def stable(value):
    return value + 10
