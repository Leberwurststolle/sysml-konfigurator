"""Thin wrapper around the `sysmlv2` command line tool of the Open-MBEE sysml-toolkit.

All semantic work (parsing, name resolution, expression evaluation, constraint
verification) is done by the toolkit. This module only starts the process and
turns its text output into Python data.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TOOLKIT_DIR = PROJECT_DIR / "toolkit"


class ToolkitError(RuntimeError):
    pass


def find_executable(explicit: Optional[str] = None) -> Path:
    candidates = [explicit, os.environ.get("SYSMLV2_BIN")]
    exe = "sysmlv2.exe" if os.name == "nt" else "sysmlv2"
    candidates.append(str(DEFAULT_TOOLKIT_DIR / "bin" / exe))
    candidates.append(shutil.which("sysmlv2"))
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise ToolkitError("sysmlv2 executable not found. Run `python setup_toolkit.py` first "
                       "or pass --sysmlv2 / set SYSMLV2_BIN.")


def find_library(explicit: Optional[str] = None) -> Path:
    candidates = [explicit, os.environ.get("SYSMLV2_LIB_DIR"),
                  str(DEFAULT_TOOLKIT_DIR / "stdlib" / "sysml-library.json")]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    raise ToolkitError("SysML standard library not found. Run `python setup_toolkit.py` first "
                       "or pass --lib / set SYSMLV2_LIB_DIR.")


# ---------------------------------------------------------------------------
# Values as the toolkit prints them
# ---------------------------------------------------------------------------

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_RE_NUMBER = re.compile(rf"^(?P<approx>≈)?(?P<num>{_NUMBER})(?:/(?P<den>\d+))?$")
_RE_QUANTITY = re.compile(rf"^(?P<num>≈?{_NUMBER}(?:/\d+)?) \[(?P<unit>[^\]]+)\]$")
_RE_ELEMENT = re.compile(r"^(?P<qn>.+) \((?P<meta>[A-Za-z]+)\)$")


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` outside of (), [] and string literals."""
    parts, depth, quote, start = [], 0, None, 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def _number(text: str) -> Optional[float]:
    m = _RE_NUMBER.match(text.strip())
    if not m:
        return None
    value = float(m.group("num"))
    if m.group("den"):
        value /= float(m.group("den"))
    return value


