"""
What this plugin calls itself.

Here rather than in ``setup.py`` alone because the number has to be readable
at runtime: it travels to every paired machine in the sync response's
``server`` block, which is what lets a technician standing at a country
server answer "what is the instance running?" without a Wagtail login.

``setup.py`` reads this file, so the packaged version and the imported one
cannot disagree. ``importlib.metadata`` answers a different question -- what
was *installed* -- which after an editable install off a working tree, or a
container somebody patched in place, is no longer what is running.

A bare string, matching the git tag verbatim. Plugin repos tag without a
leading ``v`` because a ``plugins.toml`` entry pins the tag as written, so
the string here and the string in the manifest are the same characters.
"""

#: Bumped at release, in step with the tag. See the release convention in
#: the ADL core CLAUDE.md: ``gh release create 0.5.0``, never ``git tag -a``.
VERSION = "0.4.0"

__version__ = VERSION
