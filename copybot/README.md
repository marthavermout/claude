# Copybot – copytrading met nepgeld (paper trading)

Copybot volgt ervaren traders op **Hyperliquid** en doet hun posities na, **met nepgeld**.
Zo zie je eerst of copytrading voor jou werkt, voordat er een euro op het spel staat.

## Waarom Hyperliquid?
Op Hyperliquid (een crypto-exchange) zijn alle posities van alle traders **openbaar en realtime** te zien.
Je hebt geen account, geen API-key en geen geld nodig om iemand te volgen.
Bij Binance of Bybit kan dat niet: daar zie je alleen wat de exchange je laat zien.

## Hoe het werkt (in gewone taal)
1. **Selecteren** – De bot doorzoekt de ranglijst (~48.000 traders) en filtert weg:
   - kleine accounts (geen eigen geld op het spel),
   - "geluksvogels" (één extreme maand, of verlies over de hele periode),
   - market makers en HFT-bots (handelen te snel om na te doen),
   - gokkers met een hefboom van meer dan 5x.
2. **Volgen** – Elke 15 seconden kijkt de bot wat jouw traders open hebben staan.
3. **Kopiëren naar verhouding** – Heeft de trader 20% van zijn geld in BTC long, dan doet jij dat ook,
   maar dan met jouw (nep)bedrag. Volg je meerdere traders, dan krijgt elk een gelijk deel.
4. **Risicoregels gaan altijd vóór de trader:**
   | Regel | Standaard | Betekenis |
   |---|---|---|
   | `max_leverage` | 2x | Nooit meer dan 2x je vermogen in posities |
   | `max_position_pct` | 25% | Nooit meer dan 25% in één munt |
   | `daily_loss_limit_pct` | 5% | 5% verlies vandaag → alles dicht, pauze tot morgen |
   | `max_drawdown_pct` | 20% | 20% onder je hoogste stand → alles dicht, bot stopt |
5. **Nepuitvoering** – Elke trade wordt gesimuleerd tegen de echte marktprijs, inclusief
   kosten (0,045%) en een kleine prijsachterstand (slippage). Resultaten gaan naar `data/`.

Standaard kopieert de bot **alleen nieuwe trades**. Posities die de trader al had toen de bot startte,
slaat hij over (jij zou tegen een slechtere prijs instappen). Dit zet je aan of uit met `copy_existing_positions`.

## Installeren & starten
Vereist: Python 3.11+.

```bash
cd copybot
pip install -r requirements.txt
cp config.example.toml config.toml

python -m copybot select           # 1. kies traders, plak 2-3 adressen in config.toml
python -m copybot run              # 2. start (laat draaien; stoppen met Ctrl+C)
python -m copybot status           # 3. bekijk resultaat (in een tweede venster)
```

De bot moet 24/7 draaien om niets te missen. Zet hem daarom op een goedkope VPS
(bijvoorbeeld Hetzner, ~€4 per maand) met `tmux` of `systemd`. Je laptop dicht = bot uit.

Bestanden in `data/`:
- `trades.csv` – elke (nep)trade
- `equity.csv` – je vermogen per ronde, makkelijk te openen in Excel om een grafiek van te maken
- `state.json` – posities en risicostatus (de bot gaat hier verder na een herstart)

## Gratis 24/7 laten draaien + zelf leren (GitHub Actions)
De repo is openbaar, dus GitHub Actions is gratis en onbeperkt. `.github/workflows/copybot.yml` doet elk uur:

1. **Review (1x per dag)** – `tune` bekijkt alle trades en zoekt fouten:
   - welke trader kost geld? (elke trader heeft een eigen *schaduwboek*: wat als je alleen hem kopieerde)
   - eten de kosten de winst op? draait de bot vaak binnen een uur zijn eigen trade om (churn)?
   - werd de dagverlies-stop te vaak geraakt?
   - doen we het slechter dan gewoon BTC vasthouden? lopen we ver achter op de trader zelf?
2. **Verbeteren** – maximaal **één** wijziging per review, bijvoorbeeld een slechte trader vervangen,
   de hefboom verlagen of de minimale trade-grootte verhogen.
3. **Handelen** – ~50 minuten paper trading, daarna wordt alles opgeslagen op de branch `copybot-data`.

Lees het logboek: branch `copybot-data` → `JOURNAL.md` (nieuwste bovenaan). Daar staat per review
welke fouten er gevonden zijn en wat de bot besloten heeft, en waarom.
Het laatste rapport zie je ook bij elke run onder *Actions → Copybot → Summary*.

**Beschermingen tegen "verbeteren tot je kapot bent" (overfitting):**
| Regel | Waarom |
|---|---|
| Een trader wordt pas beoordeeld na 72 uur én 5 gesloten trades | Te weinig data = toeval |
| Max 1 wijziging per review, daarna 48 uur afkoelen | Anders weet je niet welke wijziging wat deed |
| Risico mag automatisch alleen strenger, nooit losser | Een bot die zichzelf meer hefboom geeft na een goede week is precies hoe accounts opblazen |
| Weggestuurde trader komt op een zwarte lijst | Voorkomt heen-en-weer wisselen tussen dezelfde namen |

Alle drempels staan in `copybot/tune.py` (`TuneRules`). Beslissingen worden bewaard in `overrides.json`
op de data-branch; verwijder dat bestand om opnieuw te beginnen.

**Beperkingen van de gratis variant:**
- GitHub start geplande runs soms 5–30 minuten te laat. In die gaten wordt niet gehandeld (posities blijven open).
- Geplande workflows draaien alleen vanaf de standaard-branch van de repo, dus deze code moet daar gemerged zijn.
- GitHub zet geplande workflows uit na 60 dagen zonder activiteit in de repo. Je krijgt daar een mail over; één klik zet ze weer aan.
- Alles is openbaar (de repo is publiek), dus ook je nep-resultaten.

## Eerlijk advies
- **Draai minimaal 4–8 weken op papier.** Eén goede week zegt niks; dat kan gewoon geluk zijn.
- **Vergelijk met niets doen.** Doet de bot het slechter dan gewoon BTC kopen en vasthouden, dan heeft hij geen zin.
- **Traders van vorige maand ≠ traders van volgende maand.** Draai `select` elke paar weken opnieuw en kijk wie er nog in staat.
- **Funding wordt niet meegerekend.** Op Hyperliquid betaal of ontvang je elk uur funding over open posities.
  Bij lang aanhoudende posities kan dat de uitkomst een paar procent verschuiven.
- **Echt geld zit er bewust nog niet in.** Dat komt pas als de paper-resultaten overtuigen, en dan nog met een klein bedrag.

## Tests
```bash
python -m unittest discover -s tests
```
