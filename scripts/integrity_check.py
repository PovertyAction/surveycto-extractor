"""Repo-integrity gate: nothing the toolkit needs is invisible, dead, or unimportable.

WHY THIS IS A COMMITTED SCRIPT AND NOT A ONE-OFF SWEEP. The three checks below
started life as ad-hoc greps in a scratch session. A sweep whose instrument is
disposable cannot be re-run against the commit it certifies, so its "0 findings"
is always a claim about some earlier tree. Hence: in the repo, behind a recipe
(`just integrity-check`), and **stamping the HEAD it measured into line 1 of its
own report** -- so a clean number can always be tied to the commit it describes.

EVERY SWEEP IS ABLATED IN THE SAME RUN. Each check runs twice: once against the
tree, once against a tree with a defect injected, and the second run MUST report
it. A check that has not been shown to fire is a decoration, and this repo has
already been bitten by the pattern -- the `ragged` flag it relies on was, for a
while, computed from the indices a column *consumed* rather than the ones it
*supplied*, which reported a tidy match for exactly the shape the flag exists to
surface. The tests caught that; nothing was watching the sweeps.

  1  no file the toolkit needs is gitignored
  2  no tracked file names a code or doc path that does not exist
     (this is the sweep that found `docs/coding_guidelines/...` cited from
     `transformers/logic_converter.py`, where the real path has no `docs/` prefix
     -- invisible to 284 tests and both CI workflows)
  3  every module under src/ parses and imports

CONTRACT (consumes): the git index; every tracked *.py/*.md/*.yaml/*.json/*.toml
  plus the Justfile
OUTPUTS: data/_integrity/integrity_sweeps.txt (the report, stamped with its HEAD)
CLI: uv run python scripts/integrity_check.py   (exit 1 on any failure)

Sweep 3 imports the optional `mcp` extra's module, so run it in an environment
synced with `--all-extras` (which is what `just integrity-check` and the
pre-commit workflow both do).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Suffixes worth reading for path references. Data and instrument files
# (.dta/.parquet/.xlsx/.csv) are deliberately absent: they are binary or bulk
# content, not places a path reference lives.
CODE_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".toml", ".lock"}

# Directories whose contents must be visible to a fresh clone. `.claude/` is
# NOT here: this repo gitignores it on purpose (`.gitignore:16`, local per-project
# Claude settings), and `sample/output/` is generated, so both would be false
# positives for sweep 1 rather than findings.
CODE_ROOTS = (
    "src",
    "tests",
    "scripts",
    "docs",
    "coding_guidelines",
    "skill",
    ".github",
)
SAMPLE_ROOT = "sample"
SAMPLE_EXCLUDE = "sample/output"

CODE_FILES_AT_ROOT = (
    "Justfile",
    "pyproject.toml",
    "uv.lock",
    ".dep-accept.toml",
    ".pre-commit-config.yaml",
    ".markdownlint.yaml",
    ".python-version",
    ".mcp.json.example",
    ".gitignore",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
)

# Reference prefixes that legitimately do not resolve in a clean checkout, each
# with the reason on the record. This suppresses REVIEWED classes of reference,
# never "anything unresolved" -- a new prefix still reports.
ALLOWED_PREFIXES: dict[str, str] = {
    # `skill/SKILL.md` and the README tell a user where to COPY the skill to.
    # `.claude/` is gitignored here, so the destination never exists in-tree.
    ".claude/": "skill install destination, not repo content (.claude/ is gitignored)",
    # The acceptance engine is deliberately outside this repo, one shared copy
    # per machine; the Justfile resolves it through $DEP_ACCEPT.
    "scripts/accept_dep_prs.py": "lives in the external dep-accept skill checkout",
    # `data/` is the user's own survey tree, gitignored in full. Every reference
    # to it is an instruction or a config example, never a repo path -- which
    # also covers the SurveyCTO docs-site fragments quoted in
    # coding_guidelines/surveycto_refs/ and the XPath-shaped false positives
    # (`data/phone/date`) the regex picks out of expression text.
    "data/": "the user's own gitignored data tree; references are instructions",
    # A commented-out `output_do = ` example in README.md, showing where a user's
    # generated .do file would land in THEIR project.
    "scripts/cleaning/": "commented-out config example for a user's own project",
}

# This module's own source is a REGISTRY of references (the allowlist above), not
# a user of them. Scanning it would make every allowlist key look like a live
# reference to a path that does not exist -- the checker reporting itself.
SELF = "scripts/integrity_check.py"

PATH_RE = re.compile(
    r"(?<![\w./-])((?:src|tests|scripts|docs|coding_guidelines|skill|sample|data"
    r"|\.github|\.claude)/[A-Za-z0-9_./-]+)"
)
# Bare repo-root filenames referenced by name; half the tooling lives in files
# with no directory component.
BARE_RE = re.compile(
    r"(?<![\w./-])(Justfile|pyproject\.toml|uv\.lock|\.dep-accept\.toml"
    r"|\.mcp\.json\.example|\.pre-commit-config\.yaml)(?![\w/-])"
)
PLACEHOLDER = re.compile(r"[<>*{}]|\.\.\.|\$")
READABLE = CODE_SUFFIXES | {".example", ".cfg", ".txt"}


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _head() -> str:
    sha = _git("rev-parse", "HEAD").strip()[:7]
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    dirty = " (dirty)" if _git("status", "--porcelain").strip() else ""
    return f"{sha} @ {branch}{dirty}"


TRACKED = [ln for ln in _git("ls-files").splitlines() if ln.strip()]


class Report:
    """The report being built, printed as it goes and written out at the end."""

    def __init__(self) -> None:
        """Start an empty report with no recorded failures."""
        self.lines: list[str] = []
        self.failures: list[str] = []

    def say(self, line: str = "") -> None:
        """Record and print one report line."""
        self.lines.append(line)
        print(line)

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        """Record one pass/fail result, tracking failures for the exit code."""
        if not ok:
            self.failures.append(label)
        self.say(
            f"  [{'ok' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else "")
        )


def _candidate_code_files() -> list[str]:
    """Every file a fresh clone needs in order to build, test, or document itself."""
    out = []
    for r in CODE_ROOTS:
        base = ROOT / r
        if not base.exists():
            continue
        out += [
            p.relative_to(ROOT).as_posix()
            for p in base.rglob("*")
            if p.is_file()
            and p.suffix in CODE_SUFFIXES
            and ".venv" not in p.parts
            and "__pycache__" not in p.parts
        ]
    sample = ROOT / SAMPLE_ROOT
    if sample.exists():
        out += [
            rel
            for p in sample.rglob("*")
            if p.is_file()
            and p.suffix in CODE_SUFFIXES
            and not (rel := p.relative_to(ROOT).as_posix()).startswith(SAMPLE_EXCLUDE)
        ]
    out += [f for f in CODE_FILES_AT_ROOT if (ROOT / f).exists()]
    return sorted(set(out))


def _ignored_among(paths: list[str]) -> list[str]:
    r"""Which of `paths` git ignores, read by OUTPUT and never by exit status.

    `git check-ignore` exits 1 when nothing matched, so reading its status would
    make a clean sweep indistinguishable from a broken invocation.

    Fed BYTES on purpose. With `text=True`, Python writes stdin through a
    TextIOWrapper whose default newline translation turns every `\\n` into
    `os.linesep`, so on Windows git received `config.toml\\r` -- which matches no
    ignore rule -- and only the final line, the one with no trailing `\\r`, was
    tested at all. The sweep reported a clean "ok" while checking exactly one
    path. Sweep 1's own ablation is what surfaced it.
    """
    if not paths:
        return []
    r = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=ROOT,
        input=("\n".join(paths) + "\n").encode("utf-8"),
        capture_output=True,
    )
    out = r.stdout.decode("utf-8", errors="replace")
    return [ln.strip().strip('"') for ln in out.splitlines() if ln.strip()]


def _on_disk() -> set[str]:
    return {
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*")
        if ".git" not in p.parts and ".venv" not in p.parts
    }


def _resolves(ref: str, disk: set[str]) -> bool:
    """Return True if `ref` exists, or is a truncation of something that does."""
    if (ROOT / ref).exists():
        return True
    pre = ref.rstrip("/")
    return any(
        d == pre or d.startswith(pre + " ") or d.startswith(pre + "/") for d in disk
    )


def _plausible(ref: str) -> bool:
    last = ref.rstrip("/").rsplit("/", 1)[-1]
    return not last.isdigit() and any(c.isalpha() for c in last)


def _allowed(ref: str) -> str | None:
    """Return the recorded reason `ref` is exempt, or None if it is not."""
    for prefix, why in ALLOWED_PREFIXES.items():
        if ref.startswith(prefix):
            return why
    return None


def _referenced(extra: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Every repo-relative path referenced by a tracked file -> the files naming it."""
    hits: dict[str, list[str]] = {}
    for rel in [t for t in TRACKED if t != SELF] + list(extra):
        p = ROOT / rel
        if p.suffix not in READABLE and rel != "Justfile":
            continue
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in PATH_RE.finditer(text):
            ref = m.group(1).rstrip(".,);:'\"`")
            if PLACEHOLDER.search(ref) or not _plausible(ref):
                continue
            hits.setdefault(ref, []).append(rel)
        for m in BARE_RE.finditer(text):
            hits.setdefault(m.group(1), []).append(rel)
    return hits


