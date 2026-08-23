"""censor — delete comments (and optionally docstrings) from Python code.

Library use::

    from censor import Mode, strip_source, verify

    stripped = strip_source(src, Mode.OWN_LINE)
    assert verify(src, stripped, Mode.OWN_LINE)
"""

from censor._core import DEFAULT_KEEPS, Mode, strip_source, verify

__version__ = "0.1.0"

__all__ = ["Mode", "strip_source", "verify", "DEFAULT_KEEPS", "__version__"]
