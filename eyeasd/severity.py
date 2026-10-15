import io
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from eyeasd.segmentation import make_session

SEVERITY_CLASSES = ["normal", "mild", "moderate", "severe"]
INPUT_SIZE = 224
FEATURE_DIM = 2048
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)
BACKBONE_PATH = Path(
    os.environ.get("EYEASD_BACKBONE", Path(__file__).resolve().parent.parent / "models" / "resnet50_features.onnx")
)

MIN_PER_CLASS = 3
HIDDEN = 128
TRAIN_TIME_BUDGET_S = 40


class FeatureExtractor:
    def __init__(self, model_path: Path = BACKBONE_PATH):
        self.session = make_session(model_path)

    def embed(self, roi: Image.Image) -> np.ndarray:
        x = np.asarray(roi.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR), np.float32) / 255
        x = ((x - IMAGENET_MEAN) / IMAGENET_STD).transpose(2, 0, 1)[None]
        return self.session.run(None, {"image": x})[0][0].astype(np.float32)


@dataclass
class MLPHead:
    classes: list[str]
    mean: np.ndarray
    std: np.ndarray
    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        h = np.maximum((np.atleast_2d(features) - self.mean) / self.std @ self.w1 + self.b1, 0)
        logits = h @ self.w2 + self.b2
        e = np.exp(logits - logits.max(1, keepdims=True))
        return e / e.sum(1, keepdims=True)

    def predict(self, features: np.ndarray) -> dict[str, float]:
        return dict(zip(self.classes, self.predict_proba(features)[0].tolist()))

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        np.savez_compressed(buf, classes=np.array(self.classes), mean=self.mean, std=self.std, w1=self.w1, b1=self.b1, w2=self.w2, b2=self.b2)
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> "MLPHead":
        z = np.load(io.BytesIO(data))
        return cls(classes=z["classes"].tolist(), **{k: z[k] for k in ("mean", "std", "w1", "b1", "w2", "b2")})


@dataclass
class TrainResult:
    head: MLPHead
    n_train: int
    n_val: int
    metrics: dict = field(default_factory=dict)


def training_readiness(label_counts: dict[str, int]) -> str | None:
    usable = [c for c, n in label_counts.items() if n >= MIN_PER_CLASS]
    if len(usable) < 2:
        return f"Needs at least {MIN_PER_CLASS} approved cases in each of two severity levels."
    return None


def split_indices(labels: list[str], groups: list[str], val_fraction: float = 0.2, seed: int = 0):
    rng = np.random.default_rng(seed)
    by_label: dict[str, list[str]] = {}
    for lab, g in zip(labels, groups):
        by_label.setdefault(lab, [])
        if g not in by_label[lab]:
            by_label[lab].append(g)
    val_groups = set()
    for gs in by_label.values():
        gs = sorted(gs)
        rng.shuffle(gs)
        if len(gs) >= MIN_PER_CLASS:
            val_groups.update(gs[: max(1, round(len(gs) * val_fraction))])
    val = [i for i, g in enumerate(groups) if g in val_groups]
    train = [i for i, g in enumerate(groups) if g not in val_groups]
    return np.array(train), np.array(val)


def _evaluate(head: MLPHead, x: np.ndarray, y: np.ndarray) -> dict:
    k = len(head.classes)
    pred = head.predict_proba(x).argmax(1)
    cm = np.zeros((k, k), int)
    np.add.at(cm, (y, pred), 1)
    tp = np.diag(cm).astype(float)
    present = cm.sum(1) > 0
    precision = np.divide(tp, cm.sum(0), out=np.zeros(k), where=cm.sum(0) > 0)
    recall = np.divide(tp, cm.sum(1), out=np.zeros(k), where=present)
    f1 = np.divide(2 * precision * recall, precision + recall, out=np.zeros(k), where=(precision + recall) > 0)
    return {
        "accuracy": float(tp.sum() / max(cm.sum(), 1)),
        "precision": float(precision[present].mean()) if present.any() else 0.0,
        "recall": float(recall[present].mean()) if present.any() else 0.0,
        "f1": float(f1[present].mean()) if present.any() else 0.0,
        "per_class_recall": {c: float(recall[i]) for i, c in enumerate(head.classes) if present[i]},
        "confusion": cm.tolist(),
    }


