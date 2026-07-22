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

## Wellen 2–4 (Kurzfassung)
- W2: Sekundär-Drop-Zwangs-Cuts (mit Phasen-Hold-Clearing), O-Ton-Pops (markante
  Originalton-Momente punktuell über das Bed heben — Ducking-Maschinerie aus 1.4),
  J-/L-Cuts im Export (Ton führt/zieht nach).
- W3: Eye-Trace-Kontinuität (Blickführung über Schnitte; Murchs Regel 4), Shot-Size-
  Grammatik (weit→mittel→nah-Wechselregeln), visuelle Reime/Callbacks.
- W4: Render→Watch→Refine-Selbstschleife (das System schaut sein eigenes Preview und
  iteriert bis zur Abnahme-Metrik), lernende Präferenzen aus Nutzer-Korrekturen.

## Welle 2 — Detail-Spezifikation (UMGESETZT; verankert im Post-Welle-1-Code)
Status: 2.1/2.2/2.3 umgesetzt und getestet (tests/test_magie_wave2.py, 12 Tests;
volle Suite 1888 passed + 1 skipped). Sekundär-Drops feuern nur bei echtem
musikalischem Gewicht — bestehende Arc-Fixtures unverändert (test_social grün).

### 2.1 Sekundär-Drop-Zwangs-Cuts (Arc-Styles) mit Phasen-Hold-Clearing
Anker: montage.py Zeile ~124–133 (Doc), Drop-Pin ~1813–1826, `_drop_hold` (1458).
Heute forcieren nur "auto"/"short" auf JEDEM in-range Drop einen Cut mit Hold und
räumen Grid-Cuts ~2 Beats danach frei. In Arc-Styles (trailer/paced/wedding/…)
pinnt nur der Klimax auf den BESTEN Drop; die Sekundär-Drops bleiben ungenutzt.
- Sekundär-Drops (alle in-range Drops außer dem Klimax-Pin) nach `drop_weight`
  sortieren; die stärksten K forcieren einen Cut EXAKT auf dem Drop — aber nur wenn
  sie musikalisch tragen (Mindest-`drop_weight`-Schwelle, dokumentiert) und weit
  genug vom Klimax und voneinander entfernt sind (Mindestabstand in Beats).
- Phasen-Hold-Clearing: Ein Sekundär-Drop, der in einen laufenden Phasen-Hold
  (Opening-Hold, langer Klimax-Hold) fällt, darf den Hold nicht zerschneiden ohne
  ihn zu RÄUMEN — d. h. den Hold bis zum Drop laufen lassen, am Drop hart schneiden,
  danach das Phasen-Muster sauber neu aufsetzen (kein Sliver, kein halber Hold).
  Der Klimax-Pin und seine Arc-Squeeze-Floors (1.5) bleiben unangetastet.
- Der Sekundär-Drop-Slot ist ebenfalls ein HOLD (2-Beat-Minimum wie der Klimax),
  bekommt einen Impact-Cue (wie "auto": Impact auf jedem drop-forced Cut, montage.py
  482), und der stärkste ungenutzte Moment wird darauf gecastet.
