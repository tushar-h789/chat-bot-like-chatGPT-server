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

A healthy database returns `{"status":"ok"}`. Auth, conversation, and chat stream routes are documented in [../docs/api.md](../docs/api.md). The Bruno collection is `bruno/`. It does not call `POST /api/v1/chat`.

`AI_PROVIDER` is `gemini` or `openai`. The default is `gemini`.

Gemini settings belong in `.env` only: `GEMINI_API_KEY`, `GEMINI_MODEL`, and `GEMINI_TIMEOUT_SECONDS`. Copy them from `.env.example`. The suggested free-tier model is `gemini-3.5-flash-lite`. It answers quickly. `gemini-3.8-flash` is also free, but it is often slow or busy. Get a key from [Google AI Studio](https://aistudio.google.com/apikey). Do not paste the key into chat. `POST /api/v1/chat` uses the provider named by `AI_PROVIDER`. The model can call `current_time` and `calculate`. The server runs the function and sends the result back. `web_search` turns on Google Search for Gemini, or web search for OpenAI, for that message. `POST /api/v1/chat/regenerate` replaces the latest assistant reply in that same row.

`POST /api/v1/files` stores an upload for the signed-in user. Chat can attach those files to a message. Text file contents are included in the model turn. Images are sent as image input. PDFs are sent as document input. `GET /api/v1/usage` returns token totals for the signed-in user. `USAGE_LIMIT_TOKENS_PER_DAY` refuses chat once today's tokens reach that cap. `0` turns the cap off. Set the optional USD-per-million prices in `.env` if you want `cost_usd`. Leave them empty to skip dollars. Login and register are limited per address. Chat is limited per user. `compose.prod.yaml` at the repository root is the production Compose file. It reads secrets from the environment and is not started here.

`OPENAI_API_KEY` and `OPENAI_MODEL` also belong in `.env` only. Tests inject a fake provider and do not call OpenAI or Gemini.

## Tests

```bash
pytest
```
