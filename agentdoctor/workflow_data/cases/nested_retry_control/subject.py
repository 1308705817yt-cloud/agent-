from workflow import graph
async def run(ready_after):
    return await graph.execute(ready_after)


def stable(value):
    return value + 10
