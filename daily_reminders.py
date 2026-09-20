"""Run once a day (via a systemd timer, see deploy/) to send each user's
reminder digest, if they're due one and haven't already gotten one today.
Each user's own reminder_leads settings and Gmail token are used - see
_maybe_send_daily_reminder in app.py for the per-user logic itself.
"""
import db
from app import _maybe_send_daily_reminder, app

with app.app_context():
    for user_id in db.list_user_ids():
        _maybe_send_daily_reminder(user_id)
