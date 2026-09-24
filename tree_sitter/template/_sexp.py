"""A tiny S-expression builder for tree-sitter query patterns.

The compiler builds a tree of :class:`Sexp` nodes and then renders it to the
textual query language that :class:`tree_sitter.Query` accepts. Keeping this
separate from the tree walk makes the emitted text easy to test and to
pretty-print for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "AnonNode",
    "NamedNode",
    "Pattern",
    "Predicate",
    "Sexp",
    "extras_pattern",
    "quote",
    "render",
]


def quote(text: str) -> str:
    """Quote a literal string for the tree-sitter query language."""
    out = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class Sexp:
    """Base class for a renderable query fragment."""

    def render(self, indent: int = 0) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class AnonNode(Sexp):
    """An anonymous (punctuation/keyword) node, written as a quoted literal."""

    text: str
    captures: list[str] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        return quote(self.text) + _caps(self.captures)


@dataclass
class NamedNode(Sexp):
    """A named node, written as ``(type child...)``.

    Attributes
    ----------
    kind : str | None
        Node type. ``None`` renders the wildcard ``_``.
    children : list[Sexp]
        Child patterns, in order.
    field_name : str | None
        Emitted as ``field: (child)`` by the parent when set.
    anchored : bool
        When true, ``.`` anchors are emitted around the child sequence so that
        *only* the listed children may appear. When false, extra children are
        allowed (this is what ``...`` in a template turns off).
    captures : list[str]
        Capture names attached to this node.
    quantifier : str | None
        One of ``"?"``, ``"*"``, ``"+"``.
    extras : tuple[str, ...]
        Node kinds that may appear anywhere in an anchored child sequence
        without breaking the match — comments, typically. Anchors otherwise
        reject them, which would make a template stop matching source merely
        because someone commented it.
    """

    kind: str | None
    children: list[Sexp] = field(default_factory=list)
    field_name: str | None = None
    anchored: bool = True
    captures: list[str] = field(default_factory=list)
    quantifier: str | None = None
    extras: tuple[str, ...] = ()
    #: Emit a leading ``(_)*`` in an un-anchored sequence so that unlisted
    #: earlier siblings can be skipped. Not every node accepts it, so the
    #: compiler probes the grammar before turning it on.
    skip_siblings: bool = False
    #: Gap positions that must *not* get an anchor in an otherwise anchored
    #: sequence. Gap ``i`` is the slot before child ``i``; gap ``len(children)``
    #: is the slot after the last child. Opening exactly the gap an
    #: ``AnyChildren`` hole vacated relaxes the sibling count *there* while the
    #: remaining anchors keep every written child pinned to its position.
    open_gaps: frozenset[int] = frozenset()
    #: Gap positions that must not get an *extras* run even though the sequence
    #: is anchored. Some positions reject a comment where their siblings accept
    #: one -- JS rejects one before an argument list's ``(`` -- and a single
    #: illegal position must not cost the whole node its comment tolerance.
    no_extras_at: frozenset[int] = frozenset()
    #: Extra anonymous literals a quantified node may also match, rendered as an
    #: alternation: ``[(_) @cap ","]*``. Lets a ``*``/``+`` capture span a
    #: separated list instead of stopping at the first separator.
    alternatives: tuple[str, ...] = ()

    def render(self, indent: int = 0) -> str:
        pad = "  " * indent
        head = "_" if self.kind is None else self.kind
        if self.alternatives and self.quantifier and not self.children:
            branches = " ".join([f"({head}){_caps(self.captures)}", *map(quote, self.alternatives)])
            return f"[{branches}]{self.quantifier}"
        if not self.children:
            body = f"({head})"
        else:
            parts: list[str] = []
            for child in self.children:
                rendered = child.render(indent + 1)
                prefix = ""
                if isinstance(child, NamedNode) and child.field_name:
                    prefix = f"{child.field_name}: "
                parts.append("  " * (indent + 1) + prefix + rendered)
            if self.anchored:
                # Anchors must appear *between* every pair of children, not just
                # at the ends: with end-only anchors a middle wildcard floats,
                # so `f(a, b)` would match a one-argument pattern.
                #
                # Each child is preceded by `. extras *.*` -- an anchor, an
                # optional run of extras, then another anchor -- so a comment in
                # the matched source does not defeat the anchoring. The second
                # anchor is essential: an extras run with nothing after it lets
                # any sibling slide through it, which made a `<div><p>hi</p>`
                # template match `<div><p>hi</p><p>yo</p>`.
                #
                # The sequence still ends with a bare `.` and never a trailing
                # extras run, which is what rejects additional *meaningful*
                # children.
                #
                # A gap listed in ``open_gaps`` is left un-anchored: that is
                # where an ``AnyChildren`` hole was removed, so any number of
                # siblings may appear there while every other position stays
                # pinned.
                ind = "  " * (indent + 1)
                anchor = ind + "."
                gap = ind + extras_pattern(self.extras) if self.extras else None
                interleaved = []
                for i, part in enumerate(parts):
                    open_here = i in self.open_gaps
                    if not open_here:
                        interleaved.append(anchor)
                    if gap and not open_here and i not in self.no_extras_at:
                        interleaved.append(gap)
                        interleaved.append(anchor)
                    interleaved.append(part)
                if len(parts) not in self.open_gaps:
                    interleaved.append(anchor)
                inner = "\n".join(interleaved)
            elif self.skip_siblings:
                # An un-anchored sequence needs an explicit leading `(_)*`.
                # Without it tree-sitter only matches the listed children
                # against the *first* children of the node, so a `...` body
                # would find `versioning = true` only when it happens to be the
                # first attribute. `(_)*` lets unlisted siblings be skipped.
                ind = "  " * (indent + 1)
                inner = "\n".join([ind + "(_)*", *parts])
            else:
                inner = "\n".join(parts)
            body = f"({head}\n{inner}\n{pad})"
        return body + _quant(self.quantifier) + _caps(self.captures)


@dataclass
class Predicate(Sexp):
    """A predicate such as ``(#eq? @cap "text")``."""

    name: str
    args: list[str] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        return f"(#{self.name} " + " ".join(self.args) + ")"


@dataclass
class Pattern(Sexp):
    """A complete top-level pattern: a root node plus its predicates.

    The root and its predicates are wrapped in one extra pair of parentheses.
    That grouping is required, not cosmetic: ``(block ...)`` followed by a bare
    ``(#eq? ...)`` is parsed by tree-sitter as *two* patterns, and the predicate
    then constrains neither of them.
    """

    root: Sexp
    predicates: list[Predicate] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        if not self.predicates:
            return self.root.render(indent)
        pad = "  " * indent
        root_text = self.root.render(indent + 1)
        preds = "\n".join("  " * (indent + 1) + p.render() for p in self.predicates)
        return f"{pad}({root_text}\n{preds}{pad})".lstrip()


def extras_pattern(extras: tuple[str, ...]) -> str:
    """Render a repeated-alternation pattern matching any number of *extras*."""
    if len(extras) == 1:
        return f"({extras[0]})*"
    inner = " ".join(f"({kind})" for kind in extras)
    return f"[{inner}]*"


def _caps(captures: list[str]) -> str:
    return "".join(f" @{c}" for c in captures)


def _quant(quantifier: str | None) -> str:
    return quantifier or ""


def render(pattern: Sexp) -> str:
    """Render a pattern to query text."""
    return pattern.render()
