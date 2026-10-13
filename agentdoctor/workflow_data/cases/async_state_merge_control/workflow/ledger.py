import asyncio
from .settings import UNIT_VALUE

async def hydrate(units):
    await asyncio.sleep(0)
    return {'balance': units * UNIT_VALUE}
