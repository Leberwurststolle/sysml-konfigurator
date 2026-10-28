package Fahrrad {
    doc /* Kleines Testmodell: Regeln in Bauteil-Definitionen und Schnittstellen,
         * Port-Werte über bind, variantenspezifische Regel, verschachtelte Variation. */

    private import ScalarValues::*;

    enum def Achse {
        enum Schnellspanner;
        enum Steckachse;
    }

    port def AchsPort {
        attribute achse : Achse;
    }

    interface def AchsVerbindung {
        end nabe : AchsPort;
        end ausfall : ~AchsPort;
        assert constraint { nabe.achse == ausfall.achse }
    }

    part def Rahmen {
        attribute preis : Real;
        attribute achse : Achse;
        attribute maxReifen : Real;
        port ausfallende : ~AchsPort;
        bind ausfallende.achse = achse;
    }

    part def Laufrad {
        attribute preis : Real;
        attribute achse : Achse;
        attribute breite : Real;
        attribute speichen : Natural;
        attribute farbe : String;
        port nabe : AchsPort;
        bind nabe.achse = achse;
        assert constraint genugSpeichen { speichen >= 20 }
    }

    part def Alu :> Rahmen { :>> preis = 400.0; :>> achse = Achse::Schnellspanner; :>> maxReifen = 40.0; }
    part def Carbon :> Rahmen { :>> preis = 1200.0; :>> achse = Achse::Steckachse; :>> maxReifen = 32.0; }

    part def Strasse :> Laufrad { :>> preis = 300.0; :>> achse = Achse::Schnellspanner; :>> breite = 28.0; :>> speichen = 24; :>> farbe = "Schwarz"; }
    part def Aero :> Laufrad {
        :>> preis = 900.0; :>> achse = Achse::Steckachse; :>> breite = 28.0; :>> speichen = 18;
        assert constraint nurMitCarbon { preis > 0.0 }
    }

    part def Rad {
        part rahmen : Rahmen;
        part laufrad : Laufrad;
        interface : AchsVerbindung connect nabe ::> laufrad.nabe to ausfall ::> rahmen.ausfallende;
        attribute gesamtPreis : Real = rahmen.preis + laufrad.preis;
        assert constraint reifenPasst { laufrad.breite <= rahmen.maxReifen }
    }

    part radLinie : Rad {
        variation part :>> rahmen : Rahmen {
            variant part alu : Alu;
            variant part carbon : Carbon;
        }
        variation part :>> laufrad : Laufrad {
            variant part strasse : Strasse;
            variant part aero : Aero {
                variation attribute :>> farbe : String {
                    variant attribute rot = "Rot";
                    variant attribute weiss = "Weiß";
                }
            }
        }
    }

    part stadtrad :> radLinie {
        part :>> rahmen = radLinie::rahmen::alu;
        part :>> laufrad = radLinie::laufrad::strasse;
    }

    part rennrad :> radLinie {
        part :>> rahmen :> radLinie::rahmen::carbon;
        part :>> laufrad :> radLinie::laufrad::aero {
            attribute :>> farbe :> radLinie::laufrad::aero::farbe::weiss;
        }
    }
}
