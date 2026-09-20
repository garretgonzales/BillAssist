import colorsys
import math
import os
import re
import secrets
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta
from flask import Flask, redirect, render_template, request, session, url_for
from markupsafe import Markup
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import gmail_scraper

app = Flask(__name__)
db.init_db()

# Needed to sign the session cookie that holds the OAuth "state" value
# between /gmail/authorize and /oauth2callback. Persisted to a gitignored
# local file (like token.json/credentials.json) so it survives restarts -
# a fresh key on every restart would invalidate any in-flight OAuth
# redirect and log everyone else out of their session.
_SECRET_KEY_PATH = Path(__file__).parent / "flask_secret_key.txt"
if not _SECRET_KEY_PATH.exists():
    _SECRET_KEY_PATH.write_text(secrets.token_hex(32))
app.secret_key = _SECRET_KEY_PATH.read_text().strip()

# A family device stays logged in indefinitely once it signs in - this is a
# small trusted-household app, not a public service, so re-prompting for a
# password on every visit is friction with no real security benefit here.
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=365)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# Browsers (and Cloudflare's edge cache) hang onto static/style.css for
# hours at a time - appending its own last-modified time as a query
# string means a new deploy is automatically a new URL, so it's always
# fetched fresh with zero action needed from anyone (no Incognito, no
# manual cache clear). Recomputed per-request rather than cached at
# startup, so it's correct immediately after a deploy without a restart
# race - reading one file's mtime is cheap enough not to matter.
@app.context_processor
def inject_versioned_static():
    def versioned_static(filename):
        path = os.path.join(app.static_folder, filename)
        try:
            version = int(os.path.getmtime(path))
        except OSError:
            version = 0
        return f"{url_for('static', filename=filename)}?v={version}"
    return dict(versioned_static=versioned_static)


# A bill's category is free text (never a fixed enum), so this maps by
# keyword rather than exact match - "Home Internet" and "internet bill"
# both still land on the wifi icon. Falls back to a generic bill icon for
# anything that matches nothing, rather than showing no icon at all.
CATEGORY_ICON_KEYWORDS = {
    "health": "heart-medical",
    "medical": "heart-medical",
    "medication": "capsule",
    "pharmacy": "capsule",
    "grocer": "shopping-cart",
    "shopping": "shopping-bag",
    "fitness": "dumbbell",
    "gym": "dumbbell",
    "transport": "car-sideview",
    "car": "car-sideview",
    "auto": "car-sideview",
    "parking": "parking-circle",
    "gas": "pump",
    "fuel": "pump",
    "travel": "plane-departure",
    "education": "graduation-cap",
    "school": "graduation-cap",
    "book": "books",
    "internet": "wifi",
    "wifi": "wifi",
    "phone": "phone",
    "mobile": "phone",
    "electric": "plug",
    "utilit": "plug",
    "bank": "credit-card",
    "credit": "credit-card",
    "loan": "dollar-alt",
    "insurance": "lock",
    "entertain": "music",
    "music": "music",
    "stream": "music",
    "subscription": "envelope",
    "shipping": "truck",
    "delivery": "truck",
    "gift": "gift",
    "store": "store",
    "retail": "store",
    "meal": "crockery",
    "food": "crockery",
    "dining": "crockery",
    "restaurant": "crockery",
}
DEFAULT_CATEGORY_ICON = "bill"

# Every icon a user can explicitly pick when adding/editing a bill -
# (slug, display name), in the same order as the source icon pack.
# Slugs match the filenames in static/icons/categories/.
ICON_CHOICES = [
    ("bill", "Bill"),
    ("envelope", "Envelope"),
    ("envelope-download", "Scan Envelope"),
    ("graduation-cap", "Education"),
    ("heart-medical", "Health"),
    ("lock", "Lock"),
    ("gift", "Gift"),
    ("store", "Store"),
    ("shopping-cart", "Groceries"),
    ("shopping-bag", "Shopping"),
    ("dumbbell", "Fitness"),
    ("car-sideview", "Transportation"),
    ("truck", "Shipping"),
    ("star", "Custom"),
    ("phone", "Phone"),
    ("usd-circle", "Money Circle"),
    ("credit-card", "Credit Card"),
    ("dollar-alt", "Dollar Sign"),
    ("wifi", "Internet"),
    ("books", "Books"),
    ("capsule", "Medication"),
    ("parking-circle", "Parking"),
    ("crockery", "Meal"),
    ("plane-departure", "Travel"),
    ("plug", "Electricity"),
    ("music", "Music"),
    ("pump", "Gas"),
]


@lru_cache(maxsize=64)
def _load_category_icon_svg(slug):
    path = os.path.join(app.static_folder, "icons", "categories", f"{slug}.svg")
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _category_icon_slug(category):
    key = (category or "").strip().lower()
    for keyword, icon_slug in CATEGORY_ICON_KEYWORDS.items():
        if keyword in key:
            return icon_slug
    return DEFAULT_CATEGORY_ICON


