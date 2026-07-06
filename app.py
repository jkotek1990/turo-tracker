import calendar
import csv
import io
import json
from datetime import date, datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, Response, session
from werkzeug.security import generate_password_hash, check_password_hash

from db import (
    get_db,
    init_db,
    get_meta,
    set_meta,
    CATEGORIES_INCOME,
    CATEGORIES_EXPENSE,
    DEFAULT_MILEAGE_RATE,
    DEFAULT_OIL_CHANGE_INTERVAL,
    DEFAULT_TIRE_CHANGE_INTERVAL,
)
from turo_import import parse_csv, parse_vehicle_name, TuroCsvError

# Insurance policy "type" options, shown as a dropdown on the car form.
INSURANCE_TYPES = ["Personal", "Commercial", "Turo's policy"]

app = Flask(__name__)
app.secret_key = "turo-tracker-local-secret"  # local-only app, fine to hardcode
app.permanent_session_lifetime = timedelta(days=30)

# Bump this (e.g. 0.1 -> 0.2) any time a feature/fix is added, so the version shown
# in the corner of every page reflects the latest change. Stays under 1.0 until this
# is considered fully productionized; only then does it become 1.0.
APP_VERSION = "0.92"

# Routes reachable without being logged in. "static" serves CSS/JS; "setup" bootstraps
# the first account when the users table is empty; "login" is the login form itself.
PUBLIC_ENDPOINTS = {"static", "setup", "login"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    conn = get_db()
    user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    conn.close()
    if user_count == 0:
        return redirect(url_for("setup"))
    if not session.get("user_id"):
        return redirect(url_for("login"))


@app.context_processor
def inject_version():
    return {"app_version": APP_VERSION}


@app.context_processor
def inject_business_name():
    conn = get_db()
    name = get_business_name(conn)
    conn.close()
    return {"business_name": name}


@app.context_processor
def inject_current_user():
    uid = session.get("user_id")
    if not uid:
        return {"current_user": None}
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return {"current_user": user}


def all_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY display_name, username").fetchall()


def user_label(user_row):
    """Best display name for a user row (or a joined user via a *_user_id column)."""
    if user_row is None:
        return None
    return user_row["display_name"] or user_row["username"]


@app.route("/setup", methods=["GET", "POST"])
def setup():
    conn = get_db()
    user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if user_count > 0:
        conn.close()
        return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        display_name = request.form.get("display_name", "").strip()
        if not username or not password:
            conn.close()
            flash("Username and password are required.", "error")
            return redirect(url_for("setup"))
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), display_name or username),
        )
        conn.commit()
        conn.close()
        flash("Account created. Log in to continue.", "success")
        return redirect(url_for("login"))
    conn.close()
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if user_count == 0:
        conn.close()
        return redirect(url_for("setup"))
    if request.method == "POST":
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (request.form.get("username", "").strip(),)
        ).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user_label(user)}.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
        return redirect(url_for("login"))
    conn.close()
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
def manage_users():
    conn = get_db()
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        display_name = request.form.get("display_name", "").strip()
        if not username or not password:
            flash("Username and password are required.", "error")
        elif conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            flash("That username is already taken.", "error")
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), display_name or username),
            )
            conn.commit()
            flash("User added.", "success")
        conn.close()
        return redirect(url_for("manage_users"))
    users = all_users(conn)
    conn.close()
    return render_template("users.html", users=users)


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    if user_id == session.get("user_id"):
        flash("You can't delete the account you're currently logged in as.", "error")
        return redirect(url_for("manage_users"))
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if count <= 1:
        conn.close()
        flash("Can't delete the last remaining user.", "error")
        return redirect(url_for("manage_users"))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User deleted.", "success")
    return redirect(url_for("manage_users"))


def tax_years_available(conn):
    rows = conn.execute(
        """
        SELECT DISTINCT y FROM (
            SELECT strftime('%Y', date) AS y FROM transactions
            UNION
            SELECT strftime('%Y', date) AS y FROM general_expenses
        )
        WHERE y IS NOT NULL
        ORDER BY y DESC
        """
    ).fetchall()
    return [r["y"] for r in rows]


MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def monthly_totals(conn, year):
    """Income/expense/net per calendar month for one year, combining per-car
    transactions and general (fleet-wide) expenses, same as the tax export."""
    months = [{"month": f"{i:02d}", "name": MONTH_NAMES[i - 1], "income": 0.0, "expense": 0.0} for i in range(1, 13)]
    by_month = {m["month"]: m for m in months}

    for r in conn.execute(
        "SELECT type, strftime('%m', date) AS m, amount FROM transactions WHERE strftime('%Y', date) = ?",
        (str(year),),
    ).fetchall():
        if r["m"] in by_month:
            if r["type"] == "income":
                by_month[r["m"]]["income"] += r["amount"]
            else:
                by_month[r["m"]]["expense"] += r["amount"]

    for r in conn.execute(
        "SELECT strftime('%m', date) AS m, amount FROM general_expenses WHERE strftime('%Y', date) = ?",
        (str(year),),
    ).fetchall():
        if r["m"] in by_month:
            by_month[r["m"]]["expense"] += r["amount"]

    for r in conn.execute(
        "SELECT strftime('%m', date) AS m, interest_amount FROM paybacks WHERE strftime('%Y', date) = ?",
        (str(year),),
    ).fetchall():
        if r["m"] in by_month:
            by_month[r["m"]]["expense"] += r["interest_amount"]

    for m in months:
        m["net"] = m["income"] - m["expense"]
    return months


@app.route("/monthly-summary")
def monthly_summary():
    conn = get_db()
    years = tax_years_available(conn)
    year = request.args.get("year") or (years[0] if years else str(date.today().year))
    if not years:
        years = [year]
    months = monthly_totals(conn, year)
    conn.close()
    total_income = sum(m["income"] for m in months)
    total_expense = sum(m["expense"] for m in months)
    max_val = max([m["income"] for m in months] + [m["expense"] for m in months] + [0.01])
    for m in months:
        m["income_pct"] = round(m["income"] / max_val * 100, 1)
        m["expense_pct"] = round(m["expense"] / max_val * 100, 1)
    return render_template(
        "monthly_summary.html",
        years=years,
        year=year,
        months=months,
        total_income=total_income,
        total_expense=total_expense,
        net=total_income - total_expense,
    )


def tax_category_totals(conn, year):
    """Category totals for one calendar year, combining per-car transactions and
    general (fleet-wide) expenses under the same category names. Mileage's dollar
    total is included in expense_totals like any other category; total miles for
    that category is returned separately since it's informational, not a dollar figure."""
    income_totals = {cat: 0.0 for cat in CATEGORIES_INCOME}
    expense_totals = {cat: 0.0 for cat in CATEGORIES_EXPENSE}
    mileage_miles = 0.0

    for r in conn.execute(
        "SELECT type, category, amount, miles FROM transactions WHERE strftime('%Y', date) = ?",
        (str(year),),
    ).fetchall():
        if r["type"] == "income":
            income_totals[r["category"]] = income_totals.get(r["category"], 0.0) + r["amount"]
        else:
            expense_totals[r["category"]] = expense_totals.get(r["category"], 0.0) + r["amount"]
            if r["category"] == "Mileage" and r["miles"]:
                mileage_miles += r["miles"]

    for r in conn.execute(
        "SELECT category, amount, miles FROM general_expenses WHERE strftime('%Y', date) = ?",
        (str(year),),
    ).fetchall():
        expense_totals[r["category"]] = expense_totals.get(r["category"], 0.0) + r["amount"]
        if r["category"] == "Mileage" and r["miles"]:
            mileage_miles += r["miles"]

    interest_total = total_interest_paid(conn, year=year)

    return income_totals, expense_totals, mileage_miles, interest_total


