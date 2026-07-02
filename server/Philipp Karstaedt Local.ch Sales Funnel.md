# Senior Product Owner AI Sales Agent / Sales-Tools

## High Level
Zuerst würde ich die Realität und Anforderungen des internen Stakeholders (Sales-Manager) klären. Die Fragestellung deutet fundamental auf eine wiederkehrende Sales-Intelligence-Problemstellung hin.

Zwei Excel-Listen würde ich nicht als Zielarchitektur behandeln. Sie zeigen, welche Daten der Sales Manager heute manuell zusammenführt. Für eine erste Analyse würde ich sie einmalig als Diskussionsgrundlage nutzen, um Datenfelder, Join-Key, Datenqualität und Zieloutput zu verstehen. Für einen wiederkehrenden Sales-Prozess sollte die Logik direkt an Salesforce und die zugrunde liegende local.ch-Datenquelle angebunden werden. Sonst automatisiert man nur einen manuellen Workaround.

### Zentrale Annahmen

1. Salesforce ist das führende System für Kundendaten, Vertragsstatus, Produkte, Umsatz und Sales-Aktionen.

2. Die Kundennummer ist der zentrale Join-Key zwischen Salesforce und local.ch-Daten.

3. Ich habe exemplarisch öffentliche local.ch-Profile geprüft; eine interne Kundennummer scheint dort nicht einfach sichtbar zu sein. Deshalb würde ich zuerst validieren, ob Liste B nicht doch eine intern angereicherte Quelle ist.

4. Der AI-Agent darf nur öffentliche Website-Informationen analysieren und keine Kundendaten verändern.

### Workflow-Trennung

1. **Vertragsreaktivierung:** kein AI-Kernproblem. Beispiel: Vertragsablauf innerhalb von 60 Tagen + aktiver Kunde + Umsatz-/Produktsegment → Salesforce Task oder Kampagnenzuordnung. AI könnte später bei Angebotsberechnung oder Outreach-Texten helfen, das sprengt hier aber den Scope.
2. **Website-Potenzialanalyse:** AI-geeignet, weil Websites qualitativ bewertet und in konkrete Sales-Anlässe übersetzt werden müssen.
3. **Salesforce-Writeback:** nachgelagert und regelbasiert. Der AI-Agent schreibt nicht direkt in Salesforce, sondern liefert validiertes JSON. Daraus entstehen Tasks, Flags, Kampagnenzuordnungen oder Human-Review-Fälle.

Im Kern geht es zuerst um saubere Segmentierung, klare Systemgrenzen und einen kontrollierten AI-Analyseprompt mit strukturiertem Output. Die zurückgeschriebene Analyse kann der Sales Manager nutzen, um Kunden gezielt zu kontaktieren und passende Upsell-Möglichkeiten darzulegen.

### Toolwahl

1. Salesforce Flow / Salesforce Campaigns für Vertragsreaktivierung, weil Vertragsablauf, Kundensegment, Produktbestand und Kampagnenlogik regelbasiert sind.

2. Python / SQL auf VM für deterministisches Scaffolding um den Agenten: Join über Kundennummer, Dublettenprüfung, fehlende Websites, ungültige URLs, fehlende Vertragsdaten sowie günstiges HTML-/Metadata-Scraping vorab, um unnötige teure LLM-/Browser-Agent-Analysen zu vermeiden.

3. Browserfähiger AI-Agent mit Playwright/Chrome für die Website-Potenzialanalyse. In Frage kommen Agenten mit Skill-Support, Playwright und visueller Analysefähigkeit, z.B. LangGraph/LangChain + Playwright, OpenAI/Codex oder Claude Code App Server oder ein abgesicherter Hermes-Agent auf VM.

4. Versionierter Website-Upsell-Skill als Bewertungsgrundlage, damit die Analyse konsistent bleibt und nicht bei jeder Website frei improvisiert.

Auf dieser Basis läuft ein günstiger deterministischer Website-Check, getriggert über Salesforce-Flows. Nur wenn sich die Website verändert hat, wird die vollständige AI-Website-Potenzialanalyse gestartet. Dadurch werden Crawling- und LLM-Kosten begrenzt, ohne relevante Website-Änderungen zu verpassen. Es müsste geklärt werden, wie man mit den Backlog an existierenden Kunden umgehet.

Der `$localsearch-upsell-analysis` Skill ist eine versionierte Bewertungsrubrik für den Website-Agenten. Dieser Skill ist ein wichtiger einmaliger Setup-Schritt. Er sollte nicht nur technisch geschrieben werden, sondern cross-funktional mit Sales, Marketing, Product und ggf. Web-/SEO-Experten entstehen. Er beschreibt den Benchmark einer "guten" kommerziellen Website, die localsearch-Angebotslogik, erlaubtes Browser-Verhalten und das JSON-Output-Schema. Dadurch bewertet der Agent Websites konsistent nach Sales-relevanten Kriterien wie Mobile-Nutzung, Technik, Kontakt, CTA, lokaler Sichtbarkeit, Trust-Signalen, Aktualität, SEO und Buchungs-/Shop-/Anfragefähigkeit, und zwar so dass es sie direkt mit dem Angebot von localsearch.ch in Verbindung bringt.

## Skill und Prompt

### Prompt für den Website-Analyse-Agenten

#### System Prompt
Du bist ein AI Sales-Enablement-Agent für Website-Analysen.
Du hast Browserzugriff über Chrome/Playwright. Nutze den bereitgestellten $localseach-upsell-analysis skill als verbindliche Bewertungsgrundlage.

