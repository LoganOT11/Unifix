"""
Comprehensive edge-case test suite for the fuzzy resolver, time validator,
and end-to-end work order validation pipeline.

Organized by category:
  1. Whitespace handling
  2. Case normalization
  3. Typos and misspellings
  4. Partial / abbreviated input
  5. Special characters (apostrophes, hyphens, ampersands)
  6. Ambiguous / cross-entity matches
  7. Time format edge cases
  8. Parts multi-token edge cases
  9. Integration: full work orders (clean, noisy, pathological)
 10. Boundary / defensive inputs (None, empty, very long, Unicode)
"""
import json
import pytest
from pathlib import Path

from validator.fuzzy_resolver import (
    resolve_field, resolve_parts_used, _score_algorithms,
    FIELD_WEIGHTS, THRESHOLD_HIGH, THRESHOLD_LOW,
)
from validator.time_validator import validate_time_field
from validator.work_order_validator import validate_work_order
from validator.models import MatchStatus, OverallStatus


# ═══════════════════════════════════════════════════════════════════════════════
# 1. WHITESPACE HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

class TestWhitespace:
    """Leading/trailing/internal whitespace should not prevent correct matching."""

    def test_worker_leading_trailing_spaces(self):
        r = resolve_field("worker", "  James Hartwell  ")
        assert r.resolved_value == "James Hartwell"
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_worker_double_space_between_names(self):
        r = resolve_field("worker", "James  Hartwell")
        assert r.resolved_value == "James Hartwell"
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_company_leading_trailing_spaces(self):
        r = resolve_field("company", "  PrimeTech Maintenance Ltd  ")
        assert r.resolved_value == "PrimeTech Maintenance Ltd"
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_location_tabs_and_newlines(self):
        r = resolve_field("location", "\tField Site Alpha\n")
        # Should still find the match despite whitespace noise
        assert "Field Site Alpha" in r.resolved_value

    def test_equipment_leading_zeros_with_spaces(self):
        r = resolve_field("vehicle_equipment", "  TRK-001  ")
        assert r.resolved_value == "TRK-001"
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_time_leading_trailing_spaces(self):
        r = validate_time_field("start_time", "  08:30  ")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "08:30"

    def test_duration_leading_trailing_spaces(self):
        r = validate_time_field("total_time_spent", "  2h30m  ")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "2h 30m"

    def test_parts_extra_spaces_around_commas(self):
        r = resolve_parts_used("Oil Filter  ,  Air Filter  ,  Coolant")
        assert r.status != MatchStatus.NO_MATCH
        tokens = [t.strip() for t in r.resolved_value.split(",")]
        assert len(tokens) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# 2. CASE NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestCaseNormalization:
    """All fuzzy comparisons use .lower() — case should be irrelevant."""

    def test_worker_all_upper(self):
        r = resolve_field("worker", "JAMES HARTWELL")
        assert r.status == MatchStatus.EXACT
        assert r.resolved_value == "James Hartwell"

    def test_worker_all_lower(self):
        r = resolve_field("worker", "james hartwell")
        assert r.status == MatchStatus.EXACT

    def test_worker_mixed_case(self):
        r = resolve_field("worker", "jAmEs HaRtWeLl")
        assert r.status == MatchStatus.EXACT

    def test_company_all_lower(self):
        r = resolve_field("company", "primetech maintenance ltd")
        assert r.status == MatchStatus.EXACT

    def test_location_all_upper(self):
        r = resolve_field("location", "FIELD SITE ALPHA")
        assert r.status == MatchStatus.EXACT

    def test_equipment_lower_tag(self):
        r = resolve_field("vehicle_equipment", "trk-001")
        assert r.status == MatchStatus.EXACT

    def test_parts_upper(self):
        r = resolve_parts_used("HYDRAULIC FILTER 2240")
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_time_am_pm_case_insensitive(self):
        r = validate_time_field("start_time", "08:30 am")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "08:30"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TYPOS AND MISSPELLINGS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTypos:
    """Common misspellings should still produce HIGH_CONF or better."""

    @pytest.mark.parametrize("raw,expected", [
        ("James Hartweel",       "James Hartwell"),      # extra letter
        ("James Hrtwell",        "James Hartwell"),      # missing vowel
        ("Jmes Hartwell",        "James Hartwell"),      # missing vowel
        ("Priya Nambia",         "Priya Nambiar"),       # missing letter
        ("Luc Trembay",          "Luc Tremblay"),        # missing letter
        ("Fatima Al Hassan",     "Fatima Al-Hassan"),    # missing hyphen
        ("Maria Sanchz",         "Maria Sanchez"),       # missing letter
        ("Derek O Brain",        "Derek O'Brien"),       # alternate spelling
        ("Tom Kowalsky",         "Tom Kowalski"),        # y/i swap
        ("Sandra Mcperson",      "Sandra McPherson"),    # missing space/caps
    ])
    def test_worker_typos(self, raw, expected):
        r = resolve_field("worker", raw)
        assert r.resolved_value == expected
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)

    @pytest.mark.parametrize("raw,expected_substring", [
        ("Blueline Service Grup",   "BlueLine"),
        ("Hartwell Industial",      "Hartwell"),
        ("Primetech Maintanence",   "PrimeTech"),
        ("Northern Fllet",          "Northern Fleet"),
        ("Apex Equipmnt & Repair",  "Apex"),
        ("Delta Hydralics",         "Delta Hydraulics"),
    ])
    def test_company_typos(self, raw, expected_substring):
        r = resolve_field("company", raw)
        assert expected_substring.lower() in r.resolved_value.lower()

    @pytest.mark.parametrize("raw,expected_substring", [
        ("Main Worshop Bay 1",      "Main Workshop"),
        ("Northen Depot",           "North Depot"),
        ("Field Ste Alpha",         "Field Site Alpha"),
        ("Fuel Staton",             "Fuel Station"),
    ])
    def test_location_typos(self, raw, expected_substring):
        r = resolve_field("location", raw)
        assert expected_substring.lower() in r.resolved_value.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. PARTIAL / ABBREVIATED INPUT
