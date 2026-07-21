# Magie-Blueprint

Dauerhafte Kopie im Repo — Welle 1 ist vollständig umgesetzt; die Spezifikationen
unten bleiben als Referenz für die getroffenen Entscheidungen stehen.

## These
WOW = Koinzidenz × Kontrast × Absicht. Monteur hat den halben Faktor (beat-genaues
Timing) perfektioniert — Welle 1 macht daraus Treffer (Bild-Peak ehrlich ±0.25 s am
Cut-Lead-Punkt), Atem (echte Stille unter Dips und vor dem Drop, als explizit
entschiedene Umkehr über alle Render-Pfade) und ein abnahmefähiges Preview mit
sichtbaren Titeln. Alles deterministisch und lokal, unter Erhalt der Haus-Garantien —
Zero-Repeat, bit-identische Pins/Arrangements, Plan-Format-Toleranz — wobei 1.1/1.3
neutral degradieren und 1.5–1.7 als bewusste Default-Änderungen mit Fixture-Updates
geführt werden.

## Status
- 1.1 Peak-on-Beat — UMGESETZT (Koinzidenz 33%→93% auf Demo-Material).
- 1.2 Bewusste Stille (music_gaps, Träger-Pflicht, Pre-Drop-Beat, Re-Entry auf
  Downbeat/Hit, Continuous-Option, alle 5 Render-Wege) — UMGESETZT.
- 1.3 SFX source_offset (Riser spielen die LETZTEN run_up-Sekunden, Whoosh-Peak==Cut,
  Impact-Peak==Hit, Tails klingen aus; only-when-set-Serialisierung) — UMGESETZT.
- 1.6 Atem im Kanon (Post-Peak-Recovery-Cut, heiße/kühle 8er-Phrasengruppen in langen
  Klimax-Phasen; Default-Änderung → Fixtures) — UMGESETZT.
- 1.8 Titel im Preview (drawtext auf Black-Segmenten, geteilte Helfer mit Export,
  Sonden-gegatet) — UMGESETZT.
- 1.9 First-Frame-Gate für den Short-Hook (FrameMetric-Schärfe/Kontrast, nie am Peak
  vorbei) — UMGESETZT.
- 1.4 Ducking + Two-Pass-Loudnorm — UMGESETZT (Duck-Tiefe 6.00 dB gemessen,
  Export −13.98 LUFS; ehrliche Degradations-Notes bei extremem Crest-Faktor).
- 1.5 Drop-Intelligenz — UMGESETZT (bester Drop, Arc-Squeeze-Floor, Short-Drop-Pin,
  Loop-Naht mit Exit→Hook-Motion-Bonus, Low-Band-Feinschliff hinter Fixtures).
- 1.7 Frame-Hygiene — UMGESETZT (Sliver-Absorption ≥0.3 s, fps-bewusste Leads über
  cut_lead_for, Dissolves auf dem Grid, EIN geteilter Beat-Quantisierungs-Helfer
  an allen Dip/Dissolve/Title-Sites).

## Welle 1 — Spezifikationen (Referenz)

### 1.4 Ducking + Two-Pass-Loudnorm
−4…−6 dB Ducking-Envelopes am Musik-Bed unter jedem platzierten SFX-Akzent
(Impact/Braam-Fenster; Riser bekommt ein sanfteres Shelf, damit er über dem Bed in
seinen Hit liest) und unter prominenten O-Ton-Momenten im Mix-Modus — als
multiplizierende Volume-Envelopes in der bestehenden Linear-Chain-Gate-Architektur
(die music_gaps-Trapezoide komponieren identisch; Maschinerie wiederverwenden,
Komposition dokumentieren). Feste, dokumentierte dB-Werte; deterministisch.
render_export: echtes Two-Pass-loudnorm mit linear=true (Pass 1 misst, Pass 2 wendet
Messwerte an); render_preview bleibt Single-Pass (Tempo). Diese Maschinerie ist
zugleich der Unterbau für die W2-O-Ton-Pops — Naht im Docstring benennen.

### 1.5 Drop-Intelligenz (re-skopiert)
- BESTER Drop statt erster: In-Range-Drops nach musikalischem Gewicht scoren
  (Envelope-Sprunghöhe, Energie der Folgesektion), Klimax auf den besten pinnen.
- Arc-Squeeze-Floor: Nachbar-Phasengrenzen behalten Mindestanteile, wenn der Drop-Pin
  sie quetscht (bewusste Default-Änderung → Fixtures mit Kommentar).
- "short": pinnt einen Cut auf den Drop, den best_energy_window schon ins Fenster
  legt — Pin und _WINDOW_DROP_LEAD=0.15 ko-designen; Hook/Punch/Loop-Arc verifizieren.
- Loop-Naht (Shorts): Song-Fenster-ENDE auf eine anschlussfähige Phrasengrenze legen
  (musikalischer Rückschluss zum Fensteranfang) + Exit→Hook-Entry-Motion-Bonus fürs
  Casting des letzten Slots. Notes benennen die Naht.
- Low-Band-Onset-Feinschliff der Drop-Instants NUR hinter Regressions-Fixtures
  (ersetzt bestehenden Downbeat-Snap, wo die Low-Band-Evidenz eindeutig ist).
- Sekundär-Drop-Zwangs-Cuts bleiben DRAUSSEN (W2; braucht eigenes
  Phasen-Hold-Clearing).

### 1.7 Frame-Hygiene
- Sliver-Slots eliminieren: kein erzeugter Slot unter ~0.3 s / 2 Frames — an JEDER
  erzeugenden Stelle (Grid-Reste, Merges, Arrangement-Snapping, Dip-Carving);
  deterministisch in Nachbarn absorbieren.
- Typisierte fps-bewusste Cut-Leads: 0.04-s-Lead und Dissolve-Lead fps-bewusst
  (fps als Plan-Input oder saubere Sekunden-Näherung — EINE Entscheidung, überall);
  Dissolve-Lead 0 braucht Reorder, da _plan_finishing NACH dem Grid-Lead läuft.
- Dips/Dissolves beat-quantisiert über ALLE Sites: 3 Dissolve-Stellen +
  adjust_entry_boundary-Vertrag, 2 _DIP_SECONDS-Stellen, _TITLE_FADE/
  _DIP_MIN_REMAINDER — EIN geteilter Quantisierungs-Helfer. test_arrange mit
  Dual-Fixtures.

## Abnahme (Welle 1 gesamt)
Koinzidenz-Rate gegen ±0.25 s (nicht 1 Frame); Stille-Ehrlichkeit 0 %→100 %;
Sync-Assertion über vier Quellen (Bild-Peak, SFX-Peak, O-Ton, Titel);
Integrated Loudness −14 LUFS ±1 LU im Export; Flinch-Test PLUS Mute-Test
(trägt der Schnitt ohne Ton?).

## Wellen 2–4 (Kurzfassung; Detail-Synthese folgt nach Welle 1)
- W2: Sekundär-Drop-Zwangs-Cuts (mit Phasen-Hold-Clearing), O-Ton-Pops (markante
  Originalton-Momente punktuell über das Bed heben — Ducking-Maschinerie aus 1.4),
  J-/L-Cuts im Export (Ton führt/zieht nach).
- W3: Eye-Trace-Kontinuität (Blickführung über Schnitte; Murchs Regel 4), Shot-Size-
  Grammatik (weit→mittel→nah-Wechselregeln), visuelle Reime/Callbacks.
- W4: Render→Watch→Refine-Selbstschleife (das System schaut sein eigenes Preview und
  iteriert bis zur Abnahme-Metrik), lernende Präferenzen aus Nutzer-Korrekturen.
