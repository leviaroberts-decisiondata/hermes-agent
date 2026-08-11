#!/usr/bin/env python3
"""
Regression coverage for the write_file line-numbered-dump guard.

`_looks_like_line_numbered_dump` (WTS 7e1d32e9) exists to stop an agent writing
a `read_file` dump — lines shaped `NN|content` — back over a real file and
corrupting it. `write_file_tool` calls it on every write.

It used `re.match` while `tools/file_tools.py` never imported `re` at module
scope; the only `import re` in the file is a function-local alias inside
`patch_tool`. So the guard raised `NameError` instead of returning a verdict:

  * the corruption guard protected nothing, and
  * it took `write_file` down with it for any content of 5+ non-empty lines
    (fewer than 5 returns early, which is why short writes worked and the bug
    stayed hidden across all 15 gateways).

The guard had no test at all. These cover both halves — that it runs, and that
it still discriminates — so a future refactor cannot silently drop the import
again.

Run with:  python -m pytest tests/tools/test_write_file_line_numbered_guard.py -v
"""

import os
import re
import shutil
import tempfile
import unittest

from tools import file_tools
from tools.file_tools import _looks_like_line_numbered_dump, write_file_tool


ORDINARY_MULTILINE = (
    "def main():\n"
    "    value = compute()\n"
    "    if value:\n"
    "        emit(value)\n"
    "    return value\n"
    "\n"
    "main()\n"
)

READ_FILE_DUMP = (
    "     1|def main():\n"
    "     2|    value = compute()\n"
    "     3|    if value:\n"
    "     4|        emit(value)\n"
    "     5|    return value\n"
    "     6|\n"
    "     7|main()\n"
)


class TestModuleImportsRe(unittest.TestCase):
    """The one-line root cause, asserted directly."""

    def test_re_is_available_at_module_scope(self):
        # `patch_tool` has its own `import re as _re`; that local alias does NOT
        # satisfy `_looks_like_line_numbered_dump`, which reads the global name.
        self.assertTrue(hasattr(file_tools, "re"), "tools.file_tools must import re at module scope")
        self.assertIs(file_tools.re, re)


class TestLineNumberedDumpGuard(unittest.TestCase):
    """The guard must return a verdict, and the verdict must be right."""

    def test_does_not_raise_on_content_long_enough_to_reach_the_regex(self):
        # 5+ non-empty lines is the threshold that used to raise NameError.
        try:
            _looks_like_line_numbered_dump(ORDINARY_MULTILINE)
        except NameError as exc:  # pragma: no cover - the regression itself
            self.fail(f"guard raised instead of returning a verdict: {exc}")

    def test_flags_a_read_file_dump(self):
        self.assertTrue(_looks_like_line_numbered_dump(READ_FILE_DUMP))

    def test_does_not_flag_ordinary_multiline_content(self):
        self.assertFalse(_looks_like_line_numbered_dump(ORDINARY_MULTILINE))

    def test_short_content_is_never_flagged(self):
        # Below the 5-line threshold the guard returns early — this is the path
        # that kept working while every longer write failed.
        self.assertFalse(_looks_like_line_numbered_dump("one|two\nthree|four\n"))

    def test_empty_and_none_content_are_safe(self):
        self.assertFalse(_looks_like_line_numbered_dump(""))
        self.assertFalse(_looks_like_line_numbered_dump(None))

    def test_a_minority_of_pipe_prefixed_lines_is_not_a_dump(self):
        # Markdown tables and log excerpts must stay writable.
        mixed = "intro\n" + "     1|quoted\n" + "body\nmore body\ntail\nend\n"
        self.assertFalse(_looks_like_line_numbered_dump(mixed))


class TestWriteFileToolEndToEnd(unittest.TestCase):
    """The user-visible symptom: writing a normal multi-line file."""

    def setUp(self):
        # NOT bare mkdtemp(): on macOS that lands under /var/folders/..., which
        # write_file's sensitive-path guard correctly refuses — the failure then
        # looks like this guard rather than that one. /tmp resolves to
        # /private/tmp and is allowed.
        self.tmp = tempfile.mkdtemp(dir="/tmp", prefix="hermes-writefile-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_writes_multiline_content_without_nameerror(self):
        target = os.path.join(self.tmp, "regression.py")
        result = write_file_tool(target, ORDINARY_MULTILINE)

        self.assertNotIn("NameError", str(result))
        self.assertNotIn("is not defined", str(result))
        self.assertTrue(os.path.exists(target), f"file was not written: {result}")
        with open(target) as fh:
            self.assertEqual(fh.read(), ORDINARY_MULTILINE)

    def test_still_refuses_a_read_file_dump(self):
        # The guard's actual job — it must reject, not crash and not write.
        target = os.path.join(self.tmp, "would-be-corrupted.py")
        result = write_file_tool(target, READ_FILE_DUMP)

        self.assertNotIn("NameError", str(result))
        self.assertFalse(
            os.path.exists(target) and open(target).read() == READ_FILE_DUMP,
            "a line-numbered dump was written verbatim — the corruption guard is not working",
        )


if __name__ == "__main__":
    unittest.main()
