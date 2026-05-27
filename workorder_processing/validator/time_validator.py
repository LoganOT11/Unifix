import re
from .models import FieldResult, MatchStatus

_PATTERNS = [
    (re.compile(r'^(\d{1,2}):(\d{2})$'), lambda m: (int(m.group(1)), int(m.group(2)))),
    (re.compile(r'^(\d{1,2}):(\d{2}):\d{2}$'), lambda m: (int(m.group(1)), int(m.group(2)))),
    (re.compile(r'^(\d{1,2})h(\d{2})m?$', re.I), lambda m: (int(m.group(1)), int(m.group(2)))),
    (re.compile(r'^(\d{1,2}):(\d{2})\s*(am|pm)$', re.I),
        lambda m: (
            (int(m.group(1)) % 12) + (12 if m.group(3).lower() == 'pm' else 0),
            int(m.group(2))
        )
    ),
]

_DURATION_PATTERNS = [
    re.compile(r'(\d+)\s*h(?:our)?s?\s*(\d+)\s*m(?:in)?', re.I),
    re.compile(r'(\d+)\s*h(?:our)?s?$', re.I),
    re.compile(r'(\d+)\s*m(?:in(?:ute)?s?)$', re.I),
    re.compile(r'^(\d{1,2}):(\d{2})$'),
]


def _parse_clock_time(value: str) -> str | None:
    v = value.strip()
    for pattern, extractor in _PATTERNS:
        m = pattern.match(v)
        if m:
            h, minutes = extractor(m)
            if 0 <= h <= 23 and 0 <= minutes <= 59:
                return f"{h:02d}:{minutes:02d}"
    return None


def _parse_duration(value: str) -> str | None:
    v = value.strip()

    m = _DURATION_PATTERNS[0].search(v)
    if m:
        return f"{int(m.group(1))}h {int(m.group(2))}m"

    m = _DURATION_PATTERNS[1].match(v)
    if m:
        return f"{int(m.group(1))}h 0m"

    m = _DURATION_PATTERNS[2].match(v)
    if m:
        mins = int(m.group(1))
        return f"{mins // 60}h {mins % 60}m"

    m = _DURATION_PATTERNS[3].match(v)
    if m:
        return f"{int(m.group(1))}h {int(m.group(2))}m"

    return None


def validate_time_field(field_name: str, raw_value: str) -> FieldResult:
    if not raw_value or not raw_value.strip():
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value="",
            status=MatchStatus.EMPTY,
            notes="Time field is empty.",
        )

    is_duration = field_name == "total_time_spent"
    parsed = _parse_duration(raw_value) if is_duration else _parse_clock_time(raw_value)

    if parsed:
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value=parsed,
            status=MatchStatus.TIME_VALID,
            score=100.0,
            notes=f"Normalised from '{raw_value}' → '{parsed}'",
        )
    else:
        return FieldResult(
            field_name=field_name,
            raw_value=raw_value,
            resolved_value=raw_value,
            status=MatchStatus.TIME_INVALID,
            score=0.0,
            notes=f"Could not parse '{raw_value}' as a {'duration' if is_duration else 'clock time'}.",
        )
