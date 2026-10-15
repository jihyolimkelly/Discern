from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, LargeBinary, String, Text, create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

ASD_RELEASE_MODES = ("off", "review", "immediate")
DEFAULT_SETTINGS = {
    "asd_release": "review",
    "require_consent": True,
    "consent_text": (
        "I agree that this image and its analysis are stored in the research database "
        "and reviewed by an administrator."
    ),
    "ai_estimate": True,
    "auto_retrain": True,
    "retrain_every": 20,
}
CASE_STATUSES = ("pending", "approved", "rejected")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    original_ext: Mapped[str] = mapped_column(String(8))
    width: Mapped[int]
    height: Mapped[int]
    consent: Mapped[bool]

    disc_found: Mapped[bool]
    vcdr: Mapped[float | None]
    hcdr: Mapped[float | None]
    area_cdr: Mapped[float | None]
    disc_diameter_px: Mapped[float | None]

    model_probs: Mapped[dict | None] = mapped_column(JSON)
    model_predicted: Mapped[str | None] = mapped_column(String(16))
    model_version: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewer_label: Mapped[str | None] = mapped_column(String(16))
    reviewer_asd: Mapped[float | None]
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    seg_status: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")
    seg_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseFile(Base):

    __tablename__ = "case_files"

    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)


class AIEstimate(Base):

    __tablename__ = "ai_estimates"

    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model: Mapped[str] = mapped_column(String(64))
    probs: Mapped[dict] = mapped_column(JSON)
    predicted: Mapped[str] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(Text)
    image_quality: Mapped[str] = mapped_column(String(8))


class Model(Base):

    __tablename__ = "models"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    trigger: Mapped[str] = mapped_column(String(16))
    classes: Mapped[list] = mapped_column(JSON)
    label_count: Mapped[int]
    n_train: Mapped[int]
    n_val: Mapped[int]
    metrics: Mapped[dict] = mapped_column(JSON)
    params: Mapped[bytes] = mapped_column(LargeBinary)
    active: Mapped[bool] = mapped_column(default=False, index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)


def make_sessionmaker(database_url: str) -> sessionmaker[Session]:
    if database_url.startswith("sqlite"):
        engine = create_engine(database_url, connect_args={"check_same_thread": False})
    else:
        for prefix in ("postgres://", "postgresql://"):
            if database_url.startswith(prefix):
                database_url = "postgresql+psycopg://" + database_url[len(prefix):]
        engine = create_engine(database_url, pool_pre_ping=True, pool_size=2, max_overflow=3)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(engine) -> None:
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            default = f" DEFAULT '{col.server_default.arg}'" if col.server_default is not None else ""
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}{default}"))
            except DBAPIError:
                if col.name not in {c["name"] for c in inspect(engine).get_columns(table.name)}:
                    raise


def get_settings(db: Session) -> dict:
    stored = {s.key: s.value for s in db.scalars(select(Setting))}
    return {k: stored.get(k, default) for k, default in DEFAULT_SETTINGS.items()}


def save_settings(db: Session, values: dict) -> dict:
    for key, value in values.items():
        db.merge(Setting(key=key, value=value))
    db.commit()
    return get_settings(db)
