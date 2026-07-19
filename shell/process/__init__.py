"""Process operations: argv vectors and raw host shell strings.

**Deliberately empty.** Like :mod:`shell.fs`, this package marker exports
nothing until the handlers behind the names exist. A stub that raises is worse
than an import error: it looks like a capability right up to the moment it is
used.

When the handlers land, note that the two live here for a reason and are not
interchangeable. An argv vector is what a ``control``-profile operation may use;
a raw shell string is a ``project``-profile construct only, because the gate can
only inspect a string that a shell will later re-interpret.
"""

__all__: tuple[str, ...] = ()
