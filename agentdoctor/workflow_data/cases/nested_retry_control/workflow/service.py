import asyncio

calls = 0

def reset():
    global calls
    calls = 0

async def status(ready_after):
    global calls
    await asyncio.sleep(0)
    calls += 1
    return {'done': calls >= ready_after}
