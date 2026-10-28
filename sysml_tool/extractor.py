"""Extract the design space of a SysML v2 product-line model.

A *product line* is any usage or definition that owns variation points
(`variation part cpu : CPU { variant part ... }`). For each one the extractor
collects, generically and without model-specific names:

* the variation points and their variants, with every attribute value of a
  variant evaluated by the toolkit (units, enums, inherited values included),
* the asserted constraints of the system (compatibility rules),
* computed attributes of the system (key figures such as total price),
* requirements whose subject the product line can satisfy,
* configurations already modelled (`part x :> productLine { part :>> cpu = ... }`),
* the toolkit's verification verdicts for the model as it is.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

from .model_graph import ModelGraph, spell_name, usage_keyword
from .source import SourceFile, rewrite_chains
from .toolkit import Toolkit, ToolkitError, make_list, parse_value

SCHEMA = "sysml-design-space/2"
_SEP = "@@SEP@@"
_MAX_QUERY_CHARS = 20000  # Windows limits a command line to 32767 characters


class ExtractionError(RuntimeError):
    def __init__(self, message: str, diagnostics: Optional[list] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


def collect_model_files(paths: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files.extend(sorted(x for x in p.rglob("*") if x.suffix in (".sysml", ".kerml")))
        elif p.suffix in (".sysml", ".kerml") and p.is_file():
            files.append(p)
        else:
            raise ExtractionError(f"Not a SysML file or folder: {p}")
    if not files:
        raise ExtractionError("No .sysml/.kerml files found.")
    return [f.resolve() for f in files]


class Extractor:
    def __init__(self, toolkit: Toolkit, files: list[Path]):
        self.tk = toolkit
        self.files = files
        self.sources = {f: SourceFile(f) for f in files}
        self.graph: Optional[ModelGraph] = None
        self.warnings: list[str] = []
        self._block_cache: dict[str, Optional[tuple[SourceFile, tuple[int, int]]]] = {}
        self._by_qn: Optional[dict[str, dict]] = None

    # ------------------------------------------------------------------ main

    def run(self) -> dict:
        started = time.time()
        diagnostics = self.tk.check(self.files)
        errors = [d for d in diagnostics if d["severity"] == "error"]
        if errors:
            raise ExtractionError("The model has errors; fix them first (see `sysmlv2 check`).", errors)

        self.graph = ModelGraph(json.loads(self.tk.compact_json(self.files)))
        model_verdicts = self.tk.verify(self.files)

        product_lines = [self._product_line(owner, vps) for owner, vps in self._find_product_lines()]

        return {
            "schema": SCHEMA,
            "generator": {"toolkit": self.tk.version(), "library": str(self.tk.lib)},
            "files": [str(f) for f in self.files],
            "packages": [self.graph.name(p) for p in self.graph.elements("Package")
                         if self.graph.owner(p) is not None and self.graph.owner(p)["@type"] == "Namespace"],
            "context": {"imports": self._imports()},
            "diagnostics": diagnostics,
            "elementCount": len(self.graph.by_id),
            "productLines": product_lines,
            "modelVerification": self._summarize(model_verdicts),
            "warnings": self.warnings,
            "extractionSeconds": round(time.time() - started, 2),
        }

    # ------------------------------------------------------- product lines

    def _find_product_lines(self) -> list[tuple[dict, list[dict]]]:
        g = self.graph
        variant_ids = {m["@id"] for e in g.elements() if e.get("isVariation")
                       for _, m in g.owned_members(e, ("VariantMembership",))}
        groups: dict[str, tuple[dict, list[dict]]] = {}
        for e in g.elements():
            if not self._is_variation_point(e):
                continue
            owner = g.owner(e)
            if owner is None or g.qualified_name(owner) is None:
                continue
            if owner.get("isVariation") or owner["@id"] in variant_ids:
                continue  # variants, and variation points nested in a variant, belong to the enclosing product line
            groups.setdefault(owner["@id"], (owner, []))[1].append(e)
        return list(groups.values())

    def _is_variation_point(self, e: dict) -> bool:
        if not self.graph.is_usage(e) or e["@type"] == "EnumerationUsage":
            return False
        return bool(e.get("isVariation")) or self._variation_definition(e) is not None

    def _variation_definition(self, usage: dict) -> Optional[dict]:
        for t in self.graph.declared_types(usage):
            if t.get("isVariation") and t["@type"] != "EnumerationDefinition":
                return t
        return None

    def _product_line(self, owner: dict, vps: list[dict]) -> dict:
        g = self.graph
        system_types = g.usage_type_closure(owner) if g.is_usage(owner) else g.definition_closure(owner)
        rule_owners = system_types + ([owner] if g.is_usage(owner) else [])

        variation_points = [entry for vp in vps for entry in self._variation_point(vp)]
        variants = [v for vp in variation_points for v in vp["variants"]]
        self._evaluate_attributes(variants)
        for v in variants:
            del v["_queries"]
        rules = self._rules(rule_owners)
        rules += self._component_rules(system_types, variation_points)
        rules += self._interface_rules(rule_owners)
        unnamed: dict[str, int] = {}
        for r in rules:
            where = (r["owner"] or "").split("::")[-1]
            if not r["name"]:
                unnamed[where] = unnamed.get(where, 0) + 1
            r["label"] = r["name"] or f"{where}: Regel {unnamed[where]}"
        return {
            "name": g.name(owner),
            "qualifiedName": g.qualified_name(owner),
            "reference": g.spelling(owner),
            "metaclass": owner["@type"],
            "keyword": usage_keyword(owner["@type"].replace("Definition", "Usage")),
            "isDefinition": g.is_definition(owner),
            "types": [g.qualified_name(t) for t in g.types(owner)] if g.is_usage(owner) else [],
            "doc": g.documentation(owner),
            "variationPoints": variation_points,
            "combinations": _combinations(variation_points),
            "rules": rules,
            "keyFigures": self._key_figures(rule_owners),
            "requirements": self._requirements(system_types),
            "configurations": self._configurations(owner, variation_points),
        }

    def _variation_point(self, vp: dict, parent: Optional[dict] = None) -> list[dict]:
        """The variation point, followed by the variation points nested in its variants.

        A nested variation point (a `variation` inside a variant) is only active
        while that variant is selected. Its key is `parent/variant/name`.
        """
        g = self.graph
        var_def = None if vp.get("isVariation") else self._variation_definition(vp)
        holder = vp if var_def is None else var_def
        variants = [m for rel, m in g.owned_members(holder, ("VariantMembership",))]
        types = g.types(vp)
        feature = g.name(vp)
        key = feature if parent is None else f"{parent['variationPoint']}/{parent['variant']}/{feature}"
        entries = [self._variant(v, feature) for v in variants]

        attribute_names: list[str] = []
        for e in entries:
            for name in e["attributes"]:
                if name not in attribute_names:
                    attribute_names.append(name)
        point = {
            "name": key,
            "feature": feature,
            "parent": parent,
            "qualifiedName": g.qualified_name(vp),
            "metaclass": vp["@type"],
            "keyword": usage_keyword(vp["@type"]),
            "type": g.qualified_name(types[0]) if types else None,
            "doc": g.documentation(vp),
            "variants": entries,
            "attributeNames": attribute_names,
        }
        out = [point]
        for v, entry in zip(variants, entries):
            for _, m in g.owned_members(v):
                if self._is_variation_point(m):
                    nested = self._variation_point(m, {"variationPoint": key, "variant": entry["name"]})
                    entry["nested"].append(nested[0]["name"])
                    out += nested
        return out

    def _variant(self, v: dict, feature: str) -> dict:
        g = self.graph
        types = g.types(v)
        ref = g.spelling(v)
        nested = {g.name(m) for _, m in g.owned_members(v) if self._is_variation_point(m)}
        names: list[str] = []
        for d in (g.definition_closure(types[0]) if types else []):
            for _, m in g.owned_members(d):
                if m["@type"] == "AttributeUsage" and (n := g.name(m)) and n not in names and n not in nested:
                    names.append(n)
        for _, m in g.owned_members(v):
            if m["@type"] == "AttributeUsage" and (n := g.name(m)) and n not in names and n not in nested:
                names.append(n)
        queries = {n: f"{ref}.{spell_name(n)}" for n in names}
        if v["@type"] == "AttributeUsage":
            # `variant attribute weiss = "Weiß";` is a value itself, shown under the variation point's name
            queries = {feature: ref}
        doc = g.documentation(v) or (g.documentation(types[0]) if types else None)
        return {
            "name": g.name(v),
            "qualifiedName": g.qualified_name(v),
            "reference": ref,
            "metaclass": v["@type"],
            "type": g.qualified_name(types[0]) if types else None,
            "doc": doc,
            "attributes": {n: None for n in queries},
            "nested": [],
            "_queries": queries,
        }

    # --------------------------------------------- attribute evaluation

    def _evaluate_attributes(self, variants: list[dict]) -> None:
        """Evaluate all attributes with as few `query` runs as possible (each run loads the model).

        Values are concatenated into one sequence expression with marker strings
        between them; chunks stay short enough for the Windows command line limit.
        """
        chunks: list[list[dict]] = [[]]
        length = 0
        for v in variants:
            if not v["attributes"] or not v["reference"]:
                v["attributes"] = {}
                continue
            size = sum(len(q) + len(_SEP) + 8 for q in v["_queries"].values())
            if chunks[-1] and length + size > _MAX_QUERY_CHARS:
                chunks.append([])
                length = 0
            chunks[-1].append(v)
            length += size
        workers = min(8, (os.cpu_count() or 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self._query_chunk, [c for c in chunks if c]))

    def _query_chunk(self, chunk: list[dict]) -> None:
        parts = []
        for v in chunk:
            for q in v["_queries"].values():
                parts += [q, f'"{_SEP}"']
        lines, _ = self.tk.query_lines(self.files, "(" + ", ".join(parts) + ")")
        if not lines:
            # One unresolvable attribute fails the whole query; narrow down.
            if len(chunk) > 1:
                for v in chunk:
                    self._query_chunk([v])
            else:
                v = chunk[0]
                v["attributes"] = {n: self._query_single(v, n) for n in v["attributes"]}
            return
        groups: list[list[str]] = [[]]
        for line in lines:
            if line == f'"{_SEP}"':
                groups.append([])
            else:
                groups[-1].append(line)
        it = iter(groups)
        for v in chunk:
            v["attributes"] = {n: _to_value(next(it, [])) for n in v["attributes"]}

    def _query_single(self, variant: dict, name: str) -> Optional[dict]:
        lines, err = self.tk.query_lines(self.files, variant["_queries"][name])
        return _to_value(lines) if lines else None

    # ------------------------------------------------------------ rules

    def _owner_block(self, owner: dict) -> Optional[tuple[SourceFile, tuple[int, int]]]:
        qn = self.graph.qualified_name(owner)
        if qn is None:
            return None
        if qn in self._block_cache:
            return self._block_cache[qn]
        result = None
        loc = self.tk.describe_location(self.files, self.graph.spelling(owner))
        if loc:
            src = self.sources.get(Path(loc[0]).resolve())
            if src is not None:
                block = src.find_block(src.offset(loc[1], loc[2]))
                if block is not None:
                    result = (src, block)
        self._block_cache[qn] = result
        return result

    def _constraints_of(self, owner: dict) -> list[dict]:
        """Constraint members of `owner`, paired with their source text."""
        g = self.graph
        members = []
        for rel, m in g.owned_members(owner):
            if m["@type"] in ("AssertConstraintUsage", "ConstraintUsage"):
                members.append((rel, m))
        if not members:
            return []
        located = self._owner_block(owner)
        spans = located[0].constraint_blocks(located[1]) if located else []
        if len(spans) != len(members):
            by_name = {s.name: s for s in spans if s.name}
            paired = [(rel, m, by_name.get(g.name(m) or "")) for rel, m in members]
        else:
            paired = [(rel, m, s) for (rel, m), s in zip(members, spans)]

        out = []
        for rel, m, span in paired:
            entry = {
                "name": g.name(m),
                "qualifiedName": g.qualified_name(m),
                "metaclass": m["@type"],
                "owner": g.qualified_name(owner),
                "membership": rel["@type"],
                "kind": rel.get("kind"),
                "negated": bool(m.get("isNegated")),
                "doc": g.documentation(m),
                "expression": None, "file": None, "line": None, "col": None,
            }
            if span is not None:
                line, col = located[0].line_col(span.body_start)
                entry.update(expression=span.body, file=str(located[0].path), line=line, col=col)
            else:
                self.warnings.append(f"No source text found for a constraint in {g.qualified_name(owner)}")
            out.append(entry)
        return out

    def _rules(self, owners: list[dict]) -> list[dict]:
        rules = []
        for owner in owners:
            for c in self._constraints_of(owner):
                if c["metaclass"] == "AssertConstraintUsage":
                    rules.append(dict(c, scope="system", context=None, appliesTo=None))
        return rules

    def _component_rules(self, system_types: list[dict], variation_points: list[dict]) -> list[dict]:
        """`assert constraint`s of the parts' definitions (e.g. `threads >= kerne` in `CPU`).

        `verify` only evaluates such a constraint where it is written, i.e. with the
        part still unbound. The tool re-asserts it in the configuration with every
        feature chain prefixed by the part (`cpu.threads >= cpu.kerne`). Constraints
        of a variant's own type only apply while that variant is selected.
        """
        g = self.graph
        top = {vp["feature"]: vp for vp in variation_points if vp["parent"] is None}
        rules = []
        for part in self._parts(system_types):
            name = g.name(part)
            common = g.usage_type_closure(part)
            common_ids = {d["@id"] for d in common}
            holders: list[tuple[dict, list[dict], Optional[list[str]]]] = [(d, common, None) for d in common]
            vp = top.get(name)
            specific: dict[str, tuple[dict, list[dict], list[str]]] = {}
            for variant in (vp["variants"] if vp else []):
                usage = self._element(variant["qualifiedName"])
                if usage is None:
                    continue
                closure = g.usage_type_closure(usage)
                for d in [x for x in closure if x["@id"] not in common_ids] + [usage]:
                    specific.setdefault(d["@id"], (d, closure, []))[2].append(variant["name"])
            holders += list(specific.values())
            for holder, context, variants in holders:
                for c in self._constraints_of(holder):
                    if c["metaclass"] != "AssertConstraintUsage" or not c["expression"]:
                        continue
                    expression = rewrite_chains(c["expression"], lambda p: self._rebase([name], p, context))
                    rules.append(dict(c, scope="component", context=name, sourceExpression=c["expression"],
                                      expression=expression,
                                      appliesTo={vp["name"]: variants} if variants is not None and vp else None))
        return rules

    def _interface_rules(self, owners: list[dict]) -> list[dict]:
        """`assert constraint`s of the interfaces and connections used in the system.

        The constraint is written over the interface's ends (`laufwerk.schnittstelle
        == anschluss.schnittstelle`); re-asserted in the configuration the ends are
        replaced by the connected features, and port values are followed through
        `bind` to the part's attributes (`speicher.schnittstelle`), because
        `verify` does not evaluate bindings.
        """
        g = self.graph
        context = [o for o in owners if g.is_definition(o)]
        rules = []
        for owner in owners:
            for _, conn in g.owned_members(owner):
                if conn["@type"] not in ("InterfaceUsage", "ConnectionUsage"):
                    continue
                ends = {n: [g.name(f) for f in g.chain(t)] for n, t in g.connector_ends(conn) if n and t is not None}
                if not ends:
                    continue
                label = " ↔ ".join(".".join(p) for p in ends.values())
                for d in [x for t in g.types(conn) for x in g.definition_closure(t)]:
                    for c in self._constraints_of(d):
                        if c["metaclass"] != "AssertConstraintUsage" or not c["expression"]:
                            continue

                        def rebase(path: list[str]) -> Optional[str]:
                            if path[0] not in ends:
                                return None
                            return self._spell(self._resolve(ends[path[0]] + path[1:], context))

                        rules.append(dict(c, scope="interface", context=label, sourceExpression=c["expression"],
                                          expression=rewrite_chains(c["expression"], rebase), appliesTo=None))
        return rules

    # -------------------------------------------- feature paths and bindings

    def _element(self, qualified_name: str) -> Optional[dict]:
        if self._by_qn is None:
            self._by_qn = {}
            for e in self.graph.elements():
                qn = self.graph.qualified_name(e)
                if qn and qn not in self._by_qn:
                    self._by_qn[qn] = e
        return self._by_qn.get(qualified_name)

    def _parts(self, types: list[dict]) -> list[dict]:
        """Part usages of the system (most specific declaration of each name)."""
        found: dict[str, dict] = {}
        for d in types:
            for _, m in self.graph.owned_members(d):
                if m["@type"] in ("PartUsage", "ItemUsage") and (n := self.graph.name(m)):
                    found[n] = m
        return list(found.values())

    def _member(self, context: list[dict], name: str) -> Optional[dict]:
        for d in reversed(context):
            for _, m in self.graph.owned_members(d):
                if self.graph.name(m) == name and self.graph.is_usage(m):
                    return m
        return None

    def _binds(self, context: list[dict]) -> list[tuple]:
        """`bind a.b = c;` in the given definitions, as pairs of name paths or literal spellings."""
        g = self.graph
        out = []
        for d in context:
            for _, b in g.owned_members(d):
                if b["@type"] != "BindingConnectorAsUsage":
                    continue
                sides = []
                for _, target in g.connector_ends(b):
                    if target is None:
                        break
                    chain = g.chain(target)
                    owner = g.owner(chain[0]) if len(chain) == 1 else None
                    if owner is not None and owner["@type"] == "EnumerationDefinition":
                        sides.append(g.spelling(chain[0]))  # an enum literal
                    else:
                        sides.append([g.name(f) for f in chain])
                if len(sides) == 2:
                    out.append(tuple(sides))
        return out

    def _resolve(self, path: list[str], context: list[dict], depth: int = 0):
        """Follow `bind`s from a port feature to what it is bound to: a name path, or a literal spelling."""
        if depth > 10 or not path:
            return path
        for a, b in self._binds(context):
            for x, y in ((a, b), (b, a)):
                if not isinstance(x, list) or path[:len(x)] != x:
                    continue
                head = self._member(context, x[0])
                if head is None or head["@type"] != "PortUsage":
                    continue
                if isinstance(y, str):
                    return y if len(path) == len(x) else path
                return self._resolve(y + path[len(x):], context, depth + 1)
        if len(path) > 1 and (child := self._member(context, path[0])) is not None:
            sub = self._resolve(path[1:], self.graph.usage_type_closure(child), depth + 1)
            return sub if isinstance(sub, str) else [path[0]] + sub
        return path

    def _rebase(self, prefix: list[str], path: list[str], context: list[dict]) -> Optional[str]:
        """`threads` written in `CPU` -> `cpu.threads`; None if `path` is no feature of the context."""
        if self._member(context, path[0]) is None:
            return None
        resolved = self._resolve(path, context)
        return resolved if isinstance(resolved, str) else self._spell(prefix + resolved)

    @staticmethod
    def _spell(path) -> str:
        return path if isinstance(path, str) else ".".join(spell_name(p) for p in path)

    # ------------------------------------------------------ key figures

    def _key_figures(self, owners: list[dict]) -> list[dict]:
        g = self.graph
        out, seen = [], set()
        for owner in owners:
            for _, m in g.owned_members(owner):
                if m["@type"] != "AttributeUsage" or g.feature_value_expression(m) is None:
                    continue
                name = g.name(m)
                if not name or name in seen:
                    continue
                seen.add(name)
                out.append({"name": name, "qualifiedName": g.qualified_name(m), "owner": g.qualified_name(owner),
                            "expression": self._value_text(m), "doc": g.documentation(m)})
        return out

    def _value_text(self, attribute: dict) -> Optional[str]:
        loc = self.tk.describe_location(self.files, self.graph.spelling(attribute))
        if not loc:
            return None
        src = self.sources.get(Path(loc[0]).resolve())
        if src is None:
            return None
        i = src.offset(loc[1], loc[2])
        text = src.text
        eq = None
        while i < len(text):
            if src.is_code(i) and text[i] in ";{":
                break
            if src.is_code(i) and text[i] == "=" and text[i - 1] not in "=!<>:" and text[i + 1] != "=":
                eq = i
                break
            i += 1
        if eq is None:
            return None
        end = eq + 1
        while end < len(text) and not (src.is_code(end) and text[end] in ";{"):
            end += 1
        return " ".join(text[eq + 1:end].split())

    # ------------------------------------------------------ requirements

    def _subject_types(self, req: dict) -> Optional[list[dict]]:
        """Types of the requirement's subject; [] if untyped; None if it has no subject."""
        g = self.graph
        chain = [req] + (g.definition_closure(g.types(req)[0]) if g.is_usage(req) and g.types(req) else [])
        if g.is_definition(req):
            chain = list(reversed(g.definition_closure(req)))
        for holder in chain:
            for _, m in g.owned_members(holder, ("SubjectMembership",)):
                return g.types(m)
        return None

    def _requirements(self, system_types: list[dict]) -> list[dict]:
        g = self.graph
        system_ids = {t["@id"] for t in system_types}

        def applicable(req: dict) -> bool:
            types = self._subject_types(req)
            return types is not None and (not types or any(t["@id"] in system_ids for t in types))

        usages = [r for r in g.elements("RequirementUsage")
                  if applicable(r) and (o := g.owner(r)) is not None and o["@type"] == "Package"]
        used_defs = {t["@id"] for u in usages for t in g.types(u)}
        defs = [d for d in g.elements("RequirementDefinition") if applicable(d) and d["@id"] not in used_defs]

        out = []
        for req in usages + defs:
            types = g.types(req) if g.is_usage(req) else []
            holders = ([req] + (g.definition_closure(types[0])[::-1] if types else [])) if g.is_usage(req) \
                else g.definition_closure(req)[::-1]
            constraints = []
            for h in holders:
                for c in self._constraints_of(h):
                    if c["membership"] == "RequirementConstraintMembership":
                        constraints.append(c)
            doc = g.documentation(req) or next((g.documentation(t) for t in types if g.documentation(t)), None)
            out.append({
                "name": g.name(req),
                "qualifiedName": g.qualified_name(req),
                "reference": g.spelling(req),
                "isDefinition": g.is_definition(req),
                "definition": g.qualified_name(types[0]) if types else None,
                "doc": doc,
                "constraints": constraints,
            })
        return out

    # ---------------------------------------------------- configurations

    def _configurations(self, owner: dict, variation_points: list[dict]) -> list[dict]:
        g = self.graph

        def children(parent: Optional[dict]) -> list[dict]:
            return [vp for vp in variation_points if vp["parent"] == parent]

        def collect(container: dict, candidates: list[dict], selection: dict) -> None:
            for _, m in g.owned_members(container):
                redefined = {g.qualified_name(r) for r in g.redefined(m)}
                vp = next((c for c in candidates if c["qualifiedName"] in redefined), None)
                if vp is None and candidates and candidates[0]["parent"] is not None:
                    # inside `part :>> gehaeuse :> ...::midiTower { attribute :>> farbe ... }` the
                    # redefinition may resolve to the general feature; the name decides
                    vp = next((c for c in candidates if c["feature"] == g.name(m)), None)
                if vp is None:
                    continue
                # `part :>> cpu = productLine::cpu::ryzen5;` or `part :>> cpu :> productLine::cpu::ryzen5;`
                target = g.referenced_element(g.feature_value_expression(m))
                if target is None:
                    target = next(iter(g.subsetted(m)), None)
                if target is not None:
                    selection[vp["name"]] = g.name(target)
                    collect(m, children({"variationPoint": vp["name"], "variant": g.name(target)}), selection)

        out = []
        for u in g.elements():
            if not g.is_usage(u) or u["@id"] == owner["@id"]:
                continue
            specializes = any(f["@id"] == owner["@id"] for f in g.subsetted(u)) or \
                (g.is_definition(owner) and any(t["@id"] == owner["@id"] for t in g.declared_types(u)))
            if not specializes:
                continue
            selection: dict = {}
            collect(u, children(None), selection)
            out.append({"name": g.name(u), "qualifiedName": g.qualified_name(u),
                        "doc": g.documentation(u), "selection": selection})
        return out

    # ------------------------------------------------------------ misc

    def _imports(self) -> list[str]:
        imports: list[str] = []
        for src in self.sources.values():
            for target in src.imports():
                if target not in imports:
                    imports.append(target)
        for p in self.graph.elements("Package"):
            owner = self.graph.owner(p)
            if owner is not None and owner["@type"] == "Namespace":
                target = f"{spell_name(self.graph.name(p))}::*"
                if target not in imports:
                    imports.append(target)
        return imports

    @staticmethod
    def _summarize(verdicts) -> dict:
        counts = {"satisfied": 0, "violated": 0, "undecided": 0}
        for v in verdicts:
            counts[v.verdict] += 1
        claims = [v.to_dict() for v in verdicts if v.metaclass == "SatisfyRequirementUsage"]
        violated = [v.to_dict() for v in verdicts if v.verdict == "violated" and v.metaclass != "SatisfyRequirementUsage"]
        return {"counts": counts, "satisfyClaims": claims, "violatedConstraints": violated}


def _to_value(lines: list[str]) -> Optional[dict]:
    if not lines:
        return None
    values = [parse_value(line) for line in lines]
    return make_list(values)


def _combinations(variation_points: list[dict]) -> int:
    """Number of complete selections; a variant with nested variation points counts once per nested choice."""
    by_key = {vp["name"]: vp for vp in variation_points}

    def count(vp: dict) -> int:
        return max(1, sum(_product(count(by_key[k]) for k in v["nested"]) for v in vp["variants"]))

    return _product(count(vp) for vp in variation_points if vp["parent"] is None)


def _product(numbers: Iterable[int]) -> int:
    result = 1
    for n in numbers:
        result *= max(n, 1)
    return result


def extract(paths: Iterable[str | Path], toolkit: Optional[Toolkit] = None) -> dict:
    tk = toolkit or Toolkit()
    files = collect_model_files(paths)
    try:
        return Extractor(tk, files).run()
    except ToolkitError as err:
        raise ExtractionError(str(err)) from err