- Bewusste Default-Änderung → test_social.py-Fixtures für die betroffenen Arc-Styles
  neu, feld-diff-dokumentiert. "auto"/"short" bleiben byte-identisch (haben's schon).
  Byte-Parität schützt Fallbacks, nicht die sanktionierten Arc-Defaults.

### 2.2 O-Ton-Pops (Originalton punktuell über das Bed heben)
Anker: preview.py Zeile 165–166 & 879–880 (benannte W2-Naht), `ducking_windows`/
`_duck_filters`/`_bed_envelope_filters`, `_DUCK_OTON_DB`, die bestehende Prominenz-
Messung (Teile ≥ `_DUCK_OTON_STANDOUT_DB` über dem Median via volumedetect).
- Spiegle die Ducking-Maschinerie als LIFT: eine Volume-Envelope mit Floor > 1
  (Boost, z. B. +3…+4 dB, dokumentiert) auf der ORIGINALTON-Kette über jedem
  markanten O-Ton-Fenster — dieselbe Trapez-Mechanik, dieselbe Linear-Chain-
  Komposition. Der Pop ist die andere Seite der Medaille zum Musik-Ducking:
  unter dem O-Ton-Fenster duckt die Musik (schon da), im selben Fenster hebt der
  O-Ton (neu). Nur im Mix-Modus.
- Deterministisch, gemessen (nicht geraten): dasselbe volumedetect-Fenster wie 1.4.
  Ein Pop braucht Kopf­raum — nach dem Lift darf der O-Ton −1 dBTP nicht reißen
  (clampen, ehrliche Note wenn geclamped). Export UND Preview? Preview mischt keinen
  O-Ton-Pop wenn es auch SFX nicht mischt — konsistent mit 1.4 (Pops = Deliverable,
  gehören in render_export; Preview bleibt schlank). Naht im Docstring benennen.
- Leere Fenster ⇒ byte-identische alte Graphen.

### 2.3 J-/L-Cuts im Export (Ton führt / zieht nach)
Anker: io/fcpxml.py (verbundene Audio-Clips, Offset = Schnittpunkt, Zeile ~49;
`_audio_lane`/`_lane_track`), preview.py render_export Originalton-Bett (~1282+).
- An ausgewählten Szenen-Übergängen den Originalton-Schnittpunkt frame-genau vom
  BILD-Schnitt entkoppeln: J-Cut (Ton des NÄCHSTEN Shots setzt vor dem Bildschnitt
  ein — Antizipation) bzw. L-Cut (Ton des VORIGEN Shots klingt über den Bildschnitt
  nach — Kontinuität). Kleiner, typisierter Lead/Lag (z. B. 3–8 Frames, fps-bewusst
  via cut_lead_for-Denkart), deterministisch.
- NUR wo es dient: nicht am Peak-on-Beat-Cut (Sync ist heilig), nicht über
  music_gaps/Stille-Kanten, nicht über einen drop-forced Cut, nicht wenn ein
  platzierter SFX/Impact am Schnitt sitzt. Bevorzugt an ruhigen Continuity-Merges
  und Hot→Cool-Phrasenwechseln.
- fcpxml: der Originalton-Connected-Clip bekommt In/Out + Offset so, dass er den
  Bildschnitt um Lead/Lag überlappt (Resolve-Roundtrip trägt das nativ). Export
  (ffmpeg): das Originalton-Segment entsprechend früher/später einblenden mit
  Mikro-Crossfade an der Naht. Musik-Bett und Grid unberührt.
- Plan-Format-Toleranz: J/L-Metadaten nur-wenn-gesetzt serialisieren; hand-gebaute
  Pläne ohne J/L bleiben byte-identisch.

## Abnahme (Welle 2)
Sekundär-Drops: jeder geforcte Sekundär-Cut liegt exakt (±1 Frame) auf seinem Drop,
kein Sliver, Klimax-Pin/Arc-Floors unverändert. O-Ton-Pop: gemessener Boost am
markanten Moment, O-Ton-Peak liest über dem Bett (RMS-Assertion im Fenster),
−1 dBTP gehalten. J/L: Ton-Kante messbar vom Bild-Schnitt versetzt, Peak-on-Beat-
und Drop-Cuts nachweislich UNVERSETZT. Haus-Garantien: Zero-Repeat, bit-identische
Pins/Arrangements, "auto"/"short" byte-identisch, Fallback-Byte-Parität.

## Welle 3 — Detail-Spezifikation (UMGESETZT; filmtheoretisch verankert)
Status: 3.1/3.2/3.3 umgesetzt (monteur/spatial.py: Fokuspunkt + Shot-Size, Cache
.monteur-spatial.json wie daylight; Moment.shot_size/entry_focus/exit_focus
only-when-set; Shot-Grammatik wide→medium→close, Eye-Trace-Tie-Breaker,
visuelle Reime). tests/test_magie_wave3.py (27 Tests); volle Suite 1915 + 1.
Alles Tie-Breaker — Sync/Drop/Rhythmus gewinnen; Fallback-Byte-Parität wo kein
Bild-Signal da ist.

Grundthese W3: Bis hier trifft der Schnitt den TON. Welle 3 macht das BILD kohärent —
Walter Murchs „Rule of Six" (Emotion > Story > Rhythmus > Eye-Trace > 2D-Ebene >
3D-Kontinuität) sagt: die untersten Ränge dürfen für die oberen geopfert werden,
aber wo Emotion/Story/Rhythmus schon sitzen (Welle 1+2), heben Eye-Trace und
Bildgrammatik das Ergebnis von „richtig getimt" auf „müheloses Sehen".

