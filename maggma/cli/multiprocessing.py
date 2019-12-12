#!/usr/bin/env python
# coding utf-8

import asyncio
from asyncio import BoundedSemaphore
from aioitertools import zip_longest
from concurrent.futures import ProcessPoolExecutor
from maggma.utils import tqdm


class AsyncBackPressuredMap:
    """
    Wrapper for an iterator to provide
    async access with backpressure
    """

    def __init__(self, iterator, builder, executor):
        self.iterator = iter(iterator)
        self.process = builder.process_item
        self.executor = executor
        self.back_pressure = BoundedSemaphore(builder.chunk_size)

    async def __aiter__(self):
        return self

    async def __anext__(self):
        await self.back_pressure.acquire()
        loop = asyncio.get_running_loop()

        try:
            item = next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration

        async def process_and_release():
            future = loop.run_in_executor(self.executor, self.process, item)
            await future
            self.back_pressure.release()
            return future

        return process_and_release()


async def grouper(iterable, n, fillvalue=None):
    """
    Collect data into fixed-length chunks or blocks.
    """
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx
    args = [iterable] * n
    iterator = zip_longest(*args, fillvalue=fillvalue)

    async for group in iterator:
        group = [g for g in group if g is not None]
        yield group


async def multi(builder, num_workers):
    builder.connect()
    cursor = builder.get_items()
    executor = ProcessPoolExecutor(num_workers)
    mapper = AsyncBackPressuredMap(tqdm(cursor, desc="Get"), builder, executor)

    async for chunk in grouper(mapper, builder.chunk_size, fillvalue=None):
        chunk = await asyncio.gather(*chunk)
        processed_items = [c.result() for c in chunk if chunk is not None]
        builder.update_targets(processed_items)