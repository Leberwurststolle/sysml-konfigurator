"""Navigation over the KerML/SysML compact interchange JSON emitted by `sysmlv2 convert`.

The compact form is a flat list of elements that reference each other by
`{"@id": ...}`. Standard-library elements are referenced by their normative ids
but are not part of the list; lookups of those return None.
"""
from __future__ import annotations

import re
from typing import Iterator, Optional

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ref_id(ref) -> Optional[str]:
    if isinstance(ref, dict):
        return ref.get("@id")
    return None


def spell_name(name: str) -> str:
    """Spell a name for use in SysML text (quote it if it is not a basic identifier)."""
    if _IDENT.match(name):
        return name
    return "'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'"


def spell_qualified(segments: list[str]) -> str:
    return "::".join(spell_name(s) for s in segments)


class ModelGraph:
    def __init__(self, elements: list[dict]):
        self.by_id: dict[str, dict] = {e["@id"]: e for e in elements}
        self._name_cache: dict[str, Optional[str]] = {}

    # -- basic access -------------------------------------------------------

    def get(self, ref) -> Optional[dict]:
        if isinstance(ref, str):
            return self.by_id.get(ref)
        rid = ref_id(ref)
        return self.by_id.get(rid) if rid else None

    def elements(self, *types: str) -> Iterator[dict]:
        for e in self.by_id.values():
            if not types or e["@type"] in types:
                yield e

    def owned_relationships(self, e: dict) -> Iterator[dict]:
        for ref in e.get("ownedRelationship") or []:
            rel = self.get(ref)
            if rel is not None:
                yield rel

    def owned_members(self, e: dict, membership_types: Optional[tuple] = None) -> Iterator[tuple[dict, dict]]:
        """Yield (membership, member) for every element owned through a membership."""
        for rel in self.owned_relationships(e):
            if not rel["@type"].endswith("Membership"):
                continue
            if membership_types and rel["@type"] not in membership_types:
                continue
            for ref in rel.get("ownedRelatedElement") or []:
                member = self.get(ref)
                if member is not None:
                    yield rel, member

    def owner(self, e: dict) -> Optional[dict]:
        rel = self.get(e.get("owningRelationship"))
        if rel is None:
            return None
        return self.get(rel.get("owningRelatedElement"))

    def owned_rels_of_type(self, e: dict, *types: str) -> Iterator[dict]:
        for rel in self.owned_relationships(e):
            if rel["@type"] in types:
                yield rel

    # -- names ----------------------------------------------------------------

    def name(self, e: Optional[dict]) -> Optional[str]:
        """Declared name, or the name inherited through a redefinition (`:>> cpu`)."""
        if e is None:
            return None
        eid = e["@id"]
        if eid in self._name_cache:
            return self._name_cache[eid]
        self._name_cache[eid] = None  # cycle guard
        name = e.get("declaredName")
        if not name:
            for rel in self.owned_rels_of_type(e, "Redefinition"):
                name = self.name(self.get(rel.get("redefinedFeature")))
                if name:
                    break
        self._name_cache[eid] = name
        return name

    def qualified_segments(self, e: dict) -> Optional[list[str]]:
        segments = []
        cur: Optional[dict] = e
        while cur is not None and cur["@type"] != "Namespace":
            n = self.name(cur)
            if not n:
                return None
            segments.append(n)
            cur = self.owner(cur)
        return list(reversed(segments))

    def qualified_name(self, e: Optional[dict]) -> Optional[str]:
        if e is None:
            return None
        segs = self.qualified_segments(e)
        return "::".join(segs) if segs else None

    def spelling(self, e: dict) -> Optional[str]:
        segs = self.qualified_segments(e)
        return spell_qualified(segs) if segs else None

    # -- typing and specialization ----------------------------------------------

    def declared_types(self, e: dict) -> list[dict]:
        out = []
        for rel in self.owned_rels_of_type(e, "FeatureTyping", "ConjugatedPortTyping"):
            t = self.get(rel.get("type"))
            if t is not None:
                out.append(t)
        return out

    def redefined(self, e: dict) -> list[dict]:
        return [f for rel in self.owned_rels_of_type(e, "Redefinition")
                if (f := self.get(rel.get("redefinedFeature"))) is not None]

    def subsetted(self, e: dict) -> list[dict]:
        out = []
        for rel in self.owned_rels_of_type(e, "Subsetting", "ReferenceSubsetting"):
            f = self.get(rel.get("subsettedFeature") or rel.get("referencedFeature"))
            if f is not None:
                out.append(f)
        return out

    def types(self, e: dict, _seen: Optional[set] = None) -> list[dict]:
        """Declared types, else types inherited from redefined/subsetted features."""
        seen = _seen if _seen is not None else set()
        if e["@id"] in seen:
            return []
        seen.add(e["@id"])
        direct = self.declared_types(e)
        if direct:
            return direct
        for f in self.redefined(e) + self.subsetted(e):
            inherited = self.types(f, seen)
            if inherited:
                return inherited
        return []

    def superclassifiers(self, d: dict) -> list[dict]:
        return [s for rel in self.owned_rels_of_type(d, "Subclassification")
                if (s := self.get(rel.get("superclassifier"))) is not None]

    def definition_closure(self, d: dict) -> list[dict]:
        """The definition and all in-model supertypes, most general first."""
        order: list[dict] = []
        seen: set = set()

        def visit(x: dict) -> None:
            if x["@id"] in seen:
                return
            seen.add(x["@id"])
            for s in self.superclassifiers(x):
                visit(s)
            order.append(x)

        visit(d)
        return order

    def conforms(self, d: dict, ancestor: dict) -> bool:
        return any(x["@id"] == ancestor["@id"] for x in self.definition_closure(d))

    def usage_type_closure(self, u: dict) -> list[dict]:
        """For a usage: the definitions (with supertypes) that type it, plus the usages it specializes."""
        out: list[dict] = []
        seen: set = set()
        for t in self.types(u):
            for x in self.definition_closure(t):
                if x["@id"] not in seen:
                    seen.add(x["@id"])
                    out.append(x)
        return out

    # -- content ----------------------------------------------------------------

    def documentation(self, e: dict) -> Optional[str]:
        bodies = []
        for _, m in self.owned_members(e):
            if m["@type"] == "Documentation" and m.get("body"):
                bodies.append(" ".join(m["body"].split()))
        return "\n".join(bodies) if bodies else None

    def feature_value_expression(self, e: dict) -> Optional[dict]:
        for rel in self.owned_rels_of_type(e, "FeatureValue"):
            for ref in rel.get("ownedRelatedElement") or []:
                expr = self.get(ref)
                if expr is not None:
                    return expr
        return None

    def referenced_element(self, expr: Optional[dict]) -> Optional[dict]:
        """Target of a FeatureReferenceExpression (`= pcProduktlinie::cpu::ryzen5`)."""
        if expr is None or expr["@type"] != "FeatureReferenceExpression":
            return None
        for rel in self.owned_relationships(expr):
            if rel["@type"] == "Membership":
                target = self.get(rel.get("memberElement"))
                if target is not None:
                    return target
            for ref in rel.get("ownedRelatedElement") or []:
                inner = self.get(ref)
                if inner is not None:
                    # A qualified chain is represented as a Feature with FeatureChainings.
                    chain = [self.get(c.get("chainingFeature")) for c in self.owned_rels_of_type(inner, "FeatureChaining")]
                    if chain and chain[-1] is not None:
                        return chain[-1]
        return None

    def chain(self, f: dict) -> list[dict]:
        """The features of a chain `a.b.c` (a Feature with FeatureChainings), or [f] for a plain feature."""
        links = [self.get(c.get("chainingFeature")) for c in self.owned_rels_of_type(f, "FeatureChaining")]
        return [x for x in links if x is not None] if links else [f]

    def connector_ends(self, connector: dict) -> list[tuple[Optional[str], Optional[dict]]]:
        """(end name, connected feature) for each end of a connector, interface or binding."""
        out = []
        for _, end in self.owned_members(connector, ("EndFeatureMembership",)):
            target = None
            for rel in self.owned_rels_of_type(end, "ReferenceSubsetting"):
                target = self.get(rel.get("referencedFeature"))
            out.append((self.name(end), target))
        return out

    @staticmethod
    def is_usage(e: dict) -> bool:
        return e["@type"].endswith("Usage")

    @staticmethod
    def is_definition(e: dict) -> bool:
        return e["@type"].endswith("Definition")


# Keyword used to write a usage of a given metaclass in textual SysML.
USAGE_KEYWORDS = {
    "PartUsage": "part", "ItemUsage": "item", "AttributeUsage": "attribute", "PortUsage": "port",
    "ActionUsage": "action", "StateUsage": "state", "ConnectionUsage": "connection",
    "InterfaceUsage": "interface", "AllocationUsage": "allocation", "OccurrenceUsage": "occurrence",
    "ReferenceUsage": "ref", "CalculationUsage": "calc", "ConstraintUsage": "constraint",
    "RequirementUsage": "requirement", "ViewUsage": "view", "UseCaseUsage": "use case",
    "AnalysisCaseUsage": "analysis", "VerificationCaseUsage": "verification",
    "EnumerationUsage": "enum", "FlowUsage": "flow", "MetadataUsage": "metadata",
}


def usage_keyword(metaclass: str) -> str:
    return USAGE_KEYWORDS.get(metaclass, "ref")
