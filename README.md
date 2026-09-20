# Bill Tracker

Bill tracker with a Flask web UI, SQLite storage, and an optional Gmail
scan that finds candidate bill emails for you to review and approve.
Nothing from Gmail is added to your bill list automatically - matches sit
in a review queue until you confirm vendor/amount/due date.

Supports multiple people sharing one instance, each with their own login
and their own private bills/incomes/reminders/Gmail connection - see
"Accounts" below.

Bills can repeat on a set schedule (every N days/weeks/months/years) - when
you mark one paid, the next occurrence is created automatically with the
due date rolled forward.

It can also email you a reminder (to your own Gmail address) for bills
that are overdue, due soon, or missing a due date - automatically once a
day, or on demand from the Review page. See "Gmail setup" below.

## Set up from a fresh clone

```
git clone <this repo's URL>
cd BillTracker
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
```

First run creates `bills.db` (SQLite) and `flask_secret_key.txt`
automatically - nothing to set up by hand. Open http://127.0.0.1:5432 and
click **Sign up** to create the first account.

Gmail scanning/reminders are optional and per-account - see "Gmail setup"
below whenever you want them; the app works fully without it, just without
automatic bill detection or email reminders.

## Run it day to day

```
venv/bin/python app.py
```

(or write yourself a small launcher script like this project's own
`run.sh`, which also sets `FLASK_DEBUG=1` and opens your browser)

Open http://127.0.0.1:5432 if it doesn't open automatically.

Want this reachable from somewhere other than this machine? See
[`deploy/README.md`](deploy/README.md) for running it on a Compute Engine
VM with no public IP, reachable only through an authenticated IAP tunnel
(or a Cloudflare Tunnel, for a stable URL other people can use directly).

## Accounts

Every person using the app creates their own login at `/signup` (email +
password). Everything - bills, incomes, categories, reminder settings,
Gmail connection - is private to that account; nobody sees anyone else's
data. Once logged in, a device stays logged in for a year (this is meant
for a small trusted household, not a public service, so there's no
repeated password-prompting).

## Personalizing your account

The Settings page (top bar) lets each account customize a few things,
saved per-account and independent of anyone else sharing the instance:

- **Greeting** - replace the default "Hi, <name>" shown at the top of
  every page with your own text. Leave the field blank and save to go
  back to the default.
- **Theme color** - replace the app's default purple accent everywhere
  it's used (topbar, buttons, active tabs, category icon badges, badges,
  etc.) with a color of your choice. Pick one of the 22 preset swatches
  for an instant one-click change, or click "Custom color..." to reveal a
  full color picker for any hex value. "Reset to default" clears it and
  goes back to the built-in purple. Under the hood, only your chosen
  color is stored - the app derives matching darker/soft/on-color shades
  from it automatically (via HLS color math), and picks white or dark
  text over it automatically depending on how light the color is, so
  contrast stays readable no matter what you pick.
- **Dark mode** - a toggle switch that swaps the whole app to a dark
  background with light text. Works together with theme color - the
  accent color you picked (or the default purple) is automatically
  re-derived with dark-mode-appropriate shades rather than reusing the
  light-mode ones.
- **Bill icons** - when adding or editing a bill, pick any icon from the
  built-in icon pack to represent it, instead of the one guessed from its
  category.

## Multiple Gmail accounts

Each person's account can connect more than one Gmail account, not just
one - e.g. a personal inbox and a shared household/bills inbox. From
Settings > Gmail Accounts:

- **Connect a Gmail account** - starts the same consent-screen flow as
  before; repeat it to add another account (it always shows Google's
  account chooser, so you can pick a different Google account each time).
- **Reconnect** - re-grants one specific connected account whose token
  expired or was revoked, without creating a duplicate.
- **Disconnect** - removes one connected account; the others keep working.

"Scan Gmail now" scans every connected account and merges the results
into one pending-review queue. "Send reminder now" (and the daily
auto-reminder) sends the same digest of unpaid bills to each connected
account, from that account to itself - so if you connect two inboxes,
both get a copy of the reminder. One account's expired token doesn't
block the others; the scan/reminder result tells you if an account needs
reconnecting.

## Gmail setup (required only if you want the scan or reminder features)

