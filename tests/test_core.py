import re
import textwrap

import pytest

from censor import Mode
from censor import _cli
from censor import strip_source
from censor import verify
from censor._cli import main


def strip(src, mode=Mode.OWN_LINE, **kw):
    out = strip_source(src, mode, **kw)
    assert verify(src, out, mode), "stripped output failed verification"
    return out


# --- own-line mode (the default) -------------------------------------------


def test_own_line_deleted_trailing_kept():
    src = "# banner\nx = 1  # trailing\n    # indented\ny = 2\n"
    assert strip(src) == "x = 1  # trailing\ny = 2\n"


def test_own_line_inside_multiline_expression():
    src = textwrap.dedent("""\
        x = [
            1,  # one
            # own-line inside brackets
            2,
        ]
        """)
    expected = "x = [\n    1,  # one\n    2,\n]\n"
    assert strip(src) == expected


def test_comment_only_file_becomes_empty():
    assert strip("# a\n# b\n") == ""
    assert strip("# no trailing newline") == ""


def test_empty_and_commentless_sources_unchanged():
    assert strip("") == ""
    src = "x = 1\ny = 2\n"
    assert strip(src) is src


def test_own_line_comment_at_eof_without_newline():
    assert strip("x = 1\n# tail") == "x = 1\n"


def test_crlf_line_endings_preserved():
    src = "# gone\r\nx = 1  # kept\r\nif x:\r\n    # gone too\r\n    y = 2\r\n"
    assert strip(src) == "x = 1  # kept\r\nif x:\r\n    y = 2\r\n"


def test_form_feed_does_not_desync_lines():
    src = "x = 1\n\f# page two\ny = 2\n"
    assert strip(src) == "x = 1\ny = 2\n"


# --- all mode ---------------------------------------------------------------


def test_all_removes_trailing_comment_and_padding():
    src = "x = 1    # trailing\n# own\ny = 2\n"
    assert strip(src, Mode.ALL) == "x = 1\ny = 2\n"


def test_all_trailing_comment_at_eof_without_newline():
    assert strip("x = 1  # c", Mode.ALL) == "x = 1"


def test_all_keeps_crlf_of_edited_line():
    assert strip("x = 1  # c\r\ny = 2\r\n", Mode.ALL) == "x = 1\r\ny = 2\r\n"


def test_all_comment_after_open_bracket():
    src = "foo = dict(  # opening\n    a=1,\n)\n"
    assert strip(src, Mode.ALL) == "foo = dict(\n    a=1,\n)\n"


# --- docstrings mode --------------------------------------------------------


def test_docstrings_removed_everywhere():
    src = textwrap.dedent('''\
        """Module docstring."""
        # comment
        import os


        class C:
            """Class docstring."""

            def method(self):
                """Method docstring.

                Spanning lines.
                """
                return os


        async def f():
            """Async docstring."""
        ''')
    out = strip(src, Mode.DOCSTRINGS)
    assert '"""' not in out
    assert "# comment" not in out
    # sole-statement bodies got a pass at the docstring's indentation
    assert "class C:\n\n    def method(self):\n        return os" in out
    assert "async def f():\n    pass\n" in out


def test_sole_docstring_module_gets_no_pass():
    assert strip('"""Only a docstring."""\n', Mode.DOCSTRINGS) == ""


def test_sole_docstring_class_and_def_get_pass():
    src = 'class C:\n    """Doc."""\n'
    assert strip(src, Mode.DOCSTRINGS) == "class C:\n    pass\n"
    src = 'def f():\n    """Doc.\n\n    More.\n    """\n'
    assert strip(src, Mode.DOCSTRINGS) == "def f():\n    pass\n"


def test_pass_line_keeps_crlf():
    src = 'def f():\r\n    """Doc."""\r\n'
    assert strip(src, Mode.DOCSTRINGS) == "def f():\r\n    pass\r\n"


def test_docstring_sharing_a_line_with_code_is_kept():
    src = 'def f(): "doc"\n'
    assert strip(src, Mode.DOCSTRINGS) == src
    src = 'def f():\n    "doc"; x = 1\n'
    assert strip(src, Mode.DOCSTRINGS) == src


