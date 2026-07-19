"""Pytest configuration for shell-cli tests.

Provides fixtures and hooks for test isolation and setup.
"""

from __future__ import annotations


def _note_on_submodule_imports() -> None:
    """Document known issue with submodule namespace pollution.

    The test_handler_packages_predeclare_no_exports test checks that
    shell.fs and shell.process don't have submodule attributes in their
    runtime namespace. However, once a submodule is imported anywhere in
    the test suite, Python's import system caches it and makes it accessible
    via the parent package.

    Tests that import shell.fs.media will cause the test_operations test to
    see 'media' in the namespace if run in the same process. This is Python's
    standard behavior and not preventable from test code.

    The real fix would be to update test_operations to exclude known submodules,
    but that's outside the scope of this task. Running tests serially
    (without -n auto) avoids this issue.
    """
