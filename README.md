# censor

Delete comments — and optionally docstrings — from a Python codebase.
Fast, and built so it provably cannot touch anything the program can see.

```console
$ censor format path/to/project       # strip comments and orphan strings, in place
$ censor check src/                   # CI gate: exit 1 if anything would change
$ censor check --diff --delete docstrings pkg/   # preview without writing
```

Zero runtime dependencies, pure stdlib, Python 3.11+.

## What gets deleted

Deletions are selected per category with `--delete CAT` (repeatable) and
subtracted from with `--skip CAT` (repeatable):

| Category | Meaning |
|---|---|
| own-line | comments that occupy their own line |
| trailing | comments after code on the same line |
| orphan-strings | a bare string statement after an assignment |
| docstrings | module/class/function docstrings |

The default is equivalent to
`--delete own-line --delete trailing --delete orphan-strings`; docstrings
are only ever deleted when explicitly selected. `--delete` replaces the
default selection entirely (ruff select-style); `--skip` keeps a category
despite the selection.

When docstrings are selected, a class or function whose body is *only* a
docstring gets a `pass` at the same indentation so the code still parses.
A docstring that shares its line with other code (`def f(): "doc"`) is
conservatively kept.

### Orphan strings

An orphan string is a string literal sitting on its own as a statement
after an assignment — a comment wearing a docstring's clothes:

```python
RETIRING = frozenset({DONE, CLOSED})
"""Which statuses retire a task.        <- deleted: not a docstring

Prose that a docstring-length cap would have caught, parked one
statement below the only place it could have been enforced."""
```

Python evaluates and discards it, so removing it cannot change the
program. A real docstring is `body[0]`, so no assignment can precede it
and none of them match. Indentation is no escape: the rule applies inside
`if`/`for`/`while`/`with`/`try` bodies too, and to a whole run of strings,
not just the first. A string sharing its line with code (`x = 1; "doc"`)
is left alone, since deletion works by whole lines.

**This deletes PEP 224 / Sphinx `autodoc` attribute docstrings**, which
use exactly the same syntax and are indistinguishable from the pattern
above. If your package documents module or class attributes that way, opt
out with `--skip orphan-strings` or `skip = ["orphan-strings"]`.

Selecting this category (or `docstrings`) means the file must *parse*, not
merely tokenize: a source that the running interpreter's `ast` rejects —
one using newer syntax, say — is skipped and reported rather than
comment-stripped.

## What is always preserved

- **Shebang** (`#!...` on line 1) and **PEP 263 coding declarations**
  (lines 1–2) — deleting these can break execution or the file's encoding,
  so they survive every selection, even `--no-default-keeps`.
- **Tool pragmas**: comments starting with `# noqa`, `# fmt:`,
  `# isort:`, `# ruff:`, `# mypy:`, `# type:`, `# pyright:`, `# pragma:`
  (case-insensitive, flexible spacing). Pass `--no-default-keeps` if you
  really do want "literally all comments".
- Anything matching your own `--keep REGEX` (matched against the comment
  text, including the `#`).

## The safety guarantee

`censor` never regenerates code. The only edits it makes are deleting whole
physical lines and cutting a line at the start column of a trailing comment,
so every surviving line is byte-for-byte identical to the input — formatting,
quotes, escapes, encodings, BOMs and CRLF line endings included.

On top of that, every rewrite must pass an independent verification gate
before the file is touched:

- **Comment deletions** re-tokenize input and output and require the token
  streams — minus `COMMENT`/`NL` tokens — to be identical. Equal significant
  token streams mean the compiler sees the exact same program.
- **Docstring and orphan-string deletions** parse both sides and require the
  ASTs to match after the removed string statements are normalized out of the
  input. An AST cannot see comments, so this path additionally requires every
  comment on the preserve-list to survive. A file from which no string
  statement was actually removed is verified by tokens, as before.

Any mismatch, or any internal error at all, leaves the file untouched and is
reported (exit code 2). Files that cannot be tokenized/parsed, decoded, or
that use lone-CR line endings are skipped the same way. Writes are atomic
(temp file + `os.replace`), so a crash can never leave a half-written file.

The stdlib of the running interpreter is used as a test corpus: every target
set must verify and be idempotent on every file.

## CLI

censor uses two subcommands, so the invocations you already know from
`ruff` and `black` do what you'd expect:

```text
censor check PATH... [--fix] [--delete CAT]... [--skip CAT]... [options]
censor format PATH... [--check] [--delete CAT]... [--skip CAT]... [options]
```

