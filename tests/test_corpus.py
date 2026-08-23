"""Run every mode over the running interpreter's stdlib as a fuzz corpus.

Every file must strip, verify, and be idempotent under a second pass.
"""

import io
import pathlib
import sysconfig
import tokenize

import pytest

from censor import Mode
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


@pytest.mark.parametrize("mode", list(Mode), ids=lambda m: m.value)
def test_stdlib_corpus(mode):
    files = _corpus()
    assert len(files) > 200, "stdlib corpus unexpectedly small"
    failures = []
    for path in files:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        src = raw.decode(encoding)
        try:
            stripped = strip_source(src, mode)
        except Exception as exc:
            failures.append("%s: strip raised %r" % (path, exc))
            continue
        if not verify(src, stripped, mode):
            failures.append("%s: failed verification" % path)
            continue
        if strip_source(stripped, mode) != stripped:
            failures.append("%s: not idempotent" % path)
    assert not failures, "\n".join(failures)
