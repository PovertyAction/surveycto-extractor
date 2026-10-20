"""Per-column PII signal: evidence for the people cleaning the data, never enforcement.

Ported from surveycto-python-dict's ``pii.py`` for a project that has the XLSForm and
the Stata ``.dta`` only. Each column gets ``{"status", "basis"[, "presidio"]}``; the
first matching rule wins:

0. ``contains_pii`` / ``reviewed``: the column or its question is on the user's
   blacklist; ``whitelisted`` / ``reviewed``: on the whitelist.
1. ``cleared_by_form``: the question is ``publishable`` in the XLSForm. With
   ``trust_publishable = false`` this rule is skipped and ``publishable`` is only
   noted in the basis.
2. ``contains_pii``: device columns (``deviceid``, ``username``, ...); a field that
   preloads device or user metadata whatever its name; a calculate that calls
   ``enumerator-id()``, ``enumerator-name()`` or ``username()``; and a calculate whose
   formula reads such a field, a geo or attachment field or a blacklisted one,
   directly or through other calculates (basis ``lineage:<field>``). Geo and
   attachment fields themselves are ``contains_pii`` by their type.
3. ``whitelisted``: SurveyCTO system columns with no question; integer, decimal,
   date, datetime and time questions, repeat counts and the device's start/end
   timestamps; selects with inline choices; calculates that only generate a value
   (``once(uuid())``, ``duration()``) or whose every reference is cleared or
   whitelisted, and that do not ``pulldata()``.
4. ``may_contain_pii`` / ``no_hits``: everything else, a column with no question
   included, scanned with Presidio on its distinct non-empty values (sampled with
   ``seed`` above ``max_distinct``). With ``no_personal_data`` every column past
   rule 2 is ``whitelisted`` / ``project`` and Presidio never runs.

``shows_values`` is the gate: an output an agent reads shows a flagged column's
counts, never its values. Choice lists read from data (an external choices CSV, a
``search()`` CSV) are screened all or nothing by ``withhold_reasons``. Blocks hold
entity counts, the number of hit rows and up to five of their ``KEY``s, never
matched text.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import warnings
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING

from surveycto_extractor.parsers.type_parser import parse_question_type
from surveycto_extractor.parsers.xml_contract import pulldata_columns

# Type hints only: config_loader imports this module, and every CLI imports it.
if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

# SurveyCTO system columns that hold only IDs, timestamps and codes, lowercased as
# a Stata export names them. The others (instancename, submittername,
# review_comments, ...) can hold names or free text, so they are scanned.
_SAFE_SYSTEM = {
    "key",
    "submissiondate",
    "completiondate",
    "starttime",
    "endtime",
    "duration",
    "formdef_version",
    "review_status",
    "instanceid",
}
_PULLDATA = re.compile(r"(?<![\w-])pulldata\s*\(", re.IGNORECASE)
_DEVICE_COLUMNS = {
    "deviceid",
    "subscriberid",
    "simid",
    "simserial",
    "devicephonenum",
    "phonenumber",
    "username",
}
_CONTAINS_TYPES = {
    "geopoint",
    "geotrace",
    "geoshape",
    "image",
    "audio",
    "video",
    "file",
}
# XLSForm types whose first word hides an attachment.
_ATTACHMENT_TYPES = {"text audit": "file", "audio audit": "audio", "comments": "file"}
# Numbers, dates, repeat counts, and the timestamps the device writes.
_TYPED_SAFE = {
    "integer",
    "decimal",
    "date",
    "datetime",
    "time",
    "repeat_count",
    "start",
    "end",
    "today",
}
# A geopoint's export columns: <name>latitude ... (CSV: <name>-Latitude).
_GEO_PART = re.compile(r"(.+?)[-_]?(latitude|longitude|altitude|accuracy)$", re.I)
# Fields that capture who or which device collected the submission, whatever their name.
_METADATA_TYPES = {
    "deviceid",
    "subscriberid",
    "simserial",
    "phonenumber",
    "username",
    "enumerator",  # stores the enumerator's "name (id)"
}
_METADATA_CALLS = re.compile(r"(?<![\w-])(enumerator-id|enumerator-name|username)\(\)")
_SELECT_TYPES = {"select_one", "select_multiple"}
_CALC_TYPES = {"calculate", "calculate_here"}
_STRUCTURAL = {"begin group", "end group", "begin repeat", "end repeat"}
_REF = re.compile(r"\$\{([^}]+)\}")
_CALL = re.compile(r"([A-Za-z][\w-]*)\s*\(")
# Functions that make a value without reading the form or the device.
_GENERATED_CALLS = {
    "once",
    "uuid",
    "now",
    "today",
    "random",
    "concat",
    "if",
    "duration",
}
# The statuses that clear a calculate reading the field (lineage), and those that hold
# or may hold PII.
SAFE = {"cleared_by_form", "whitelisted"}
FLAGGED = {"contains_pii", "may_contain_pii"}
# Enough KEYs to find examples; the review workbook has every flagged row.
_MAX_HIT_KEYS = 5
# A choice list read from data longer than this is withheld whatever it holds.
MAX_INLINE_CHOICES = 50
MODEL_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "{model}-3.8.0/{model}-3.8.0-py3-none-any.whl"
)
# A sheet's rows, the header included.
_EXCEL_ROWS = 1_048_576


def shows_values(status: str) -> bool:
    """Return whether an output an agent reads may show a column's values (range,
    frequencies, codes) for its pii ``status``: every status but a flagged one.
    Counts, null rates and ``KEY``s are shown for every column.
    """
    return status not in FLAGGED


class MissingSpacyModel(RuntimeError):
    """A configured language's spaCy model is not installed."""


