"""Pure ball->block assignment (US-B3), shared by the API and the Epic D events pipeline."""

from collections.abc import Sequence

#: ``(block_no, start_s, end_s)`` — ``end_s is None`` means the block is still open.
BlockSpan = tuple[int, float, float | None]


def assign_block(blocks: Sequence[BlockSpan], t_s: float) -> int | None:
    """Return the ``block_no`` whose span contains ``t_s``, or ``None`` if uncovered.

    Spans are half-open ``[start_s, end_s)``; an open block (``end_s is None``)
    covers ``[start_s, inf)``. Boundary rule: a ball exactly on a boundary
    belongs to the LATER block — ``t_s == start_s`` matches that block, while
    ``t_s == end_s`` does NOT match the earlier block (it falls into the next
    block if one starts there, otherwise into a gap).
    """
    for block_no, start_s, end_s in blocks:
        if t_s >= start_s and (end_s is None or t_s < end_s):
            return block_no
    return None
