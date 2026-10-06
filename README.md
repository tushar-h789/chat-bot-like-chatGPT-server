# AI Chatbot API

FastAPI service for the chatbot. The browser talks only to this API. The OpenAI key stays here.

## Local setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

If `python3 -m venv` reports that `ensurepip` is unavailable, the OS package `python3.14-venv` is missing. Do not install it unless you mean to change the system. This repository was bootstrapped without that package:

```bash
python3 -m venv --without-pip .venv
curl -fsSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python3
.venv/bin/pip install -e ".[dev]"
```

PostgreSQL starts from the repository root:

```bash
docker compose up -d postgres
```

The local database user, password, and database name are all `chatbot`, on port `5432`. PostgreSQL 18 keeps its files under `/var/lib/postgresql`.

Apply migrations, then check the API:

```bash
alembic upgrade head
curl http://127.0.0.1:8000/api/v1/health
```

A healthy database returns `{"status":"ok"}`. Auth routes are documented in [../docs/api.md](../docs/api.md). The Bruno collection is `bruno/ai-chatbot-api`.

## Tests

```bash
pytest
```