@dataclass(frozen=True)
class PiiConfig:
    """``[pii]`` settings."""

    languages: dict[str, str] = field(
        default_factory=lambda: {"es": "es_core_news_lg", "en": "en_core_web_lg"}
    )
    entities: tuple[str, ...] = ("PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "LOCATION")
    # Extra entity name -> regex, e.g. {"NATIONAL_ID": r"\b\d{8,10}\b"}.
    id_patterns: dict[str, str] = field(default_factory=dict)
    # Presidio scores bare phone numbers 0.4.
    score_threshold: float = 0.4
    max_distinct: int = 1000
    seed: int = 0
    # Question or Stata column names the user reviewed and cleared.
    whitelist: tuple[str, ...] = ()
    # Question or Stata column names the user reviewed as personal data, or a
    # pulldata() column as dataset.column; above publishable.
    blacklist: tuple[str, ...] = ()
    # False: publishable clears nothing; the column goes through the other rules.
    trust_publishable: bool = True
    # True: the project holds no personal data; every column past the blacklist,
    # publishable and collection-metadata rules is whitelisted, unscanned.
    no_personal_data: bool = False

    def __post_init__(self):
        """Raise when a name is on both the whitelist and the blacklist."""
        both = set(self.whitelist) & set(self.blacklist)
        if both:
            raise ValueError(
                f"on both the whitelist and the blacklist: {', '.join(sorted(both))}"
            )

    @classmethod
    def from_table(cls, table: dict) -> PiiConfig:
        """Return the config for a ``[pii]`` TOML table; an unknown key raises."""
        unknown = set(table) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown key(s) in [pii]: {', '.join(sorted(unknown))}")
        kwargs = dict(table)
        for key in ("entities", "whitelist", "blacklist"):
            if key in kwargs:
                kwargs[key] = tuple(kwargs[key])
        return cls(**kwargs)


def build_analyzer(config: PiiConfig):
    """Return a Presidio analyzer for the configured languages and ID patterns."""
    # Imported here: spaCy and Presidio take seconds to load.
    import spacy
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    missing = [
        (lang, model)
        for lang, model in config.languages.items()
        if not spacy.util.is_package(model)
    ]
    if missing:
        lines = "\n".join(
            f"  {lang}: uv add {MODEL_URL.format(model=model)}"
            for lang, model in missing
        )
        raise MissingSpacyModel(
            "spaCy model(s) for [pii] languages not installed; install them in this "
            f"project with:\n{lines}"
        )
    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": k, "model_name": v} for k, v in config.languages.items()
            ],
        }
    ).create_engine()
    analyzer = AnalyzerEngine(
        nlp_engine=nlp, supported_languages=list(config.languages)
    )
    for name, regex in config.id_patterns.items():
        for lang in config.languages:
            analyzer.registry.add_recognizer(
                PatternRecognizer(
                    supported_entity=name,
                    supported_language=lang,
                    patterns=[Pattern(name=name, regex=regex, score=1.0)],
                )
            )
    return analyzer


