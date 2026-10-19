import json
from decimal import Decimal
from pathlib import Path

import pytest

import weldcost
from weldcost import (
    ZERO,
    Joint,
    estimate,
    joint_figures,
    main,
    minimum_passes,
    parse_job,
    to_dict,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "bracket.weld"

VALID = """\
labour rate=50
gas cylinder=9000 flow=10 price=100
stock MIG pack_kg=15 price=40
joint J1 fillet leg=6 length=1 passes=1 process=MIG qty=1
"""


def errors_of(text):
    # Line-0 issues (such as "no joint lines") are file-level, not line errors.
    _, issues = parse_job(text)
    return [(i.line, i.message) for i in issues if i.level == "error" and i.line]


def test_fillet_weld_metal_mass_matches_hand_calculation():
    # leg 6 mm -> area 18 mm2; 1 m = 1000 mm -> 18000 mm3 * 7.85e-6 kg/mm3 = 0.14130 kg
    joint = Joint(1, "J1", "fillet", Decimal("6"), ZERO, Decimal("1"), 1, "MIG", 1, ZERO)
    weld, _, _ = joint_figures(joint)
    assert weld == Decimal("0.14130")


def test_groove_uses_given_area_and_quantity():
    joint = Joint(1, "G1", "groove", ZERO, Decimal("24"), Decimal("0.5"), 1, "STICK", 4, ZERO)
    weld, _, _ = joint_figures(joint)
    # 24 mm2 * 500 mm * 7.85e-6 * 4 pieces
    assert weld == Decimal("0.37680")


def test_consumable_uses_process_efficiency():
    joint = Joint(1, "J1", "fillet", Decimal("6"), ZERO, Decimal("1"), 1, "STICK", 1, ZERO)
    weld, consumable, _ = joint_figures(joint)
    assert consumable == weld / Decimal("0.60")


def test_arc_hours_use_deposition_rate():
    joint = Joint(1, "J1", "fillet", Decimal("6"), ZERO, Decimal("1"), 1, "MIG", 1, ZERO)
    weld, _, arc = joint_figures(joint)
    assert arc == weld / Decimal("3.0")


def test_minimum_passes_grows_with_leg_size():
    assert minimum_passes(Decimal("6")) == 1
    assert minimum_passes(Decimal("6.5")) == 2
    assert minimum_passes(Decimal("12")) == 2
    assert minimum_passes(Decimal("13")) == 3


def test_unknown_keyword_reports_its_line_number():
    text = "job name=x\n\nbogus thing=1\n"
    assert errors_of(text) == [(3, "unknown keyword 'bogus'; expected one of job, labour, gas, stock, joint")]


def test_duplicate_joint_id_is_an_error_on_the_second_line():
    text = VALID + "joint J1 fillet leg=6 length=2 process=MIG\n"
    errs = errors_of(text)
    assert (5, "duplicate joint id 'J1'") in errs


def test_second_labour_line_is_rejected():
    errs = errors_of("labour rate=50\nlabour rate=60\n")
    assert errs == [(2, "only one 'labour' line is allowed")]


def test_unknown_option_names_every_offender():
    errs = errors_of("joint J1 fillet leg=6 length=1 process=MIG colour=red size=2\n")
    assert len(errs) == 1
    assert "colour, size" in errs[0][1]


def test_missing_stock_is_reported_once_per_process_at_first_joint():
    text = (
        "gas cylinder=9000 flow=10 price=100\n"
        "joint J1 fillet leg=6 length=1 process=MIG\n"
        "joint J2 fillet leg=6 length=2 process=MIG\n"
    )
    errs = errors_of(text)
    assert errs == [(2, "no 'stock MIG' line for consumable packs")]


def test_shielding_gas_required_but_missing_is_an_error():
    text = "stock MIG pack_kg=15 price=40\njoint J1 fillet leg=6 length=1 process=MIG\n"
    errs = errors_of(text)
    assert (2, "MIG needs shielding gas; add a 'gas' line") in errs


def test_under_passed_fillet_is_a_warning_not_an_error():
    text = VALID.replace("leg=6 length=1 passes=1", "leg=8 length=1 passes=1")
    _, issues = parse_job(text)
    warnings = [i for i in issues if i.level == "warning"]
    assert any("needs at least 2 pass(es), got 1" in i.message for i in warnings)
    assert not errors_of(text)


def test_pack_count_rounds_up_to_whole_packs():
    # 1 m of 6 mm MIG weld at 0.95 efficiency: 0.14130 / 0.95 = 0.14874 kg, far under one 15 kg pack
    job, _ = parse_job(VALID)
    est = estimate(job)
    row = est.rows[0]
    assert row.packs == 1
    assert row.cost == Decimal("40.00")


def test_pack_count_for_a_bigger_order_is_ceiling():
    # 0.14130 kg weld per piece / 0.95 efficiency = 0.14874 kg consumable per piece
    # 100 pieces -> 14.874 kg, fits one 15 kg pack; 102 pieces -> 15.171 kg, needs two
    job, _ = parse_job(VALID.replace("qty=1", "qty=100"))
    assert estimate(job).rows[0].packs == 1
    job, _ = parse_job(VALID.replace("qty=1", "qty=102"))
    assert estimate(job).rows[0].packs == 2


def test_gas_cylinders_follow_flow_times_arc_minutes():
    job, _ = parse_job(VALID)
    est = estimate(job)
    arc_hours = est.rows[0].arc_hours
    expected_litres = Decimal("10") * arc_hours * 60
    assert est.gas_litres == expected_litres
    assert est.gas_cylinders == 1


def test_labour_includes_handling_minutes_and_is_rounded_to_cents():
    text = VALID.replace("qty=1", "qty=2 handling=30")
    job, _ = parse_job(text)
    est = estimate(job)
    # handling: 30 min x 2 pieces = 1 h; arc hours doubled
    assert est.handling_hours == Decimal("1")
    assert est.labour_cost == ((est.arc_hours + Decimal("1")) * Decimal("50")).quantize(Decimal("0.01"))


def test_total_is_sum_of_costs():
    job, _ = parse_job(VALID)
    est = estimate(job)
    assert est.total == est.consumables_cost + est.gas_cost + est.labour_cost


def test_json_report_has_expected_keys():
    job, _ = parse_job(EXAMPLE.read_text(encoding="utf-8"))
    data = to_dict(estimate(job))
    assert set(data) == {"name", "currency", "processes", "arc_hours", "handling_hours", "labour_hours", "gas", "costs"}
    assert data["currency"] == "GBP"
    assert {p["process"] for p in data["processes"]} == {"MIG", "STICK"}
    json.dumps(data)  # must be serialisable


def test_example_file_is_clean():
    job, issues = parse_job(EXAMPLE.read_text(encoding="utf-8"))
    assert [i for i in issues if i.level == "error"] == []
    assert len(job.joints) == 3


def test_cli_gate_fails_when_total_is_over_limit(tmp_path, capsys):
    f = tmp_path / "job.weld"
    f.write_text(VALID, encoding="utf-8")
    assert main(["report", str(f), "--max-cost", "1"]) == weldcost.EXIT_GATE
    assert main(["report", str(f), "--max-cost", "1000"]) == weldcost.EXIT_OK


def test_cli_lint_strict_turns_warnings_into_failure(tmp_path):
    f = tmp_path / "job.weld"
    f.write_text(VALID.replace("leg=6 length=1 passes=1", "leg=8 length=1 passes=1"), encoding="utf-8")
    assert main(["lint", str(f)]) == weldcost.EXIT_OK
    assert main(["lint", str(f), "--strict"]) == weldcost.EXIT_GATE


def test_cli_rejects_a_broken_file_with_exit_2(tmp_path):
    f = tmp_path / "bad.weld"
    f.write_text("joint J1 fillet leg=oops length=1 process=MIG\n", encoding="utf-8")
    assert main(["report", str(f)]) == weldcost.EXIT_INPUT