def test_docstring_with_trailing_comment_is_kept():
    src = 'def f():\n    """doc"""  # trailing\n    return 1\n'
    assert strip(src, Mode.DOCSTRINGS) == src


def test_docstrings_mode_keeps_trailing_comments():
    src = "x = 1  # trailing\ny = 2\n"
    assert strip(src, Mode.DOCSTRINGS) == src


def test_non_docstring_string_statement_untouched():
    src = 'x = 1\n"""just a string in the middle."""\ny = 2\n'
    assert strip(src, Mode.DOCSTRINGS) == src


def test_fstring_first_statement_is_not_a_docstring():
    src = 'f"""not a docstring {1}"""\nx = 1\n'
    assert strip(src, Mode.DOCSTRINGS) == src


# --- always-preserved comments ---------------------------------------------

SHEBANG_SRC = "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n# normal\nx = 1  # t\n"


@pytest.mark.parametrize("mode", list(Mode))
def test_shebang_and_coding_survive_every_mode(mode):
    out = strip(SHEBANG_SRC, mode, default_keeps=False)
    assert out.startswith("#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n")
    assert "# normal" not in out


@pytest.mark.parametrize(
    "pragma",
    [
        "# noqa",
        "# noqa: E501",
        "#noqa",
        "# NOQA",
        "# type: ignore",
        "# fmt: off",
        "# isort:skip",
        "# ruff: noqa",
        "# mypy: disallow-untyped-defs",
        "# pyright: ignore[reportGeneralTypeIssues]",
        "# pragma: no cover",
    ],
)
def test_default_pragmas_kept_even_in_all_mode(pragma):
    src = "x = 1  %s\n" % pragma
    assert strip(src, Mode.ALL) == src


def test_pragma_lookalikes_are_deleted():
    assert strip("x = 1  # noqasaurus\n", Mode.ALL) == "x = 1\n"


def test_no_default_keeps_deletes_pragmas():
    src = "x = 1  # noqa\n# fmt: off\n"
    assert strip(src, Mode.ALL, default_keeps=False) == "x = 1\n"


def test_keep_regex():
    src = "# KEEP: license\n# normal\nx = 1\n"
    out = strip(src, keep=re.compile(r"KEEP"))
    assert out == "# KEEP: license\nx = 1\n"


# --- verification gate ------------------------------------------------------


def test_verify_rejects_code_deletion():
    src = "x = 1\ny = 2\n"
    assert not verify(src, "x = 1\n", Mode.OWN_LINE)
    assert not verify(src, "x = 1\n", Mode.DOCSTRINGS)


def test_verify_rejects_code_mutation():
    assert not verify("x = 1\n", "x = 2\n", Mode.OWN_LINE)


def test_verify_accepts_comment_deletion_only():
    src = "# c\nx = 1\n"
    assert verify(src, "x = 1\n", Mode.OWN_LINE)


def test_idempotent():
    src = SHEBANG_SRC + 'def f():\n    """doc"""\n    # inner\n    return 1\n'
    for mode in Mode:
        once = strip(src, mode)
        assert strip(once, mode) == once


# --- CLI --------------------------------------------------------------------


def test_cli_default_strips_in_place(tmp_path, capsys):
    f = tmp_path / "a.py"
    f.write_text("# gone\nx = 1  # stays\n")
    assert main([str(tmp_path)]) == 0
    assert f.read_text() == "x = 1  # stays\n"
    assert "1 changed" in capsys.readouterr().err


