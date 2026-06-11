# Findings: Confidence Scoring & Retrieval-Augmented Disambiguation
**Date:** 2026-06-03  
**Scope:** Read-only investigation. No production code was modified.

---

## 1. Hypothesis Ledger

| # | Hypothesis | Verdict |
|---|---|---|
| H1 | `FieldResult.top_candidates` holds K nearest entries with enough detail (name, score, canonical record) to drive a disambiguation prompt | **Partial** |
| H2 | Gemini self-reported confidence wired into composite score as HIGH +5 / MEDIUM 0 / LOW −10 | **Confirmed** |
| H3 | Veracity pass re-submits full first-pass JSON to same model at lower temperature, no reference data | **Confirmed** |
| H4 | Image pipeline skips field validation and veracity entirely | **Confirmed** |
| H5 | Field-level positional/region data exists for v3 image form enabling per-field cropping | **Refuted** |
| H6 | `genai` client/model exposes logprobs or multi-candidate signals for alternative uncertainty | **Refuted (logprobs) / Partial (multi-sample)** |

### H1 — Partial

`top_candidates` is populated by `_find_best_match` as `list[{"value": str, "score": float}]` — name and raw composite score only. Canonical record fields (e.g. `id`, `department`) are **not** included per candidate; they are only resolved for the single winner via `resolve_canonical`.

**Evidence:** `fuzzy_resolver.py:61`
```python
top_candidates = [{"value": c, "score": round(s, 2)} for c, s, _ in scored[:top_n]]
```
`models.py:33`: `top_candidates: list[dict] = field(default_factory=list)`

`top_n` is hardcoded to **3** in `_find_best_match`'s signature; there is no per-field override in config.

**Critical gap — `parts_used`:** In `resolve_parts_used` (`fuzzy_resolver.py:157`), top_candidates for each token are explicitly discarded (`_`). The `FieldResult` returned for `parts_used` always has `top_candidates = []`. A disambiguation prompt for parts tokens cannot be built from existing data without re-running matching or extending the resolver.

**Summary for disambiguation prompt assembly:** Name + fuzzy score per candidate is sufficient to ask "which of these best matches what you heard?" For workers/companies/locations, the 3 candidates give a plausible shortlist. For equipment the candidate pool is tripled (tags + descriptions + combined strings) which means the top-3 may all be string variants of the same physical asset rather than 3 distinct assets — the prompt should de-duplicate. For parts, the resolver must be extended to surface per-token candidates.

### H2 — Confirmed

`GEMINI_CONFIDENCE_BOOST` dict at `fuzzy_resolver.py:15-19`:
```python
GEMINI_CONFIDENCE_BOOST: dict[str, float] = {
    "HIGH":   5.0,
    "MEDIUM": 0.0,
    "LOW":   -10.0,
}
```

Applied in `resolve_field_with_gemini_confidence` (`fuzzy_resolver.py:188-193`):
```python
boost = self._confidence_boosts.get(gemini_confidence.upper(), 0.0)
if boost != 0.0 and result.score > 0.0:
    adjusted = max(0.0, min(100.0, result.score + boost))
    result.score = round(adjusted, 2)
    result.status = self._status_from_score(adjusted)
```

The boost is applied to the **winning candidate's composite score after matching** — not per-candidate, not before `_find_best_match` selects the winner. Status is re-derived from the adjusted score via `_status_from_score`.

**Worked example of compounding failure:** Thresholds are `exact=100, high_conf=85, low_conf=60`. A hallucinated reading "James Harwell" fuzzy-scores 83.0 against the closest DB entry "James Hartwell" — placing it at `LOW_CONF` (60–84). Gemini emits `worker__confidence: HIGH`. Boost: 83.0 + 5.0 = 88.0 → `HIGH_CONF`. The wrong name is accepted with HIGH confidence, and the veracity check is not triggered (because overall_status may still be PASS and the raw gemini confidence is HIGH, not LOW). The `+5` reward for confident extraction is applied even when the extraction is subtly wrong, crossing the `HIGH_CONF` threshold and silencing the safety valve.