@app.route("/tax-export")
def tax_export():
    conn = get_db()
    years = tax_years_available(conn)
    year = request.args.get("year") or (years[0] if years else str(date.today().year))
    if not years:
        years = [year]
    income_totals, expense_totals, mileage_miles, interest_total = tax_category_totals(conn, year)
    vehicles = conn.execute(
        "SELECT * FROM cars WHERE purchase_price IS NOT NULL ORDER BY nickname"
    ).fetchall()
    compliance_cars = conn.execute(
        "SELECT * FROM cars WHERE vin IS NOT NULL OR license_plate IS NOT NULL "
        "OR registration_expiration IS NOT NULL OR inspection_due_date IS NOT NULL "
        "OR insurance_carrier IS NOT NULL ORDER BY nickname"
    ).fetchall()
    distribution_totals = distribution_totals_by_user(conn, year=year)
    conn.close()
    total_income = sum(income_totals.values())
    total_expense = sum(expense_totals.values()) + interest_total
    return render_template(
        "tax_export.html",
        years=years,
        year=year,
        categories_income=CATEGORIES_INCOME,
        categories_expense=CATEGORIES_EXPENSE,
        income_totals=income_totals,
        expense_totals=expense_totals,
        mileage_miles=mileage_miles,
        interest_total=interest_total,
        total_income=total_income,
        total_expense=total_expense,
        net=total_income - total_expense,
        vehicles=vehicles,
        compliance_cars=compliance_cars,
        distribution_totals=distribution_totals,
    )


@app.route("/tax-export/download")
def tax_export_download():
    year = request.args.get("year") or str(date.today().year)
    conn = get_db()
    business_name = get_business_name(conn)
    income_totals, expense_totals, mileage_miles, interest_total = tax_category_totals(conn, year)
    vehicles = conn.execute(
        "SELECT * FROM cars WHERE purchase_price IS NOT NULL ORDER BY nickname"
    ).fetchall()
    compliance_cars = conn.execute(
        "SELECT * FROM cars WHERE vin IS NOT NULL OR license_plate IS NOT NULL "
        "OR registration_expiration IS NOT NULL OR inspection_due_date IS NOT NULL "
        "OR insurance_carrier IS NOT NULL ORDER BY nickname"
    ).fetchall()
    distribution_totals = distribution_totals_by_user(conn, year=year)
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    title = f"{business_name} - Tax Summary - {year}" if business_name else f"Turo Tracker Tax Summary - {year}"
    writer.writerow([title])
    writer.writerow([])

    writer.writerow(["Income", "Amount"])
    total_income = 0.0
    for cat in CATEGORIES_INCOME:
        amt = income_totals.get(cat, 0.0)
        total_income += amt
        writer.writerow([cat, f"{amt:.2f}"])
    writer.writerow(["Total Income", f"{total_income:.2f}"])
    writer.writerow([])

    writer.writerow(["Expenses", "Amount", "Miles (Mileage category only)"])
    total_expense = 0.0
    for cat in CATEGORIES_EXPENSE:
        amt = expense_totals.get(cat, 0.0)
        total_expense += amt
        if cat == "Mileage":
            writer.writerow([cat, f"{amt:.2f}", f"{mileage_miles:.1f}"])
        else:
            writer.writerow([cat, f"{amt:.2f}", ""])
    writer.writerow(["Loan Interest (from logged paybacks)", f"{interest_total:.2f}", ""])
    total_expense += interest_total
    writer.writerow(["Total Expenses", f"{total_expense:.2f}", ""])
    writer.writerow([])

    writer.writerow(["Net Profit", f"{total_income - total_expense:.2f}"])
    writer.writerow([])

    if vehicles:
        writer.writerow(["Vehicle Depreciation Info (for your accountant)"])
        writer.writerow(["Car", "Purchase Price", "Placed in Service", "Business Use %"])
        for v in vehicles:
            writer.writerow(
                [
                    v["nickname"],
                    f"{v['purchase_price']:.2f}",
                    v["placed_in_service_date"] or "",
                    f"{v['business_use_pct']:.0f}",
                ]
            )
        writer.writerow([])

    if compliance_cars:
        writer.writerow(["Vehicle Compliance & Insurance"])
        writer.writerow(["Car", "VIN", "License Plate", "Registration Exp.", "Inspection Due", "Insurance Carrier", "Policy #", "Insurance Type", "Insurance Renewal"])
        for v in compliance_cars:
            writer.writerow(
                [
                    v["nickname"], v["vin"] or "", v["license_plate"] or "",
                    v["registration_expiration"] or "", v["inspection_due_date"] or "",
                    v["insurance_carrier"] or "", v["insurance_policy_number"] or "",
                    v["insurance_type"] or "", v["insurance_renewal_date"] or "",
                ]
            )
        writer.writerow([])

    if distribution_totals:
        writer.writerow(["Partner Distributions (cash draws, not P&L - for basis/capital account tracking)"])
        writer.writerow(["Partner", "Total Distributions"])
        for row in distribution_totals:
            writer.writerow([row["label"], f"{row['total']:.2f}"])

    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=turo_tax_summary_{year}.csv"
    return response


def get_business_name(conn):
    return get_meta(conn, "business_name") or ""


def get_mileage_rate(conn):
    val = get_meta(conn, "irs_mileage_rate")
    return float(val) if val else DEFAULT_MILEAGE_RATE


def resolve_amount_and_mileage(conn, category, form):
    """For the Mileage category, amount is computed server-side from miles x the
    current IRS rate (never trust a client-computed amount). Otherwise amount is
    whatever was entered, and there's no mileage to record."""
    if category == "Mileage":
        miles = float(form.get("miles") or 0)
        rate = get_mileage_rate(conn)
        return round(miles * rate, 2), miles, rate
    return float(form["amount"]), None, None


def get_default_oil_change_interval(conn):
    val = get_meta(conn, "oil_change_interval_miles")
    return int(float(val)) if val else DEFAULT_OIL_CHANGE_INTERVAL


def get_oil_change_interval(car, conn):
    """Per-car override if set, else the fleet-wide default."""
    if car["oil_change_interval_miles"]:
        return int(car["oil_change_interval_miles"])
    return get_default_oil_change_interval(conn)


def car_odometer_status(car, conn):
    current = conn.execute(
        "SELECT MAX(reading) AS r FROM odometer_logs WHERE car_id = ?", (car["id"],)
    ).fetchone()["r"]
    last_oil = conn.execute(
        "SELECT reading, date FROM odometer_logs WHERE car_id = ? AND oil_change = 1 ORDER BY reading DESC LIMIT 1",
        (car["id"],),
    ).fetchone()
    last_oil_reading = last_oil["reading"] if last_oil else None
    last_oil_date = last_oil["date"] if last_oil else None
    interval = get_oil_change_interval(car, conn)
    since = (current - last_oil_reading) if (current is not None and last_oil_reading is not None) else None
    due = since is not None and since >= interval
    remaining = (interval - since) if since is not None else None
    return {
        "current": current,
        "last_oil_reading": last_oil_reading,
        "last_oil_date": last_oil_date,
        "since": since,
        "interval": interval,
        "due": due,
        "remaining": remaining,
    }


