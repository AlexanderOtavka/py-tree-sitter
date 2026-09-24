"""Rewrite source text at captured nodes, without corrupting the offsets.

A query gives you :class:`~tree_sitter.Node` objects; the obvious next thing to
want is to *edit* the source at those nodes. Doing it by hand walks straight into
three traps, every one of which corrupts the output silently rather than raising:

1. **Forward iteration invalidates every later offset.** Splicing match 1 changes
   the length of the text, so match 2's ``start_byte`` no longer points where it
   did when the query ran. A naive ``for m in matches: out = out[:s] + new + out[e:]``
   loop drifts by the accumulated length delta and lands mid-token.
2. **Node offsets are BYTE offsets, not character indices.** ``start_byte`` /
   ``end_byte`` count UTF-8 bytes. Slicing a :class:`str` with them works right up
   until the source contains a non-ASCII character, after which every subsequent
   slice is short by the number of continuation bytes before it -- so an edit
   quietly eats the neighbouring character instead of raising.
3. **Overlapping edits.** Nothing stops you editing a node *and* a node nested
   inside it -- a captured ``string_lit`` and the ``"`` token that starts it share
   a start offset. Applied blindly, one edit clobbers part of the other.

:class:`Edits` fixes all three by construction:

* Edits are **batched**: every call just records a byte range and a replacement,
  measured against the original source. Nothing moves until :meth:`Edits.apply`,
  so the offsets you queue are the offsets the query gave you, no matter what
  order you queue them in. That is the entire point -- callers may iterate matches
  in plain source order, the natural thing to do.
* :meth:`Edits.apply` applies in **descending start order**, so each splice only
  disturbs text that has already been dealt with. Trap 1 cannot happen.
* The source is converted to ``bytes`` exactly once, in the constructor, and back
  exactly once, in :meth:`Edits.apply`. All splicing happens in the byte domain
  that ``start_byte`` / ``end_byte`` are expressed in. Trap 2 cannot happen.
* :meth:`Edits.apply` refuses conflicting edits with
  :class:`OverlappingEditError` instead of producing quietly wrong text.
* Every queued node is checked against the source it is supposed to describe, by
  comparing the node's own text to the bytes at its offsets. A node parsed from a
  *different* source is the same class of bug -- a range check alone misses it
  whenever the other source happens to be long enough -- so it raises rather than
  splicing at a meaningless position.

This module deliberately knows nothing about template queries. It takes plain
:class:`~tree_sitter.Node` objects, so it works with a hand-written
S-expression :class:`~tree_sitter.Query`, a manual tree walk, a template query --
anything that can hand you a node.

.. code-block:: python

    from tree_sitter import Language, Parser, Query, QueryCursor
    from tree_sitter.template import Edits

    edits = Edits(source)
    for node in QueryCursor(Query(PY, "(function_definition name: (identifier) @n)")).captures(
        tree.root_node
    )["n"]:
        edits.replace(node, node.text.decode().upper())
    new_source = edits.apply()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tree_sitter import Node

__all__ = ["Edits", "OverlappingEditError"]

# Node byte offsets are UTF-8 offsets by definition, so this is not configurable:
# any other codec would make ``start_byte`` mean something different from what
# tree-sitter measured, which is trap 2 wearing a disguise.
_ENCODING = "utf-8"

_REPLACE = "replace"
_INSERT = "insert"


class OverlappingEditError(ValueError):
    """Two queued edits cannot both be applied.

    Raised by :meth:`Edits.apply`. The message names both byte ranges and both
    replacement texts, because the conflict is almost always a node and a node
    nested inside it, and the ranges are what tell them apart.

    Subclasses :class:`ValueError` so that callers who only care that the batch
    was rejected do not have to import this name.
    """


@dataclass(frozen=True, slots=True)
class _Edit:
    """One queued edit: replace ``[start, end)`` of the original source with ``text``.

    An insertion is simply an edit with ``start == end``. ``seq`` is the position
    in the queue, used to break ties deterministically.
    """

    start: int
    end: int
    text: bytes
    kind: str
    seq: int

    @property
    def is_insert(self) -> bool:
        return self.kind == _INSERT

    def describe(self) -> str:
        preview = self.text.decode(_ENCODING, "replace")
        what = "insert" if self.is_insert else "replace"
        where = f"at {self.start}" if self.is_insert else f"[{self.start}, {self.end})"
        return f"{what} {where} with {preview!r}"


class Edits:
    """A batch of source rewrites, queued against node offsets and applied at once.

    Every mutating method returns ``self``, so calls chain. Nothing is applied
    until :meth:`apply`, which is non-destructive: it builds a fresh result from
    the original source each time, leaving this object usable afterwards.

    Parameters
    ----------
    source : str | bytes
        The exact source the nodes were parsed from. :meth:`apply` returns the
        same type: ``str`` in, ``str`` out; ``bytes`` in, ``bytes`` out.

    Examples
    --------
    >>> Edits('a = "x"').replace(node, '"y"').apply()
    'a = "y"'
    """

    __slots__ = ("_edits", "_source", "_was_str")

    def __init__(self, source: str | bytes) -> None:
        self._was_str = isinstance(source, str)
        # Trap 2: the one and only encode. Everything downstream is bytes.
        self._source = source.encode(_ENCODING) if isinstance(source, str) else bytes(source)
        self._edits: list[_Edit] = []

    # -- queueing ---------------------------------------------------------

    def replace(self, node: Node, text: str) -> Edits:
        """Queue replacing ``node``'s source text with ``text``."""
        self._check_node(node)
        return self._queue(node.start_byte, node.end_byte, text, _REPLACE)

    def replace_all(self, nodes: Iterable[Node], text: str) -> Edits:
        """Queue the same replacement for several nodes.

        A quantified capture matches several nodes, and indexing a match yields
        only the first of them -- pass ``match.all(name)`` here to edit them all.
        """
        for node in nodes:
            self.replace(node, text)
        return self

    def insert_before(self, node: Node, text: str) -> Edits:
        """Queue inserting ``text`` immediately before ``node``.

        Zero-width at ``node.start_byte``: it does not replace anything, so it
        never conflicts with an edit to a different node.
        """
        self._check_node(node)
        return self._queue(node.start_byte, node.start_byte, text, _INSERT)

    def insert_after(self, node: Node, text: str) -> Edits:
        """Queue inserting ``text`` immediately after ``node``.

        Zero-width at ``node.end_byte``.
        """
        self._check_node(node)
        return self._queue(node.end_byte, node.end_byte, text, _INSERT)

    def delete(self, node: Node) -> Edits:
        """Queue removing ``node``'s source text. Equivalent to replacing it with ``""``."""
        return self.replace(node, "")

    def _check_node(self, node: Node) -> None:
        """Verify *node* really describes this source.

        The byte-range check in :meth:`_queue` only catches offsets past the end,
        so a node parsed from a *different* source of similar length would splice
        at meaningless positions and corrupt silently -- precisely the failure this
        class exists to prevent. Comparing the node's own text against the source
        at those offsets closes that hole for one bytes comparison per edit.
        """
        start, end = node.start_byte, node.end_byte
        if 0 <= start <= end <= len(self._source) and self._source[start:end] == node.text:
            return
        found = (
            self._source[start:end] if 0 <= start <= end <= len(self._source) else b"<out of range>"
        )
        raise ValueError(
            f"node {node.type!r} spans [{start}, {end}) where this source has "
            f"{found!r}, not the node's own text {node.text!r} -- "
            f"was it parsed from a different source?"
        )

    def _queue(self, start: int, end: int, text: str, kind: str) -> Edits:
        if start < 0 or end > len(self._source):
            # Cheap guard against nodes parsed from a *different* source, which
            # would otherwise splice at meaningless offsets and corrupt silently
            # in exactly the same way the traps above do.
            raise ValueError(
                f"byte range [{start}, {end}) is outside the source "
                f"(0 to {len(self._source)}) -- were these nodes parsed from this source?"
            )
        self._edits.append(_Edit(start, end, text.encode(_ENCODING), kind, len(self._edits)))
        return self

    # -- applying ---------------------------------------------------------

    def apply(self) -> str | bytes:
        """Apply every queued edit and return the rewritten source.

        Non-destructive: the queue is untouched, so calling this twice returns the
        same result and the object stays usable.

        Returns
        -------
        str | bytes
            ``str`` if this :class:`Edits` was built from a ``str``, else ``bytes``.

        Raises
        ------
        OverlappingEditError
            If two queued edits conflict. See :meth:`Edits.apply` notes below.

        Notes
        -----
        Edits are applied in descending ``start`` order, so every splice happens
        to the right of the text still to be edited and the queued offsets stay
        valid against the original source (trap 1).

        Ties at the same ``start`` are broken by descending ``end``, then by
        descending queue position. Two consequences worth knowing:

        * A replacement is applied before a zero-width insertion at the same
          offset, which puts an ``insert_before`` outside the replacement text --
          the intuitive reading of "before this node".
        * Two insertions at the *same* offset are both applied, and land in
          **queue order**: the first one queued appears first in the output. The
          caller controls the sequence simply by the order of the calls.
        """
        ordered = self._ordered()
        self._check_overlaps(ordered)
        out = bytearray(self._source)
        # Descending, so each splice only moves text that is already final.
        for edit in reversed(ordered):
            out[edit.start : edit.end] = edit.text
        result = bytes(out)
        # Trap 2: the one and only decode.
        return result.decode(_ENCODING) if self._was_str else result

    def _ordered(self) -> list[_Edit]:
        """Return the edits in ascending application order.

        :meth:`apply` walks this list backwards. Sorting ascending here (rather
        than descending) is what makes the overlap sweep readable.
        """
        return sorted(self._edits, key=lambda e: (e.start, e.end, e.seq))

    @staticmethod
    def _check_overlaps(ordered: list[_Edit]) -> None:
        """Raise if any two edits in ``ordered`` (ascending) cannot both be applied.

        Tracks the furthest-reaching replacement seen so far rather than just the
        previous edit, so a wide replacement is still compared against everything
        it encloses even when zero-width insertions sit between them.
        """
        widest: _Edit | None = None
        for edit in ordered:
            if widest is not None:
                if edit.is_insert:
                    # An insertion anchors to a position, not to text. Strictly
                    # inside a replaced range that position is destroyed, so the
                    # insertion has nothing to anchor to -- refuse. At a boundary
                    # the position survives, so allow it.
                    conflicts = widest.start < edit.start < widest.end
                else:
                    # Touching ranges (end == start) are legitimate neighbours,
                    # e.g. a `pair` node and the `,` token right after it; only a
                    # strict overlap is a conflict. Two replacements of the same
                    # node overlap themselves, and so are caught here too.
                    conflicts = widest.end > edit.start
                if conflicts:
                    raise OverlappingEditError(
                        f"cannot apply both edits: {widest.describe()} overlaps {edit.describe()}"
                    )
            if not edit.is_insert and (widest is None or edit.end > widest.end):
                widest = edit

    # -- introspection ----------------------------------------------------

    def __len__(self) -> int:
        """Number of queued edits."""
        return len(self._edits)

    def __bool__(self) -> bool:
        """``True`` if any edit is queued."""
        return bool(self._edits)

    def __repr__(self) -> str:
        kind = "str" if self._was_str else "bytes"
        return f"<Edits {len(self._edits)} queued, {len(self._source)} {kind} bytes>"
