"""Step 1 of the template pipeline: render a PEP 750 template into parseable text.

A query template is written in the *target language's own syntax*, with the
interesting positions marked by interpolations::

    t'''
      resource "aws_s3_bucket" {capture("name")} {{
        acl = "private"
        {...}
      }}
    '''

Rendering turns every hole into a **sentinel identifier** (``TSQH0``, ``TSQH1``,
…) so that the target grammar can parse the result. A bare identifier is not
always valid where a hole sits — an HCL block body wants ``TSQH0 = 0``, a JSON
object member wants ``"TSQH0": 0`` — so the renderer *searches* a small list of
padding candidates per hole and keeps the first combination that parses cleanly.
Nothing about this is hardcoded per language: the grammar decides.

The output is the rendered text plus one :class:`~._holes.HoleSlot` per hole,
whose ``start``/``end`` give the **byte** span of the whole rendered hole,
padding included. The compiler uses that span to find the hole's node.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import tree_sitter

from ._errors import TemplateSyntaxError
from ._holes import AnyChildren, Hole, HoleSlot

if TYPE_CHECKING:
    from string.templatelib import Template

    from tree_sitter import Language, Node, Parser

__all__ = [
    "PAD_CANDIDATES",
    "SENTINEL_PREFIX",
    "RenderResult",
    "render",
]

#: Sentinel ``i`` is ``f"{SENTINEL_PREFIX}{i}"``. Chosen to be a valid
#: identifier in every grammar we care about and vanishingly unlikely to occur
#: in a real template.
SENTINEL_PREFIX = "TSQH"

#: ``(pad_prefix, pad_suffix)`` pairs tried around each sentinel, in order. The
#: first combination that makes the *whole* template parse cleanly wins.
PAD_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("", ""),  # TSQH0
    ('"', '"'),  # "TSQH0"          string position
    ("", " = 0"),  # TSQH0 = 0        HCL/attribute body
    ('"', '": 0'),  # "TSQH0": 0       JSON object member
    ("", "()"),  # TSQH0()          call/expression-statement position
    ("", ";"),  # TSQH0;           statement position, semicolon languages
    ("", ","),  # TSQH0,           list/argument position
)

#: How many times the greedy sweep over holes is repeated before giving up.
#: Padding choices interact (hole 0's padding can decide whether hole 1 parses),
#: so one extra pass lets earlier holes react to later ones.
_MAX_PASSES = 3


@dataclass
class RenderResult:
    """The rendered template: text for the parser, plus where the holes are."""

    #: Rendered template text, dedented, parseable by the target grammar.
    text: str
    #: One slot per *hole*, in template order. ``str`` interpolations splice
    #: literally and produce no slot, so this is not one-per-interpolation.
    slots: list[HoleSlot] = field(default_factory=list)

    @property
    def source(self) -> bytes:
        """``text`` encoded as UTF-8 — the exact bytes the slots index into."""
        return self.text.encode("utf-8")


def render(
    template: Template,
    language: Language,
    *,
    parser: Parser | None = None,
) -> RenderResult:
    """Render a PEP 750 ``Template`` into parseable text plus hole slots.

    Parameters
    ----------
    template : string.templatelib.Template
        The ``t"..."`` template. Literal parts are used as-is; each
        interpolation is coerced into a :class:`~._holes.Hole` (or spliced
        literally, if its value is a ``str``).
    language : tree_sitter.Language
        Grammar used to decide whether a candidate rendering parses.
    parser : tree_sitter.Parser, optional
        Prebuilt parser for ``language``, to avoid rebuilding one per call.

    Returns
    -------
    RenderResult
        ``text`` plus a :class:`~._holes.HoleSlot` per hole with byte spans set.

    Raises
    ------
    TypeError
        An interpolation's value is neither a ``Hole``, ``Ellipsis``, nor ``str``.
    TemplateSyntaxError
        No combination of sentinel paddings parses without errors.
    """
    literals, slots = _split(template)
    literals = _dedent(literals, slots)

    if parser is None:
        parser = tree_sitter.Parser(language)

    candidates = _search(literals, slots, parser)
    text, spans = _assemble(literals, slots, candidates)

    for slot, (start, end), cand in zip(slots, spans, candidates, strict=True):
        slot.pad_prefix, slot.pad_suffix = PAD_CANDIDATES[cand]
        slot.start, slot.end = start, end

    return RenderResult(text=text, slots=slots)


def _split(template: Template) -> tuple[list[str], list[HoleSlot]]:
    """Split a template into literal chunks and hole slots.

    Returns ``len(slots) + 1`` literal chunks: ``literals[i]`` precedes hole
    ``i`` and ``literals[-1]`` trails the last hole. ``str`` interpolations are
    appended to the surrounding literal chunk rather than becoming holes.
    """
    literals: list[str] = [""]
    slots: list[HoleSlot] = []

    for part in template:
        if isinstance(part, str):
            literals[-1] += part
            continue

        value: Any = part.value
        expression: str = getattr(part, "expression", "") or ""

        if isinstance(value, str):
            # Not a hole: splice the text straight into the template. Lets a
            # caller parameterize e.g. a resource type without a capture.
            literals[-1] += value
            continue

        hole = _coerce(value, expression)
        index = len(slots)
        slots.append(
            HoleSlot(
                index=index,
                hole=hole,
                sentinel=f"{SENTINEL_PREFIX}{index}",
                expression=expression,
            )
        )
        literals.append("")

    return literals, slots


def _coerce(value: Any, expression: str) -> Hole:
    """Coerce one interpolated value into a :class:`~._holes.Hole`."""
    if isinstance(value, Hole):
        return value
    if value is Ellipsis:
        return AnyChildren()
    raise TypeError(
        f"cannot interpolate {type(value).__name__} into a query template: "
        f"{{{expression}}} — expected a Hole (capture(...), anything(...), "
        f"Alternatives(...)), `...`, or a str to splice literally"
    )


def _dedent(literals: list[str], slots: list[HoleSlot]) -> list[str]:
    """Dedent the *assembled* text, then hand the literal chunks back.

    Indentation has to be measured on the whole template at once, otherwise
    chunks that start mid-line get a different common prefix than chunks that
    start a line. So: assemble with bare sentinels, dedent, strip leading and
    trailing blank lines, then cut the result back apart at the sentinels.
    Padding never contains a newline, so this dedent is valid for every
    candidate rendering.
    """
    probe = literals[0]
    for slot, literal in zip(slots, literals[1:], strict=True):
        probe += slot.sentinel + literal

    dedented = _strip_blank_lines(textwrap.dedent(probe))

    out: list[str] = []
    pos = 0
    for slot in slots:
        # Sentinels appear in index order, so a plain forward search cannot
        # mistake TSQH1 for the prefix of a later TSQH10.
        found = dedented.find(slot.sentinel, pos)
        if found < 0:  # pragma: no cover - only if a literal ate a sentinel
            raise TemplateSyntaxError(
                f"internal error: sentinel {slot.sentinel!r} vanished while "
                f"dedenting template:\n{dedented}"
            )
        out.append(dedented[pos:found])
        pos = found + len(slot.sentinel)
    out.append(dedented[pos:])
    return out


def _strip_blank_lines(text: str) -> str:
    """Drop leading and trailing lines that are empty or all whitespace."""
    lines = text.split("\n")
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _assemble(
    literals: list[str],
    slots: list[HoleSlot],
    candidates: list[int],
) -> tuple[str, list[tuple[int, int]]]:
    """Build the rendered text and the byte span of each rendered hole."""
    chunks: list[str] = [literals[0]]
    offset = len(literals[0].encode("utf-8"))
    spans: list[tuple[int, int]] = []

    for slot, literal, cand in zip(slots, literals[1:], candidates, strict=True):
        prefix, suffix = PAD_CANDIDATES[cand]
        rendered = prefix + slot.sentinel + suffix
        size = len(rendered.encode("utf-8"))
        spans.append((offset, offset + size))
        chunks.append(rendered)
        chunks.append(literal)
        offset += size + len(literal.encode("utf-8"))

    return "".join(chunks), spans


#: Quote characters that, when they already surround a hole in the template,
#: make the quote-adding padding candidates wrong.
_QUOTES = ("\"", "'", "`")


def _allowed_candidates(literals: list[str], index: int) -> list[int]:
    """Return the padding candidates worth trying for hole *index*.

    A hole the user already wrote inside quotes -- ``"{capture('name')}"`` --
    must not be given quote padding on top, or the template renders as
    ``""TSQH0""``. That still *parses* in HCL (an empty string followed by an
    identifier), so the search would happily accept it and then compile a
    pattern expecting children that do not exist. Excluding the candidate is
    more robust than trying to detect the damage afterwards.
    """
    before = literals[index]
    after = literals[index + 1] if index + 1 < len(literals) else ""
    quoted = before.endswith(_QUOTES) and after.startswith(_QUOTES)

    candidates = list(range(len(PAD_CANDIDATES)))
    if quoted:
        # Inside quotes the sentinel is already in a valid position, so the only
        # sensible candidate is the bare one. Anything else -- extra quotes, or
        # trailing syntax like ` = 0` -- lands *inside* the string literal, where
        # it becomes part of the text rather than structure.
        candidates = [c for c in candidates if PAD_CANDIDATES[c] == ("", "")]
    return candidates


def _search(literals: list[str], slots: list[HoleSlot], parser: Parser) -> list[int]:
    """Find a padding candidate per hole that makes the whole template parse.

    Greedy and sequential: hole ``i`` is decided while every other hole is held
    at its current best guess, then we move on. The sweep is repeated (up to
    :data:`_MAX_PASSES`) because an early hole's choice can be invalidated by a
    later one.
    """
    candidates = [0] * len(slots)
    allowed = [_allowed_candidates(literals, i) for i in range(len(slots))]

    def score(cands: list[int]) -> tuple[tuple[int, int], str]:
        text, _ = _assemble(literals, slots, cands)
        return _error_score(parser, text), text

    best, text = score(candidates)
    if best == _CLEAN:
        return candidates

    for _ in range(_MAX_PASSES):
        for i in range(len(slots)):
            original = candidates[i]
            local_best, local_cand = best, original
            for cand in allowed[i]:
                if cand == original:
                    continue
                candidates[i] = cand
                current, _ = score(candidates)
                if current == _CLEAN:
                    return candidates
                if current < local_best:
                    local_best, local_cand = current, cand
            candidates[i] = local_cand
            best = local_best
        final, text = score(candidates)
        if final == _CLEAN:
            return candidates
        best = final

    raise TemplateSyntaxError(_syntax_message(parser, text))


#: The score of a cleanly parsing text; see :func:`_error_score`.
_CLEAN = (0, 0)


def _error_score(parser: Parser, text: str) -> tuple[int, int]:
    """Rank how badly ``text`` fails to parse; :data:`_CLEAN` means it doesn't.

    A clean parse has no ``ERROR`` node, no missing node, and a root that does
    not report ``has_error``. Otherwise the score is
    ``(bytes covered by bad nodes, number of bad nodes)``. Byte extent comes
    first because a single ``ERROR`` node can swallow several holes at once: the
    node *count* then stays at 1 no matter how much a candidate helps, which
    leaves the greedy search with no gradient to follow, while the extent
    shrinks as each hole is fixed.
    """
    tree = parser.parse(text.encode("utf-8"))
    root = tree.root_node
    count = 0
    extent = 0
    for node in _bad_nodes(root):
        count += 1
        extent += max(node.end_byte - node.start_byte, 1)
    if count == 0 and root.has_error:
        return (1, 1)
    return (extent, count)


def _bad_nodes(root: Node):
    """Yield every ``ERROR`` or missing node, in pre-order."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            yield node
        if node.has_error:
            # Only descend where an error can actually live.
            stack.extend(reversed(node.children))


def _syntax_message(parser: Parser, text: str) -> str:
    """Build the TemplateSyntaxError message: what we parsed, and what broke."""
    tree = parser.parse(text.encode("utf-8"))
    root = tree.root_node
    first = next(_bad_nodes(root), None)
    if first is None:
        where = "root node reports has_error but no ERROR/MISSING node was found"
    else:
        kind = "MISSING" if first.is_missing else first.type
        row, col = first.start_point
        where = f"{kind} at row {row}, column {col}"
    return f"template does not parse in the target grammar ({where}).\nRendered text was:\n{text}"