Der Skill enthält:

1. **Benchmark guter Websites**
   Der Skill beschreibt, wie eine zeitgemäße kommerzielle Website bzw. E-Commerce-Präsenz idealerweise funktioniert. Er definiert Kriterien wie mobile Nutzung, technische Stabilität, Ladezeit, klare Positionierung, verständliche Produkt-/Leistungsdarstellung, einfache Kontaktaufnahme, starke Call-to-Actions, Buchung/Anfrage/Shop-Funktionalität, lokale Sichtbarkeit, Trust-Signale und Aktualität.

   Zweck dieses Benchmarks ist, für jede analysierte Website den Abstand zum möglichen Optimum sichtbar zu machen

2. **localsearch-Angebotslogik**
   Eine Erklärung, bei welchen Website-Schwächen localsearch sinnvoll helfen kann. Die Kernfrage lautet: Welche konkreten Schwachpunkte der Website können mit welchen localsearch-Angeboten adressiert werden? Was hat der Kunde schon? Was braucht er noch? Wo sind die Möglichkeiten, zusätzliche Angebote oder hochqualitative Updates zu verkaufen? 

3. **Browser-/Playwright-Anleitung**
   Wie du die Website prüfen sollst: relevante öffentliche Seiten besuchen, keine Formulare absenden, keine Kundendaten ändern, keine Kundenkommunikation auslösen.

4. **Output-Schema**
   Eine semantische Erklärung, was jedes JSON-Feld bedeutet und wie Score, Empfehlung, Evidenz, Unsicherheiten und Human-Review-Flag zu befüllen sind.

Ziel:
Bewerte, ob die Website ihr digitales Potenzial bereits gut ausschöpft oder ob ein sinnvoller Gesprächsanlass für Website-Neubau, Website-Optimierung, lokale Sichtbarkeit, local.ch-Präsenz oder verwandte localsearch-Angebote besteht.

Aufgabe:

1. Besuche die Website über den Browser.
2. Prüfe nur öffentlich sichtbare Informationen.
3. Vergleiche die Website mit dem Benchmark aus dem Skill.
4. Identifiziere konkrete Schwachpunkte.
5. Ordne diese Schwachpunkte passenden localsearch-Angebotsbereichen zu.
6. Vergib einen Website-Potenzial-Score von 1 bis 5.
7. Empfiehl genau eine nächste Aktion:

   * Website-Neubau anbieten
   * Website-Optimierung anbieten
   * lokale Sichtbarkeit / local.ch-Präsenz prüfen
   * E-Commerce / Buchung / Anfrageprozess prüfen
   * manuelle Prüfung nötig
   * kein klarer Handlungsbedarf
8. Formuliere einen kurzen Gesprächsaufhänger für Sales.
9. Markiere Unsicherheiten klar.

Regeln:

* Keine Fakten erfinden.
* Keine Formulare absenden.
* Keine Kundendaten ändern.
* Keine automatischen E-Mails schreiben.
* Technische Beobachtungen und Sales-Interpretation trennen.
* Ausgabe ausschließlich als JSON.

#### User Prompt
Der Agent erhält für jede Analyse als Input:

Input:

* customer_id
* customer_name
* language
* website_url
* existing_products
* revenue_segment
* last_analysis_date

#### Output Schema

{
"customer_id": "...",
"website_reachable": true,
"potential_score": 1,
"recommended_action": "...",
"sales_reason": "...",
"talk_track": "...",
"matched_localsearch_opportunity": "...",
"detected_weaknesses": [
{
"weakness": "...",
"why_it_matters": "...",
"possible_localsearch_solution": "..."
}
],
"evidence": [
{
"observation": "...",
"source_url": "..."
}
],
"uncertainties": ["..."],
"needs_human_review": false,
"skill_version": "..."
}

Der Output wird dann durch deterministischen Code wieder nach Salesforce für den Kunden zurückgeschrieben. Flags für Sales Manager Review werden gesetzt und der Sales Manager hat einen direkten Leitfaden für das Kundengespräch.

### $localseach-upsell-analysis Skill als Benchmark / Bewertungsgrundlage

Vor dem produktiven Einsatz braucht der Agent eine Bewertungsrubrik: Was gilt als gute KMU-Website bzw. als ausgeschöpftes digitales Potenzial? 

Beispielkriterien:

* mobil gut nutzbar
* schnell und technisch sauber
* klare Kontaktmöglichkeiten
* Online-Shop / e-Kommerzimplementierung aktuell und sauber?
* sichtbare Adresse, Öffnungszeiten und lokale Informationen
* klare Beschreibung von Leistungen/Produkten
* Call-to-Action-Analyse
* Möglichkeit für direkte Buchung, Anfrage oder Shop, falls relevant
* aktuelle Inhalte
* lokale SEO-Signale
* Vertrauen: Bewertungen, Referenzen, Bilder, Team, Zertifikate
* Verbindung zu local.ch bzw. lokaler digitaler Präsenz

Analysedimension:
Der Agent braucht: klare Beschreibung wo localsearch.ch helfen kann bzw welche Dienste und Services verkauft werden. Basierend auf der Gegenüberstellung von Webseite und den Beispielkriterien kann der Agent dann direkt die Upsell-Empfehlung geben. Die Fragestellung lautet im Endeffekt: Welche Schwachpunkte auf der Website können mit welchen Angeboten von Localsearch.ch bereinigt werden? 

Weiterhin enthält der Skill Anleitungen für den Agenten wie er die Playwright-Browser-Automierung bedienen kann und eine semantische Erklärung des Output-Schemas.