def get_default_tire_change_interval(conn):
    val = get_meta(conn, "tire_change_interval_miles")
    return int(float(val)) if val else DEFAULT_TIRE_CHANGE_INTERVAL


def get_tire_change_interval(car, conn):
    """Per-car override if set, else the fleet-wide default."""
    if car["tire_change_interval_miles"]:
        return int(car["tire_change_interval_miles"])
    return get_default_tire_change_interval(conn)


def car_tire_status(car, conn):
    """Same shape/logic as car_odometer_status, but for tire replacement instead of oil changes."""
    current = conn.execute(
        "SELECT MAX(reading) AS r FROM odometer_logs WHERE car_id = ?", (car["id"],)
    ).fetchone()["r"]
    last_tire = conn.execute(
        "SELECT reading, date FROM odometer_logs WHERE car_id = ? AND tire_change = 1 ORDER BY reading DESC LIMIT 1",
        (car["id"],),
    ).fetchone()
    last_tire_reading = last_tire["reading"] if last_tire else None
    last_tire_date = last_tire["date"] if last_tire else None
    interval = get_tire_change_interval(car, conn)
    since = (current - last_tire_reading) if (current is not None and last_tire_reading is not None) else None
    due = since is not None and since >= interval
    remaining = (interval - since) if since is not None else None
    return {
        "current": current,
        "last_tire_reading": last_tire_reading,
        "last_tire_date": last_tire_date,
        "since": since,
        "interval": interval,
        "due": due,
        "remaining": remaining,
    }


def _parse_iso_date(value):
    """Parse a 'YYYY-MM-DD' string into a date. date.fromisoformat() needs Python 3.7+,
    but this app targets 3.6, so parse it manually instead."""
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_due_soon(value, days_ahead=30):
    """True if an ISO date string is today, overdue, or within `days_ahead` days out."""
    if not value:
        return False
    try:
        d = _parse_iso_date(value)
    except ValueError:
        return False
    return d <= date.today() + timedelta(days=days_ahead)


def car_compliance_status(car):
    """Registration/inspection/insurance renewal flags, all due-within-30-days or overdue."""
    return {
        "registration_due": _date_due_soon(car["registration_expiration"]),
        "inspection_due": _date_due_soon(car["inspection_due_date"]),
        "insurance_due": _date_due_soon(car["insurance_renewal_date"]),
    }


@app.route("/settings", methods=["GET", "POST"])
def settings():
    conn = get_db()
    if request.method == "POST":
        set_meta(conn, "business_name", request.form.get("business_name", "").strip())
        rate = float(request.form["mileage_rate"])
        set_meta(conn, "irs_mileage_rate", str(rate))
        interval = int(request.form["oil_change_interval_miles"])
        set_meta(conn, "oil_change_interval_miles", str(interval))
        tire_interval = int(request.form["tire_change_interval_miles"])
        set_meta(conn, "tire_change_interval_miles", str(tire_interval))
        conn.commit()
        conn.close()
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))
    business_name = get_business_name(conn)
    mileage_rate = get_mileage_rate(conn)
    oil_change_interval = get_default_oil_change_interval(conn)
    tire_change_interval = get_default_tire_change_interval(conn)
    conn.close()
    return render_template(
        "settings.html",
        business_name=business_name,
        mileage_rate=mileage_rate,
        oil_change_interval=oil_change_interval,
        tire_change_interval=tire_change_interval,
    )


def total_interest_paid(conn, car_id=None, year=None):
    """Sum of the interest portion of logged paybacks - the only part of a loan
    payment that's actually tax-deductible (principal is just debt paydown)."""
    query = "SELECT COALESCE(SUM(interest_amount), 0) AS t FROM paybacks WHERE 1=1"
    params = []
    if car_id is not None:
        query += " AND car_id = ?"
        params.append(car_id)
    if year is not None:
        query += " AND strftime('%Y', date) = ?"
        params.append(str(year))
    return conn.execute(query, params).fetchone()["t"]


def car_totals(car_id, conn):
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END), 0) AS expense
        FROM transactions WHERE car_id = ?
        """,
        (car_id,),
    ).fetchone()
    income = row["income"]
    interest = total_interest_paid(conn, car_id=car_id)
    expense = row["expense"] + interest
    return {"income": income, "expense": expense, "interest": interest, "net": income - expense}


def car_reimbursement_total(car_id, conn):
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM transactions WHERE car_id = ? AND type='expense' AND paid_by_company = 0",
        (car_id,),
    ).fetchone()["t"]


def total_reimbursement_owed(conn):
    """Combined outstanding reimbursement across per-car expenses and general expenses."""
    car_side = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM transactions WHERE type='expense' AND paid_by_company = 0"
    ).fetchone()["t"]
    general_side = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM general_expenses WHERE paid_by_company = 0"
    ).fetchone()["t"]
    return car_side + general_side


def reimbursement_rows(conn, car_id=None, scope=None):
    """All expense items still needing reimbursement. If car_id is set, only that car's
    transactions. If scope='general', only fleet-wide general expenses. Otherwise everything."""
    users_by_id = {u["id"]: user_label(u) for u in all_users(conn)}
    rows = []
    if scope != "general":
        query = (
            "SELECT t.id, t.date, t.category, t.amount, t.notes, t.reimburse_to_user_id, c.nickname AS source "
            "FROM transactions t JOIN cars c ON c.id = t.car_id "
            "WHERE t.type='expense' AND t.paid_by_company = 0"
        )
        params = []
        if car_id:
            query += " AND t.car_id = ?"
            params.append(car_id)
        for r in conn.execute(query, params).fetchall():
            rows.append(
                {
                    "kind": "transaction",
                    "id": r["id"],
                    "date": r["date"],
                    "category": r["category"],
                    "amount": r["amount"],
                    "notes": r["notes"],
                    "source": r["source"],
                    "reimburse_to": users_by_id.get(r["reimburse_to_user_id"], "Unspecified"),
                }
            )
    if not car_id and scope != "car":
        for r in conn.execute(
            "SELECT * FROM general_expenses WHERE paid_by_company = 0"
        ).fetchall():
            rows.append(
                {
                    "kind": "general",
                    "id": r["id"],
                    "date": r["date"],
                    "category": r["category"],
                    "amount": r["amount"],
                    "notes": r["notes"],
                    "source": "General (fleet-wide)",
                    "reimburse_to": users_by_id.get(r["reimburse_to_user_id"], "Unspecified"),
                }
            )
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def owed_totals(car, conn):
    """Balance still owed for a financed/fronted car. Only the principal portion of a
    payback reduces this balance - interest isn't debt paydown, it's already counted as
    a deductible expense in Net Profit (see total_interest_paid / car_totals)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount - interest_amount), 0) AS principal, "
        "COALESCE(SUM(interest_amount), 0) AS interest FROM paybacks WHERE car_id = ?",
        (car["id"],),
    ).fetchone()
    paid = row["principal"]
    interest_paid = row["interest"]
    initial = car["owed_initial"] or 0
    return {"initial": initial, "paid": paid, "remaining": initial - paid, "interest_paid": interest_paid}


