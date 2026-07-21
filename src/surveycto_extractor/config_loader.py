"""Load the per-project config (``config.toml`` preferred, ``config.py`` fallback).

The config is user-supplied, gitignored, and lives in the working directory the
tool is run from (create one with ``surveycto-init``). It is NOT part of the
installed package -- the package *discovers* it, it does not import it.

**Format.** The canonical form is declarative **``config.toml``** (parsed, never
executed). A legacy **``config.py``** is still honoured as a fallback so existing
projects keep working during the transition. Either way the loader returns an
object exposing the same attributes consumers read: ``SURVEYS``, ``DATASETS``,
``SURVEY_COLUMNS``, ``CHOICES_COLUMNS``, ``EXCLUDED_TYPES``, ``SYSTEM_PREFIXES``,
and optionally ``SENTINEL_MEANINGS`` / ``SENTINEL_SCAN_ONLY``.

Because TOML is declarative it cannot compute paths (no ``Path(__file__).parent``),
so path-valued entries are **resolved by the loader relative to the config file's
directory** (absolute paths are left as-is).

Resolution order:
  1. An injected ``config`` module already in ``sys.modules`` (test seam; also
     lets a caller pre-load a config that every consumer then shares).
  2. ``SURVEYCTO_CONFIG`` (a file path to a ``.toml`` or ``.py``), if set.
  3. ``./config.toml`` in the current working directory, else ``./config.py``.
  4. ``None`` -- absent config is graceful; callers fall back to defaults or
     exit with a clear message when they actually need it.

A config that is PRESENT but broken (TOML parse error, or a ``.py`` SyntaxError)
raises loudly -- matching the old ``except ModuleNotFoundError`` semantics, which
deliberately let a broken config fail rather than silently reverting (#5).
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # 3.10
    import tomli as tomllib

_cache = {}

# IPA-standard defaults for the "usually identical across projects" fields, so a
# config.toml only has to declare [surveys.*] / [datasets.*]. Override via a
# [columns] table.
_DEFAULT_SURVEY_COLUMNS = [
    "type",
    "name",
    "label",
    "constraint",
    "relevance",
    "required",
    "calculation",
]
_DEFAULT_CHOICES_COLUMNS = ["list_name", "value", "label"]
_DEFAULT_EXCLUDED_TYPES = [
    "begin group",
    "end group",
    "begin repeat",
    "end repeat",
    "note",
    "start",
    "end",
    "deviceid",
    "username",
    "simserial",
    "phonenumber",
]
_DEFAULT_SYSTEM_PREFIXES = ["instanceID", "instanceName", "KEY", "SET-OF-"]

# Per-entry keys that hold filesystem paths (resolved relative to the config dir).
# Everything else (name, max_section_depth, geo_bbox, skip_ord_dta, and the Stata
# global literal sumstats_dir_stata) passes through untouched.
_SURVEY_PATH_KEYS = {"input_file", "external_choices_csv", "output_dir", "sections_dir"}
_SURVEY_PATHLIST_KEYS = {"pulldata_search_dirs"}
_DATASET_PATH_KEYS = {
    "data",
    "questions_json",
    "output_json",
    "output_xlsx",
    "xml_path",
    "output_do",
}
_DATASET_PATHLIST_KEYS = {"xml_attachments_dirs"}


def config_path():
    """Return the path config would load from (env override, else ./config.toml|py)."""
    override = os.environ.get("SURVEYCTO_CONFIG")
    if override:
        return Path(override)
    cwd = Path.cwd()
    toml = cwd / "config.toml"
    return toml if toml.is_file() else cwd / "config.py"


def load_config(path=None):
    """Return the user config object, or ``None`` if no config is present."""
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

    cfg = _load_toml(p) if p.suffix == ".toml" else _load_py(p)
    _cache[key] = cfg
    return cfg


def get_datasets(config=None):
    """Return the DATASETS dict from config, or ``{}`` when config is absent/empty."""
    cfg = config if config is not None else load_config()
    return getattr(cfg, "DATASETS", {}) if cfg is not None else {}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_py(p: Path):
    """Exec a legacy ``config.py`` and return the module (present-but-broken raises)."""
    spec = importlib.util.spec_from_file_location("surveycto_user_config", p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # broken config raises (loud, intentional)
    return module


def _load_toml(p: Path):
    """Parse ``config.toml`` into a config namespace with paths resolved vs its dir."""
    base = p.parent
    with open(p, "rb") as fh:
        data = tomllib.load(fh)  # broken TOML raises (loud, intentional)

    ns = SimpleNamespace()
    ns.SURVEYS = {
        name: _resolve_entry(base, entry, _SURVEY_PATH_KEYS, _SURVEY_PATHLIST_KEYS)
        for name, entry in (data.get("surveys") or {}).items()
    }
    ns.DATASETS = {
        name: _resolve_entry(base, entry, _DATASET_PATH_KEYS, _DATASET_PATHLIST_KEYS)
        for name, entry in (data.get("datasets") or {}).items()
    }

    cols = data.get("columns") or {}
    ns.SURVEY_COLUMNS = cols.get("survey", list(_DEFAULT_SURVEY_COLUMNS))
    ns.CHOICES_COLUMNS = cols.get("choices", list(_DEFAULT_CHOICES_COLUMNS))
    ns.EXCLUDED_TYPES = cols.get("excluded_types", list(_DEFAULT_EXCLUDED_TYPES))
    ns.SYSTEM_PREFIXES = cols.get("system_prefixes", list(_DEFAULT_SYSTEM_PREFIXES))

    # Sentinels: an empty [sentinels.meanings] table is a deliberate override
    # (disable recoding), so set the attr whenever the table is PRESENT; leave it
    # unset (-> resolve to the built-in default) only when it is absent (#6).
    sentinels = data.get("sentinels")
    if sentinels is not None:
        meanings = sentinels.get("meanings")
        if meanings is not None:
            ns.SENTINEL_MEANINGS = {int(k): tuple(v) for k, v in meanings.items()}
        scan_only = sentinels.get("scan_only")
        if scan_only is not None:
            ns.SENTINEL_SCAN_ONLY = [int(x) for x in scan_only]

    return ns


def _resolve_path(base: Path, value):
    """Resolve one path string vs ``base``; empty/None -> None; absolute kept as-is."""
    if value is None or value == "":
        return None
    q = Path(value)
    return q if q.is_absolute() else base / q


def _resolve_entry(base: Path, entry: dict, path_keys: set, pathlist_keys: set) -> dict:
    """Return a copy of a surveys/datasets entry with path-valued keys resolved."""
    out = dict(entry)
    for k in path_keys:
        if k in out:
            out[k] = _resolve_path(base, out[k])
    for k in pathlist_keys:
        if k in out:
            out[k] = [
                _resolve_path(base, x) for x in out[k] if x is not None and x != ""
            ]
    if isinstance(out.get("geo_bbox"), list):
        out["geo_bbox"] = tuple(out["geo_bbox"])
    return out
