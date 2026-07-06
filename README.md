# turo-tracker
A simple local web app to track income and expenses per car in your Turo fleet.
## Features

- Add/edit/deactivate/delete cars
- Log income and expense transactions per car (date, category, amount, notes)
- Dashboard with total income, expenses, and net profit across your fleet
- Per-car detail page with its own income/expense/net totals and transaction history
- Import Turo's "Trip earnings" CSV export directly as income transactions

## Setup

Requires Python 3.9+.

```bash
cd turo-tracker
python3 -m venv venv
source venv/bin/activate      # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python3 app.py
```

On startup it prints two URLs:

```
 * Local:   http://127.0.0.1:5050
 * Network: http://192.168.1.23:5050   (use this on your phone/other devices)
```

Open the **Local** URL on this computer, or the **Network** URL from any other phone/computer/tablet on the same WiFi.

[jkotek@vm turo-tracker]$ cat README.md
# Turo Income & Expense Tracker

A simple local web app to track income and expenses per car in your Turo fleet.

## Features

- Add/edit/deactivate/delete cars
- Log income and expense transactions per car (date, category, amount, notes)
- Dashboard with total income, expenses, and net profit across your fleet
- Per-car detail page with its own income/expense/net totals and transaction history
- Import Turo's "Trip earnings" CSV export directly as income transactions

## Setup

Requires Python 3.9+.

```bash
cd turo-tracker
python3 -m venv venv
source venv/bin/activate      # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python3 app.py
```

On startup it prints two URLs:

```
 * Local:   http://127.0.0.1:5050
 * Network: http://192.168.1.23:5050   (use this on your phone/other devices)
```

Open the **Local** URL on this computer, or the **Network** URL from any other phone/computer/tablet on the same WiFi.

The first run automatically creates `turo.db` (a SQLite database file) in this folder â that's where all your data lives. Back it up by copying that one file.

The very first time you open the app, you'll be asked to create an account (username + password) â this becomes the first partner/user. Once logged in, add accounts for other partners from the **Users** page. Everyone with an account has equal, full access to add/edit/delete anything.

### Accessing from another device

- Make sure the other device is on the same WiFi network as the computer running the app.
- Use the "Network" URL printed on startup (it changes if your computer's local IP changes â e.g. after reconnecting to WiFi).
- If it doesn't load, your computer's firewall may be blocking incoming connections on port 5050 â you may need to allow Python/this app through your firewall (macOS: System Settings â Network â Firewall; Windows: Windows Defender Firewall â Allow an app).
- Each person logs in with their own account; sessions stay logged in for 30 days per browser. This is basic access control for a trusted home network, not hardened security â still don't port-forward this app to the public internet.
- To restrict it back to just this computer, run with `HOST=127.0.0.1 python3 app.py`.

## Importing from Turo

Click **Import CSV** in the top bar and upload the trip earnings export from Turo's Host Tools â Earnings page.

- Each row becomes one income transaction (category "Trip payout") using Turo's "Total earnings" column, which is already net of discounts, Turo's fees, and add-ons â so no double counting.
- The first time you import, you'll be asked to match each vehicle in the file to a car â either link it to an existing car or let it create a new one automatically (named after your Turo listing title). After that, the same vehicle auto-links on future imports.
- Every trip is shown in a preview table before anything is saved, with a checkbox per row. Completed and in-progress trips are checked by default; upcoming "Booked" trips and no-fee cancellations are unchecked by default since nothing's been earned yet â check them manually if you want to include them.
- Re-uploading a file that overlaps a previous import is safe: trips are matched by Turo's reservation ID, so already-imported trips get updated in place (useful once a "Booked" trip completes and its final earnings are known) instead of being duplicated.

## Notes

- This runs Flask's built-in development server, which is fine for personal/local use on your home network. Login is basic (username/password, no lockouts or 2FA) â don't port-forward it or otherwise expose it to the public internet as-is.
- Optional environment variables: `HOST` (default `0.0.0.0`, i.e. reachable on your network), `PORT` (default `5050`), `DEBUG` (default `false`).
- Income categories: Trip payout, Reimbursement, Referral bonus, Other income.
- Expense categories: Maintenance, Cleaning, Insurance, Loan payment, Fuel, Mileage, Turo service fee, Registration/Tax, Tolls/Parking/Citations, Storage, Supplies, Other expense.
- To reset all data, stop the app and delete `turo.db` â it will be recreated empty on next run.

## Additional tracking

- **Vehicle compliance**: VIN, license plate, registration expiration, and inspection due date per car, with a due/overdue badge on the dashboard and car detail page (Edit Car page).
- **Tire tracking**: same pattern as oil changes â set a per-car or fleet-wide default interval (Settings), log tire changes, get a due badge.
- **Insurance policy info**: carrier, policy number, type (personal/commercial/Turo's), and renewal date per car, with a renewal-due badge.
- **Loan interest vs. principal**: when logging a Payback on a financed car, split the total payment into interest (tax-deductible, now correctly counted in Net Profit) and principal (debt paydown, affects Cash Flow only).
- **Protection plan tier**: optional free-text field per trip income entry, for your own records (not included in Turo's CSV export).
- **Utilization**: a report of booked days vs. days in the month per car, from imported trip start/end dates.
- **Ownership %**: per-partner ownership stake per car (Edit Car page), for future profit-split/K-1 prep â not yet used in any calculation.
- **Partner distributions**: cash draws taken by a partner, tracked separately from expenses/paybacks (their own page), for basis/capital account records.

## Possible next steps

- Import Turo's expense-side exports (if/when they offer one) the same way
- Per-partner profit allocation using the ownership % now tracked per car, for K-1 prep
- Guest damage/claims tracking (deductible paid, claim status, downtime)
