"""censor — delete comments (and optionally docstrings) from Python code.

Library use::

    from censor import Mode, strip_source, verify

    stripped = strip_source(src, Mode.OWN_LINE)
    assert verify(src, stripped, Mode.OWN_LINE)
"""

from censor._core import DEFAULT_KEEPS
from censor._core import DocstringViolation
from censor._core import Mode
from censor._core import docstring_violations
from censor._core import strip_source
from censor._core import verify

__version__ = "0.1.0"

__all__ = [
    "Mode",
    "strip_source",
    "verify",
    "DEFAULT_KEEPS",
    "DocstringViolation",
    "docstring_violations",
    "__version__",
]