def funding_owed_label(car, conn):
    """Who a financed/fronted car's balance is owed to, for display."""
    if car["funding_type"] == "financed":
        return "the bank"
    if car["funding_type"] == "cash":
        if car["owed_to_user_id"]:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (car["owed_to_user_id"],)).fetchone()
            if user:
                return user_label(user)
        return "you"
    return None


def car_ownership_rows(conn, car_id):
    """Every user with their ownership pct for this car (0 if not set), for the
    car form and car detail display."""
    existing = {
        r["user_id"]: r["pct"]
        for r in conn.execute("SELECT user_id, pct FROM car_ownership WHERE car_id = ?", (car_id,)).fetchall()
    }
    rows = []
    for user in all_users(conn):
        rows.append({"user_id": user["id"], "label": user_label(user), "pct": existing.get(user["id"], 0)})
    return rows


def save_car_ownership(conn, car_id, form):
    """Replace this car's ownership rows from submitted ownership_pct_<user_id> fields.
    Only non-zero percentages are stored."""
    conn.execute("DELETE FROM car_ownership WHERE car_id = ?", (car_id,))
    for user in all_users(conn):
        raw = form.get(f"ownership_pct_{user['id']}")
        pct = float(raw) if raw else 0
        if pct:
            conn.execute(
                "INSERT INTO car_ownership (car_id, user_id, pct) VALUES (?, ?, ?)",
                (car_id, user["id"], pct),
            )


def distribution_totals_by_user(conn, year=None):
    """Total cash distributions (partner draws) per partner, optionally filtered to one year."""
    query = (
        "SELECT u.id, u.display_name, u.username, COALESCE(SUM(d.amount), 0) AS total "
        "FROM users u LEFT JOIN distributions d ON d.user_id = u.id"
    )
    params = []
    if year is not None:
        query += " AND strftime('%Y', d.date) = ?"
        params.append(str(year))
    query += " GROUP BY u.id ORDER BY u.display_name, u.username"
    rows = conn.execute(query, params).fetchall()
    return [
        {"user_id": r["id"], "label": r["display_name"] or r["username"], "total": r["total"]}
        for r in rows
        if r["total"]
    ]


def general_expense_total(conn):
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM general_expenses"
    ).fetchone()["total"]


def total_principal_paid(conn):
    """Fleet-wide principal paid down across all paybacks (excludes interest, which is
    already counted as a Net Profit expense rather than a Cash Flow-only capital paydown)."""
    return conn.execute(
        "SELECT COALESCE(SUM(amount - interest_amount), 0) AS t FROM paybacks"
    ).fetchone()["t"]


def period_totals(conn, year_month):
    """Income/expense/net for a single 'YYYY-MM' period, combining per-car
    transactions and general (fleet-wide) expenses."""
    income = 0.0
    expense = 0.0
    for r in conn.execute(
        "SELECT type, amount FROM transactions WHERE strftime('%Y-%m', date) = ?",
        (year_month,),
    ).fetchall():
        if r["type"] == "income":
            income += r["amount"]
        else:
            expense += r["amount"]
    for r in conn.execute(
        "SELECT amount FROM general_expenses WHERE strftime('%Y-%m', date) = ?",
        (year_month,),
    ).fetchall():
        expense += r["amount"]
    for r in conn.execute(
        "SELECT interest_amount FROM paybacks WHERE strftime('%Y-%m', date) = ?",
        (year_month,),
    ).fetchall():
        expense += r["interest_amount"]
    return {"income": income, "expense": expense, "net": income - expense}


@app.route("/")
def dashboard():
    conn = get_db()
    cars = conn.execute("SELECT * FROM cars ORDER BY active DESC, nickname").fetchall()
    cars_with_totals = []
    grand_income = grand_expense = grand_interest = 0
    for car in cars:
        totals = car_totals(car["id"], conn)
        owed = owed_totals(car, conn)
        reimbursement = car_reimbursement_total(car["id"], conn)
        odometer_status = car_odometer_status(car, conn)
        tire_status = car_tire_status(car, conn)
        compliance = car_compliance_status(car)
        funding_label = funding_owed_label(car, conn)
        grand_income += totals["income"]
        grand_expense += totals["expense"]
        grand_interest += totals["interest"]
        cars_with_totals.append(
            {
                **dict(car),
                **totals,
                "owed_remaining": owed["remaining"],
                "reimbursement": reimbursement,
                "odometer_status": odometer_status,
                "tire_status": tire_status,
                "compliance": compliance,
                "funding_label": funding_label,
            }
        )
    general_total = general_expense_total(conn)
    grand_expense += general_total
    reimbursement_owed = total_reimbursement_owed(conn)
    last_import_at = get_meta(conn, "last_import_at")
    grand_net = grand_income - grand_expense
    principal_paid = total_principal_paid(conn)
    cash_flow = grand_net - principal_paid
    today = date.today()
    current_month_label = f"{MONTH_NAMES[today.month - 1]} {today.year}"
    current_month_totals = period_totals(conn, today.strftime("%Y-%m"))
    conn.close()
    return render_template(
        "dashboard.html",
        current_month_label=current_month_label,
        current_month_totals=current_month_totals,
        cars=cars_with_totals,
        grand_income=grand_income,
        grand_expense=grand_expense,
        grand_net=grand_net,
        cash_flow=cash_flow,
        general_total=general_total,
        reimbursement_owed=reimbursement_owed,
        last_import_at=last_import_at,
    )


