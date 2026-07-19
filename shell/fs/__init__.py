"""Confined filesystem operations.

**Deliberately empty.** This package marker exports nothing, and that is the
design: predeclaring names before the modules behind them exist would mean
shipping either fake exports or stubs that raise, and a consumer cannot tell
either apart from a real capability until it calls one.

When the read, write, edit, list, stat and media handlers land, consumers import
their modules explicitly (``from shell.fs import read``) rather than relying on
a curated re-export surface here.
"""

__all__: tuple[str, ...] = ()
