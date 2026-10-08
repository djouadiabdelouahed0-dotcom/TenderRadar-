# TenderRadar MVP 0.1

A minimal proof-of-concept for searching live EU procurement notices through the official TED Search API.

## What is included
- FastAPI backend
- Simple browser UI
- CPV + buyer country filters
- Minimum-value post-filter
- Heuristic match score
- Official TED notice links
- No database, login, AI, paid API, ads, or payment system

## Run locally

1. Install Python 3.11+.
2. Open a terminal in this folder.
3. Create a virtual environment:

```bash
python -m venv .venv
```

4. Activate it.

Windows PowerShell:
```powershell
.venv\\Scripts\\Activate.ps1
```

macOS/Linux:
```bash
source .venv/bin/activate
```

5. Install dependencies:

```bash
pip install -r requirements.txt
```

6. Start the app:

```bash
uvicorn app:app --reload
```

7. Open:

`http://127.0.0.1:8000`

## First test
Use:
- CPV: `72000000`
- Country: `DE`
- Minimum value: `0`

The backend asks TED for active notices published in the last 30 days and then performs the minimum-value filter locally.

## Important
The match score is an MVP heuristic, not a prediction of winning probability.
The API response schema can evolve; the normalisation layer is deliberately isolated in `app.py` so it can be adjusted without changing the UI.
