# batiment_py

Site FastAPI + Jinja2 pour estimation travaux, pages locales SEO, et collecte de leads.

## Lancer en local
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```
Ouvre: http://localhost:8000

## Fichiers importants
- `app.py`: routes, SEO, API chat, capture de lead
- `seo.py`: configuration site, JSON-LD, sitemap
- `pricing.py`: estimation deterministe (fallback)
- `db.py`: stockage des leads SQLite (`leads.sqlite`)

## A personnaliser
- `seo.py`: remplacer `SITE["url"]`, `SITE["phone"]`, `SITE["email"]`
- `content/services.json` et `content/cities.json`

# batiment-fastapi
