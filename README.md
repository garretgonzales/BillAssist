<div align="center">

<h1>🧾 Bill Tracker</h1>

<p><strong>Keep household bills visible, organized, and on time.</strong></p>

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img alt="Flask" src="https://img.shields.io/badge/Flask-Web_App-000000?style=for-the-badge&logo=flask&logoColor=white" />
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-Storage-003B57?style=for-the-badge&logo=sqlite&logoColor=white" />
  <img alt="Gmail API" src="https://img.shields.io/badge/Gmail-Optional_Integration-EA4335?style=for-the-badge&logo=gmail&logoColor=white" />
</p>

</div>

Bill Tracker is a Flask web application for managing household bills, incomes, reminders, and recurring payments. It stores data in SQLite and can optionally scan connected Gmail accounts for candidate bills that you review and approve before they enter your bill list.

Nothing from Gmail is added automatically. Matches stay in a review queue until you confirm the vendor, amount, and due date.

| 🧾 Bill organization | 🔒 Private accounts | 📬 Optional Gmail automation |
| :---: | :---: | :---: |
| Track bills, incomes, categories, due dates, recurring schedules, and payment history. | Each person has a separate login and private bills, reminders, income, and Gmail connections. | Find candidate bill emails and send personalized reminders without requiring Gmail features. |

## 📑 Contents