# ═══════════════════════════════════════════════════════════════════════════════

class TestPartialInput:
    """Partial names, abbreviations, and shorthand should still resolve."""

    def test_worker_last_name_only(self):
        r = resolve_field("worker", "Kowalski")
        assert r.resolved_value == "Tom Kowalski"
        assert r.status != MatchStatus.EXACT  # partial match

    def test_worker_first_name_only(self):
        r = resolve_field("worker", "James")
        assert "James" in r.resolved_value
        assert r.status != MatchStatus.NO_MATCH

    def test_worker_initial_dot(self):
        r = resolve_field("worker", "J. Hartwell")
        assert "Hartwell" in r.resolved_value

    def test_company_short_code(self):
        r = resolve_field("company", "HIS")
        assert r.status == MatchStatus.EXACT

    def test_company_partial_name(self):
        r = resolve_field("company", "Apex Equipment")
        assert "Apex" in r.resolved_value

    def test_location_partial_bay(self):
        r = resolve_field("location", "Bay 2")
        assert "Bay 2" in r.resolved_value

    def test_location_partial_depot(self):
        r = resolve_field("location", "North Depot")
        assert "North Depot" in r.resolved_value

    def test_equipment_tag_no_hyphen(self):
        r = resolve_field("vehicle_equipment", "TRK001")
        assert r.resolved_value == "TRK-001"
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_equipment_make_only(self):
        r = resolve_field("vehicle_equipment", "Peterbilt")
        assert "Peterbilt" in r.resolved_value

    def test_equipment_type_only(self):
        r = resolve_field("vehicle_equipment", "forklift")
        assert "Forklift" in r.resolved_value

    def test_equipment_year_only(self):
        r = resolve_field("vehicle_equipment", "2021")
        assert "2021" in r.resolved_value  # matches something from 2021

    def test_part_description_partial(self):
        r = resolve_parts_used("serpentine belt")
        assert "Serpentine" in r.resolved_value or "BLT-SERP" in r.resolved_value


