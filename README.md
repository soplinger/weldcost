# weldcost

**Offline welding job estimator: weld metal, consumable packs, shielding gas, arc and labour time, and exact cost from one plain-text file.**

weldcost reads a small job file that lists your stock, gas cylinder, labour rate and each weld joint. It works out how much weld metal each joint needs, how many consumable packs and gas cylinders to order, how long the arc and labour take, and what the job costs to the cent. It runs entirely offline with Python 3 standard library only.

## Features

- **Plain-text job files** with `key=value` lines, `#` comments and quoted names.
- **Fillet and groove joints.** Fillets are sized from leg length (cross-section `leg²/2`). Grooves take a cross-section area in mm².
- **Exact weld metal mass** in kg from steel density (7.85 g/cm³), joint length and quantity.
- **Process-aware consumables.** MIG/MAG, FCAW, TIG and STICK each have a deposition efficiency, so consumable mass is weld metal divided by efficiency.
- **Arc time** per process from its deposition rate, so you get arc hours for the whole job.
- **Labour time and cost** from arc time plus per-piece handling minutes, at an hourly rate, rounded half-up to cents.
- **Shielding gas planning.** Gas use is flow rate × arc minutes for gas-shielded processes, converted to whole cylinders and priced.
- **Whole-pack buy list.** Consumables and cylinders are rounded up to whole packs, with leftover (spare) kg shown.
- **Line-numbered lint** for unknown keywords, unknown options, bad numbers, duplicate joint IDs, duplicate singleton lines and missing stock or gas lines.
- **Minimum-pass warnings.** A fillet's leg size sets the minimum number of passes (one per 6 mm, rounded up). Under-passed joints get a warning.
- **Reports in two formats.** A readable table, or JSON for other tools.
- **CI cost gate.** `report --max-cost` exits 1 when the total is over budget. `lint --strict` exits 1 on warnings.
- **Job comparison.** `compare` shows metric-by-metric differences between two job files, for example two design options.
- **Quick calculator.** `quick` gives weld kg, consumable kg, arc hours and gas needs for one joint without writing a file.
- **Process reference table.** `processes` lists the efficiency, deposition rate and gas requirement for each process.

## Quick start

Save this as `job.weld` (or use the included `examples/bracket.weld`):

```text
# Bracket batch: 40 mild-steel brackets plus a few stub-welded gussets
job name="Bracket batch 40" currency=GBP
labour rate=55.00
gas cylinder=9000 flow=14 price=145.00

stock MIG pack_kg=15 price=38.50
stock STICK pack_kg=2.5 price=16.00

joint J1 fillet leg=6 length=1.2 passes=1 process=MIG qty=40 handling=2
joint J2 fillet leg=8 length=0.6 passes=2 process=MIG qty=40 handling=2
joint J3 groove area=24 length=0.3 passes=1 process=STICK qty=10 handling=4
```

Run the report:

```console
$ python3 weldcost.py report examples/bracket.weld
```

It prints a per-process table with weld kg, consumable kg, arc hours, packs and cost, followed by the gas, labour and total lines, all in GBP. Use `--json` for machine-readable output.

Then order the parts:

```console
$ python3 weldcost.py buy examples/bracket.weld
```

## Job file reference

| Line | Required options | Optional options | Notes |
| --- | --- | --- | --- |
| `job` | | `name=`, `currency=` | At most one. Defaults: "Untitled job", USD. |
| `labour` | `rate=` | | At most one. Hourly rate. Missing means zero labour cost (warning). |
| `gas` | `cylinder=`, `flow=`, `price=` | | At most one. Cylinder litres, flow in L/min, price per cylinder. |
| `stock` | `pack_kg=`, `price=` | | One per process, e.g. `stock MIG pack_kg=15 price=38.50`. |
| `joint` | `ID fillet`: `leg=` (mm). `ID groove`: `area=` (mm²). Both: `length=` (m). | `passes=`, `process=`, `qty=`, `handling=` (min per piece) | Process is required. `qty` defaults to 1. |

Processes: `MIG`, `MAG`, `FCAW`, `TIG`, `STICK`. Run `python3 weldcost.py processes` for their constants.

## Commands

```console
python3 weldcost.py lint FILE [--strict]
python3 weldcost.py report FILE [--json] [--max-cost AMOUNT]
python3 weldcost.py buy FILE
python3 weldcost.py compare FILE_A FILE_B
python3 weldcost.py quick --process MIG --leg 6 --length 1.2 --qty 4
python3 weldcost.py processes
```

Exit codes: `0` success, `1` cost gate failed or warnings under `--strict`, `2` unreadable file, bad arguments or errors in the job file.

## How it works

- Fillet cross-section: `leg² / 2` mm². Groove cross-section: the `area=` you give.
- Weld metal (kg): `area_mm² × length_mm × 7.85e-6 kg/mm³ × qty`.
- Consumable (kg): weld metal ÷ process efficiency (MIG 0.95, FCAW 0.85, TIG 0.98, STICK 0.60, MAG 0.95).
- Arc hours: weld metal ÷ process deposition rate (MIG/MAG 3.0 kg/h, FCAW 4.0, TIG 0.8, STICK 1.5).
- Packs and cylinders: rounded up to whole units.
- Labour hours: arc hours + handling minutes ÷ 60.
- All money uses `Decimal` and rounds half-up to cents.

## Testing

```console
$ python3 -m pytest -q
```

The tests cover fillet and groove mass, efficiency and arc-time maths, minimum passes, line-numbered parse errors, cross-line checks (missing stock, missing gas), pack rounding, gas cylinders, labour and totals, JSON output, the cost gate, strict lint and the bundled example. Install pytest yourself; nothing else is needed.

## Limits

- Steel density is fixed at 7.85 g/cm³ (mild steel). Stainless and aluminium would need a different density.
- Fillet cross-section assumes equal legs and a flat face. For a convex or concave bead, enter the measured area with a groove line.
- Deposition rates and efficiencies are typical shop values, not a substitute for your own consumable data sheets. Edit the `PROCESSES` table in `weldcost.py` to match your equipment.

## Project layout

```text
weldcost.py              the tool (single file, stdlib only)
examples/bracket.weld    sample job file
tests/                   pytest suite
```
