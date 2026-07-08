# Monteur — Launch-Checkliste

Strategie: **Open-Core.** Der MCP-Server + CLI sind der kostenlose,
quelloffene Wachstumsmotor (Distribution über MCP-Verzeichnisse und
GitHub); Monteur Studio und Komfort-Features werden das Bezahlprodukt.
Positionierung: *editorielle Intelligenz*, nicht Fernbedienung — gegen
das kostenlose davinci-resolve-mcp (rohe API-Kontrolle) differenzieren
wir uns über Urteil: Sichtung, Beats, Pacing, Auto-Schnitt.

## Phase 0 — Vor jeder Veröffentlichung (Hygiene)

- [ ] Repo umbenennen (`monteur`), Beschreibung + Topics setzen
      (davinci-resolve, mcp, ai, video-editing, film)
- [ ] Lizenz entscheiden: Empfehlung Open Core — Engines/CLI/MCP unter
      MIT oder Apache-2.0, Studio-App proprietär (oder alles offen und
      später Cloud/Support monetarisieren; Entscheidung dokumentieren)
- [ ] Eigener Praxistest: 1 echtes Projekt (Footage + Song) durch den
      kompletten Create-Workflow; Heuristiken nachjustieren
- [ ] Installations-Doku für macOS/Windows von Null (Python, pipx,
      Resolve-Scripting aktivieren), von einer unbeteiligten Person
      getestet
- [ ] 60–90s Demo-Video: Ordner + Song rein → fertige Timeline in
      Resolve (Bildschirmaufnahme, kein Talking Head nötig)

## Phase 1 — Soft Launch (Distribution ohne Budget)

- [ ] PyPI-Release `monteur` (pip install monteur)
- [ ] MCP-Verzeichnisse eintragen: mcpmarket.com, mcpservers.org,
      Claude-Code-Marketplaces, Awesome-MCP-Listen (PR)
- [ ] GitHub-README mit GIF/Video oben, klare "vs. davinci-resolve-mcp"
      Abgrenzung ("hands vs. eyes")
- [ ] Blackmagic-Forum + r/davinciresolve: EIN ehrlicher Show-and-Tell-
      Post ("Ich habe ein Open-Source-Tool gebaut, das …"), danach
      dauerhafte Präsenz durch Fragen-Beantworten (kein Spam; 17x
      bessere Konversion durch anhaltende Präsenz vs. Launch-Spike)
- [ ] Product Hunt / Indie-Hackers-Post nachziehen, sobald erste
      externe Nutzer Feedback gegeben haben

## Phase 2 — Creator-Kanal (der eigentliche Hebel)

- [ ] 3–5 Resolve-Tutorial-YouTuber identifizieren (10k–200k Abos,
      Workflow-Fokus) und langfristige Partnerschaften anbieten
      (Rev-Share/Hybrid statt Einmal-Sponsoring; ausführliche
      Workflow-Videos konvertieren besser als Ad-Reads)
- [ ] Eigene Tutorial-Serie: "Drehtag → erster Schnitt in 10 Minuten",
      "Claude schneidet mit mir" (der MCP-Wow-Moment ist sehr
      video-tauglich)
- [ ] Feedback-Loop: Discord oder GitHub Discussions für frühe Nutzer

## Phase 3 — Monetarisierung

- [ ] Preismodell testen: Einmalkauf 49–79 € für Studio-App ODER
      15–29 €/Monat mit Team-Features; Abo-Müdigkeit im Hobby-Segment
      ernst nehmen (Blackmagic hat den Markt auf "einmal zahlen" erzogen)
- [ ] AppSumo-Lifetime-Deal als Kickstart erwägen (Reichweite gegen
      Marge)
- [ ] Referenz-Kunden: 5 Filmemacher, die Vorher/Nachher-Zeitersparnis
      bezeugen ("Sichtung: 3 Stunden → 10 Minuten")

## Risiken im Blick behalten

- Blackmagic baut native KI-Features (Resolve-Releases beobachten;
  unsere Nische: Musik-Montage, Pacing-Urteil, Claude-Anbindung)
- Eddie AI expandiert im Scripted-Bereich → Assembly-Feature nicht
  frontal gegen Eddie vermarkten
- MCP-Server-Kategorie kommodifiziert sich → Differenzierung immer
  über die Engines, nie über "Resolve-Steuerung"
