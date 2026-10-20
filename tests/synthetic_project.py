"""A small invented SurveyCTO project: an XLSForm, an external choices CSV and a
Stata export, written into a tmp dir for the PII tests. Every value is fictional;
the CANARIES are the ones no agent-facing output may show.
"""

import types
from pathlib import Path

import pandas as pd
from openpyxl import Workbook

from surveycto_extractor.pii import PiiConfig

SURVEY = [
    # type, name, label, calculation, appearance, publishable
    ("start", "starttime", "", "", "", ""),
    ("end", "endtime", "", "", "", ""),
    ("deviceid", "deviceid", "", "", "", ""),
    ("username", "username", "", "", "", ""),
    ("text", "hhid", "Household ID", "", "", ""),
    ("text", "respondent", "Respondent's name", "", "", ""),
    ("text", "phone", "Phone number", "", "", ""),
    ("text", "village", "Village", "", "", "yes"),
    ("integer", "age", "Age", "", "", ""),
    ("decimal", "area", "Plot area", "", "", ""),
    ("date", "dob", "Date of birth", "", "", ""),
    ("integer", "secret_num", "A number on the blacklist", "", "", ""),
    ("select_one yesno", "consent", "Consent", "", "", ""),
    ("select_one hh", "hh_pick", "Household from the roster", "", "", ""),
    ("geopoint", "gps", "Location", "", "", ""),
    ("image", "photo", "Photo", "", "", ""),
    ("calculate", "tag", "", "concat(${respondent}, '-', ${phone})", "", ""),
    ("calculate", "age2", "", "${age} * 2", "", ""),
    ("calculate", "lat", "", "selected-at(${gps}, 0)", "", ""),
    ("calculate", "coder", "", "concat('by ', enumerator-name())", "", ""),
    ("calculate", "secret_calc", "", "${secret_num} + 1", "", ""),
    (
        "calculate",
        "prev",
        "",
        "pulldata('prev_round', 'owner', 'hhid', ${hhid})",
        "",
        "",
    ),
    ("calculate", "elapsed", "", "duration()", "", ""),
    # A phone number stored as a number: flagged by Presidio, so no range.
    ("calculate", "phone_num", "", "${phone}", "", ""),
    # Collection metadata under a name of the form's own.
    ("username", "enum_user", "", "", "", ""),
    # Choices from a search() CSV the XML overlay resolves.
    (
        "select_one roster",
        "roster_pick",
        "Member from the roster",
        "",
        "search('roster', 'matches', 'grp', 'a')",
        "",
    ),
]
CHOICES = [("yesno", "1", "Yes"), ("yesno", "0", "No"), ("roster", "value", "label")]
# The external roster: names, so the list is withheld.
EXTERNAL = pd.DataFrame(
    {"hh_value": ["h1", "h2"], "hh_label": ["John Smith", "Mary Johnson"]}
)

ROWS = {
    "key": ["uuid:aaa", "uuid:bbb", "uuid:ccc"],
    "submissiondate": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]),
    "starttime": pd.to_datetime(
        ["2026-01-05 09:00", "2026-01-06 09:00", "2026-01-07 09:00"]
    ),
    "endtime": pd.to_datetime(
        ["2026-01-05 10:00", "2026-01-06 10:00", "2026-01-07 10:00"]
    ),
    "deviceid": ["dev-1", "dev-2", "dev-1"],
    "username": ["user1", "user2", "user1"],
    "hhid": ["H001", "H002", "H003"],
    "respondent": ["Robert Brown", "Jorge Perez", "=Lucy Gray\x0b"],
    "phone": ["212-555-0147", "3105550123", ""],
    "village": ["Villa Ficticia Uno", "Villa Ficticia Dos", ""],
    "age": [34, 51, 29],
    "area": [1.5, 2.25, 3.0],
    "dob": pd.to_datetime(["1931-07-04", "1950-02-02", "1960-03-03"]),
    "secret_num": [987654, 987655, 987656],
    "consent": [1, 0, 1],
    "hh_pick": ["h1", "h2", "h1"],
    "gpslatitude": [4.123456, 4.2, 4.3],
    "gpslongitude": [-74.654321, -74.1, -74.2],
    "gpsaltitude": [2600.5, 2601.0, 2602.0],
    "gpsaccuracy": [5.0, 6.0, 7.0],
    "photo": ["photo1.jpg", "photo2.jpg", ""],
    "tag": ["Robert Brown-212-555-0147", "Jorge Perez-3105550123", ""],
    "age2": [68, 102, 58],
    "lat": [4.123456, 4.2, 4.3],
    "coder": ["by Pedro Ramirez", "by Pedro Ramirez", "by Pedro Ramirez"],
    "secret_calc": [987655, 987656, 987657],
    "prev": ["Mary Johnson", "John Smith", ""],
    "elapsed": [3600, 3500, 3400],
    "phone_num": [2125550147.0, 3105550123.0, float("nan")],
    "enum_user": ["user7", "user8", "user9"],
    "roster_pick": ["r1", "r2", "r1"],
    # A system column the settings sheet can fill with names: scanned.
    "instancename": ["Robert Brown household", "Jorge Perez household", ""],
}
ROSTER = "grp,value,label\na,r1,Emily Davis\na,r2,Mary Johnson\n"
XFORM = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head>
    <model>
      <instance><synth id="synth" version="1"><roster_pick/></synth></instance>
      <bind nodeset="/synth/roster_pick" type="select1"/>
    </model>
  </h:head>
  <h:body>
    <select1 ref="/synth/roster_pick"
             appearance="search('roster', 'matches', 'grp', 'a')">
      <label>Member from the roster</label>
      <item><label>label</label><value>value</value></item>
    </select1>
  </h:body>
