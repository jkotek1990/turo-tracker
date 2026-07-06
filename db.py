import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "turo.db"

CATEGORIES_INCOME = ["Trip payout", "Reimbursement", "Referral bonus", "Other income"]
CATEGORIES_EXPENSE = [
    "Maintenance",
    "Cleaning",
    "Insurance",
    "Loan payment",
    "Fuel",
    "Mileage",
    "Turo service fee",
    "Registration/Tax",
    "Tolls/Parking/Citations",
    "Storage",
    "Supplies",
    "Other expense",
]

# Last rate confirmed from irs.gov/tax-professionals/standard-mileage-rates (2025 business rate).
# The IRS updates this annually, usually announced in December; update it any time in Settings.
DEFAULT_MILEAGE_RATE = 0.70

# Fleet-wide default oil-change interval; each car can override this individually.
DEFAULT_OIL_CHANGE_INTERVAL = 4000

# Fleet-wide default tire-replacement interval; each car can override this individually.
# Typical passenger tires last roughly 25k-50k miles depending on tire type/driving -
# adjust to whatever your tires actually spec.
DEFAULT_TIRE_CHANGE_INTERVAL = 30000


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)", (key, value))


def _ensure_column(conn, table, column, ddl):
    """Add a column to an existing table if it's not already there (idempotent migration)."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            make TEXT,
            model TEXT,
            year INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            turo_vehicle_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('income','expense')),
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            notes TEXT,
            reservation_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (car_id) REFERENCES cars(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS paybacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (car_id) REFERENCES cars(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS general_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS odometer_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            reading REAL NOT NULL,
            oil_change INTEGER NOT NULL DEFAULT 0,
            reservation_id TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (car_id) REFERENCES cars(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS car_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            pct REAL NOT NULL DEFAULT 0,
            UNIQUE(car_id, user_id),
            FOREIGN KEY (car_id) REFERENCES cars(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            notes TEXT,
            created_by_user_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_car_id ON transactions(car_id);
        CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_transactions_reservation_id ON transactions(reservation_id);
        CREATE INDEX IF NOT EXISTS idx_cars_turo_vehicle_id ON cars(turo_vehicle_id);
        CREATE INDEX IF NOT EXISTS idx_paybacks_car_id ON paybacks(car_id);
        CREATE INDEX IF NOT EXISTS idx_general_expenses_date ON general_expenses(date);
        CREATE INDEX IF NOT EXISTS idx_odometer_logs_car_id ON odometer_logs(car_id);
        CREATE INDEX IF NOT EXISTS idx_odometer_logs_reservation_id ON odometer_logs(reservation_id);
        CREATE INDEX IF NOT EXISTS idx_car_ownership_car_id ON car_ownership(car_id);
        CREATE INDEX IF NOT EXISTS idx_distributions_user_id ON distributions(user_id);
        CREATE INDEX IF NOT EXISTS idx_distributions_date ON distributions(date);
        """
    )
    # Migrations for DBs created before these columns existed.
    _ensure_column(conn, "cars", "turo_vehicle_id", "turo_vehicle_id TEXT")
    _ensure_column(conn, "transactions", "reservation_id", "reservation_id TEXT")
    _ensure_column(conn, "cars", "funding_type", "funding_type TEXT")
    _ensure_column(conn, "cars", "owed_initial", "owed_initial REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "transactions", "paid_by_company", "paid_by_company INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "general_expenses", "paid_by_company", "paid_by_company INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "transactions", "miles", "miles REAL")
    _ensure_column(conn, "transactions", "mileage_rate", "mileage_rate REAL")
    _ensure_column(conn, "general_expenses", "miles", "miles REAL")
    _ensure_column(conn, "general_expenses", "mileage_rate", "mileage_rate REAL")
    _ensure_column(conn, "cars", "oil_change_interval_miles", "oil_change_interval_miles INTEGER")
    _ensure_column(conn, "cars", "owed_to_user_id", "owed_to_user_id INTEGER")
    _ensure_column(conn, "transactions", "reimburse_to_user_id", "reimburse_to_user_id INTEGER")
    _ensure_column(conn, "transactions", "created_by_user_id", "created_by_user_id INTEGER")
    _ensure_column(conn, "general_expenses", "reimburse_to_user_id", "reimburse_to_user_id INTEGER")
    _ensure_column(conn, "general_expenses", "created_by_user_id", "created_by_user_id INTEGER")
    _ensure_column(conn, "paybacks", "created_by_user_id", "created_by_user_id INTEGER")
    _ensure_column(conn, "cars", "purchase_price", "purchase_price REAL")
    _ensure_column(conn, "cars", "placed_in_service_date", "placed_in_service_date TEXT")
    _ensure_column(conn, "cars", "business_use_pct", "business_use_pct REAL NOT NULL DEFAULT 100")
    # Vehicle documents & compliance.
    _ensure_column(conn, "cars", "vin", "vin TEXT")
    _ensure_column(conn, "cars", "license_plate", "license_plate TEXT")
    _ensure_column(conn, "cars", "registration_expiration", "registration_expiration TEXT")
    _ensure_column(conn, "cars", "inspection_due_date", "inspection_due_date TEXT")
    # Tire replacement tracking (mirrors oil-change interval pattern).
    _ensure_column(conn, "cars", "tire_change_interval_miles", "tire_change_interval_miles INTEGER")
    _ensure_column(conn, "odometer_logs", "tire_change", "tire_change INTEGER NOT NULL DEFAULT 0")
    # Insurance policy tracking.
    _ensure_column(conn, "cars", "insurance_carrier", "insurance_carrier TEXT")
    _ensure_column(conn, "cars", "insurance_policy_number", "insurance_policy_number TEXT")
    _ensure_column(conn, "cars", "insurance_type", "insurance_type TEXT")
    _ensure_column(conn, "cars", "insurance_renewal_date", "insurance_renewal_date TEXT")
    # Loan principal/interest split on paybacks (only interest is tax-deductible).
    _ensure_column(conn, "paybacks", "interest_amount", "interest_amount REAL NOT NULL DEFAULT 0")
    # Trip end date (for utilization reporting) + optional protection plan tier per trip.
    _ensure_column(conn, "transactions", "trip_end_date", "trip_end_date TEXT")
    _ensure_column(conn, "transactions", "protection_plan", "protection_plan TEXT")
    conn.commit()
    conn.close()
