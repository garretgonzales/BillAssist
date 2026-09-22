import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent / "bills.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    amount REAL,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'unpaid',
    category TEXT,
    notes TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    source_domain TEXT,
    recurrence_unit TEXT,
    recurrence_interval INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT UNIQUE NOT NULL,
    vendor TEXT,
    amount REAL,
    due_date TEXT,
    snippet TEXT,
    email_date TEXT,
    sender_domain TEXT,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    email_address TEXT,
    token_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_messages (
    gmail_message_id TEXT PRIMARY KEY,
    rejected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    amount REAL NOT NULL,
    pay_date TEXT,
    recurrence_unit TEXT,
    recurrence_interval INTEGER,
    recurrence_parent_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT,
    lead_value INTEGER NOT NULL,
    lead_unit TEXT NOT NULL
);
"""

MIGRATIONS = [
    ("bills_source_domain", "ALTER TABLE bills ADD COLUMN source_domain TEXT"),
    ("bills_recurrence_unit", "ALTER TABLE bills ADD COLUMN recurrence_unit TEXT"),
    ("bills_recurrence_interval", "ALTER TABLE bills ADD COLUMN recurrence_interval INTEGER"),
    ("bills_recurrence_parent_id", "ALTER TABLE bills ADD COLUMN recurrence_parent_id INTEGER"),
    ("bills_total_amount", "ALTER TABLE bills ADD COLUMN total_amount REAL"),
    ("bills_reminder_lead_value", "ALTER TABLE bills ADD COLUMN reminder_lead_value INTEGER"),
    ("bills_reminder_lead_unit", "ALTER TABLE bills ADD COLUMN reminder_lead_unit TEXT"),
    ("bills_total_payments", "ALTER TABLE bills ADD COLUMN total_payments INTEGER"),
    ("pending_bills_sender_domain", "ALTER TABLE pending_bills ADD COLUMN sender_domain TEXT"),
    ("bills_until_cancelled", "ALTER TABLE bills ADD COLUMN until_cancelled INTEGER"),
    ("bills_user_id", "ALTER TABLE bills ADD COLUMN user_id INTEGER"),
    ("pending_bills_user_id", "ALTER TABLE pending_bills ADD COLUMN user_id INTEGER"),
    ("reminder_log_user_id", "ALTER TABLE reminder_log ADD COLUMN user_id INTEGER"),
    ("incomes_user_id", "ALTER TABLE incomes ADD COLUMN user_id INTEGER"),
    ("reminder_leads_user_id", "ALTER TABLE reminder_leads ADD COLUMN user_id INTEGER"),
    ("users_custom_greeting", "ALTER TABLE users ADD COLUMN custom_greeting TEXT"),
    ("users_name", "ALTER TABLE users ADD COLUMN name TEXT"),
    ("bills_icon", "ALTER TABLE bills ADD COLUMN icon TEXT"),
    ("users_theme_color", "ALTER TABLE users ADD COLUMN theme_color TEXT"),
    ("users_theme_mode", "ALTER TABLE users ADD COLUMN theme_mode TEXT"),
]

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

@contextmanager
def conn_scope():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
    for name, migration in MIGRATIONS:
        if name in applied:
            continue
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (name, now()),
        )
    if "user_scoping_backfill" not in applied:
        conn.execute(
            "CREATE TABLE categories_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER, name TEXT NOT NULL, UNIQUE(user_id, name))"
        )
        conn.execute("INSERT INTO categories_new (id, name) SELECT id, name FROM categories")
        conn.execute("DROP TABLE categories")
        conn.execute("ALTER TABLE categories_new RENAME TO categories")

        conn.execute(
            "CREATE TABLE rejected_messages_new ("
            "gmail_message_id TEXT NOT NULL, user_id INTEGER, rejected_at TEXT NOT NULL, "
            "PRIMARY KEY (user_id, gmail_message_id))"
        )
        conn.execute(
            "INSERT INTO rejected_messages_new (gmail_message_id, rejected_at) "
            "SELECT gmail_message_id, rejected_at FROM rejected_messages"
        )
        conn.execute("DROP TABLE rejected_messages")
        conn.execute("ALTER TABLE rejected_messages_new RENAME TO rejected_messages")

        owner = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if owner is not None:
            for table in (
                "bills", "pending_bills", "reminder_log",
                "incomes", "reminder_leads", "categories", "rejected_messages",
            ):
                conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (owner["id"],))

        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            ("user_scoping_backfill", now()),
        )
    conn.execute(
        "INSERT OR IGNORE INTO categories (user_id, name) "
        "SELECT DISTINCT user_id, category FROM bills WHERE category IS NOT NULL AND TRIM(category) != ''"
    )
    if "reminder_leads_seed" not in applied:
        conn.execute(
            "INSERT INTO reminder_leads (vendor, lead_value, lead_unit) VALUES (NULL, 3, 'day')"
        )
        conn.execute(
            "INSERT INTO reminder_leads (vendor, lead_value, lead_unit) "
            "SELECT DISTINCT vendor, reminder_lead_value, reminder_lead_unit FROM bills "
            "WHERE reminder_lead_value IS NOT NULL AND reminder_lead_unit IS NOT NULL"
        )
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            ("reminder_leads_seed", now()),
        )
    if "gmail_accounts_backfill" not in applied:
        old_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gmail_tokens'"
        ).fetchone()
        if old_table is not None:
            conn.execute(
                "INSERT INTO gmail_accounts (user_id, email_address, token_json, created_at, updated_at) "
                "SELECT user_id, NULL, token_json, updated_at, updated_at FROM gmail_tokens"
            )
            conn.execute("DROP TABLE gmail_tokens")
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            ("gmail_accounts_backfill", now()),
        )
    conn.commit()
    conn.close()

def now():
    return datetime.now(timezone.utc).isoformat()

def create_user(email, password_hash, name=None):
    with conn_scope() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, name, created_at) VALUES (?, ?, ?, ?)",
            (email, password_hash, name, now()),
        )
        return cur.lastrowid

def get_user_by_email(email):
    with conn_scope() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

def get_user_by_id(user_id):
    with conn_scope() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

def set_custom_greeting(user_id, text):
    with conn_scope() as conn:
        conn.execute("UPDATE users SET custom_greeting = ? WHERE id = ?", (text, user_id))

def set_theme_color(user_id, hex_color):
    with conn_scope() as conn:
        conn.execute("UPDATE users SET theme_color = ? WHERE id = ?", (hex_color, user_id))

def set_theme_mode(user_id, mode):
    with conn_scope() as conn:
        conn.execute("UPDATE users SET theme_mode = ? WHERE id = ?", (mode, user_id))

def list_bills(user_id, status=None, search=None, due_before=None, due_range=None):
    with conn_scope() as conn:
        clauses, params = ["user_id = ?"], [user_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append("(vendor LIKE ? OR category LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        if due_before is not None:
            clauses.append("(due_date IS NULL OR due_date < ?)")
            params.append(due_before)
        elif due_range is not None:
            start, end = due_range
            clauses.append("due_date >= ? AND due_date < ?")
            params.extend([start, end])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return conn.execute(
            f"SELECT * FROM bills {where} ORDER BY status ASC, due_date IS NULL, due_date ASC", params
        ).fetchall()

def get_last_reminder_sent(user_id):
    with conn_scope() as conn:
        row = conn.execute(
            "SELECT MAX(sent_at) AS sent_at FROM reminder_log WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["sent_at"] if row else None

def log_reminder_sent(user_id):
    with conn_scope() as conn:
        conn.execute("INSERT INTO reminder_log (sent_at, user_id) VALUES (?, ?)", (now(), user_id))

def add_bill(
    user_id,
    vendor,
    amount,
    due_date,
    category=None,
    notes=None,
    source="manual",
    source_domain=None,
    recurrence_unit=None,
    recurrence_interval=None,
    recurrence_parent_id=None,
    total_amount=None,
    reminder_lead_value=None,
    reminder_lead_unit=None,
    total_payments=None,
    until_cancelled=None,
    icon=None,
):
    with conn_scope() as conn:
        conn.execute(
            "INSERT INTO bills (user_id, vendor, amount, due_date, status, category, notes, source, "
            "source_domain, recurrence_unit, recurrence_interval, recurrence_parent_id, total_amount, "
            "reminder_lead_value, reminder_lead_unit, total_payments, until_cancelled, icon, created_at) "
            "VALUES (?, ?, ?, ?, 'unpaid', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, vendor, amount, due_date, category, notes, source,
                source_domain, recurrence_unit, recurrence_interval, recurrence_parent_id, total_amount,
                reminder_lead_value, reminder_lead_unit, total_payments, until_cancelled, icon, now(),
            ),
        )

def list_distinct_vendors(user_id):
    with conn_scope() as conn:
        rows = conn.execute(
            "SELECT DISTINCT vendor FROM bills WHERE user_id = ? ORDER BY vendor COLLATE NOCASE", (user_id,)
        ).fetchall()
        return [r["vendor"] for r in rows]

def list_reminder_leads(user_id, vendor):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT id, lead_value, lead_unit FROM reminder_leads WHERE user_id = ? AND vendor = ? "
            "ORDER BY lead_value",
            (user_id, vendor),
        ).fetchall()

def list_general_reminder_leads(user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT id, lead_value, lead_unit FROM reminder_leads "
            "WHERE user_id = ? AND vendor IS NULL ORDER BY lead_value",
            (user_id,),
        ).fetchall()

def add_reminder_lead(user_id, vendor, lead_value, lead_unit):
    with conn_scope() as conn:
        conn.execute(
            "INSERT INTO reminder_leads (user_id, vendor, lead_value, lead_unit) VALUES (?, ?, ?, ?)",
            (user_id, vendor, lead_value, lead_unit),
        )

def delete_reminder_lead(lead_id, user_id):
    with conn_scope() as conn:
        conn.execute("DELETE FROM reminder_leads WHERE id = ? AND user_id = ?", (lead_id, user_id))

def list_user_ids():
    with conn_scope() as conn:
        rows = conn.execute("SELECT id FROM users").fetchall()
        return [row["id"] for row in rows]

def list_gmail_accounts(user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT id, email_address, updated_at FROM gmail_accounts WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()

def get_gmail_account(account_id, user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM gmail_accounts WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        ).fetchone()

def get_gmail_account_row(account_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM gmail_accounts WHERE id = ?", (account_id,)
        ).fetchone()

def get_gmail_account_by_email(user_id, email_address):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM gmail_accounts WHERE user_id = ? AND email_address = ?",
            (user_id, email_address),
        ).fetchone()

def add_gmail_account(user_id, token_json, email_address=None):
    with conn_scope() as conn:
        cur = conn.execute(
            "INSERT INTO gmail_accounts (user_id, email_address, token_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, email_address, token_json, now(), now()),
        )
        return cur.lastrowid

def update_gmail_account_token(account_id, token_json, email_address=None):
    with conn_scope() as conn:
        if email_address is not None:
            conn.execute(
                "UPDATE gmail_accounts SET token_json = ?, email_address = ?, updated_at = ? WHERE id = ?",
                (token_json, email_address, now(), account_id),
            )
        else:
            conn.execute(
                "UPDATE gmail_accounts SET token_json = ?, updated_at = ? WHERE id = ?",
                (token_json, now(), account_id),
            )

def delete_gmail_account(account_id, user_id):
    with conn_scope() as conn:
        conn.execute(
            "DELETE FROM gmail_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        )

def list_recurring_without_child(user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM bills b "
            "WHERE user_id = ? AND recurrence_unit IS NOT NULL AND recurrence_interval IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM bills c WHERE c.recurrence_parent_id = b.id)",
            (user_id,),
        ).fetchall()

def get_bill(bill_id, user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM bills WHERE id = ? AND user_id = ?", (bill_id, user_id)
        ).fetchone()

def _recurring_chain_ids(conn, bill_id, user_id):
    row = conn.execute(
        "SELECT * FROM bills WHERE id = ? AND user_id = ?", (bill_id, user_id)
    ).fetchone()
    if not row:
        return []
    node = row
    while node["recurrence_parent_id"]:
        parent = conn.execute(
            "SELECT * FROM bills WHERE id = ? AND user_id = ?", (node["recurrence_parent_id"], user_id)
        ).fetchone()
        if not parent:
            break
        node = parent
    ids = []
    current_id = node["id"]
    while current_id is not None:
        ids.append(current_id)
        child = conn.execute(
            "SELECT id FROM bills WHERE recurrence_parent_id = ? AND user_id = ?", (current_id, user_id)
        ).fetchone()
        current_id = child["id"] if child else None
    return ids

def update_bill(
    bill_id, user_id, vendor, amount, due_date, category, notes,
    recurrence_unit, recurrence_interval, total_amount, total_payments,
    until_cancelled=None, icon=None,
):
    with conn_scope() as conn:
        conn.execute(
            "UPDATE bills SET vendor = ?, amount = ?, due_date = ?, category = ?, notes = ?, "
            "recurrence_unit = ?, recurrence_interval = ?, total_amount = ?, total_payments = ?, "
            "until_cancelled = ?, icon = ? WHERE id = ? AND user_id = ?",
            (
                vendor, amount, due_date, category, notes,
                recurrence_unit, recurrence_interval, total_amount, total_payments, until_cancelled, icon,
                bill_id, user_id,
            ),
        )
        if recurrence_unit:
            other_ids = [i for i in _recurring_chain_ids(conn, bill_id, user_id) if i != bill_id]
            for other_id in other_ids:
                conn.execute(
                    "UPDATE bills SET vendor = ?, amount = ?, category = ?, notes = ?, "
                    "recurrence_unit = ?, recurrence_interval = ?, total_amount = ?, total_payments = ?, "
                    "until_cancelled = ?, icon = ? WHERE id = ? AND user_id = ?",
                    (
                        vendor, amount, category, notes,
                        recurrence_unit, recurrence_interval, total_amount, total_payments, until_cancelled, icon,
                        other_id, user_id,
                    ),
                )

def set_bill_status(bill_id, user_id, status):
    with conn_scope() as conn:
        conn.execute(
            "UPDATE bills SET status = ? WHERE id = ? AND user_id = ?", (status, bill_id, user_id)
        )

def delete_bill_chain(bill_id, user_id):
    with conn_scope() as conn:
        for current_id in _recurring_chain_ids(conn, bill_id, user_id):
            conn.execute("DELETE FROM bills WHERE id = ? AND user_id = ?", (current_id, user_id))

def add_category(user_id, name):
    name = (name or "").strip()
    if not name:
        return
    with conn_scope() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )

def list_categories(user_id):
    with conn_scope() as conn:
        rows = conn.execute(
            "SELECT name FROM categories WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,)
        ).fetchall()
        return [row["name"] for row in rows]

def count_overdue_bills(user_id, today):
    with conn_scope() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM bills WHERE user_id = ? AND status = 'unpaid' "
            "AND due_date IS NOT NULL AND due_date < ?",
            (user_id, today),
        ).fetchone()
        return row["n"]

def get_summary(user_id, next_month_start, month_after_next_start):
    with conn_scope() as conn:
        params = {"user_id": user_id, "next_start": next_month_start, "after_next": month_after_next_start}
        row = conn.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN status = 'unpaid' AND (due_date IS NULL OR due_date < :next_start) "
            "    THEN amount ELSE 0 END), 0) AS unpaid_total, "
            "  SUM(CASE WHEN status = 'unpaid' AND (due_date IS NULL OR due_date < :next_start) "
            "    THEN 1 ELSE 0 END) AS unpaid_count, "
            "  COALESCE(SUM(CASE WHEN status = 'paid' AND (due_date IS NULL OR due_date < :next_start) "
            "    THEN amount ELSE 0 END), 0) AS paid_total, "
            "  SUM(CASE WHEN status = 'paid' AND (due_date IS NULL OR due_date < :next_start) "
            "    THEN 1 ELSE 0 END) AS paid_count, "
            "  SUM(CASE WHEN (due_date IS NULL OR due_date < :next_start) THEN 1 ELSE 0 END) AS total_count, "
            "  COALESCE(SUM(CASE WHEN status = 'unpaid' AND due_date >= :next_start AND due_date < :after_next "
            "    THEN amount ELSE 0 END), 0) AS next_month_total, "
            "  SUM(CASE WHEN status = 'unpaid' AND due_date >= :next_start AND due_date < :after_next "
            "    THEN 1 ELSE 0 END) AS next_month_count, "
            "  COALESCE(SUM(CASE WHEN (due_date IS NULL OR due_date < :next_start) "
            "    THEN amount ELSE 0 END), 0) AS this_month_all_total, "
            "  COALESCE(SUM(CASE WHEN due_date >= :next_start AND due_date < :after_next "
            "    THEN amount ELSE 0 END), 0) AS next_month_all_total "
            "FROM bills WHERE user_id = :user_id",
            params,
        ).fetchone()

        by_category = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS cat, "
            "COALESCE(SUM(amount), 0) AS total "
            "FROM bills WHERE user_id = :user_id "
            "AND (due_date IS NULL OR due_date < :next_start) "
            "GROUP BY cat ORDER BY total DESC",
            params,
        ).fetchall()

        by_category_paid = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS cat, "
            "COALESCE(SUM(amount), 0) AS total "
            "FROM bills WHERE user_id = :user_id "
            "AND status = 'paid' AND (due_date IS NULL OR due_date < :next_start) "
            "GROUP BY cat ORDER BY total DESC",
            params,
        ).fetchall()

    summary = dict(row)
    summary["unpaid_count"] = summary["unpaid_count"] or 0
    summary["paid_count"] = summary["paid_count"] or 0
    summary["total_count"] = summary["total_count"] or 0
    summary["next_month_count"] = summary["next_month_count"] or 0
    return summary, by_category, by_category_paid

def get_all_bills_totals(user_id, next_month_start):
    with conn_scope() as conn:
        rows = conn.execute(
            "SELECT * FROM bills WHERE user_id = ? AND (due_date IS NULL OR due_date < ?) AND ("
            "  (recurrence_unit IS NOT NULL AND id IN ("
            "    SELECT MIN(id) FROM bills WHERE user_id = ? AND recurrence_unit IS NOT NULL "
            "    GROUP BY vendor, amount, category, recurrence_unit, recurrence_interval, COALESCE(until_cancelled, 0)"
            "  )) "
            "  OR (recurrence_unit IS NULL AND total_amount IS NOT NULL)"
            ") ORDER BY vendor COLLATE NOCASE",
            (user_id, next_month_start, user_id),
        ).fetchall()

    recurring_payment_total = sum(r["amount"] or 0 for r in rows if r["recurrence_unit"])
    tracked_total_amount = sum(
        r["total_amount"] for r in rows
        if r["total_amount"] is not None and not r["until_cancelled"]
    )
    return rows, recurring_payment_total, tracked_total_amount

def list_recurring_bill_estimates(user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM bills WHERE user_id = ? AND recurrence_unit IS NOT NULL AND id IN ("
            "  SELECT MIN(id) FROM bills WHERE user_id = ? AND recurrence_unit IS NOT NULL "
            "  GROUP BY vendor, amount, category, recurrence_unit, recurrence_interval, COALESCE(until_cancelled, 0)"
            ") ORDER BY vendor COLLATE NOCASE",
            (user_id, user_id),
        ).fetchall()

def list_known_bill_signatures(user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT DISTINCT vendor, source_domain FROM bills WHERE user_id = ?", (user_id,)
        ).fetchall()

def add_pending_bill(user_id, gmail_message_id, vendor, amount, due_date, snippet, email_date, sender_domain=None):
    try:
        with conn_scope() as conn:
            conn.execute(
                "INSERT INTO pending_bills (user_id, gmail_message_id, vendor, amount, due_date, snippet, email_date, sender_domain, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, gmail_message_id, vendor, amount, due_date, snippet, email_date, sender_domain, now()),
            )
        return True
    except sqlite3.IntegrityError:
        return False

def list_pending_bills(user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM pending_bills WHERE user_id = ? ORDER BY detected_at DESC", (user_id,)
        ).fetchall()

def get_pending_bill(pending_id, user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM pending_bills WHERE id = ? AND user_id = ?", (pending_id, user_id)
        ).fetchone()

def delete_pending_bill(pending_id, user_id):
    with conn_scope() as conn:
        conn.execute("DELETE FROM pending_bills WHERE id = ? AND user_id = ?", (pending_id, user_id))

def mark_message_rejected(user_id, gmail_message_id):
    with conn_scope() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rejected_messages (gmail_message_id, user_id, rejected_at) VALUES (?, ?, ?)",
            (gmail_message_id, user_id, now()),
        )

def list_rejected_message_ids(user_id):
    with conn_scope() as conn:
        rows = conn.execute(
            "SELECT gmail_message_id FROM rejected_messages WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {row["gmail_message_id"] for row in rows}

def add_income(user_id, label, amount, pay_date, recurrence_unit=None, recurrence_interval=None):
    with conn_scope() as conn:
        conn.execute(
            "INSERT INTO incomes (user_id, label, amount, pay_date, recurrence_unit, recurrence_interval, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, label, amount, pay_date, recurrence_unit, recurrence_interval, now()),
        )

def list_incomes(user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM incomes WHERE user_id = ? ORDER BY pay_date IS NULL, pay_date ASC", (user_id,)
        ).fetchall()

def get_income(income_id, user_id):
    with conn_scope() as conn:
        return conn.execute(
            "SELECT * FROM incomes WHERE id = ? AND user_id = ?", (income_id, user_id)
        ).fetchone()

def update_income(income_id, user_id, label, amount, pay_date, recurrence_unit, recurrence_interval):
    with conn_scope() as conn:
        conn.execute(
            "UPDATE incomes SET label = ?, amount = ?, pay_date = ?, recurrence_unit = ?, "
            "recurrence_interval = ? WHERE id = ? AND user_id = ?",
            (label, amount, pay_date, recurrence_unit, recurrence_interval, income_id, user_id),
        )

def delete_income(income_id, user_id):
    with conn_scope() as conn:
        conn.execute("DELETE FROM incomes WHERE id = ? AND user_id = ?", (income_id, user_id))
