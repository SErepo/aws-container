"""
Flask CSV -> PostgreSQL uploader.

A small learning app that:
  * accepts a CSV upload (Customer ID = 8 digits, Hours Worked)
  * inserts new customers or UPDATES existing ones (cumulative hours, no duplicates)
  * shows a confirmation summary after processing
  * lets you view all customer data stored in PostgreSQL

The app is intentionally simple so it is easy to containerize with Docker and
deploy to AWS ECS/Fargate with an Amazon RDS PostgreSQL backend.
"""

import csv
import io
import os
import time

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from flask_sqlalchemy import SQLAlchemy


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def build_database_uri() -> str:
    """
    Build the SQLAlchemy connection string.

    We prefer a single DATABASE_URL (this is what most cloud providers / RDS
    setups inject). If it is not present we assemble one from individual
    POSTGRES_* variables so local docker-compose "just works".
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        # SQLAlchemy 1.4+/2.0 want the "postgresql://" scheme, some providers
        # still hand out "postgres://" — normalise it.
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        return database_url

    user = os.getenv("POSTGRES_USER", "appuser")
    password = os.getenv("POSTGRES_PASSWORD", "apppassword")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db_name = os.getenv("POSTGRES_DB", "customers")
    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = build_database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Recycle connections so long-lived containers don't hold stale DB handles.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload cap

db = SQLAlchemy(app)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class Customer(db.Model):
    """One row per unique 8-digit customer id."""

    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id = db.Column(db.String(8), unique=True, nullable=False, index=True)
    hours_worked = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(
        db.DateTime, server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "hours_worked": self.hours_worked,
            "updated_at": self.updated_at,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
ALLOWED_EXTENSIONS = {".csv"}


def _allowed_file(filename: str) -> bool:
    return os.path.splitext(filename.lower())[1] in ALLOWED_EXTENSIONS


def _normalise_customer_id(raw: str) -> str | None:
    """Return an 8-digit customer id string or None if invalid."""
    cid = (raw or "").strip()
    if cid.isdigit() and len(cid) == 8:
        return cid
    return None


def process_csv(stream) -> dict:
    """
    Parse an uploaded CSV stream and upsert rows.

    Returns a summary dict describing what happened. Supports headers named
    (case-insensitively) 'customer id'/'customer_id' and 'hours worked'/'hours_worked'.
    If no recognizable header is found the first two columns are used positionally.
    """
    text = stream.read()
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")  # handle Excel BOM

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]

    if not rows:
        return {"inserted": 0, "updated": 0, "skipped": 0, "errors": ["CSV is empty."]}

    # Detect header row.
    header = [c.strip().lower() for c in rows[0]]
    id_idx, hours_idx = 0, 1
    data_rows = rows
    if any(h in ("customer id", "customer_id", "customerid") for h in header):
        for i, h in enumerate(header):
            if h in ("customer id", "customer_id", "customerid"):
                id_idx = i
            if h in ("hours worked", "hours_worked", "hoursworked", "hours"):
                hours_idx = i
        data_rows = rows[1:]

    inserted = updated = skipped = 0
    errors: list[str] = []

    for line_no, row in enumerate(data_rows, start=2):
        if len(row) <= max(id_idx, hours_idx):
            skipped += 1
            errors.append(f"Line {line_no}: not enough columns.")
            continue

        cid = _normalise_customer_id(row[id_idx])
        if cid is None:
            skipped += 1
            errors.append(f"Line {line_no}: '{row[id_idx].strip()}' is not an 8-digit id.")
            continue

        try:
            hours = float(str(row[hours_idx]).strip())
        except ValueError:
            skipped += 1
            errors.append(f"Line {line_no}: hours '{row[hours_idx].strip()}' is not a number.")
            continue

        existing = Customer.query.filter_by(customer_id=cid).first()
        if existing:
            existing.hours_worked += hours  # cumulative, no duplicate rows
            updated += 1
        else:
            db.session.add(Customer(customer_id=cid, hours_worked=hours))
            inserted += 1

    db.session.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}


def init_db_with_retry(retries: int = 10, delay: int = 3) -> None:
    """
    Create tables, retrying while PostgreSQL warms up.

    In docker-compose / ECS the DB container or RDS instance may not accept
    connections the instant the web container starts, so we retry politely.
    """
    for attempt in range(1, retries + 1):
        try:
            with app.app_context():
                db.create_all()
            app.logger.info("Database ready (attempt %s).", attempt)
            return
        except OperationalError as exc:
            app.logger.warning(
                "Database not ready (attempt %s/%s): %s", attempt, retries, exc
            )
            time.sleep(delay)
    app.logger.error("Could not connect to the database after %s attempts.", retries)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files or request.files["file"].filename == "":
        flash("Please choose a CSV file to upload.", "error")
        return redirect(url_for("index"))

    file = request.files["file"]
    if not _allowed_file(file.filename):
        flash("Only .csv files are allowed.", "error")
        return redirect(url_for("index"))

    try:
        summary = process_csv(file.stream)
    except Exception as exc:  # pragma: no cover - defensive
        app.logger.exception("Failed to process CSV")
        flash(f"Failed to process file: {exc}", "error")
        return redirect(url_for("index"))

    return render_template("index.html", summary=summary)


@app.route("/data")
def view_data():
    customers = Customer.query.order_by(Customer.customer_id.asc()).all()
    total_hours = sum(c.hours_worked for c in customers)
    return render_template(
        "view_data.html", customers=customers, total_hours=total_hours
    )


@app.route("/health")
def health():
    """Lightweight endpoint used by the ALB target group health checks."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return {"status": "healthy"}, 200
    except Exception as exc:  # pragma: no cover
        return {"status": "unhealthy", "detail": str(exc)}, 503


# Initialise the schema as soon as the module is imported (works under gunicorn too).
init_db_with_retry()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
