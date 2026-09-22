"""Scans Gmail for bill/invoice emails, and sends bill-reminder emails.

Nothing found by scanning is written to the real `bills` table directly -
matches land in `pending_bills` so a human confirms vendor/amount/due-date
before they count as a real bill. Email parsing is heuristic and gets
things wrong. Sending is limited to composing and sending reminder emails
this app writes itself - never anything read from Gmail.
"""
import base64
import json
import os
import re
from email.mime.text import MIMEText
from pathlib import Path

from dateutil import parser as dateparser
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import db

APP_DIR = Path(__file__).parent
CREDENTIALS_PATH = APP_DIR / "credentials.json"
TOKEN_PATH = APP_DIR / "token.json"

# Read-only scanning, plus send - needed for reminder emails. This app never
# reads, modifies, or deletes anything beyond what gmail.readonly grants,
# and gmail.send only ever sends messages this app itself composed.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Always a "Web application" OAuth client, whether the app is running
# truly locally or on a Compute Engine VM reached through an IAP tunnel -
# either way the browser's address bar (and thus the redirect target)
# reads http://localhost:PORT, since the tunnel forwards a local port to
# the VM. That's what makes one fixed redirect URI work in both setups.
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5432/oauth2callback")
if REDIRECT_URI.startswith("http://localhost") or REDIRECT_URI.startswith("http://127.0.0.1"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

DEFAULT_QUERY = (
    '(subject:(bill OR invoice OR statement OR "payment due" OR "amount due" OR autopay) '
    'OR "amount due" OR "payment due") newer_than:30d'
)

AMOUNT_RE = re.compile(r"\$\s?([0-9][0-9,]*\.\d{2})")
DUE_HINT_RE = re.compile(
    r"(due\s*(?:date|by|on)?\s*[:\-]?\s*)([A-Za-z0-9,\.\/\- ]{4,40})", re.IGNORECASE
)


class NoCredentials(Exception):
    """credentials.json is missing, the saved token predates a scope this
    app now needs, or there's no token at all - either way, a user must
    go through /gmail/authorize before this call can succeed. A web app
    can't pop a browser open itself the way a desktop script could, so
    this is raised for the caller (a Flask route) to redirect there."""


def _require_credentials_file():
    if not CREDENTIALS_PATH.exists():
        raise NoCredentials(
            f"Missing {CREDENTIALS_PATH}. Follow the Gmail setup steps in README.md first."
        )


def build_flow(state=None, code_verifier=None):
    _require_credentials_file()
    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_PATH), scopes=SCOPES, state=state, redirect_uri=REDIRECT_URI
    )
    if code_verifier:
        flow.code_verifier = code_verifier
    return flow


def get_authorization_url():
    """Starts the web OAuth flow: returns (url, state, code_verifier) -
    redirect the user's browser to `url`, and stash both `state` and
    `code_verifier` (e.g. in the session) to use when they land back on
    the callback route. Some OAuth clients now require PKCE - without
    passing the same verifier back at token-exchange time, Google
    rejects the exchange with 'Missing code verifier'.

    select_account forces Google's account chooser instead of silently
    reusing whichever Google account the browser last consented with -
    needed so a second/third "Connect another Gmail account" click can
    actually pick a different account instead of just re-granting the
    same one."""
    flow = build_flow()
    flow.autogenerate_code_verifier = True
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="select_account consent"
    )
    return auth_url, state, flow.code_verifier


def exchange_code_for_token(user_id, state, authorization_response_url, code_verifier, account_id=None):
    """Call from the /oauth2callback route with the state and code_verifier
    you stashed, and the full callback URL (request.url).

    account_id=None means "connect a new account": the resulting token is
    matched to an existing gmail_accounts row by email address (so
    re-granting the same Google account updates it in place instead of
    creating a duplicate), or inserted as a new row otherwise. Passing an
    account_id (reconnecting one specific already-connected account whose
    token expired/was revoked) updates that row directly. Returns the
    account_id that was created or updated."""
    flow = build_flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=authorization_response_url)
    token_json = flow.credentials.to_json()
    service = build("gmail", "v1", credentials=flow.credentials)
    email_address = get_own_email_address(service)

    if account_id is not None:
        db.update_gmail_account_token(account_id, token_json, email_address=email_address)
        return account_id

    existing = db.get_gmail_account_by_email(user_id, email_address)
    if existing:
        db.update_gmail_account_token(existing["id"], token_json, email_address=email_address)
        return existing["id"]
    return db.add_gmail_account(user_id, token_json, email_address=email_address)


