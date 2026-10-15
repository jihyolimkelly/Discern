import base64
import http.client
import json
import os
import re
import urllib.error
import urllib.request

from eyeasd.severity import SEVERITY_CLASSES

MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
RESPONSES_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses"
TIMEOUT_S = 60

PROMPT = """You are scoring a pediatric fundus photograph for a research prototype that reproduces a paper \
linking the optic nerve head to autism spectrum disorder (ASD) symptom severity. Use these published findings:
- Kim et al., JAMA Network Open 2023 (958 children, Severance Hospital, Seoul): deep-learning models screened ASD \
from retinal photographs and kept their performance using only the 10% of the image around the optic disc; \
symptom-severity prediction was moderate (AUROC 0.74).
- Small OCT studies in children with ASD report a thinner peripapillary retinal nerve fiber layer, mostly in the \
temporal and nasal-superior sectors.
- A retinal-photograph study found a larger optic disc diameter in children with ASD (279.5 vs 262.0 um) and a \
slightly higher cup-to-disc ratio (0.445 vs 0.414).
- A typical pediatric cup-to-disc area ratio is about 0.21.

Image 1: cropped optic disc region. Image 2: the same region with the segmented optic disc (green) and optic cup (blue).
Measured: vertical cup-to-disc ratio {vcdr:.2f}, horizontal {hcdr:.2f}, area {acdr:.2f}, disc diameter {disc_px:.0f} px.

Give your best-guess probabilities for normal, mild, moderate and severe, summing to 1. You must commit to an \
estimate: do not say it cannot be assessed, do not return equal probabilities, and give the most likely level at \
least 0.40. Base it on disc size, cup-to-disc ratio, neuroretinal rim, nerve fiber layer appearance and vessel \
pattern. Rationale: one short impersonal sentence (no "I", under 20 words) naming the main features."""

RETRY_NOTE = (
    "Your previous answer gave every level about the same probability. That is not allowed here: "
    "pick the most likely level and give it at least 0.40."
)

SCHEMA = {
    "type": "object",
    "properties": {
        **{c: {"type": "number"} for c in SEVERITY_CLASSES},
        "rationale": {"type": "string"},
        "image_quality": {"type": "string", "enum": ["good", "fair", "poor"]},
    },
    "required": [*SEVERITY_CLASSES, "rationale", "image_quality"],
    "additionalProperties": False,
}


def _one_line(text: str) -> str:
    text = " ".join(text.split())
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first if len(first) <= 200 else first[:197].rstrip() + "…"


class EstimateError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _request(roi_png: bytes, roi_overlay_jpg: bytes, prompt: str) -> tuple[dict, str]:
    body = {
        "model": MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": _data_url(roi_png, "image/png")},
                    {"type": "input_image", "image_url": _data_url(roi_overlay_jpg, "image/jpeg")},
                ],
            }
        ],
        "text": {"format": {"type": "json_schema", "name": "asd_estimate", "schema": SCHEMA, "strict": True}},
    }
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as r:
            response = json.load(r)
        text = next(
            c["text"]
            for item in response.get("output", [])
            if item.get("type") == "message"
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        )
        data = json.loads(text)
        raw = {c: max(float(data[c]), 0.0) for c in SEVERITY_CLASSES}
    except urllib.error.HTTPError as exc:
        raise EstimateError(f"Estimate service error {exc.code}.") from exc
    except (OSError, http.client.HTTPException, StopIteration, KeyError, ValueError) as exc:
        raise EstimateError(f"Could not get an estimate ({type(exc).__name__}).") from exc
    total = sum(raw.values()) or 1.0
    return {**data, "probs": {c: v / total for c, v in raw.items()}}, response.get("model", MODEL)


def estimate(roi_png: bytes, roi_overlay_jpg: bytes, metrics: dict) -> dict:
    if not available():
        raise EstimateError("The estimate service is not configured.")
    prompt = PROMPT.format(
        vcdr=metrics["vcdr"], hcdr=metrics["hcdr"], acdr=metrics["area_cdr"], disc_px=metrics.get("disc_diameter_px") or 0
    )
    data, model = _request(roi_png, roi_overlay_jpg, prompt)
    if max(data["probs"].values()) - min(data["probs"].values()) < 0.10:
        data, model = _request(roi_png, roi_overlay_jpg, prompt + "\n\n" + RETRY_NOTE)
    probs = data["probs"]
    return {
        "model": model,
        "probs": probs,
        "predicted": max(probs, key=probs.get),
        "rationale": _one_line(str(data["rationale"])),
        "image_quality": data["image_quality"],
    }
