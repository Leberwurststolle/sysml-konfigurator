"""Check configurations of a product line with the toolkit's `verify`.

For every configuration to check, a small SysML package is generated next to
the model:

    part cfg_0 :> PcModell::pcProduktlinie {
        part :>> cpu :> PcModell::pcProduktlinie::cpu::ryzen5;  // the selection
        part :>> gehaeuse :> PcModell::pcProduktlinie::gehaeuse::midiTower {
            attribute :>> farbe :> ...::midiTower::farbe::weiss;   // nested selection
        }
        ...
        assert constraint rule_0 { cpu.sockel == mainboard.sockel }    // system rules, re-asserted
        assert constraint rule_12 { cpu.threads >= cpu.kerne }         // component rule, rebased
        assert constraint figure_0 { gesamtPreis != gesamtPreis }      // probe, see below
    }
    satisfy PcModell::gamingAnforderungen by cfg_0;                     // requirements

Why the rules are re-asserted: `verify` evaluates an assert constraint in the
context that owns it. The rules live in the system definition (or in a part or
interface definition), where the parts are still unbound; copied into the
configuration they are evaluated against the selected variants.

Why the probes: `sysmlv2 eval` (v0.10.10) does not follow a variant selected by
a feature value, but `verify` does, and it reports the values that decided a
violated constraint. `x != x` is always violated, so its report carries the
value of `x`.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .model_graph import spell_name
from .toolkit import Toolkit, Verdict

PACKAGE = "KonfiguratorPruefung"


class ConfigurationChecker:
    def __init__(self, toolkit: Toolkit, design_space: dict, max_parallel: int = 2):
        self.tk = toolkit
        self.ds = design_space
        self.files = [Path(f) for f in design_space["files"]]
        self._slots = threading.Semaphore(max_parallel)

    # ------------------------------------------------------------ public

    def check(self, product_line: int, selection: dict, with_requirements: bool = True) -> dict:
        return self.check_many(product_line, [selection], with_requirements)[0]

    def check_many(self, product_line: int, selections: list[dict], with_requirements: bool = True) -> list[dict]:
        pl = self.ds["productLines"][product_line]
        text, line_map = self.render(pl, selections, with_requirements)
        with self._slots:
            tmp = Path(tempfile.mkdtemp(prefix="konfigurator-"))
            try:
                generated = tmp / f"{PACKAGE}.sysml"
                generated.write_text(text, encoding="utf-8")
                verdicts = self.tk.verify([*self.files, generated])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return self._interpret(pl, selections, verdicts, generated, line_map, with_requirements)

    def alternatives(self, product_line: int, selection: dict) -> dict:
        """Status of every single-slot change to `selection`, checked in one verify run.

        Changing a variant activates its nested variation points; they are filled
        with their first variant so that the alternative is a complete selection.
        """
        pl = self.ds["productLines"][product_line]
        variants_to_try = []
        for vp in active_variation_points(pl, selection):
            for v in vp["variants"]:
                changed = complete_selection(pl, dict(selection, **{vp["name"]: v["name"]}))
                variants_to_try.append((vp["name"], v["name"], changed))
        results = self.check_many(product_line, [c for _, _, c in variants_to_try], with_requirements=False)
        out: dict = {}
        for (vp_name, v_name, _), r in zip(variants_to_try, results):
            out.setdefault(vp_name, {})[v_name] = {
                "status": r["status"],
                "violatedRules": [x["label"] for x in r["rules"] if x["verdict"] == "violated"],
                "keyFigures": r["keyFigures"],
            }
        return out

    def snippet(self, product_line: int, selection: dict, name: str = "meineKonfiguration") -> str:
        """The configuration as SysML text to paste into the model."""
        pl = self.ds["productLines"][product_line]
        lines = [f"{pl['keyword']} {spell_name(name)} {':' if pl['isDefinition'] else ':>'} {pl['reference']} {{"]
        lines += [f"    {line}" for line in self._selection_lines(pl, selection)]
        lines.append("}")
        return "\n".join(lines)

    # ---------------------------------------------------------- rendering

    def _selection_lines(self, pl: dict, selection: dict, parent: Optional[dict] = None) -> list[str]:
        """`part :>> cpu :> productLine::cpu::ryzen5;`, with nested selections in a body.

        Subsetting (`:>`) rather than a feature value (`=`): `verify` evaluates a
        nested selection only when the part specializes the selected variant.
        """
        lines = []
        for vp in pl["variationPoints"]:
            if vp["parent"] != parent:
                continue
            chosen = selection.get(vp["name"])
            variant = next((v for v in vp["variants"] if v["name"] == chosen), None)
            if variant is None:
                continue
            head = f"{vp['keyword']} :>> {spell_name(vp['feature'])} :> {variant['reference']}"
            inner = self._selection_lines(pl, selection, {"variationPoint": vp["name"], "variant": variant["name"]})
            if inner:
                lines += [f"{head} {{", *(f"    {line}" for line in inner), "}"]
            else:
                lines.append(f"{head};")
        return lines

    def render(self, pl: dict, selections: list[dict], with_requirements: bool) -> tuple[str, dict]:
        out: list[str] = []
        line_map: dict[int, tuple] = {}

        def emit(text: str, key: Optional[tuple] = None) -> None:
            out.append(text)
            if key is not None:
                line_map[len(out)] = key

        emit(f"package {PACKAGE} {{")
        for target in self.ds["context"]["imports"]:
            emit(f"    private import {target};")

        req_refs = []
        if with_requirements:
            for i, req in enumerate(pl["requirements"]):
                if req["isDefinition"]:
                    emit(f"    requirement req_{i} : {req['reference']};")
                    req_refs.append(f"req_{i}")
                else:
                    req_refs.append(req["reference"])

        relation = ":" if pl["isDefinition"] else ":>"
        for c, selection in enumerate(selections):
            emit(f"    {pl['keyword']} cfg_{c} {relation} {pl['reference']} {{")
            for line in self._selection_lines(pl, selection):
                emit(f"        {line}")
            for r, rule in enumerate(pl["rules"]):
                if not rule["expression"] or not applies(rule, selection):
                    continue
                body = " ".join(rule["expression"].split())
                if rule["negated"]:
                    body = f"not ({body})"
                emit(f"        assert constraint rule_{r} {{ {body} }}", (c, "rule", r))
            for k, fig in enumerate(pl["keyFigures"]):
                n = spell_name(fig["name"])
                emit(f"        assert constraint figure_{k} {{ {n} != {n} }}", (c, "figure", k))
            emit("    }")
            for q, ref in enumerate(req_refs):
                emit(f"    satisfy {ref} by cfg_{c};", (c, "requirement", q))
        emit("}")
        return "\n".join(out) + "\n", line_map

    # ------------------------------------------------------- interpretation

    def _interpret(self, pl: dict, selections: list[dict], verdicts: list[Verdict], generated: Path,
                   line_map: dict, with_requirements: bool) -> list[dict]:
        results = []
        for selection in selections:
            results.append({
                "selection": selection,
                "complete": all(selection.get(vp["name"]) for vp in active_variation_points(pl, selection)),
                "rules": [{"index": i, "name": r["name"], "label": r["label"], "scope": r["scope"],
                           "context": r["context"], "expression": r["expression"],
                           "verdict": "undecided" if applies(r, selection) else "notApplicable",
                           "detail": None if r["expression"] else "kein Quelltext gefunden", "bindings": {}}
                          for i, r in enumerate(pl["rules"])],
                "keyFigures": {f["name"]: None for f in pl["keyFigures"]},
                "requirements": [] if not with_requirements else [
                    {"index": i, "name": q["name"], "verdict": "undecided", "detail": None,
                     "constraints": [{"expression": c["expression"], "kind": c["kind"], "verdict": None,
                                      "bindings": {}, "detail": None} for c in q["constraints"]]}
                    for i, q in enumerate(pl["requirements"])],
            })

        constraint_index = {}
        for qi, req in enumerate(pl["requirements"]):
            for ci, c in enumerate(req["constraints"]):
                if c["file"]:
                    constraint_index[(_norm(c["file"]), c["line"])] = (qi, ci)

        gen = _norm(str(generated))
        for v in verdicts:
            if _norm(v.file) != gen or v.line not in line_map:
                continue
            c, kind, idx = line_map[v.line]
            res = results[c]
            if kind == "rule":
                res["rules"][idx].update(verdict=v.verdict, detail=v.detail, bindings=v.bindings)
            elif kind == "figure":
                name = pl["keyFigures"][idx]["name"]
                res["keyFigures"][name] = v.bindings.get(name) if v.verdict == "violated" else None
            elif kind == "requirement":
                req = res["requirements"][idx]
                req.update(verdict=v.verdict, detail=v.detail)
                listed = set()
                for child in v.children:
                    hit = constraint_index.get((_norm(child.file), child.line))
                    if hit and hit[0] == idx:
                        req["constraints"][hit[1]].update(verdict=child.verdict, bindings=child.bindings,
                                                          detail=child.detail)
                        listed.add(hit[1])
                if v.verdict in ("satisfied", "violated"):
                    # `verify` lists only the constraints that decided a failed claim.
                    for ci, con in enumerate(req["constraints"]):
                        if ci not in listed:
                            con["verdict"] = "satisfied"

        for res in results:
            verdicts_ = [r["verdict"] for r in res["rules"] if r["verdict"] != "notApplicable"]
            if "violated" in verdicts_:
                res["status"] = "invalid"
            elif all(x == "satisfied" for x in verdicts_) and res["complete"]:
                res["status"] = "valid"
            else:
                res["status"] = "unknown"
        return results


def active_variation_points(pl: dict, selection: dict) -> list[dict]:
    """Top-level variation points, plus nested ones whose variant is selected (parents come first in the list)."""
    active, names = [], set()
    for vp in pl["variationPoints"]:
        parent = vp["parent"]
        if parent is None or (parent["variationPoint"] in names
                              and selection.get(parent["variationPoint"]) == parent["variant"]):
            active.append(vp)
            names.add(vp["name"])
    return active


def complete_selection(pl: dict, selection: dict) -> dict:
    """Drop choices of inactive nested variation points; choose the first variant of newly active ones."""
    out = dict(selection)
    while True:
        active = active_variation_points(pl, out)
        names = {vp["name"] for vp in active}
        cleaned = {k: v for k, v in out.items() if k in names}
        for vp in active:
            if vp["parent"] is not None and not cleaned.get(vp["name"]) and vp["variants"]:
                cleaned[vp["name"]] = vp["variants"][0]["name"]
        if cleaned == out:
            return out
        out = cleaned


def applies(rule: dict, selection: dict) -> bool:
    """A rule from a variant's own type applies only while that variant is selected."""
    return not rule.get("appliesTo") or all(selection.get(vp) in variants for vp, variants in rule["appliesTo"].items())


def _norm(path: str) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return path.lower()