### 3.1 Eye-Trace-Kontinuität (Blickführung über Schnitte; Murchs Regel 4)
- Pro Clip die Position des Aufmerksamkeitspunkts schätzen (Bewegungsschwerpunkt /
  Salienz aus den schon extrahierten Frame-Metriken — kein neues ML, deterministisch
  und offline; nutze die vorhandenen 64×36-RGB/Metrik-Frames wie daylight.py).
- Beim Casting/Ordering zweier benachbarter Slots einen sanften Bonus, wenn der
  Aufmerksamkeitspunkt des ausgehenden Frames nahe dem des eingehenden liegt
  (Blick springt nicht quer über die Leinwand) — bzw. bewusster, dosierter
  Kontrast an gewollten Akzenten. Als Scoring-Term im bestehenden Cast/Merge-Pfad,
  NICHT als harte Regel (Rang 4 weicht Rang 1–3: Peak-on-Beat, Drop, Rhythmus
  gewinnen immer).
- Am Peak-on-Beat-/Drop-Cut NIE die Sync opfern — Eye-Trace ist nur Tie-Breaker.

### 3.2 Shot-Size-Grammatik (weit → mittel → nah)
- Shot-Size pro Clip klassifizieren (Näherung aus Motiv-/Salienz-Größe im Frame:
  wide/medium/close), offline, deterministisch, only-when-set serialisiert.
- Grammatik-Bonus beim Ordering: Etablieren (weit) → Entwickeln (mittel) →
  Zahlen (nah); zwei gleich große Shots hintereinander werden mild bestraft
  (Ausnahme: bewusste Intensivierung close→close am Klimax). Beim Motorrad:
  Totale der Straße → Fahrer/Maschine mittel → Detail (Gasgriff, Tacho, Blick).
- Fügt sich in Hot/Cool-Phrasengruppen (1.6) und die Daylight-Kohärenz ein —
  Bildgröße ist eine WEITERE Kontrast-Achse, kollidiert nicht mit den bestehenden.

### 3.3 Visuelle Reime / Callbacks
- Paare visuell verwandter Shots erkennen (ähnliche Komposition/Bewegung/Farbe
  über die vorhandenen Metriken) und, wo Rhythmus es zulässt, als Reim setzen:
  ein Motiv am Anfang, sein Echo am Ende (rahmt das Video), oder ein Match-Cut
  an einer Phrasengrenze. Sparsam und bewusst — ein Reim, der wirkt, nicht zehn,
  die zur Masche werden. Respektiert Zero-Repeat (ein Reim ist Ähnlichkeit, NICHT
  derselbe Moment doppelt).

### Abnahme (Welle 3)
Eye-Trace: messbar geringere durchschnittliche Blickpunkt-Distanz über Schnitte
ggü. Baseline, OHNE dass ein Peak/Drop-Cut sich verschiebt. Shot-Grammatik:
messbar weniger gleichgroße Nachbar-Paare, Etablier-Shot vorn. Reime: erkannt und
gesetzt, Zero-Repeat unverletzt. Haus-Garantien wie immer; Bild-Scoring als
Tie-Breaker, nie über Sync/Drop/Rhythmus.

## Welle 4 — Detail-Spezifikation (UMGESETZT; verankert im Post-W3-Code)
Status: 4.1/4.2/4.3 umgesetzt (monteur/critique.py Scorecard, monteur/refine.py
opt-in-Schleife, monteur/preferences.py ~/.monteur/preferences.json). tests/
test_magie_wave4.py (19 Tests) + test_web (+4); volle Suite 1938 + 1, NULL
Fixtures angefasst (Byte-Parität hielt). refine: schwacher Plan Koinzidenz
0%→80%; Präferenz-Signal kippt Klimax medium→close; Default byte-identisch,
alle Wave-4-Terme Tie-Breaker unter einem Order-Schritt. Damit ist die
Magie-Blueprint (Wellen 1–4) vollständig.

Grundthese W4: Bis hier baut Monteur EINEN Schnitt und hofft, dass er sitzt. W4
schließt die Schleife — das System bewertet seinen eigenen Schnitt gegen die
Abnahme-Metriken der Wellen 1–3, iteriert deterministisch bis sie erfüllt sind,
und lernt aus den Korrekturen des Nutzers. Alles offline, deterministisch,
unter Erhalt der Haus-Garantien.

