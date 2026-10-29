# Discern

> **Note on Repository History**  
> Discern has been tested in real-world settings, including schools and hospitals. Because the original development records contain sensitive information that cannot be publicly disclosed, this public repository contains the codebase but not its full history to protect privacy and security.

Segments the optic disc and cup in a fundus photo, measures the cup-to-disc ratio, and gives an ASD severity estimate that an admin reviews. Research use only, not a diagnosis.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --port 8000
```

- `/` landing page
- `/analyze` upload and result
- `/admin` cases, review, model, settings

On first start the admin password is created in `data/config.json`.

## Environment variables

| Name | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres URL. Without it, SQLite in `data/` is used. |
| `ADMIN_PASSWORD`, `SECRET_KEY` | Admin login (required on Vercel) |
| `OPENAI_API_KEY` | Optional. Provisional estimate while no model is trained. |

## Deploy (Vercel)

```bash
vercel deploy --prod
```

The model files are large, so set `VERCEL_SUPPORT_LARGE_FUNCTIONS=1` in the project's environment variables.

## Folders

- `app.py`: API and pages
- `eyeasd/`: segmentation, measurements, severity model, database
- `models/`: ONNX models (SegFormer-B3 disc/cup, ResNet-50 features)
- `static/`: pages, styles, scripts, images