def _entities(texts: list[str], config: PiiConfig, analyzer) -> list[set[str]]:
    """Return the entity types Presidio finds in each text.

    spaCy reads the texts in batches, several times faster than one call per text,
    and Presidio's recognizers then run on each text's parse.
    """
    wanted = [*config.entities, *config.id_patterns]
    found: list[set[str]] = [set() for _ in texts]
    for lang in config.languages:
        parsed = analyzer.nlp_engine.process_batch(texts, lang, batch_size=256)
        for hits, (text, artifacts) in zip(found, parsed, strict=True):
            hits.update(
                r.entity_type
                for r in analyzer.analyze(
                    text=text,
                    language=lang,
                    entities=wanted,
                    score_threshold=config.score_threshold,
                    nlp_artifacts=artifacts,
                )
            )
    return found


def rule_questions(
    survey_df: pd.DataFrame, questions: list[dict], external_lists: set[str]
) -> list[dict]:
    """Return the rules' view of the form: one entry per named survey-sheet row, the
    metadata rows ``questions.json`` leaves out included, plus each repeat's count.
    A select on a list from the external choices CSV (``external_lists``) has its
    choices from data, like a ``search()`` select.
    """
    out = []
    for _, row in survey_df.iterrows():
        name = row.get("name", "")
        raw = row.get("type", "")
        if not name or raw.lower().replace("_", " ") in _STRUCTURAL:
            continue
        base, list_name = parse_question_type(raw)
        qtype = _ATTACHMENT_TYPES.get(raw.lower()) or base
        appearance = row.get("appearance") or None
        out.append(
            {
                "variable_name": name,
                "type": qtype.lower(),
                "calculation": row.get("calculation") or None,
                "choices_from_data": list_name in external_lists
                or "search(" in (appearance or ""),
                "publishable": row.get("publishable", "").lower()
                in ("yes", "true", "true()", "1"),
            }
        )
    named = {q["variable_name"] for q in out}
    out.extend(
        {
            "variable_name": q["variable_name"],
            "type": "repeat_count",
            "calculation": None,
            "choices_from_data": False,
            "publishable": False,
        }
        for q in questions
        if q["type"] == "repeat_count" and q["variable_name"] not in named
    )
    return out


