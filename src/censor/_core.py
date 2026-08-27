"""The stripping engine and its safety gate.
The source is only ever edited by deleting whole physical lines, or by
cutting a line at the start column of a trailing comment: code is never
regenerated, so every surviving line is byte-for-byte identical to the
input. Physical lines split on ``"\\n"`` only (the tokenizer's view).
Every edit is checked by :func:`verify`; refuse to persist unverified results.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter
from typing import Dict
from typing import Iterable
from typing import Iterator
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import Pattern
from typing import Set
from typing import Tuple

__all__ = [
    "OWN_LINE",
    "TRAILING",
    "DOCSTRINGS",
    "ORPHAN_STRINGS",
    "ALL_TARGETS",
    "TARGETS",
    "strip_source",
    "verify",
    "DEFAULT_KEEPS",
    "DocstringViolation",
    "docstring_violations",
]


OWN_LINE = "own-line"
TRAILING = "trailing"
DOCSTRINGS = "docstrings"
ORPHAN_STRINGS = "orphan-strings"
ALL_TARGETS = frozenset({OWN_LINE, TRAILING, ORPHAN_STRINGS})
TARGETS = frozenset({OWN_LINE, TRAILING, DOCSTRINGS, ORPHAN_STRINGS})
_AST_TARGETS = frozenset({DOCSTRINGS, ORPHAN_STRINGS})


DEFAULT_KEEPS: "Pattern[str]" = re.compile(
    r"^#\s*(?:noqa\b|fmt:|isort:|ruff:|mypy:|type:|pyright:|pragma:)",
    re.IGNORECASE,
)

_CODING = re.compile(r"^#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")

_DOCSTRING_OWNERS = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)

_Docstrings = List[Tuple[ast.stmt, ast.Expr, bool]]
_Orphans = List[Tuple[List[ast.stmt], ast.Expr]]


def _physical_lines(src: str) -> List[str]:
    return io.StringIO(src).readlines()


def _kept_comment(
    text: str, row: int, keep: "Optional[Pattern[str]]", default_keeps: bool
) -> bool:
    if row == 1 and text.startswith("#!"):
        return True
    if row <= 2 and _CODING.match(text):
        return True
    if default_keeps and DEFAULT_KEEPS.match(text):
        return True
    return keep is not None and keep.search(text) is not None


def _kept(
    tok: tokenize.TokenInfo, keep: "Optional[Pattern[str]]", default_keeps: bool
) -> bool:
    return _kept_comment(tok.string, tok.start[0], keep, default_keeps)


def _string_expr(node: ast.stmt) -> bool:
    """True when *node* is a bare string-literal expression statement.

    F-strings are :class:`ast.JoinedStr`, not :class:`ast.Constant`, so
    they are code and never match.
    """
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _line_context(lines: List[str], node: ast.stmt) -> "Tuple[str, str]":
    """What shares *node*'s own lines: the text before it and after it.

    ``col_offset`` counts UTF-8 bytes, not characters, so the cut is made
    on the encoded line.  Slicing the ``str`` directly would overshoot the
    end of a non-ASCII literal and report code — or a kept pragma comment
    — that follows it on the line as absent.
    """
    return (
        lines[node.lineno - 1].encode()[: node.col_offset].decode(),
        lines[node.end_lineno - 1].encode()[node.end_col_offset :].decode(),
    )


def _surviving_comments(
    src: str,
    lines: List[str],
    selected: frozenset,
    keep: "Optional[Pattern[str]]",
    default_keeps: bool,
) -> "Set[int]":
    """Rows holding a comment that the comment pass will not remove.

    Kept comments, and whole categories the selection left out.  These
    are what a string statement must not cover: the rest of the rows it
    occupies were losing their comments anyway.
    """
    rows = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        own_line = not lines[row - 1][:col].strip()
        category = OWN_LINE if own_line else TRAILING
        if category not in selected or _kept(tok, keep, default_keeps):
            rows.add(row)
    return rows


def _line_is_clear(
    lines: List[str], surviving: "Set[int]", node: ast.stmt
) -> bool:
    """True when deleting *node*'s physical lines would take nothing else.

    Nothing but a comment may follow it on its last line, and nothing may
    precede it on its first.  A parenthesised literal covers rows between
    the two, so every comment the span covers must be one already on its
    way out — *surviving* is the complement of.  An AST cannot see
    comments, so :func:`verify` would never notice one leaving this way."""

    if not surviving.isdisjoint(range(node.lineno, node.end_lineno + 1)):
        return False
    before, residue = _line_context(lines, node)
    if before.strip():
        return False
    return not residue.strip() or residue.lstrip().startswith("#")


def _deletable_docstrings(
    lines: List[str], surviving: "Set[int]", tree: ast.Module
) -> "_Docstrings":
    """Docstrings whose physical lines contain nothing but the docstring.

    Returns ``(owner, docstring_stmt, sole)`` triples; *sole* means the
    docstring is the entire body of a class or function, so deleting it
    requires a ``pass`` in its place.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = node.body
        if not body or not _string_expr(body[0]):
            continue
        doc = body[0]
        if not _line_is_clear(lines, surviving, doc):
            continue
        sole = len(body) == 1 and not isinstance(node, ast.Module)
        found.append((node, doc, sole))
    return found