**Secondary observation:** `should_run_veracity` reads `gemini_confidences` directly (`veracity.py:25`) — the raw string values from the first-pass parse. So a HIGH gemini confidence that boosted a borderline wrong match to HIGH_CONF means both the status guard (PASS, no REVIEW) and the confidence guard (HIGH, not LOW) suppress veracity. The "success" is accepted silently with no second-pass check.

### H3 — Confirmed

`run_veracity_check` (`veracity.py:30-63`) sends:
1. The original audio bytes as a `types.Part` (line 48)
2. A formatted prompt containing `{source_transcript}` (usually `"[See attached audio]"` since `BasePipeline._run_veracity` calls with no transcript arg, `veracity.py:134-140`) and `{first_pass_json}` — the full 13-field work order dict serialized to JSON (line 44-46)

Temperature is hardcoded to **0.1** at `veracity.py:57`:
```python
config=types.GenerateContentConfig(
    response_mime_type="application/json",
    temperature=0.1,
)
```

The prompt (`veracity_v1.txt`) contains **no reference data, no candidate lists**. The model is asked to verify each field by finding "evidence in the source material" — but the source material it sees is the audio (which it must re-transcribe in the same call) and the first-pass JSON it is being asked to check. The anchoring problem is structural: the first-pass JSON is presented as `<first_pass_extraction>` alongside the audio, which primes the model toward its own earlier output.

**Config/code discrepancy:** `audio_v1.yaml` defines `trigger_statuses: [REVIEW, FAIL]` and `trigger_confidences: [LOW]`, but `should_run_veracity` at `veracity.py:13-14` hardcodes these as module-level constants and **never reads them from `VeracityConfig`**. The YAML values are loaded into `VeracityConfig.trigger_statuses/trigger_confidences` but those fields are never consulted anywhere in the codebase.

### H4 — Confirmed (config-driven, not override-driven)

`ImagePipeline` (`pipeline/image.py`) does **not** override `_validate_fields` or `_run_veracity`. The skip is entirely driven by `image_v3.yaml`:
```yaml
validation: null
veracity:
  enabled: false
```

In `BasePipeline._validate_fields` (`base.py:114-115`):
```python
if self.config.validation is None:
    return None
```

In `BasePipeline._run_veracity` (`base.py:127`):
```python
if not self.config.veracity.enabled or val_result is None:
    return {"ran": False}
```

Both guards fire: `validation=null` causes `_validate_fields` to return `None`, and `veracity.enabled=false` additionally gates `_run_veracity`. The skip is **total**.

### H5 — Refuted

No positional data exists anywhere in the codebase. The image extraction prompt (`image_extraction_v3.txt`) requests field values only; no bounding-box or coordinate output. `PreprocessResult` (`image_preprocessor.py:16-22`) carries only `image_bytes, mime_type, quality_before, operations_applied, estimated_improvement`. The preprocessed bytes are what Gemini receives; the original file bytes are not retained separately in the pipeline after `preprocess()` returns.

The Uni-Fix form is a fixed-layout French paper form. Fixed-layout cropping is theoretically derivable by manually defining field coordinates, but no coordinate template exists in the codebase, and OpenCV field detection is not implemented. Per-field image crops for disambiguation are **not feasible without prerequisite work** (see §Image Verdict below).

### H6 — Refuted (logprobs), Partial (multi-sample)

`call_gemini_generic` (`gemini_client.py:63-92`) passes a `types.GenerateContentConfig` with only `response_mime_type` and `temperature`. The `GenerateContentResponse` is only mined for `usage_metadata` (token counts) and `candidates[0].finish_reason` — no logprob field is accessed or checked. The Google GenAI SDK at `api_version="v1"` does not surface per-token log probabilities in the Python SDK; there is no `logprobs` parameter in `GenerateContentConfig` at this version.

**Multi-sample / self-consistency:** `candidate_count` (N parallel samples in one call) is not used but is a valid `GenerateContentConfig` parameter. The current architecture is synchronous and single-call; increasing `candidate_count` would require changes only to the parsing step (handling multiple `candidates[]` objects). However, this returns independently sampled candidates from a single forward pass — it is not the same as N independent API calls with different random seeds. True self-consistency would require N independent `call_gemini_generic` invocations with a loop, which is architecturally straightforward but adds latency and cost.

---

## 2. Confidence Flow Diagram