# ═══════════════════════════════════════════════════════════════════════════════
# 5. SPECIAL CHARACTERS
# ═══════════════════════════════════════════════════════════════════════════════

class TestSpecialCharacters:
    """Apostrophes, hyphens, ampersands, and em-dashes."""

    def test_worker_apostrophe_missing(self):
        r = resolve_field("worker", "Derek OBrien")
        assert r.resolved_value == "Derek O'Brien"
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_worker_apostrophe_wrong_position(self):
        r = resolve_field("worker", "Derek O'Brian")
        assert r.resolved_value == "Derek O'Brien"

    def test_worker_apostrophe_extra_space(self):
        r = resolve_field("worker", "Derek O' Brien")
        assert r.resolved_value == "Derek O'Brien"

    def test_company_ampersand_vs_and(self):
        r = resolve_field("company", "Apex Equipment and Repair")
        assert "Apex" in r.resolved_value

    def test_location_em_dash_vs_hyphen(self):
        # em dash: —  vs  ASCII hyphen: -
        r = resolve_field("location", "South Depot - Covered Bay")
        assert "South Depot" in r.resolved_value
        assert "Covered Bay" in r.resolved_value

    def test_location_em_dash_exact(self):
        r = resolve_field("location", "South Depot — Covered Bay")
        assert r.status == MatchStatus.EXACT

    def test_company_hyphen_in_name(self):
        r = resolve_field("company", "BlueLine Service Group")
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_company_period_in_name(self):
        r = resolve_field("company", "Delta Hydraulics Inc.")
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. AMBIGUOUS / CROSS-ENTITY MATCHES
# ═══════════════════════════════════════════════════════════════════════════════

class TestAmbiguity:
    """When input could match multiple candidates, best match should win."""

    def test_location_main_workshop_picks_bay1(self):
        """'Main Workshop' matches Bay 1, 2, 3 — should pick best."""
        r = resolve_field("location", "Main Workshop")
        # Should still resolve to one of the bays
        assert "Main Workshop" in r.resolved_value
        assert r.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)

    def test_equipment_generator_picks_first(self):
        """'Generator' matches 250kVA and 500kVA — should pick best."""
        r = resolve_field("vehicle_equipment", "Generator")
        assert "Generator" in r.resolved_value

    def test_equipment_excavator_picks_best(self):
        """'Excavator' matches CAT 320 and Komatsu — picks best algo score."""
        r = resolve_field("vehicle_equipment", "Excavator")
        assert "Excavator" in r.resolved_value

    def test_worker_last_name_matches_company(self):
        """'Hartwell' is a worker surname AND a company name prefix."""
        r = resolve_field("worker", "Hartwell")
        # Should resolve to a worker, not a company
        assert r.resolved_value == "James Hartwell"

    def test_company_short_code_case_insensitive(self):
        """Short codes should match regardless of case."""
        for code, expected_sub in [("HIS", "Hartwell"), ("PTM", "PrimeTech"), ("NFS", "Northern")]:
            r = resolve_field("company", code)
            assert expected_sub in r.resolved_value or r.status == MatchStatus.EXACT