def sweep_1(rep: Report) -> None:
    """No file the toolkit needs is hidden from a fresh clone by .gitignore."""
    rep.say("SWEEP 1 -- no file the toolkit needs is gitignored")
    cands = _candidate_code_files()
    rep.say(
        f"  scanned {len(cands)} code/doc/config files under "
        f"{', '.join((*CODE_ROOTS, SAMPLE_ROOT))} + repo root"
    )
    ignored = _ignored_among(cands)
    rep.check("none of them is ignored by git", not ignored, ", ".join(ignored[:10]))
    # Ablation: two paths this repo ignores ON PURPOSE. If the sweep cannot see
    # these, it cannot see an accidental ignore either.
    abl = _ignored_among(["config.toml", ".mcp.json"])
    rep.check(
        "ABLATION: the sweep DOES report known-ignored paths", len(abl) == 2, str(abl)
    )


def sweep_2(rep: Report) -> None:
    """No tracked file cites a code or doc path that does not exist."""
    rep.say()
    rep.say("SWEEP 2 -- no tracked file names a code or doc path that does not exist")
    disk = _on_disk()
    hits = _referenced()
    miss = [
        (r, sorted(set(s))) for r, s in sorted(hits.items()) if not _resolves(r, disk)
    ]
    allowed = [(r, s, _allowed(r)) for r, s in miss if _allowed(r)]
    live = [(r, s) for r, s in miss if not _allowed(r)]

    rep.say(
        f"  {len(hits)} distinct repo-relative paths referenced across "
        f"{len(TRACKED)} tracked files"
    )
    rep.say(f"  ALLOWED  {len(allowed)} unresolved references in reviewed classes:")
    for ref, srcs, why in allowed:
        rep.say(f"      - {ref}  <- {', '.join(srcs)}  ({why})")
    for ref, srcs in live:
        rep.say(f"      LIVE     {ref}   <- {', '.join(srcs)}")
    rep.check(
        "no live code or doc reference is dead", not live, f"{len(live)} unresolved"
    )

    # Ablation: inject a doc naming a module that has never existed.
    inject = ROOT / "docs" / "_ablation_probe.md"
    inject.write_text(
        "see src/surveycto_extractor/module_that_never_existed.py for details\n",
        encoding="utf-8",
    )
    try:
        abl_hits = _referenced(extra=("docs/_ablation_probe.md",))
        named = [
            r
            for r in abl_hits
            if r.endswith("module_that_never_existed.py") and not _resolves(r, disk)
        ]
        rep.check(
            "ABLATION: the sweep DOES name an injected dead reference",
            len(named) == 1,
            str(named),
        )
    finally:
        inject.unlink()
    # Ablation: the allowlist must suppress a REFERENCE, never a whole directory
    # it happens to sit under. A real dead path beside an allowed one still reports.
    rep.check(
        "ABLATION: the allowlist does not swallow a neighbouring dead path",
        _allowed(".claude/skills/survey-expert/x.py") is not None
        and _allowed("scripts/no_such_helper.py") is None,
        "",
    )


