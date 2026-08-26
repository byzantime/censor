"""censor — delete comments (and optionally docstrings) from Python code.

Library use::

    from censor import ALL_TARGETS, strip_source, verify

    stripped = strip_source(src, {"own-line"})
    assert verify(src, stripped, {"own-line"})
"""

from censor._core import ALL_TARGETS
from censor._core import DEFAULT_KEEPS
from censor._core import DOCSTRINGS
from censor._core import ORPHAN_STRINGS
from censor._core import OWN_LINE
from censor._core import TARGETS
from censor._core import TRAILING
from censor._core import DocstringViolation
from censor._core import docstring_violations
from censor._core import OrphanString
from censor._core import orphan_string_violations
from censor._core import strip_orphan_strings
from censor._core import strip_source
from censor._core import verify

__version__ = "0.1.0"

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
    "OrphanString",
    "orphan_string_violations",
    "strip_orphan_strings",
    "__version__",
]