class _Rules:
    """Question-level rules, memoised so calculate lineage resolves once."""

    def __init__(
        self,
        questions: list[dict],
        whitelist: set[str],
        blacklist: set[str],
        trust_publishable: bool,
        no_personal_data: bool = False,
    ):
        self.by_name = {q["variable_name"]: q for q in questions}
        # Stata lowercases names, so an unmatched column is looked up lowercased.
        self.by_lower = {n.lower(): n for n in self.by_name}
        self.whitelist = whitelist
        self.blacklist = blacklist
        self.trust_publishable = trust_publishable
        # In a project declared to hold no personal data, geo and attachment fields are
        # whitelisted, so they carry nothing.
        self.personal_types = set() if no_personal_data else _CONTAINS_TYPES
        self._memo: dict[str, dict | None] = {}
        self._carried: dict[str, str | None] = {}

    def static(self, name: str) -> dict | None:
        """Return the question's block when a rule decides it without data, else None."""
        if name not in self._memo:
            self._memo[name] = None  # a reference cycle resolves to "scan"
            self._memo[name] = self._decide(self.by_name[name])
        return self._memo[name]

    def _decide(self, q: dict) -> dict | None:
        qtype = q["type"]
        if q["variable_name"] in self.blacklist:
            return {"status": "contains_pii", "basis": ["reviewed"]}
        if q["variable_name"] in self.whitelist:
            return {"status": "whitelisted", "basis": ["reviewed"]}
        if q["publishable"] and self.trust_publishable:
            return {"status": "cleared_by_form", "basis": ["publishable"]}
        if basis := self.personal(q["variable_name"]):
            return {"status": "contains_pii", "basis": [basis]}
        if qtype in _CONTAINS_TYPES:
            return {"status": "contains_pii", "basis": [f"type:{qtype}"]}
        if qtype in _TYPED_SAFE:
            return {"status": "whitelisted", "basis": [f"type:{qtype}"]}
        if qtype in _SELECT_TYPES and not q["choices_from_data"]:
            return {"status": "whitelisted", "basis": ["choices:inline"]}
        if qtype in _CALC_TYPES and not self._pulldata(q):
            calculation = q["calculation"] or ""
            refs = list(dict.fromkeys(_REF.findall(calculation)))
            # Reading no field and calling only functions that generate a value, it
            # holds nothing anyone entered: once(uuid(5)), today().
            calls = set(_CALL.findall(calculation))
            if not refs and calls <= _GENERATED_CALLS:
                return {"status": "whitelisted", "basis": ["generated"]}
            if refs and all(r in self.by_name and self._safe(r) for r in refs):
                return {"status": "whitelisted", "basis": [f"lineage:{','.join(refs)}"]}
        return None

    def personal(self, name: str | None) -> str | None:
        """Return why question ``name`` captures collection metadata, else None: its
        type, or a function its calculation calls.
        """
        q = self.by_name.get(name)
        if q is None:
            return None
        if q["type"] in _METADATA_TYPES:
            return f"metadata:{q['type']}"
        calls = _METADATA_CALLS.search(q["calculation"] or "")
        if calls:
            return f"calls:{calls.group(1)}()"
        return None

    def carried(self, name: str | None) -> str | None:
        """Return a field a calculate's formula reads, directly or through other
        calculates, that is blacklisted, collection metadata or of a type that holds
        personal data (a geopoint, an image, ...), else None: ``selected-at(${gps},
        0)`` holds the latitude. A whitelisted field carries nothing. A
        ``pulldata()`` holds its dataset column, not its key: it carries the column
        when that is blacklisted.
        """
        q = self.by_name.get(name)
        if q is None or q["type"] not in _CALC_TYPES:
            return None
        if self._pulldata(q):
            return next(
                (c for c in pulldata_columns(q["calculation"]) if c in self.blacklist),
                None,
            )
        if name not in self._carried:
            self._carried[name] = None  # a reference cycle carries nothing
            for ref in dict.fromkeys(_REF.findall(q["calculation"] or "")):
                if ref in self.whitelist:
                    continue
                found = (
                    ref
                    if ref in self.blacklist
                    or self.personal(ref)
                    or self.by_name.get(ref, {}).get("type") in self.personal_types
                    else self.carried(ref)
                )
                if found:
                    self._carried[name] = found
                    break
        return self._carried[name]

    def _safe(self, name: str) -> bool:
        block = self.static(name)
        return block is not None and block["status"] in SAFE

    @staticmethod
    def _pulldata(q: dict) -> bool:
        return bool(_PULLDATA.search(q["calculation"] or ""))

    def scan_basis(self, name: str | None) -> list[str]:
        if name is None:
            return ["no_question"]
        q = self.by_name[name]
        return [f"type:{q['type']}"] + (["pulldata"] if self._pulldata(q) else [])