def sweep_3(rep: Report) -> None:
    """Every module under src/ parses and imports."""
    rep.say()
    rep.say("SWEEP 3 -- every module under src/ parses and imports")
    pys = sorted((ROOT / "src").rglob("*.py"))
    mods = [
        ".".join(p.relative_to(ROOT / "src").with_suffix("").parts)
        for p in pys
        if p.name != "__init__.py"
    ]
    bad_parse = []
    for p in pys:
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad_parse.append(f"{p.relative_to(ROOT)}: {exc}")
    rep.say(f"  {len(pys)} .py files under src/; {len(mods)} importable modules")
    rep.check("every src/**/*.py parses", not bad_parse, "; ".join(bad_parse[:3]))

    code = (
        "import importlib,sys\n"
        "bad=[]\n"
        "for m in sys.argv[1:]:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as e:\n"
        "        bad.append(f'{m}: {type(e).__name__}: {e}')\n"
        "print(chr(10).join(bad))\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code, *mods], cwd=ROOT, capture_output=True, text=True
    )
    bad_import = [ln for ln in r.stdout.splitlines() if ln.strip()]
    for b in bad_import:
        rep.say(f"      IMPORT FAIL  {b}")
    rep.check(
        "every module imports",
        not bad_import and r.returncode == 0,
        "; ".join(bad_import[:3]) or r.stderr.strip()[:200],
    )
    r2 = subprocess.run(
        [sys.executable, "-c", code, "surveycto_extractor.no_such_module_here"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    rep.check(
        "ABLATION: an unimportable module IS reported",
        "no_such_module_here" in r2.stdout,
        r2.stdout.strip()[:100],
    )


def main() -> None:
    """Run all three sweeps, write the stamped report, exit 1 on any failure."""
    rep = Report()
    rep.say(
        f"INTEGRITY CHECK -- measured at HEAD {_head()}, {len(TRACKED)} tracked files"
    )
    rep.say()
    sweep_1(rep)
    sweep_2(rep)
    sweep_3(rep)
    rep.say()
    verdict = "PASS" if not rep.failures else "FAIL -- " + "; ".join(rep.failures)
    rep.say(f"INTEGRITY CHECK: {verdict}")
    # data/ is gitignored in full, so the report can never be committed by
    # accident -- the same reason .dep-accept.toml keeps its outputs under
    # sample/output/.
    out = ROOT / "data" / "_integrity" / "integrity_sweeps.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(rep.lines) + "\n", encoding="utf-8")
    print(f"  -> {out.relative_to(ROOT).as_posix()}")
    raise SystemExit(1 if rep.failures else 0)


if __name__ == "__main__":
    main()

# OUTPUT VARIABLES (data/_integrity/integrity_sweeps.txt)
#   line 1        the HEAD and tracked-file count the sweeps measured, so a
#                 published number can always be tied to the commit it describes
#   SWEEP 1..3    per-sweep results, each with its ABLATION lines
#   final line    PASS, or FAIL with the failing labels