- `check` reports what would change and never writes; add `--fix`
  (ruff-style) to rewrite in place. Exits 1 if anything would change.
- `format` (alias `strip`) rewrites in place (black-style); add
  `--check` to only report instead.

Shared options:

```text
  --delete CAT          delete this category instead of the default
                        (orphan-strings, own-line, trailing); one of
                        docstrings, orphan-strings, own-line, trailing;
                        repeatable to select several
  --skip CAT            keep this category despite the selection (repeatable)
  --diff                print unified diffs instead of writing (never writes)
  --max-doc-lines N     report docstrings longer than N content lines; exit 1
                        if any (composes with every selection)
  --keep REGEX          also keep comments matching REGEX (repeatable)
  --default-keeps / --no-default-keeps
                        keep the built-in pragma preserve-list (default: true)
  --exclude GLOB        skip matching paths (repeatable; matches basename
                        or full path)
  --jobs N              worker processes (default: CPU count)
  --config PATH         read configuration from this pyproject.toml instead
                        of discovering one
  --isolated            ignore any pyproject.toml configuration
```

Running `censor` without a command is a usage error (exit 2); nothing is
written.

## Configuration

`censor` reads settings from a `[tool.censor]` table in a `pyproject.toml`,
the same way `black` and `ruff` do:

```toml
[tool.censor]
delete = ["own-line"]    # replaces the default (own-line, trailing,
                         # orphan-strings)
skip = ["trailing"]      # subtracted from the selection
keep = ["^# KEEP"]       # list of regexes for comments to preserve
default-keeps = true     # built-in pragma preserve-list (# noqa etc.)
exclude = ["migrations/*"]
```

Discovery is black-style: starting from the common ancestor of the input
paths, censor walks up until it finds a `pyproject.toml` with a
`[tool.censor]` table — stopping at the project root (a directory holding
`.git` or `.hg`). Pass `--config PATH` to read an explicit file (it may be
a normal pyproject or carry a bare top-level `[censor]` table), or
`--isolated` to ignore configuration entirely.

Precedence follows the black convention: **configuration supplies defaults;
any explicit CLI flag wins outright** (`check`, `format`, `diff`, `jobs` and
paths are CLI-only and cannot be set in the file).

## CI gate

```console
$ censor check src/
would strip comments from: src/pkg/mod.py
censor: 1 files contain comments censor would delete.
censor: to fix, run: censor format src/
censor: (rewrites in place; run with --diff first to preview the deletions)
$ echo $?
1
```

Directories are searched recursively for `*.py`; `.git`, `.venv`, `venv`,
`__pycache__`, `build`, `dist`, `.tox`, `.nox`, `.eggs` and common tool
caches are skipped. Explicitly named files are processed as-is.

Exit codes: `0` success, `1` changes needed (`censor check` without `--fix`, `censor format --check`, or docstring
violations from `--max-doc-lines`), `2` any file skipped or failed (every
such file is listed on stderr, and left untouched).

## Docstring length cap

```console
$ censor check --max-doc-lines 20 src/   # CI gate for oversized docstrings
```

`--max-doc-lines N` reports every module/class/function docstring whose
content spans more than *N* lines, as
`path:lineno: docstring of 'name' has M lines (limit N)`. It is a check,
never a rewrite: docstrings are never modified by this flag, and it composes
with every selection. The count covers the docstring's own text — interior
blank lines do not count, the quote-only opening/closing lines do not. Any
violation makes the exit code 1.

The cap only inspects real docstrings (`body[0]`), so prose relocated one
statement below the thing it describes is invisible to it. That is the hole
`orphan-strings` closes: such a string is deleted outright rather than
measured.

Performance: files are processed in parallel with a process pool; stripping
plus verifying runs at roughly 8 MB of source per second per core (the
whole 15 MB CPython stdlib takes about two seconds on two cores).

## Library use

```python
from censor import DOCSTRINGS, strip_source, verify

stripped = strip_source(
    src, {"own-line", "docstrings"}, keep=re.compile(r"KEEP")
)
assert verify(src, stripped, {"own-line", "docstrings"})  # do before persisting
```

`strip_source` is pure (`str -> str`) and raises `ValueError` on unknown
category names and `SyntaxError` / `tokenize.TokenError` on source it
cannot process. `verify` recomputes equivalence from scratch; treat a
`False` (or any exception) as "do not use the result".

## Development

```console
$ uv run --group dev pytest
```