This is per-account - each person who wants Gmail scanning/reminders sets
this up once, tied to their own Google identity. The OAuth *client*
(`credentials.json`) is shared app-wide and only needs to be created once
by whoever's running the instance; the OAuth *authorization* (clicking
through Google's consent screen) is done individually by each person.

**One-time, done by whoever's running the instance:**

1. Go to https://console.cloud.google.com/ and create a project (or reuse one).
2. Enable the **Gmail API** for that project (APIs & Services > Library).
3. Go to APIs & Services > Credentials > Create Credentials > OAuth client ID.
   - Application type: **Web application**.
   - Authorized redirect URI: `http://localhost:5432/oauth2callback`
     (same value whether you're running truly locally or reaching the app
     through a tunnel - the browser's address bar reads `localhost` either
     way). If you're running on a different port, adjust accordingly, or
     set the `OAUTH_REDIRECT_URI` environment variable to override it.
4. Configure the OAuth consent screen if prompted (User type: External is
   fine for personal/family use; add each Google account that'll use
   Gmail features as a test user, unless you publish the app).
5. Download the client secret JSON and save it as `credentials.json` in
   the project root.

**Per-person, done by each account once:**

1. Log in, click "Scan Gmail now" or "Send reminder now" on the Review
   page. A browser window opens for you to sign in and grant access.
2. Your resulting token is saved under your own account (in `bills.db`,
   not a shared file), so you won't need to re-auth every time (until it
   expires) - and it only ever affects your own scanning/reminders, never
   anyone else's.

The app requests two scopes: `gmail.readonly` (for scanning) and
`gmail.send` (for reminder emails) - it never sends anything except the
reminder emails it composes itself, and never deletes or modifies
anything in your mailbox. If your saved token predates the reminder
feature, it only has the read-only scope; the app detects this
automatically and the next click of "Send reminder now" (or the daily
auto-check, once you've granted it interactively once) prompts you to
re-consent and add the send permission.

`credentials.json`, `bills.db`, and `flask_secret_key.txt` are all
gitignored since they're either secrets or local/private data - don't
commit them. (A legacy `token.json` file may exist from before Gmail
tokens moved into the database - it's no longer read by the app and can
be deleted.)

The daily auto-check never opens a browser or blocks a page load waiting
on consent - if your saved token doesn't yet have `gmail.send`, it just
skips sending silently and tries again the next day. Only clicking "Send
reminder now" yourself can trigger the interactive consent screen.

## How the Gmail matching works

- Searches recent mail (default: last 45 days) for messages that look like
  bills/invoices/statements based on subject keywords.
- For each match, pulls a dollar amount and a due date out of the email
  body using regex/heuristics - this is approximate and will sometimes
  miss or misread things.
- Inserts each new match (deduped by Gmail message ID) into a pending
  queue on the Review page, where you edit and approve or reject it
  before it becomes a real bill.
- Skips any vendor you've already added manually or approved from a past
  scan - matched by sender domain (e.g. `billing@comcast.com`) or by an
  exact vendor name match, so recurring bills you've already tracked don't
  keep resurfacing for review. Bills you reject are not remembered, so a
  rejected match can come back in a later scan.

To change what it searches for, edit `DEFAULT_QUERY` in `gmail_scraper.py`
(it's a normal Gmail search string, same syntax as the Gmail search box).

## How reminders work

- A reminder covers every one of your unpaid bills that's overdue, due
  within your configured lead time (see Reminder Settings - defaults to 3
  days, customizable generally or per-vendor), or has no due date set.
- Skipped entirely if nothing currently qualifies for you - no empty "all
  clear" emails.
- By default, each account's check runs at most once per calendar day,
  triggered the next time that person loads the main dashboard - so it
  fires the first time you open the app on a given day, not on a fixed
  clock time. It only actually sends if Gmail is already authorized with
  the send scope (see above).
- For a reminder that fires on a real schedule even if nobody opens the
  app that day, install the `daily_reminders.py` systemd timer - see
  "Daily reminder timer" in [`deploy/README.md`](deploy/README.md). It
  loops over every account and sends each person's own digest using their
  own settings, bills, and Gmail connection.
- "Send reminder now" on the Review page bypasses the once-per-day limit,
  for testing or an on-demand nudge - and is the only path that can
  trigger the Gmail consent screen if it hasn't been granted yet.
- The email goes to whatever address your authorized Google account
  reports as its own (`users.getProfile`) - i.e. the same Gmail account
  you authorized, sent to itself.