def get_service(account_id, allow_interactive=True):
    """allow_interactive is unused now (kept so existing call sites don't
    need to change) - a web app can never open a browser on the user's
    behalf, so the only way to (re-)authorize is always the same: raise
    NoCredentials and let the caller redirect to reconnect that account.

    account_id is a gmail_accounts.id, already resolved from a user-scoped
    lookup by the caller (list_gmail_accounts/get_gmail_account) - this
    function itself does no ownership check, same as the old user_id-keyed
    version trusted its caller's user_id."""
    _require_credentials_file()

    account = db.get_gmail_account_row(account_id)
    if account is None:
        raise NoCredentials("This Gmail account was disconnected - reconnect it in Settings.")

    creds = Credentials.from_authorized_user_info(json.loads(account["token_json"]), SCOPES)
    if not creds.scopes or not set(SCOPES).issubset(set(creds.scopes)):
        creds = None  # saved token predates a scope we now need

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            db.update_gmail_account_token(account_id, creds.to_json())
        else:
            raise NoCredentials(
                "Gmail needs (re-)authorization for this account - reconnect it in Settings."
            )

    service = build("gmail", "v1", credentials=creds)

    # Legacy rows migrated from the old single-token table don't know their
    # own email address yet (that endpoint was never called for them) -
    # fill it in opportunistically the first time we successfully build a
    # working service for this account, so Settings can show a real
    # address instead of "Account #3" forever.
    if not account["email_address"]:
        try:
            db.update_gmail_account_token(
                account_id, creds.to_json(), email_address=get_own_email_address(service)
            )
        except Exception:
            pass

    return service


def get_own_email_address(service):
    return service.users().getProfile(userId="me").execute()["emailAddress"]


def send_email(service, to_address, subject, body_text):
    message = MIMEText(body_text)
    message["to"] = to_address
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _vendor_from_from_header(from_header):
    match = re.match(r"^\s*\"?([^\"<]+?)\"?\s*<", from_header)
    if match:
        return match.group(1).strip()
    return from_header.split("@")[-1].split(">")[0].strip() or from_header


def _domain_from_from_header(from_header):
    match = re.search(r"@([\w.\-]+)>?\s*$", from_header.strip())
    return match.group(1).lower() if match else None


def _normalize_vendor(vendor):
    return re.sub(r"[^a-z0-9]", "", (vendor or "").lower())


def _already_tracked(vendor, domain, known_vendors, known_domains):
    if domain and domain in known_domains:
        return True
    norm = _normalize_vendor(vendor)
    return bool(norm) and norm in known_vendors


def _decode_body(payload):
    """Best-effort plain-text extraction from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")

    for part in payload.get("parts", []) or []:
        text = _decode_body(part)
        if text:
            return text
    return ""


def _extract_amount(text):
    amounts = [float(a.replace(",", "")) for a in AMOUNT_RE.findall(text)]
    if not amounts:
        return None
    return max(amounts)


def _extract_due_date(text):
    match = DUE_HINT_RE.search(text)
    candidates = [match.group(2)] if match else []
    # fall back: try parsing any date-shaped token in the first 500 chars
    for candidate in candidates:
        try:
            return dateparser.parse(candidate, fuzzy=True).date().isoformat()
        except (ValueError, OverflowError):
            continue
    return None


def scan(user_id, query=None, max_results=50):
    """Fetch candidate bill emails and insert unseen, not-already-tracked ones
    into pending_bills. Any vendor you've already added manually or approved
    from a previous scan is skipped so it doesn't keep coming back for
    review, and any exact email you've explicitly rejected before is
    skipped too (even though rejecting deletes its pending_bills row,
    rejected_messages remembers it permanently) - otherwise it would just
    resurface on the very next scan for as long as it stays within the
    lookback window.

    Scans every Gmail account this user has connected, not just one - a
    single account with an expired/revoked token is skipped (reported back
    in `errors`) rather than failing the whole scan, so the other connected
    accounts still get scanned.

    Returns (inserted_count, skipped_count, errors) - errors is a list of
    account labels (email address, or "account #N" if not yet known) that
    couldn't be scanned and need reconnecting in Settings.
    """
    accounts = db.list_gmail_accounts(user_id)
    if not accounts:
        raise NoCredentials(
            "Gmail needs (re-)authorization for this - click the button once to grant it."
        )

    query = query or DEFAULT_QUERY

    known = db.list_known_bill_signatures(user_id)
    known_vendors = {_normalize_vendor(row["vendor"]) for row in known if row["vendor"]}
    known_domains = {row["source_domain"].lower() for row in known if row["source_domain"]}
    rejected_ids = db.list_rejected_message_ids(user_id)

    inserted, skipped = 0, 0
    errors = []
    for account in accounts:
        try:
            service = get_service(account["id"])
        except NoCredentials:
            errors.append(account["email_address"] or f"account #{account['id']}")
            continue

        results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        messages = results.get("messages", [])

        for msg_meta in messages:
            if msg_meta["id"] in rejected_ids:
                skipped += 1
                continue
            msg = service.users().messages().get(userId="me", id=msg_meta["id"], format="full").execute()
            headers = msg["payload"].get("headers", [])
            from_header = _header(headers, "From")
            date_header = _header(headers, "Date")
            subject = _header(headers, "Subject")

            body = _decode_body(msg["payload"]) or msg.get("snippet", "")
            text_for_parsing = f"{subject}\n{body}"

            vendor = _vendor_from_from_header(from_header) or subject
            domain = _domain_from_from_header(from_header)

            if _already_tracked(vendor, domain, known_vendors, known_domains):
                skipped += 1
                continue

            amount = _extract_amount(text_for_parsing)
            due_date = _extract_due_date(text_for_parsing)
            snippet = msg.get("snippet", "")[:300]

            added = db.add_pending_bill(
                user_id=user_id,
                gmail_message_id=msg["id"],
                vendor=vendor,
                amount=amount,
                due_date=due_date,
                snippet=snippet,
                email_date=date_header,
                sender_domain=domain,
            )
            if added:
                inserted += 1
            else:
                skipped += 1

    return inserted, skipped, errors