- [Current features](#-current-features)
- [How the bill workflow works](#-how-the-bill-workflow-works)
- [Accounts and privacy](#-accounts-and-privacy)
- [Personalization](#-personalization)
- [Gmail scanning](#-gmail-scanning)
- [Reminders](#-reminders)
- [Technology stack](#-technology-stack)
- [Run locally](#-run-locally)
- [Verification](#-verification)
- [Deployment](#-deployment)
- [Project structure](#-project-structure)
- [Security and local data](#-security-and-local-data)

## ✨ Current features

### Bill management

- Add, edit, and delete bills with vendors, amounts, due dates, notes, categories, and icons.
- Mark bills paid and automatically create the next occurrence for recurring bills.
- Repeat bills every N days, weeks, months, or years.
- Track incomes alongside bills.
- Review unpaid bills, overdue bills, bills due soon, and bills without due dates.
- Configure general reminder lead times or vendor-specific lead times.

### Gmail-assisted workflow

- Connect more than one Gmail account per user.
- Scan recent mail for messages that look like bills, invoices, or statements.
- Extract candidate amounts and due dates using email-body heuristics.
- Deduplicate candidates by Gmail message ID.
- Review, edit, approve, or reject candidates before they become bills.
- Avoid repeatedly suggesting vendors already added or approved in the past.
- Reconnect or disconnect individual Gmail accounts without affecting the others.

### Reminders

- Send a digest for unpaid bills that are overdue, due within the configured lead time, or missing a due date.
- Send reminders manually from the Review page.
- Automatically check once per calendar day when a user opens the dashboard.
- Run an independent daily systemd timer so reminders can send even when nobody opens the app.
- Send each reminder from a connected Gmail account to that same account.
- Skip empty reminder emails when no bills currently qualify.

### Account experience

- Sign up and log in at `/signup` and `/login`.
- Keep bills, incomes, categories, settings, reminders, and Gmail connections private per account.
- Stay signed in on a device for one year.
- Customize the greeting shown throughout the app.
- Choose from 22 preset theme colors or provide a custom hex color.
- Toggle dark mode and automatically derive readable accent shades and text contrast.
- Choose a built-in icon when adding or editing a bill.

## 🔄 How the bill workflow works

```text
Sign up or log in
        |
        v
Add bills manually or connect Gmail
        |
        v
Review bills, income, due dates, and reminders
        |
        v
Mark a bill paid
        |
        v
Create the next recurring occurrence when applicable
        |
        v
Send an on-demand or scheduled reminder digest
```

Gmail scanning follows a separate approval flow:

```text
Connect a Gmail account
        |
        v
Search recent messages for bill-like email
        |
        v
Extract approximate vendor, amount, and due date
        |
        v
Place new matches in the Review queue
        |
        v
Approve, edit, or reject each candidate
        |
        v
Add approved candidates to the bill list
```

## 🔒 Accounts and privacy

Every person using an instance creates an account with an email address and password. Bills, incomes, categories, reminder settings, Gmail connections, and pending Gmail matches belong to the authenticated account that created them.

The application is intended for a small trusted household rather than a public multi-tenant service. Once logged in, a device remains logged in for one year so household members are not repeatedly prompted for their password.

## 🎨 Personalization

The Settings page lets each account customize:

- **Greeting:** Replace the default `Hi, <name>` text or clear it to restore the default.
- **Theme color:** Choose one of 22 preset swatches or any custom hex value. The app derives darker, softer, and on-color shades from the selected color and automatically chooses readable white or dark text.
- **Dark mode:** Switch the full application to a dark background with light text. The selected accent is re-derived for dark mode.
- **Bill icons:** Select an icon from the built-in icon pack when adding or editing a bill.

## 📬 Gmail scanning

Gmail features are optional and configured per account. The OAuth client credentials are shared by the instance, while each person completes their own Google authorization flow.

The scanner:

- Searches recent mail, defaulting to the last 45 days, using subject keywords for bills, invoices, and statements.
- Uses regular expressions and heuristics to estimate a dollar amount and due date from the message body.
- Adds new matches to the Review page, deduplicated by Gmail message ID.
- Skips vendors already added manually or approved from an earlier scan when the sender domain or exact vendor name matches.
- Does not remember rejected matches, so a rejected candidate may appear again in a later scan.

To change the search behavior, edit `DEFAULT_QUERY` in [`gmail_scraper.py`](gmail_scraper.py). It uses normal Gmail search syntax.

### Connect multiple Gmail accounts

From **Settings > Gmail Accounts**, each account can:

- **Connect** another Gmail inbox through Google's consent flow.
- **Reconnect** one account if its token expires or is revoked.
- **Disconnect** one account while leaving other connected accounts active.

"Scan Gmail now" scans every connected account and combines the results into one review queue. A token problem in one account does not block the others; the result identifies which account needs reconnection.

### Gmail setup

#### One-time setup for the instance owner

1. Create or reuse a project at [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Gmail API** under **APIs & Services > Library**.
3. Create an OAuth client under **APIs & Services > Credentials**.
   - Choose **Web application**.
   - Add `http://localhost:5432/oauth2callback` as an authorized redirect URI.
   - Use the same URI for local use or an IAP tunnel because the browser still connects through `localhost`.
   - Set `OAUTH_REDIRECT_URI` if the application uses another port or callback URI.
4. Configure the OAuth consent screen if Google asks. For personal or family use, add each participating Google account as a test user unless the app is published.
5. Download the client secret JSON as `credentials.json` in the project root.

#### Per-person setup

1. Log in and click **Scan Gmail now** or **Send reminder now** on the Review page.
2. Sign in to Google and grant access.
3. The resulting token is stored in that user's row in `bills.db`, not in a shared token file.

The app requests `gmail.readonly` for scanning and `gmail.send` for reminder emails. It never deletes or modifies mailbox messages and only sends the reminder emails it composes itself. If an older token lacks the send scope, the next manual reminder action prompts for consent again. The daily check skips silently until that consent has been granted interactively.

## ⏰ Reminders

A reminder includes every unpaid bill that is:

- Overdue.
- Due within the configured lead time, which defaults to three days and can be customized generally or per vendor.
- Missing a due date.

The dashboard-triggered check runs at most once per calendar day when that person loads the main dashboard. It sends only when Gmail is already authorized with the send scope.

"Send reminder now" bypasses the once-per-day limit and is the only path that can open Google's interactive consent flow. The email is sent to the address reported by the authorized Google account, meaning each connected account receives its own digest from itself.

For scheduled reminders independent of browser activity, install the daily timer described in [`deploy/README.md`](deploy/README.md). The timer loops over every account and uses each person's own bills, reminder settings, Gmail connection, and daily-send history.

## 🧰 Technology stack

| Layer | Technology |
| --- | --- |
| Web application | Python, Flask, Flask-Login |
| Storage | SQLite |
| Gmail integration | Gmail API, Google OAuth 2.0 |
| Date handling | `python-dateutil` |
| Production server | Gunicorn |
| Scheduling | systemd service and timer units |
| Deployment | Docker or Google Compute Engine behind IAP |
| Frontend | Server-rendered Flask templates, HTML, CSS, and JavaScript |

## 🚀 Run locally

### Prerequisites

- Python 3
- `venv` support
- A browser
- Gmail OAuth credentials only if scanning or reminders are needed

### Install and start

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
```

On first run, the app creates `bills.db` and `flask_secret_key.txt` automatically. Nothing needs to be configured by hand for basic bill tracking.

Open [http://127.0.0.1:5432](http://127.0.0.1:5432) and click **Sign up** to create the first account.

Gmail scanning and reminders are optional. The application is fully usable without them.

For a convenient local launcher, use [`run.sh`](run.sh), which sets `FLASK_DEBUG=1` and opens the browser:

```bash
./run.sh
```

### Gmail-enabled local setup

Save the downloaded OAuth client secret as `credentials.json` in the project root, then complete the per-person authorization flow from the Review page. Do not commit the credentials or generated local data files.

## ✅ Verification

Run the application directly:

```bash
venv/bin/python app.py
```

For a manual end-to-end check:

1. Register a new account and log in.
2. Add a bill and confirm it appears only for that account.
3. Add a recurring bill, mark it paid, and verify the next occurrence is created with a rolled-forward due date.
4. Configure a reminder lead time and confirm the Review page identifies qualifying bills.
5. If Gmail is configured, connect an account and review a candidate before approving it.
6. Send a reminder manually and confirm it reaches the authorized Gmail account.
7. Connect a second account and verify its bills and Gmail data remain isolated from the first account.
8. Toggle the theme color and dark mode, refresh the page, and confirm the settings persist.

## ☁️ Deployment

The included deployment path runs the application on a Google Compute Engine VM with no public IP. Access is through an authenticated IAP tunnel, so only identities granted the necessary Google IAM roles can reach the app.

The VM setup uses Gunicorn behind a systemd service and forwards the internal application port to `http://127.0.0.1:5432` on the local machine. The complete setup, update, data-copy, Gmail OAuth, and reminder-timer instructions are in [`deploy/README.md`](deploy/README.md).

For a fresh VM, the deployment flow is:

1. Review and run [`deploy/gcp-setup.sh`](deploy/gcp-setup.sh).
2. Copy the application to the VM through IAP.
3. Install Python dependencies in a VM virtual environment.
4. Install and enable `deploy/billtracker.service`.
5. Start an IAP tunnel to the internal application port.
6. Optionally install the system-level `billtracker-reminders.timer`.

The deployment guide also documents a local user-level reminder timer. Both timers default to 8:00 AM and can be changed in their `.timer` files.

## 🗂️ Project structure

```text
BillTracker/
|- app.py                         # Flask routes, authentication, bills, Gmail, reminders
|- db.py                          # SQLite schema, migrations, and data access
|- gmail_scraper.py               # Gmail OAuth, scanning, and message matching
|- daily_reminders.py             # Scheduled reminder entry point
|- requirements.txt               # Python dependencies
|- run.sh                         # Local debug launcher
|- Dockerfile                     # Container build
|- templates/                     # Server-rendered application pages
|- static/                        # Stylesheet and favicon
|- deploy/                        # GCP, systemd, and deployment documentation
|- bills.db                       # Local private SQLite database, generated at runtime
|- credentials.json               # Local Gmail OAuth client secret, not committed
`- flask_secret_key.txt           # Generated Flask secret, not committed
```

## 🔐 Security and local data

`credentials.json`, `bills.db`, and `flask_secret_key.txt` are gitignored because they contain credentials, secrets, or private financial data. Do not commit them.

A legacy `token.json` may exist from before Gmail tokens moved into the database. The application no longer reads it and it can be deleted after confirming it is no longer needed.

When copying the application to a cloud VM, the deployment process deliberately does not copy `bills.db` or `token.json` automatically. This prevents financial data and Gmail authorization data from being duplicated without an explicit decision.

The application is designed for a small trusted household. If it is exposed beyond that setting, add a hardened production boundary such as HTTPS, stronger operational monitoring, backups, and a more formal account-recovery and session-management policy.
