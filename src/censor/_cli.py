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
from typing import Sequence
from typing import Tuple

from censor._core import Mode
from censor._core import strip_source
from censor._core import verify

#: Directories never descended into (in addition to ``--exclude`` globs).
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

#: Below this many files the process pool costs more than it saves.
SERIAL_THRESHOLD = 20

UNCHANGED = "unchanged"
CHANGED = "changed"
SKIPPED = "skipped"  # could not be tokenized/parsed/decoded; left untouched
FAILED = "failed"  # verification refused the result; left untouched

#: Project-root markers: config discovery stops after a directory with one.
PROJECT_ROOT_MARKERS = frozenset({".git", ".hg"})

#: Keys allowed in ``[tool.censor]`` mapped to the type each value must have.
CONFIG_KEYS = {
    "mode": str,
    "keep": list,
    "default-keeps": bool,
    "exclude": list,
}


def _read_toml(path: Path, parser: argparse.ArgumentParser) -> dict:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
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
        if key == "mode" and value not in tuple(m.value for m in Mode):
            parser.error(
                "%s: [tool.censor] mode must be one of: %s"
                % (source, ", ".join(m.value for m in Mode))
            )
        if key == "keep":
            for pattern in value:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    parser.error(
                        "%s: [tool.censor] keep entry %r is not a valid "
                        "regex: %s" % (source, pattern, exc)
                    )
    return table


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
            # Explicitly named files are taken as-is, .py suffix or not.
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
    mode: str  # Mode.value; plain data keeps the pool pickling trivial
    keep: "Optional[str]"
    default_keeps: bool
    write: bool
    want_diff: bool


def _read_source(path: str) -> "Tuple[str, str]":
    """Read *path* and decode it per its PEP 263 coding declaration.

    Raises OSError/SyntaxError/UnicodeDecodeError/LookupError on failure.
    """
    raw = Path(path).read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding), encoding


def _verified(src: str, stripped: str, mode: Mode) -> bool:
    try:
        return bool(verify(src, stripped, mode))
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


def _process_one(cfg: _Config, path: str) -> Result:
    # Rewrite symlinks' targets rather than replacing the links themselves.
    path = os.path.realpath(path)
    try:
        src, encoding = _read_source(path)
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return Result(path, SKIPPED, "cannot read: %s" % exc)
    if src.count("\r") != src.count("\r\n"):
        # Lone-CR line endings would desynchronise our \n-based line view
        # from the tokenizer's; vanishingly rare, so refuse rather than risk.
        return Result(path, SKIPPED, "lone-CR line endings are not supported")
    keep = re.compile(cfg.keep) if cfg.keep is not None else None
    try:
        stripped = strip_source(
            src, Mode(cfg.mode), keep, default_keeps=cfg.default_keeps
        )
    except (SyntaxError, tokenize.TokenError, ValueError) as exc:
        return Result(path, SKIPPED, "cannot parse: %s" % exc)
    except Exception as exc:  # never let an engine bug touch the file
        return Result(path, FAILED, "internal error: %r" % exc)
    if stripped == src:
        return Result(path, UNCHANGED)
    if not _verified(src, stripped, Mode(cfg.mode)):
        return Result(path, FAILED, "verification failed; file left untouched")
    diff = None
    if cfg.want_diff:
        diff = _diff(path, src, stripped)
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
        description="Delete comments (and optionally docstrings) from Python "
        "code. Files are rewritten in place, atomically, and only when "
        "the result "
        "provably preserves the program; anything that cannot be proven safe "
        "is left untouched and reported.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="files or directories to strip",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--docstrings",
        dest="mode",
        action="store_const",
        const=Mode.DOCSTRINGS,
        help="also delete module/class/function docstrings",
    )
    group.add_argument(
        "--all",
        dest="mode",
        action="store_const",
        const=Mode.ALL,
        help="delete every comment, including trailing ones",
    )
    # None means "not given"; the effective value comes from config
    # discovery, falling back to Mode.OWN_LINE.
    parser.set_defaults(mode=None)
    parser.add_argument(
        "--diff",
        action="store_true",
        help="print unified diffs instead of writing",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any file would change",
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
    from censor import __version__

    parser.add_argument(
        "--version", action="version", version="%(prog)s " + __version__
    )
    return parser


def _report(result: Result, ns: argparse.Namespace, counts: "dict") -> None:
    counts[result.status] += 1
    if result.diff:
        sys.stdout.write(result.diff)
    if result.status == CHANGED and ns.check and not ns.diff:
        print("would strip comments from: %s" % result.path)
    if result.message:
        print("censor: %s: %s" % (result.path, result.message), file=sys.stderr)


def _merge(
    ns: argparse.Namespace, config: dict, parser: argparse.ArgumentParser
) -> "Tuple[Mode, Optional[str], bool, List[str]]":
    """Resolve (mode, keep-regex, default_keeps, exclude): config supplies
    defaults, any explicit CLI flag wins outright."""
    if ns.mode is not None:
        mode = ns.mode
    elif "mode" in config:
        mode = Mode(config["mode"])
    else:
        mode = Mode.OWN_LINE
    patterns = (
        list(ns.keep) if ns.keep is not None else list(config.get("keep") or [])
    )
    if not patterns:
        keep = None
    else:
        # Each pattern is bracketed so alternation can't bleed across them.
        keep = "|".join("(?:%s)" % p for p in patterns)
        try:
            re.compile(keep)
        except re.error as exc:
            parser.error("invalid --keep regex: %s" % exc)
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
    return mode, keep, default_keeps, exclude


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    config = _load_config(ns.paths, ns.config, ns.isolated, parser)
    mode, keep, default_keeps, exclude = _merge(ns, config, parser)
    files, missing = _discover(ns.paths, exclude)
    for path in missing:
        print("censor: no such file or directory: %s" % path, file=sys.stderr)
    if not files:
        print("censor: no Python files found", file=sys.stderr)
        return 2 if missing else 0

    write = not (ns.diff or ns.check)
    cfg = _Config(mode.value, keep, default_keeps, write, ns.diff)
    jobs = ns.jobs if ns.jobs is not None else os.cpu_count() or 1

    counts = {UNCHANGED: 0, CHANGED: 0, SKIPPED: 0, FAILED: 0}
    for result in _run(cfg, files, jobs):
        _report(result, ns, counts)

    verb = "would change" if ns.check or ns.diff else "changed"
    print(
        "censor: %d file%s: %d %s, %d unchanged, %d skipped, %d failed"
        % (
            len(files),
            "s" if len(files) != 1 else "",
            counts[CHANGED],
            verb,
            counts[UNCHANGED],
            counts[SKIPPED],
            counts[FAILED],
        ),
        file=sys.stderr,
    )
    if counts[FAILED] or counts[SKIPPED] or missing:
        return 2
    if ns.check and counts[CHANGED]:
        argv = sys.argv[1:] if argv is None else list(argv)
        rerun = shlex.join(a for a in argv if a not in ("--check", "--diff"))
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
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
