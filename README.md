# AI Chatbot API

FastAPI service for a ChatGPT-style chat. The browser talks only to this API. This process calls Gemini or OpenAI and stores the transcript in PostgreSQL. The provider API key stays in `.env` and never goes to the browser.

`AI_PROVIDER` is `gemini` or `openai`. The default is `gemini`. The suggested model is `gemini-3.5-flash-lite`.

The rules this service enforces are in [Business rules](#business-rules). The same rules are written in [docs/business-logic.md](docs/business-logic.md). The HTTP shapes are in [docs/api.md](docs/api.md).

## Stack

- Python 3.13+, FastAPI, Pydantic v2, Uvicorn
- SQLAlchemy 2 async, Alembic, asyncpg, PostgreSQL 18
- `pwdlib` with Argon2id for passwords
- `google-genai` Interactions API, or the official `openai` SDK `responses.create`
- pytest for tests. Tests inject a fake provider and do not call Gemini or OpenAI.

## Business rules

### Accounts

Email is trimmed and stored in lowercase. Passwords are 12 to 128 characters and stored as Argon2id hashes. There is no email verification. A duplicate email is `409`. A wrong email and a wrong password both return the same `401`.

Login creates an opaque session token in an `HttpOnly` `session` cookie. The database stores only the SHA-256 hash. The session lasts 7 days from creation and is not extended later. Logout revokes it. State-changing requests also need a double-submit CSRF token. Cookies are `Secure` only when `ENVIRONMENT=production`.

### Ownership

Every conversation, message, file, and usage query uses the user id from the session. Another person's row is `404`, the same as a missing row.

### Conversations and messages

A conversation title defaults to `New chat`, is trimmed, and is at most 200 characters. The first user message replaces `New chat` with the first line of that message.

A chat message must contain text. A file does not replace the text. One message can attach at most 4 files owned by that user. The saved message stores the typed text. File excerpts, image bytes, and PDF bytes are added only on the provider turn.

The user row is saved as `complete`. The assistant row starts empty and `incomplete`.

### Model call

`POST /api/v1/chat` checks auth, CSRF, the per-minute chat limit, and the daily token cap before it writes the new rows. The response is server-sent events: `conversation`, `tool`, `delta`, `done`, and `error`.

Text files are copied into the turn and trimmed to `MAX_MESSAGE_CHARS`. Images are sent as image input. PDFs are sent as document bytes. This API does not extract PDF text locally. A missing attachment becomes a short note in the turn, not a 500.

`web_search: true` is stored on the user message. Gemini then gets the Google Search tool, and OpenAI gets the web search tool. Regenerate reuses that flag from the last user message.

Stopping the browser request stores the partial assistant text as `cancelled`. A provider failure keeps the partial text as `incomplete` and sends an `error` event. User-facing provider errors omit the API key and the upstream body.

### Regenerate

`POST /api/v1/chat/regenerate` checks the daily cap before it clears anything. Only the latest message can be regenerated, and it must be an assistant message with a non-blank user turn before it. The same row is cleared and streamed again. The model does not see the replaced text. The previous usage row for that message is deleted so the total is not counted twice.

### Tools

Every request offers `current_time` and `calculate`. The server runs the function. At most 4 model rounds run for one user message.

`current_time` returns UTC as `YYYY-MM-DDTHH:MM:SSZ`. `calculate` accepts numbers, parentheses, and `+ - * /` only, up to 100 characters. Names, `**`, and results whose magnitude exceeds 1e12 are rejected. Unknown functions and bad math return an error string to the model. Finished calls are stored on the assistant message as `tool_calls`.

### Files

`POST /api/v1/files` stores one upload. The extension decides the type, not the client `Content-Type`. Allowed extensions: `txt`, `md`, `csv`, `json`, `pdf`, `png`, `jpg`, `jpeg`, `webp`, `gif`.

An empty file is `422`. A file over `MAX_UPLOAD_BYTES` (default 5 MiB) is `413`. A body over `MAX_BODY_BYTES` (default 6 MiB) is `413` before it is read. Bytes live under `UPLOAD_DIR/<user id>/` with a generated file name. Download and delete are limited to the owner.

### Usage and limits

A finished reply with token counts writes one `usage_events` row. Deleting a conversation keeps that row. Deleting the user removes it. `GET /api/v1/usage` returns the totals, today's tokens, and the daily cap. `cost_usd` is filled only when both USD-per-million prices are set for every provider in the total. Prices are not hardcoded.

`USAGE_LIMIT_TOKENS_PER_DAY` counts tokens since 00:00 UTC. The default is `100000`. `0` turns the cap off. Chat and regenerate return `429 usage_limited` once today's total has already reached the cap. One reply can finish above the cap. The next request is refused.

Login and register are limited per client address, default 20 per minute. Chat is limited per user, default 30 per minute. The window lives in this process. `0` disables that limiter. Over the limit is `429 rate_limited`.

### Out of scope

This service does not pick a model per message, bill a subscription, verify email, or search a private document library. `compose.prod.yaml` in this repository is not started from here.

## Local setup

```bash
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

PostgreSQL starts from this directory:

```bash
docker compose up -d postgres
```

The local database user, password, and database name are all `chatbot`, on port `5432`. PostgreSQL 18 keeps its files under `/var/lib/postgresql`.

Apply migrations, then check the API:

```bash
alembic upgrade head
curl http://127.0.0.1:8000/api/v1/health
```

A healthy database returns `{"status":"ok"}`. `GET /api/v1/health/live` checks the process only. The Bruno collection is `bruno/` in this repository. It does not call `POST /api/v1/chat`.

Put `GEMINI_API_KEY` and `GEMINI_MODEL` in `.env` only. Copy the names from `.env.example`. Get a key from [Google AI Studio](https://aistudio.google.com/apikey). Do not paste the key into chat. `OPENAI_API_KEY` and `OPENAI_MODEL` stay empty unless `AI_PROVIDER=openai`.

## Tests

```bash
pytest
```
