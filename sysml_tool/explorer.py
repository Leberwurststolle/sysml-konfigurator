"""Explore the design space: draw configurations and check them in batches.

Every complete selection of a product line has an index in [0, combinations),
decoded like a mixed-radix number. Variants with nested variation points take
one digit range per nested choice, so the numbering covers exactly the complete
selections. This gives

* uniform random sampling over complete selections (`random`),
* an exhaustive pass for small models (`all`, paged by index),
* neighbours of admissible configurations (`neighbors`): one or two variation
  points changed, so the sample concentrates where admissible ones are,
* selections passed in by the caller (`given`), e.g. the modelled configurations.

All candidates are checked by `ConfigurationChecker.check_many`, i.e. by the
toolkit's `verify`; nothing here evaluates rules itself.
"""
from __future__ import annotations

import random
from typing import Iterable, Optional

from .checker import ConfigurationChecker, active_variation_points, complete_selection

MODES = ("random", "neighbors", "all", "given")


def selection_key(selection: dict) -> str:
    """Canonical text for a selection, used to recognise duplicates."""
    return ",".join(f"{k}.{v}" for k, v in sorted(selection.items()) if v)


class DesignSpace:
    """Index <-> selection numbering of one product line."""

    def __init__(self, pl: dict):
        self.pl = pl
        self.vps = {vp["name"]: vp for vp in pl["variationPoints"]}
        self._counts: dict = {}
        self.top = [vp for vp in pl["variationPoints"] if vp["parent"] is None]
        self.total = _product(self._count(vp) for vp in self.top)

    def _count(self, vp: dict) -> int:
        if vp["name"] not in self._counts:
            self._counts[vp["name"]] = max(1, sum(self._variant_count(v) for v in vp["variants"]))
        return self._counts[vp["name"]]

    def _variant_count(self, variant: dict) -> int:
        return _product(self._count(self.vps[k]) for k in variant["nested"] if k in self.vps)

    def selection_at(self, index: int) -> dict:
        if not 0 <= index < self.total:
            raise IndexError(index)
        selection: dict = {}
        self._decode(self.top, index, selection)
        return selection

    def _decode(self, vps: list[dict], index: int, selection: dict) -> None:
        for vp in reversed(vps):  # last variation point = lowest digit
            index, digit = divmod(index, self._count(vp))
            for variant in vp["variants"]:
                n = self._variant_count(variant)
                if digit < n:
                    selection[vp["name"]] = variant["name"]
                    self._decode([self.vps[k] for k in variant["nested"] if k in self.vps], digit, selection)
                    break
                digit -= n

    def random_selection(self, rng: random.Random) -> dict:
        return self.selection_at(rng.randrange(self.total))

    def neighbor(self, selection: dict, rng: random.Random) -> Optional[dict]:
        """Change one or two active variation points to another variant (nested choices drawn at random)."""
        out = dict(selection)
        for _ in range(rng.choice((1, 1, 2))):
            choices = [vp for vp in active_variation_points(self.pl, out) if len(vp["variants"]) > 1]
            if not choices:
                return None
            vp = rng.choice(choices)
            out[vp["name"]] = rng.choice([v["name"] for v in vp["variants"] if v["name"] != out.get(vp["name"])])
            out = complete_selection(self.pl, out)
            for nested in active_variation_points(self.pl, out):  # random instead of first nested variant
                if nested["parent"] == {"variationPoint": vp["name"], "variant": out[vp["name"]]}:
                    out[nested["name"]] = rng.choice(nested["variants"])["name"]
        return out


def explore(checker: ConfigurationChecker, product_line: int, mode: str, count: int, seed: int = 0,
            offset: int = 0, around: Iterable[dict] = (), known: Iterable[str] = (),
            selections: Iterable[dict] = ()) -> dict:
    """Draw up to `count` new selections and check them in one verify run."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    pl = checker.ds["productLines"][product_line]
    space = DesignSpace(pl)
    rng = random.Random(seed)
    seen = set(known)
    picked: list[dict] = []

    def take(selection: Optional[dict]) -> None:
        if selection is None:
            return
        key = selection_key(selection)
        if key not in seen:
            seen.add(key)
            picked.append(selection)

    next_offset = offset
    if mode == "given":
        for selection in list(selections)[:count]:
            take(complete_selection(pl, selection))
    elif mode == "all":
        next_offset = min(space.total, offset + count)
        for i in range(offset, next_offset):
            take(space.selection_at(i))
    else:
        bases = list(around)
        attempts = 0
        while len(picked) < count and attempts < count * 20 and len(seen) < space.total:
            attempts += 1
            if mode == "neighbors" and bases:
                take(space.neighbor(rng.choice(bases), rng))
            else:
                take(space.random_selection(rng))

    results = checker.check_many(product_line, picked, with_requirements=False) if picked else []
    units = {name: v["unit"] for r in results for name, v in r["keyFigures"].items() if v and v.get("unit")}
    return {
        "mode": mode,
        "total": space.total,
        "nextOffset": next_offset,
        "units": units,
        "points": [{
            "key": selection_key(r["selection"]),
            "selection": r["selection"],
            "status": r["status"],
            "figures": {name: (v["value"] if v and isinstance(v.get("value"), (int, float))
                               and not isinstance(v.get("value"), bool) else None)
                        for name, v in r["keyFigures"].items()},
            "violated": [x["label"] for x in r["rules"] if x["verdict"] == "violated"],
        } for r in results],
    }


def _product(numbers: Iterable[int]) -> int:
    result = 1
    for n in numbers:
        result *= max(n, 1)
    return result
