import hashlib
import io
import os
import secrets
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from eyeasd.db import AIEstimate, Case, CaseFile, Model, utcnow
from eyeasd.segmentation import (
    DiscCupResult,
    OpticDiscSegmenter,
    crop_roi,
    draw_overlay,
    ellipse_mask,
    is_dicom,
    load_fundus,
    result_from_masks,
)
from eyeasd.severity import FeatureExtractor, MLPHead, train_head, training_readiness


def encode(img: Image.Image, fmt: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **({"quality": 90} if fmt == "JPEG" else {}))
    return buf.getvalue(), f"image/{fmt.lower()}"


def model_version(model_id: int) -> str:
    return f"model-{model_id}"


INITIAL_TRIGGER = "initial"


class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._segmenter: OpticDiscSegmenter | None = None
        self._extractor: FeatureExtractor | None = None
        self._head_id: int | None = None
        self._head: MLPHead | None = None

    def load(self) -> None:
        with self._load_lock:
            if self._segmenter is None:
                self._segmenter = OpticDiscSegmenter()
            if self._extractor is None:
                self._extractor = FeatureExtractor()

    @property
    def segmenter(self) -> OpticDiscSegmenter:
        self.load()
        return self._segmenter

    @property
    def extractor(self) -> FeatureExtractor:
        self.load()
        return self._extractor


    def active_head(self, db: Session) -> tuple[int | None, MLPHead | None]:
        model_id = db.scalar(select(Model.id).where(Model.active))
        if model_id != self._head_id:
            self._head = MLPHead.from_bytes(db.get(Model, model_id).params) if model_id else None
            self._head_id = model_id
        return self._head_id, self._head


    def create_case(self, db: Session, data: bytes, filename: str, consent: bool, keep_original: bool = True) -> Case:
        timing, t = {}, time.perf_counter()

        def lap(name):
            nonlocal t
            now = time.perf_counter()
            timing[name] = round((now - t) * 1000)
            t = now

        try:
            fmt = "dcm" if is_dicom(data) else (Image.open(io.BytesIO(data)).format or "img").lower()
            rgb, (orig_w, orig_h) = load_fundus(io.BytesIO(data))
        except Exception as exc:
            raise ValueError("Could not read the image file.") from exc
        lap("decode")

        model_id, head = self.active_head(db)
        self.load()
        lap("load")
        with self.lock:
            seg = self.segmenter.segment(rgb)
            lap("segment")
            roi = crop_roi(rgb, seg) if seg else None
            feature = self.extractor.embed(roi) if roi is not None else None
            lap("embed")
        probs = head.predict(feature) if head is not None and feature is not None else None

        case = Case(
            public_id=secrets.token_urlsafe(9),
            filename=Path(filename or "upload").name[:255],
            sha256=hashlib.sha256(data).hexdigest(),
            original_ext={"jpeg": "jpg", "mpo": "jpg"}.get(fmt, fmt)[:8],
            width=orig_w,
            height=orig_h,
            consent=consent,
            disc_found=seg is not None,
            model_version=model_version(model_id) if probs else None,
            model_probs=probs,
            model_predicted=max(probs, key=probs.get) if probs else None,
        )
        files = {"display": encode(Image.fromarray(rgb), "JPEG")}
        if keep_original:
            files["original"] = (data, "application/dicom" if fmt == "dcm" else f"image/{'jpeg' if fmt in ('jpeg', 'mpo') else fmt}")
        if seg:
            files.update(self._seg_files(case, rgb, seg, roi, feature))
        lap("encode")
        db.add(case)
        db.flush()
        db.add_all(CaseFile(case_id=case.id, kind=k, data=d, content_type=ct) for k, (d, ct) in files.items())
        db.commit()
        lap("store")
        affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        self.last_timing = {**timing, "cpus": os.cpu_count(), "cpu_affinity": affinity}
        return case

    @staticmethod
    def _seg_files(case: Case, rgb: np.ndarray, seg: DiscCupResult, roi: Image.Image, feature: np.ndarray) -> dict:
        scale = max(case.width, case.height) / max(rgb.shape[:2])
        case.vcdr, case.hcdr, case.area_cdr = seg.vcdr, seg.hcdr, seg.area_cdr
        case.disc_diameter_px = seg.disc_diameter * scale
        case.disc_found = True
        overlay = draw_overlay(rgb, seg)
        return {
            "overlay": encode(overlay, "JPEG"),
            "roi": encode(roi, "PNG"),
            "roi_overlay": encode(crop_roi(np.asarray(overlay), seg), "JPEG"),
            "disc_mask": encode(Image.fromarray(seg.disc.astype(np.uint8) * 255), "PNG"),
            "cup_mask": encode(Image.fromarray(seg.cup.astype(np.uint8) * 255), "PNG"),
            "embedding": (feature.tobytes(), "application/x-float32"),
        }

    @staticmethod
    def _png_mask(db: Session, case: Case, kind: str) -> np.ndarray | None:
        f = db.get(CaseFile, (case.id, kind))
        return np.asarray(Image.open(io.BytesIO(f.data))) > 127 if f else None

    def display_rgb(self, db: Session, case: Case) -> np.ndarray:
        return np.asarray(Image.open(io.BytesIO(db.get(CaseFile, (case.id, "display")).data)).convert("RGB"))

    def current_masks(self, db: Session, case: Case) -> tuple[np.ndarray | None, np.ndarray | None]:
        return self._png_mask(db, case, "disc_mask"), self._png_mask(db, case, "cup_mask")


    def review_segmentation(self, db: Session, case: Case, action: str, disc: dict | None = None, cup: dict | None = None) -> Case:
        if action == "confirm":
            if not case.disc_found:
                raise ValueError("There is no detected optic disc to confirm. Draw it with Correct instead.")
            case.seg_status, case.seg_reviewed_at = "confirmed", utcnow()
            db.commit()
            return case

        rgb = self.display_rgb(db, case)
        if action == "correct":
            disc_mask = ellipse_mask(rgb.shape[:2], **disc)
            cup_mask = ellipse_mask(rgb.shape[:2], **cup) & disc_mask
            for kind in ("disc_mask", "cup_mask"):
                current = db.get(CaseFile, (case.id, kind))
                if current is not None and db.get(CaseFile, (case.id, kind.replace("_mask", "_auto"))) is None:
                    db.add(CaseFile(case_id=case.id, kind=kind.replace("_mask", "_auto"), data=current.data, content_type=current.content_type))
            status = "corrected"
        elif action == "revert":
            disc_mask, cup_mask = self._png_mask(db, case, "disc_auto"), self._png_mask(db, case, "cup_auto")
            if disc_mask is None:
                raise ValueError("This case has no automatic segmentation to go back to.")
            status = "auto"
        else:
            raise ValueError(f"Unknown action {action!r}.")

        seg = result_from_masks(disc_mask, cup_mask)
        if seg is None:
            raise ValueError("The optic disc is too small.")
        roi = crop_roi(rgb, seg)
        with self.lock:
            feature = self.extractor.embed(roi)
        for kind, (data, content_type) in self._seg_files(case, rgb, seg, roi, feature).items():
            db.merge(CaseFile(case_id=case.id, kind=kind, data=data, content_type=content_type))
        model_id, head = self.active_head(db)
        probs = head.predict(feature) if head is not None else None
        case.model_probs, case.model_predicted = probs, (max(probs, key=probs.get) if probs else None)
        case.model_version = model_version(model_id) if probs else None
        db.execute(delete(AIEstimate).where(AIEstimate.case_id == case.id))
        case.seg_status, case.seg_reviewed_at = status, (utcnow() if status != "auto" else None)
        db.commit()
        return case

    def _feature(self, db: Session, case: Case) -> np.ndarray | None:
        stored = db.get(CaseFile, (case.id, "embedding"))
        if stored is not None:
            return np.frombuffer(stored.data, np.float32)
        roi = db.get(CaseFile, (case.id, "roi"))
        if roi is None:
            return None
        with self.lock:
            feature = self.extractor.embed(Image.open(io.BytesIO(roi.data)))
        db.add(CaseFile(case_id=case.id, kind="embedding", data=feature.tobytes(), content_type="application/x-float32"))
        return feature


    @staticmethod
    def label_counts(db: Session) -> dict[str, int]:
        rows = db.execute(
            select(Case.reviewer_label, func.count())
            .where(Case.status == "approved", Case.reviewer_label.is_not(None), Case.disc_found)
            .group_by(Case.reviewer_label)
        ).all()
        return dict(rows)

    def train(self, db: Session, trigger: str) -> Model:
        reason = training_readiness(self.label_counts(db))
        if reason:
            raise ValueError(reason)
        cases = db.scalars(
            select(Case).where(Case.status == "approved", Case.reviewer_label.is_not(None), Case.disc_found).order_by(Case.id)
        ).all()
        rows = [(c, self._feature(db, c)) for c in cases]
        rows = [(c, f) for c, f in rows if f is not None]
        features = np.stack([f for _, f in rows])
        with self.lock:
            result = train_head(features, [c.reviewer_label for c, _ in rows], [c.sha256 for c, _ in rows])
        model = Model(
            trigger=trigger,
            classes=result.head.classes,
            label_count=len(rows),
            n_train=result.n_train,
            n_val=result.n_val,
            metrics=result.metrics,
            params=result.head.to_bytes(),
        )
        db.add(model)
        db.flush()
        self.activate(db, model.id)
        return model

    def activate(self, db: Session, model_id: int) -> None:
        db.execute(update(Model).values(active=Model.id == model_id))
        head = MLPHead.from_bytes(db.get(Model, model_id).params)
        rows = db.execute(
            select(Case, CaseFile.data).join(CaseFile, (CaseFile.case_id == Case.id) & (CaseFile.kind == "embedding"))
        ).all()
        if rows:
            probs = head.predict_proba(np.stack([np.frombuffer(d, np.float32) for _, d in rows]))
            for (case, _), p in zip(rows, probs):
                case.model_probs = dict(zip(head.classes, p.tolist()))
                case.model_predicted = head.classes[int(p.argmax())]
                case.model_version = model_version(model_id)
        db.commit()
        self._head_id, self._head = model_id, head

    def maybe_auto_retrain(self, db: Session, settings: dict) -> Model | None:
        if not settings.get("auto_retrain"):
            return None
        counts = self.label_counts(db)
        if training_readiness(counts):
            return None
        if self.new_labels(db, counts) < max(1, int(settings.get("retrain_every", 20))):
            return None
        return self.train(db, trigger="auto")

    @staticmethod
    def new_labels(db: Session, counts: dict[str, int]) -> int:
        active = db.scalar(select(Model).where(Model.active))
        used = active.label_count if active is not None and active.trigger != INITIAL_TRIGGER else 0
        return max(0, sum(counts.values()) - used)
