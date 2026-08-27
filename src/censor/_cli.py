"""Command-line interface: discovery, parallelism, atomic in-place writes."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import functools
import io
import os
import re
import shlex
import sys
import tempfile
import tokenize
import tomllib
from concurrent.futures import ProcessPoolExecutor
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable
from typing import Iterator
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import Pattern
from typing import Sequence
from typing import Tuple

from censor._core import ALL_TARGETS
from censor._core import TARGETS
from censor._core import docstring_violations
from censor._core import strip_source
from censor._core import verify

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        ".tox",
        ".nox",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

SERIAL_THRESHOLD = 20

UNCHANGED = "unchanged"
CHANGED = "changed"
SKIPPED = "skipped"
FAILED = "failed"

PROJECT_ROOT_MARKERS = frozenset({".git", ".hg"})

CONFIG_KEYS = {
    "delete": list,
    "skip": list,
    "keep": list,
    "default-keeps": bool,
    "exclude": list,
}


def _read_toml(path: Path, parser: argparse.ArgumentParser) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        parser.error("cannot read %s: %s" % (path, exc))
    except tomllib.TOMLDecodeError as exc:
        parser.error("invalid TOML in %s: %s" % (path, exc))


def _explicit_config(
    config_path: Path, parser: argparse.ArgumentParser
) -> dict:
    """Read ``[tool.censor]`` (or a bare top-level ``[censor]``)."""
    data = _read_toml(config_path, parser)
    table = data.get("censor", data.get("tool", {}).get("censor"))
    if table is None:
        return {}
    if not isinstance(table, dict):
        parser.error(
            "%s: [tool.censor] must be a table, got %r" % (config_path, table)
        )
    return _validate_config(table, str(config_path), parser)


def _load_config(
    paths: "Sequence[Path]",
    config_path: "Optional[Path]",
    isolated: bool,
    parser: argparse.ArgumentParser,
) -> dict:
    """Load ``[tool.censor]`` settings, black-style.

    With no explicit *config_path*, walk up from the common ancestor of
    *paths*; the first directory whose ``pyproject.toml`` contains a
    ``[tool.censor]`` table wins. Discovery stops at a project root (a
    directory holding ``.git``/``.hg``) or the filesystem root.
    """
    if isolated:
        return {}
    if config_path is not None:
        return _explicit_config(config_path, parser)
    try:
        anchor = Path(os.path.commonpath([str(p) for p in paths]))
    except ValueError:
        return {}
    for directory in [anchor, *anchor.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            table = _read_toml(candidate, parser).get("tool", {}).get("censor")
            if table is not None:
                return _validate_config(table, str(candidate), parser)
        if any((directory / m).exists() for m in PROJECT_ROOT_MARKERS):
            break
    return {}


def _validate_list_key(
    key: str, entries: list, source: str, parser: argparse.ArgumentParser
) -> None:
    """Every entry of a list-valued key must be a string; ``keep`` entries
    must additionally compile as regexes."""
    for entry in entries:
        if not isinstance(entry, str):
            parser.error(
                "%s: [tool.censor] %s entries must be strings, got %r"
                % (source, key, entry)
            )
        if key == "keep":
            try:
                re.compile(entry)
            except re.error as exc:
                parser.error(
                    "%s: [tool.censor] keep entry %r is not a valid "
                    "regex: %s" % (source, entry, exc)
                )


def _validate_config(
    table: dict, source: str, parser: argparse.ArgumentParser
) -> dict:
    unknown = sorted(set(table) - set(CONFIG_KEYS))
    if unknown:
        parser.error(
            "unknown key%s in [tool.censor] (%s): %s; valid keys are: %s"
            % (
                "s" if len(unknown) != 1 else "",
                source,
                ", ".join(unknown),
                ", ".join(sorted(CONFIG_KEYS)),
            )
        )
    for key, value in table.items():
        expected = CONFIG_KEYS[key]
        if not isinstance(value, expected):
            parser.error(
                "%s: [tool.censor] %s must be a %s, got %r"
                % (
                    source,
                    key,
                    {str: "string", list: "list", bool: "boolean"}[expected],
                    value,
                )
            )
        if key in ("keep", "exclude", "delete", "skip"):
            _validate_list_key(key, value, source, parser)
        if key in ("delete", "skip"):
            bad = sorted(set(value) - TARGETS)
            if bad:
                parser.error(
                    "%s: [tool.censor] %s entries must be one of: %s (got %s)"
                    % (
                        source,
                        key,
                        ", ".join(sorted(TARGETS)),
                        ", ".join(bad),
                    )
                )
    return table


class Result(NamedTuple):
    path: str
    status: str
    message: "Optional[str]" = None
    diff: "Optional[str]" = None
    violations: "Tuple[str, ...]" = ()


def _excluded(path: str, name: str, excludes: "Sequence[str]") -> bool:
    return any(fnmatch(path, g) or fnmatch(name, g) for g in excludes)


def _discover(
    paths: "Iterable[Path]", excludes: "Sequence[str]"
) -> "Tuple[List[str], List[str]]":
    """Return (python files, missing arguments), sorted and deduplicated."""
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
                    if name.endswith(".py") and not _excluded(
                        full, name, excludes
                    ):
                        files.add(full)
        elif root.is_file():
            files.add(str(root))
        else:
            missing.append(str(root))
    return sorted(files), missing


def _atomic_write(path: str, data: bytes) -> None:
    parent, name = os.path.split(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(
        dir=parent, prefix=name + ".", suffix=".censor-tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        with contextlib.suppress(OSError):
            os.chmod(tmp, os.stat(path).st_mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class _Config(NamedTuple):
    targets: "Tuple[str, ...]"
    keep: "Optional[str]"
    default_keeps: bool
    write: bool
    want_diff: bool
    max_doc_lines: "Optional[int]" = None


def _read_source(path: str) -> "Tuple[str, str]":
    """Read *path* and decode it per its PEP 263 coding declaration.

    Raises OSError/SyntaxError/UnicodeDecodeError/LookupError on failure.
    """
    raw = Path(path).read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding), encoding


def _verified(
    src: str, stripped: str, cfg: _Config, keep: "Optional[Pattern[str]]"
) -> bool:
    """Whether *stripped* passes the gate.  *keep* is the compiled cfg.keep.

    It must be the very pattern the strip used, or the two sides disagree
    about which comments were deletable.
    """
    try:
        return bool(
            verify(
                src,
                stripped,
                cfg.targets,
                keep=keep,
                default_keeps=cfg.default_keeps,
            )
        )
    except Exception:
        return False


def _diff(path: str, src: str, stripped: str) -> str:
    return "".join(
        difflib.unified_diff(
            src.splitlines(keepends=True),
            stripped.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )


def _doc_violation_lines(
    cfg: _Config, path: str, src: str
) -> "Tuple[str, ...]":
    """Formatted --max-doc-lines violations for *src* (empty when unset)."""
    if cfg.max_doc_lines is None:
        return ()
    vs = docstring_violations(src, cfg.max_doc_lines)
    return tuple(
        "%s:%d: docstring of '%s' has %d lines (limit %d)"
        % (path, v.lineno, v.name, v.lines, cfg.max_doc_lines)
        for v in vs
    )


def _finish(
    cfg: _Config,
    path: str,
    src: str,
    stripped: str,
    encoding: str,
    violations: "Tuple[str, ...]",
) -> Result:
    """Diff and/or write *stripped* after verification already passed."""
    diff = None
    if cfg.want_diff:
        diff = _diff(path, src, stripped)
    if cfg.write:
        try:
            _atomic_write(path, stripped.encode(encoding))
        except OSError as exc:
            return Result(
                path, FAILED, "cannot write: %s" % exc, None, violations
            )
    return Result(path, CHANGED, None, diff, violations)


def _process_one(cfg: _Config, path: str) -> Result:
    path = os.path.realpath(path)
    try:
        src, encoding = _read_source(path)
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return Result(path, SKIPPED, "cannot read: %s" % exc)
    if src.count("\r") != src.count("\r\n"):
        return Result(path, SKIPPED, "lone-CR line endings are not supported")
    keep = re.compile(cfg.keep) if cfg.keep is not None else None
    try:
        violations = _doc_violation_lines(cfg, path, src)
    except SyntaxError as exc:
        return Result(path, SKIPPED, "cannot parse: %s" % exc)
    try:
        stripped = strip_source(
            src, cfg.targets, keep, default_keeps=cfg.default_keeps
        )
    except (SyntaxError, tokenize.TokenError, ValueError) as exc:
        return Result(path, SKIPPED, "cannot parse: %s" % exc, None, violations)
    except Exception as exc:
        return Result(
            path, FAILED, "internal error: %r" % exc, None, violations
        )
    if stripped == src:
        return Result(path, UNCHANGED, None, None, violations)
    if not _verified(src, stripped, cfg, keep):
        return Result(
            path,
            FAILED,
            "verification failed; file left untouched",
            None,
            violations,
        )
    return _finish(cfg, path, src, stripped, encoding, violations)


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


def _build_shared_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="files or directories to strip",
    )
    parser.add_argument(
        "--delete",
        action="append",
        choices=sorted(TARGETS),
        metavar="CAT",
        help="delete this category instead of the default (%s); one of %s, "
        "repeatable to select several"
        % (", ".join(sorted(ALL_TARGETS)), ", ".join(sorted(TARGETS))),
    )
    parser.add_argument(
        "--skip",
        action="append",
        choices=sorted(TARGETS),
        metavar="CAT",
        help="keep this category despite the selection (repeatable)",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="print unified diffs instead of writing",
    )
    parser.add_argument(
        "--max-doc-lines",
        type=int,
        metavar="N",
        help="report docstrings longer than N content lines; exit 1 if any "
        "(composes with every selection)",
    )
    parser.add_argument(
        "--keep",
        metavar="REGEX",
        action="append",
        help="also keep comments matching REGEX (repeatable)",
    )
    parser.add_argument(
        "--default-keeps",
        action=argparse.BooleanOptionalAction,
        help="keep the built-in pragma preserve-list (# noqa / # type: / "
        "# pragma: and friends); shebang and coding lines always survive "
        "(default: true)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        metavar="GLOB",
        help="skip paths matching this glob (repeatable)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="read configuration from this pyproject.toml instead of "
        "discovering one",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="ignore any pyproject.toml configuration",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        metavar="N",
        help="worker processes (default: number of CPUs)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="censor",
        description="Delete comments (and optionally docstrings) from Python "
        "code. Categories: own-line (a comment alone on its line), trailing "
        "(after code on the same line), orphan-strings (a bare string "
        "statement after an assignment — a docstring that is not one), "
        "docstrings. Default selection: --delete own-line trailing "
        "orphan-strings. Files are rewritten in place, atomically, and only "
        "when the result provably preserves the program; anything that "
        "cannot be proven safe is left untouched and reported.",
    )
    from censor import __version__

    parser.add_argument(
        "--version", action="version", version="%(prog)s " + __version__
    )
    parent = argparse.ArgumentParser(add_help=False)
    _build_shared_options(parent)
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND"
    )
    check = subparsers.add_parser(
        "check",
        parents=[parent],
        help="report what would change, without writing (like ruff check)",
    )
    check.add_argument(
        "--fix",
        action="store_true",
        help="rewrite the offending files in place",
    )
    fmt = subparsers.add_parser(
        "format",
        parents=[parent],
        aliases=["strip"],
        help="strip in place (like black); add --check to only report",
    )
    fmt.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any file would change",
    )
    return parser


_VALUE_FLAGS = (
    "--keep",
    "--exclude",
    "--config",
    "--max-doc-lines",
    "--jobs",
    "--delete",
    "--skip",
)


def _normalise_argv(argv: List[str], parser: argparse.ArgumentParser) -> None:
    """Hoist the command token so flags before it still parse."""
    if any(a in ("-h", "--help", "--version") for a in argv):
        return
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("check", "format", "strip"):
            if i != 0:
                argv.insert(0, argv.pop(i))
            return
        i += 2 if arg in _VALUE_FLAGS else 1
    parser.error(
        "no command given; use `censor check PATH` to report or "
        "`censor format PATH` to rewrite in place"
    )


def _report(
    result: Result,
    ns: argparse.Namespace,
    counts: "dict",
    checking: bool,
) -> None:
    counts[result.status] += 1
    if result.violations:
        counts["violations"] += len(result.violations)
        for line in result.violations:
            print(line)
    if result.diff:
        sys.stdout.write(result.diff)
    if result.status == CHANGED and checking and not ns.diff:
        print("would strip comments from: %s" % result.path)
    if result.message:
        print("censor: %s: %s" % (result.path, result.message), file=sys.stderr)


def _merge(
    ns: argparse.Namespace, config: dict, parser: argparse.ArgumentParser
) -> "Tuple[Tuple[str, ...], Optional[str], bool, List[str]]":
    """Resolve (targets, keep-regex, default_keeps, exclude): config
    supplies defaults, any explicit CLI flag wins outright."""
    if ns.delete is not None:
        selected = set(ns.delete)
    elif "delete" in config:
        selected = set(config["delete"])
    else:
        selected = set(ALL_TARGETS)
    skips = ns.skip if ns.skip is not None else set(config.get("skip") or [])
    selected -= set(skips)
    if not selected:
        parser.error("nothing to delete")
    patterns = (
        list(ns.keep) if ns.keep is not None else list(config.get("keep") or [])
    )
    if not patterns:
        keep = None
    else:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                parser.error("invalid --keep regex %r: %s" % (pattern, exc))
        keep = "|".join("(?:%s)" % p for p in patterns)
    if ns.default_keeps is not None:
        default_keeps = ns.default_keeps
    elif "default-keeps" in config:
        default_keeps = config["default-keeps"]
    else:
        default_keeps = True
    exclude = (
        list(ns.exclude)
        if ns.exclude is not None
        else list(config.get("exclude") or [])
    )
    return tuple(sorted(selected)), keep, default_keeps, exclude


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    parser = _build_parser()
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    _normalise_argv(argv, parser)
    ns = parser.parse_args(argv)
    command = "format" if ns.command == "strip" else ns.command
    if ns.max_doc_lines is not None and ns.max_doc_lines < 1:
        parser.error("--max-doc-lines must be at least 1")
    config = _load_config(ns.paths, ns.config, ns.isolated, parser)
    targets, keep, default_keeps, exclude = _merge(ns, config, parser)
    files, missing = _discover(ns.paths, exclude)
    for path in missing:
        print("censor: no such file or directory: %s" % path, file=sys.stderr)
    if not files:
        print("censor: no Python files found", file=sys.stderr)
        return 2 if missing else 0

    checking = not ns.fix if command == "check" else bool(ns.check)
    write = not checking and not ns.diff
    cfg = _Config(
        targets, keep, default_keeps, write, ns.diff, ns.max_doc_lines
    )
    jobs = ns.jobs if ns.jobs is not None else os.cpu_count() or 1

    counts = {UNCHANGED: 0, CHANGED: 0, SKIPPED: 0, FAILED: 0, "violations": 0}
    for result in _run(cfg, files, jobs):
        _report(result, ns, counts, checking)

    verb = "would change" if checking or ns.diff else "changed"
    summary = (
        "censor: %d file%s: %d %s, %d unchanged, %d skipped, %d failed"
        % (
            len(files),
            "s" if len(files) != 1 else "",
            counts[CHANGED],
            verb,
            counts[UNCHANGED],
            counts[SKIPPED],
            counts[FAILED],
        )
    )
    if ns.max_doc_lines is not None:
        summary += ", %d docstring violations" % counts["violations"]
    print(summary, file=sys.stderr)
    if counts[FAILED] or counts[SKIPPED] or missing:
        return 2
    if checking and counts[CHANGED]:
        rest = [a for a in argv[1:] if a not in ("--check", "--diff", "--fix")]
        rerun = shlex.join(["format", *rest])
        print(
            "censor: %d files contain comments censor would delete."
            % counts[CHANGED],
            file=sys.stderr,
        )
        print("censor: to fix, run: censor %s" % rerun, file=sys.stderr)
        print(
            "censor: (rewrites in place; run with --diff first to preview "
            "the deletions)",
            file=sys.stderr,
        )
    if (checking and counts[CHANGED]) or counts["violations"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
