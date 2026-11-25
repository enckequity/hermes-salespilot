"""Test package setup.

These tests import the plugin's modules top-level, so the plugin's own ``tools.py`` shadows the
engine's ``tools`` package and the engine's "nobody can answer an approval" reader cannot be imported
in this process. Unpatched, ``approval.unattended()`` would then fail closed for every test. Stand in
with "someone can answer"; tests/test_teamshift.py checks the fail-closed paths and runs the real
reader in an engine subprocess.
"""

try:
    import approval as _approval
except ImportError:  # collected from outside the plugin root
    _approval = None

if _approval is not None and _approval._no_user_reader() is None:
    _approval._no_user_reader = lambda: (lambda: False)