```
audio_extraction_v2.txt prompt
  └─ <confidence_markers> section
       └─ instructs model to emit "field__confidence": "HIGH"|"MEDIUM"|"LOW"
            for: worker, company, location, vehicle_equipment, parts_used

parser.py:extract_confidence_markers() [lines 113-141]
  ├─ key.endswith("__confidence") → field_name = key.removesuffix("__confidence")
  ├─ value is None → stored as "MEDIUM"           [line 134]
  ├─ unexpected value → stored as str(value).upper() (no rejection/validation)
  └─ missing fuzzy fields default to "MEDIUM" via .setdefault() [line 139]
       ↓
  confidences: {"worker": "HIGH", "company": "LOW", ...}

work_order_validator.py:validate_work_order() [lines 37-41]
  └─ for each field in fuzzy_field_names:
       gemini_conf = conf.get(field_name, "MEDIUM")
       resolver.resolve_field_with_gemini_confidence(field_name, raw, gemini_conf)
            ↓
fuzzy_resolver.py:resolve_field_with_gemini_confidence() [lines 177-195]
  ├─ resolve_field() → raw composite score via _find_best_match (weighted avg of 3-6 algos)
  ├─ boost = GEMINI_CONFIDENCE_BOOST.get(confidence.upper(), 0.0)
  ├─ if boost != 0 and score > 0:
  │    adjusted = clamp(score + boost, 0, 100)
  │    result.score = adjusted
  │    result.status = _status_from_score(adjusted)  ← threshold re-applied
  └─ return FieldResult (with boosted score/status)
       ↓
  FieldResult.status: EXACT/HIGH_CONF/LOW_CONF/NO_MATCH

resolution.py:compute_overall_status() [lines 15-41]
  ├─ required fields with NO_MATCH/TIME_INVALID/EMPTY → FAIL
  ├─ any LOW_CONF → REVIEW
  └─ otherwise → PASS
       ↓
  OverallStatus: PASS | REVIEW | FAIL

veracity.py:should_run_veracity() [lines 19-27]
  ├─ overall_status in {"REVIEW","FAIL"} → True
  └─ any gemini_confidence in {"LOW"} → True   ← reads raw confidences, not boosted status

REDESIGN INTERVENTION POINTS:
  [A] Remove GEMINI_CONFIDENCE_BOOST or move it out of the scoring path
      → Affects: fuzzy_resolver.py:188-193 (score adjustment)
      → Downstream: threshold retuning required
  [B] Per-field disambiguation stage inserted between _validate_fields and _run_veracity
      → Consumes: top_candidates (name+score) from NO_MATCH/LOW_CONF fields
      → Replaces: veracity for reference-backed fields
  [C] Slim veracity retained for time/free-text fields (no reference grounding needed)
```

---

## 3. Veracity Baseline

**What the current stage sees:**
- Original audio bytes (as media Part)
- Placeholder transcript `"[See attached audio]"` in the prompt (pipeline never extracts a separate transcript, so `first_pass_transcript` arg is always `None` in production — `veracity.py:42`)
- Full 13-field first-pass work order dict, serialized as JSON with `indent=2`

**What it does NOT see:**
- Reference data lists (no worker names, location names, etc.)
- Candidate lists from the fuzzy resolver
- Per-field scores or match statuses from the first pass

**Temperature:** 0.1, hardcoded at `veracity.py:57`

**Output contract** (`veracity_v1.txt:22-35`): `verified_fields` dict, each field with `original_value`, `verdict` (CORRECT/INCORRECT/UNCERTAIN), `corrected_value`, `evidence`, `confidence`. Plus `overall_verdict` and `corrections_count`.

**Correction application** (`veracity.py:66-87`):
- `INCORRECT` verdict → field replaced unconditionally
- `UNCERTAIN` + veracity `confidence == "HIGH"` → field replaced
- `UNCERTAIN` + LOW/MEDIUM → skipped
- No guard against regression; if veracity introduces a worse value, it is applied