def _normalise_targets(targets: "Iterable[str]") -> frozenset:
    selected = frozenset(targets)
    unknown = selected - TARGETS
    if unknown:
        raise ValueError(
            "unknown target%s: %s; valid targets are: %s"
            % (
                "s" if len(unknown) != 1 else "",
                ", ".join(sorted(unknown)),
                ", ".join(sorted(TARGETS)),
            )
        )
    return selected


def _mark_comments(
    src: str,
    lines: List[str],
    selected: frozenset,
    keep: "Optional[Pattern[str]]",
    default_keeps: bool,
) -> "Tuple[Set[int], Dict[int, str]]":
    """Return the lines to drop and the lines to cut for comment removal."""
    delete: Set[int] = set()
    replace: Dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT or _kept(tok, keep, default_keeps):
            continue
        row, col = tok.start
        line = lines[row - 1]
        if not line[:col].strip():
            if OWN_LINE in selected:
                delete.add(row - 1)
        elif TRAILING in selected:
            replace[row - 1] = line[:col].rstrip() + line[tok.end[1] :]
    return delete, replace


def _deletable_strings(
    src: str,
    lines: List[str],
    tree: ast.Module,
    selected: frozenset,
    keep: "Optional[Pattern[str]]",
    default_keeps: bool,
) -> "Tuple[_Docstrings, _Orphans]":
    """The docstrings and orphan strings the selection deletes from *tree*.

    Both are located before either is removed, so that :func:`verify`'s
    two normalisations cannot interfere.
    """
    docs: _Docstrings = []
    orphans: _Orphans = []
    surviving = _surviving_comments(src, lines, selected, keep, default_keeps)
    if DOCSTRINGS in selected:
        docs = _deletable_docstrings(lines, surviving, tree)
    if ORPHAN_STRINGS in selected:
        orphans = _deletable_orphan_strings(lines, surviving, tree)
    return docs, orphans


def _drop_lines(
    delete: "Set[int]", replace: Dict[int, str], first: int, last: int
) -> None:
    """Delete rows *first*..*last* outright, overriding any line cut.

    A cut queued for one of them by the comment pass has to go: the span
    was only chosen because every comment it covers was leaving anyway,
    and :func:`strip_source` keeps a replaced line even when it is also
    marked for deletion.
    """
    delete.update(range(first, last + 1))
    for row in range(first, last + 1):
        replace.pop(row, None)


def strip_source(
    src: str,
    targets: "Iterable[str]" = ALL_TARGETS,
    keep: "Optional[Pattern[str]]" = None,
    *,
    default_keeps: bool = True,
) -> str:
    """Return *src* with the selected comment categories deleted.

    *targets* is an iterable of category names (see :data:`TARGETS`).
    Shebang, coding lines, and kept comments always survive.  Raises
    ValueError on unknown names; SyntaxError/TokenError when *src* cannot
    be tokenized, or parsed if an AST category is selected.  *src* must
    use ``\\n``/``\\r\\n`` endings.  Run the result through :func:`verify`.
    """
    selected = _normalise_targets(targets)
    lines = _physical_lines(src)
    delete, replace = _mark_comments(src, lines, selected, keep, default_keeps)
    if selected & _AST_TARGETS:
        docs, orphans = _deletable_strings(
            src, lines, ast.parse(src), selected, keep, default_keeps
        )
        for _owner, doc, sole in docs:
            first, last = doc.lineno - 1, doc.end_lineno - 1
            _drop_lines(delete, replace, first, last)
            if sole:
                indent = lines[first][: doc.col_offset]
                ending = lines[last][len(lines[last].rstrip("\r\n")) :]
                replace[last] = indent + "pass" + ending
        for _body, node in orphans:
            _drop_lines(delete, replace, node.lineno - 1, node.end_lineno - 1)
    if not delete and not replace:
        return src
    return "".join(
        replace[i] if i in replace else line
        for i, line in enumerate(lines)
        if i in replace or i not in delete
    )


