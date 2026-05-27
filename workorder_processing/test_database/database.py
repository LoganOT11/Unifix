"""
Test reference database for fuzzy matching extracted work-order fields.

Uses SQLite + FTS5 (full-text search) so no extra dependencies are needed.
Fuzzy matching is handled via FTS5 prefix queries with a pure-Python
SequenceMatcher fallback (from difflib, in the stdlib).

Quick start:
    python database.py --seed      # create + populate the test database
    python database.py             # just create empty tables

Usage from main.py:
    from test_database.database import get_db, fuzzy_match
    rows = fuzzy_match(get_db(), "equipment", "Cat D6T")
"""

import os
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
DB_DIR = Path(__file__).resolve().parent
DB_PATH = DB_DIR / "test_data.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Equipment / vehicles ------------------------------------------------
CREATE TABLE IF NOT EXISTS equipment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    category    TEXT,           -- dozer, excavator, truck, crane, etc.
    manufacturer TEXT           -- Caterpillar, Komatsu, John Deere, etc.
);

CREATE VIRTUAL TABLE IF NOT EXISTS equipment_fts USING fts5(
    name, category, manufacturer,
    content=equipment, content_rowid=id
);

-- Companies (vendors, manufacturers, clients) --------------------------
CREATE TABLE IF NOT EXISTS companies (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL,
    type    TEXT,               -- manufacturer, vendor, client, dealer
    city    TEXT,
    state   TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS companies_fts USING fts5(
    name, type, city, state,
    content=companies, content_rowid=id
);

-- Locations / job sites ------------------------------------------------
CREATE TABLE IF NOT EXISTS locations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL,
    address TEXT,
    city    TEXT,
    state   TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS locations_fts USING fts5(
    name, address, city, state,
    content=locations, content_rowid=id
);

-- Parts catalog --------------------------------------------------------
CREATE TABLE IF NOT EXISTS parts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT    NOT NULL,
    description TEXT,
    category    TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS parts_fts USING fts5(
    part_number, description, category,
    content=parts, content_rowid=id
);
"""

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
SEED_EQUIPMENT = [
    ("Caterpillar D6T Dozer", "dozer", "Caterpillar"),
    ("Caterpillar D8T Dozer", "dozer", "Caterpillar"),
    ("Caterpillar 320 Excavator", "excavator", "Caterpillar"),
    ("Caterpillar 336 Excavator", "excavator", "Caterpillar"),
    ("Caterpillar 140 Motor Grader", "grader", "Caterpillar"),
    ("Komatsu PC200 Excavator", "excavator", "Komatsu"),
    ("Komatsu PC360 Excavator", "excavator", "Komatsu"),
    ("Komatsu D65 Dozer", "dozer", "Komatsu"),
    ("John Deere 850L Dozer", "dozer", "John Deere"),
    ("John Deere 210G Excavator", "excavator", "John Deere"),
    ("Volvo A40G Articulated Hauler", "hauler", "Volvo"),
    ("Volvo EC220E Excavator", "excavator", "Volvo"),
    ("Hitachi ZX350 Excavator", "excavator", "Hitachi"),
    ("Terex TA400 Hauler", "hauler", "Terex"),
    ("Bobcat S650 Skid Steer", "skid steer", "Bobcat"),
    ("Bobcat T770 Track Loader", "track loader", "Bobcat"),
    ("Ford F-550 Service Truck", "service truck", "Ford"),
    ("Ford F-150 Crew Cab", "light truck", "Ford"),
    ("Chevrolet Silverado 3500", "light truck", "Chevrolet"),
    ("Kenworth T880 Dump Truck", "dump truck", "Kenworth"),
    ("Grove RT540 Crane", "crane", "Grove"),
    ("Manitowoc 999 Crawler Crane", "crane", "Manitowoc"),
    ("Genie Z-60 Boom Lift", "aerial lift", "Genie"),
    ("JLG 660SJ Boom Lift", "aerial lift", "JLG"),
    ("Ingersoll Rand P185 Air Compressor", "compressor", "Ingersoll Rand"),
    ("Atlas Copco XAS 185 Compressor", "compressor", "Atlas Copco"),
]

SEED_COMPANIES = [
    ("Caterpillar Inc.", "manufacturer", "Peoria", "IL"),
    ("Komatsu Ltd.", "manufacturer", "Chicago", "IL"),
    ("John Deere Construction", "manufacturer", "Moline", "IL"),
    ("Volvo Construction Equipment", "manufacturer", "Shippensburg", "PA"),
    ("Hitachi Construction Machinery", "manufacturer", "Newnan", "GA"),
    ("United Rentals", "vendor", "Stamford", "CT"),
    ("Sunbelt Rentals", "vendor", "Fort Mill", "SC"),
    ("Herc Rentals", "vendor", "Bonita Springs", "FL"),
    ("Foley Equipment", "dealer", "Wichita", "KS"),
    ("Wheeler Machinery", "dealer", "Salt Lake City", "UT"),
    ("Empire Southwest", "dealer", "Mesa", "AZ"),
    ("Holt of California", "dealer", "Stockton", "CA"),
    ("NAPA Auto Parts", "parts supplier", "Atlanta", "GA"),
    ("Grainger Industrial Supply", "parts supplier", "Lake Forest", "IL"),
    ("Fastenal", "parts supplier", "Winona", "MN"),
    ("Acme Heavy Industries", "client", "Denver", "CO"),
    ("Summit Mining Corp", "client", "Elko", "NV"),
    ("Red Rock Construction", "client", "Phoenix", "AZ"),
    ("Valley Paving Inc.", "client", "Fresno", "CA"),
    ("Gulf States Pipeline", "client", "Houston", "TX"),
]

SEED_LOCATIONS = [
    ("Denver Maintenance Yard", "4500 York St", "Denver", "CO"),
    ("Phoenix Service Center", "2430 W Lower Buckeye Rd", "Phoenix", "AZ"),
    ("Houston Heavy Equipment Depot", "8900 Wallisville Rd", "Houston", "TX"),
    ("Salt Lake City Repair Facility", "210 S 1000 W", "Salt Lake City", "UT"),
    ("Elko Mine Service Shop", "1750 Mountain City Hwy", "Elko", "NV"),
    ("North Platte Field Office", "1200 S Dewey St", "North Platte", "NE"),
    ("Dallas Equipment Yard", "4300 Irving Blvd", "Dallas", "TX"),
    ("Reno Service Bay", "2100 E 4th St", "Reno", "NV"),
    ("Tucson Repair Depot", "3900 E Ajo Way", "Tucson", "AZ"),
    ("Fresno Maintenance Hub", "2700 S Orange Ave", "Fresno", "CA"),
    ("Albuquerque Service Center", "5000 Broadway Blvd SE", "Albuquerque", "NM"),
    ("Boise Equipment Shop", "3100 Gowen Rd", "Boise", "ID"),
    ("Cheyenne Field Garage", "500 W 15th St", "Cheyenne", "WY"),
    ("Billings Heavy Repair", "3100 Hwy 87 E", "Billings", "MT"),
    ("Oklahoma City Service Center", "700 S Council Rd", "Oklahoma City", "OK"),
]

SEED_PARTS = [
    ("CAT-6T-7489", "Track chain assembly - D6T", "undercarriage"),
    ("CAT-6T-7490", "Track shoe bolt kit - D6T", "undercarriage"),
    ("CAT-320-5512", "Hydraulic pump - 320 excavator", "hydraulics"),
    ("CAT-320-8821", "Swing bearing - 320 excavator", "swing system"),
    ("KOM-PC200-3310", "Bucket cylinder seal kit - PC200", "hydraulics"),
    ("KOM-D65-2210", "Transmission filter - D65", "transmission"),
    ("JD-850L-1120", "Blade lift cylinder - 850L", "hydraulics"),
    ("JD-210G-4450", "Fuel injector - 210G", "engine"),
    ("VOL-A40G-7780", "Brake disc kit - A40G", "brakes"),
    ("VOL-EC220-3310", "Boom cylinder - EC220E", "hydraulics"),
    ("HIT-ZX350-8810", "Main relief valve - ZX350", "hydraulics"),
    ("BOB-S650-2200", "Drive chain - S650", "drive train"),
    ("BOB-T770-3100", "Lift arm bushing kit - T770", "loader arms"),
    ("IR-P185-5500", "Air end seal kit - P185", "compressor"),
    ("ATL-XAS185-4400", "Separator element - XAS185", "compressor"),
    ("GEN-Z60-1150", "Platform control board - Z-60", "electrical"),
    ("JLG-660SJ-2200", "Drive motor - 660SJ", "drive system"),
    ("GRO-RT540-6610", "Outrigger cylinder - RT540", "stabilizers"),
    ("FOR-F550-8800", "Glow plug set - 6.7L Power Stroke", "engine"),
    ("CHEV-3500-2200", "Transmission cooler - 3500HD", "cooling"),
]

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    """Return a connection to the test database (creates if missing)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and FTS indexes if they don't exist."""
    conn.executescript(SCHEMA)


def seed_db(conn: sqlite3.Connection) -> None:
    """Populate the database with example data (idempotent – skips if data exists)."""
    init_db(conn)

    datasets = [
        ("equipment", SEED_EQUIPMENT),
        ("companies", SEED_COMPANIES),
        ("locations", SEED_LOCATIONS),
        ("parts", SEED_PARTS),
    ]

    for table, rows in datasets:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        if cur.fetchone()[0] > 0:
            continue  # already seeded

        if table == "equipment":
            conn.executemany(
                "INSERT INTO equipment (name, category, manufacturer) VALUES (?, ?, ?)",
                rows,
            )
        elif table == "companies":
            conn.executemany(
                "INSERT INTO companies (name, type, city, state) VALUES (?, ?, ?, ?)",
                rows,
            )
        elif table == "locations":
            conn.executemany(
                "INSERT INTO locations (name, address, city, state) VALUES (?, ?, ?, ?)",
                rows,
            )
        else:  # parts
            conn.executemany(
                "INSERT INTO parts (part_number, description, category) VALUES (?, ?, ?)",
                rows,
            )

        # Rebuild FTS index for this table
        conn.execute(f"INSERT INTO {table}_fts({table}_fts) VALUES ('rebuild')")
        print(f"  ✔  Seeded {len(rows)} rows into '{table}'")

    conn.commit()


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------
def _similarity(a: str, b: str) -> float:
    """Return a 0–1 similarity score between two strings (pure stdlib)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def fuzzy_match(
    conn: sqlite3.Connection,
    table: str,
    query: str,
    threshold: float = 0.6,
    limit: int = 5,
) -> list[dict]:
    """
    Fuzzy-match *query* against *table* and return the best candidates.

    Strategy:
      1. FTS5 prefix query to quickly narrow the field.
      2. Score each candidate with SequenceMatcher.
      3. Return results above *threshold*, sorted by score desc.

    Parameters
    ----------
    conn      : SQLite connection
    table     : one of 'equipment', 'companies', 'locations', 'parts'
    query     : the user-provided / AI-extracted string to match against
    threshold : minimum similarity score (0–1)
    limit     : max results to return

    Returns
    -------
    List of dicts with keys: id, name, score, plus table-specific fields.
    """
    # Build a safe FTS prefix query: each word gets a trailing *
    tokens = [f'"{token}"*' for token in query.split() if token]
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)

    # Determine the display-name column per table
    name_col = {
        "equipment": "name",
        "companies": "name",
        "locations": "name",
        "parts": "part_number",
    }.get(table, "name")

    try:
        rows = conn.execute(
            f"""
            SELECT t.id, t.{name_col} AS name, t.*
            FROM {table}_fts f
            JOIN {table} t ON f.rowid = t.id
            WHERE {table}_fts MATCH ?
            LIMIT ?
            """,
            (fts_query, limit * 3),  # over-fetch; we re-score below
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS query syntax error – fall back to full scan on small tables
        rows = conn.execute(
            f"SELECT id, {name_col} AS name, * FROM {table} LIMIT ?",
            (limit * 3,),
        ).fetchall()

    # Score and filter
    scored = []
    for row in rows:
        score = _similarity(query, row["name"])
        if score >= threshold:
            d = dict(row)
            d["score"] = round(score, 3)
            scored.append(d)

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    conn = get_db()
    init_db(conn)

    if "--seed" in sys.argv:
        print("🌱  Seeding test database …")
        seed_db(conn)
        print(f"✅  Done  →  {DB_PATH}")
    elif "--query" in sys.argv:
        # Quick test: python database.py --query equipment "Cat dozer"
        try:
            idx = sys.argv.index("--query")
            table = sys.argv[idx + 1]
            query = sys.argv[idx + 2]
        except (IndexError, ValueError):
            print("Usage: python database.py --query <table> <search-string>")
            sys.exit(1)

        results = fuzzy_match(conn, table, query)
        for r in results:
            print(f"  {r['name']:50s}  score={r['score']}")
        if not results:
            print("  (no matches)")
    else:
        print(f"✅  Database ready at {DB_PATH}")
        print("    Use --seed to populate with example data.")
        print("    Use --query <table> <term> to test fuzzy matching.")

    conn.close()