@app.route("/cars/new", methods=["GET", "POST"])
def new_car():
    conn = get_db()
    if request.method == "POST":
        funding_type = request.form.get("funding_type") or None
        owed_to_user_id = request.form.get("owed_to_user_id") or None
        cur = conn.execute(
            """INSERT INTO cars
               (nickname, make, model, year, funding_type, owed_initial, oil_change_interval_miles,
                owed_to_user_id, purchase_price, placed_in_service_date, business_use_pct,
                vin, license_plate, registration_expiration, inspection_due_date,
                tire_change_interval_miles, insurance_carrier, insurance_policy_number,
                insurance_type, insurance_renewal_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form["nickname"].strip(),
                request.form.get("make", "").strip(),
                request.form.get("model", "").strip(),
                request.form.get("year") or None,
                funding_type,
                float(request.form.get("owed_initial") or 0),
                request.form.get("oil_change_interval_miles") or None,
                owed_to_user_id if funding_type == "cash" else None,
                request.form.get("purchase_price") or None,
                request.form.get("placed_in_service_date") or None,
                float(request.form.get("business_use_pct") or 100),
                request.form.get("vin", "").strip() or None,
                request.form.get("license_plate", "").strip() or None,
                request.form.get("registration_expiration") or None,
                request.form.get("inspection_due_date") or None,
                request.form.get("tire_change_interval_miles") or None,
                request.form.get("insurance_carrier", "").strip() or None,
                request.form.get("insurance_policy_number", "").strip() or None,
                request.form.get("insurance_type") or None,
                request.form.get("insurance_renewal_date") or None,
            ),
        )
        car_id = cur.lastrowid
        save_car_ownership(conn, car_id, request.form)
        conn.commit()
        conn.close()
        flash("Car added.", "success")
        return redirect(url_for("dashboard"))
    default_interval = get_default_oil_change_interval(conn)
    default_tire_interval = get_default_tire_change_interval(conn)
    users = all_users(conn)
    ownership_rows = [{"user_id": u["id"], "label": user_label(u), "pct": 0} for u in users]
    conn.close()
    return render_template(
        "car_form.html",
        car=None,
        default_interval=default_interval,
        default_tire_interval=default_tire_interval,
        users=users,
        ownership_rows=ownership_rows,
        insurance_types=INSURANCE_TYPES,
    )


@app.route("/cars/<int:car_id>/edit", methods=["GET", "POST"])
def edit_car(car_id):
    conn = get_db()
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    if car is None:
        conn.close()
        flash("Car not found.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        funding_type = request.form.get("funding_type") or None
        owed_to_user_id = request.form.get("owed_to_user_id") or None
        conn.execute(
            """UPDATE cars SET
                 nickname=?, make=?, model=?, year=?, active=?, funding_type=?, owed_initial=?,
                 oil_change_interval_miles=?, owed_to_user_id=?, purchase_price=?, placed_in_service_date=?,
                 business_use_pct=?, vin=?, license_plate=?, registration_expiration=?, inspection_due_date=?,
                 tire_change_interval_miles=?, insurance_carrier=?, insurance_policy_number=?,
                 insurance_type=?, insurance_renewal_date=?
               WHERE id=?""",
            (
                request.form["nickname"].strip(),
                request.form.get("make", "").strip(),
                request.form.get("model", "").strip(),
                request.form.get("year") or None,
                1 if request.form.get("active") == "on" else 0,
                funding_type,
                float(request.form.get("owed_initial") or 0),
                request.form.get("oil_change_interval_miles") or None,
                owed_to_user_id if funding_type == "cash" else None,
                request.form.get("purchase_price") or None,
                request.form.get("placed_in_service_date") or None,
                float(request.form.get("business_use_pct") or 100),
                request.form.get("vin", "").strip() or None,
                request.form.get("license_plate", "").strip() or None,
                request.form.get("registration_expiration") or None,
                request.form.get("inspection_due_date") or None,
                request.form.get("tire_change_interval_miles") or None,
                request.form.get("insurance_carrier", "").strip() or None,
                request.form.get("insurance_policy_number", "").strip() or None,
                request.form.get("insurance_type") or None,
                request.form.get("insurance_renewal_date") or None,
                car_id,
            ),
        )
        save_car_ownership(conn, car_id, request.form)
        conn.commit()
        conn.close()
        flash("Car updated.", "success")
        return redirect(url_for("dashboard"))
    default_interval = get_default_oil_change_interval(conn)
    default_tire_interval = get_default_tire_change_interval(conn)
    users = all_users(conn)
    ownership_rows = car_ownership_rows(conn, car_id)
    conn.close()
    return render_template(
        "car_form.html",
        car=car,
        default_interval=default_interval,
        default_tire_interval=default_tire_interval,
        users=users,
        ownership_rows=ownership_rows,
        insurance_types=INSURANCE_TYPES,
    )


@app.route("/cars/<int:car_id>/delete", methods=["POST"])
def delete_car(car_id):
    conn = get_db()
    conn.execute("DELETE FROM cars WHERE id = ?", (car_id,))
    conn.commit()
    conn.close()
    flash("Car and its transactions deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/cars/<int:car_id>")
def car_detail(car_id):
    conn = get_db()
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    if car is None:
        conn.close()
        flash("Car not found.", "error")
        return redirect(url_for("dashboard"))
    transactions = conn.execute(
        "SELECT * FROM transactions WHERE car_id = ? ORDER BY date DESC, id DESC",
        (car_id,),
    ).fetchall()
    paybacks = conn.execute(
        "SELECT * FROM paybacks WHERE car_id = ? ORDER BY date DESC, id DESC",
        (car_id,),
    ).fetchall()
    odometer_logs = conn.execute(
        "SELECT * FROM odometer_logs WHERE car_id = ? ORDER BY date DESC, reading DESC, id DESC",
        (car_id,),
    ).fetchall()
    totals = car_totals(car_id, conn)
    owed = owed_totals(car, conn)
    reimbursement = car_reimbursement_total(car_id, conn)
    odometer_status = car_odometer_status(car, conn)
    tire_status = car_tire_status(car, conn)
    compliance = car_compliance_status(car)
    cash_flow = totals["net"] - owed["paid"]
    funding_label = funding_owed_label(car, conn)
    users_by_id = {u["id"]: user_label(u) for u in all_users(conn)}
    ownership_rows = [r for r in car_ownership_rows(conn, car_id) if r["pct"]]
    conn.close()
    return render_template(
        "car_detail.html",
        car=car,
        transactions=transactions,
        totals=totals,
        paybacks=paybacks,
        owed=owed,
        reimbursement=reimbursement,
        odometer_logs=odometer_logs,
        odometer_status=odometer_status,
        tire_status=tire_status,
        compliance=compliance,
        cash_flow=cash_flow,
        funding_label=funding_label,
        users_by_id=users_by_id,
        ownership_rows=ownership_rows,
    )


@app.route("/cars/<int:car_id>/transactions/new", methods=["GET", "POST"])
def new_transaction(car_id):
    conn = get_db()
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    if car is None:
        conn.close()
        flash("Car not found.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        amount, miles, mileage_rate = resolve_amount_and_mileage(
            conn, request.form["category"], request.form
        )
        paid_by_company = 1 if request.form.get("paid_by_company") == "on" else 0
        reimburse_to_user_id = request.form.get("reimburse_to_user_id") or None
        protection_plan = request.form.get("protection_plan", "").strip() or None
        conn.execute(
            "INSERT INTO transactions (car_id, date, type, category, amount, notes, paid_by_company, miles, mileage_rate, reimburse_to_user_id, created_by_user_id, protection_plan) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                car_id,
                request.form["date"],
                request.form["type"],
                request.form["category"],
                amount,
                request.form.get("notes", "").strip(),
                paid_by_company,
                miles,
                mileage_rate,
                None if paid_by_company else reimburse_to_user_id,
                session.get("user_id"),
                protection_plan,
            ),
        )
        conn.commit()
        conn.close()
        flash("Transaction added.", "success")
        return redirect(url_for("car_detail", car_id=car_id))
    mileage_rate = get_mileage_rate(conn)
    users = all_users(conn)
    conn.close()
    return render_template(
        "transaction_form.html",
        car=car,
        txn=None,
        today=date.today().isoformat(),
        categories_income=CATEGORIES_INCOME,
        categories_expense=CATEGORIES_EXPENSE,
        mileage_rate=mileage_rate,
        users=users,
    )


@app.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
def edit_transaction(txn_id):
    conn = get_db()
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if txn is None:
        conn.close()
        flash("Transaction not found.", "error")
        return redirect(url_for("dashboard"))
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (txn["car_id"],)).fetchone()
    if request.method == "POST":
        amount, miles, mileage_rate = resolve_amount_and_mileage(
            conn, request.form["category"], request.form
        )
        paid_by_company = 1 if request.form.get("paid_by_company") == "on" else 0
        reimburse_to_user_id = request.form.get("reimburse_to_user_id") or None
        protection_plan = request.form.get("protection_plan", "").strip() or None
        conn.execute(
            "UPDATE transactions SET date=?, type=?, category=?, amount=?, notes=?, paid_by_company=?, miles=?, mileage_rate=?, reimburse_to_user_id=?, protection_plan=? WHERE id=?",
            (
                request.form["date"],
                request.form["type"],
                request.form["category"],
                amount,
                request.form.get("notes", "").strip(),
                paid_by_company,
                miles,
                mileage_rate,
                None if paid_by_company else reimburse_to_user_id,
                protection_plan,
                txn_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("Transaction updated.", "success")
        return redirect(url_for("car_detail", car_id=car["id"]))
    mileage_rate = get_mileage_rate(conn)
    users = all_users(conn)
    conn.close()
    return render_template(
        "transaction_form.html",
        car=car,
        txn=txn,
        today=date.today().isoformat(),
        categories_income=CATEGORIES_INCOME,
        categories_expense=CATEGORIES_EXPENSE,
        mileage_rate=mileage_rate,
        users=users,
    )


@app.route("/transactions/<int:txn_id>/delete", methods=["POST"])
def delete_transaction(txn_id):
    conn = get_db()
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    car_id = txn["car_id"] if txn else None
    conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    conn.commit()
    conn.close()
    flash("Transaction deleted.", "success")
    if car_id:
        return redirect(url_for("car_detail", car_id=car_id))
    return redirect(url_for("dashboard"))


@app.route("/cars/<int:car_id>/paybacks/new", methods=["GET", "POST"])
def new_payback(car_id):
    conn = get_db()
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    if car is None:
        conn.close()
        flash("Car not found.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        interest_amount = float(request.form.get("interest_amount") or 0) if car["funding_type"] == "financed" else 0
        conn.execute(
            "INSERT INTO paybacks (car_id, date, amount, interest_amount, notes, created_by_user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                car_id,
                request.form["date"],
                float(request.form["amount"]),
                interest_amount,
                request.form.get("notes", "").strip(),
                session.get("user_id"),
            ),
        )
        conn.commit()
        conn.close()
        flash("Payback logged.", "success")
        return redirect(url_for("car_detail", car_id=car_id))
    funding_label = funding_owed_label(car, conn)
    conn.close()
    return render_template(
        "payback_form.html", car=car, payback=None, today=date.today().isoformat(), funding_label=funding_label
    )


@app.route("/paybacks/<int:payback_id>/edit", methods=["GET", "POST"])
def edit_payback(payback_id):
    conn = get_db()
    payback = conn.execute("SELECT * FROM paybacks WHERE id = ?", (payback_id,)).fetchone()
    if payback is None:
        conn.close()
        flash("Payback not found.", "error")
        return redirect(url_for("dashboard"))
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (payback["car_id"],)).fetchone()
    if request.method == "POST":
        interest_amount = float(request.form.get("interest_amount") or 0) if car["funding_type"] == "financed" else 0
        conn.execute(
            "UPDATE paybacks SET date=?, amount=?, interest_amount=?, notes=? WHERE id=?",
            (
                request.form["date"],
                float(request.form["amount"]),
                interest_amount,
                request.form.get("notes", "").strip(),
                payback_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("Payback updated.", "success")
        return redirect(url_for("car_detail", car_id=car["id"]))
    funding_label = funding_owed_label(car, conn)
    conn.close()
    return render_template(
        "payback_form.html", car=car, payback=payback, today=date.today().isoformat(), funding_label=funding_label
    )


@app.route("/paybacks/<int:payback_id>/delete", methods=["POST"])
def delete_payback(payback_id):
    conn = get_db()
    payback = conn.execute("SELECT * FROM paybacks WHERE id = ?", (payback_id,)).fetchone()
    car_id = payback["car_id"] if payback else None
    conn.execute("DELETE FROM paybacks WHERE id = ?", (payback_id,))
    conn.commit()
    conn.close()
    flash("Payback deleted.", "success")
    if car_id:
        return redirect(url_for("car_detail", car_id=car_id))
    return redirect(url_for("dashboard"))


@app.route("/cars/<int:car_id>/oil-change/new", methods=["GET", "POST"])
def new_oil_change(car_id):
    conn = get_db()
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    if car is None:
        conn.close()
        flash("Car not found.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        conn.execute(
            "INSERT INTO odometer_logs (car_id, date, reading, oil_change, notes) VALUES (?, ?, ?, 1, ?)",
            (
                car_id,
                request.form["date"],
                float(request.form["reading"]),
                request.form.get("notes", "").strip(),
            ),
        )
        conn.commit()
        conn.close()
        flash("Oil change logged.", "success")
        return redirect(url_for("car_detail", car_id=car_id))
    odometer_status = car_odometer_status(car, conn)
    conn.close()
    return render_template(
        "oil_change_form.html", car=car, today=date.today().isoformat(), odometer_status=odometer_status
    )


@app.route("/cars/<int:car_id>/tire-change/new", methods=["GET", "POST"])
def new_tire_change(car_id):
    conn = get_db()
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone()
    if car is None:
        conn.close()
        flash("Car not found.", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        conn.execute(
            "INSERT INTO odometer_logs (car_id, date, reading, tire_change, notes) VALUES (?, ?, ?, 1, ?)",
            (
                car_id,
                request.form["date"],
                float(request.form["reading"]),
                request.form.get("notes", "").strip(),
            ),
        )
        conn.commit()
        conn.close()
        flash("Tire change logged.", "success")
        return redirect(url_for("car_detail", car_id=car_id))
    tire_status = car_tire_status(car, conn)
    conn.close()
    return render_template(
        "tire_change_form.html", car=car, today=date.today().isoformat(), tire_status=tire_status
    )


@app.route("/odometer-logs/<int:log_id>/delete", methods=["POST"])
def delete_odometer_log(log_id):
    conn = get_db()
    log = conn.execute("SELECT * FROM odometer_logs WHERE id = ?", (log_id,)).fetchone()
    car_id = log["car_id"] if log else None
    conn.execute("DELETE FROM odometer_logs WHERE id = ?", (log_id,))
    conn.commit()
    conn.close()
    flash("Odometer entry deleted.", "success")
    if car_id:
        return redirect(url_for("car_detail", car_id=car_id))
    return redirect(url_for("dashboard"))


@app.route("/reimbursements")
def reimbursements():
    car_id = request.args.get("car_id", type=int)
    scope = request.args.get("scope")
    conn = get_db()
    car = conn.execute("SELECT * FROM cars WHERE id = ?", (car_id,)).fetchone() if car_id else None
    rows = reimbursement_rows(conn, car_id=car_id, scope=scope)
    total = sum(r["amount"] for r in rows)
    conn.close()
    return render_template(
        "reimbursements.html", rows=rows, total=total, car=car, scope=scope
    )


@app.route("/reimbursements/mark_paid", methods=["POST"])
def mark_reimbursement_paid():
    kind = request.form["kind"]
    item_id = int(request.form["id"])
    conn = get_db()
    if kind == "transaction":
        conn.execute("UPDATE transactions SET paid_by_company = 1 WHERE id = ?", (item_id,))
    else:
        conn.execute("UPDATE general_expenses SET paid_by_company = 1 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    flash("Marked as paid back.", "success")
    car_id = request.form.get("car_id") or None
    scope = request.form.get("scope") or None
    return redirect(url_for("reimbursements", car_id=car_id, scope=scope))


@app.route("/expenses")
def general_expenses():
    conn = get_db()
    expenses = conn.execute(
        "SELECT * FROM general_expenses ORDER BY date DESC, id DESC"
    ).fetchall()
    total = general_expense_total(conn)
    reimbursement_total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM general_expenses WHERE paid_by_company = 0"
    ).fetchone()["t"]
    users_by_id = {u["id"]: user_label(u) for u in all_users(conn)}
    conn.close()
    return render_template(
        "general_expenses.html",
        expenses=expenses,
        total=total,
        reimbursement_total=reimbursement_total,
        users_by_id=users_by_id,
    )


@app.route("/expenses/new", methods=["GET", "POST"])
def new_general_expense():
    conn = get_db()
    if request.method == "POST":
        amount, miles, mileage_rate = resolve_amount_and_mileage(
            conn, request.form["category"], request.form
        )
        paid_by_company = 1 if request.form.get("paid_by_company") == "on" else 0
        reimburse_to_user_id = request.form.get("reimburse_to_user_id") or None
        conn.execute(
            "INSERT INTO general_expenses (date, category, amount, notes, paid_by_company, miles, mileage_rate, reimburse_to_user_id, created_by_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.form["date"],
                request.form["category"],
                amount,
                request.form.get("notes", "").strip(),
                paid_by_company,
                miles,
                mileage_rate,
                None if paid_by_company else reimburse_to_user_id,
                session.get("user_id"),
            ),
        )
        conn.commit()
        conn.close()
        flash("Expense added.", "success")
        return redirect(url_for("general_expenses"))
    mileage_rate = get_mileage_rate(conn)
    users = all_users(conn)
    conn.close()
    return render_template(
        "general_expense_form.html",
        expense=None,
        today=date.today().isoformat(),
        categories_expense=CATEGORIES_EXPENSE,
        mileage_rate=mileage_rate,
        users=users,
    )


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
def edit_general_expense(expense_id):
    conn = get_db()
    expense = conn.execute(
        "SELECT * FROM general_expenses WHERE id = ?", (expense_id,)
    ).fetchone()
    if expense is None:
        conn.close()
        flash("Expense not found.", "error")
        return redirect(url_for("general_expenses"))
    if request.method == "POST":
        amount, miles, mileage_rate = resolve_amount_and_mileage(
            conn, request.form["category"], request.form
        )
        paid_by_company = 1 if request.form.get("paid_by_company") == "on" else 0
        reimburse_to_user_id = request.form.get("reimburse_to_user_id") or None
        conn.execute(
            "UPDATE general_expenses SET date=?, category=?, amount=?, notes=?, paid_by_company=?, miles=?, mileage_rate=?, reimburse_to_user_id=? WHERE id=?",
            (
                request.form["date"],
                request.form["category"],
                amount,
                request.form.get("notes", "").strip(),
                paid_by_company,
                miles,
                mileage_rate,
                None if paid_by_company else reimburse_to_user_id,
                expense_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("Expense updated.", "success")
        return redirect(url_for("general_expenses"))
    mileage_rate = get_mileage_rate(conn)
    users = all_users(conn)
    conn.close()
    return render_template(
        "general_expense_form.html",
        expense=expense,
        today=date.today().isoformat(),
        categories_expense=CATEGORIES_EXPENSE,
        mileage_rate=mileage_rate,
        users=users,
    )


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_general_expense(expense_id):
    conn = get_db()
    conn.execute("DELETE FROM general_expenses WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()
    flash("Expense deleted.", "success")
    return redirect(url_for("general_expenses"))


@app.route("/distributions")
def distributions():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM distributions ORDER BY date DESC, id DESC"
    ).fetchall()
    users_by_id = {u["id"]: user_label(u) for u in all_users(conn)}
    totals = distribution_totals_by_user(conn)
    total = sum(r["amount"] for r in rows)
    conn.close()
    return render_template(
        "distributions.html", rows=rows, users_by_id=users_by_id, totals=totals, total=total
    )


@app.route("/distributions/new", methods=["GET", "POST"])
def new_distribution():
    conn = get_db()
    if request.method == "POST":
        conn.execute(
            "INSERT INTO distributions (date, user_id, amount, notes, created_by_user_id) VALUES (?, ?, ?, ?, ?)",
            (
                request.form["date"],
                int(request.form["user_id"]),
                float(request.form["amount"]),
                request.form.get("notes", "").strip(),
                session.get("user_id"),
            ),
        )
        conn.commit()
        conn.close()
        flash("Distribution logged.", "success")
        return redirect(url_for("distributions"))
    users = all_users(conn)
    conn.close()
    return render_template(
        "distribution_form.html", distribution=None, today=date.today().isoformat(), users=users
    )


@app.route("/distributions/<int:distribution_id>/edit", methods=["GET", "POST"])
def edit_distribution(distribution_id):
    conn = get_db()
    distribution = conn.execute(
        "SELECT * FROM distributions WHERE id = ?", (distribution_id,)
    ).fetchone()
    if distribution is None:
        conn.close()
        flash("Distribution not found.", "error")
        return redirect(url_for("distributions"))
    if request.method == "POST":
        conn.execute(
            "UPDATE distributions SET date=?, user_id=?, amount=?, notes=? WHERE id=?",
            (
                request.form["date"],
                int(request.form["user_id"]),
                float(request.form["amount"]),
                request.form.get("notes", "").strip(),
                distribution_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("Distribution updated.", "success")
        return redirect(url_for("distributions"))
    users = all_users(conn)
    conn.close()
    return render_template(
        "distribution_form.html", distribution=distribution, today=date.today().isoformat(), users=users
    )


@app.route("/distributions/<int:distribution_id>/delete", methods=["POST"])
def delete_distribution(distribution_id):
    conn = get_db()
    conn.execute("DELETE FROM distributions WHERE id = ?", (distribution_id,))
    conn.commit()
    conn.close()
    flash("Distribution deleted.", "success")
    return redirect(url_for("distributions"))


def utilization_totals(conn, year):
    """Booked days per car per month, counted as actual booked calendar days (not whole
    trips dumped into their start month) - so a trip that runs past a month boundary
    correctly splits its nights across both months, and a month's total can never exceed
    the number of days that actually exist in it. Overlapping/duplicate bookings on the
    same calendar day are also only counted once, since days are tracked as a set.

    Trips imported before trip-end-date tracking was added (or entered manually, which
    has no end date field at all) don't have a trip_end_date on file - those are counted
    as a single day so the report doesn't crash or skip them, but that undercounts their
    actual length. unknown_trips/total_trips are returned so the page can warn about it
    and point at re-importing the CSV to backfill the real end dates."""
    year_int = int(year)
    year_start = date(year_int, 1, 1)
    year_end = date(year_int, 12, 31)
    cars = conn.execute("SELECT * FROM cars ORDER BY active DESC, nickname").fetchall()
    data = []
    total_trips = 0
    unknown_trips = 0
    for car in cars:
        months = [{"month": f"{i:02d}", "name": MONTH_NAMES[i - 1], "booked_days": 0.0} for i in range(1, 13)]
        by_month = {m["month"]: m for m in months}
        booked_dates = set()
        for r in conn.execute(
            "SELECT date, trip_end_date FROM transactions "
            "WHERE car_id = ? AND category = 'Trip payout' AND strftime('%Y', date) = ?",
            (car["id"], str(year)),
        ).fetchall():
            total_trips += 1
            if not r["trip_end_date"]:
                unknown_trips += 1
            try:
                start = _parse_iso_date(r["date"])
            except ValueError:
                continue
            try:
                end = _parse_iso_date(r["trip_end_date"]) if r["trip_end_date"] else start
            except ValueError:
                end = start
            if end < start:
                end = start
            # Only count the portion of the trip that falls within the reported year -
            # there are only 12 month buckets here, so anything spilling into another
            # year isn't represented on this page.
            d = max(start, year_start)
            last = min(end, year_end)
            while d <= last:
                booked_dates.add(d)
                d += timedelta(days=1)
        for d in booked_dates:
            m = f"{d.month:02d}"
            by_month[m]["booked_days"] += 1
        for m in months:
            days_in_month = calendar.monthrange(year_int, int(m["month"]))[1]
            m["days_in_month"] = days_in_month
            m["utilization_pct"] = round(m["booked_days"] / days_in_month * 100, 1) if days_in_month else 0
        data.append({"car": car, "months": months})
    return data, total_trips, unknown_trips


@app.route("/utilization")
def utilization():
    conn = get_db()
    years = tax_years_available(conn)
    year = request.args.get("year") or (years[0] if years else str(date.today().year))
    if not years:
        years = [year]
    data, total_trips, unknown_trips = utilization_totals(conn, year)
    conn.close()
    return render_template(
        "utilization.html",
        years=years,
        year=year,
        data=data,
        month_names=MONTH_NAMES,
        total_trips=total_trips,
        unknown_trips=unknown_trips,
    )


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "GET":
        conn = get_db()
        last_import_at = get_meta(conn, "last_import_at")
        conn.close()
        return render_template("import_upload.html", last_import_at=last_import_at)

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("Please choose a CSV file to upload.", "error")
        return redirect(url_for("import_csv"))

    try:
        rows, skipped = parse_csv(file.read())
    except TuroCsvError as e:
        flash(str(e), "error")
        return redirect(url_for("import_csv"))

    if not rows:
        flash("No usable trip rows were found in that file.", "error")
        return redirect(url_for("import_csv"))

    if skipped:
        flash(f"Skipped {skipped} row(s) missing a reservation id, vehicle, or date.", "error")

    conn = get_db()
    existing_cars = conn.execute("SELECT * FROM cars ORDER BY nickname").fetchall()
    linked_by_vid = {c["turo_vehicle_id"]: c for c in existing_cars if c["turo_vehicle_id"]}
    conn.close()

    # One entry per distinct vehicle in the file, in first-seen order.
    vehicles = {}
    for r in rows:
        vehicles.setdefault(r["vehicle_id"], {"title": r["vehicle_title"], "name": r["vehicle_name"]})

    unmapped_vehicles = {vid: v for vid, v in vehicles.items() if vid not in linked_by_vid}
    mapped_vehicles = {vid: linked_by_vid[vid] for vid in vehicles if vid in linked_by_vid}

    return render_template(
        "import_confirm.html",
        rows=rows,
        rows_json=json.dumps(rows),
        unmapped_vehicles=unmapped_vehicles,
        mapped_vehicles=mapped_vehicles,
        existing_cars=existing_cars,
    )


@app.route("/import/confirm", methods=["POST"])
def import_confirm():
    rows = json.loads(request.form["rows_json"])
    checked_ids = set(request.form.getlist("import_ids"))

    conn = get_db()

    vehicle_ids = {}
    for r in rows:
        vehicle_ids.setdefault(r["vehicle_id"], r)

    vid_to_car_id = {}
    for vid, sample in vehicle_ids.items():
        field = f"vehicle_map_{vid}"
        if field in request.form:
            val = request.form[field]
            if val == "new":
                make, model, year = parse_vehicle_name(sample["vehicle_name"])
                nickname = sample["vehicle_title"] or sample["vehicle_name"] or f"Vehicle {vid}"
                cur = conn.execute(
                    "INSERT INTO cars (nickname, make, model, year, turo_vehicle_id) VALUES (?, ?, ?, ?, ?)",
                    (nickname, make, model, year, vid),
                )
                vid_to_car_id[vid] = cur.lastrowid
            else:
                vid_to_car_id[vid] = int(val)
        else:
            existing = conn.execute(
                "SELECT id FROM cars WHERE turo_vehicle_id = ?", (vid,)
            ).fetchone()
            vid_to_car_id[vid] = existing["id"] if existing else None

    created = updated = skipped = 0
    odometer_logged = 0
    for r in rows:
        if r["reservation_id"] not in checked_ids:
            skipped += 1
            continue
        car_id = vid_to_car_id.get(r["vehicle_id"])
        if not car_id:
            skipped += 1
            continue
        trip_end_date = r.get("odometer_date") or r["date"]
        existing_txn = conn.execute(
            "SELECT id FROM transactions WHERE reservation_id = ?", (r["reservation_id"],)
        ).fetchone()
        if existing_txn:
            conn.execute(
                "UPDATE transactions SET car_id=?, date=?, amount=?, notes=?, trip_end_date=? WHERE id=?",
                (car_id, r["date"], r["amount"], r["notes"], trip_end_date, existing_txn["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO transactions
                   (car_id, date, type, category, amount, notes, reservation_id, trip_end_date)
                   VALUES (?, ?, 'income', 'Trip payout', ?, ?, ?, ?)""",
                (car_id, r["date"], r["amount"], r["notes"], r["reservation_id"], trip_end_date),
            )
            created += 1

        # Turo's export includes a check-out odometer reading per trip; log it so
        # oil-change tracking stays current without any manual entry.
        checkout_odometer = r.get("checkout_odometer")
        if checkout_odometer is not None:
            odometer_date = r.get("odometer_date") or r["date"]
            existing_log = conn.execute(
                "SELECT id FROM odometer_logs WHERE reservation_id = ?", (r["reservation_id"],)
            ).fetchone()
            if existing_log:
                conn.execute(
                    "UPDATE odometer_logs SET car_id=?, date=?, reading=? WHERE id=?",
                    (car_id, odometer_date, checkout_odometer, existing_log["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO odometer_logs
                       (car_id, date, reading, oil_change, reservation_id, notes)
                       VALUES (?, ?, ?, 0, ?, 'From Turo CSV import (check-out odometer)')""",
                    (car_id, odometer_date, checkout_odometer, r["reservation_id"]),
                )
            odometer_logged += 1

    if created or updated:
        set_meta(conn, "last_import_at", conn.execute("SELECT datetime('now')").fetchone()[0])

    conn.commit()
    conn.close()
    flash(
        f"Import complete: {created} trip(s) added, {updated} updated, {skipped} skipped. "
        f"{odometer_logged} odometer reading(s) recorded.",
        "success",
    )
    return redirect(url_for("dashboard"))


def _lan_ip():
    """Best-effort guess at this machine's LAN IP, for printing a friendly URL."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    import os

    init_db()

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("DEBUG", "false").lower() == "true"

    if host == "0.0.0.0":
        lan_ip = _lan_ip()
        print(f" * Local:   http://127.0.0.1:{port}")
        if lan_ip:
            print(f" * Network: http://{lan_ip}:{port}  (use this on your phone/other devices)")

    app.run(host=host, port=port, debug=debug)
