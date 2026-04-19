# batiment_py

Site FastAPI + Jinja2 pour estimation travaux, pages locales SEO, et collecte de leads.

## Lancer en local
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8081
```
Ouvrir: http://127.0.0.1:8081

## Configuration Outlook (envoi devis client + compte rendu interne)
Le backend charge automatiquement `batiment_py/.env` au demarrage.

1. Copier le modele:
```bash
cp .env.example .env
```
2. Renseigner `SMTP_PASSWORD` dans `.env`.
3. Lancer avec le script dedie:
```bash
./run_outlook.sh
```

## Configuration Gmail (option)
Si vous utilisez `divclass72@gmail.com`:
```bash
cp .env.gmail.example .env
./run_gmail.sh
```

Prerequis Gmail:
- activer la validation en 2 etapes du compte Google,
- creer un mot de passe d'application Gmail (16 caracteres) pour `SMTP_PASSWORD`.

## Mode devis + rendu 3D reel (obligatoire)
L'endpoint `/api/architecture-3d` est en mode strict:
- pas de fallback demo/photo-preview,
- generation 3D IA obligatoire,
- envoi devis client obligatoire,
- copie interne obligatoire.

Variables necessaires:
- `OPENAI_API_KEY` (generation rendu 3D)
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_SSL`, `SMTP_STARTTLS`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`
- `SMTP_FROM_NAME`, `SMTP_REPLY_TO`
- `INTERNAL_REPORT_EMAIL` (copie interne)
- `EMAIL_REMINDER_J1_ENABLED` (optionnel, default `true` pour relance J+1)

## Tracking conversion leads
- Cookie visiteur first-party: `rb_vid` (180 jours)
- Tracking capture: `visitor_id`, landing page, referrer, UTM
- Donnees rattachees aux depots:
  - `/api/lead` (contact classique)
  - `/api/leads` (chat)
  - `/api/handoff` (transfert conseiller)
  - `/api/architecture-3d` (devis intelligent)

## Fichiers importants
- `app.py`: routes, SEO, API chat, capture de lead
- `seo.py`: configuration site, JSON-LD, sitemap
- `pricing.py`: estimation deterministe (fallback)
- `db.py`: stockage des leads SQLite (`leads.sqlite`)
- `run_outlook.sh`: demarrage avec defaults Outlook sur `devis@eurobatservices.com`

## A personnaliser
- `seo.py`: remplacer `SITE["url"]`, `SITE["phone"]`, `SITE["email"]`
- `content/services.json` et `content/cities.json`

## Intelligence Eurobat (capture multi-sources IDF)

Le projet inclut maintenant un moteur de capture geolocalisee pour:
- besoins / annonces / chantiers BTP en Ile-de-France,
- auto-requetage par departement et ville,
- collecte Google + plateformes + social,
- deduplication, scoring et stockage SQL.

### Endpoints API
- `GET /api/intelligence/health`
- `POST /api/intelligence/queries/preview`
- `POST /api/intelligence/run`
- `GET /api/intelligence/signals`

Exemple run API:
```bash
curl -X POST http://127.0.0.1:8081/api/intelligence/run \
  -H "Content-Type: application/json" \
  -d '{
    "department_codes": ["75", "92", "93", "94"],
    "cities": ["Paris", "Saint-Denis"],
    "max_queries": 80,
    "min_score": 35,
    "dry_run": false
  }'
```

### Script CLI
```bash
python scripts/run_intelligence.py --departments 75,92,93 --cities Paris,Saint-Denis --max-queries 80
```

Mode boucle (auto-run periodique):
```bash
python scripts/run_intelligence.py --departments 75,92,93 --interval-minutes 60
```

### Configuration
- Fichier: `content/intelligence_config.json`
- Optionnel: `SERPAPI_API_KEY` pour enrichir la recherche Google Web (source `serpapi`).

### Stockage
- Table SQL ajoutee: `opportunity_signals`
- Champs: source, url, localisation, type d'annonce, budget, contact, score, payload brut.

## SaaS IA (Essai 2 mois + Abonnement)

Le backend gere maintenant un mode SaaS pour 3 produits IA:
- `eurobat_capture`
- `devis_intelligent`
- `architecture_3d`

Fonctionnalites:
- creation de tenant (client),
- activation d'essai 60 jours (2 mois),
- upgrade vers abonnement mensuel,
- verification des droits d'acces par produit,
- entrainement IA a la demande (jobs + versionning modele).

### Endpoints SaaS IA
- `GET /api/saas-ai/health`
- `POST /api/saas-ai/tenants`
- `POST /api/saas-ai/tenants/{tenant_id}/trial`
- `POST /api/saas-ai/tenants/{tenant_id}/subscriptions/upgrade`
- `GET /api/saas-ai/tenants/{tenant_id}/subscriptions`
- `GET /api/saas-ai/tenants/{tenant_id}/entitlement?product_code=eurobat_capture`
- `POST /api/saas-ai/models/train`
- `POST /api/saas-ai/models/jobs/{job_id}/start`
- `POST /api/saas-ai/models/jobs/{job_id}/complete`
- `POST /api/saas-ai/models/jobs/{job_id}/fail`
- `GET /api/saas-ai/tenants/{tenant_id}/models`
- `GET /api/saas-ai/tenants/{tenant_id}/training-jobs`

Exemple (essai 2 mois):
```bash
curl -X POST http://127.0.0.1:8081/api/saas-ai/tenants \
  -H "Content-Type: application/json" \
  -d '{"company_name":"Eurobat","contact_email":"ops@eurobat.fr"}'

curl -X POST http://127.0.0.1:8081/api/saas-ai/tenants/1/trial \
  -H "Content-Type: application/json" \
  -d '{"product_codes":["eurobat_capture","devis_intelligent","architecture_3d"],"trial_days":60}'
```