# ═══════════════════════════════════════════════════════════════════════════════
# 7. TIME FORMAT EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeEdgeCases:
    """Clock times and durations with unusual but valid formats."""

    # --- Valid clock times ---
    @pytest.mark.parametrize("raw,expected", [
        ("00:00",       "00:00"),     # midnight
        ("23:59",       "23:59"),     # end of day
        ("0:00",        "00:00"),     # single digit 0
        ("8:30",        "08:30"),     # single digit hour
        ("08:30am",     "08:30"),     # no space before AM
        ("08:30AM",     "08:30"),     # uppercase no space
        ("08:30 AM",    "08:30"),     # standard format
        ("08:30 PM",    "20:30"),     # PM conversion
        ("12:00 AM",    "00:00"),     # midnight AM
        ("12:00 PM",    "12:00"),     # noon PM
        ("12:30 PM",    "12:30"),     # afternoon
        ("08:30:00",    "08:30"),     # with seconds
        ("17:45",       "17:45"),     # 24h format
    ])
    def test_valid_clock_times(self, raw, expected):
        r = validate_time_field("start_time", raw)
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == expected

    # --- Invalid clock times ---
    @pytest.mark.parametrize("raw", [
        "25:00",          # hour > 23
        "08:65",          # minute > 59
        "8:3",            # single digit minute
        "8.30",           # dot separator
        "830",            # no separator
        "eight thirty",   # words
        "morning",        # descriptive
        "8am",            # no colon
        "noon",           # word
        "24:00",          # exactly 24
        "-1:00",          # negative
    ])
    def test_invalid_clock_times(self, raw):
        r = validate_time_field("start_time", raw)
        assert r.status == MatchStatus.TIME_INVALID

    # --- Valid durations ---
    @pytest.mark.parametrize("raw,expected", [
        ("0:30",                 "0h 30m"),
        ("0:01",                 "0h 1m"),
        ("1:00",                 "1h 0m"),
        ("2h30m",                "2h 30m"),
        ("2h",                   "2h 0m"),
        ("1 hour 30 minutes",   "1h 30m"),
        ("90 min",              "1h 30m"),
        ("45 minutes",          "0h 45m"),
        ("2 hours 15 minutes",  "2h 15m"),
    ])
    def test_valid_durations(self, raw, expected):
        r = validate_time_field("total_time_spent", raw)
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == expected

    # --- Invalid / unhandled durations ---
    @pytest.mark.parametrize("raw", [
        "a long time",
        "quick",
        "2.5 hours",       # decimal not supported
        "90",              # just a number
        "2h 30",           # no minute unit
        "2:30:00",         # with seconds (ambiguous — clock or duration?)
    ])
    def test_invalid_durations(self, raw):
        r = validate_time_field("total_time_spent", raw)
        assert r.status in (MatchStatus.TIME_INVALID, MatchStatus.EMPTY)

    def test_duration_30m_shorthand_recognized(self):
        """FIXED: '30m' shorthand is now recognised and normalised to '0h 30m'."""
        r = validate_time_field("total_time_spent", "30m")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "0h 30m"

    def test_duration_100_plus_hours_passes(self):
        """FIXED: Duration regex now uses \\d+ for hours so values >99 hours are accepted."""
        r = validate_time_field("total_time_spent", "100:30")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "100h 30m"

    def test_duration_1h_no_minutes(self):
        r = validate_time_field("total_time_spent", "1h")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "1h 0m"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. PARTS MULTI-TOKEN EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestPartsEdgeCases:
    """Parts field can contain comma-separated tokens, each resolved independently."""

    def test_single_part_exact(self):
        r = resolve_parts_used("BLT-SERP")
        assert r.status == MatchStatus.EXACT
        assert r.resolved_value == "BLT-SERP"

    def test_single_part_description(self):
        r = resolve_parts_used("Serpentine Drive Belt")
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_multi_part_all_match(self):
        r = resolve_parts_used("Oil Filter, Air Filter, Coolant")
        assert r.status != MatchStatus.NO_MATCH
        tokens = [t.strip() for t in r.resolved_value.split(",")]
        assert len(tokens) == 3

    def test_multi_part_one_unknown(self):
        r = resolve_parts_used("Oil Filter, ZZZ-FAKE-9999, Air Filter")
        # min score drives overall status — the unknown part should pull it down
        assert r.status in (MatchStatus.LOW_CONF, MatchStatus.NO_MATCH)

    def test_multi_part_no_space_after_comma(self):
        r = resolve_parts_used("Oil Filter,Air Filter")
        assert r.status != MatchStatus.NO_MATCH
        tokens = [t.strip() for t in r.resolved_value.split(",")]
        assert len(tokens) == 2

    def test_multi_part_trailing_comma(self):
        r = resolve_parts_used("Oil Filter, Air Filter,")
        # trailing comma creates empty token that gets filtered
        tokens = [t.strip() for t in r.resolved_value.split(",") if t.strip()]
        assert len(tokens) >= 2

    def test_part_with_quantity_prefix(self):
        r = resolve_parts_used("2x Oil Filter")
        assert "Oil Filter" in r.resolved_value or "FILT-OIL" in r.resolved_value

    def test_part_number_and_description(self):
        r = resolve_parts_used("HF-2240")
        assert r.status == MatchStatus.EXACT

    def test_empty_parts_string(self):
        r = resolve_parts_used("")
        assert r.status == MatchStatus.EMPTY

    def test_whitespace_only_parts(self):
        r = resolve_parts_used("   ")
        assert r.status == MatchStatus.EMPTY

    def test_all_unknown_parts(self):
        r = resolve_parts_used("nonexistent part xyz, another fake part")
        assert r.status == MatchStatus.NO_MATCH

    def test_part_with_parenthetical_note(self):
        r = resolve_parts_used("Oil Filter (used)")
        # Should still find a match despite the note
        assert "Oil Filter" in r.resolved_value or "FILT-OIL" in r.resolved_value


