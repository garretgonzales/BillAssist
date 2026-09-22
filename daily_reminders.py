import db
from app import _maybe_send_daily_reminder, app

with app.app_context():
    for user_id in db.list_user_ids():
        _maybe_send_daily_reminder(user_id)