class DocstringViolation(NamedTuple):
    """A docstring whose line count exceeds a configured cap."""

    name: str
    lineno: int
    lines: int


def _docstring_stmt(
    node: "ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef",
) -> "Optional[Tuple[ast.Expr, ast.Constant]]":
    """The docstring of *node* as ``(statement, string constant)``, or None.

    Same detection rule as :func:`_deletable_docstrings` but without any
    layout restriction — this is a read-only check, so docstrings that share
    their line with other code count too.
    """
    body = node.body
    if not body or not _string_expr(body[0]):
        return None
    doc = body[0]
    return doc, doc.value


def docstring_violations(src: str, max_lines: int) -> List[DocstringViolation]:
    """Docstrings in *src* whose content exceeds *max_lines* lines.

    The count covers the docstring's own text with the quotes stripped:
    interior blank lines do not count, the quote-only opening and closing
    lines do not, so both ``'''one-liner'''`` and its bare-quote block
    equivalent are one line.  Never rewrites anything; raises
    :class:`SyntaxError` when *src* does not parse.
    """
    tree = ast.parse(src)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        found = _docstring_stmt(node)
        if found is None:
            continue
        doc, const = found
        n = sum(1 for line in const.value.splitlines() if line.strip())
        if n > max_lines:
            name = "module" if isinstance(node, ast.Module) else node.name
            violations.append(DocstringViolation(name, doc.lineno, n))
    return violations


def _statement_blocks(tree: ast.Module) -> "Iterator[List[ast.stmt]]":
    """Every statement list in *tree*.

    Module, class and function bodies, and the bodies of compound
    statements (``if``/``for``/``while``/``with``/``try`` and their
    ``else``/``finally``/``except`` arms).
    """
    for node in ast.walk(tree):
        for _field, value in ast.iter_fields(node):
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], ast.stmt)
            ):
                yield value


def _deletable_orphan_strings(
    lines: List[str], surviving: "Set[int]", tree: ast.Module
) -> "_Orphans":
    """Orphan strings as ``(containing body, string statement)`` pairs.

    A bare string expression whose nearest preceding non-string statement
    is an assignment — a docstring that is not one.  A whole run qualifies,
    not just the first, so stripping is idempotent.  Real docstrings never
    match: a docstring is ``body[0]``, so no assignment precedes it.  Ones
    not owning their physical lines are skipped: deletion is by lines.
    """
    found = []
    for body in _statement_blocks(tree):
        after_assign = False
        for node in body:
            if not _string_expr(node):
                after_assign = isinstance(node, (ast.Assign, ast.AnnAssign))
                continue
            if after_assign and _line_is_clear(lines, surviving, node):
                found.append((body, node))
    return found


def _kept_comment_counts(
    src: str, keep: "Optional[Pattern[str]]", default_keeps: bool
) -> "Counter[str]":
    """How often each comment the preserve-list protects occurs in *src*."""
    return Counter(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT and _kept(tok, keep, default_keeps)
    )


_INSIGNIFICANT = frozenset({tokenize.COMMENT, tokenize.NL})


def _significant_tokens(src: str) -> "List[Tuple[int, str]]":
    sig = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in _INSIGNIFICANT:
            continue
        sig.append(
            (tok.type, "" if tok.type == tokenize.NEWLINE else tok.string)
        )
    return sig


def verify(
    src: str,
    stripped: str,
    targets: "Iterable[str]" = ALL_TARGETS,
    *,
    keep: "Optional[Pattern[str]]" = None,
    default_keeps: bool = True,
) -> bool:
    """Recompute, from scratch, that *stripped* is equivalent to *src*.

    Comment deletions are verified by comparing token streams with
    COMMENT/NL filtered out.  Only when a docstring or orphan string is
    really removed are both sides parsed and ``ast.dump`` compared, plus a
    check that no preserved comment vanished — an AST cannot see those.
    *keep* and *default_keeps* must match those used for stripping.
    """
    selected = _normalise_targets(targets)
    if selected & _AST_TARGETS:
        lines = _physical_lines(src)
        tree = ast.parse(src)
        docs, orphans = _deletable_strings(
            src, lines, tree, selected, keep, default_keeps
        )
        if docs or orphans:
            for owner, doc, sole in docs:
                owner.body.remove(doc)
                if sole:
                    owner.body.append(ast.Pass())
            for body, node in orphans:
                body.remove(node)
            if _kept_comment_counts(src, keep, default_keeps) - (
                _kept_comment_counts(stripped, keep, default_keeps)
            ):
                return False
            return ast.dump(tree) == ast.dump(ast.parse(stripped))
    return _significant_tokens(src) == _significant_tokens(stripped)