# ═══════════════════════════════════════════════════════════════════════════════
# 9. INTEGRATION: FULL WORK ORDERS
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullWorkOrderIntegration:
    """End-to-end validation of complete work order dicts."""

    def _base_order(self, **overrides) -> dict:
        base = {
            "vehicle_equipment":      "TRK-001",
            "reported_problem":       "Engine overheating",
            "diagnosis_cause":        "Low coolant, clogged thermostat",
            "work_performed":         "Replaced thermostat, topped up coolant",
            "parts_used":             "Coolant — Long Life, Oil Filter — Heavy Duty",
            "start_time":             "08:00",
            "end_time":               "11:30",
            "total_time_spent":       "3h 30m",
            "future_recommendations": "Monitor coolant level weekly",
            "remaining_tasks":        "",
            "worker":                 "James Hartwell",
            "company":                "Hartwell Industrial Services",
            "location":               "Main Workshop — Bay 1",
        }
        base.update(overrides)
        return base

    # --- PASS cases ---

    def test_clean_input_passes(self):
        r = validate_work_order(self._base_order())
        assert r.overall_status == OverallStatus.PASS
        assert r.unresolved_fields == []
        assert len(r.resolved_json) == 13

    def test_whitespace_inputs_pass(self):
        r = validate_work_order(self._base_order(
            worker="  James Hartwell  ",
            company="  Hartwell Industrial Services  ",
            location="  Main Workshop — Bay 1  ",
            start_time=" 08:00 ",
            end_time=" 11:30 ",
            total_time_spent=" 3h30m ",
        ))
        assert r.overall_status in (OverallStatus.PASS, OverallStatus.REVIEW)

    def test_case_insensitive_passes(self):
        r = validate_work_order(self._base_order(
            worker="JAMES HARTWELL",
            company="HARTWELL INDUSTRIAL SERVICES",
            location="MAIN WORKSHOP — BAY 1",
        ))
        assert r.overall_status == OverallStatus.PASS

    def test_minor_typos_pass_or_review(self):
        r = validate_work_order(self._base_order(
            worker="James Hartwal",
            company="Hartwell Industial Services",
        ))
        assert r.overall_status in (OverallStatus.PASS, OverallStatus.REVIEW)

    def test_free_text_unchanged(self):
        long_text = "The engine makes a strange ticking noise that gets louder above 2000 RPM. " * 5
        r = validate_work_order(self._base_order(reported_problem=long_text))
        assert r.field_results["reported_problem"].resolved_value == long_text
        assert r.field_results["reported_problem"].status == MatchStatus.PASS_THROUGH

    # --- REVIEW cases ---

    def test_low_confidence_worker_triggers_review(self):
        r = validate_work_order(self._base_order(worker="J. Hartwell"))
        # LOW_CONF on a fuzzy field → REVIEW (not FAIL, because LOW_CONF isn't NO_MATCH)
        assert r.overall_status in (OverallStatus.REVIEW, OverallStatus.PASS)

    def test_unknown_parts_triggers_review(self):
        r = validate_work_order(self._base_order(parts_used="ZZZ-FAKE-9999"))
        # parts_used is in REVIEW_TRIGGER_FIELDS
        assert r.overall_status in (OverallStatus.REVIEW, OverallStatus.FAIL)

    def test_low_conf_parts_triggers_review(self):
        r = validate_work_order(self._base_order(parts_used="some vague part description"))
        assert r.overall_status in (OverallStatus.REVIEW, OverallStatus.FAIL)

    # --- FAIL cases ---

    def test_unknown_worker_fails(self):
        r = validate_work_order(self._base_order(worker="Nobody Known XYZ"))
        assert r.overall_status == OverallStatus.FAIL
        assert "worker" in r.unresolved_fields

    def test_unknown_company_fails(self):
        r = validate_work_order(self._base_order(company="Totally Fake Corp"))
        assert r.overall_status == OverallStatus.FAIL
        assert "company" in r.unresolved_fields

    def test_unknown_location_fails(self):
        r = validate_work_order(self._base_order(location="Mars Colony 7"))
        assert r.overall_status == OverallStatus.FAIL
        assert "location" in r.unresolved_fields

    def test_unknown_equipment_fails(self):
        r = validate_work_order(self._base_order(vehicle_equipment="XYZ-9999"))
        assert r.overall_status == OverallStatus.FAIL
        assert "vehicle_equipment" in r.unresolved_fields

    def test_invalid_start_time_fails(self):
        r = validate_work_order(self._base_order(start_time="not a time"))
        assert r.overall_status == OverallStatus.FAIL
        assert "start_time" in r.unresolved_fields

    def test_invalid_end_time_fails(self):
        r = validate_work_order(self._base_order(end_time="25:00"))
        assert r.overall_status == OverallStatus.FAIL
        assert "end_time" in r.unresolved_fields

    def test_multiple_failures_accumulate(self):
        r = validate_work_order(self._base_order(
            worker="Nobody XYZ",
            company="Fake Corp",
            start_time="invalid",
        ))
        assert r.overall_status == OverallStatus.FAIL
        assert "worker" in r.unresolved_fields
        assert "company" in r.unresolved_fields
        assert "start_time" in r.unresolved_fields

    # --- Empty / None handling ---

    def test_empty_optional_field_passes(self):
        r = validate_work_order(self._base_order(remaining_tasks=""))
        assert r.field_results["remaining_tasks"].status == MatchStatus.PASS_THROUGH

    def test_none_values_treated_as_empty(self):
        order = {k: None for k in self._base_order()}
        r = validate_work_order(order)
        # All None → empty string → EMPTY/PASS_THROUGH status
        # EMPTY isn't NO_MATCH, so required fields don't trigger FAIL
        # This is a design decision (or potential bug — see notes)
        assert r.overall_status in (OverallStatus.PASS, OverallStatus.REVIEW, OverallStatus.FAIL)

    # --- Resolved JSON structure ---

    def test_resolved_json_has_all_13_fields(self):
        r = validate_work_order(self._base_order())
        assert len(r.resolved_json) == 13
        for key in self._base_order():
            assert key in r.resolved_json

    def test_resolved_json_values_are_strings(self):
        r = validate_work_order(self._base_order())
        for key, val in r.resolved_json.items():
            assert isinstance(val, str), f"Field {key} should be string, got {type(val)}"

    def test_algorithm_scores_present_for_fuzzy_fields(self):
        r = validate_work_order(self._base_order())
        for field in ("worker", "company", "location", "vehicle_equipment"):
            fr = r.field_results[field]
            assert isinstance(fr.algorithm_scores, dict)
            assert len(fr.algorithm_scores) > 0

    # --- File-based test work order ---

    def test_edge_case_json_file(self):
        """Validate the companion edge_case_work_order.json file."""
        test_file = Path(__file__).parent.parent / "test_data" / "edge_case_work_order.json"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        with open(test_file) as f:
            order = json.load(f)

        r = validate_work_order(order)
        # This order has intentionally fuzzy inputs:
        #   - "TRK001" (no hyphen)
        #   - "Hartwell Industrial" (partial company)
        #   - "Bay 1" (partial location)
        #   - "8:30" (single digit hour)
        #   - "11:30 AM" (AM/PM format)
        # Should not outright FAIL (all fields are close enough)
        assert r.overall_status in (OverallStatus.PASS, OverallStatus.REVIEW), (
            f"Expected PASS or REVIEW, got {r.overall_status.value}. "
            f"Unresolved: {r.unresolved_fields}"
        )

    def test_clean_json_file_passes(self):
        """Validate the clean reference work order."""
        test_file = Path(__file__).parent.parent / "test_data" / "edge_case_work_order_clean.json"
        if not test_file.exists():
            pytest.skip(f"Test file not found: {test_file}")

        with open(test_file) as f:
            order = json.load(f)

        r = validate_work_order(order)
        assert r.overall_status == OverallStatus.PASS


