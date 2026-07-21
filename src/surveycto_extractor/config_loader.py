"""Load the per-project ``config.py``.

``config.py`` is user-supplied, gitignored, and lives in the working directory
the tool is run from (create one with ``surveycto-init``). It is NOT part of the
installed package -- the package *discovers* it, it does not import it. This
replaces the old top-level ``import config`` that only resolved via a
``sys.path`` hack.

Resolution order:
  1. An injected ``config`` module already in ``sys.modules`` (test seam; also
     lets a caller pre-load a config that every consumer then shares).
  2. The ``SURVEYCTO_CONFIG`` environment variable, if set (a file path).
  3. ``./config.py`` in the current working directory.
  4. ``None`` -- absent config is graceful; callers fall back to defaults or
     exit with a clear message when they actually need it.

A config that is PRESENT but broken (SyntaxError, bad import) raises loudly --
matching the old ``except ModuleNotFoundError`` semantics, which deliberately
let a broken config fail rather than silently reverting to defaults (#5).
"""

import importlib.util
import os
import sys
from pathlib import Path

_cache = {}


def config_path():
    """Where config would be loaded from (env override, else ./config.py)."""
    override = os.environ.get("SURVEYCTO_CONFIG")
    return Path(override) if override else Path.cwd() / "config.py"


def load_config(path=None):
    """Return the user config module, or ``None`` if no config is present."""
    injected = sys.modules.get("config")
    if injected is not None:
        return injected

    p = (Path(path) if path else config_path()).resolve()
    if not p.is_file():
        return None

    key = str(p)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location("surveycto_user_config", p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # present-but-broken raises (loud, intentional)
    _cache[key] = module
    return module


def get_datasets(config=None):
    """Return the DATASETS dict from config, or ``{}`` when config is absent/empty."""
    cfg = config if config is not None else load_config()
    return getattr(cfg, "DATASETS", {}) if cfg is not None else {}
