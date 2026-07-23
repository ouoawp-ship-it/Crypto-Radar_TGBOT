from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from ..models import NormalizedTransfer


class TransferCollector(ABC):
    @abstractmethod
    def collect(self) -> Iterable[NormalizedTransfer]:
        """Yield normalized transfers without coupling domain logic to transport."""


@dataclass(frozen=True)
class BlockRange:
    start_block: int
    end_block: int


class EvmLogBackfillCollector(ABC):
    """P3.0 transport contract for later HTTP ``eth_getLogs`` adapters."""

    def block_ranges(
        self, start_block: int, end_block: int, *, batch_size: int
    ) -> Iterator[BlockRange]:
        if start_block < 0 or end_block < start_block or batch_size <= 0:
            raise ValueError("invalid block range")
        cursor = start_block
        while cursor <= end_block:
            batch_end = min(end_block, cursor + batch_size - 1)
            yield BlockRange(cursor, batch_end)
            cursor = batch_end + 1

    @abstractmethod
    def fetch_logs(self, block_range: BlockRange) -> Iterable[dict[str, object]]:
        """Fetch a bounded block range. P3.1 supplies the network adapter."""