</h:html>
"""

# Values only flagged columns hold: none may reach an agent-facing output.
CANARIES = [
    "Robert Brown",  # respondent: Presidio PERSON
    "212-555-0147",  # phone: Presidio PHONE_NUMBER
    "Pedro Ramirez",  # coder: enumerator-name()
    "987654",  # secret_num: blacklisted integer (its range)
    "987657",  # secret_calc: carries secret_num (its range)
    "1931-07-04",  # dob: blacklisted date (its range)
    "4.123456",  # gpslatitude, lat: geopoint
    "John Smith",  # external roster label, prev pulldata
    "dev-2",  # deviceid
    "2125550147",  # phone_num: Presidio PHONE_NUMBER on a number (its range)
    "user8",  # enum_user: username metadata
    "Emily Davis",  # roster.csv, read by the XML overlay's search()
]


def _xlsx(path: Path) -> None:
    wb = Workbook()
    survey = wb.active
    survey.title = "survey"
    survey.append(["type", "name", "label", "calculation", "appearance", "publishable"])
    for row in SURVEY:
        survey.append(list(row))
    choices = wb.create_sheet("choices")
    choices.append(["list_name", "value", "label"])
    for row in CHOICES:
        choices.append(list(row))
    wb.save(path)


def build(root: Path, pii: PiiConfig) -> types.ModuleType:
    """Write the project under ``root`` and return a config module for it."""
    data = root / "data"
    out = root / "out"
    data.mkdir()
    _xlsx(data / "form.xlsx")
    EXTERNAL.to_csv(data / "choices.csv", index=False)
    (data / "roster.csv").write_text(ROSTER, encoding="utf-8")
    (data / "form.xml").write_text(XFORM, encoding="utf-8")
    pd.DataFrame(ROWS).to_stata(
        data / "synth.dta",
        write_index=False,
        convert_dates={
            "submissiondate": "tc",
            "starttime": "tc",
            "endtime": "tc",
            "dob": "td",
        },
        version=118,
    )
    config = types.ModuleType("config")
    config.SURVEYS = {
        "synth": {
            "input_file": data / "form.xlsx",
            "external_choices_csv": data / "choices.csv",
            "output_dir": out,
            "sections_dir": out / "sections",
            "name": "Synthetic",
        }
    }
    config.DATASETS = {
        "synth": {
            "data": data / "synth.dta",
            "questions_json": out / "synth_questions.json",
            "output_json": out / "synth_variable_dictionary.json",
            "output_xlsx": out / "synth_variable_dictionary.xlsx",
            "xml_path": data / "form.xml",
        }
    }
    config.SURVEY_COLUMNS = ["type", "name", "label", "calculation"]
    config.CHOICES_COLUMNS = ["list_name", "value", "label"]
    config.EXCLUDED_TYPES = [
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
    config.SYSTEM_PREFIXES = ["instanceID", "instanceName", "KEY", "SET-OF-"]
    config.PII = pii
    return config