@app.context_processor
def inject_category_icon():
    def category_icon(category):
        return Markup(_load_category_icon_svg(_category_icon_slug(category)))

    def bill_icon(bill):
        # A user-picked icon always wins over the category-keyword guess -
        # this is what add/edit bill's icon picker sets.
        slug = bill["icon"] if bill["icon"] else _category_icon_slug(bill["category"])
        return Markup(_load_category_icon_svg(slug))

    def icon_svg(slug):
        return Markup(_load_category_icon_svg(slug))

    return dict(category_icon=category_icon, bill_icon=bill_icon, icon_svg=icon_svg, icon_choices=ICON_CHOICES)


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_ACCENT = "#4a3aeb"
THEME_COLOR_PRESETS = [
    "#EEE3AB", "#80A1C1", "#B7FFD8", "#FFC1CF", "#63B0CD", "#39393A",
    "#1B512D", "#FFEE88", "#2C2C54", "#F49D6E", "#C97064", "#F26430",
    "#2A2D34", "#DC493A", "#4392F1", "#392759", "#A599B5", "#CDDDDD",
    "#F1DAC4", "#690500", "#934B00", "#BB6B00",
]

# Dark-mode equivalents of the neutral (non-accent) :root variables in
# style.css - dark tinted backgrounds with lighter foreground text of the
# same hue family, inverted from the light theme's light-bg/dark-text
# pairing. Accent variables aren't here since they're derived per-request
# from the user's chosen (or default) accent color instead - see
# _derive_theme_shades.
DARK_VARS = {
    "bg": "#121218",
    "card-bg": "#1c1c24",
    "border": "#2e2e38",
    "text": "#f2f2f6",
    "text-muted": "#9a9aa8",
    "danger-bg": "#3a1f1f",
    "danger-border": "#6b3232",
    "danger-text": "#f4938c",
    "paid-bg": "#16301f",
    "paid-text": "#6fd695",
    "unpaid-bg": "#3a2c12",
    "unpaid-text": "#f0a94e",
    # Flat neutral chip (plain buttons/links/notes) - a lighter gray than
    # --card-bg so those elements still read as a distinct "chip" instead
    # of blending into the card, same relationship as the light theme.
    "btn-bg": "#2a2a34",
    "btn-bg-hover": "#34343f",
    "veil-bg": "rgba(18, 18, 24, 0.55)",
}


