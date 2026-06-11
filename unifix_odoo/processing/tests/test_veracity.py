"""Tests for processor/veracity.py — Issue #1 coverage."""

import json
import pytest
from unittest.mock import MagicMock, patch

from processor.veracity import (
    should_run_veracity,
    run_veracity_check,
    apply_veracity_corrections,
    VERACITY_TRIGGER_STATUSES,
    VERACITY_TRIGGER_CONFIDENCE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_validation_result(status_value: str):
    """Build a minimal mock ValidationResult with .overall_status.value."""
    return MagicMock(overall_status=MagicMock(value=status_value))


# ---------------------------------------------------------------------------
# should_run_veracity
# ---------------------------------------------------------------------------

class TestShouldRunVeracity:

    def test_review_status_triggers_veracity(self):
        result = _make_validation_result("REVIEW")
        assert should_run_veracity(result, {}) is True

    def test_fail_status_triggers_veracity(self):
        result = _make_validation_result("FAIL")
        assert should_run_veracity(result, {}) is True

    def test_pass_with_all_medium_confidence_skips_veracity(self):
        result = _make_validation_result("PASS")
        confidences = {"worker": "MEDIUM", "company": "MEDIUM", "location": "MEDIUM"}
        assert should_run_veracity(result, confidences) is False

    def test_pass_with_one_low_confidence_triggers_veracity(self):
        result = _make_validation_result("PASS")
        confidences = {"worker": "HIGH", "company": "LOW", "location": "MEDIUM"}
        assert should_run_veracity(result, confidences) is True

    def test_pass_with_empty_confidences_skips_veracity(self):
        result = _make_validation_result("PASS")
        assert should_run_veracity(result, {}) is False

    def test_pass_with_all_high_confidence_skips_veracity(self):
        result = _make_validation_result("PASS")
        confidences = {"worker": "HIGH", "company": "HIGH"}
        assert should_run_veracity(result, confidences) is False

    def test_trigger_statuses_set_is_correct(self):
        assert VERACITY_TRIGGER_STATUSES == {"REVIEW", "FAIL"}

    def test_trigger_confidence_set_is_correct(self):
        assert VERACITY_TRIGGER_CONFIDENCE == {"LOW"}


# ---------------------------------------------------------------------------
# apply_veracity_corrections
# ---------------------------------------------------------------------------

class TestApplyVeracityCorrections:

    def _base_order(self):
        return {
            "worker": "James Hartwell",
            "company": "Hartwell Industrial Services",
            "location": "Main Workshop Bay 1",
        }

    def test_incorrect_verdict_changes_field(self):
        order = self._base_order()
        veracity_result = {
            "verified_fields": {
                "worker": {
                    "original_value": "James Hartwell",
                    "verdict": "INCORRECT",
                    "corrected_value": "Tom Kowalski",
                    "evidence": "Audio says Tom Kowalski",
                    "confidence": "HIGH",
                }
            }
        }
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert corrected["worker"] == "Tom Kowalski"
        assert "worker" in changed

    def test_correct_verdict_leaves_field_unchanged(self):
        order = self._base_order()
        veracity_result = {
            "verified_fields": {
                "worker": {
                    "original_value": "James Hartwell",
                    "verdict": "CORRECT",
                    "corrected_value": "James Hartwell",
                    "evidence": "Confirmed in audio",
                    "confidence": "HIGH",
                }
            }
        }
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert corrected["worker"] == "James Hartwell"
        assert "worker" not in changed

    def test_uncertain_with_high_confidence_applies_correction(self):
        order = self._base_order()
        veracity_result = {
            "verified_fields": {
                "location": {
                    "original_value": "Main Workshop Bay 1",
                    "verdict": "UNCERTAIN",
                    "corrected_value": "North Depot",
                    "evidence": "Unclear in audio but probably North Depot",
                    "confidence": "HIGH",
                }
            }
        }
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert corrected["location"] == "North Depot"
        assert "location" in changed

    def test_uncertain_with_low_confidence_is_skipped(self):
        order = self._base_order()
        veracity_result = {
            "verified_fields": {
                "company": {
                    "original_value": "Hartwell Industrial Services",
                    "verdict": "UNCERTAIN",
                    "corrected_value": "Some Other Company",
                    "evidence": "Unclear",
                    "confidence": "LOW",
                }
            }
        }
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert corrected["company"] == "Hartwell Industrial Services"
        assert "company" not in changed

    def test_uncertain_with_medium_confidence_is_skipped(self):
        order = self._base_order()
        veracity_result = {
            "verified_fields": {
                "worker": {
                    "original_value": "James Hartwell",
                    "verdict": "UNCERTAIN",
                    "corrected_value": "Different Name",
                    "evidence": "Partially audible",
                    "confidence": "MEDIUM",
                }
            }
        }
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert corrected["worker"] == "James Hartwell"
        assert "worker" not in changed

    def test_field_not_in_work_order_is_skipped(self):
        order = self._base_order()
        veracity_result = {
            "verified_fields": {
                "vehicle_equipment": {
                    "original_value": "TRK-001",
                    "verdict": "INCORRECT",
                    "corrected_value": "EXC-010",
                    "evidence": "Audio mentions excavator",
                    "confidence": "HIGH",
                }
            }
        }
        # vehicle_equipment is not in the work order dict
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert "vehicle_equipment" not in corrected
        assert "vehicle_equipment" not in changed

    def test_empty_verified_fields_returns_unchanged_work_order_and_empty_list(self):
        order = self._base_order()
        veracity_result = {"verified_fields": {}, "overall_verdict": "PASS", "corrections_count": 0}
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert corrected == order
        assert changed == []

    def test_missing_verified_fields_key_returns_unchanged(self):
        order = self._base_order()
        corrected, changed = apply_veracity_corrections(order, {})
        assert corrected == order
        assert changed == []

    def test_original_work_order_is_not_mutated(self):
        order = self._base_order()
        original_worker = order["worker"]
        veracity_result = {
            "verified_fields": {
                "worker": {
                    "original_value": "James Hartwell",
                    "verdict": "INCORRECT",
                    "corrected_value": "Tom Kowalski",
                    "evidence": "...",
                    "confidence": "HIGH",
                }
            }
        }
        apply_veracity_corrections(order, veracity_result)
        assert order["worker"] == original_worker  # original unchanged

    def test_multiple_corrections_all_applied(self):
        order = self._base_order()
        veracity_result = {
            "verified_fields": {
                "worker": {
                    "verdict": "INCORRECT",
                    "corrected_value": "Tom Kowalski",
                    "confidence": "HIGH",
                },
                "company": {
                    "verdict": "INCORRECT",
                    "corrected_value": "PrimeTech Maintenance Ltd",
                    "confidence": "HIGH",
                },
            }
        }
        corrected, changed = apply_veracity_corrections(order, veracity_result)
        assert corrected["worker"] == "Tom Kowalski"
        assert corrected["company"] == "PrimeTech Maintenance Ltd"
        assert set(changed) == {"worker", "company"}


# ---------------------------------------------------------------------------
# run_veracity_check
# ---------------------------------------------------------------------------

VALID_VERACITY_RESPONSE = {
    "verified_fields": {
        "worker": {
            "original_value": "James Hartwell",
            "verdict": "CORRECT",
            "corrected_value": "James Hartwell",
            "evidence": "Clearly stated in audio",
            "confidence": "HIGH",
        }
    },
    "overall_verdict": "PASS",
    "corrections_count": 0,
}


class TestRunVeracityCheck:

    def _make_client_and_model(self):
        client = MagicMock()
        model_id = "gemini-test-model"
        return client, model_id

    @patch("processor.veracity.call_gemini_generic")
    def test_valid_json_response_returns_parsed_dict(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = json.dumps(VALID_VERACITY_RESPONSE)
        mock_call.return_value = mock_response

        client, model_id = self._make_client_and_model()
        result = run_veracity_check(
            client=client,
            model_id=model_id,
            audio_bytes=b"fake audio bytes",
            mime_type="audio/wav",
            first_pass_json={"worker": "James Hartwell"},
        )

        assert result == VALID_VERACITY_RESPONSE
        assert result["overall_verdict"] == "PASS"
        assert result["corrections_count"] == 0

    @patch("processor.veracity.call_gemini_generic")
    def test_invalid_json_response_returns_empty_dict(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = "this is not valid JSON {{"
        mock_call.return_value = mock_response

        client, model_id = self._make_client_and_model()
        result = run_veracity_check(
            client=client,
            model_id=model_id,
            audio_bytes=b"fake audio bytes",
            mime_type="audio/wav",
            first_pass_json={"worker": "James Hartwell"},
        )

        assert result == {}

    @patch("processor.veracity.call_gemini_generic")
    def test_api_call_error_returns_empty_dict(self, mock_call):
        from processor.exceptions import APICallError
        mock_call.side_effect = APICallError("Simulated API failure")

        client, model_id = self._make_client_and_model()
        result = run_veracity_check(
            client=client,
            model_id=model_id,
            audio_bytes=b"fake audio bytes",
            mime_type="audio/wav",
            first_pass_json={"worker": "James Hartwell"},
        )

        assert result == {}

    @patch("processor.veracity.call_gemini_generic")
    def test_generic_exception_returns_empty_dict(self, mock_call):
        mock_call.side_effect = RuntimeError("Unexpected failure")

        client, model_id = self._make_client_and_model()
        result = run_veracity_check(
            client=client,
            model_id=model_id,
            audio_bytes=b"fake audio bytes",
            mime_type="audio/wav",
            first_pass_json={"worker": "James Hartwell"},
        )

        assert result == {}

    @patch("processor.veracity.call_gemini_generic")
    def test_transcript_included_when_provided(self, mock_call):
        """When a transcript is provided, it should be embedded in the prompt text."""
        mock_response = MagicMock()
        mock_response.text = json.dumps(VALID_VERACITY_RESPONSE)
        mock_call.return_value = mock_response

        client, model_id = self._make_client_and_model()
        run_veracity_check(
            client=client,
            model_id=model_id,
            audio_bytes=b"fake audio bytes",
            mime_type="audio/wav",
            first_pass_json={"worker": "James Hartwell"},
            first_pass_transcript="Worker James Hartwell performed maintenance on TRK-001.",
        )

        # Verify call_gemini_generic was called; the prompt arg (contents[1]) contains transcript
        assert mock_call.called
        call_args = mock_call.call_args
        contents = call_args.kwargs.get("contents") or call_args.args[0] if call_args.args else call_args.kwargs["contents"]
        # contents is [audio_part, prompt_string]; check the prompt string
        prompt_str = contents[1] if isinstance(contents, list) else ""
        assert "James Hartwell performed maintenance" in prompt_str

    @patch("processor.veracity.call_gemini_generic")
    def test_no_transcript_uses_placeholder(self, mock_call):
        mock_response = MagicMock()
        mock_response.text = json.dumps(VALID_VERACITY_RESPONSE)
        mock_call.return_value = mock_response

        client, model_id = self._make_client_and_model()
        run_veracity_check(
            client=client,
            model_id=model_id,
            audio_bytes=b"fake audio bytes",
            mime_type="audio/wav",
            first_pass_json={"worker": "James Hartwell"},
        )

        call_args = mock_call.call_args
        contents = call_args.kwargs.get("contents", call_args.args[0] if call_args.args else [])
        prompt_str = contents[1] if isinstance(contents, list) else ""
        assert "[See attached audio]" in prompt_str
