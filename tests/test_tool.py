"""Tests for the extractor and the configuration checker.

Run from the project folder:
    python -m unittest discover -s tests -v

The integration tests use the PC model (default: ../SysML2 erste Versuche,
override with PC_MODEL_DIR) and the installed toolkit; they are skipped when
either is missing.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from sysml_tool.checker import ConfigurationChecker  # noqa: E402
from sysml_tool.explorer import DesignSpace, explore, selection_key  # noqa: E402
from sysml_tool.extractor import extract  # noqa: E402
from sysml_tool.source import SourceFile, rewrite_chains  # noqa: E402
from sysml_tool.toolkit import Toolkit, ToolkitError, parse_value, parse_verify_output  # noqa: E402

MODEL_DIR = Path(os.environ.get("PC_MODEL_DIR", PROJECT.parent / "SysML2 erste Versuche"))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ParserTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(parse_value("88 [W]")["value"], 88)
        self.assertEqual(parse_value("88 [W]")["unit"], "W")
        self.assertEqual(parse_value("3.8 [GHz]")["display"], "3.8 GHz")
        self.assertEqual(parse_value('"be quiet!"')["value"], "be quiet!")
        self.assertIs(parse_value("true")["value"], True)
        self.assertAlmostEqual(parse_value("1/3")["value"], 1 / 3)
        enum = parse_value("PcParts::CpuSockel::AM5 (EnumerationUsage)")
        self.assertEqual((enum["kind"], enum["display"]), ("element", "AM5"))
        self.assertEqual(parse_value("<indeterminate>")["value"], None)
        seq = parse_value("(1 [kg], 2 [kg])")
        self.assertEqual([i["value"] for i in seq["items"]], [1, 2])

    def test_verify_output_with_windows_paths(self):
        text = (
            r"C:\Users\x\M.sysml:12:5  <anonymous> (SatisfyRequirementUsage, satisfies P::req by cfg_0): VIOLATED" "\n"
            r"  C:\Users\x\P.sysml:189:30  <anonymous> (ConstraintUsage): VIOLATED (with pc.cpu.kerne = 6, pc.x = (1, 2))" "\n"
            r"C:\Users\x\M.sysml:20:34  kpi1 (AssertConstraintUsage): VIOLATED (with gesamtPreis = 1745)" "\n"
            "0 satisfied, 2 violated, 0 undecided\n"
        )
        claim, kpi = parse_verify_output(text)
        self.assertEqual(claim.file, r"C:\Users\x\M.sysml")
        self.assertEqual((claim.requirement, claim.satisfied_by, claim.verdict), ("P::req", "cfg_0", "violated"))
        self.assertEqual(claim.children[0].line, 189)
        self.assertEqual(claim.children[0].bindings["pc.cpu.kerne"]["value"], 6)
        self.assertEqual(len(claim.children[0].bindings["pc.x"]["items"]), 2)
        self.assertEqual(kpi.bindings["gesamtPreis"]["value"], 1745)

    def test_constraint_blocks_skip_comments_and_nested_defs(self):
        text = (
            "part def S {\n"
            "    // constraint { not this }\n"
            "    assert constraint a { x > 1 /* } */ }\n"
            "    part def Inner { assert constraint hidden { y } }\n"
            "    require constraint { \"}\" == z }\n"
            "    constraint def NotAUsage { true }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.sysml"
            path.write_text(text, encoding="utf-8")
            src = SourceFile(path)
            spans = src.constraint_blocks(src.find_block(0))
        self.assertEqual([(s.name, s.body) for s in spans], [("a", "x > 1"), (None, '"}" == z')])

    def test_rewrite_chains_only_touches_leading_feature_chains(self):
        features = {"threads", "kerne", "sockel"}
        rewrite = lambda p: "cpu." + ".".join(p) if p[0] in features else None  # noqa: E731
        self.assertEqual(rewrite_chains("threads >= kerne", rewrite), "cpu.threads >= cpu.kerne")
        self.assertEqual(rewrite_chains("other.kerne > 1 and sockel == CpuSockel::AM5", rewrite),
                         "other.kerne > 1 and cpu.sockel == CpuSockel::AM5")
        self.assertEqual(rewrite_chains('includes(liste, sockel) or "kerne" == name', rewrite),
                         'includes(liste, cpu.sockel) or "kerne" == name')


def _toolkit():
    try:
        return Toolkit()
    except ToolkitError:
        return None


@unittest.skipUnless(_toolkit() and MODEL_DIR.is_dir(), "toolkit or PC model not available")
class PcModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tk = Toolkit()
        cls.ds = extract([MODEL_DIR], cls.tk)
        cls.pl = cls.ds["productLines"][0]
        cls.checker = ConfigurationChecker(cls.tk, cls.ds)

    def variant(self, vp_name, variant_name):
        vp = next(v for v in self.pl["variationPoints"] if v["name"] == vp_name)
        return next(v for v in vp["variants"] if v["name"] == variant_name)

    def test_design_space(self):
        self.assertEqual(self.pl["qualifiedName"], "PcModell::pcProduktlinie")
        self.assertEqual([vp["name"] for vp in self.pl["variationPoints"]],
                         ["cpu", "mainboard", "ram", "speicher", "grafik", "netzteil", "kuehler", "gehaeuse"])
        self.assertEqual(self.pl["combinations"], 19440)
        self.assertEqual([r["scope"] for r in self.pl["rules"]].count("system"), 11)
        self.assertEqual(self.pl["rules"][0]["expression"], "cpu.sockel == mainboard.sockel")

    def test_component_and_interface_rules_are_rebased_onto_the_system(self):
        rules = {(r["scope"], r["context"]): r["expression"] for r in self.pl["rules"] if r["scope"] != "system"}
        self.assertEqual(rules[("component", "cpu")], "cpu.boostTakt >= cpu.basisTakt")
        # port values are followed through `bind` to the parts' attributes
        self.assertEqual(rules[("interface", "speicher.anschluss ↔ mainboard.m2")],
                         "speicher.schnittstelle == PcParts::SpeicherSchnittstelle::NVMe")
        self.assertEqual(rules[("interface", "ram.ramAnschluss ↔ mainboard.ramBank")], "ram.ramTyp == mainboard.ramTyp")
        self.assertEqual([k["name"] for k in self.pl["keyFigures"]], ["gesamtLeistung", "gesamtPreis", "netzteilReserve"])
        self.assertEqual([q["name"] for q in self.pl["requirements"]],
                         ["bueroAnforderungen", "gamingAnforderungen", "workstationAnforderungen"])

    def test_variant_values_are_evaluated_with_inheritance_and_units(self):
        ryzen = self.variant("cpu", "ryzen5")["attributes"]
        self.assertEqual(ryzen["sockel"]["display"], "AM5")
        self.assertEqual((ryzen["leistungsaufnahme"]["value"], ryzen["leistungsaufnahme"]["unit"]), (88, "W"))
        self.assertEqual(ryzen["hersteller"]["value"], "AMD")  # inherited from Komponente
        ram = self.variant("ram", "ram32")["attributes"]
        self.assertEqual(ram["gesamtKapazitaet"]["display"], "32 GB")  # computed in Arbeitsspeicher
        tower = self.variant("kuehler", "tower")["attributes"]
        self.assertEqual([i["display"] for i in tower["unterstuetzteSockel"]["items"]], ["AM5", "LGA1700", "LGA1851"])

    def test_modelled_configurations(self):
        configs = {c["name"]: c["selection"] for c in self.pl["configurations"]}
        self.assertEqual(configs["gamingPC"]["grafik"], "rtx5080")
        self.assertEqual(len(configs["workstation"]), 8)

    def test_modelled_configurations_are_valid_with_documented_figures(self):
        expected = {"bueroPC": (127, 606), "gamingPC": (577, 2602), "workstation": (537, 4892)}
        for cfg in self.pl["configurations"]:
            r = self.checker.check(0, cfg["selection"])
            self.assertEqual(r["status"], "valid", cfg["name"])
            figures = (r["keyFigures"]["gesamtLeistung"]["value"], r["keyFigures"]["gesamtPreis"]["value"])
            self.assertEqual(figures, expected[cfg["name"]], cfg["name"])

    def test_invalid_configuration_names_the_broken_rules(self):
        selection = dict(self.pl["configurations"][0]["selection"], mainboard="b760m", grafik="rtx5080")
        r = self.checker.check(0, selection)
        self.assertEqual(r["status"], "invalid")
        violated = {x["label"] for x in r["rules"] if x["verdict"] == "violated"}
        self.assertEqual(violated, {"sockelPasst", "netzteilAusreichend", "SockelVerbindung: Regel 1"})
        self.assertEqual(r["keyFigures"]["gesamtLeistung"]["value"], 487)

    def test_requirements(self):
        gaming = next(c for c in self.pl["configurations"] if c["name"] == "gamingPC")["selection"]
        r = self.checker.check(0, gaming)
        verdicts = {q["name"]: q["verdict"] for q in r["requirements"]}
        self.assertEqual(verdicts, {"bueroAnforderungen": "violated", "gamingAnforderungen": "satisfied",
                                    "workstationAnforderungen": "violated"})
        buero = next(q for q in r["requirements"] if q["name"] == "bueroAnforderungen")
        price = next(c for c in buero["constraints"] if "gesamtPreis" in c["expression"])
        self.assertEqual((price["verdict"], price["bindings"]["pc.gesamtPreis"]["value"]), ("violated", 2602))

    def test_alternatives(self):
        buero = self.pl["configurations"][0]["selection"]
        alt = self.checker.alternatives(0, buero)
        self.assertEqual(alt["cpu"]["ryzen5"]["status"], "valid")
        self.assertIn("kuehlungAusreichend", alt["cpu"]["ryzen7X3D"]["violatedRules"])
        self.assertEqual(alt["grafik"]["rtx5080"]["violatedRules"], ["netzteilAusreichend"])

    def test_incomplete_selection_is_not_valid(self):
        r = self.checker.check(0, {"cpu": "ryzen5"})
        self.assertFalse(r["complete"])
        self.assertNotEqual(r["status"], "valid")


@unittest.skipUnless(_toolkit(), "toolkit not available")
class FixtureModelTests(unittest.TestCase):
    """tests/fixtures/Fahrrad.sysml: component and interface rules, nested variation."""

    @classmethod
    def setUpClass(cls):
        cls.tk = Toolkit()
        cls.ds = extract([FIXTURES], cls.tk)
        cls.pl = cls.ds["productLines"][0]
        cls.checker = ConfigurationChecker(cls.tk, cls.ds)

    def verdicts(self, selection):
        r = self.checker.check(0, selection)
        return r["status"], {x["label"]: x["verdict"] for x in r["rules"]}

    def test_nested_variation_belongs_to_its_variant(self):
        self.assertEqual(len(self.ds["productLines"]), 1)
        names = [vp["name"] for vp in self.pl["variationPoints"]]
        self.assertEqual(names, ["rahmen", "laufrad", "laufrad/aero/farbe"])
        nested = self.pl["variationPoints"][2]
        self.assertEqual(nested["parent"], {"variationPoint": "laufrad", "variant": "aero"})
        self.assertEqual(nested["variants"][1]["attributes"]["farbe"]["value"], "Weiß")
        self.assertEqual(self.pl["combinations"], 6)  # 2 Rahmen x (1 + 2 Laufrad-Varianten)

    def test_modelled_configuration_with_nested_selection(self):
        configs = {c["name"]: c["selection"] for c in self.pl["configurations"]}
        self.assertEqual(configs["rennrad"], {"rahmen": "carbon", "laufrad": "aero", "laufrad/aero/farbe": "weiss"})

    def test_nested_selection_is_required_while_active(self):
        r = self.checker.check(0, {"rahmen": "carbon", "laufrad": "aero"})
        self.assertFalse(r["complete"])
        self.assertTrue(self.checker.check(0, {"rahmen": "alu", "laufrad": "strasse"})["complete"])

    def test_component_rule_and_variant_specific_rule(self):
        status, v = self.verdicts({"rahmen": "alu", "laufrad": "strasse"})
        self.assertEqual(status, "valid")
        self.assertEqual(v["nurMitCarbon"], "notApplicable")  # defined in Aero only
        status, v = self.verdicts({"rahmen": "carbon", "laufrad": "aero", "laufrad/aero/farbe": "rot"})
        self.assertEqual((status, v["genugSpeichen"], v["nurMitCarbon"]), ("invalid", "violated", "satisfied"))

    def test_interface_rule_through_port_bindings(self):
        _, v = self.verdicts({"rahmen": "alu", "laufrad": "aero", "laufrad/aero/farbe": "rot"})
        self.assertEqual(v["AchsVerbindung: Regel 1"], "violated")

    def test_alternatives_fill_newly_active_nested_choices(self):
        alt = self.checker.alternatives(0, {"rahmen": "carbon", "laufrad": "strasse"})
        self.assertNotIn("laufrad/aero/farbe", alt)
        self.assertEqual(alt["laufrad"]["aero"]["violatedRules"], ["genugSpeichen"])

    def test_snippet_nests_the_selection(self):
        text = self.checker.snippet(0, {"rahmen": "carbon", "laufrad": "aero", "laufrad/aero/farbe": "weiss"}, "x")
        self.assertIn("part :>> laufrad :> Fahrrad::radLinie::laufrad::aero {\n"
                      "        attribute :>> farbe :> Fahrrad::radLinie::laufrad::aero::farbe::weiss;\n    }", text)

    def test_numbering_covers_each_complete_selection_once(self):
        space = DesignSpace(self.pl)
        selections = [space.selection_at(i) for i in range(space.total)]
        self.assertEqual(space.total, self.pl["combinations"])
        self.assertEqual(len({selection_key(s) for s in selections}), space.total)
        self.assertIn({"rahmen": "carbon", "laufrad": "aero", "laufrad/aero/farbe": "weiss"}, selections)
        self.assertTrue(all(self.checker.check(0, s, with_requirements=False)["complete"] for s in selections))

    def test_explore_all_given_and_neighbors(self):
        everything = explore(self.checker, 0, "all", 10)
        self.assertEqual((everything["total"], everything["nextOffset"], len(everything["points"])), (6, 6, 6))
        status = {p["key"]: p["status"] for p in everything["points"]}
        self.assertEqual(status["laufrad.strasse,rahmen.alu"], "valid")
        self.assertEqual(status["laufrad.aero,laufrad/aero/farbe.rot,rahmen.carbon"], "invalid")
        given = explore(self.checker, 0, "given", 5, selections=[{"rahmen": "alu", "laufrad": "strasse"}],
                        known=[])
        self.assertEqual([p["violated"] for p in given["points"]], [[]])
        known = ["laufrad.strasse,rahmen.alu"]
        near = explore(self.checker, 0, "neighbors", 3, seed=1, around=[{"rahmen": "alu", "laufrad": "strasse"}], known=known)
        self.assertTrue(near["points"])
        self.assertNotIn(known[0], [p["key"] for p in near["points"]])


if __name__ == "__main__":
    unittest.main()