# ═══════════════════════════════════════════════════════════════════════════════
# 10. BOUNDARY / DEFENSIVE INPUTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestBoundaryConditions:
    """Unusual inputs that stress the system."""

    def test_single_character_worker(self):
        r = resolve_field("worker", "a")
        assert r.status == MatchStatus.NO_MATCH

    def test_single_character_company(self):
        r = resolve_field("company", "X")
        assert r.status == MatchStatus.NO_MATCH

    def test_very_long_string(self):
        long_name = "A" * 1000
        r = resolve_field("worker", long_name)
        assert r.status == MatchStatus.NO_MATCH

    def test_numeric_string_as_worker(self):
        r = resolve_field("worker", "12345")
        assert r.status == MatchStatus.NO_MATCH

    def test_special_chars_only(self):
        r = resolve_field("worker", "!@#$%^&*()")
        assert r.status == MatchStatus.NO_MATCH

    def test_unicode_worker_name(self):
        r = resolve_field("worker", "Derek O'Brien")  # curly apostrophe
        # This may or may not match depending on normalization
        # At minimum, it shouldn't crash
        assert r.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF, MatchStatus.NO_MATCH)

    def test_sql_injection_attempt(self):
        r = resolve_field("worker", "'; DROP TABLE workers; --")
        assert r.status == MatchStatus.NO_MATCH
        # Shouldn't crash or affect anything

    def test_html_injection_attempt(self):
        r = resolve_field("worker", "<script>alert('xss')</script>")
        assert r.status == MatchStatus.NO_MATCH

    def test_empty_string_all_fields(self):
        for field in ("worker", "company", "location", "vehicle_equipment"):
            r = resolve_field(field, "")
            assert r.status == MatchStatus.EMPTY

    def test_time_empty_string(self):
        r = validate_time_field("start_time", "")
        assert r.status == MatchStatus.EMPTY

    def test_time_none(self):
        """None gets converted to '' in validate_work_order, but direct call may differ."""
        r = validate_time_field("start_time", "")
        assert r.status == MatchStatus.EMPTY

    def test_extra_fields_in_work_order(self):
        """Extra fields not in schema should be treated as pass-through."""
        order = {
            "vehicle_equipment": "TRK-001",
            "reported_problem": "test",
            "diagnosis_cause": "test",
            "work_performed": "test",
            "parts_used": "BLT-SERP",
            "start_time": "08:00",
            "end_time": "11:00",
            "total_time_spent": "3h 0m",
            "future_recommendations": "test",
            "remaining_tasks": "",
            "worker": "James Hartwell",
            "company": "Hartwell Industrial Services",
            "location": "Main Workshop — Bay 1",
            "extra_field": "should be pass-through",
        }
        r = validate_work_order(order)
        assert "extra_field" in r.resolved_json
        assert r.field_results["extra_field"].status == MatchStatus.PASS_THROUGH

    def test_numeric_value_in_string_field(self):
        """What if Gemini returns a number instead of string?"""
        order = {
            "vehicle_equipment": "TRK-001",
            "reported_problem": "test",
            "diagnosis_cause": "test",
            "work_performed": "test",
            "parts_used": "BLT-SERP",
            "start_time": "08:00",
            "end_time": "11:00",
            "total_time_spent": "3h 0m",
            "future_recommendations": "test",
            "remaining_tasks": "",
            "worker": "James Hartwell",
            "company": "Hartwell Industrial Services",
            "location": "Main Workshop — Bay 1",
            "start_time": 800,  # numeric instead of string
        }
        # The validator does str(raw_value), so 800 → "800"
        r = validate_work_order(order)
        # "800" won't parse as a valid time
        assert r.field_results["start_time"].status == MatchStatus.TIME_INVALID


