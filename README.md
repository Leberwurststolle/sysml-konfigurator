# SysML v2 Extractor & Konfigurator

Liest ein SysML-v2-Produktlinienmodell (150-%-Modell mit `variation`/`variant`), schreibt den
Lösungsraum als JSON und bietet eine Weboberfläche, in der man eine Konfiguration
zusammenstellt und sofort sieht, ob sie zulässig ist.

Die gesamte SysML-Semantik (Parsen, Namensauflösung, Auswertung von Ausdrücken mit Einheiten,
Prüfung von Constraints und `satisfy`) übernimmt das
[Open-MBEE sysml-toolkit](https://github.com/Open-MBEE/sysml-toolkit). Das Python-Tool baut
keine eigene Auswertung nach. Es ruft die `sysmlv2`-CLI auf und bereitet deren Ergebnisse auf.

## Voraussetzungen

* Python ≥ 3.9 (nur Standardbibliothek, kein `pip install` nötig)
* Windows (x64), macOS (Apple Silicon oder Intel) oder Linux
* Einmalig Internet für den Download des Toolkits

## Installation

```bash
python setup_toolkit.py
```

Lädt `sysmlv2` 0.10.10 für das eigene Betriebssystem und die OMG-Standardbibliothek nach
`./toolkit/`. Beide Downloads werden per SHA256 geprüft. Unter Windows je nach Installation
`py` statt `python` verwenden.

Wenn GitHub-Downloads gesperrt sind: `python setup_toolkit.py --print-urls` zeigt die zwei
Dateien. Diese manuell herunterladen und mit `python setup_toolkit.py --from-dir <ordner>`
installieren. Alternativ geht ein vorhandenes `sysmlv2` per `--sysmlv2 <pfad>` /
`SYSMLV2_BIN` und eine Bibliothek per `--lib <pfad>` / `SYSMLV2_LIB_DIR`.

## Benutzung

**Extractor** (Modell → JSON):

```bash
python extract.py "../SysML2 erste Versuche" -o design_space.json
```

**Konfigurator** (Weboberfläche, öffnet den Browser auf http://127.0.0.1:8765):

```bash
python konfigurator.py "../SysML2 erste Versuche"
```

* **Konfigurieren:** pro Variationspunkt eine Variante wählen. Rechts stehen der Status
  (zulässig / nicht zulässig), die Kennwerte (z. B. Gesamtpreis), jede Regel mit den
  beteiligten Werten und jede Anforderung. Jede Variantenkarte zeigt, ob der Wechsel auf
  diese Variante die Konfiguration zulässig lässt und wie sich die Kennwerte ändern.
* **Lösungsraum:** geprüfte Konfigurationen als Punktwolke über drei frei wählbare Kennwerte,
  drehbar in 3D oder als 2D-Ansicht (X·Y, X·Z, Y·Z). Grün = zulässig, grau = nicht zulässig,
  blau = Pareto-optimal unter den zulässigen. Pro Achse lässt sich einstellen, ob größer oder
  kleiner besser ist (`↑ max` / `↓ min`); voreingestellt ist `min` nur für Kennwerte, deren Name
  auf „preis“/„kosten“ endet. Beim ersten Öffnen werden die Konfigurationen aus dem Modell, 300
  zufällige und 300 Nachbarn zulässiger Konfigurationen geprüft (einige Sekunden). Ein Klick auf
  einen Punkt zeigt die Konfiguration, „Im Konfigurator öffnen“ übernimmt sie. Die Tabelle
  darunter listet die Pareto-Front, „CSV aller Punkte“ exportiert die Stichprobe.
* **Modell:** alle Variationspunkte als sortierbare Tabellen, Regeln, Kennwerte,
  Anforderungen, im Modell vorhandene Konfigurationen und das `verify`-Ergebnis des Modells.
* **Als SysML übernehmen:** die gewählte Konfiguration als SysML-Text zum Einfügen ins Modell.
* **Modell neu laden:** nach Änderungen an den `.sysml`-Dateien neu einlesen.
* Die URL enthält die Auswahl und lässt sich als Lesezeichen speichern.

**Tests:**

```bash
python -m unittest discover -s tests -v
```

## Was das Tool im Modell erwartet

Das Tool kennt keine PC-spezifischen Namen. Es erkennt die Elemente an ihrer SysML-Struktur:

| Im Modell | Wird im Tool zu |
|---|---|
| Usage oder Definition mit `variation`-Elementen, z. B. `part pcProduktlinie : PC { variation part :>> cpu ... }` | Produktlinie |
| `variation part :>> cpu : CPU { variant part ryzen5 : Ryzen5_7600; ... }` | Variationspunkt mit Varianten |
| `variant part midiTower : MidiTower { variation attribute :>> farbe { variant attribute weiss = "Weiß"; ... } }` | abhängiger Variationspunkt `gehaeuse/midiTower/farbe`, nur wählbar, solange `midiTower` gewählt ist |
| Attribute der Variantentypen (auch geerbte und berechnete) | Variantenwerte, vom Toolkit ausgewertet |
| `assert constraint` in der Systemdefinition (`PC`), ihren Obertypen oder der Produktlinie | Regeln, die jede Konfiguration erfüllen muss |
| `assert constraint` in den Definitionen der Teile (`threads >= kerne` in `CPU`) | Bauteilregeln, geprüft als `cpu.threads >= cpu.kerne`; Regeln im Typ einer Variante gelten nur, solange sie gewählt ist |
| `assert constraint` in `interface def`/`connection def`, die das System verwendet | Schnittstellenregeln; Enden werden durch die verbundenen Features ersetzt, Port-Werte über `bind` verfolgt |
| Attribute mit Wert im System, z. B. `attribute gesamtPreis = ...` | Kennwerte |
| `requirement` / `requirement def` mit `subject` vom Systemtyp | Anforderungen, geprüft per `satisfy` |
| `part bueroPC :> pcProduktlinie { part :>> cpu = pcProduktlinie::cpu::ryzen5; ... }` | vorhandene Konfigurationen („Ausgangspunkt“) |

Eine verschachtelte Auswahl in einer Konfiguration braucht `:>` beim übergeordneten Teil, sonst
wertet `verify` sie nicht aus:

```sysml
part :>> gehaeuse :> pcProduktlinie::gehaeuse::midiTower {
    attribute :>> farbe :> pcProduktlinie::gehaeuse::midiTower::farbe::weiss;
}
```

Regeln sollten benannt sein (`assert constraint sockelPasst { ... }`), sonst erscheinen sie als
„Besitzer: Regel n“, z. B. „SockelVerbindung: Regel 1“.

## So funktioniert die Prüfung

Für jede zu prüfende Auswahl erzeugt das Tool eine temporäre SysML-Datei neben dem Modell:

```sysml
part cfg_0 :> PcModell::pcProduktlinie {
    part :>> cpu :> PcModell::pcProduktlinie::cpu::ryzen5;     // Auswahl
    part :>> gehaeuse :> PcModell::pcProduktlinie::gehaeuse::midiTower {
        attribute :>> farbe :> ...::midiTower::farbe::weiss;      // verschachtelte Auswahl
    }
    ...
    assert constraint rule_0 { cpu.sockel == mainboard.sockel }   // Regeln aus PC
    assert constraint rule_12 { cpu.threads >= cpu.kerne }        // Bauteilregel aus CPU
    assert constraint rule_14 { speicher.schnittstelle == PcParts::SpeicherSchnittstelle::NVMe }
                                                                  // Schnittstellenregel
    assert constraint figure_0 { gesamtPreis != gesamtPreis }     // Messsonde für Kennwerte
}
satisfy PcModell::gamingAnforderungen by cfg_0;                    // Anforderungen
```

`sysmlv2 verify` entscheidet dann jede Regel und jede Anforderung. Ein Lauf dauert etwa 0,1 s.
Alle Alternativen einer Auswahl werden gemeinsam in einem Lauf geprüft.

### Stichprobe im Lösungsraum

Jede vollständige Auswahl hat eine Nummer zwischen 0 und der Anzahl der Kombinationen (gemischte
Basis; eine Variante mit verschachtelten Variationspunkten belegt einen Nummernbereich pro
verschachtelter Wahl). Daraus ergeben sich drei Arten, Konfigurationen zu ziehen:

* **zufällig:** gleichverteilt über alle vollständigen Auswahlen. Nur diese Punkte gehen in den
  angezeigten Schätzwert für den zulässigen Anteil ein.
* **um zulässige:** ändert 1–2 Variationspunkte bereits zulässiger Konfigurationen (bevorzugt der
  Pareto-Front). Findet deutlich mehr zulässige Konfigurationen, ist aber keine gleichverteilte
  Stichprobe.
* **alle:** bis 50.000 Kombinationen kann man alles prüfen, seitenweise nach Nummer.

Geprüft wird jeder Punkt mit `verify`, in Stapeln von 50 Konfigurationen pro Lauf, zwei Läufe
parallel (etwa 70 Konfigurationen pro Sekunde). Die Pareto-Front wird im Browser aus den
geprüften Kennwerten berechnet.

Diese Umwege sind nötig, weil die Toolkit-Version 0.10.10 sie erfordert (Stand Oktober 2026):

1. **Regeln werden in die Konfiguration kopiert.** `verify` wertet ein `assert constraint`
   nur dort aus, wo es steht. In `PC` sind die Teile noch ungebunden, das Ergebnis ist also
   „undecided“. Die Ausdrücke werden darum aus dem Quelltext gelesen (Position über
   `sysmlv2 describe`) und in der Konfiguration erneut gesetzt.
2. **Bauteil- und Schnittstellenregeln werden umgeschrieben.** In die Konfiguration
   geschachtelt (`part :>> cpu :> ... { assert constraint { threads >= kerne } }`) bleiben sie
   „undecided“. Das Tool stellt darum jeder Featurekette den Teil voran (`cpu.threads`) bzw.
   ersetzt die Interface-Enden durch die verbundenen Features.
3. **`bind` wird vom Tool verfolgt.** `verify` wertet Bindungen nicht aus;
   `speicher.anschluss.schnittstelle` bliebe „undecided“. Werte an Ports werden darum über die
   `bind`-Anweisungen der Teildefinitionen auf deren Attribute (`speicher.schnittstelle`) oder
   Literale (`bind m2.schnittstelle = SpeicherSchnittstelle::NVMe`) zurückgeführt.
4. **Auswahl per `:>`.** Mit `part :>> gehaeuse = variante { ... }` wertet `verify` eine
   verschachtelte Auswahl nicht aus, mit `:>` schon. Die Prüfdatei nutzt daher durchgehend `:>`.
5. **Kennwerte über eine Messsonde.** `sysmlv2 eval` folgt einer gewählten Variante nicht,
   `verify` dagegen schon. `x != x` ist immer verletzt, und `verify` gibt dabei den Wert von `x`
   aus.

## Aufbau

```
setup_toolkit.py         Download von sysmlv2 + Standardbibliothek
extract.py               CLI: Modell -> design_space.json
konfigurator.py          CLI: startet die Weboberfläche
sysml_tool/
  toolkit.py             Aufruf der sysmlv2-CLI, Parser für deren Ausgaben
  model_graph.py         Navigation im KerML Compact-Interchange-JSON
  source.py              Quelltext-Helfer (Constraint-Rümpfe, Imports)
  extractor.py           Lösungsraum extrahieren
  checker.py             Konfigurationen als SysML erzeugen und mit verify prüfen
  explorer.py            Nummerierung des Lösungsraums, Stichproben, Stapelprüfung
  server.py              lokaler HTTP-Server
  web/                   Oberfläche (HTML/CSS/JS, keine externen Abhängigkeiten)
tests/test_tool.py       Unit- und Integrationstests (PC-Modell, Testmodell)
tests/fixtures/          kleines Testmodell: Bauteil-/Schnittstellenregeln, bind, verschachtelte Variation
```

## JSON-Format (`design_space.json`)

```
schema, generator{toolkit, library}, files[], packages[], diagnostics[], warnings[]
context.imports[]                          Imports für generierte Prüfdateien
modelVerification{counts, satisfyClaims[]} verify-Ergebnis des Modells, wie es ist
productLines[]:
  name, qualifiedName, reference, types[], doc, combinations
  variationPoints[]: name (Schlüssel, verschachtelt "vp/variante/feature"), feature,
                     parent{variationPoint, variant} | null, type, attributeNames[],
    variants[]: name, qualifiedName, reference, metaclass, type, doc, nested[],
      attributes{name: {kind, value, unit, display, text, items?}}
  rules[]: name, label, scope (system|component|interface), context, expression (geprüft),
           sourceExpression, appliesTo{variationPoint: [variant]} | null, owner, file, line, col
  keyFigures[]: name, expression, owner
  requirements[]: name, qualifiedName, doc, constraints[{kind, expression, ...}]
  configurations[]: name, doc, selection{variationPoint: variant}
```

`kind` ist `number`, `quantity` (mit `unit`), `string`, `boolean`, `element` (Enum-Literal),
`list` (mit `items`) oder `unknown`.

## Grenzen (Stand jetzt)

* Bauteilregeln werden für die direkten Teile des Systems gelesen (`cpu`, `mainboard`, ...),
  nicht für Teile in Teilen. Schnittstellenregeln für die Interfaces, die das System oder die
  Produktlinie selbst enthält.
* `bind` wird nur von einem Port-Feature zu einem Attribut oder Literal verfolgt
  (`bind port.x = y`), wie bei Ports üblich. Bindungen zwischen zwei Ports oder in Varianten-
  typen werden nicht verfolgt; eine Regel darüber bleibt „undecided“.
* Bei großen Modellen zeigt der Lösungsraum eine Stichprobe, nicht alle zulässigen
  Kombinationen. Die Pareto-Front ist die der geprüften Punkte; bessere Konfigurationen können
  außerhalb der Stichprobe liegen. Eine vollständige Aufzählung der zulässigen Kombinationen
  wäre der nächste Schritt (Constraint-Solver).
* Achsen sind die Kennwerte des Systems (Attribute mit Wert). Anforderungen werden in der
  Stichprobe nicht geprüft, um sie schnell zu halten.
* Bei sehr vielen Modelldateien kann unter Windows die maximale Kommandozeilenlänge
  (32.767 Zeichen) erreicht werden, weil jede Datei als Pfad übergeben wird.
