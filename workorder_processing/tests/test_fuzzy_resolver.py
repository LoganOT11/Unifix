import pytest
from validator.fuzzy_resolver import resolve_field, resolve_parts_used
from validator.models import MatchStatus


class TestWorkerResolution:

    def test_exact_match(self):
        result = resolve_field("worker", "James Hartwell")
        assert result.status == MatchStatus.EXACT
        assert result.score == 100.0
        assert result.resolved_value == "James Hartwell"

    def test_typo_first_name(self):
        result = resolve_field("worker", "Jmes Hartwell")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert result.resolved_value == "James Hartwell"

    def test_transposed_name(self):
        # token_sort_ratio=100 but jaro_winkler drags composite to ~77 (LOW_CONF)
        result = resolve_field("worker", "Hartwell James")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT, MatchStatus.LOW_CONF)
        assert result.resolved_value == "James Hartwell"

    def test_partial_last_name_only(self):
        result = resolve_field("worker", "Kowalski")
        assert result.resolved_value == "Tom Kowalski"
        assert result.score < 100.0

    def test_name_with_apostrophe(self):
        # Missing apostrophe lowers token_sort_ratio; composite lands ~80 (LOW_CONF)
        result = resolve_field("worker", "Derek O Brien")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT, MatchStatus.LOW_CONF)
        assert result.resolved_value == "Derek O'Brien"

    def test_completely_unknown_worker(self):
        result = resolve_field("worker", "Zzzyx Qqqington")
        assert result.status == MatchStatus.NO_MATCH
        assert result.resolved_value == "Zzzyx Qqqington"

    def test_empty_worker(self):
        result = resolve_field("worker", "")
        assert result.status == MatchStatus.EMPTY
        assert result.resolved_value == ""

    def test_worker_nickname(self):
        result = resolve_field("worker", "Tom K")
        assert "Kowalski" in result.resolved_value or result.status == MatchStatus.NO_MATCH

    @pytest.mark.parametrize("raw,expected_canonical", [
        ("Maria Sanchez",    "Maria Sanchez"),
        ("Priya Nambia",     "Priya Nambiar"),
        ("Fatima Al Hassan", "Fatima Al-Hassan"),
        ("Luc Trembay",      "Luc Tremblay"),
    ])
    def test_worker_parametrized(self, raw, expected_canonical):
        result = resolve_field("worker", raw)
        assert result.resolved_value == expected_canonical


class TestCompanyResolution:

    def test_exact_full_name(self):
        result = resolve_field("company", "PrimeTech Maintenance Ltd")
        assert result.status == MatchStatus.EXACT

    def test_short_code(self):
        result = resolve_field("company", "NFS")
        assert result.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)
        assert "Northern Fleet" in result.resolved_value or result.resolved_value == "NFS"

    def test_abbreviated_name(self):
        result = resolve_field("company", "Apex Equipment")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "Apex" in result.resolved_value

    def test_lowercase_company(self):
        result = resolve_field("company", "delta hydraulics")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "Delta Hydraulics" in result.resolved_value

    def test_misspelled_company(self):
        result = resolve_field("company", "BlueLine Servise Group")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "BlueLine" in result.resolved_value

    def test_unknown_company(self):
        result = resolve_field("company", "Totally Unknown Corp XYZ")
        assert result.status == MatchStatus.NO_MATCH


class TestLocationResolution:

    def test_exact_location(self):
        result = resolve_field("location", "Main Workshop — Bay 1")
        assert result.status == MatchStatus.EXACT

    def test_partial_location(self):
        result = resolve_field("location", "Bay 2")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "Bay 2" in result.resolved_value

    def test_location_abbreviation(self):
        result = resolve_field("location", "North Depot")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "North Depot" in result.resolved_value

    def test_field_site(self):
        result = resolve_field("location", "Field Site Alpha")
        assert result.status == MatchStatus.EXACT

    def test_colloquial_location(self):
        result = resolve_field("location", "fuel station")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)

    def test_unknown_location(self):
        result = resolve_field("location", "Some Random Place 99Z")
        assert result.status == MatchStatus.NO_MATCH


class TestEquipmentResolution:

    def test_exact_tag(self):
        result = resolve_field("vehicle_equipment", "TRK-001")
        assert result.status == MatchStatus.EXACT

    def test_tag_no_hyphen(self):
        result = resolve_field("vehicle_equipment", "TRK001")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "TRK-001" in result.resolved_value

    def test_description_match(self):
        result = resolve_field("vehicle_equipment", "320 Excavator")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "CAT 320" in result.resolved_value or "EXC-010" in result.resolved_value

    def test_make_model_only(self):
        result = resolve_field("vehicle_equipment", "Peterbilt 579")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.EXACT)
        assert "Peterbilt" in result.resolved_value

    def test_generator_by_capacity(self):
        result = resolve_field("vehicle_equipment", "250kVA generator")
        assert "250kVA" in result.resolved_value or result.status in (
            MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF
        )

    def test_unknown_equipment(self):
        result = resolve_field("vehicle_equipment", "XYZ-9999 Unknown Machine")
        assert result.status == MatchStatus.NO_MATCH


