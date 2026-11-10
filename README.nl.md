# q2-spoolbridge (Nederlands)

**Spoolman-integratie voor de QIDI Q2 met QIDI Box die werkt met de nieuwe firmware (01.01.03.x).**

spoolbridge zet automatisch de juiste spoel actief in Moonraker, op basis van het slot dat de QIDI Box geladen heeft. Je koppelt per box-slot een spoel in Fluidd, zoals vroeger. Moonraker meet het verbruik zoals altijd, en spoolbridge boekt wat Moonraker rond een toolwissel fout doet (purges, het terugtrekken naar de box) op de juiste spoel.

> **Status: beta.** Getest op een Q2 met één QIDI Box, firmware 01.01.03.x, in meerkleurenprints: de actieve spoel volgt elke wissel. De verbruikscorrectie is tot nu toe op één print vergeleken met de slicerwaarden per filament. Meld je ervaring in de issues.

## Waarom

Sinds de recente QIDI-firmware loopt elke toolwissel via QIDI's `T_MANAGER`. De gangbare aanpak overschrijft `T0`–`T3` met eigen macro's die `EXTRUDER_LOAD` rechtstreeks aanroepen. Dat slaat QIDI's administratie over, waardoor **de box uit sync raakt** met de printer.

spoolbridge raakt de toolwissel niet aan. Het **kijkt alleen** naar `multi_color_controller.box.loaded_slot` via de Moonraker-API en zet de actieve spoel. Het overschrijft geen QIDI-macro's en bedient de box niet. De enige G-code die het stuurt is `SAVE_VARIABLE` voor zijn eigen `q2_*__spool_id`-variabelen (zie hieronder). Het volgt ook auto-refill, omdat het naar het geladen slot kijkt en niet naar het T-nummer.

## Installeren (als mks)

`qidi` heeft geen sudo, dus je installeert als `mks`. `/home/mks` is op de Q2 een snelkoppeling naar `/home/qidi`, waar `mks` niet mag schrijven. Upload daarom naar `/tmp`. Op je pc, in de map boven `q2-spoolbridge` (vervang `<printer>` door de hostnaam of het IP-adres):

```bash
scp -r q2-spoolbridge mks@<printer>:/tmp/
```

Daarna op de printer, als `mks`:

```bash
cd /tmp/q2-spoolbridge && bash install.sh
```

Doe daarna in Fluidd een **RESTART** terwijl er niets print.

Het script:
- bewaart een volledige kopie in `/opt/spoolbridge`;
- zet de include vóór het `SAVE_CONFIG`-blok, met eerst een backup (`printer.cfg.spoolbridge.bak`);
- behoudt de eigenaar en de rechten van je configbestanden;
- doet een proefdraai voordat het de dienst start.

Je kunt het gerust opnieuw uitvoeren.

**Na een QIDI-update:** `bash /opt/spoolbridge/install.sh`.
**Na een volledige herflash:** opnieuw uploaden vanaf je pc.

## Slot koppelen aan een spoel

1. Open in Fluidd de Spoolman-kaart. Elke slotmacro (`Q2_SLOT1` … `Q2_SLOT4`, `Q2_EXTERN`) krijgt een spoelkiezer.
2. Kies per slot de spoel. spoolbridge bewaart dat in `saved_variables.cfg`, dus het overleeft een herstart.
3. Laadt de box `slot0`, dan wordt de spoel van `Q2_SLOT1` actief, enzovoort.

| Box-slot | Macro | Op het scherm |
|---|---|---|
| slot0–3 | Q2_SLOT1–4 | slot 1–4 |
| slot4–7 | Q2_BOX2_SLOT1–4 | tweede box (in `spoolbridge.cfg` uit commentaar halen) |
| slot16 | Q2_EXTERN | externe houder |

## Gedrag

- QIDI meldt `slot16` ook als de toolhead leeg is. spoolbridge telt `slot16` daarom alleen als geladen als de filamentsensor in de toolhead filament ziet.
- De box laadt filament pas in de toolhead bij de printstart of een wissel. Op dat moment wisselt de actieve spoel.
- Is het geladen slot niet gekoppeld, dan maakt spoolbridge de actieve spoel **leeg**.
- Het slot is leidend: een handmatig gekozen spoel zet spoolbridge binnen een seconde terug.
- **Fluidd 1.30.x-bug:** de Fluidd die QIDI meelevert slaat een spoelkeuze op als `Q2_SLOT2__SPOOL_ID` (hoofdletters). Klipper weigert dat, dus na een herstart is de keuze weg ([upstream opgelost in fluidd#1563](https://github.com/fluidd-core/fluidd/pull/1563)). spoolbridge vangt dat op zonder Fluidd te patchen: verandert een slotmacro, dan slaat het zelf `SAVE_VARIABLE VARIABLE=q2_slot2__spool_id VALUE=<id>` op, maar alleen als de printer niet print of gepauzeerd is. Met een gerepareerde Fluidd ziet het dat de waarde al bewaard is en doet het niets.
- **Purges en toolwissels worden correct geboekt.** Moonraker boekt alleen extrusie als de extruder boven zijn hoogste positie ooit komt. Een toolwissel op de Q2 trekt eerst ~160 mm terug naar de box, waardoor Moonraker de purge en het begin van de volgende kleur op *geen* spoel boekt, en de vorige spoel laat betalen voor filament dat terug in de box ging. spoolbridge houdt per spoel zelf bij hoeveel er verbruikt is. Een spoel betaalt voor alles wat voorbij de cutter ging terwijl hij geladen was: wat hij printte en purgede, plus het stukje dat bij zijn eigen cut in de hotend achterblijft. Dat de volgende kleur dat stukje uitduwt, kost de volgende spoel niets. In extruderwaarden: vanaf het moment dat de nieuwe tip voorbij de cutter is (toolhead-sensor + `SPOOLBRIDGE_LOAD_OFFSET`, standaard 67 mm, gemeten tegen de slicerwaarden) tot de cut (hoogste extruderpositie vóór het terugtrekken). Bij de wissel boekt het het verschil met Moonraker op die spoel in Spoolman (negatief = filament terug). De log toont elke correctie, bv. `spool 7: used 60.7 mm, Moonraker booked 0.0 mm, corrected +60.7 mm`. Uitzetten met `SPOOLBRIDGE_USAGE_CORRECTION=0`.
- Een slot leegmaken in Fluidd maakt het echt leeg: spoolbridge valt niet terug op de oude bewaarde waarde.
- Logs: `sudo journalctl -u spoolbridge -f`
- Verwijderen: `bash /opt/spoolbridge/install.sh --uninstall`

Niet gelieerd aan QIDI. Gebruik op eigen risico. Licentie: GPL-3.0-or-later.

---

*Ontwikkeld met behulp van [Claude](https://claude.ai) (Anthropic), een AI-assistent. Alle code is nagekeken en getest op een echte Q2.*