def _text(value) -> str:
    """Return a value as Presidio reads it: a whole float as an integer, so a phone
    number stored as a Stata double is ``3104567890``, not ``3104567890.0``.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class _Scans:
    """Columns to scan, collected first so Presidio reads every distinct text once.

    Columns share many values ("0", "No", a date), so a form's columns are scanned
    together: ``add`` registers a column, ``run`` analyzes the union of their
    sampled distinct values, and ``block`` gives each column its result.
    """

    def __init__(self, config: PiiConfig):
        self.config = config
        self.columns: dict = {}
        self.found: dict[str, set[str]] = {}

    def add(
        self, name, column: pa.ChunkedArray, keys: pa.ChunkedArray, basis: list[str]
    ) -> None:
        # Only the distinct values become Python objects, so a wide dataset is not
        # copied row by row. Stata stores a missing string as "", so it is missing.
        import pyarrow.compute as pc

        raw_of: dict[str, list] = {}
        for raw in pc.unique(column).to_pylist():
            if raw is not None and raw != "":
                raw_of.setdefault(_text(raw), []).append(raw)
        distinct = sorted(raw_of)
        if len(distinct) > self.config.max_distinct:
            distinct = sorted(
                random.Random(self.config.seed).sample(
                    distinct, self.config.max_distinct
                )
            )
        self.columns[name] = (column, raw_of, distinct, keys, basis)

    def run(self, analyzer) -> None:
        texts = sorted(
            {t for _, _, distinct, _, _ in self.columns.values() for t in distinct}
            - set(self.found)
        )
        if texts:
            self.found.update(zip(texts, _entities(texts, self.config, analyzer)))

    def block(self, name) -> dict:
        return _block(*self.columns[name], self.found)


def _block(
    column: pa.ChunkedArray,
    raw_of: dict[str, list],
    distinct: list[str],
    keys: pa.ChunkedArray,
    basis: list[str],
    entities: dict,
) -> dict:
    import pyarrow as pa
    import pyarrow.compute as pc

    found: dict[str, set[str]] = {}
    for value in distinct:
        for t in entities[value]:
            found.setdefault(t, set()).add(value)
    hits = set().union(*found.values()) if found else set()
    raw_hits = pa.array([r for t in sorted(hits) for r in raw_of[t]], column.type)
    hit_rows = keys.filter(pc.is_in(column, value_set=raw_hits))
    hit_keys = sorted(pc.unique(hit_rows).to_pylist())
    return {
        "status": "may_contain_pii" if found else "no_hits",
        "basis": basis + [f"presidio:{t}" for t in sorted(found)],
        "presidio": {
            "entities": {t: len(found[t]) for t in sorted(found)},
            "distinct_scanned": len(distinct),
            "hit_rows": len(hit_keys),
            "hit_keys": hit_keys[:_MAX_HIT_KEYS],
        },
    }


def key_column(names: list[str]) -> str:
    """Return the submission ``KEY`` column (``key`` in a Stata export)."""
    for name in names:
        if name.lower() == "key":
            return name
    raise ValueError(
        "the dataset has no KEY column; the PII blocks and review workbook identify "
        "rows by it"
    )


def _question_of(column: str, entry: dict, rules: _Rules) -> str | None:
    """Return the question a column holds: the dictionary's match, else a survey-sheet
    row of the same name (the metadata rows ``questions.json`` leaves out), else the
    geopoint whose part it is (``gpslatitude`` is part of ``gps``).
    """
    matched = (entry.get("survey") or {}).get("original_variable_name")
    if matched is not None:
        if matched not in rules.by_name:
            raise ValueError(
                f"column {column!r} matches question {matched!r}, which the XLSForm "
                "survey sheet lacks; re-run surveycto-extract on the current form"
            )
        return matched
    if column.lower() in rules.by_lower:
        return rules.by_lower[column.lower()]
    part = _GEO_PART.match(column)
    if part:
        point = rules.by_lower.get(part.group(1).lower())
        if point is not None and rules.by_name[point]["type"] == "geopoint":
            return point
    return None


def classify(
    table: pa.Table, vardict: dict, questions: list[dict], config: PiiConfig, analyzer
) -> dict[str, dict]:
    """Return the PII block for every column of ``table``.

    ``questions`` is ``rule_questions``' view of the form; ``vardict`` maps each
    column to its question.
    """
    variables = vardict["variables"]
    question_names = {q["variable_name"] for q in questions}
    whitelist, blacklist = set(config.whitelist), set(config.blacklist)
    # The blacklist keeps its dataset columns (dataset.column): a pulldata() of one
    # carries it.
    rules = _Rules(
        questions,
        whitelist & question_names,
        blacklist,
        config.trust_publishable,
        config.no_personal_data,
    )
    keys = table.column(key_column(table.schema.names))

    blocks = {}
    scans = _Scans(config)
    question_of = {c: _question_of(c, variables[c], rules) for c in table.schema.names}
    for column in table.schema.names:
        question = question_of[column]
        q = rules.by_name.get(question)
        if column in blacklist or question in rules.blacklist:
            block = {"status": "contains_pii", "basis": ["reviewed"]}
        elif column in whitelist or question in rules.whitelist:
            block = {"status": "whitelisted", "basis": ["reviewed"]}
        elif q is not None and q["publishable"] and config.trust_publishable:
            block = {"status": "cleared_by_form", "basis": ["publishable"]}
        elif column.lower() in _DEVICE_COLUMNS:
            block = {"status": "contains_pii", "basis": [f"device:{column}"]}
        elif basis := rules.personal(question):
            block = {"status": "contains_pii", "basis": [basis]}
        elif carried := rules.carried(question):
            block = {"status": "contains_pii", "basis": [f"lineage:{carried}"]}
        elif question is None and column.lower() in _SAFE_SYSTEM:
            block = {"status": "whitelisted", "basis": ["system"]}
        elif config.no_personal_data:
            block = {"status": "whitelisted", "basis": ["project"]}
        else:
            block = rules.static(question) if question is not None else None
            if block is None:
                scans.add(
                    column, table.column(column), keys, rules.scan_basis(question)
                )
                continue
            if q is not None and q["publishable"]:
                block = {**block, "basis": [*block["basis"], "publishable"]}
        blocks[column] = block
    scans.run(analyzer)
    for column in scans.columns:
        block = scans.block(column)
        q = rules.by_name.get(question_of[column])
        if q is not None and q["publishable"]:
            block = {**block, "basis": [*block["basis"], "publishable"]}
        blocks[column] = block
    return {column: blocks[column] for column in table.schema.names}


def withhold_reasons(
    names: set[str], choices: list[dict], config: PiiConfig, analyzer
) -> list[str]:
    """Return why a choice list read from data is withheld from the outputs, or
    ``[]`` to keep it. All or nothing: a blacklisted question among ``names`` (the
    questions using the list), more than ``MAX_INLINE_CHOICES`` choices, or any
    Presidio hit among the values and labels. Lists whose every question is
    whitelisted, and every list under ``no_personal_data``, are kept unscanned: the
    team's review outranks the size cap.
    """
    if names & set(config.blacklist):
        return ["reviewed"]
    if (names and names <= set(config.whitelist)) or config.no_personal_data:
        return []
    if len(choices) > MAX_INLINE_CHOICES:
        return [f"{len(choices)} choices"]
    texts = sorted({t for c in choices for t in (c["value"], c["label"]) if t})
    if not texts:
        return []
    found = set().union(*_entities(texts, config, analyzer))
    return [f"presidio:{e}" for e in sorted(found)]


def screen_external_lists(
    survey_df: pd.DataFrame,
    choices_df: pd.DataFrame,
    list_names: set[str],
    config: PiiConfig,
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Screen the choice lists an external choices CSV added (``list_names``).

    Returns the choices without the withheld lists' rows and each withheld list's
    reasons. The analyzer is built only when there is a list to scan.
    """
    if not list_names:
        return choices_df, {}
    users: dict[str, set[str]] = {}
    for _, row in survey_df.iterrows():
        _, list_name = parse_question_type(row.get("type", ""))
        if list_name in list_names:
            users.setdefault(list_name, set()).add(row["name"])
    analyzer = None if config.no_personal_data else build_analyzer(config)
    withheld = {}
    for list_name in sorted(list_names):
        rows = choices_df[choices_df["list_name"] == list_name]
        choices = rows[["value", "label"]].to_dict("records")
        reasons = withhold_reasons(
            users.get(list_name, set()), choices, config, analyzer
        )
        if reasons:
            withheld[list_name] = reasons
    kept = choices_df[~choices_df["list_name"].isin(withheld)]
    return kept, withheld