### 4.1 Selbst-Kritik (der „Watch"-Teil)
Anker: Plan-`notes` (Koinzidenz/Stille/Drops), `monteur.coverage`, render_export
misst `input_i` (LUFS, preview.py:1089).
- `critique(plan, *, measured_lufs=None) -> Scorecard`: ein deterministischer
  Prüfbericht gegen JEDE Abnahme-Metrik der Wellen 1–3 — Peak-Koinzidenz-Rate
  (aus Peak/Beat im Plan, ±0.25 s), Stille-Ehrlichkeit (jede music_gap hat einen
  Träger), Sliver-Freiheit (kein Slot < 0.3 s), Shot-Grammatik-Verstöße (gleich
  große Nachbarn), Drop-Treffer, und — wenn gemessen — Integrated Loudness gegen
  −14 ±1 LU. Jede Metrik: Wert + Bestanden/Durchgefallen + welche Slots schuld
  sind. Rein aus dem Plan (+ optionaler Messung), kein Video-Redecode nötig.
- render_export gibt seinen gemessenen `input_i` im Ergebnis zurück (heute nur
  intern), damit die Schleife ohne Extra-Pass messen kann.

### 4.2 Refine-Schleife (der „Refine"-Teil)
- `refine_plan(reports, music, ..., budget=N) -> (best_plan, history)`: plant,
  kritisiert, und wenn Metriken durchfallen, dreht sie GEZIELT an den passenden
  Stellschrauben (deterministisch, nicht zufällig) und plant neu — z. B. zu viele
  Sliver → Cut-Dichte runter; Koinzidenz schwach → aim/lead nachjustieren;
  Grammatik-Verstöße → Ordering-Gewicht hoch. Behält den Plan mit dem besten
  Scorecard-Aggregat. Beschränkte Iterationen (budget), voll deterministisch:
  gleiche Eingabe → gleiche Schleife → gleicher Gewinner. Die bestehende
  Ein-Schuss-Planung bleibt der Default; refine ist opt-in (ein Flag /
  Studio-Knopf „bis es sitzt"), damit alle Byte-Paritäten unberührt bleiben.
- Notes protokollieren die Schleife ehrlich („refine: 3 Durchläufe, Koinzidenz
  71%→94%, Loudness angenommen"), keine stille Iteration.

### 4.3 Lernende Präferenzen (aus Nutzer-Korrekturen)
Anker: Studio-Edits `/api/plan/adjust` (Slot/Transition/Boundary),
`/api/alternatives` (Swap), `~/.monteur/` (drafts.json/settings.json Muster).
- Ein Präferenz-Store `~/.monteur/preferences.json` (Env-Override wie drafts/
  settings, für Test-Isolation). Wenn der Nutzer einen Slot swappt, eine Grenze
  zieht oder eine Transition ändert, wird das SIGNAL abstrahiert gespeichert —
  nicht „Clip X statt Y", sondern die Richtung (z. B. „bevorzugt längere Holds
  in ruhigen Phasen", „mag close-ups am Klimax", „weniger Dissolves"). 
- Beim nächsten Planen fließen die Präferenzen als KLEINE Casting-/Ordering-
  Bias-Terme ein — Tie-Breaker, nie über Sync/Drop/Rhythmus, nie Zero-Repeat
  verletzend. Leerer Store ⇒ byte-identisch zum heutigen Verhalten (Fallback-
  Parität). Deterministisch bei gegebenem Store.
- Konservativ: nur robuste, wiederholte Signale zählen (ein einzelner Swap
  kippt nichts); der Store ist inspizierbar/löschbar (der Nutzer sieht die CLI
  nie, aber die App kann „gelerntes zurücksetzen" bieten).

### Abnahme (Welle 4)
Kritik: der Scorecard stimmt mit unabhängiger Messung überein (LUFS, Koinzidenz-
Rate) auf echtem Render. Refine: ein absichtlich schwacher Erst-Plan wird über
die Schleife nachweislich besser (Metrik steigt), deterministisch reproduzierbar,
und der Default (ohne refine) bleibt byte-identisch. Lernen: ein simuliertes
Korrektur-Signal verschiebt eine spätere Casting-Entscheidung in die erwartete
Richtung, leerer Store bleibt byte-identisch, Zero-Repeat/Sync/Drop unverletzt.
Haus-Garantien wie immer.
