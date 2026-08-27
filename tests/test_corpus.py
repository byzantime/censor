"""Run every target set over the running interpreter's stdlib as a fuzz
corpus.

Every file must strip, verify, and be idempotent under a second pass.
"""

import io
import pathlib
import sysconfig
import tokenize

import pytest

from censor import ALL_TARGETS
from censor import DOCSTRINGS
from censor import ORPHAN_STRINGS
from censor import OWN_LINE
from censor import TARGETS
from censor import TRAILING
from censor import strip_source
from censor import verify

_SKIP_PARTS = {
    "test",
    "tests",
    "idle_test",
    "idlelib",
    "site-packages",
    "lib2to3",
}


def _corpus():
    stdlib = pathlib.Path(sysconfig.get_paths()["stdlib"])
    return [
        p
        for p in sorted(stdlib.rglob("*.py"))
        if not _SKIP_PARTS.intersection(p.parts)
    ]


_TARGET_SETS = [
    frozenset({OWN_LINE}),
    frozenset({TRAILING}),
    frozenset({ORPHAN_STRINGS}),
    ALL_TARGETS,
    frozenset(TARGETS),
]


@pytest.mark.parametrize("targets", _TARGET_SETS, ids=sorted)
def test_stdlib_corpus(targets):
    files = _corpus()
    assert len(files) > 200, "stdlib corpus unexpectedly small"
    failures = []
    for path in files:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        src = raw.decode(encoding)
        try:
            stripped = strip_source(src, targets)
        except Exception as exc:
            failures.append("%s: strip raised %r" % (path, exc))
            continue
        if not verify(src, stripped, targets):
            failures.append("%s: failed verification" % path)
            continue
        if strip_source(stripped, targets) != stripped:
            failures.append("%s: not idempotent" % path)
    assert not failures, "\n".join(failures)


def test_stdlib_corpus_docstrings_only():
    targets = frozenset({DOCSTRINGS})
    for path in _corpus()[:50]:
        src = path.read_bytes().decode("utf-8", errors="replace")
        try:
            stripped = strip_source(src, targets)
        except Exception:
            continue
        assert verify(src, stripped, targets), str(path)
        assert strip_source(stripped, targets) == stripped
