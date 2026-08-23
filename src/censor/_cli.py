"""Command-line interface: discovery, parallelism, atomic in-place writes."""

from __future__ import annotations

import argparse
import difflib
import functools
import io
import os
import re
import sys
import tempfile
import tokenize
from concurrent.futures import ProcessPoolExecutor
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from censor._core import Mode, strip_source, verify

#: Directories never descended into (in addition to ``--exclude`` globs).
SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "build", "dist",
     ".tox", ".nox", ".eggs", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)

#: Below this many files the process pool costs more than it saves.
SERIAL_THRESHOLD = 20

UNCHANGED = "unchanged"
CHANGED = "changed"
SKIPPED = "skipped"  # could not be tokenized/parsed/decoded; left untouched
FAILED = "failed"  # verification refused the result; left untouched


class Result(NamedTuple):
    path: str
    status: str
    message: "Optional[str]" = None
    diff: "Optional[str]" = None


def _excluded(path: str, name: str, excludes: "Sequence[str]") -> bool:
    return any(fnmatch(path, g) or fnmatch(name, g) for g in excludes)


def _discover(
    paths: "Iterable[Path]", excludes: "Sequence[str]"
) -> "Tuple[List[str], List[str]]":
    """Return (python files, missing arguments), both sorted and deduplicated."""
    files = set()
    missing = []
    for root in paths:
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d
                    for d in dirnames
                    if d not in SKIP_DIRS
                    and not _excluded(os.path.join(dirpath, d), d, excludes)
                )
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if name.endswith(".py") and not _excluded(full, name, excludes):
                        files.add(full)
        elif root.is_file():
            # Explicitly named files are taken as-is, .py suffix or not.
            files.add(str(root))
        else:
            missing.append(str(root))
    return sorted(files), missing


def _atomic_write(path: str, data: bytes) -> None:
    parent, name = os.path.split(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=name + ".", suffix=".censor-tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, os.stat(path).st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Config(NamedTuple):
    mode: str  # Mode.value; plain data keeps the pool pickling trivial
    keep: "Optional[str]"
    default_keeps: bool
    write: bool
    want_diff: bool


def _process_one(cfg: _Config, path: str) -> Result:
    # Rewrite symlinks' targets rather than replacing the links themselves.
    path = os.path.realpath(path)
    try:
        raw = Path(path).read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        src = raw.decode(encoding)
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return Result(path, SKIPPED, "cannot read: %s" % exc)
    if src.count("\r") != src.count("\r\n"):
        # Lone-CR line endings would desynchronise our \n-based line view
        # from the tokenizer's; vanishingly rare, so refuse rather than risk.
        return Result(path, SKIPPED, "lone-CR line endings are not supported")
    keep = re.compile(cfg.keep) if cfg.keep is not None else None
    try:
        stripped = strip_source(src, Mode(cfg.mode), keep, default_keeps=cfg.default_keeps)
    except (SyntaxError, tokenize.TokenError, ValueError) as exc:
        return Result(path, SKIPPED, "cannot parse: %s" % exc)
    except Exception as exc:  # never let an engine bug touch the file
        return Result(path, FAILED, "internal error: %r" % exc)
    if stripped == src:
        return Result(path, UNCHANGED)
    try:
        ok = verify(src, stripped, Mode(cfg.mode))
    except Exception:
        ok = False
    if not ok:
        return Result(path, FAILED, "verification failed; file left untouched")
    diff = None
    if cfg.want_diff:
        diff = "".join(
            difflib.unified_diff(
                src.splitlines(keepends=True),
                stripped.splitlines(keepends=True),
                fromfile=path,
                tofile=path,
            )
        )
    if cfg.write:
        try:
            _atomic_write(path, stripped.encode(encoding))
        except OSError as exc:
            return Result(path, FAILED, "cannot write: %s" % exc)
    return Result(path, CHANGED, None, diff)


def _run(cfg: _Config, files: "List[str]", jobs: int) -> "Iterator[Result]":
    worker = functools.partial(_process_one, cfg)
    if jobs <= 1 or len(files) < SERIAL_THRESHOLD:
        return map(worker, files)

    def parallel() -> "Iterator[Result]":
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            chunksize = max(1, len(files) // (jobs * 4))
            for result in pool.map(worker, files, chunksize=chunksize):
                yield result

    return parallel()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="censor",
        description="Delete comments (and optionally docstrings) from Python code. "
        "Files are rewritten in place, atomically, and only when the result "
        "provably preserves the program; anything that cannot be proven safe "
        "is left untouched and reported.",
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH",
                        help="files or directories to strip")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--docstrings", dest="mode", action="store_const",
                       const=Mode.DOCSTRINGS,
                       help="also delete module/class/function docstrings")
    group.add_argument("--all", dest="mode", action="store_const", const=Mode.ALL,
                       help="delete every comment, including trailing ones")
    parser.set_defaults(mode=Mode.OWN_LINE)
    parser.add_argument("--diff", action="store_true",
                        help="print unified diffs instead of writing")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if any file would change")
    parser.add_argument("--keep", metavar="REGEX",
                        help="also keep comments matching this regex")
    parser.add_argument("--no-default-keeps", action="store_true",
                        help="do not keep # noqa / # type: / # pragma: "
                        "and friends (shebang and coding lines always survive)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip paths matching this glob (repeatable)")
    parser.add_argument("--jobs", type=int, metavar="N",
                        help="worker processes (default: number of CPUs)")
    from censor import __version__

    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    return parser


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.keep is not None:
        try:
            re.compile(ns.keep)
        except re.error as exc:
            parser.error("invalid --keep regex: %s" % exc)
    files, missing = _discover(ns.paths, ns.exclude)
    for path in missing:
        print("censor: no such file or directory: %s" % path, file=sys.stderr)
    if not files:
        print("censor: no Python files found", file=sys.stderr)
        return 2 if missing else 0

    write = not (ns.diff or ns.check)
    cfg = _Config(ns.mode.value, ns.keep, not ns.no_default_keeps, write, ns.diff)
    jobs = ns.jobs if ns.jobs is not None else os.cpu_count() or 1

    counts = {UNCHANGED: 0, CHANGED: 0, SKIPPED: 0, FAILED: 0}
    for result in _run(cfg, files, jobs):
        counts[result.status] += 1
        if result.diff:
            sys.stdout.write(result.diff)
        if result.status == CHANGED and ns.check and not ns.diff:
            print(result.path)
        if result.message:
            print("censor: %s: %s" % (result.path, result.message), file=sys.stderr)

    verb = "would change" if ns.check or ns.diff else "changed"
    print(
        "censor: %d file%s: %d %s, %d unchanged, %d skipped, %d failed"
        % (len(files), "s" if len(files) != 1 else "", counts[CHANGED], verb,
           counts[UNCHANGED], counts[SKIPPED], counts[FAILED]),
        file=sys.stderr,
    )
    if counts[FAILED] or counts[SKIPPED] or missing:
        return 2
    if ns.check and counts[CHANGED]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
