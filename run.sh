#!/bin/bash
set -e
cd "$(dirname "$0")"
(sleep 1 && xdg-open http://127.0.0.1:5432 >/dev/null 2>&1 &)
export FLASK_DEBUG=1
exec venv/bin/python app.py
