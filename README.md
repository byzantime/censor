# censor

Delete comments — and optionally docstrings — from a Python codebase.
Fast, and built so it provably cannot touch anything that isn't a comment.

```console
$ uvx censor path/to/project          # strip own-line comments, in place
$ censor --check src/                 # CI gate: exit 1 if anything would change
$ censor --diff --docstrings pkg/     # preview without writing
```

Zero runtime dependencies, pure stdlib, Python 3.9+.

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

```text
censor PATH... [--docstrings | --all] [options]

  --diff                print unified diffs instead of writing
  --check               write nothing; exit 1 if any file would change
  --keep REGEX          also keep comments matching REGEX
  --no-default-keeps    drop the built-in pragma preserve-list
  --exclude GLOB        skip matching paths (repeatable; matches basename
                        or full path)
  --jobs N              worker processes (default: CPU count)
```

Directories are searched recursively for `*.py`; `.git`, `.venv`, `venv`,
`__pycache__`, `build`, `dist`, `.tox`, `.nox`, `.eggs` and common tool
caches are skipped. Explicitly named files are processed as-is.

Exit codes: `0` success, `1` changes needed (`--check` only), `2` any file
skipped or failed (every such file is listed on stderr, and left untouched).

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
