"""The stripping engine and its safety gate.

Design rules that make the tool provably safe:

* The source is only ever edited by deleting whole physical lines, or by
  cutting a line at the start column of a trailing comment.  Code is never
  regenerated, so the formatting, string contents, escapes and line endings
  of every surviving line are byte-for-byte identical to the input.
* Physical lines are obtained by splitting on ``"\n"`` only (the same view
  the tokenizer's ``readline`` has) — ``str.splitlines()`` would also split
  on form feeds, ``\\u2028`` etc. and desynchronise our indices from the
  token and AST line numbers.
* Every edit is checked by :func:`verify`, which recomputes equivalence from
  scratch: comment modes compare the significant token streams of input and
  output, docstring mode compares ASTs after normalising the removed
  docstrings out of the input.  Callers must refuse to persist any result
  that does not verify.
"""

from __future__ import annotations

import ast
import enum
import io
import re
import tokenize
from typing import Dict
from typing import List
from typing import Optional
from typing import Pattern
from typing import Set
from typing import Tuple

__all__ = ["Mode", "strip_source", "verify", "DEFAULT_KEEPS"]


class Mode(enum.Enum):
    """What gets deleted."""

    #: Comments that occupy their own line (the default).
    OWN_LINE = "own-line"
    #: Own-line comments plus module/class/function docstrings.
    DOCSTRINGS = "docstrings"
    #: Every comment, including trailing ones.
    ALL = "all"


#: Tool pragmas preserved in every mode unless default keeps are disabled.
DEFAULT_KEEPS: "Pattern[str]" = re.compile(
    r"^#\s*(?:noqa\b|fmt:|isort:|ruff:|mypy:|type:|pyright:|pragma:)",
    re.IGNORECASE,
)

# PEP 263 encoding declaration; only honoured on lines 1-2.
_CODING = re.compile(r"^#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")

_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _physical_lines(src: str) -> List[str]:
    return io.StringIO(src).readlines()


def _kept(tok: tokenize.TokenInfo, keep: "Optional[Pattern[str]]", default_keeps: bool) -> bool:
    text = tok.string
    row = tok.start[0]
    if row == 1 and text.startswith("#!"):
        return True
    if row <= 2 and _CODING.match(text):
        return True
    if default_keeps and DEFAULT_KEEPS.match(text):
        return True
    return keep is not None and keep.search(text) is not None


def _deletable_docstrings(lines: List[str], tree: ast.Module) -> "List[Tuple[ast.stmt, ast.Expr, bool]]":
    """Docstrings whose physical lines contain nothing but the docstring.

    Returns ``(owner, docstring_stmt, sole)`` triples; *sole* means the
    docstring is the entire body of a class or function, so deleting it
    requires a ``pass`` in its place.  Docstrings that share a line with
    other code (``def f(): "doc"``) or a trailing comment are left alone —
    partial-line edits of statements are never attempted.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = node.body
        if not body:
            continue
        doc = body[0]
        if not (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant) and isinstance(doc.value.value, str)):
            continue
        if lines[doc.lineno - 1][: doc.col_offset].strip():
            continue
        if lines[doc.end_lineno - 1][doc.end_col_offset :].strip():
            continue
        sole = len(body) == 1 and not isinstance(node, ast.Module)
        found.append((node, doc, sole))
    return found


def strip_source(
    src: str,
    mode: Mode = Mode.OWN_LINE,
    keep: "Optional[Pattern[str]]" = None,
    *,
    default_keeps: bool = True,
) -> str:
    """Return *src* with comments (and docstrings, per *mode*) deleted.

    Shebang and PEP 263 coding lines are always preserved; comments matching
    :data:`DEFAULT_KEEPS` (unless ``default_keeps=False``) or *keep* are too.

    Raises :class:`SyntaxError`/:class:`tokenize.TokenError` when *src*
    cannot be tokenized (or, in :attr:`Mode.DOCSTRINGS`, parsed).  The result
    is not guaranteed safe by itself — run it through :func:`verify` before
    persisting it anywhere.
    """
    if not isinstance(mode, Mode):
        mode = Mode(mode)
    lines = _physical_lines(src)
    delete: Set[int] = set()
    replace: Dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT or _kept(tok, keep, default_keeps):
            continue
        row, col = tok.start
        line = lines[row - 1]
        if not line[:col].strip():
            delete.add(row - 1)
        elif mode is Mode.ALL:
            replace[row - 1] = line[:col].rstrip() + line[tok.end[1] :]
    if mode is Mode.DOCSTRINGS:
        for _owner, doc, sole in _deletable_docstrings(lines, ast.parse(src)):
            first, last = doc.lineno - 1, doc.end_lineno - 1
            delete.update(range(first, last + 1))
            if sole:
                indent = lines[first][: doc.col_offset]
                ending = lines[last][len(lines[last].rstrip("\r\n")) :]
                replace[last] = indent + "pass" + ending
    if not delete and not replace:
        return src
    return "".join(replace[i] if i in replace else line for i, line in enumerate(lines) if i in replace or i not in delete)


_INSIGNIFICANT = frozenset({tokenize.COMMENT, tokenize.NL})


def _significant_tokens(src: str) -> "List[Tuple[int, str]]":
    sig = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in _INSIGNIFICANT:
            continue
        # A NEWLINE's own text carries no meaning (the tokenizer synthesises
        # an empty one at EOF); its presence and position still must match.
        sig.append((tok.type, "" if tok.type == tokenize.NEWLINE else tok.string))
    return sig


def verify(src: str, stripped: str, mode: Mode = Mode.OWN_LINE) -> bool:
    """Recompute, from scratch, that *stripped* is equivalent to *src*.

    Comment modes compare the token streams with COMMENT/NL filtered out —
    equality implies the compiler sees identical programs, at a fraction of
    the cost of parsing.  Docstring mode parses both sides and compares
    ``ast.dump`` after normalising the removed docstrings out of *src*.
    """
    if not isinstance(mode, Mode):
        mode = Mode(mode)
    if mode is Mode.DOCSTRINGS:
        tree = ast.parse(src)
        for owner, doc, sole in _deletable_docstrings(_physical_lines(src), tree):
            owner.body.remove(doc)
            if sole:
                owner.body.append(ast.Pass())
        return ast.dump(tree) == ast.dump(ast.parse(stripped))
    return _significant_tokens(src) == _significant_tokens(stripped)
