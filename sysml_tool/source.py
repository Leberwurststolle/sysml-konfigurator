"""Small, comment-aware helpers over SysML source text.

The toolkit tells us *where* an element is declared (`sysmlv2 describe`); these
helpers cut the constraint bodies out of the text at that location so they can
be shown to the user and re-asserted inside a generated configuration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_IMPORT = re.compile(r"\b(?:(?:private|public|protected)\s+)?import\s+(?:all\s+)?([^;{]+?)\s*;")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SourceFile:
    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text(encoding="utf-8-sig")
        self._line_starts = [0]
        for i, ch in enumerate(self.text):
            if ch == "\n":
                self._line_starts.append(i + 1)
        self._code_mask = _code_mask(self.text)

    def offset(self, line: int, col: int) -> int:
        """1-based line/column (as printed by the toolkit) -> character offset."""
        return self._line_starts[line - 1] + col - 1

    def line_col(self, offset: int) -> tuple[int, int]:
        lo, hi = 0, len(self._line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, offset - self._line_starts[lo] + 1

    def is_code(self, offset: int) -> bool:
        return self._code_mask[offset]

    def find_block(self, start: int) -> Optional[tuple[int, int]]:
        """Offsets of the `{` ... `}` block that follows `start`; None if a `;` comes first."""
        i = start
        n = len(self.text)
        while i < n:
            if self.is_code(i):
                ch = self.text[i]
                if ch == ";":
                    return None
                if ch == "{":
                    end = self.matching_brace(i)
                    return (i, end) if end is not None else None
            i += 1
        return None

    def matching_brace(self, open_at: int) -> Optional[int]:
        depth = 0
        for i in range(open_at, len(self.text)):
            if not self.is_code(i):
                continue
            ch = self.text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        return None

    def constraint_blocks(self, block: tuple[int, int]) -> list["ConstraintSpan"]:
        """Every `... constraint [name] { body }` directly inside the given block, in source order."""
        open_at, close_at = block
        out: list[ConstraintSpan] = []
        i = open_at + 1
        depth = 0
        while i < close_at:
            if not self.is_code(i):
                i += 1
                continue
            ch = self.text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif depth == 0 and ch.isalpha():
                m = _WORD.match(self.text, i)
                word = m.group(0) if m else ""
                if word == "constraint" and (i == 0 or not (self.text[i - 1].isalnum() or self.text[i - 1] == "_")):
                    span = self._constraint_after(i + len(word), close_at)
                    if span is not None:
                        out.append(span)
                        i = span.close_at + 1
                        continue
                i += len(word) or 1
                continue
            i += 1
        return out

    def _constraint_after(self, i: int, limit: int) -> Optional["ConstraintSpan"]:
        header_end = i
        while header_end < limit and not (self.is_code(header_end) and self.text[header_end] in "{;"):
            header_end += 1
        if header_end >= limit or self.text[header_end] == ";":
            return None
        header = self.text[i:header_end].strip()
        if header.startswith("def ") or header == "def":
            return None
        close_at = self.matching_brace(header_end)
        if close_at is None:
            return None
        name = None
        m = _WORD.match(header)
        if m and not header.startswith((":", "~")):
            name = m.group(0)
        body_start = header_end + 1
        while body_start < close_at and self.text[body_start].isspace():
            body_start += 1
        return ConstraintSpan(name=name, open_at=header_end, close_at=close_at, body_start=body_start,
                              body=_strip_comments(self.text[header_end + 1:close_at]).strip())

    def imports(self) -> list[str]:
        """Targets of all import statements (`ScalarValues::*`, `SequenceFunctions::includes`)."""
        out = []
        for m in _IMPORT.finditer(self.text):
            if self.is_code(m.start()):
                out.append(" ".join(m.group(1).split()))
        return out


@dataclass
class ConstraintSpan:
    name: Optional[str]
    open_at: int
    close_at: int
    body_start: int
    body: str


_CHAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*")


def rewrite_chains(expression: str, rewrite: Callable[[list[str]], Optional[str]]) -> str:
    """Replace feature chains (`a.b.c`) in an expression by `rewrite(["a", "b", "c"])`.

    Only chains that start a name are passed on: not the tail of `x.y`, not parts
    of qualified names (`Pkg::Enum::literal`), not text in strings. A chain is
    kept as it is when `rewrite` returns None.
    """
    mask = _code_mask(expression)
    out, last = [], 0
    for m in _CHAIN.finditer(expression):
        start, end = m.start(), m.end()
        if not mask[start]:
            continue
        before = expression[:start].rstrip()
        if before.endswith((".", "::")) or expression[end:].lstrip().startswith("::"):
            continue
        replacement = rewrite([s.strip() for s in m.group(0).split(".")])
        if replacement is not None:
            out += [expression[last:start], replacement]
            last = end
    out.append(expression[last:])
    return "".join(out)


def _code_mask(text: str) -> list[bool]:
    """True for characters that are code (not inside comments or string/name literals)."""
    mask = [True] * len(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                mask[k] = False
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                mask[k] = False
            i = j
        elif text[i] in "\"'":
            quote = text[i]
            j = i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                mask[k] = False
            i = j + 1
        else:
            i += 1
    return mask


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)
