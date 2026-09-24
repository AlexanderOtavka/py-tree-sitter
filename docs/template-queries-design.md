# Template-string queries — internal design

Write a tree-sitter query in the *target language's own syntax* instead of in
S-expressions. A PEP 750 template string (`t"..."`) carries the example source;
interpolations mark the parts that should be captured, wildcarded, or relaxed.

```py
from tree_sitter.template import query, capture

q = query(HCL, t'''
  resource "aws_s3_bucket" {capture("name")} {{
    acl = "private"
    {...}
  }}
''')
for m in q.matches(hcl_source):
    print(m["name"].text)
```

## Pipeline

```
Template (PEP 750)
   │  1. render          _render.py
   ▼
(text with sentinel tokens, [HoleSlot...])
   │  2. parse with the TARGET grammar      tree_sitter.Parser
   ▼
Tree
   │  3. compile tree -> S-expression       _compile.py
   ▼
Pattern (_sexp.Sexp)
   │  4. render to query text               _sexp.render
   ▼
tree_sitter.Query  ──run──▶  Match objects   _query.py
```

The whole point of step 2 is that we never hand-write a parser for the target
language: the grammar that will later match the query is the same grammar that
interprets the query template.

## Step 1 — rendering (`_render.py`)

Each interpolation becomes a **sentinel identifier** (`TSQH0`, `TSQH1`, …) in
the rendered text. A sentinel must survive parsing, so the renderer tries a list
of *padding candidates* per hole and keeps the first that parses without
`ERROR`/`MISSING` nodes anywhere in the tree.

Padding is grammar-dependent and discovered empirically, not hardcoded per
language. Measured examples:

| context | bare `TSQH0` parses? | working candidate |
|---|---|---|
| HCL block body | no | `TSQH0 = 0` |
| HCL block label | yes | `TSQH0` |
| Python statement block | yes | `TSQH0` |
| JSON object member | no | `"TSQH0": 0` |

Output: the rendered text plus, for every hole, a `HoleSlot` recording the byte
span of the sentinel **including** any padding the renderer added. The compiler
uses that span to find the hole's node in the tree.

## Step 2 — parse

Parse the rendered text with the target `Language`. If it still fails to parse,
raise `TemplateSyntaxError` with the rendered text and the error node's position
so the user can see what we actually tried to parse.

## Step 3 — compiling the tree (`_compile.py`)

Walk the parse tree and emit one `Sexp` node per parse-tree node.

Four rules, each validated against real grammars:

1. **Emit all children, anonymous ones included.** Anonymous nodes carry real
   meaning (`x + y` vs `x - y` differ *only* in an anonymous `"+"`/`"-"`), so
   dropping them would silently widen the pattern.

2. **Anchor child sequences by default.** `.` before, between, and after the
   children makes the match exact — no extra siblings. This is what makes a
   template mean "a block with *these* attributes" rather than "a block
   containing at least these".

   Anchors are *required* rather than optional because tree-sitter anchors do
   **not** skip named punctuation: HCL's `block_start`/`block_end` are named
   nodes, so a "named children only" pattern silently fails to match. Verified:
   omitting them yields 0 matches.

3. **Pin leaf text with `#eq?`.** A leaf whose text varies (an `identifier`, a
   `template_literal`) matches any text by type alone, so the compiler attaches
   a private capture and an `#eq?` predicate:
   `(identifier) @_h0` + `(#eq? @_h0 "resource")`. Leaves whose text is fully
   determined by their type (anonymous `"+"`, Python's `true`) need no predicate.

4. **Holes replace subtrees.** When a node's span matches a `HoleSlot`, emit the
   hole's pattern instead of recursing:
   - `Capture` → `(kind) @name` (or `(_) @name`), plus `#match?` / `#any-of?`
     predicates when constrained.
   - `Wildcard` → `(_)` / `(kind)`.
   - `AnyChildren` (`...`) → emit nothing, and set `anchored=False` on the
     **parent**, which is what permits extra siblings there.

Comments in the template are skipped (`node.is_extra`) so a commented template
doesn't demand comments in the matched source.

### Comments in the *matched* source

Anchors reject unlisted children, and a comment is a child, so a template would
otherwise stop matching code merely because someone commented it. Each anchored
position therefore also admits a run of the grammar's comment kinds.

Two dimensions have to be narrowed, or the tolerance either fails to compile or
silently costs exactness. Both are decided by asking the grammar, not from a
per-language table:

- **kinds** — Rust names three comment kinds, but `(block (doc_comment)*)` is an
  impossible pattern, and one bad member poisons the whole alternation.
- **positions** — JS accepts a comment between an argument list's arguments but
  not before its `(`.

Three positions are always excluded, because a run there can slide past real
children rather than just comments:

| excluded position | otherwise |
|---|---|
| before a trailing anonymous delimiter | `def f(a)` matches `def f(a, b)` |
| before an untyped `(_)` wildcard | the wildcard binds the comment and slides |
| inside an adjacent same-kind run, or just past it | a 2-label HCL block matches a 3-label one |

### Anchor placement

Anchors go *between* every pair of children, not just at the ends: with end-only
anchors a middle wildcard floats, so `f(a)` would match `f(a, b)`.

An un-anchored sequence needs an explicit leading `(_)*`, or tree-sitter lines
the listed children up against the node's *first* children only — a `...` body
would then find an attribute solely in first position.

### What `...` attaches to

A padded hole resolves to the outermost node in its span, which is sometimes a
whole container: `{...}` alone in a Python body *is* the `block`. Emitting
nothing for it would un-anchor the container's **parent**, relaxing structure the
template never mentioned (`def foo(): {...}` would match `async def foo()` and
`def foo() -> int`). So such a node is kept in the pattern, unconstrained.

A container is told from a mere wrapper (Python's `expression_statement` around a
bare sentinel) by experiment: duplicate the child's text in the template and
reparse. A list container absorbs both copies as siblings; a wrapper cannot.

## Step 4 — running (`_query.py`)

Render the pattern, build a real `Query`, and run it through `QueryCursor`.
Private `_h*` captures are hidden from results. Public API:

- `query(language, template) -> TemplateQuery`
- `TemplateQuery.matches(source) -> list[Match]`, `.sexp` (the generated query
  text, for debugging), `.captures(source)`
- `Match.__getitem__(name) -> Node`, `.get(name)`, `.all(name) -> list[Node]`,
  `.text(name) -> str`

## Error handling

| error | raised when |
|---|---|
| `TemplateSyntaxError` | rendered template does not parse in the target grammar |
| `TemplateCompileError` | tree walk cannot produce a valid pattern (e.g. a hole whose span matches no node) |
| `tree_sitter.QueryError` | the generated pattern is rejected by tree-sitter |

All three include the generated artifact (rendered text or query text) in the
message, because a user debugging a template needs to see the intermediate form.

## Module layout

| file | role | depends on |
|---|---|---|
| `_holes.py` | hole descriptors (`Capture`, `Wildcard`, `AnyChildren`, `HoleSlot`) | — |
| `_sexp.py` | S-expression builder + renderer | — |
| `_render.py` | `Template` → text + slots (sentinel padding search) | `_holes` |
| `_compile.py` | tree + slots → `Pattern` | `_holes`, `_sexp` |
| `_query.py` | `query()`, `TemplateQuery`, `Match`, errors | all |
| `__init__.py` | public re-exports | all |
