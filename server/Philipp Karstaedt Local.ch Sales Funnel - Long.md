# Salesforce

## High Level
Zuerst geht es darum, die Realität und Anforderungen des internen Stakeholders zu erforschen. Die Aufgabenstellung zeigt auf eine automatisierbare, oft wiederkehrende Problemstellung hin. Ad-hoc zusammengehackte Scripts für zwei Excel Listen ist ein grundsätzlich zu verwerfender Ansatz.

## Workflows
Ich würde die Aufgabe in zwei klar getrennte Workflows aufteilen.

### Vertragsreaktivierung

Vertragsreaktivierung würde ich nicht als AI-Aufgabe behandeln. Das ist ein Salesforce-nativer, regelbasierter CRM-Workflow.

Beispiel:
Vertragsablauf in 60 Tagen + aktiver Kunde + Umsatzsegment A/B + Produkt X → Salesforce Task oder Kampagnenzuordnung für Reaktivierungsaktion Y.

Dafür braucht es kein AI. Konfigurierbare Salesforce Flow Automation reicht völlig aus. Die externe AI-Komponente würde ich nur für den zweiten Teil einsetzen: die qualitative Website-Potenzialanalyse. Es ergeben sich potenziell Ansatzpunkte für Gen-AI oder AI-assistierte Analyse für Reaktivierungs-Promo-Aktion falls Outreach automatisch über den Salesforce Flow getriggert werden soll. Wie hoch sollte die promotion sein? Wie sollte die Promotions E-Mail aussehen? Sprengt aber hier den Scope.

### Website-Potenzialanalyse

Das ist der AI-Teil. Für Kunden ohne unmittelbaren Vertragsablauf würde ein browserfähiger AI-Agent die bestehende Website analysieren und ein strukturiertes Ergebnis zurückgeben. Ich nehme an localsearch hat ein IT-Team, das eine VM bereitstellen kann.

Für diesen zweiten Workflow zieht die VM regelmäßig den relevanten Kundenausschnitt aus Salesforce und verbindet ihn über die Kundennummer mit der Datenquelle, die Liste B zugrundeliegt. Manuelle Excel-Exports skalieren schlecht - wir fragen den Sales Manager, wo er Liste B erstellen lässt und binden uns direkt an.

Kritische Annahme: Liste B muss bereits eine interne Kundennummer enthalten. Öffentlich sichtbare local.ch-Profile scheinen diese Kundennummer nicht einfach im HTML preiszugeben; dort sieht man eher eine öffentliche Listing-ID. Liste B kann daher nicht nur ein roher Public-Scrape sein, sondern muss eine interne oder angereicherte Datenquelle sein, die local.ch-Profil und Salesforce-Kunde bereits über eine Kundennummer verbindet. Diese Kundennummer ist dann der saubere Join-Key. Die Liste B wird in der Aufgabenstellung als öffentliche Daten von local.ch beschrieben, aber ganz so öffentlich können/sollten interne Daten wie Kundennummern nicht sein ;)

Es entsteht pro Kunde also ein Analyse-Datensatz:

Kundennummer + Kundenname + Sprache + Produkte + Umsatzsegment + Website-URL + letzter Analysezeitpunkt.

Auf dieser Basis läuft zuerst ein günstiger deterministischer Website-Check. z.B. über normalisierte HTML-/Text-Fingerprints. Nur wenn sich die Website verändert hat, wird die vollständige AI-Website-Potenzialanalyse gestartet. Dadurch werden Crawling- und LLM-Kosten begrenzt, ohne relevante Website-Änderungen zu verpassen. Es müsste geklärt werden, wie man mit den Backlog an existierenden Kunden umgehen sollte, ob alle in einem Rutsch oder über ein paar Wochen. Eher Implementierung und Kostenfrage. Man sollte natürlich auf kleinere Webseitenänderungen nicht überreagieren. Aber das ist auch Implementierungssache.

Falls auf jeden Fall die Kriterien für eine volle Analyse gegeben sind, läuft der Website-Agent auf der WM an.

Ein wichtiger Bestandteil wäre ein versionierter Skill: eine kurze Bewertungsrubrik, die dem Agenten vorgibt, was bei KMU-Websites als "gute digitale Präsenz" gilt. Direktes Einfügen in den Prompt ist hier zu vermeiden, um effizientere Token-Nutzung sicherzustellen.

Dieser Skill ist ein wichtiger einmaliger Setup-Schritt. Er sollte nicht nur technisch geschrieben werden, sondern cross-funktional mit Sales, Marketing, Product und ggf. Web-/SEO-Experten entstehen.

Darin stehen Kriterien wie mobile Nutzbarkeit, klare Leistungsbeschreibung, Kontakt/CTA, lokale Informationen, Trust-Signale, Aktualität, SEO-Basics und ggf. Buchung/Shop. Der Skill sorgt dafür, dass der Agent nicht frei improvisiert, sondern konsistent nach den Kriterien bewertet die localsearch wichtig sind und strukturiertes JSON zurückgibt. 

Zweck: eine gemeinsame Wissensbasis dafür, wie "gut" aussieht und welche Schwächen aus Sales-Sicht relevant und zu bewerten sind. Der Skill wird versioniert gepflegt, ist Agenten-unabhängig und wird vom Agenten bei jeder Website-Analyse als Bewertungsgrundlage genutzt.

Mögliche Varianten für den Agenten:
- Codex App Server
- (abgesicherter) Hermes-Agent auf VM
- LangGraph/LangChain-Kombi

Wichtig: Das LLM sollte austauschbar bleiben. Salesforce-Anbindung, Triggerlogik, Fingerprint-Check, Rubrik/Skill, JSON-Schema, Logging und Writeback liegen in normalem Code.

Guardrails: nur öffentliche Seiten, keine Formulare, keine E-Mails. Agent selber hat keinen Zugriff auf Salesforce. Output nur als validiertes JSON. Zurückschreiben erfolgt über nachgelagerte Tools. Die nachgelagerte Pipeline entscheidet regelbasiert, ob daraus ein Salesforce-Flag, eine Task, eine Kampagnenzuordnung oder eine manuelle Prüfung entsteht.

Die zurückgeschriebene Analyse kann dann vom Sales Manager dazu benutzt werden, um den jeweiligen Kunden anzuschreiben und basierend auf der Analyse Upsell-Möglichkeiten darzulegen 

## Skill und Prompt

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

### Prompt für den Website-Agenten

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
