WORKERS: list[dict] = [
    {"id": "W001", "name": "James Hartwell",     "department": "Heavy Equipment"},
    {"id": "W002", "name": "Maria Sanchez",       "department": "Electrical"},
    {"id": "W003", "name": "Derek O'Brien",       "department": "Heavy Equipment"},
    {"id": "W004", "name": "Priya Nambiar",       "department": "Hydraulics"},
    {"id": "W005", "name": "Tom Kowalski",        "department": "General Maintenance"},
    {"id": "W006", "name": "Fatima Al-Hassan",    "department": "Electrical"},
    {"id": "W007", "name": "Luc Tremblay",        "department": "Heavy Equipment"},
    {"id": "W008", "name": "Sandra McPherson",    "department": "General Maintenance"},
]

COMPANIES: list[dict] = [
    {"id": "C001", "name": "Hartwell Industrial Services",   "short": "HIS"},
    {"id": "C002", "name": "PrimeTech Maintenance Ltd",      "short": "PTM"},
    {"id": "C003", "name": "Northern Fleet Solutions",       "short": "NFS"},
    {"id": "C004", "name": "Apex Equipment & Repair Co.",    "short": "AER"},
    {"id": "C005", "name": "BlueLine Service Group",         "short": "BSG"},
    {"id": "C006", "name": "Delta Hydraulics Inc.",          "short": "DHI"},
]

LOCATIONS: list[dict] = [
    {"id": "L001", "name": "Main Workshop — Bay 1"},
    {"id": "L002", "name": "Main Workshop — Bay 2"},
    {"id": "L003", "name": "Main Workshop — Bay 3"},
    {"id": "L004", "name": "North Depot — Outdoor Lot"},
    {"id": "L005", "name": "South Depot — Covered Bay"},
    {"id": "L006", "name": "Field Site Alpha"},
    {"id": "L007", "name": "Field Site Beta"},
    {"id": "L008", "name": "Client Site — Hartwell Industrial"},
    {"id": "L009", "name": "Fuel Station — Zone A"},
]

EQUIPMENT: list[dict] = [
    {"id": "E001", "tag": "TRK-001", "description": "2019 Kenworth T680 — Unit 1"},
    {"id": "E002", "tag": "TRK-002", "description": "2020 Peterbilt 579 — Unit 2"},
    {"id": "E003", "tag": "EXC-010", "description": "2021 CAT 320 Excavator"},
    {"id": "E004", "tag": "EXC-011", "description": "2018 Komatsu PC360 Excavator"},
    {"id": "E005", "tag": "LDR-003", "description": "2022 Volvo L110H Wheel Loader"},
    {"id": "E006", "tag": "GEN-005", "description": "250kVA Cummins Generator — Site A"},
    {"id": "E007", "tag": "GEN-006", "description": "500kVA Caterpillar Generator — Site B"},
    {"id": "E008", "tag": "TRL-020", "description": "Flatbed Trailer — 48ft"},
    {"id": "E009", "tag": "FRK-007", "description": "Toyota 8FBU25 Forklift"},
    {"id": "E010", "tag": "VAN-012", "description": "2021 Ford Transit Service Van"},
]

PARTS: list[dict] = [
    {"id": "P001", "part_number": "HF-2240",    "description": "Hydraulic Filter 2240"},
    {"id": "P002", "part_number": "OIL-15W40",  "description": "15W-40 Engine Oil (5L)"},
    {"id": "P003", "part_number": "BLT-SERP",   "description": "Serpentine Drive Belt"},
    {"id": "P004", "part_number": "ALT-24V",    "description": "24V Alternator"},
    {"id": "P005", "part_number": "BATT-12V",   "description": "12V AGM Battery"},
    {"id": "P006", "part_number": "BATT-24V",   "description": "24V AGM Battery"},
    {"id": "P007", "part_number": "SEAL-HYD",   "description": "Hydraulic Cylinder Seal Kit"},
    {"id": "P008", "part_number": "HOSE-HYD",   "description": "High-Pressure Hydraulic Hose"},
    {"id": "P009", "part_number": "FILT-AIR",   "description": "Air Filter — Heavy Duty"},
    {"id": "P010", "part_number": "FILT-OIL",   "description": "Oil Filter — Heavy Duty"},
    {"id": "P011", "part_number": "PUMP-HYD",   "description": "Hydraulic Pump Assembly"},
    {"id": "P012", "part_number": "BRAKE-PAD",  "description": "Brake Pad Set — Front"},
    {"id": "P013", "part_number": "COOL-L",     "description": "Coolant — Long Life (5L)"},
    {"id": "P014", "part_number": "GREASE-MP",  "description": "Multi-Purpose Grease Cartridge"},
    {"id": "P015", "part_number": "FUSE-30A",   "description": "30A Blade Fuse (Pack of 10)"},
]


def get_worker_names() -> list[str]:
    return [w["name"] for w in WORKERS]

def get_company_names() -> list[str]:
    names = [c["name"] for c in COMPANIES]
    shorts = [c["short"] for c in COMPANIES]
    return names + shorts

def get_location_names() -> list[str]:
    return [loc["name"] for loc in LOCATIONS]

def get_equipment_strings() -> list[str]:
    tags = [e["tag"] for e in EQUIPMENT]
    descs = [e["description"] for e in EQUIPMENT]
    combined = [f"{e['tag']} — {e['description']}" for e in EQUIPMENT]
    return tags + descs + combined

def get_part_strings() -> list[str]:
    numbers = [p["part_number"] for p in PARTS]
    descs = [p["description"] for p in PARTS]
    combined = [f"{p['part_number']} — {p['description']}" for p in PARTS]
    return numbers + descs + combined

def resolve_worker_canonical(matched_name: str) -> dict | None:
    for w in WORKERS:
        if w["name"] == matched_name:
            return w
    return None

def resolve_equipment_canonical(matched_string: str) -> dict | None:
    for e in EQUIPMENT:
        if matched_string in (e["tag"], e["description"], f"{e['tag']} — {e['description']}"):
            return e
    return None

def resolve_part_canonical(matched_string: str) -> dict | None:
    for p in PARTS:
        if matched_string in (p["part_number"], p["description"],
                               f"{p['part_number']} — {p['description']}"):
            return p
    return None

def resolve_location_canonical(matched_name: str) -> dict | None:
    for loc in LOCATIONS:
        if loc["name"] == matched_name:
            return loc
    return None

def resolve_company_canonical(matched_name: str) -> dict | None:
    for c in COMPANIES:
        if c["name"] == matched_name or c["short"] == matched_name:
            return c
    return None