def warn_unknown_names(config: PiiConfig, known: set[str]) -> None:
    """Warn for each whitelist or blacklist name the form and dataset do not have."""
    for name in sorted((set(config.whitelist) | set(config.blacklist)) - known):
        warnings.warn(f"`{name}` cannot be found", UserWarning, stacklevel=2)


def write_pii_review(table: pa.Table, vardict: dict, path: Path) -> Path:
    """Write every flagged column to ``path``: a Columns sheet and a Values sheet with
    one row per value (``KEY``, column, value), which filters by column in Excel.
    This is the only output that holds flagged values; it sits next to the ``.dta``.
    Values past a sheet's rows go on in ``Values 2``, ``Values 3``, ...
    """
    variables = vardict["variables"]
    flagged = [
        c for c in table.schema.names if not shows_values(variables[c]["pii"]["status"])
    ]
    columns = [
        [
            c,
            (variables[c].get("survey") or {}).get("question_text") or "",
            variables[c]["pii"]["status"],
            ", ".join(variables[c]["pii"]["basis"]),
            variables[c]["pii"].get("presidio", {}).get("hit_rows", ""),
            variables[c]["non_null_count"],
        ]
        for c in flagged
    ]
    keys = table.column(key_column(table.schema.names)).to_pylist()
    values = [
        [key, c, v.replace(tzinfo=None) if isinstance(v, dt.datetime) else v]
        for c in flagged
        for key, v in zip(keys, table.column(c).to_pylist(), strict=True)
        if v is not None and v != ""
    ]
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    def cell(sheet, value):
        # Typed text keeps stray control characters, which openpyxl refuses, and
        # may start with "=", which it would store as a formula: text stays text.
        if not isinstance(value, str):
            return value
        out = WriteOnlyCell(sheet, value=ILLEGAL_CHARACTERS_RE.sub("", value))
        out.data_type = "s"
        return out

    # A sheet holds _EXCEL_ROWS rows, the header included.
    per_sheet = _EXCEL_ROWS - 1
    chunks = [values[i : i + per_sheet] for i in range(0, len(values), per_sheet)]
    workbook = Workbook(write_only=True)
    for title, header, rows in (
        (
            "Columns",
            ["column", "label", "status", "basis", "hit_rows", "non_null"],
            columns,
        ),
        *(
            ("Values" if i == 0 else f"Values {i + 1}", ["KEY", "column", "value"], c)
            for i, c in enumerate(chunks or [[]])
        ),
    ):
        sheet = workbook.create_sheet(title)
        sheet.append(header)
        for row in rows:
            sheet.append([cell(sheet, v) for v in row])
    workbook.save(path)
    return path
