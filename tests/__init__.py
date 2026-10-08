"""Test suite for ufo.

    python -m pytest tests -q
    python -m unittest discover tests -v

Everything here runs without a desktop session, without network access and
without an API key. The action layer is tested against fakes; the safety
layer, config, scheduling and decision validation are tested directly.

Isolation
---------
`UFO_HOME` is redirected to a throwaway directory before any test module is
imported. `Config.load()` reads `~/.ufo/config.json` by default — correctly,
since that is what makes `ufo config allow` persist — which means without this
the suite would pick up the developer's real allowlist, budgets and
`screenshot_on_every_step` setting, and would try to capture the screen and
talk to a model while running. `UFO_CONFIG` is cleared for the same reason: a
value inherited from the shell would override everything.

Tests that care about a real config file should build a `Config` explicitly or
point `UFO_HOME` at a temp directory of their own.
"""
import atexit
import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="ufo-tests-")

os.environ["UFO_HOME"] = _SANDBOX
os.environ.pop("UFO_CONFIG", None)
os.environ.pop("UFO_SAFETY_DRY_RUN", None)

atexit.register(lambda: __import__("shutil").rmtree(_SANDBOX, ignore_errors=True))
