#!/usr/bin/env python3
"""weldcost: offline welding consumables, shielding gas, labour and cost estimator.

Reads one plain-text job file (see README.md) and works out weld metal,
consumable packs, shielding-gas cylinders, arc and labour time, and a
cent-exact cost. Standard library only; no network access.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")
CENT = Decimal("0.01")
STEEL_KG_PER_MM3 = Decimal("0.00000785")  # mild steel, 7.85 g/cm3
LEG_PER_PASS_MM = Decimal("6")  # one fillet pass fills about 6 mm of leg

EXIT_OK = 0
EXIT_GATE = 1  # cost gate failed, or warnings under --strict
EXIT_INPUT = 2  # unreadable file, bad arguments or errors in the job file


@dataclass(frozen=True)
class Process:
    code: str
    name: str
    efficiency: Decimal  # share of the consumable that ends up as weld metal
    deposit_kg_h: Decimal  # weld metal laid down while the arc is on
    gas: bool  # needs an external shielding-gas cylinder


PROCESSES = {
    p.code: p
    for p in (
        Process("MIG", "Solid wire, gas shielded", Decimal("0.95"), Decimal("3.0"), True),
        Process("MAG", "Active gas, gas shielded", Decimal("0.95"), Decimal("3.0"), True),
        Process("FCAW", "Flux-cored, self-shielded", Decimal("0.85"), Decimal("4.0"), False),
        Process("TIG", "Filler rod, gas shielded", Decimal("0.98"), Decimal("0.8"), True),
        Process("STICK", "Covered electrode", Decimal("0.60"), Decimal("1.5"), False),
    )
}

JOINT_OPTIONS = {
    "fillet": {"leg", "length", "passes", "process", "qty", "handling"},
    "groove": {"area", "length", "passes", "process", "qty", "handling"},
}
SINGLE_LINES = {"job", "labour", "gas"}


@dataclass
class Issue:
    line: int  # 0 means the problem is not tied to one line
    level: str  # "error" or "warning"
    message: str


class LineError(Exception):
    """A problem with one line of the job file; becomes an error Issue."""


@dataclass
class Gas:
    cylinder_litres: Decimal
    flow_l_min: Decimal
    price: Decimal


@dataclass
class Stock:
    pack_kg: Decimal
    price: Decimal


@dataclass
class Joint:
    line: int
    ident: str
    kind: str  # "fillet" or "groove"
    leg_mm: Decimal  # fillet only
    area_mm2: Decimal  # groove only
    length_m: Decimal
    passes: int
    process: str
    qty: int
    handling_min: Decimal


@dataclass
class Job:
    name: str = "Untitled job"
    currency: str = "USD"
    labour_rate: Decimal | None = None
    gas: Gas | None = None
    stock: dict[str, Stock] = field(default_factory=dict)
    joints: list[Joint] = field(default_factory=list)


class Fields:
    """Typed access to the key=value options on one line."""

    def __init__(self, options: dict[str, str], allowed: set[str]):
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise LineError("unknown option(s): " + ", ".join(unknown))
        self.options = options

    def text(self, key: str, default: str | None = None) -> str:
        if key in self.options:
            return self.options[key]
        if default is None:
            raise LineError(f"missing {key}=")
        return default

    def dec(self, key: str, default: str | None = None, positive: bool = True) -> Decimal:
        raw = self.text(key, default)
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise LineError(f"{key}= must be a number, got {raw!r}") from None
        if not value.is_finite():
            raise LineError(f"{key}= must be a finite number, got {raw!r}")
        if positive and value <= 0:
            raise LineError(f"{key}= must be greater than zero")
        if not positive and value < 0:
            raise LineError(f"{key}= cannot be negative")
        return value

    def whole(self, key: str, default: str | None = None) -> int:
        value = self.dec(key, default)
        if value != value.to_integral_value():
            raise LineError(f"{key}= must be a whole number, got {value}")
        return int(value)


def split_tokens(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """Separate bare words from key=value options."""
    positional: list[str] = []
    options: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            positional.append(token)
            continue
        key, _, value = token.partition("=")
        key = key.lower()
        if not key or not value:
            raise LineError(f"bad option {token!r}; expected key=value")
        if key in options:
            raise LineError(f"option {key}= given twice")
        options[key] = value
    return positional, options


def _process_code(text: str) -> str:
    code = text.upper()
    if code not in PROCESSES:
        raise LineError(f"unknown process {text!r}; known: {', '.join(PROCESSES)}")
    return code


def _no_positional(keyword: str, positional: list[str]) -> None:
    if positional:
        raise LineError(f"'{keyword}' takes only key=value options, not {positional[0]!r}")


def _job(job: Job, positional: list[str], options: dict[str, str], line: int) -> None:
    _no_positional("job", positional)
    f = Fields(options, {"name", "currency"})
    job.name = f.text("name", "Untitled job")
    job.currency = f.text("currency", "USD")


def _labour(job: Job, positional: list[str], options: dict[str, str], line: int) -> None:
    _no_positional("labour", positional)
    f = Fields(options, {"rate"})
    job.labour_rate = f.dec("rate", positive=False)


def _gas(job: Job, positional: list[str], options: dict[str, str], line: int) -> None:
    _no_positional("gas", positional)
    f = Fields(options, {"cylinder", "flow", "price"})
    job.gas = Gas(
        cylinder_litres=f.dec("cylinder"),
        flow_l_min=f.dec("flow"),
        price=f.dec("price", positive=False),
    )


def _stock(job: Job, positional: list[str], options: dict[str, str], line: int) -> None:
    if len(positional) != 1:
        raise LineError("stock needs one process code, e.g. 'stock MIG pack_kg=15 price=38.50'")
    code = _process_code(positional[0])
    if code in job.stock:
        raise LineError(f"second stock line for {code}")
    f = Fields(options, {"pack_kg", "price"})
    job.stock[code] = Stock(pack_kg=f.dec("pack_kg"), price=f.dec("price", positive=False))


def _joint(job: Job, positional: list[str], options: dict[str, str], line: int) -> None:
    if len(positional) != 2:
        raise LineError("joint needs an id and a kind, e.g. 'joint J1 fillet leg=6 length=1.2 process=MIG'")
    ident, kind = positional[0], positional[1].lower()
    if kind not in JOINT_OPTIONS:
        raise LineError(f"unknown joint kind {kind!r}; use fillet or groove")
    if any(j.ident == ident for j in job.joints):
        raise LineError(f"duplicate joint id {ident!r}")
    f = Fields(options, JOINT_OPTIONS[kind])
    is_fillet = kind == "fillet"
    job.joints.append(
        Joint(
            line=line,
            ident=ident,
            kind=kind,
            leg_mm=f.dec("leg") if is_fillet else ZERO,
            area_mm2=ZERO if is_fillet else f.dec("area"),
            length_m=f.dec("length"),
            passes=f.whole("passes", "1"),
            process=_process_code(f.text("process")),
            qty=f.whole("qty", "1"),
            handling_min=f.dec("handling", "0", positive=False),
        )
    )


HANDLERS = {"job": _job, "labour": _labour, "gas": _gas, "stock": _stock, "joint": _joint}


def minimum_passes(leg_mm: Decimal) -> int:
    return max(1, int((leg_mm / LEG_PER_PASS_MM).to_integral_value(rounding=ROUND_CEILING)))


def check_job(job: Job) -> list[Issue]:
    """Cross-line checks that need the whole file."""
    issues: list[Issue] = []
    if not job.joints:
        issues.append(Issue(0, "error", "no joint lines; add at least one 'joint' line"))
    if job.labour_rate is None:
        issues.append(Issue(0, "warning", "no labour line; labour cost is counted as zero"))
    reported: set[str] = set()
    for joint in job.joints:
        proc = PROCESSES[joint.process]
        key = f"stock:{joint.process}"
        if joint.process not in job.stock and key not in reported:
            reported.add(key)
            issues.append(Issue(joint.line, "error", f"no 'stock {joint.process}' line for consumable packs"))
        if proc.gas and job.gas is None and "gas" not in reported:
            reported.add("gas")
            issues.append(Issue(joint.line, "error", f"{joint.process} needs shielding gas; add a 'gas' line"))
        if joint.kind == "fillet":
            need = minimum_passes(joint.leg_mm)
            if joint.passes < need:
                issues.append(
                    Issue(
                        joint.line,
                        "warning",
                        f"joint {joint.ident}: a {joint.leg_mm} mm fillet needs at least {need} pass(es), got {joint.passes}",
                    )
                )
    return issues


def parse_job(text: str) -> tuple[Job, list[Issue]]:
    job = Job()
    issues: list[Issue] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        try:
            tokens = shlex.split(raw, comments=True)
        except ValueError as exc:
            issues.append(Issue(line_no, "error", f"cannot read line ({exc})"))
            continue
        if not tokens:
            continue
        keyword, *rest = tokens
        keyword = keyword.lower()
        handler = HANDLERS.get(keyword)
        if handler is None:
            issues.append(Issue(line_no, "error", f"unknown keyword {keyword!r}; expected one of {', '.join(HANDLERS)}"))
            continue
        if keyword in SINGLE_LINES and keyword in seen:
            issues.append(Issue(line_no, "error", f"only one '{keyword}' line is allowed"))
            continue
        try:
            positional, options = split_tokens(rest)
            handler(job, positional, options, line_no)
        except LineError as exc:
            issues.append(Issue(line_no, "error", str(exc)))
            continue
        seen.add(keyword)
    issues.extend(check_job(job))
    return job, issues


def joint_figures(joint: Joint) -> tuple[Decimal, Decimal, Decimal]:
    """Weld metal kg, consumable kg and arc hours for all pieces of one joint line."""
    proc = PROCESSES[joint.process]
    area = joint.leg_mm**2 / 2 if joint.kind == "fillet" else joint.area_mm2
    weld_kg = area * joint.length_m * 1000 * STEEL_KG_PER_MM3 * joint.qty
    return weld_kg, weld_kg / proc.efficiency, weld_kg / proc.deposit_kg_h


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ceil_div(a: Decimal, b: Decimal) -> int:
    return int((a / b).to_integral_value(rounding=ROUND_CEILING))


def fixed(value: Decimal, places: int) -> str:
    return str(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


@dataclass
class ProcessRow:
    process: str
    welds: int = 0
    weld_kg: Decimal = ZERO
    consumable_kg: Decimal = ZERO
    arc_hours: Decimal = ZERO
    packs: int = 0
    pack_kg: Decimal = ZERO
    cost: Decimal = ZERO


@dataclass
class Estimate:
    name: str
    currency: str
    rows: list[ProcessRow]
    arc_hours: Decimal
    handling_hours: Decimal
    gas_litres: Decimal
    gas_cylinders: int
    gas_cost: Decimal
    consumables_cost: Decimal
    labour_cost: Decimal

    @property
    def weld_kg(self) -> Decimal:
        return sum((r.weld_kg for r in self.rows), ZERO)

    @property
    def labour_hours(self) -> Decimal:
        return self.arc_hours + self.handling_hours

    @property
    def total(self) -> Decimal:
        return self.consumables_cost + self.gas_cost + self.labour_cost


def estimate(job: Job) -> Estimate:
    """Estimate a job that has passed check_job with no errors."""
    rows: dict[str, ProcessRow] = {}
    handling_hours = ZERO
    for joint in job.joints:
        weld, consumable, arc = joint_figures(joint)
        row = rows.setdefault(joint.process, ProcessRow(joint.process))
        row.welds += joint.qty
        row.weld_kg += weld
        row.consumable_kg += consumable
        row.arc_hours += arc
        handling_hours += joint.handling_min * joint.qty / 60
    for row in rows.values():
        stock = job.stock[row.process]
        row.pack_kg = stock.pack_kg
        row.packs = ceil_div(row.consumable_kg, stock.pack_kg)
        row.cost = money(row.packs * stock.price)
    arc_hours = sum((r.arc_hours for r in rows.values()), ZERO)
    gas_arc_hours = sum((r.arc_hours for r in rows.values() if PROCESSES[r.process].gas), ZERO)
    gas_litres = ZERO
    gas_cylinders = 0
    gas_cost = ZERO
    if job.gas and gas_arc_hours:
        gas_litres = job.gas.flow_l_min * gas_arc_hours * 60
        gas_cylinders = ceil_div(gas_litres, job.gas.cylinder_litres)
        gas_cost = money(gas_cylinders * job.gas.price)
    labour_rate = job.labour_rate or ZERO
    return Estimate(
        name=job.name,
        currency=job.currency,
        rows=list(rows.values()),
        arc_hours=arc_hours,
        handling_hours=handling_hours,
        gas_litres=gas_litres,
        gas_cylinders=gas_cylinders,
        gas_cost=gas_cost,
        consumables_cost=sum((r.cost for r in rows.values()), ZERO),
        labour_cost=money((arc_hours + handling_hours) * labour_rate),
    )


def to_dict(est: Estimate) -> dict:
    return {
        "name": est.name,
        "currency": est.currency,
        "processes": [
            {
                "process": r.process,
                "welds": r.welds,
                "weld_kg": fixed(r.weld_kg, 3),
                "consumable_kg": fixed(r.consumable_kg, 3),
                "arc_hours": fixed(r.arc_hours, 2),
                "packs": r.packs,
                "pack_kg": fixed(r.pack_kg, 3),
                "cost": fixed(r.cost, 2),
            }
            for r in est.rows
        ],
        "arc_hours": fixed(est.arc_hours, 2),
        "handling_hours": fixed(est.handling_hours, 2),
        "labour_hours": fixed(est.labour_hours, 2),
        "gas": {
            "litres": fixed(est.gas_litres, 1),
            "cylinders": est.gas_cylinders,
            "cost": fixed(est.gas_cost, 2),
        },
        "costs": {
            "consumables": fixed(est.consumables_cost, 2),
            "gas": fixed(est.gas_cost, 2),
            "labour": fixed(est.labour_cost, 2),
            "total": fixed(est.total, 2),
        },
    }


def render_report(est: Estimate) -> str:
    cur = est.currency
    head = f"{'Process':<7} {'Welds':>5} {'Weld kg':>9} {'Consum kg':>10} {'Arc h':>7} {'Packs':>6} {'Cost':>10}"
    out = [f"{est.name}  ({cur})", "", head, "-" * len(head)]
    for r in est.rows:
        out.append(
            f"{r.process:<7} {r.welds:>5} {fixed(r.weld_kg, 3):>9} {fixed(r.consumable_kg, 3):>10} "
            f"{fixed(r.arc_hours, 2):>7} {r.packs:>6} {fixed(r.cost, 2):>10}"
        )
    out += [
        "",
        f"Arc time       {fixed(est.arc_hours, 2)} h",
        f"Handling       {fixed(est.handling_hours, 2)} h",
        f"Shielding gas  {fixed(est.gas_litres, 1)} L in {est.gas_cylinders} cylinder(s)",
        f"Consumables    {fixed(est.consumables_cost, 2)} {cur}",
        f"Gas            {fixed(est.gas_cost, 2)} {cur}",
        f"Labour         {fixed(est.labour_hours, 2)} h = {fixed(est.labour_cost, 2)} {cur}",
        f"TOTAL          {fixed(est.total, 2)} {cur}",
    ]
    return "\n".join(out)


def render_buy(job: Job, est: Estimate) -> str:
    head = f"{'Process':<7} {'Packs':>6} {'Pack kg':>8} {'Bought kg':>10} {'Needed kg':>10} {'Spare kg':>9}"
    out = [head, "-" * len(head)]
    for r in est.rows:
        bought = r.packs * r.pack_kg
        out.append(
            f"{r.process:<7} {r.packs:>6} {fixed(r.pack_kg, 3):>8} {fixed(bought, 3):>10} "
            f"{fixed(r.consumable_kg, 3):>10} {fixed(bought - r.consumable_kg, 3):>9}"
        )
    if est.gas_cylinders and job.gas:
        out.append(
            f"Shielding gas: {est.gas_cylinders} cylinder(s) of {fixed(job.gas.cylinder_litres, 0)} L "
            f"({fixed(est.gas_litres, 1)} L needed)"
        )
    return "\n".join(out)


def metrics(est: Estimate) -> list[tuple[str, Decimal, int]]:
    return [
        ("Weld metal kg", est.weld_kg, 3),
        ("Arc hours", est.arc_hours, 2),
        ("Labour hours", est.labour_hours, 2),
        ("Consumables", est.consumables_cost, 2),
        ("Shielding gas", est.gas_cost, 2),
        ("Labour", est.labour_cost, 2),
        ("Total", est.total, 2),
    ]


def render_processes() -> str:
    head = f"{'Code':<7} {'Efficiency':>10} {'Deposit kg/h':>13} {'Gas':>5}  Name"
    out = [head, "-" * len(head)]
    for p in PROCESSES.values():
        out.append(f"{p.code:<7} {fixed(p.efficiency, 2):>10} {fixed(p.deposit_kg_h, 1):>13} {'yes' if p.gas else 'no':>5}  {p.name}")
    return "\n".join(out)


def format_issue(path: str, issue: Issue) -> str:
    where = f"{path}:{issue.line}" if issue.line else path
    return f"{where}: {issue.level}: {issue.message}"


def load(path: str) -> tuple[Job | None, list[Issue]]:
    """Read and parse a job file. Returns None for the job when it has errors."""
    with open(path, encoding="utf-8") as handle:
        job, issues = parse_job(handle.read())
    for issue in issues:
        print(format_issue(path, issue), file=sys.stderr)
    has_errors = any(i.level == "error" for i in issues)
    return (None if has_errors else job), issues


def cmd_lint(args: argparse.Namespace) -> int:
    job, issues = load(args.file)
    errors = sum(1 for i in issues if i.level == "error")
    warnings = len(issues) - errors
    if job is None:
        print(f"{errors} error(s), {warnings} warning(s)")
        return EXIT_INPUT
    print(f"OK: {len(job.joints)} joint line(s), {warnings} warning(s)")
    return EXIT_GATE if warnings and args.strict else EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    job, _ = load(args.file)
    if job is None:
        return EXIT_INPUT
    est = estimate(job)
    print(json.dumps(to_dict(est), indent=2) if args.json else render_report(est))
    if args.max_cost is not None and est.total > args.max_cost:
        print(f"GATE FAILED: total {fixed(est.total, 2)} is over the limit {fixed(args.max_cost, 2)}", file=sys.stderr)
        return EXIT_GATE
    return EXIT_OK


def cmd_buy(args: argparse.Namespace) -> int:
    job, _ = load(args.file)
    if job is None:
        return EXIT_INPUT
    print(render_buy(job, estimate(job)))
    return EXIT_OK


def cmd_compare(args: argparse.Namespace) -> int:
    job_a, _ = load(args.a)
    job_b, _ = load(args.b)
    if job_a is None or job_b is None:
        return EXIT_INPUT
    head = f"{'Metric':<15} {'A':>12} {'B':>12} {'B - A':>12}"
    out = [head, "-" * len(head)]
    for (label, a, places), (_, b, _) in zip(metrics(estimate(job_a)), metrics(estimate(job_b))):
        out.append(f"{label:<15} {fixed(a, places):>12} {fixed(b, places):>12} {fixed(b - a, places):>12}")
    print("\n".join(out))
    return EXIT_OK


def cmd_quick(args: argparse.Namespace) -> int:
    try:
        code = _process_code(args.process)
    except LineError as exc:
        print(f"weldcost: {exc}", file=sys.stderr)
        return EXIT_INPUT
    if (args.leg is None) == (args.area is None):
        print("weldcost: give exactly one of --leg (fillet) or --area (groove)", file=sys.stderr)
        return EXIT_INPUT
    size = args.leg if args.leg is not None else args.area
    if size <= 0 or args.length <= 0 or args.qty < 1:
        print("weldcost: size, length and qty must be greater than zero", file=sys.stderr)
        return EXIT_INPUT
    joint = Joint(
        line=0,
        ident="quick",
        kind="fillet" if args.leg is not None else "groove",
        leg_mm=args.leg or ZERO,
        area_mm2=args.area or ZERO,
        length_m=args.length,
        passes=1,
        process=code,
        qty=args.qty,
        handling_min=ZERO,
    )
    weld, consumable, arc = joint_figures(joint)
    proc = PROCESSES[code]
    print(f"Weld metal   {fixed(weld, 3)} kg")
    print(f"Consumable   {fixed(consumable, 3)} kg  ({proc.code} efficiency {proc.efficiency})")
    print(f"Arc time     {fixed(arc, 2)} h  ({proc.deposit_kg_h} kg/h deposit)")
    if proc.gas:
        print(f"Shielding gas needed: flow rate (L/min) x {fixed(arc * 60, 1)} arc minutes")
    return EXIT_OK


def cmd_processes(args: argparse.Namespace) -> int:
    print(render_processes())
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weldcost", description="Offline welding consumables and cost estimator.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lint", help="check a job file; line-numbered errors and warnings")
    p.add_argument("file")
    p.add_argument("--strict", action="store_true", help="exit 1 when there are warnings")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("report", help="full estimate: weld metal, packs, gas, labour and cost")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument("--max-cost", type=Decimal, help="CI gate: exit 1 when the total is over this amount")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("buy", help="consumable packs and gas cylinders to order")
    p.add_argument("file")
    p.set_defaults(func=cmd_buy)

    p = sub.add_parser("compare", help="compare two job files metric by metric")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("quick", help="one-off joint figures without a job file")
    p.add_argument("--process", required=True)
    p.add_argument("--leg", type=Decimal, help="fillet leg in mm")
    p.add_argument("--area", type=Decimal, help="groove cross-section in mm2")
    p.add_argument("--length", type=Decimal, required=True, help="length in metres")
    p.add_argument("--qty", type=int, default=1)
    p.set_defaults(func=cmd_quick)

    p = sub.add_parser("processes", help="list the welding processes and their constants")
    p.set_defaults(func=cmd_processes)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        print(f"weldcost: {exc}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