def train_head(features: np.ndarray, labels: list[str], groups: list[str], seed: int = 0) -> TrainResult:
    classes = [c for c in SEVERITY_CLASSES if c in set(labels)] + sorted(set(labels) - set(SEVERITY_CLASSES))
    y_all = np.array([classes.index(l) for l in labels])
    train, val = split_indices(labels, groups, seed=seed)
    x, y = features[train], y_all[train]
    xv, yv = (features[val], y_all[val]) if len(val) else (x, y)

    mean, std = x.mean(0), x.std(0) + 1e-6
    xn, xvn = (x - mean) / std, (xv - mean) / std
    k, d = len(classes), features.shape[1]
    counts = np.bincount(y, minlength=k)
    cw = (counts.sum() / (k * np.maximum(counts, 1))).astype(np.float32)

    rng = np.random.default_rng(seed)
    p = {
        "w1": rng.normal(0, np.sqrt(2 / d), (d, HIDDEN)).astype(np.float32),
        "b1": np.zeros(HIDDEN, np.float32),
        "w2": rng.normal(0, np.sqrt(1 / HIDDEN), (HIDDEN, k)).astype(np.float32),
        "b2": np.zeros(k, np.float32),
    }
    m = {n: np.zeros_like(v) for n, v in p.items()}
    v2 = {n: np.zeros_like(v) for n, v in p.items()}
    lr, wd, beta1, beta2, dropout = 1e-3, 1e-3, 0.9, 0.999, 0.3
    onehot = np.eye(k, dtype=np.float32)[y]
    sample_w = cw[y] / cw[y].sum()

    def val_loss(params):
        h = np.maximum(xvn @ params["w1"] + params["b1"], 0)
        z = h @ params["w2"] + params["b2"]
        z = z - z.max(1, keepdims=True)
        logp = z - np.log(np.exp(z).sum(1, keepdims=True))
        return float(-(cw[yv] * logp[np.arange(len(yv)), yv]).sum() / cw[yv].sum())

    best, best_loss, patience, started = {n: v.copy() for n, v in p.items()}, np.inf, 0, time.monotonic()
    for step in range(1, 1001):
        pre = xn @ p["w1"] + p["b1"]
        h = np.maximum(pre, 0)
        mask = (rng.random(h.shape) > dropout).astype(np.float32) / (1 - dropout)
        hd = h * mask
        z = hd @ p["w2"] + p["b2"]
        e = np.exp(z - z.max(1, keepdims=True))
        prob = e / e.sum(1, keepdims=True)
        dz = (prob - onehot) * sample_w[:, None]
        grads = {"w2": hd.T @ dz + wd * p["w2"], "b2": dz.sum(0)}
        dh = (dz @ p["w2"].T) * mask * (pre > 0)
        grads.update(w1=xn.T @ dh + wd * p["w1"], b1=dh.sum(0))
        for n in p:
            m[n] = beta1 * m[n] + (1 - beta1) * grads[n]
            v2[n] = beta2 * v2[n] + (1 - beta2) * grads[n] ** 2
            p[n] -= lr * (m[n] / (1 - beta1**step)) / (np.sqrt(v2[n] / (1 - beta2**step)) + 1e-8)

        if step % 5 == 0:
            loss = val_loss(p)
            if loss < best_loss - 1e-4:
                best, best_loss, patience = {n: v.copy() for n, v in p.items()}, loss, 0
            else:
                patience += 1
            if patience >= 10 or time.monotonic() - started > TRAIN_TIME_BUDGET_S:
                break

    head = MLPHead(classes=classes, mean=mean.astype(np.float32), std=std.astype(np.float32), **best)
    metrics = _evaluate(head, xv, yv) if len(val) else {}
    metrics["validated"] = bool(len(val))
    return TrainResult(head=head, n_train=len(train), n_val=len(val), metrics=metrics)