def parse_value(text: str) -> dict:
    """Turn one printed value into {kind, text, display, value, unit, items}."""
    t = text.strip()
    out = {"kind": "unknown", "text": t, "display": t, "value": None}
    if t.startswith("<") and t.endswith(">"):
        return out
    if len(t) >= 2 and t[0] == t[-1] == '"':
        s = t[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out.update(kind="string", value=s, display=s)
        return out
    if t in ("true", "false"):
        out.update(kind="boolean", value=(t == "true"), display="ja" if t == "true" else "nein")
        return out
    num = _number(t)
    if num is not None:
        out.update(kind="number", value=num, display=_fmt(num) if "/" in t or "≈" in t else t)
        return out
    m = _RE_QUANTITY.match(t)
    if m:
        num = _number(m.group("num"))
        num_text = m.group("num") if "/" not in m.group("num") and "≈" not in m.group("num") else _fmt(num)
        out.update(kind="quantity", value=num, unit=m.group("unit"), display=f"{num_text} {m.group('unit')}")
        return out
    if t.startswith("(") and t.endswith(")"):
        items = [parse_value(p) for p in split_top_level(t[1:-1])]
        return make_list(items, t)
    m = _RE_ELEMENT.match(t)
    if m:
        qn = m.group("qn")
        out.update(kind="element", value=qn, metaclass=m.group("meta"), display=qn.split("::")[-1])
        return out
    return out


def make_list(items: list[dict], text: Optional[str] = None) -> dict:
    if len(items) == 1:
        return items[0]
    display = ", ".join(i["display"] for i in items)
    return {"kind": "list", "text": text or f"({display})", "display": display, "value": None, "items": items}


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "?"
    return f"{value:.6g}"


# ---------------------------------------------------------------------------
# verify output
# ---------------------------------------------------------------------------

_RE_VERDICT = re.compile(
    r"^(?P<indent>\s*)(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)\s+(?P<name>.+?) "
    r"\((?P<meta>[A-Za-z]+)(?:, satisfies (?P<req>.+?) by (?P<by>.+?))?\): "
    r"(?P<verdict>satisfied|VIOLATED|undecided)(?: \((?P<detail>.*?)\))?(?: — (?P<extra>.*))?$"
)


@dataclass
class Verdict:
    file: str
    line: int
    col: int
    name: Optional[str]
    metaclass: str
    verdict: str                      # "satisfied" | "violated" | "undecided"
    detail: Optional[str] = None      # reason text, e.g. "result is indeterminate ..."
    bindings: dict = field(default_factory=dict)   # "with a = 1, b = 2 [W]" -> parsed values
    requirement: Optional[str] = None  # for satisfy claims
    satisfied_by: Optional[str] = None
    children: list = field(default_factory=list)   # constraint verdicts listed below a claim

    def to_dict(self) -> dict:
        return {
            "file": self.file, "line": self.line, "col": self.col, "name": self.name,
            "metaclass": self.metaclass, "verdict": self.verdict, "detail": self.detail,
            "bindings": self.bindings, "requirement": self.requirement,
            "satisfiedBy": self.satisfied_by, "children": [c.to_dict() for c in self.children],
        }


def parse_verify_output(text: str) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for raw in text.splitlines():
        m = _RE_VERDICT.match(raw.rstrip())
        if not m:
            continue
        detail = m.group("detail")
        bindings = {}
        if detail and detail.startswith("with "):
            for part in split_top_level(detail[5:]):
                if " = " in part:
                    key, value = part.split(" = ", 1)
                    bindings[key.strip()] = parse_value(value)
        name = m.group("name")
        v = Verdict(
            file=m.group("file"), line=int(m.group("line")), col=int(m.group("col")),
            name=None if name == "<anonymous>" else name, metaclass=m.group("meta"),
            verdict=m.group("verdict").lower(), detail=detail, bindings=bindings,
            requirement=m.group("req"), satisfied_by=m.group("by"),
        )
        if m.group("indent") and verdicts:
            verdicts[-1].children.append(v)
        else:
            verdicts.append(v)
    return verdicts


# ---------------------------------------------------------------------------
# check output
# ---------------------------------------------------------------------------

_RE_DIAG_HEAD = re.compile(r"^(?P<sev>error|warning)(?:\[[^\]]*\])?: (?P<msg>.*)$")
_RE_DIAG_LOC = re.compile(r"^\s*--> (?P<file>.+?):(?P<line>\d+):(?P<col>\d+)\s*$")


def parse_diagnostics(text: str) -> list[dict]:
    diags: list[dict] = []
    for raw in text.splitlines():
        head = _RE_DIAG_HEAD.match(raw)
        if head:
            diags.append({"severity": head.group("sev"), "message": head.group("msg"),
                          "file": None, "line": None, "col": None})
            continue
        loc = _RE_DIAG_LOC.match(raw)
        if loc and diags and diags[-1]["file"] is None:
            diags[-1].update(file=loc.group("file"), line=int(loc.group("line")), col=int(loc.group("col")))
    return diags


# ---------------------------------------------------------------------------
# Toolkit
# ---------------------------------------------------------------------------

_RE_DESCRIBE_LOC = re.compile(r"^\s*location\s+(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)\s*$", re.M)


class Toolkit:
    def __init__(self, executable: Optional[str] = None, library: Optional[str] = None):
        self.exe = find_executable(executable)
        self.lib = find_library(library)

    def run(self, args: Sequence[str], timeout: float = 600) -> subprocess.CompletedProcess:
        cmd = [str(self.exe), *args]
        # The toolkit prints UTF-8 on every platform; never rely on the Windows console code page.
        return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)

    def version(self) -> str:
        return self.run(["--version"]).stdout.strip()

    def check(self, files: Sequence[Path]) -> list[dict]:
        proc = self.run(["check", "--lib", str(self.lib), *map(str, files)])
        return parse_diagnostics(proc.stderr + "\n" + proc.stdout)

    def compact_json(self, files: Sequence[Path]) -> str:
        proc = self.run(["convert", "--to", "compact-json", "--lib", str(self.lib), "-q", *map(str, files)])
        if proc.returncode != 0:
            raise ToolkitError(f"convert failed:\n{proc.stderr}")
        return proc.stdout

    def verify(self, files: Sequence[Path]) -> list[Verdict]:
        proc = self.run(["verify", "--lib", str(self.lib), "-q", *map(str, files)])
        verdicts = parse_verify_output(proc.stdout)
        # Exit code 1 only means "something is violated"; a missing summary means it did not run.
        if not verdicts and proc.returncode != 0 and "satisfied," not in proc.stdout:
            raise ToolkitError(f"verify failed:\n{proc.stderr or proc.stdout}")
        return verdicts

    def describe_location(self, files: Sequence[Path], qualified_name: str) -> Optional[tuple[str, int, int]]:
        proc = self.run(["describe", "--lib", str(self.lib), "-q", *map(str, files), qualified_name])
        m = _RE_DESCRIBE_LOC.search(proc.stdout)
        if not m:
            return None
        return m.group("file"), int(m.group("line")), int(m.group("col"))

    def query_lines(self, files: Sequence[Path], expression: str) -> tuple[list[str], str]:
        """Run `query`; returns (result lines, stderr)."""
        first, *rest = map(str, files)
        proc = self.run(["query", "--lib", str(self.lib), "-q", first, expression, *rest])
        if proc.returncode != 0:
            return [], proc.stderr.strip()
        return [line for line in proc.stdout.splitlines() if line.strip()], proc.stderr.strip()