def test_cli_all_mode(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1  # gone\n")
    assert main(["--all", str(f)]) == 0
    assert f.read_text() == "x = 1\n"


def test_cli_symlink_target_is_rewritten_not_replaced(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("# gone\nx = 1\n")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    assert main([str(link)]) == 0
    assert link.is_symlink()
    assert target.read_text() == "x = 1\n"


def test_cli_check_does_not_write(tmp_path, capsys):
    f = tmp_path / "a.py"
    src = "# gone\nx = 1\n"
    f.write_text(src)
    assert main(["--check", str(f)]) == 1
    assert f.read_text() == src
    assert str(f) in capsys.readouterr().out


def test_cli_check_clean_exits_zero(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    assert main(["--check", str(f)]) == 0


def test_cli_diff_previews_without_writing(tmp_path, capsys):
    f = tmp_path / "a.py"
    src = "# gone\nx = 1\n"
    f.write_text(src)
    assert main(["--diff", str(f)]) == 0
    out = capsys.readouterr().out
    assert "-# gone" in out
    assert f.read_text() == src


def test_cli_keep_and_no_default_keeps(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("# KEEP me\n# noqa: file-level\nx = 1\n")
    assert main(["--keep", "KEEP", "--no-default-keeps", str(f)]) == 0
    assert f.read_text() == "# KEEP me\nx = 1\n"


def test_cli_bad_keep_regex_errors(tmp_path, capsys):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    with pytest.raises(SystemExit) as exc:
        main(["--keep", "(", str(f)])
    assert exc.value.code == 2


def test_cli_exclude_and_skip_dirs(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("# gone\n")
    (tmp_path / "migrations").mkdir()
    skipped = tmp_path / "migrations" / "b.py"
    skipped.write_text("# stays\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    venved = tmp_path / ".venv" / "lib" / "c.py"
    venved.write_text("# stays\n")
    assert main(["--exclude", "migrations", str(tmp_path)]) == 0
    assert (tmp_path / "pkg" / "a.py").read_text() == ""
    assert skipped.read_text() == "# stays\n"
    assert venved.read_text() == "# stays\n"


def test_cli_syntax_error_file_untouched(tmp_path, capsys):
    f = tmp_path / "bad.py"
    src = "def broken(:\n    # comment\n"
    f.write_text(src)
    assert main([str(f)]) == 2
    assert f.read_text() == src
    assert "skipped" in capsys.readouterr().err


def test_cli_non_utf8_roundtrip(tmp_path):
    f = tmp_path / "latin.py"
    raw = "# -*- coding: latin-1 -*-\n# gone\ns = 'caf\xe9'\n".encode("latin-1")
    f.write_bytes(raw)
    assert main([str(f)]) == 0
    assert f.read_bytes() == "# -*- coding: latin-1 -*-\ns = 'caf\xe9'\n".encode("latin-1")


def test_cli_utf8_bom_preserved(tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes("\ufeff# gone\nx = 1\n".encode("utf-8"))
    assert main([str(f)]) == 0
    assert f.read_bytes() == "\ufeffx = 1\n".encode("utf-8")


def test_cli_lone_cr_file_skipped(tmp_path):
    f = tmp_path / "mac.py"
    raw = b"# c\rx = 1\r"
    f.write_bytes(raw)
    assert main([str(f)]) == 2
    assert f.read_bytes() == raw


def test_cli_verification_failure_leaves_file_untouched(tmp_path, monkeypatch, capsys):
    f = tmp_path / "a.py"
    src = "# gone\nx = 1\n"
    f.write_text(src)
    monkeypatch.setattr(_cli, "strip_source", lambda *a, **k: "y = 2\n")
    assert main([str(f)]) == 2
    assert f.read_text() == src
    assert "verification failed" in capsys.readouterr().err


def test_cli_engine_crash_leaves_file_untouched(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    src = "# gone\nx = 1\n"
    f.write_text(src)

    def boom(*a, **k):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(_cli, "strip_source", boom)
    assert main([str(f)]) == 2
    assert f.read_text() == src


def test_cli_missing_path(tmp_path, capsys):
    assert main([str(tmp_path / "nope.py")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_cli_preserves_file_mode(tmp_path):
    f = tmp_path / "exec.py"
    f.write_text("#!/usr/bin/env python\n# gone\nx = 1\n")
    f.chmod(0o755)
    assert main([str(f)]) == 0
    assert f.stat().st_mode & 0o777 == 0o755
    assert f.read_text() == "#!/usr/bin/env python\nx = 1\n"