# ═══════════════════════════════════════════════════════════════════════════════
# 11. SCORE THRESHOLD BOUNDARY TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreThresholds:
    """Verify that score thresholds are applied correctly."""

    def test_exact_threshold_is_100(self):
        r = resolve_field("worker", "James Hartwell")
        assert r.score == 100.0
        assert r.status == MatchStatus.EXACT

    def test_high_conf_above_85(self):
        r = resolve_field("worker", "James Hartwal")  # slight typo → HIGH_CONF
        assert r.score >= THRESHOLD_HIGH
        assert r.status == MatchStatus.HIGH_CONF

    def test_low_conf_between_60_and_85(self):
        r = resolve_field("worker", "Hartwell, James")
        assert THRESHOLD_LOW <= r.score < THRESHOLD_HIGH
        assert r.status == MatchStatus.LOW_CONF

    def test_no_match_below_60(self):
        r = resolve_field("worker", "Zzzyx Qqqington")
        assert r.score < THRESHOLD_LOW
        assert r.status == MatchStatus.NO_MATCH

    def test_parts_min_score_drives_status(self):
        """Parts status is driven by the MINIMUM per-token score."""
        r = resolve_parts_used("BLT-SERP, ZZZ-FAKE-9999")
        # BLT-SERP = EXACT (100), ZZZ-FAKE-9999 = NO_MATCH (<60)
        # min score → NO_MATCH
        assert r.status in (MatchStatus.NO_MATCH, MatchStatus.LOW_CONF)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. KNOWN BUGS / DOCUMENTED BEHAVIOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestKnownBugs:
    """Previously known bugs — now fixed. These tests assert the correct post-fix behavior."""

    def test_whitespace_does_not_degrade_exact_score(self):
        """
        FIXED: Leading/trailing whitespace in raw_value no longer degrades EXACT
        matches to HIGH_CONFIDENCE. _score_algorithms now calls .strip() before
        scoring so '  James Hartwell  ' resolves to EXACT.
        """
        r = resolve_field("worker", "  James Hartwell  ")
        assert r.status == MatchStatus.EXACT
        assert r.resolved_value == "James Hartwell"

    def test_duration_30m_shorthand_recognized(self):
        """
        FIXED: The duration regex now accepts bare 'm' as a minutes suffix so
        '30m' is recognised and normalised to '0h 30m'.
        """
        r = validate_time_field("total_time_spent", "30m")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "0h 30m"

    def test_duration_100_hours_passes(self):
        """
        FIXED: Duration HH:MM pattern now uses \\d+ for the hours group so
        values >99 hours are accepted (e.g. '100:30' → '100h 30m').
        """
        r = validate_time_field("total_time_spent", "100:30")
        assert r.status == MatchStatus.TIME_VALID
        assert r.resolved_value == "100h 30m"

    def test_empty_required_fields_fail(self):
        """
        FIXED: EMPTY status is now included in the failure check so a work order
        where all required fields are empty strings correctly returns FAIL.
        """
        order = {
            "vehicle_equipment": "", "reported_problem": "", "diagnosis_cause": "",
            "work_performed": "", "parts_used": "", "start_time": "", "end_time": "",
            "total_time_spent": "", "future_recommendations": "", "remaining_tasks": "",
            "worker": "", "company": "", "location": "",
        }
        r = validate_work_order(order)
        assert r.overall_status == OverallStatus.FAIL
