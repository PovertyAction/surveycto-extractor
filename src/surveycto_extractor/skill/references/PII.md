# PII statuses, withheld lists and flags the team disagrees with

Read this when you need a column's status explained, a select_multiple's binaries show
by position, or the team disagrees with a flag. The rule itself is in SKILL.md: use
every column, print values only of unflagged ones.

## Contents

- [Statuses and their basis](#statuses-and-their-basis)
- [Long tables](#long-tables)
- [What a script prints for a flagged column](#what-a-script-prints-for-a-flagged-column)
- [Fixing a flag](#fixing-a-flag)
- [Withheld choice lists](#withheld-choice-lists)

## Statuses and their basis

Every column carries `pii.status` with the `basis` for it. The status says where
personal data may be so it is handled carefully; it never means drop the column or
work around it.

| Status | Basis | You may see |
|---|---|---|
| `cleared_by_form` | `publishable`: the XLSForm marks the field publishable | Its values |
| `whitelisted` | `reviewed` (the team's whitelist); `system` (`KEY`, `SubmissionDate`, `starttime`, `duration`, `formdef_version`, ...); `type:integer`, `decimal`, `date`, `datetime`, `time`, `start`, `end`, `today`, `repeat_count`; `choices:inline` (a select with the form's own choices); `generated` (a calculate that makes its value, `once(uuid())`); `lineage:<refs>` (a calculate whose every input is cleared or whitelisted); `project` (`no_personal_data` is set) | Its values |
| `contains_pii` | `reviewed` (the team's blacklist); `device:<col>` (`deviceid`, `username`, ...); `metadata:<type>` (a field that stores device or enumerator metadata); `calls:enumerator-name()` (or `enumerator-id()`, `username()`); `type:geopoint`, `geotrace`, `geoshape`, `image`, `audio`, `video`, `file` (text audits and comments count as files); `lineage:<field>` (a calculate that reads one of these) | Counts and `KEY`s only |
| `may_contain_pii` | `presidio:<ENTITY>` for each entity Presidio found in the values (`[pii] entities` and `id_patterns`: names, phones, emails, places by default); the basis starts with the question's type (and `pulldata` for a preload), or `no_question` for a column no question matches | Counts and `KEY`s only |
| `no_hits` | A scanned column where Presidio found nothing: free text, a select whose choices come from data, a `pulldata()` calculate, a column with no question | Its values; the team may still review it |

Any basis may end with `publishable` when the form marks the field publishable but
`trust_publishable = false`.

## Long tables

A long table's column (`member_name` in `member.parquet`) has no dictionary entry of
its own: it takes the strictest status of the wide columns it stacks
(`member_name_1`, `member_name_2`, ...). `lookup_variable("member_name")` lists them.

## What a script prints for a flagged column

Counts, null rates, distinct counts and the `KEY`s of the rows a check flags; never
the values, `head()`, `list` or `value_counts()`/`tab`. `pii.presidio.hit_rows` counts
the rows with a hit and `hit_keys` gives up to five of their `KEY`s; quote counts and
KEYs, never values. Never open `<stem>_pii_review.xlsx`: it is the team's. Whether a
column leaves a delivered dataset is the team's decision, not a cleaning default.

## Fixing a flag

A flag the team disagrees with is fixed in `config.toml`: they add the columns they
clear to `[pii] whitelist` and personal ones to `[pii] blacklist` (question names or
Stata names; a `pulldata()` column as `dataset.column`), then re-run
`surveycto-vardict`.

## Withheld choice lists

A choice list read from data (an external choices CSV, a `search()` CSV) is withheld
when the team blacklisted a question using it, it has more than 50 choices, or
Presidio found personal data in it; `choices_withheld` says why. Refer to a withheld
list by name and source; never reconstruct it from the data. The binaries of a
withheld select_multiple carry its codes in their names, so the tools show them by
position, `<name>_<1>`, `<name>_<2>` (plus `_<instance>` in a repeat); look one up by
that name, and in scripts read the real names from the dictionary without printing
them.