**Post-veracity re-validation** (`base.py:157-169`): `validate_work_order` is re-run after corrections, but with the **original `confidences` dict** (stale — the corrected field's gemini confidence is not updated). The post-veracity result is stored in `ver_info["validation_post_veracity"]`, but the main `envelope["validation"]` key continues to reflect the pre-veracity result. The envelope does not expose which result to trust.

**Anchoring evidence in code:** The `<first_pass_extraction>` block in `veracity_v1.txt` is explicitly labeled and formatted, and the model is asked to "find evidence in the source material that supports or contradicts the value" — but the source material available in-context is the audio it already transcribed for the first pass. There is no structural mechanism preventing the model from re-confirming what it already hallucinated.

---

## 4. Integration Map

### Audio and Video pipelines

Both share the identical path from `_validate_fields` onward. `VideoPipeline` extends `AudioPipeline` (`pipeline/video.py:19`) and overrides only `validate_input` and `preprocess`; `_validate_fields` and `_run_veracity` are inherited unchanged from `BasePipeline`.

**Optimal integration point:** A new `_disambiguate_fields` hook in `BasePipeline`, called between `_validate_fields` and `_run_veracity`:

```python
# BasePipeline.run() — proposed position:
val_result = self._validate_fields(work_order, confidences)
val_result = self._disambiguate_fields(ctx, media, work_order, confidences, val_result)  # NEW
ver_result = self._run_veracity(ctx, media, work_order, confidences, val_result)
```

`BasePipeline._disambiguate_fields` would default to a no-op returning `val_result` unchanged. `AudioPipeline` (and thereby `VideoPipeline`) would override it to:
1. Iterate `val_result.field_results` for fuzzy fields with `NO_MATCH` or `LOW_CONF`
2. Pass `top_candidates` list + audio + raw_value to a disambiguation prompt
3. Re-resolve winning candidate, update `FieldResult`, recompute `overall_status`

**`top_candidates` schema each field provides:**
```python
[{"value": "James Hartwell", "score": 83.12},
 {"value": "Tom Kowalski",   "score": 61.07},
 {"value": "Derek O'Brien",  "score": 48.33}]
```
Plus the raw transcript evidence in the audio. This is sufficient for "which of these best matches what you heard?"

**`parts_used` gap:** `FieldResult.top_candidates` is always `[]` for parts (discarded at `fuzzy_resolver.py:157`). To disambiguate parts tokens, either:
- Extend `resolve_parts_used` to retain per-token top-candidates in `FieldResult`, or
- Re-run `_find_best_match` on each token inside the disambiguation step

### Image pipeline

`ImagePipeline` does not override `_validate_fields` or `_run_veracity`. The skip is config-driven (see H4). For image to participate in disambiguation, **three prerequisites** must be met:
1. A `ValidationConfig` for image_v3 must be defined (currently `null`)
2. An image field validator analogous to `validate_work_order` for v3 schema fields (currently absent)
3. Either whole-page disambiguation (send full image to model) or per-field crop (requires coordinate template — not feasible today, see H5)

Until these are in place, image disambiguation would need to use the whole image, which is less discriminating but not structurally blocked.

---

## 5. Image Verdict

**Can image inputs participate in disambiguation now?** No.

**Blocking reasons:**
1. `image_v3.yaml` sets `validation: null` → `_validate_fields` returns `None` → no `FieldResult` objects are produced → no `top_candidates` to feed a disambiguation step
2. No image field validator exists; the image pipeline has never had fuzzy resolution
3. No positional/bounding-box data is produced or retained
4. The image extraction prompt does request `field__confidence` for 5 header fields (`company, client, worker, location, date`), so confidence markers are produced and parsed — but they go into the envelope unused (validation is null)

**Prerequisite work for image disambiguation:**
1. Define a `ValidationConfig` for image fields (which fields are fuzzy-resolvable from DB vs. free-text)
2. Implement an image field validator that maps v3 schema fields to reference data lookups
3. For whole-page disambiguation: feasible with existing infrastructure once #1-2 done
4. For per-field crops: define a coordinate template for the fixed Uni-Fix form layout (manual measurement or one-time OpenCV layout detection run)

**Note on confidence markers in the image prompt:** `image_extraction_v3.txt:61-67` instructs confidence for `company, client, worker, location, date`. These are parsed by `extract_confidence_markers` (same path as audio), but since `validation` is null, the `fuzzy_names` frozenset is empty (`base.py:68: frozenset(fc.name for fc in (self.config.validation.fuzzy_fields if self.config.validation else []))`), so no defaults are injected and confidences appear only if explicitly emitted. They are stored in the envelope but drive nothing.

---

## 6. Test Impact Inventory

### Must Change (break if redesign ships)

| Test file | Class / test | Why |
|---|---|---|
| `test_veracity.py` | `TestShouldRunVeracity` (8 tests) | Pins `VERACITY_TRIGGER_STATUSES = {"REVIEW","FAIL"}` and `VERACITY_TRIGGER_CONFIDENCE = {"LOW"}` as the exact trigger set. Any change to trigger logic (e.g. per-field triggers, NEW_ENTITY status) would require updating. |
| `test_veracity.py` | `TestRunVeracityCheck.test_no_transcript_uses_placeholder` | Asserts `"[See attached audio]"` appears in prompt — breaks if transcript handling changes |
| `test_edge_cases.py:569` | `test_low_confidence_worker_triggers_review` | Tests that a borderline worker ("J. Hartwell") enters REVIEW/PASS. If `+5` boost is removed and thresholds not retuned, the score distribution changes and this test's expectations may no longer hold. |

### Should Still Pass (do not touch)

| Test file | Class / tests | Why safe |
|---|---|---|
| `test_fuzzy_resolver.py` | All `TestWorkerResolution`, `TestCompanyResolution`, `TestLocationResolution`, `TestEquipmentResolution`, `TestPartsResolution` | Use `resolve_field()` / `resolve_parts_used()` directly, which bypass `resolve_field_with_gemini_confidence`. Unaffected by boost removal. |
| `test_fuzzy_resolver.py` | `TestTimeValidation` (all) | Time validator is independent |
| `test_fuzzy_resolver.py` | `TestFullWorkOrderValidation` (most) | Tests not passing explicit confidences use the default `confidences=None` path, which defaults to MEDIUM (boost=0). Scores unchanged. |
| `test_veracity.py` | `TestApplyVeracityCorrections` (10 tests) | Correction-application logic is independent of prompt/trigger changes |
| `test_image_processing.py` | All | Image pipeline independent |

### Boost Pinning: A Notable Absence

No test in the suite directly calls `resolve_field_with_gemini_confidence` or asserts a specific score delta from the boost. The boost mechanism (`fuzzy_resolver.py:177-195`) is **untested in isolation**. This means:
- Removing the boost will not cause any direct test failure from the boost logic itself
- Indirect effects (status changes in `TestFullWorkOrderValidation` when confidences are passed) would surface only if tests pass non-MEDIUM confidences explicitly

**Reusable mocking pattern for a disambiguation test:**
```python
# Same pattern as test_veracity.py; disambiguation will use call_gemini_generic
@patch("processor.veracity.call_gemini_generic")   # or new module path
def test_disambiguation_selects_correct_candidate(self, mock_call):
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "selected": "James Hartwell",
        "reasoning": "Name matches first speaker identification"
    })
    mock_call.return_value = mock_response
    provider = InMemoryProvider()
    # ... build FieldResult with top_candidates, call disambiguation ...
```
The `InMemoryProvider` fixture pattern from `test_fuzzy_resolver.py:12-13` provides real candidate lists without any DB dependency.

---

## 7. Config and Data Model Surface

### Adding a DisambiguationConfig block

Minimal change: add an optional `disambiguation` key to YAML alongside `veracity`. The `load_document_config` function (`config/__init__.py:80-85`) already handles optional keys gracefully. A new dataclass:

```python
@dataclass
class DisambiguationConfig:
    enabled: bool
    trigger_statuses: list[str]  # e.g. ["NO_MATCH", "LOW_CONF"]
    max_candidates: int          # default 3
```

`DocumentConfig` gains `disambiguation: DisambiguationConfig | None`. Toggling per document type requires only a YAML key change.

### Status model gaps for disambiguation outcomes

The current `MatchStatus` enum (`models.py:6-14`) has no value for "resolved via disambiguation" or "human review — new entity candidate". Two additions are needed:
- `DISAMBIGUATION_RESOLVED` — field was resolved by the grounded disambiguation prompt
- `NEW_ENTITY_CANDIDATE` — model selected "none of these"; the raw value is a candidate new entity that needs human confirmation

Without these, a disambiguation "none-of-these" result has nowhere to go except `NO_MATCH`, which triggers `FAIL` for required fields — but `FAIL` is already in use for cases where no candidate even matched fuzzily. The distinction matters for routing: `FAIL` on `NO_MATCH` should be re-tried by a human; `NEW_ENTITY_CANDIDATE` on `FAIL` signals that the entity may be legitimately new and should be added to the DB.

`compute_overall_status` (`resolution.py:15-41`) is idempotent and safe to re-run: it iterates `field_results` and recomputes from scratch with no mutable state. Calling it twice (pre- and post-disambiguation) is safe.

---

## 8. Open Risks and Unknowns

| Risk | Severity | Notes |
|---|---|---|
| `top_candidates` contains only 3 entries for most fields, but for `vehicle_equipment` the candidate pool has 30 strings (10 tags + 10 descriptions + 10 combined). Top-3 may be three representations of the same asset. | Medium | De-duplication by `id` (from canonical record) required before building the prompt. Currently `top_candidates` carries no `id`. |
| `parts_used` per-token top_candidates are discarded (`_` in `resolve_parts_used`). Disambiguation for parts cannot be built from existing `FieldResult` data. | High | Either extend `resolve_parts_used` to retain per-token candidates, or re-run matching inside the disambiguation step. |
| `VeracityConfig.trigger_statuses` and `trigger_confidences` YAML fields are loaded but never read. `should_run_veracity` hardcodes its own constants. | Low-Medium | Not a blocker, but a latent inconsistency. The config fields give a false impression of per-document-type trigger configurability. |
| No canonical record IDs in `top_candidates` — only names and scores. A disambiguation step that needs to resolve the winning candidate to a canonical `id` must call `provider.resolve_canonical()` after the model selects a name. This is straightforward but adds a call per disambiguation. | Low | Straightforward to add; note it. |
| Post-veracity envelope stores the pre-veracity `val_result` in `envelope["validation"]` and post-veracity in `envelope["veracity_pass"]["validation_post_veracity"]`. If disambiguation runs and succeeds, there will be three validation snapshots. The envelope schema has no designed slot for this. | Medium | Envelope schema will need a clear versioning/ordering convention. |
| Reference-data completeness is now load-bearing. With the current 8-worker / 6-company / 9-location fixture, stale entities in production are highly likely. Every new hire, new site, new equipment registration that hasn't been added to the DB becomes a "none-of-these" candidate, firing the new-entity workflow rather than resolving cleanly. | High | Operational: the effectiveness of grounded disambiguation is directly proportional to DB freshness. Document this as a production requirement, not just a tech concern. |
| `temperature=0.1` for the veracity call is hardcoded — not configurable from YAML. If a disambiguation step is added using `call_gemini_generic`, its temperature would also need to be carefully chosen (probably lower than the 0.2 extraction temperature). | Low | Note it; easy to parameterize. |

---

## 9. Recommended Next Step

**Go/No-Go framing:** The investigation confirms the redesign is directionally correct and architecturally feasible. The confidence boost at `fuzzy_resolver.py:188-193` is the smallest, highest-leverage change: removing or gating it eliminates the compounding failure mode immediately, at the cost of a modest threshold retune and one cluster of edge-case tests that need updating. That retune should be prototyped first by running the full test suite with the boost zeroed out to measure how many tests change status — the delta will calibrate what threshold adjustments are needed.

The disambiguation stage is best introduced as a new `_disambiguate_fields` hook in `BasePipeline` (default no-op), overridden in `AudioPipeline`. The `top_candidates` schema is sufficient for non-parts fields today; `parts_used` disambiguation requires a minor resolver extension before it can participate. The "none-of-these" branch requires two new `MatchStatus` values and one additional `FieldResult` flag — small data-model changes that should be done before writing any disambiguation prompt, since they define the contract the rest of the system depends on.

The image pipeline is not a prerequisite blocker for the audio/video redesign; it has its own prerequisite chain (validation config, field validator, optional cropping) and should be scoped separately. Ship the audio/video disambiguation first, then revisit image once the base pattern is proven.