def _derive_theme_shades(hex_color, dark=False):
    """Every place in style.css that currently hardcodes the purple
    (--accent/--accent-dark/--accent-soft/--on-accent) reads it from
    these CSS custom properties instead of its own literal color - so
    overriding just these four at :root re-themes every icon/tab/button
    that already uses them, with no other CSS changes needed. Derived
    with HLS math (stdlib colorsys) rather than asking the user to pick
    four coordinated colors themselves."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)

    def shade(lightness, saturation):
        rr, gg, bb = colorsys.hls_to_rgb(h, max(0.0, min(1.0, lightness)), max(0.0, min(1.0, saturation)))
        return "#{:02x}{:02x}{:02x}".format(round(rr * 255), round(gg * 255), round(bb * 255))

    # Perceived brightness (standard luminance weights) decides whether
    # white or dark text reads better on top of the chosen color - a
    # user picking a pale/light color still needs readable button text.
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    # accent-soft is a pale wash used as a small badge/notice background -
    # in dark mode a near-white wash (the light-mode formula) would be a
    # jarring bright patch, so it's a dark tinted wash instead.
    soft_lightness = 0.24 if dark else 0.94
    return {
        "accent": hex_color,
        "accent-dark": shade(l - 0.12, s),
        "accent-soft": shade(soft_lightness, min(1.0, s * 0.6 + 0.15)),
        "on-accent": "#16161f" if luminance > 0.6 else "#ffffff",
    }


@app.context_processor
def inject_theme_style():
    def theme_style():
        is_dark = current_user.is_authenticated and current_user.theme_mode == "dark"
        color = current_user.theme_color if current_user.is_authenticated else None
        if not color or not HEX_COLOR_RE.match(color):
            # Dark mode still needs accent-soft/on-accent recomputed for
            # dark backgrounds even with no custom color chosen, so it
            # falls back to deriving from the default purple instead of
            # skipping the override entirely.
            color = DEFAULT_ACCENT if is_dark else None

        declarations = ""
        if color:
            shades = _derive_theme_shades(color, dark=is_dark)
            declarations += "".join(f"--{name}:{value};" for name, value in shades.items())
        if is_dark:
            declarations += "".join(f"--{name}:{value};" for name, value in DARK_VARS.items())

        if not declarations:
            return ""
        return Markup(f"<style>:root{{{declarations}}}</style>")
    return dict(theme_style=theme_style, theme_color_presets=THEME_COLOR_PRESETS)


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.email = row["email"]
        self.custom_greeting = row["custom_greeting"]
        self.name = row["name"]
        self.theme_color = row["theme_color"]
        self.theme_mode = row["theme_mode"]


@login_manager.user_loader
def load_user(user_id):
    row = db.get_user_by_id(user_id)
    return User(row) if row else None


RECURRENCE_UNITS = ("day", "week", "month", "year")
REMINDER_LEAD_UNITS = ("day", "week")
REMINDER_WINDOW_DAYS = 3  # default lead time for bills with no custom setting

PIE_RADIUS = 52
PIE_STROKE = 18
PIE_CIRCUMFERENCE = 2 * math.pi * PIE_RADIUS
PIE_PALETTE = ["#4f83cc", "#8fb8e8", "#22c55e", "#f59e0b", "#fb7185", "#38bdf8", "#f472b6", "#facc15"]


@app.template_filter("mmddyyyy")
def format_mmddyyyy(value):
    """Dates are stored as ISO (yyyy-mm-dd) for sorting/comparison and for
    <input type="date"> fields, which require that format - this only
    reformats how a stored date is displayed as text."""
    if not value:
        return value
    return date.fromisoformat(value).strftime("%m/%d/%Y")


def _build_pie_slices(by_category, total):
    """Donut-chart slice geometry (SVG stroke-dasharray/offset), keyed off
    the same unpaid total shown in the dashboard tile above it."""
    slices = []
    cumulative = 0.0
    for i, row in enumerate(by_category):
        if not total:
            break
        pct = row["total"] / total * 100
        arc = PIE_CIRCUMFERENCE * (pct / 100)
        slices.append({
            "category": row["cat"],
            "total": row["total"],
            "pct": pct,
            "color": PIE_PALETTE[i % len(PIE_PALETTE)],
            "dash": f"{arc:.2f} {PIE_CIRCUMFERENCE - arc:.2f}",
            "offset": f"-{cumulative:.2f}",
        })
        cumulative += arc
    return slices


def _next_month_bounds():
    start = date.today().replace(day=1) + relativedelta(months=1)
    end = start + relativedelta(months=1)
    return start.isoformat(), end.isoformat()


def _parse_recurrence(form):
    interval = form.get("recurrence_interval", "").strip()
    unit = form.get("recurrence_unit", "").strip()
    if not interval or unit not in RECURRENCE_UNITS:
        return None, None
    return unit, int(interval)


def _next_due_date(due_date, unit, interval):
    base = date.today()
    if due_date:
        try:
            base = date.fromisoformat(due_date)
        except ValueError:
            pass
    return (base + relativedelta(**{f"{unit}s": interval})).isoformat()


def _resolve_category(user_id, form):
    """A submitted 'new_category' wins over the dropdown pick, and gets
    saved to the categories list so it shows up next time."""
    new_category = form.get("new_category", "").strip()
    if new_category:
        db.add_category(user_id, new_category)
        return new_category
    return form.get("category", "").strip() or None


def _parse_bill_form(user_id, form):
    amount = form.get("amount", "").strip()
    total_amount = form.get("total_amount", "").strip()
    total_payments = form.get("total_payments", "").strip()
    recurrence_unit, recurrence_interval = _parse_recurrence(form)
    return dict(
        vendor=form["vendor"].strip(),
        amount=float(amount) if amount else None,
        due_date=form.get("due_date", "").strip() or None,
        category=_resolve_category(user_id, form),
        notes=form.get("notes", "").strip() or None,
        recurrence_unit=recurrence_unit,
        recurrence_interval=recurrence_interval,
        total_amount=float(total_amount) if total_amount else None,
        total_payments=int(total_payments) if total_payments else None,
        until_cancelled=1 if form.get("until_cancelled") else 0,
        icon=form.get("icon", "").strip() or None,
    )


def _parse_income_form(form):
    amount = form.get("amount", "").strip()
    recurrence_unit, recurrence_interval = _parse_recurrence(form)
    return dict(
        label=form.get("label", "").strip() or "Paycheck",
        amount=float(amount) if amount else 0,
        pay_date=form.get("pay_date", "").strip() or None,
        recurrence_unit=recurrence_unit,
        recurrence_interval=recurrence_interval,
    )


def _backfill_recurring(user_id, month_after_next_start):
    """Keep every recurring bill's next occurrence already scheduled, so a
    repeating bill keeps going indefinitely - independent of whether anyone
    ever marks the current one paid - until someone deletes it. Each bill
    gets at most one child (enforced by list_recurring_without_child), and
    this catches a chain up across however many missed periods (e.g. the
    app wasn't opened for a few months) rather than advancing one step."""
    for _ in range(120):  # safety cap; a normal run does 1-3 passes
        candidates = db.list_recurring_without_child(user_id)
        created_any = False
        for bill in candidates:
            next_due = _next_due_date(bill["due_date"], bill["recurrence_unit"], bill["recurrence_interval"])
            if bill["due_date"] and next_due >= month_after_next_start:
                continue  # already covers this-month-and-next; no need to go further yet
            db.add_bill(
                user_id=user_id,
                vendor=bill["vendor"],
                amount=bill["amount"],
                due_date=next_due,
                category=bill["category"],
                notes=bill["notes"],
                source=bill["source"],
                source_domain=bill["source_domain"],
                recurrence_unit=bill["recurrence_unit"],
                recurrence_interval=bill["recurrence_interval"],
                recurrence_parent_id=bill["id"],
                reminder_lead_value=bill["reminder_lead_value"],
                reminder_lead_unit=bill["reminder_lead_unit"],
                total_amount=bill["total_amount"],
                total_payments=bill["total_payments"],
                until_cancelled=bill["until_cancelled"],
                icon=bill["icon"],
            )
            created_any = True
        if not created_any:
            break


def _income_occurrences_in_window(income, start, end):
    """Every date this income lands on within [start, end) - projected
    from the single stored row rather than materialized as extra rows,
    since an income has no per-occurrence state (nothing to mark paid)
    the way a bill does. A recurring income can land more than once in a
    window (e.g. biweekly can occur twice in one month); each occurrence
    yielded still shares the one underlying row, so editing or deleting
    it always acts on that single definition, not a specific date."""
    if not income["pay_date"]:
        return
    if not income["recurrence_unit"]:
        if start <= income["pay_date"] < end:
            yield income["pay_date"]
        return
    step = relativedelta(**{f"{income['recurrence_unit']}s": income["recurrence_interval"]})
    occurrence = date.fromisoformat(income["pay_date"])
    while occurrence.isoformat() < start:
        occurrence += step
    while occurrence.isoformat() < end:
        yield occurrence.isoformat()
        occurrence += step


def _describe_due(due_date, today):
    if not due_date:
        return "no due date set"
    try:
        delta = (date.fromisoformat(due_date) - today).days
    except ValueError:
        return "no due date set"
    if delta < 0:
        return f"overdue by {-delta} day{'s' if delta != -1 else ''}"
    if delta == 0:
        return "due today"
    return f"due in {delta} day{'s' if delta != 1 else ''}"


def _effective_lead_days_list(user_id, bill):
    """All configured lead times (in days) that apply to this bill - its
    vendor's own reminder_leads rows if any exist, else the general
    (vendor-less) ones. Reminders are a single daily digest, not a separate
    email per threshold, so multiple entries widen how far out a bill starts
    showing up rather than firing distinct pings - it surfaces every day
    from the farthest configured lead until it's paid."""
    vendor_leads = db.list_reminder_leads(user_id, bill["vendor"])
    leads = vendor_leads if vendor_leads else db.list_general_reminder_leads(user_id)
    days = [lead["lead_value"] * (7 if lead["lead_unit"] == "week" else 1) for lead in leads]
    return days or [REMINDER_WINDOW_DAYS]  # safety net if somehow nothing is configured


def _bills_due_for_reminder(user_id, today):
    """Unpaid bills worth a reminder today: no due date at all (so they
    aren't silently skipped just for lacking one), or due within that
    bill's own farthest lead time (which also naturally covers anything
    overdue, since an overdue due_date is always <= any non-negative
    cutoff)."""
    due = []
    for bill in db.list_bills(user_id, status="unpaid"):
        if not bill["due_date"]:
            due.append(bill)
            continue
        cutoff = (today + relativedelta(days=max(_effective_lead_days_list(user_id, bill)))).isoformat()
        if bill["due_date"] <= cutoff:
            due.append(bill)
    return due


def _estimate_per_payment(total_amount, total_payments, until_cancelled):
    """Total amount divided evenly across the number of payments - None
    (displayed as "-") unless a bill has both set, and never computed for
    an until_cancelled (open-ended) bill, which has no fixed total to
    divide in the first place."""
    if until_cancelled or not total_amount or not total_payments:
        return None
    return total_amount / total_payments


def _estimate_payments_needed(total_amount, amount):
    """How many payments of the bill's own recurring amount it would take
    to clear a given total - rounded up, since a partial final payment
    still counts as one more payment. Used whenever a bill has a total
    amount but no explicit payment count entered, whether or not it's
    also marked until_cancelled - a total to clear is a total to clear
    either way."""
    if not total_amount or not amount:
        return None
    return math.ceil(total_amount / amount)


def _build_reminder_email(bills, today):
    total = sum(b["amount"] or 0 for b in bills)
    lines = [f"You have {len(bills)} bill(s) needing attention:", ""]
    for b in bills:
        amount = f"${b['amount']:.2f}" if b["amount"] is not None else "amount unknown"
        lines.append(f"- {b['vendor']}: {amount}, {_describe_due(b['due_date'], today)}")
    lines += ["", f"Total: ${total:.2f}", "", "- Bill Tracker"]
    subject = f"Bill Tracker: {len(bills)} bill{'s' if len(bills) != 1 else ''} need attention"
    return subject, "\n".join(lines)


def _send_reminder_now(user_id):
    """Sends unconditionally (no once-per-day check) and logs it. Raises on
    failure - callers decide whether that's shown to a user or swallowed.

    Sends one copy of the same digest (covering every one of this user's
    unpaid bills, not just ones matched by a particular inbox) to each
    connected Gmail account, from that account to itself - mirroring the
    single-account behavior this generalizes from. One account's expired
    token doesn't block the others; this only raises if every connected
    account failed."""
    today = date.today()
    bills = _bills_due_for_reminder(user_id, today)
    if not bills:
        return 0
    accounts = db.list_gmail_accounts(user_id)
    if not accounts:
        raise gmail_scraper.NoCredentials(
            "Gmail needs (re-)authorization for this - click the button once to grant it."
        )
    subject, body = _build_reminder_email(bills, today)
    sent_any = False
    last_exc = None
    for account in accounts:
        try:
            service = gmail_scraper.get_service(account["id"])
            to_address = gmail_scraper.get_own_email_address(service)
            gmail_scraper.send_email(service, to_address, subject, body)
            sent_any = True
        except Exception as exc:
            last_exc = exc
    if not sent_any and last_exc:
        raise last_exc
    db.log_reminder_sent(user_id)
    return len(bills)


def _maybe_send_daily_reminder(user_id):
    """Auto-sends at most once per calendar day, and only if Gmail is
    already fully authorized - never blocks a page load on a consent
    screen. Any failure here is silent; the manual button surfaces errors."""
    last_sent = db.get_last_reminder_sent(user_id)
    # db.now() stores UTC timestamps, so compare against a UTC date here -
    # comparing against date.today() (local) could be off by a day right
    # around midnight depending on timezone, causing a double-send or a
    # skipped day.
    today_utc = datetime.now(timezone.utc).date().isoformat()
    if last_sent and last_sent[:10] == today_utc:
        return
    try:
        today = date.today()
        bills = _bills_due_for_reminder(user_id, today)
        if not bills:
            return
        accounts = db.list_gmail_accounts(user_id)
        if not accounts:
            return
        subject, body = _build_reminder_email(bills, today)
        sent_any = False
        for account in accounts:
            try:
                service = gmail_scraper.get_service(account["id"], allow_interactive=False)
                to_address = gmail_scraper.get_own_email_address(service)
                gmail_scraper.send_email(service, to_address, subject, body)
                sent_any = True
            except Exception:
                continue  # this account isn't authorized (yet) - others may still send fine
        if sent_any:
            db.log_reminder_sent(user_id)
    except Exception:
        pass  # not authorized yet, or a transient API error - try again next load


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        error = None
        if not email or not password:
            error = "Email and password are required."
        elif password != confirm:
            error = "Passwords don't match."
        elif db.get_user_by_email(email):
            error = "An account with that email already exists."
        if error:
            return render_template("signup.html", error=error, email=email, name=name)
        user_id = db.create_user(email, generate_password_hash(password), name or None)
        login_user(User(db.get_user_by_id(user_id)), remember=True)
        return redirect(url_for("index"))
    return render_template("signup.html", error=None, email="", name="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        row = db.get_user_by_email(email)
        if not row or not check_password_hash(row["password_hash"], password):
            return render_template("login.html", error="Invalid email or password.", email=email)
        login_user(User(row), remember=True)
        next_url = request.args.get("next")
        return redirect(next_url or url_for("index"))
    return render_template("login.html", error=None, email="")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    user_id = int(current_user.id)
    search = request.args.get("q", "").strip()
    tab = request.args.get("tab", "this_month")
    if tab not in ("this_month", "next_month"):
        tab = "this_month"
    status_filter = request.args.get("status", "unpaid")
    if status_filter not in ("unpaid", "paid"):
        status_filter = "unpaid"

    next_month_start, month_after_next_start = _next_month_bounds()
    _backfill_recurring(user_id, month_after_next_start)
    _maybe_send_daily_reminder(user_id)

    if tab == "next_month":
        bills = db.list_bills(
            user_id, status=status_filter, search=search or None,
            due_range=(next_month_start, month_after_next_start),
        )
    else:
        bills = db.list_bills(
            user_id, status=status_filter, search=search or None, due_before=next_month_start
        )

    bills_shown_total = sum(bill["amount"] or 0 for bill in bills)

    pending_count = len(db.list_pending_bills(user_id))
    categories = db.list_categories(user_id)

    summary, by_category = db.get_summary(user_id, next_month_start, month_after_next_start)
    pie_slices = _build_pie_slices(by_category, summary["this_month_all_total"])

    # Flip-side of the "Your bills" card - every distinct bill regardless of
    # month/status/search, so it never touches the totals or graph above.
    totals_bills, recurring_payment_total, tracked_total_amount = db.get_all_bills_totals(user_id, next_month_start)

    # Its own card, after "Your bills": one row per distinct recurring
    # bill with an estimated even per-payment amount (total / count) - "-"
    # wherever either isn't set, or the bill is until_cancelled.
    payment_estimates = []
    for bill in db.list_recurring_bill_estimates(user_id):
        payments_needed_estimate = (
            _estimate_payments_needed(bill["total_amount"], bill["amount"])
            if bill["total_payments"] is None else None
        )
        if payments_needed_estimate is not None:
            # # Payments itself is a calculated figure here (total_amount
            # / amount), so per-payment is just the bill's own amount by
            # construction - showing "-" for it would be inconsistent
            # with the # Payments column suddenly having a value.
            estimate = bill["amount"]
        else:
            estimate = _estimate_per_payment(
                bill["total_amount"], bill["total_payments"], bill["until_cancelled"]
            )
        payment_estimates.append({
            "bill": bill,
            "estimate": estimate,
            "payments_needed_estimate": payments_needed_estimate,
        })
    # Paychecks/expected income, bucketed by the same this-month/next-month
    # window as bills so "Net" below is a real answer to "can this month's
    # income cover what's due this month" - not just a flat running list.
    # Projected on the fly from each stored income (see
    # _income_occurrences_in_window) rather than reading extra rows, since
    # there's exactly one row per income ever - unlike bills, nothing about
    # an income is ever "paid", so there's nothing per-occurrence to store.
    if tab == "next_month":
        window_start, window_end = next_month_start, month_after_next_start
        due_total_for_tab = summary["next_month_total"]
        bills_total_for_tab = summary["next_month_all_total"]
    else:
        window_start, window_end = date.today().replace(day=1).isoformat(), next_month_start
        due_total_for_tab = summary["unpaid_total"]
        bills_total_for_tab = summary["this_month_all_total"]
    paid_for_tab = bills_total_for_tab - due_total_for_tab
    incomes = sorted(
        (
            {**dict(income), "pay_date": occurrence_date}
            for income in db.list_incomes(user_id)
            for occurrence_date in _income_occurrences_in_window(income, window_start, window_end)
        ),
        key=lambda i: i["pay_date"],
    )
    income_total = sum(income["amount"] or 0 for income in incomes)
    net_for_tab = income_total - paid_for_tab

    # Group by source name so duplicate-named incomes (a recurring income
    # landing twice in this window, or two separate entries someone gave
    # the same name) collapse into one row showing their combined total -
    # expandable to see each individually. A label that appears only once
    # stays as a single plain row (group.occurrences has length 1).
    income_groups_by_label = {}
    for income in incomes:
        income_groups_by_label.setdefault(income["label"], []).append(income)
    income_groups = [
        {"label": label, "total": sum(occ["amount"] or 0 for occ in occurrences), "occurrences": occurrences}
        for label, occurrences in income_groups_by_label.items()
    ]
    income_groups.sort(key=lambda g: g["occurrences"][0]["pay_date"])

    return render_template(
        "index.html",
        bills=bills,
        bills_shown_total=bills_shown_total,
        pending_count=pending_count,
        categories=categories,
        search=search,
        summary=summary,
        pie_slices=pie_slices,
        pie_circumference=PIE_CIRCUMFERENCE,
        tab=tab,
        status_filter=status_filter,
        totals_bills=totals_bills,
        recurring_payment_total=recurring_payment_total,
        tracked_total_amount=tracked_total_amount,
        payment_estimates=payment_estimates,
        incomes=incomes,
        income_groups=income_groups,
        income_total=income_total,
        due_total_for_tab=due_total_for_tab,
        bills_total_for_tab=bills_total_for_tab,
        paid_for_tab=paid_for_tab,
        net_for_tab=net_for_tab,
    )


@app.route("/bills/add", methods=["POST"])
@login_required
def add_bill():
    user_id = int(current_user.id)
    db.add_bill(user_id=user_id, source="manual", **_parse_bill_form(user_id, request.form))
    return redirect(url_for("index"))


@app.route("/bills/<int:bill_id>/edit", methods=["GET"])
@login_required
def edit_bill_form(bill_id):
    user_id = int(current_user.id)
    bill = db.get_bill(bill_id, user_id)
    if not bill:
        return redirect(url_for("index"))
    categories = db.list_categories(user_id)
    pending_count = len(db.list_pending_bills(user_id))
    return render_template("edit_bill.html", bill=bill, categories=categories, pending_count=pending_count)


@app.route("/bills/<int:bill_id>/edit", methods=["POST"])
@login_required
def edit_bill(bill_id):
    user_id = int(current_user.id)
    db.update_bill(bill_id, user_id, **_parse_bill_form(user_id, request.form))
    return redirect(url_for("index"))


@app.route("/bills/<int:bill_id>/pay", methods=["POST"])
@login_required
def pay_bill(bill_id):
    # A recurring bill's next occurrence is handled generically by
    # _backfill_recurring() on the index page this redirects to - paying
    # one doesn't need to special-case creating the next one here.
    db.set_bill_status(bill_id, int(current_user.id), "paid")
    return redirect(url_for("index"))


@app.route("/bills/<int:bill_id>/unpay", methods=["POST"])
@login_required
def unpay_bill(bill_id):
    db.set_bill_status(bill_id, int(current_user.id), "unpaid")
    return redirect(url_for("index"))


@app.route("/bills/<int:bill_id>/delete", methods=["POST"])
@login_required
def delete_bill(bill_id):
    db.delete_bill_chain(bill_id, int(current_user.id))
    return redirect(url_for("index"))


@app.route("/incomes/add", methods=["POST"])
@login_required
def add_income():
    fields = _parse_income_form(request.form)
    if fields["amount"] > 0:
        db.add_income(user_id=int(current_user.id), **fields)
    return redirect(url_for("index", tab=request.form.get("tab", "this_month")))


@app.route("/incomes/<int:income_id>/edit", methods=["GET"])
@login_required
def edit_income_form(income_id):
    user_id = int(current_user.id)
    income = db.get_income(income_id, user_id)
    if not income:
        return redirect(url_for("index"))
    pending_count = len(db.list_pending_bills(user_id))
    tab = request.args.get("tab", "this_month")
    return render_template("edit_income.html", income=income, pending_count=pending_count, tab=tab)


@app.route("/incomes/<int:income_id>/edit", methods=["POST"])
@login_required
def edit_income(income_id):
    db.update_income(income_id, int(current_user.id), **_parse_income_form(request.form))
    return redirect(url_for("index", tab=request.form.get("tab", "this_month")))


@app.route("/incomes/<int:income_id>/delete", methods=["POST"])
@login_required
def delete_income(income_id):
    db.delete_income(income_id, int(current_user.id))
    return redirect(url_for("index", tab=request.form.get("tab", "this_month")))


@app.route("/review")
@login_required
def review():
    user_id = int(current_user.id)
    message = request.args.get("message")
    no_new = request.args.get("no_new") == "1"
    pending = db.list_pending_bills(user_id)
    last_reminder_sent = db.get_last_reminder_sent(user_id)
    pending_count = len(pending)
    return render_template(
        "review.html",
        pending=pending,
        message=message,
        no_new=no_new,
        last_reminder_sent=last_reminder_sent,
        pending_count=pending_count,
    )


@app.route("/gmail/scan", methods=["POST"])
@login_required
def gmail_scan():
    no_new = False
    try:
        inserted, skipped, errors = gmail_scraper.scan(int(current_user.id))
        message = f"Scan complete: {inserted} new candidate(s) found, {skipped} already seen."
        if errors:
            message += f" {len(errors)} account(s) need reconnecting in Settings: {', '.join(errors)}."
        no_new = inserted == 0
    except gmail_scraper.NoCredentials:
        # Not authorized (or not authorized with enough scope) yet - send
        # the user's browser through consent, then they just click again.
        return redirect(url_for("gmail_authorize"))
    except Exception as exc:  # surfaces auth/API errors to the review page instead of a 500
        message = f"Gmail scan failed: {exc}"
    return redirect(url_for("review", message=message, no_new="1" if no_new else None))


@app.route("/reminders/send", methods=["POST"])
@login_required
def send_reminder():
    try:
        count = _send_reminder_now(int(current_user.id))
        message = (
            f"Reminder sent for {count} bill{'s' if count != 1 else ''}."
            if count else "Nothing due soon - no reminder needed."
        )
    except gmail_scraper.NoCredentials:
        return redirect(url_for("gmail_authorize"))
    except Exception as exc:
        message = f"Sending the reminder failed: {exc}"
    return redirect(url_for("review", message=message))


def _start_gmail_oauth(return_to, account_id=None):
    """A web app can't pop a browser open on the user's behalf the way a
    desktop script could - this sends their own browser to Google's
    consent screen instead, with /oauth2callback picking up the result.

    return_to is the route name to land back on afterward (its page shows
    the resulting message). account_id=None means "connect a new/any
    account"; passing one means "reconnect this specific already-connected
    account" - stashed in the session same as state/code_verifier since
    the callback is a separate request."""
    try:
        auth_url, state, code_verifier = gmail_scraper.get_authorization_url()
    except gmail_scraper.NoCredentials as exc:
        return redirect(url_for(return_to, message=str(exc)))
    session["oauth_state"] = state
    session["oauth_code_verifier"] = code_verifier
    session["oauth_account_id"] = account_id
    session["oauth_return_to"] = return_to
    return redirect(auth_url)


@app.route("/gmail/authorize")
@login_required
def gmail_authorize():
    """Entry point used when a scan/reminder action discovers there's no
    Gmail account connected at all - lands back on Review."""
    return _start_gmail_oauth("review")


@app.route("/settings/gmail/connect")
@login_required
def gmail_connect():
    """"Connect another Gmail account" from Settings - always starts a
    fresh connection (never reconnects one already-connected account),
    lands back on Settings."""
    return _start_gmail_oauth("settings")


@app.route("/settings/gmail/<int:account_id>/reconnect")
@login_required
def gmail_reconnect(account_id):
    """Re-grants one specific connected account whose token expired or was
    revoked, without creating a duplicate row for it."""
    if not db.get_gmail_account(account_id, int(current_user.id)):
        return redirect(url_for("settings"))
    return _start_gmail_oauth("settings", account_id=account_id)


@app.route("/settings/gmail/<int:account_id>/disconnect", methods=["POST"])
@login_required
def gmail_disconnect(account_id):
    db.delete_gmail_account(account_id, int(current_user.id))
    return redirect(url_for("settings"))


@app.route("/oauth2callback")
@login_required
def oauth2callback():
    state = session.get("oauth_state")
    code_verifier = session.get("oauth_code_verifier")
    account_id = session.pop("oauth_account_id", None)
    return_to = session.pop("oauth_return_to", "review")
    is_new_account = account_id is None
    try:
        gmail_scraper.exchange_code_for_token(
            int(current_user.id), state, request.url, code_verifier, account_id=account_id
        )
        # A newly connected account previously just sat there until
        # someone separately clicked "Scan Gmail now" - easy to miss with
        # multiple accounts, since connecting lands back on Settings, not
        # Review. Scanning immediately here means a fresh connection is
        # useful right away instead of silently doing nothing.
        if is_new_account:
            try:
                inserted, skipped, errors = gmail_scraper.scan(int(current_user.id))
                message = f"Gmail account connected - scan found {inserted} new candidate(s)."
                if errors:
                    message += f" {len(errors)} other account(s) need reconnecting."
            except gmail_scraper.NoCredentials:
                message = "Gmail account connected."
        else:
            message = "Gmail authorized - click Scan Gmail now or Send reminder now again."
    except Exception as exc:
        message = f"Gmail authorization failed: {exc}"
    return redirect(url_for(return_to, message=message))


@app.route("/settings")
@login_required
def settings():
    user_id = int(current_user.id)
    pending_count = len(db.list_pending_bills(user_id))
    return render_template(
        "settings.html",
        pending_count=pending_count,
        message=request.args.get("message"),
        gmail_accounts=db.list_gmail_accounts(user_id),
    )


@app.route("/settings/greeting", methods=["POST"])
@login_required
def save_greeting():
    text = request.form.get("custom_greeting", "").strip()
    db.set_custom_greeting(int(current_user.id), text or None)
    return redirect(url_for("settings"))


@app.route("/settings/theme", methods=["POST"])
@login_required
def save_theme_color():
    color = request.form.get("theme_color", "").strip()
    if request.form.get("reset") or not HEX_COLOR_RE.match(color):
        color = None
    db.set_theme_color(int(current_user.id), color)
    return redirect(url_for("settings"))


@app.route("/settings/theme-mode", methods=["POST"])
@login_required
def save_theme_mode():
    mode = "dark" if request.form.get("dark_mode") else None
    db.set_theme_mode(int(current_user.id), mode)
    return redirect(url_for("settings"))


@app.route("/reminders/settings")
@login_required
def reminder_settings():
    user_id = int(current_user.id)
    pending_count = len(db.list_pending_bills(user_id))
    vendors = db.list_distinct_vendors(user_id)
    selected_vendor = request.args.get("vendor", "").strip()
    if selected_vendor not in vendors:
        selected_vendor = None
    return render_template(
        "reminder_settings.html",
        pending_count=pending_count,
        vendors=vendors,
        selected_vendor=selected_vendor,
        general_leads=db.list_general_reminder_leads(user_id),
        vendor_leads=db.list_reminder_leads(user_id, selected_vendor) if selected_vendor else [],
        lead_units=REMINDER_LEAD_UNITS,
    )


@app.route("/reminders/settings/add", methods=["POST"])
@login_required
def add_reminder_lead():
    vendor = request.form.get("vendor", "").strip() or None
    lead_value = request.form.get("lead_value", "").strip()
    lead_unit = request.form.get("lead_unit", "").strip()
    if lead_value.isdigit() and int(lead_value) > 0 and lead_unit in REMINDER_LEAD_UNITS:
        db.add_reminder_lead(int(current_user.id), vendor, int(lead_value), lead_unit)
    return redirect(url_for("reminder_settings", vendor=vendor or ""))


@app.route("/reminders/settings/<int:lead_id>/delete", methods=["POST"])
@login_required
def delete_reminder_lead(lead_id):
    vendor = request.form.get("vendor", "").strip()
    db.delete_reminder_lead(lead_id, int(current_user.id))
    return redirect(url_for("reminder_settings", vendor=vendor))


@app.route("/review/<int:pending_id>/approve", methods=["POST"])
@login_required
def approve_pending(pending_id):
    user_id = int(current_user.id)
    pending = db.get_pending_bill(pending_id, user_id)
    if pending:
        vendor = request.form.get("vendor", pending["vendor"])
        amount = request.form.get("amount", "")
        due_date = request.form.get("due_date", "") or None
        recurrence_unit, recurrence_interval = _parse_recurrence(request.form)
        db.add_bill(
            user_id=user_id,
            vendor=vendor,
            amount=float(amount) if amount else None,
            due_date=due_date,
            source="gmail",
            source_domain=pending["sender_domain"],
            recurrence_unit=recurrence_unit,
            recurrence_interval=recurrence_interval,
        )
        db.delete_pending_bill(pending_id, user_id)
    return redirect(url_for("review"))


@app.route("/review/<int:pending_id>/reject", methods=["POST"])
@login_required
def reject_pending(pending_id):
    user_id = int(current_user.id)
    pending = db.get_pending_bill(pending_id, user_id)
    if pending:
        db.mark_message_rejected(user_id, pending["gmail_message_id"])
        db.delete_pending_bill(pending_id, user_id)
    return redirect(url_for("review"))


if __name__ == "__main__":
    # debug=True exposes Werkzeug's interactive debugger, which is a remote
    # code execution risk if it's ever reachable by anyone but you - default
    # it off, and have the local launcher script opt back in explicitly.
    # Bind 0.0.0.0 whenever PORT is set (Cloud Run/gunicorn-style convention,
    # and what a Compute Engine deployment needs) so it's reachable in that
    # setup; local runs default to 127.0.0.1-only.
    debug = os.environ.get("FLASK_DEBUG") == "1"
    port = int(os.environ.get("PORT", 5432))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(debug=debug, host=host, port=port)
