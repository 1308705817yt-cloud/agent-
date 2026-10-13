import asyncio
from .settings import MULTIPLIER

async def fetch(item):
    await asyncio.sleep((item % 3) * 0.001)
    return {'item': item, 'value': item * MULTIPLIER}
