"""Bounded, ordered in-process work for resource preparation."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable, TypeVar


T = TypeVar("T")
R = TypeVar("R")


def checked_workers(workers: int) -> int:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError("资源处理线程数必须为 1-16 的整数")
    return workers


def bounded_map(function: Callable[[T], R], values: Iterable[T], workers: int = 4) -> list[R]:
    """Keep at most workers tasks active, join failures, return input order."""
    checked_workers(workers)
    iterator = iter(enumerate(values))
    results: dict[int, R] = {}
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="phigros-prepare")
    pending = {}

    def fill() -> None:
        while len(pending) < workers:
            try:
                index, value = next(iterator)
            except StopIteration:
                break
            pending[pool.submit(function, value)] = index

    try:
        fill()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            # Observe all failures before starting another batch.
            for future in sorted(completed, key=pending.__getitem__):
                results[pending.pop(future)] = future.result()
            fill()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return [results[index] for index in range(len(results))]
