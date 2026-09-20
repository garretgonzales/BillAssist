FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py db.py gmail_scraper.py ./
COPY templates ./templates
COPY static ./static
CMD exec gunicorn -w 2 -b 0.0.0.0:$PORT app:app
