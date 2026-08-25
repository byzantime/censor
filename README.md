# censor

Delete comments — and optionally docstrings — from a Python codebase.
Fast, and built so it provably cannot touch anything that isn't a comment.

```console
$ censor format path/to/project       # strip own-line comments, in place
$ censor check src/                   # CI gate: exit 1 if anything would change
$ censor check --diff --docstrings pkg/   # preview without writing
```

Zero runtime dependencies, pure stdlib, Python 3.11+.

## The three modes

| Mode | Flag | Deletes |
|---|---|---|
| own-line *(default)* | — | comments that occupy their own line |
| docstrings | `--docstrings` | own-line comments **plus** module/class/function docstrings |
| all | `--all` | every comment, including trailing ones |

The default deliberately leaves trailing comments alone: they are where
`# noqa`, `# type: ignore` and friends live, and where deleting hurts most.

In `--docstrings` mode, a class or function whose body is *only* a docstring
gets a `pass` at the same indentation so the code still parses. A docstring
that shares a physical line with other code (`def f(): "doc"`) or carries a
trailing comment is conservatively kept.

## What is always preserved

- **Shebang** (`#!...` on line 1) and **PEP 263 coding declarations**
  (lines 1–2) — deleting these can break execution or the file's encoding,
  so they survive every mode, even `--all --no-default-keeps`.
- **Tool pragmas**, in every mode: comments starting with `# noqa`, `# fmt:`,
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

- **Comment modes** re-tokenize input and output and require the token
  streams — minus `COMMENT`/`NL` tokens — to be identical. Equal significant
  token streams mean the compiler sees the exact same program.
- **Docstring mode** parses both sides and requires the ASTs to match after
  the removed docstrings are normalized out of the input.

Any mismatch, or any internal error at all, leaves the file untouched and is
reported (exit code 2). Files that cannot be tokenized/parsed, decoded, or
that use lone-CR line endings are skipped the same way. Writes are atomic
(temp file + `os.replace`), so a crash can never leave a half-written file.

The stdlib of the running interpreter is used as a test corpus: all three
modes must verify and be idempotent on every file.

## CLI

censor uses two subcommands, so the invocations you already know from
`ruff` and `black` do what you'd expect:

```text
censor check PATH... [--fix] [--docstrings | --all] [options]
censor format PATH... [--check] [--docstrings | --all] [options]
```

- `check` reports what would change and never writes; add `--fix`
  (ruff-style) to rewrite in place. Exits 1 if anything would change.
- `format` (alias `strip`) rewrites in place (black-style); add
  `--check` to only report instead.

Shared options:

```text
  --diff                print unified diffs instead of writing (never writes)
  --max-doc-lines N     report docstrings longer than N content lines; exit 1
                        if any (composes with every mode)
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
mode = "own-line"        # "own-line" | "docstrings" | "all"
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
with every mode. The count covers the docstring's own text — interior blank
lines do not count, the quote-only opening/closing lines do not. Any violation
makes the exit code 1.

Performance: files are processed in parallel with a process pool; stripping
plus verifying runs at roughly 8 MB of source per second per core (the
whole 15 MB CPython stdlib takes about two seconds on two cores).

## Library use

```python
from censor import Mode, strip_source, verify

stripped = strip_source(src, Mode.DOCSTRINGS, keep=re.compile(r"KEEP"))
assert verify(src, stripped, Mode.DOCSTRINGS)  # do this before persisting
```

`strip_source` is pure (`str -> str`) and raises `SyntaxError` /
`tokenize.TokenError` on source it cannot process. `verify` recomputes
equivalence from scratch; treat a `False` (or any exception) as "do not use
the result".

## Development

```console
$ uv run --group dev pytest
```
