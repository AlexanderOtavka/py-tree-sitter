"""A tiny S-expression builder for tree-sitter query patterns.

The compiler builds a tree of :class:`Sexp` nodes and then renders it to the
textual query language that :class:`tree_sitter.Query` accepts. Keeping this
separate from the tree walk makes the emitted text easy to test and to
pretty-print for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Sexp", "Pattern", "NamedNode", "AnonNode", "Predicate", "render", "quote"]


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
    """

    kind: str | None
    children: list[Sexp] = field(default_factory=list)
    field_name: str | None = None
    anchored: bool = True
    captures: list[str] = field(default_factory=list)
    quantifier: str | None = None

    def render(self, indent: int = 0) -> str:
        pad = "  " * indent
        head = "_" if self.kind is None else self.kind
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
            inner = "\n".join(parts)
            if self.anchored:
                anchor = "  " * (indent + 1) + "."
                inner = f"{anchor}\n{inner}\n{anchor}"
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
    """A complete top-level pattern: a root node plus trailing predicates."""

    root: Sexp
    predicates: list[Predicate] = field(default_factory=list)

    def render(self, indent: int = 0) -> str:
        out = self.root.render(indent)
        if self.predicates:
            preds = "\n".join("  " * (indent + 1) + p.render() for p in self.predicates)
            out = f"{out}\n{preds}"
        return out


def _caps(captures: list[str]) -> str:
    return "".join(f" @{c}" for c in captures)


def _quant(quantifier: str | None) -> str:
    return quantifier or ""


def render(pattern: Sexp) -> str:
    """Render a pattern to query text."""
    return pattern.render()