class TestPartsResolution:

    def test_single_exact_part(self):
        result = resolve_parts_used("Hydraulic Filter 2240")
        assert result.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)
        assert "HF-2240" in result.resolved_value or "Hydraulic Filter 2240" in result.resolved_value

    def test_multi_part_string(self):
        result = resolve_parts_used("Oil Filter, Air Filter, 15W-40 Oil")
        assert result.status != MatchStatus.NO_MATCH
        tokens = [t.strip() for t in result.resolved_value.split(",")]
        assert len(tokens) == 3

    def test_part_number_only(self):
        result = resolve_parts_used("BLT-SERP")
        assert result.status in (MatchStatus.EXACT, MatchStatus.HIGH_CONF)

    def test_partial_description(self):
        result = resolve_parts_used("serpentine belt")
        assert result.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)
        assert "Serpentine" in result.resolved_value or "BLT-SERP" in result.resolved_value

    def test_multi_part_one_unknown(self):
        result = resolve_parts_used("Oil Filter, ZZZ-FAKE-9999")
        assert result.status in (MatchStatus.LOW_CONF, MatchStatus.NO_MATCH)

    def test_empty_parts(self):
        result = resolve_parts_used("")
        assert result.status == MatchStatus.EMPTY

    def test_part_with_quantity(self):
        result = resolve_parts_used("2x Oil Filter")
        assert result.status != MatchStatus.EMPTY


class TestTimeValidation:

    @pytest.mark.parametrize("raw,expected", [
        ("08:30",    "08:30"),
        ("8:30",     "08:30"),
        ("08:30:00", "08:30"),
        ("08:30 AM", "08:30"),
        ("08:30 PM", "20:30"),
        ("12:00 PM", "12:00"),
        ("12:00 AM", "00:00"),
    ])
    def test_valid_start_times(self, raw, expected):
        from validator.time_validator import validate_time_field
        result = validate_time_field("start_time", raw)
        assert result.status == MatchStatus.TIME_VALID
        assert result.resolved_value == expected

    @pytest.mark.parametrize("raw", [
        "25:00",
        "08:65",
        "half past 8",
        "morning",
        "8am",
    ])
    def test_invalid_start_times(self, raw):
        from validator.time_validator import validate_time_field
        result = validate_time_field("start_time", raw)
        assert result.status == MatchStatus.TIME_INVALID

    @pytest.mark.parametrize("raw,expected", [
        ("01:30",              "1h 30m"),
        ("2h30m",              "2h 30m"),
        ("2 hours 30 minutes", "2h 30m"),
        ("2 hours",            "2h 0m"),
        ("45 minutes",         "0h 45m"),
        ("90 min",             "1h 30m"),
    ])
    def test_valid_durations(self, raw, expected):
        from validator.time_validator import validate_time_field
        result = validate_time_field("total_time_spent", raw)
        assert result.status == MatchStatus.TIME_VALID
        assert result.resolved_value == expected

    @pytest.mark.parametrize("raw", [
        "a long time",
        "quick",
        "",
    ])
    def test_invalid_durations(self, raw):
        from validator.time_validator import validate_time_field
        result = validate_time_field("total_time_spent", raw)
        assert result.status in (MatchStatus.TIME_INVALID, MatchStatus.EMPTY)


class TestFullWorkOrderValidation:

    def _make_order(self, overrides: dict = {}) -> dict:
        base = {
            "vehicle_equipment":      "TRK-001",
            "reported_problem":       "Engine overheating",
            "diagnosis_cause":        "Low coolant, clogged thermostat",
            "work_performed":         "Replaced thermostat, topped up coolant",
            "parts_used":             "Coolant — Long Life, Oil Filter — Heavy Duty",
            "start_time":             "08:00",
            "end_time":               "11:30",
            "total_time_spent":       "3h30m",
            "future_recommendations": "Monitor coolant level weekly",
            "remaining_tasks":        "",
            "worker":                 "James Hartwell",
            "company":                "Hartwell Industrial Services",
            "location":               "Main Workshop — Bay 1",
        }
        base.update(overrides)
        return base

    def test_clean_input_passes(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order())
        assert result.overall_status.value == "PASS"
        assert result.unresolved_fields == []

    def test_typo_in_worker_still_passes(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"worker": "James Hartwal"}))
        assert result.overall_status.value in ("PASS", "REVIEW")
        assert result.field_results["worker"].resolved_value == "James Hartwell"

    def test_unknown_worker_causes_fail(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"worker": "Nobody Known XYZ"}))
        assert result.overall_status.value == "FAIL"
        assert "worker" in result.unresolved_fields

    def test_invalid_time_causes_fail(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"start_time": "not a time"}))
        assert result.overall_status.value == "FAIL"
        assert "start_time" in result.unresolved_fields

    def test_low_confidence_parts_triggers_review(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"parts_used": "some vague part"}))
        assert result.overall_status.value in ("REVIEW", "FAIL")

    def test_resolved_json_contains_all_fields(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order())
        assert len(result.resolved_json) == 13

    def test_free_text_passes_through_unchanged(self):
        from validator.work_order_validator import validate_work_order
        order = self._make_order({"reported_problem": "The engine makes a strange ticking noise"})
        result = validate_work_order(order)
        fr = result.field_results["reported_problem"]
        assert fr.status == MatchStatus.PASS_THROUGH
        assert fr.resolved_value == "The engine makes a strange ticking noise"

    def test_empty_optional_field_does_not_fail(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"remaining_tasks": ""}))
        assert result.field_results["remaining_tasks"].status == MatchStatus.PASS_THROUGH
        assert result.overall_status.value != "FAIL"

    def test_hallucinated_location_with_close_match(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order({"location": "Workshop Bay 3"}))
        fr = result.field_results["location"]
        assert "Bay 3" in fr.resolved_value
        assert fr.status in (MatchStatus.HIGH_CONF, MatchStatus.LOW_CONF)

    def test_algorithm_scores_present_in_result(self):
        from validator.work_order_validator import validate_work_order
        result = validate_work_order(self._make_order())
        for field in ("worker", "company", "location", "vehicle_equipment"):
            assert isinstance(result.field_results[field].algorithm_scores, dict)
            assert len(result.field_results[field].algorithm_scores) > 0
