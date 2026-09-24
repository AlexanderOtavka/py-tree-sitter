"""Hole descriptors: the values users interpolate into a query template.

A *hole* is anything the user substitutes into a ``t"..."`` template. Every hole
is rendered into the template text as a syntactically valid *sentinel* token so
that the target grammar can still parse the result; the compiler then recognizes
the sentinel in the parse tree and replaces it with the appropriate piece of
S-expression.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Alternatives",
    "AnyChildren",
    "Capture",
    "Hole",
    "Wildcard",
    "anything",
    "capture",
]


class Hole:
    """Base class for a value interpolated into a query template."""

    __slots__ = ()

    #: Whether this hole stands for a *sequence* of unmatched siblings
    #: (``{...}``) rather than for a single node.
    is_variadic: bool = False


@dataclass(frozen=True)
class Capture(Hole):
    """Capture the node at this position under ``name``.

    Parameters
    ----------
    name : str
        Capture name, exposed as ``match[name]``.
    kind : str | None
        Optional node type to require at this position (e.g. ``"string_lit"``).
        When ``None``, any node matches.
    pattern : str | None
        Optional regular expression the captured text must match. Compiled to a
        ``#match?`` predicate.
    one_of : tuple[str, ...] | None
        Optional set of literal texts the captured node must equal. Compiled to
        an ``#any-of?`` predicate.
    quantifier : str | None
        Optional tree-sitter quantifier: ``"?"``, ``"*"`` or ``"+"``.
    """

    name: str
    kind: str | None = None
    pattern: str | None = None
    one_of: tuple[str, ...] | None = None
    quantifier: str | None = None

    def __post_init__(self):
        if not self.name:
            raise ValueError("capture name must not be empty")
        if self.quantifier not in (None, "?", "*", "+"):
            raise ValueError(f"invalid quantifier: {self.quantifier!r}")

    @property
    def is_variadic(self) -> bool:
        return self.quantifier in ("*", "+")


@dataclass(frozen=True)
class Wildcard(Hole):
    """Match exactly one node of any (or a given) type, without capturing it."""

    kind: str | None = None


@dataclass(frozen=True)
class AnyChildren(Hole):
    """Match any number of additional sibling nodes at this position.

    This is what a bare ``...`` (``Ellipsis``) in a template compiles to: it
    relaxes the enclosing node's child sequence so that unlisted children are
    permitted.
    """

    is_variadic: bool = True


@dataclass(frozen=True)
class Alternatives(Hole):
    """Match any one of several literal source texts at this position."""

    texts: tuple[str, ...]
    name: str | None = None

    def __post_init__(self):
        if not self.texts:
            raise ValueError("Alternatives requires at least one text")


def capture(
    name: str,
    kind: str | None = None,
    *,
    pattern: str | None = None,
    one_of: list[str] | tuple[str, ...] | None = None,
    quantifier: str | None = None,
) -> Capture:
    """Create a :class:`Capture` hole.

    Examples
    --------
    >>> capture("name")                      # capture any node as @name
    >>> capture("name", "string_lit")        # require a string_lit
    >>> capture("n", pattern=r"^aws_")       # regex constraint
    >>> capture("n", one_of=["a", "b"])      # literal alternatives
    """
    return Capture(
        name=name,
        kind=kind,
        pattern=pattern,
        one_of=tuple(one_of) if one_of is not None else None,
        quantifier=quantifier,
    )


def anything(kind: str | None = None) -> Wildcard:
    """Match a single node of any type (or of type ``kind``) without capturing."""
    return Wildcard(kind=kind)


@dataclass
class HoleSlot:
    """Internal bookkeeping for one hole occurrence in a rendered template."""

    index: int
    hole: Hole
    sentinel: str
    #: Byte offset of the sentinel token in the rendered template text.
    start: int = -1
    end: int = -1
    #: Extra padding text the renderer added around the sentinel to keep the
    #: template parseable (e.g. ``" = 0"``). Used to size the sentinel's span.
    pad_prefix: str = ""
    pad_suffix: str = ""
    expression: str = field(default="", compare=False)
