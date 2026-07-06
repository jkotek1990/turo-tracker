"""Parsing helpers for Turo's "Trip earnings" CSV export.

Turo's export includes one row per reservation with a running list of fee/
discount columns; the "Total earnings" column is already the net amount paid
to the host, so each row becomes a single income transaction.
"""
import csv
import io
import re
from datetime import datetime

REQUIRED_COLUMNS = {
    "Reservation ID",
    "Vehicle",
    "Vehicle name",
    "Vehicle id",
    "Guest",
    "Trip start",
    "Trip status",
    "Total earnings",
}

# Statuses for trips that haven't actually happened yet / earned nothing.
# Rows with these statuses are still shown in the preview but unchecked by
# default so the user can decide.
FUTURE_STATUS = {"Booked"}
REALIZED_STATUSES = {"Completed", "In-progress"}


class TuroCsvError(ValueError):
    pass


def _parse_money(raw):
    if raw is None:
        return 0.0
    s = raw.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_number(raw):
    """Plain numeric field (e.g. odometer reading). Returns None if blank/unparseable,
    since Turo leaves this blank for trips with no odometer recorded (bookings,
    cancellations, etc.)."""
    if raw is None:
        return None
    s = raw.strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_trip_date(raw):
    """Turo dates look like '2026-06-07 10:30 AM'. Fall back gracefully."""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_vehicle_name(name):
    """Best-effort split of e.g. 'Buick Encore 2014' into make/model/year."""
    parts = (name or "").split()
    year = None
    if parts and re.fullmatch(r"(19|20)\d{2}", parts[-1]):
        year = int(parts[-1])
        parts = parts[:-1]
    make = parts[0] if parts else ""
    model = " ".join(parts[1:]) if len(parts) > 1 else ""
    return make, model, year


def default_checked(status, amount):
    if status in REALIZED_STATUSES:
        return True
    if status in FUTURE_STATUS:
        return False
    # e.g. a guest cancellation that still paid the host a cancellation fee
    return amount != 0


def parse_csv(file_bytes):
    """Parse the raw uploaded file bytes into a list of row dicts.

    Raises TuroCsvError if the file doesn't look like a Turo trip earnings
    export. Malformed individual rows (missing reservation id) are skipped
    and counted, not raised.
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        raise TuroCsvError(
            "This doesn't look like a Turo trip earnings export "
            f"(missing columns: {', '.join(sorted(missing))})."
        )

    rows = []
    skipped = 0
    for raw in reader:
        reservation_id = (raw.get("Reservation ID") or "").strip()
        vehicle_id = (raw.get("Vehicle id") or "").strip()
        date = _parse_trip_date(raw.get("Trip start"))
        if not reservation_id or not vehicle_id or not date:
            skipped += 1
            continue

        status = (raw.get("Trip status") or "").strip()
        amount = _parse_money(raw.get("Total earnings"))
        guest = (raw.get("Guest") or "").strip()
        checkout_odometer = _parse_number(raw.get("Check-out odometer"))
        odometer_date = _parse_trip_date(raw.get("Trip end")) or date

        rows.append(
            {
                "reservation_id": reservation_id,
                "vehicle_id": vehicle_id,
                "vehicle_title": (raw.get("Vehicle") or "").strip(),
                "vehicle_name": (raw.get("Vehicle name") or "").strip(),
                "guest": guest,
                "date": date,
                "status": status,
                "amount": amount,
                "notes": f"Guest: {guest} • {status} • Res #{reservation_id}",
                "checked": default_checked(status, amount),
                "checkout_odometer": checkout_odometer,
                "odometer_date": odometer_date,
            }
        )

    return rows, skipped
