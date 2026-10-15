import hmac
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eyeasd import ai_estimate

from eyeasd.config import ON_VERCEL, ROOT, load_config
from eyeasd.db import (
    ASD_RELEASE_MODES,
    CASE_STATUSES,
    AIEstimate,
    Case,
    CaseFile,
    Model,
    get_settings,
    make_sessionmaker,
    save_settings,
    utcnow,
)
from eyeasd.pipeline import Engine, model_version
from eyeasd.segmentation import SEG_MODEL_ID, mask_ellipse
from eyeasd.severity import MIN_PER_CLASS, SEVERITY_CLASSES, training_readiness

MAX_UPLOAD_BYTES = int(4.3 * 1024 * 1024) if ON_VERCEL else 25 * 1024 * 1024
LOW_RES_PX = 800
SESSION_COOKIE = "eyeasd_admin"
SESSION_MAX_AGE = 12 * 3600
VIEW_FILES = ("display", "overlay", "roi", "roi_overlay", "disc_mask", "cup_mask")

config = load_config()
SessionLocal = make_sessionmaker(config["database_url"])
signer = URLSafeTimedSerializer(config["secret_key"], salt="admin-session")
engine = Engine()

app = FastAPI(title="Discern")


def get_db():
    with SessionLocal() as db:
        yield db


