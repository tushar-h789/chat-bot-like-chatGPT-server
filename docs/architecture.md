# Architecture decisions

Approved for implementation. Later phases follow this document unless we explicitly change a decision.

## Applications

This repository is the API. The browser app is a separate repository. They share an HTTP contract only.

## Frontend

- Next.js App Router, TypeScript strict, Tailwind CSS
- JSON calls use Axios
- The chat stream uses `fetch`, because the request must be cancellable
- TanStack Query owns server state
- Zustand owns interface state such as the sidebar and the composer
- The interface is custom Tailwind. It does not use shadcn/ui
- Assistant messages render as Markdown. Fenced code uses Shiki. User messages stay plain text. Raw HTML is not rendered.

## Backend

- FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic
- Routes stay thin
- Services own business rules
- The chat service depends on an AI provider interface
- `GeminiProvider` calls the Gemini Interactions API. `OpenAIProvider` calls `responses.create`
- `AI_PROVIDER` selects the implementation. The matching model setting is `GEMINI_MODEL` or `OPENAI_MODEL`

## Data

PostgreSQL 18. UUID primary keys. Timestamps are timezone-aware.

Tables: users, sessions, conversations, messages, files, and usage_events. Tool calls are stored on the assistant message `metadata`, not in their own table.

Message rows keep `content` for text, `status` for stream completion, and nullable JSON columns (`content_parts`, `metadata`) so later multimodal content does not require a new table.

Queries are always scoped to the user id taken from the server session.

## Authentication

Opaque session token in an `HttpOnly` cookie. The database stores only a hash. Passwords use Argon2id.

Defaults until changed:

- No email verification in the MVP
- Minimum password length 12
- Session lifetime 7 days
- State-changing cookie requests require a double-submit CSRF token

`get_current_user` is the seam a future auth provider can replace.

Phase 3 routes are `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`, and `GET /api/v1/auth/csrf`. The request and response bodies are in [api.md](api.md).

Phase 4 routes are `POST/GET /api/v1/conversations`, `GET/PATCH/DELETE /api/v1/conversations/{id}`, and `POST/GET /api/v1/conversations/{id}/messages`. The public message route stores `role=user` only. A missing conversation and another user's conversation both return `404`.

## Model provider

`AIProvider` is the seam `POST /api/v1/chat` calls. `AI_PROVIDER` selects the implementation. `gemini` is the default and uses `GeminiProvider` with the Gemini Interactions API. `openai` uses `OpenAIProvider` and `responses.create`. Both stream through the same `StreamItem` events.

The model value comes from `GEMINI_MODEL` or `OPENAI_MODEL`, matching the selected provider. An empty key or model fails before any network call. Each request sends `store=false`, so the transcript stays in our database. The timeout defaults to 60 seconds.

When the response includes usage, the provider keeps `input_tokens`, `output_tokens`, and `total_tokens`. Failures use a `gemini_` or `openai_` code for not configured, timeout, rate limit, unavailable, or a general error. The messages shown to the client are the same for both providers. Those messages omit the API key and the upstream body. Logs record a status code or response error code only. Tests inject a fake client and do not call the live API.

Every model request includes two functions, `current_time` and `calculate`. The model decides whether to call one. The server runs it and sends the result back before the reply finishes. At most four model rounds run for one user message. Unknown functions and invalid math return an error string to the model. The calls are stored on the assistant message.

## Streaming

`POST /api/v1/chat` returns `text/event-stream`. Auth and CSRF run first, so a rejected request is JSON (`401`, `403`, `404`, or `422`) rather than an event stream.

Events: `conversation` (id and title), `tool` (function name, arguments, and result), `delta` (incremental assistant text), `done` (saved message id and status), `error` (stable code and safe message).

The user message is saved as `complete`. The assistant row starts `incomplete` and is finished when the stream ends. `POST /api/v1/chat/regenerate` clears the latest assistant row and streams a new reply into that same row. The model sees the earlier text turns, not the replaced reply. A successful stream stores `complete`, or `incomplete` when the model marks the response incomplete. A model failure mid-stream keeps the partial text as `incomplete` and sends an `error` event. Stopping generation aborts the browser request. The API cancels the provider stream and stores the partial assistant message with status `cancelled`.

The browser uses `fetch` and `AbortController` for this endpoint. Axios stays on the JSON routes.

Voice stays in the browser. Mic writes speech into the composer with the Web Speech API. Speak reads one assistant reply with speech synthesis. No audio is uploaded, and neither action calls the model.

Uploaded files live in the `files` table and under `UPLOAD_DIR`, one directory per user. A user message can store up to four file references in `content_parts`. Text files are copied into the provider turn. Images are sent as image input. PDFs are sent as document input. A message can ask for web search. Gemini then receives the Google Search tool, and OpenAI receives the web search tool. The model decides whether to call it.

## Production hardening

Login and register are limited per client address. Chat is limited per signed-in user. Both windows are one minute and live in the API process. `GET /api/v1/health/live` checks the process. `GET /api/v1/health` still checks PostgreSQL.

Responses send `nosniff`, `DENY` framing, `no-referrer`, a locked-down permissions policy, and `default-src 'none'`. `Strict-Transport-Security` is added only when `ENVIRONMENT=production`. Request logs record method, path, status, and duration. They do not record bodies. A `Content-Length` above `MAX_BODY_BYTES` is `413` before the body is read.

Finished replies with token counts are stored in `usage_events`, indexed by `(user_id, created_at)`. `GET /api/v1/usage` returns the totals. `USAGE_LIMIT_TOKENS_PER_DAY` caps tokens from finished replies since 00:00 UTC. The default is 100000. `0` turns the cap off. Chat and regenerate are refused once today's total has reached it. Dollar cost is calculated only from optional per-provider prices. Existing conversation and message indexes stay as they are. `compose.prod.yaml` and `.github/workflows/ci.yml` in this repository prepare the API. They do not deploy. The web app has its own workflow in its repository.

## Local database

`compose.yaml` in this repository runs `postgres:18`. The data volume is mounted at `/var/lib/postgresql`, which is the path PostgreSQL 18 expects. Credentials in that file are for local development only.
