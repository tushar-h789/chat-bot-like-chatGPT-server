# AI Chatbot

A ChatGPT-style chat product. The browser talks only to this API. The API calls Gemini by default, or OpenAI when configured, and stores the transcript in PostgreSQL. The model provider is not the system of record.

The browser app is a separate repository: [chat-bot-like-chatGPT-clinet](https://github.com/tushar-h789/chat-bot-like-chatGPT-clinet). The two apps deploy separately and share an HTTP contract only.

## What is included

A signed-in person can:

- Register and sign in. Passwords are hashed with Argon2id. The session is an opaque HttpOnly cookie that lasts 7 days. There is no email verification.
- Create, rename, and delete conversations. Messages stay after a refresh.
- Send a message and see the reply stream in. Stop aborts the request and keeps the partial reply.
- Regenerate the latest assistant reply in place.
- Attach up to four files: text (`txt`, `md`, `csv`, `json`), images (`png`, `jpg`, `jpeg`, `webp`, `gif`), and PDF. Text is included in the model turn, images are sent as image input, and PDFs are sent as document bytes.
- Turn on web search for a message. The choice is saved and reused when that reply is regenerated.
- See when the model calls `current_time` or `calculate`. The server runs those two functions. At most four model rounds run for one message.
- Use the microphone to fill the composer, and hear one assistant reply read aloud. Both stay in the browser. No audio is uploaded.
- See today's token use against a daily cap. The default is 100,000 tokens since 00:00 UTC. Set the cap to `0` to turn it off.

Assistant replies render as Markdown, with highlighted code. User messages stay plain text. The page does not render raw HTML from the model.

## What is not included

- A model picker
- Subscriptions or billing
- Email verification
- Search over a private document library
- A deployment. `compose.prod.yaml` and CI exist in each app. Nothing has been deployed from them.

## Stack

| Part | Choice |
| --- | --- |
| Web | Next.js 16, React 19, TypeScript, Tailwind CSS 4 |
| Web data | TanStack Query, Zustand, Axios for JSON, `fetch` for the chat stream |
| API | FastAPI, Pydantic v2, SQLAlchemy 2, Alembic |
| Database | PostgreSQL 18 |
| Models | Gemini Interactions API by default (`gemini-3.5-flash-lite`). OpenAI Responses API when `AI_PROVIDER=openai` |
| Tests | pytest in this repository. Vitest in the web repository. Tests use a fake model client. |

## Layout

This repository is the API.

| Path | Purpose |
| --- | --- |
| [README.md](../README.md) | How to run the API and the rules it enforces |
| [docs/business-logic.md](business-logic.md) | Product rules the code enforces |
| [docs/architecture.md](architecture.md) | Technical decisions |
| [docs/api.md](api.md) | HTTP contract |
| [bruno/](../bruno) | Request collection. It does not call the chat stream |
| [compose.yaml](../compose.yaml) | Local PostgreSQL |
| [compose.prod.yaml](../compose.prod.yaml) | Production shape for Postgres and the API. Not started from the app |
| [.github/workflows/ci.yml](../.github/workflows/ci.yml) | API tests |

The web repository has its own README, production Compose file, and CI workflow.

## Run locally

Use `http://localhost:3000` for the web app, not `127.0.0.1`, so the session cookie stays on `localhost`.

1. Start PostgreSQL from this repository:

```bash
docker compose up -d postgres
```

Local database user, password, and database name are all `chatbot`, on port `5432`. That account is for local development only.

2. Start the API:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Put `GEMINI_API_KEY` and `GEMINI_MODEL` in `.env` only. Do not commit that file. If `python3 -m venv` cannot create a virtualenv, follow the bootstrap note in [README.md](../README.md).

3. Start the web app from the [frontend repository](https://github.com/tushar-h789/chat-bot-like-chatGPT-clinet):

```bash
npm install
cp .env.example .env.local
npm run dev
```

`.env.local` should set `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`.

A healthy database check is `GET http://127.0.0.1:8000/api/v1/health`, which returns `{"status":"ok"}`.

## Configuration

Settings live in `.env`. Names and defaults are in `.env.example`.

| Setting | Role |
| --- | --- |
| `AI_PROVIDER` | `gemini` (default) or `openai` |
| `GEMINI_MODEL` | Suggested value: `gemini-3.5-flash-lite` |
| `MAX_MESSAGE_CHARS` | Typed message length. Default 16000 |
| `MAX_UPLOAD_BYTES` | One file. Default 5 MiB |
| `USAGE_LIMIT_TOKENS_PER_DAY` | Daily token cap. Default 100000. `0` disables it |
| `RATE_LIMIT_AUTH_PER_MINUTE` | Login and register per address. Default 20 |
| `RATE_LIMIT_CHAT_PER_MINUTE` | Chat per user. Default 30 |

Dollar cost stays empty until both input and output USD-per-million prices are set for the provider in use. Prices are not hardcoded.

## Tests

```bash
pytest
```

Web tests run in the frontend repository with `npm test`.

## Further reading

- [README.md](../README.md) — how to run the API and the rules it enforces
- [business-logic.md](business-logic.md) — product rules
- [api.md](api.md) — HTTP contract
- [Frontend repository](https://github.com/tushar-h789/chat-bot-like-chatGPT-clinet) — how the page behaves