def require_admin(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    try:
        if token and signer.loads(token, max_age=SESSION_MAX_AGE) == "admin":
            return
    except BadSignature:
        pass
    raise HTTPException(401, "Sign in required.")


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def model_block(case: Case) -> dict:
    if case.model_probs is None:
        return {"trained": False}
    return {
        "trained": True,
        "version": case.model_version,
        "probs": case.model_probs,
        "predicted": case.model_predicted,
        "asd_probability": 1 - case.model_probs["normal"] if "normal" in case.model_probs else None,
    }


def case_common(case: Case) -> dict:
    files = {k: f"/api/cases/{case.public_id}/files/{k}" for k in VIEW_FILES if k == "display" or case.disc_found}
    return {
        "case_id": case.public_id,
        "created_at": iso(case.created_at),
        "filename": case.filename,
        "found": case.disc_found,
        "image": {"width": case.width, "height": case.height, "low_resolution": max(case.width, case.height) < LOW_RES_PX},
        "images": files,
        "metrics": {
            "vcdr": case.vcdr,
            "hcdr": case.hcdr,
            "area_cdr": case.area_cdr,
            "disc_diameter_px": case.disc_diameter_px,
        }
        if case.disc_found
        else None,
    }


_UNSET = object()


def ai_block(db: Session, case: Case, settings: dict, est=_UNSET) -> dict | None:
    if not case.disc_found or case.model_probs is not None or not settings["ai_estimate"]:
        return None
    if est is _UNSET:
        est = db.get(AIEstimate, case.id)
    if est is None:
        return {"pending": True} if ai_estimate.available() else None
    probs = {c: est.probs[c] for c in SEVERITY_CLASSES if c in est.probs}
    return {
        "source": "provisional",
        "informative": max(probs.values()) - min(probs.values()) >= 0.10,
        "model": est.model,
        "probs": probs,
        "predicted": est.predicted,
        "asd_probability": 1 - probs.get("normal", 0.0),
        "rationale": est.rationale,
        "image_quality": est.image_quality,
        "created_at": iso(est.created_at),
    }


def public_case(case: Case, settings: dict, db: Session) -> dict:
    release = settings["asd_release"]
    asd: dict = {"release": release, "status": case.status}
    if release == "off":
        asd = {"release": "off"}
    elif case.status == "rejected":
        asd["note"] = case.reviewer_note
    else:
        if case.status == "approved":
            asd.update(label=case.reviewer_label, likelihood=case.reviewer_asd, note=case.reviewer_note, reviewed_at=iso(case.reviewed_at))
        if release == "immediate" and case.disc_found:
            asd["model"] = model_block(case)
            asd["ai"] = ai_block(db, case, settings)
    return {**case_common(case), "asd": asd}


def admin_case(case: Case) -> dict:
    return {
        **case_common(case),
        "consent": case.consent,
        "sha256": case.sha256,
        "status": case.status,
        "model": model_block(case) if case.disc_found else None,
        "review": {"label": case.reviewer_label, "asd": case.reviewer_asd, "note": case.reviewer_note, "reviewed_at": iso(case.reviewed_at)},
        "seg": {"status": case.seg_status, "reviewed_at": iso(case.seg_reviewed_at)},
    }


def estimated_asd(db: Session, case: Case) -> float | None:
    if case.model_probs and "normal" in case.model_probs:
        return 1 - case.model_probs["normal"]
    block = ai_block(db, case, get_settings(db))
    return block["asd_probability"] if block and block.get("probs") and block["informative"] else None


def get_or_create_estimate(db: Session, case: Case) -> dict:
    settings = get_settings(db)
    if not case.disc_found:
        raise HTTPException(400, "No optic disc was detected in this image.")
    if case.model_probs is not None:
        raise HTTPException(400, "A trained model already scored this case.")
    if not settings["ai_estimate"]:
        raise HTTPException(400, "The provisional estimate is turned off.")
    if not ai_estimate.available():
        raise HTTPException(503, "The provisional estimate is not configured.")
    if db.get(AIEstimate, case.id) is None:
        roi, roi_overlay = db.get(CaseFile, (case.id, "roi")), db.get(CaseFile, (case.id, "roi_overlay"))
        metrics = {"vcdr": case.vcdr, "hcdr": case.hcdr, "area_cdr": case.area_cdr, "disc_diameter_px": case.disc_diameter_px}
        try:
            result = ai_estimate.estimate(roi.data, roi_overlay.data, metrics)
        except ai_estimate.EstimateError as exc:
            raise HTTPException(502, str(exc))
        db.add(AIEstimate(case_id=case.id, **result))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return ai_block(db, case, settings)


def find_case(db: Session, public_id: str) -> Case:
    case = db.scalar(select(Case).where(Case.public_id == public_id))
    if case is None:
        raise HTTPException(404, "Case not found.")
    return case


@app.get("/api/config")
def public_config(db: Session = Depends(get_db)):
    s = get_settings(db)
    return {
        "asd_release": s["asd_release"],
        "require_consent": s["require_consent"],
        "consent_text": s["consent_text"],
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


@app.post("/api/analyze")
def analyze(file: UploadFile, consent: bool = Form(False), db: Session = Depends(get_db)):
    settings = get_settings(db)
    if settings["require_consent"] and not consent:
        raise HTTPException(400, "Consent is required before an image can be analyzed.")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Image is too large (max {MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MB).")
    started = time.perf_counter()
    try:
        case = engine.create_case(db, data, file.filename, consent)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    result = public_case(case, settings, db)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    result["timing_ms"] = engine.last_timing
    if not case.disc_found:
        result["message"] = "No optic disc found. Make sure this is a fundus photo with the optic disc visible."
    return result


@app.get("/api/cases/{public_id}")
def get_public_case(public_id: str, db: Session = Depends(get_db)):
    return public_case(find_case(db, public_id), get_settings(db), db)


@app.post("/api/cases/{public_id}/estimate")
def public_estimate(public_id: str, db: Session = Depends(get_db)):
    if get_settings(db)["asd_release"] != "immediate":
        raise HTTPException(403, "ASD results are released by an administrator.")
    return get_or_create_estimate(db, find_case(db, public_id))


@app.get("/api/cases/{public_id}/files/{kind}")
def case_file(public_id: str, kind: str, db: Session = Depends(get_db)):
    case = find_case(db, public_id)
    f = db.get(CaseFile, (case.id, kind)) if kind in VIEW_FILES or kind == "original" else None
    if f is None:
        raise HTTPException(404, "File not found.")
    headers = {"Cache-Control": "private, max-age=86400"}
    if kind == "original":
        headers["Content-Disposition"] = f'attachment; filename="{case.public_id}.{case.original_ext}"'
    return Response(f.data, media_type=f.content_type, headers=headers)


class LoginBody(BaseModel):
    password: str


@app.post("/api/admin/login")
def admin_login(body: LoginBody, response: Response):
    expected = config["admin_password"]
    if not expected:
        raise HTTPException(503, "Admin sign-in is not configured. Set ADMIN_PASSWORD on the server.")
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        time.sleep(0.5)
        raise HTTPException(401, "Incorrect password.")
    response.set_cookie(
        SESSION_COOKIE, signer.dumps("admin"), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict", secure=ON_VERCEL
    )
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/admin/summary", dependencies=[Depends(require_admin)])
def admin_summary(db: Session = Depends(get_db)):
    by_status = dict(db.execute(select(Case.status, func.count()).group_by(Case.status)).all())
    labels = dict(
        db.execute(
            select(Case.reviewer_label, func.count()).where(Case.status == "approved").group_by(Case.reviewer_label)
        ).all()
    )
    return {
        "counts": {"total": sum(by_status.values()), **{s: by_status.get(s, 0) for s in CASE_STATUSES}},
        "labels": {c: labels.get(c, 0) for c in SEVERITY_CLASSES},
        "model": active_model_info(db),
        "segmentation_model": SEG_MODEL_ID,
        "database": SessionLocal.kw["bind"].dialect.name,
        "database_ephemeral": config["database_ephemeral"],
        "ai": {"available": ai_estimate.available(), "model": ai_estimate.MODEL},
    }


@app.get("/api/admin/cases", dependencies=[Depends(require_admin)])
def admin_cases(status: str = "all", limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    query = select(Case)
    if status in CASE_STATUSES:
        query = query.where(Case.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Case.created_at.desc()).limit(min(limit, 200)).offset(offset)).all()
    settings = get_settings(db)
    estimates = {e.case_id: e for e in db.scalars(select(AIEstimate).where(AIEstimate.case_id.in_([c.id for c in rows])))}
    return {"total": total, "cases": [{**admin_case(c), "ai": ai_block(db, c, settings, estimates.get(c.id))} for c in rows]}


@app.get("/api/admin/cases/{public_id}", dependencies=[Depends(require_admin)])
def admin_get_case(public_id: str, db: Session = Depends(get_db)):
    case = find_case(db, public_id)
    has_original = db.scalar(select(CaseFile.kind).where(CaseFile.case_id == case.id, CaseFile.kind == "original"))
    return {
        **admin_case(case),
        "original": f"/api/cases/{case.public_id}/files/original" if has_original else None,
        "ai": ai_block(db, case, get_settings(db)),
        "seg_editor": seg_editor(db, case),
    }


def seg_editor(db: Session, case: Case) -> dict:
    w, h = engine.display_rgb(db, case).shape[1::-1]
    disc, cup = engine.current_masks(db, case)
    disc_e = mask_ellipse(disc) if disc is not None else None
    cup_e = mask_ellipse(cup) if cup is not None else None
    if disc_e is None:
        disc_e = {"cx": w / 2, "cy": h / 2, "rx": w * 0.055, "ry": w * 0.06}
    if cup_e is None:
        cup_e = {"cx": disc_e["cx"], "cy": disc_e["cy"], "rx": disc_e["rx"] * 0.45, "ry": disc_e["ry"] * 0.45}
    has_auto = db.scalar(select(CaseFile.kind).where(CaseFile.case_id == case.id, CaseFile.kind == "disc_auto")) is not None
    return {"width": w, "height": h, "disc": disc_e, "cup": cup_e, "has_auto": has_auto}


class Ellipse(BaseModel):
    cx: float
    cy: float
    rx: float = Field(gt=1)
    ry: float = Field(gt=1)


class SegBody(BaseModel):
    action: Literal["confirm", "correct", "revert"]
    disc: Ellipse | None = None
    cup: Ellipse | None = None


@app.post("/api/admin/cases/{public_id}/segmentation", dependencies=[Depends(require_admin)])
def admin_segmentation(public_id: str, body: SegBody, db: Session = Depends(get_db)):
    case = find_case(db, public_id)
    if body.action == "correct" and (body.disc is None or body.cup is None):
        raise HTTPException(400, "Send both the optic disc and the optic cup.")
    try:
        engine.review_segmentation(
            db, case, body.action, body.disc.model_dump() if body.disc else None, body.cup.model_dump() if body.cup else None
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return admin_get_case(public_id, db)


@app.post("/api/admin/cases/{public_id}/estimate", dependencies=[Depends(require_admin)])
def admin_estimate(public_id: str, db: Session = Depends(get_db)):
    return get_or_create_estimate(db, find_case(db, public_id))


class ReviewBody(BaseModel):
    decision: Literal["approve", "reject", "reopen"]
    label: Literal[tuple(SEVERITY_CLASSES)] | None = None
    asd_probability: float | None = Field(default=None, ge=0, le=1)
    note: str | None = Field(default=None, max_length=2000)


@app.post("/api/admin/cases/{public_id}/review", dependencies=[Depends(require_admin)])
def admin_review(public_id: str, body: ReviewBody, db: Session = Depends(get_db)):
    case = find_case(db, public_id)
    if body.decision == "approve":
        if body.label is None:
            raise HTTPException(400, "Choose a severity label before approving.")
        if not case.disc_found:
            raise HTTPException(400, "Cases without a detected optic disc can only be rejected.")
        case.status, case.reviewer_label = "approved", body.label
        case.reviewer_asd = body.asd_probability if body.asd_probability is not None else estimated_asd(db, case)
    elif body.decision == "reject":
        case.status, case.reviewer_label, case.reviewer_asd = "rejected", None, None
    else:
        case.status, case.reviewer_label, case.reviewer_asd = "pending", None, None
    case.reviewer_note = (body.note or "").strip() or None
    case.reviewed_at = utcnow() if body.decision != "reopen" else None
    db.commit()
    retrained = engine.maybe_auto_retrain(db, get_settings(db)) if body.decision == "approve" else None
    return {**admin_case(case), "retrained": model_info(retrained) if retrained else None}


@app.delete("/api/admin/cases/{public_id}", dependencies=[Depends(require_admin)])
def admin_delete_case(public_id: str, db: Session = Depends(get_db)):
    case = find_case(db, public_id)
    db.execute(delete(CaseFile).where(CaseFile.case_id == case.id))
    db.execute(delete(AIEstimate).where(AIEstimate.case_id == case.id))
    db.delete(case)
    db.commit()
    return {"ok": True}


class SettingsBody(BaseModel):

    asd_release: Literal[ASD_RELEASE_MODES] | None = None
    require_consent: bool | None = None
    consent_text: str | None = Field(default=None, min_length=1, max_length=1000)
    ai_estimate: bool | None = None
    auto_retrain: bool | None = None
    retrain_every: int | None = Field(default=None, ge=1, le=10000)


@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_get_settings(db: Session = Depends(get_db)):
    return get_settings(db)


@app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_put_settings(body: SettingsBody, db: Session = Depends(get_db)):
    return save_settings(db, body.model_dump(exclude_none=True))


def model_info(m: Model) -> dict:
    return {
        "id": m.id,
        "version": model_version(m.id),
        "created_at": iso(m.created_at),
        "trigger": m.trigger,
        "classes": m.classes,
        "label_count": m.label_count,
        "n_train": m.n_train,
        "n_val": m.n_val,
        "metrics": m.metrics,
        "active": m.active,
    }


def active_model_info(db: Session) -> dict | None:
    m = db.scalar(select(Model).where(Model.active))
    return model_info(m) if m else None


@app.get("/api/admin/models", dependencies=[Depends(require_admin)])
def admin_models(db: Session = Depends(get_db)):
    counts = engine.label_counts(db)
    settings = get_settings(db)
    active = active_model_info(db)
    return {
        "active": active,
        "history": [model_info(m) for m in db.scalars(select(Model).order_by(Model.id.desc()).limit(50))],
        "label_counts": {c: counts.get(c, 0) for c in SEVERITY_CLASSES},
        "new_labels": engine.new_labels(db, counts),
        "not_ready_reason": training_readiness(counts),
        "auto_retrain": settings["auto_retrain"],
        "retrain_every": settings["retrain_every"],
        "min_per_class": MIN_PER_CLASS,
    }


@app.post("/api/admin/models/train", dependencies=[Depends(require_admin)])
def admin_train(db: Session = Depends(get_db)):
    try:
        return model_info(engine.train(db, trigger="manual"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/admin/models/{model_id}/activate", dependencies=[Depends(require_admin)])
def admin_activate(model_id: int, db: Session = Depends(get_db)):
    if db.get(Model, model_id) is None:
        raise HTTPException(404, "Model not found.")
    engine.activate(db, model_id)
    return active_model_info(db)


PAGE_CACHE = {"Cache-Control": "public, max-age=0, s-maxage=600, stale-while-revalidate=86400"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(ROOT / "static" / "favicon.ico", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
def landing_page():
    return FileResponse(ROOT / "static" / "landing.html", headers=PAGE_CACHE)


@app.get("/analyze")
@app.get("/case/{public_id}")
def index_page(public_id: str | None = None):
    return FileResponse(ROOT / "static" / "index.html", headers=PAGE_CACHE)


@app.get("/admin")
def admin_page():
    return FileResponse(ROOT / "static" / "admin.html", headers=PAGE_CACHE)


@app.get("/api/warmup")
def warmup():
    engine.load()
    return {"ready": True}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
