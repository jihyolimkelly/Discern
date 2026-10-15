import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps, UnidentifiedImageError
from eyeasd.morphology import box_mean, erode, largest_component

SEG_MODEL_ID = "pamixsun/segformer_for_optic_disc_cup_segmentation"
_MODELS = Path(__file__).resolve().parent.parent / "models"
SEG_MODEL_PATH = Path(
    os.environ.get("EYEASD_SEG_MODEL")
    or next((p for p in (_MODELS / "segformer_disc_cup.fp32.onnx", _MODELS / "segformer_disc_cup.int8.onnx") if p.exists()), "")
)
SEG_INPUT = 512
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)


def make_session(model_path: Path) -> ort.InferenceSession:
    options = ort.SessionOptions()
    options.intra_op_num_threads = int(os.environ.get("EYEASD_ORT_THREADS", "0"))
    return ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])

WORK_SIZE = 1024
ROI_SCALE = 1.5
REFINE_SCALE = 4.0
FALLBACK_WINDOW = 0.35
MIN_DISC_PROB = 0.05
MIN_DISC_PX = 20


def is_dicom(data: bytes) -> bool:
    return data[128:132] == b"DICM"


def _dicom_to_image(path_or_file) -> Image.Image:
    import pydicom

    arr = pydicom.dcmread(path_or_file).pixel_array
    if arr.ndim == 4 or (arr.ndim == 3 and arr.shape[-1] not in (3, 4)):
        arr = arr[0]
    if arr.dtype != np.uint8:
        arr = arr.astype(np.float32)
        arr = ((arr - arr.min()) / max(float(arr.max() - arr.min()), 1e-6) * 255).astype(np.uint8)
    return Image.fromarray(arr)


def load_fundus(path_or_file) -> tuple[np.ndarray, tuple[int, int]]:
    try:
        img = ImageOps.exif_transpose(Image.open(path_or_file))
    except UnidentifiedImageError:
        if hasattr(path_or_file, "seek"):
            path_or_file.seek(0)
        img = _dicom_to_image(path_or_file)
    img = img.convert("RGB")
    original_size = img.size
    scale = WORK_SIZE / max(img.size)
    if scale != 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.BICUBIC)
    return np.asarray(img), original_size


@dataclass
class DiscCupResult:
    disc: np.ndarray
    cup: np.ndarray
    center: tuple[float, float]
    disc_diameter: float
    vcdr: float
    hcdr: float
    area_cdr: float


def _extent(mask: np.ndarray, axis: int) -> int:
    idx = np.where(mask.any(axis=axis))[0]
    return int(idx[-1] - idx[0] + 1) if len(idx) else 0


class OpticDiscSegmenter:
    def __init__(self, model_path: Path = SEG_MODEL_PATH):
        self.session = make_session(model_path)

    def _probs(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        x = np.asarray(Image.fromarray(rgb).resize((SEG_INPUT, SEG_INPUT), Image.BILINEAR), np.float32) / 255
        x = ((x - IMAGENET_MEAN) / IMAGENET_STD).transpose(2, 0, 1)[None]
        logits = self.session.run(None, {"pixel_values": x})[0][0]
        logits = np.stack([np.asarray(Image.fromarray(c).resize((w, h), Image.BILINEAR)) for c in logits])
        e = np.exp(logits - logits.max(0, keepdims=True))
        return e / e.sum(0, keepdims=True)

    def segment(self, rgb: np.ndarray) -> DiscCupResult | None:
        h, w = rgb.shape[:2]
        probs = self._probs(rgb)
        labels = probs.argmax(0)
        disc = largest_component(labels >= 1)

        if disc.sum() >= MIN_DISC_PX:
            ys, xs = np.nonzero(disc)
            cx, cy = xs.mean(), ys.mean()
            half = max(_extent(disc, 0), _extent(disc, 1)) * REFINE_SCALE / 2
        else:
            disc_prob = box_mean(probs[1] + probs[2], max(3, min(h, w) // 50))
            if disc_prob.max() < MIN_DISC_PROB:
                return None
            cy, cx = np.unravel_index(disc_prob.argmax(), disc_prob.shape)
            half = FALLBACK_WINDOW * min(h, w) / 2
        x0, x1 = int(max(0, cx - half)), int(min(w, cx + half))
        y0, y1 = int(max(0, cy - half)), int(min(h, cy + half))
        refined = np.zeros_like(labels)
        refined[y0:y1, x0:x1] = self._probs(np.ascontiguousarray(rgb[y0:y1, x0:x1])).argmax(0)
        if (refined >= 1).sum() >= MIN_DISC_PX:
            labels = refined

        disc = largest_component(labels >= 1)
        if disc.sum() < MIN_DISC_PX:
            return None
        return result_from_masks(disc, largest_component((labels == 2) & disc) & disc)


def result_from_masks(disc: np.ndarray, cup: np.ndarray) -> DiscCupResult | None:
    if disc.sum() < MIN_DISC_PX:
        return None
    cup = cup & disc
    ys, xs = np.nonzero(disc)
    disc_v, disc_h = _extent(disc, 1), _extent(disc, 0)
    return DiscCupResult(
        disc=disc,
        cup=cup,
        center=(float(xs.mean()), float(ys.mean())),
        disc_diameter=float(max(disc_v, disc_h)),
        vcdr=_extent(cup, 1) / disc_v,
        hcdr=_extent(cup, 0) / disc_h,
        area_cdr=float(cup.sum() / disc.sum()),
    )


def ellipse_mask(shape: tuple[int, int], cx: float, cy: float, rx: float, ry: float) -> np.ndarray:
    yy, xx = np.ogrid[: shape[0], : shape[1]]
    return ((xx - cx) / max(rx, 0.5)) ** 2 + ((yy - cy) / max(ry, 0.5)) ** 2 <= 1.0


def mask_ellipse(mask: np.ndarray) -> dict | None:
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    return {
        "cx": float(xs.min() + xs.max()) / 2,
        "cy": float(ys.min() + ys.max()) / 2,
        "rx": float(xs.max() - xs.min() + 1) / 2,
        "ry": float(ys.max() - ys.min() + 1) / 2,
    }


def crop_roi(rgb: np.ndarray, seg: DiscCupResult, scale: float = ROI_SCALE) -> Image.Image:
    side = max(int(round(seg.disc_diameter * scale)), 32)
    cx, cy = seg.center
    left, top = int(round(cx - side / 2)), int(round(cy - side / 2))
    return Image.fromarray(rgb).crop((left, top, left + side, top + side))


def draw_overlay(rgb: np.ndarray, seg: DiscCupResult) -> Image.Image:
    out = rgb.astype(np.float32)
    thickness = max(1, round(max(rgb.shape[:2]) / 400))
    for mask, color in ((seg.disc & ~seg.cup, (34, 211, 166)), (seg.cup, (79, 141, 255))):
        out[mask] = out[mask] * 0.65 + np.array(color) * 0.35
    for mask, color in ((seg.disc, (34, 211, 166)), (seg.cup, (79, 141, 255))):
        edge = mask & ~erode(mask, thickness)
        out[edge] = color
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))
