# Business logic

These are the product rules the code enforces. Defaults below match `backend/app/core/config.py` and `backend/app/core/security.py` unless an environment variable overrides them.

## Product

A signed-in person chats with one configured model. The browser talks only to this API. The API calls Gemini or OpenAI. The transcript stays in PostgreSQL. Each provider request sends `store=false`, so the provider is not the system of record.

`AI_PROVIDER` selects the implementation. The default is `gemini`. An empty API key or an empty model name fails before any network call. The browser never receives either key.

There is no model picker, no subscription, no email verification, and no retrieval over a private document library.

## Accounts

- Email is trimmed and stored in lowercase.
- Password length is 12 to 128 characters. The database stores an Argon2id hash.
- Registering an email that already exists returns `409 email_already_registered`.
- A wrong email and a wrong password both return `401 invalid_credentials` with the same message. A missing user still runs a password check against a dummy hash.
- Register and login create an opaque session token. The `session` cookie is `HttpOnly`. The database stores only its SHA-256 hash.
- A session lasts 7 days from creation. It is not extended on later requests. Logout sets `revoked_at`. A missing, expired, or revoked session is `401 unauthenticated`.
- State-changing cookie requests need a double-submit CSRF token: the `csrf_token` cookie and the `X-CSRF-Token` header must match.
- Cookies are `Secure` only when `ENVIRONMENT=production`. `SameSite` is `Lax`.
- `get_current_user` is the only place a request becomes a user. A later auth provider can replace that seam.
- Admin routes use `get_current_admin`. Access is `users.is_admin` or an email in `ADMIN_EMAILS`. Anyone else receives `403 forbidden`. `GET /api/v1/admin/stats` returns account and usage totals. It does not change chat ownership rules.

## Ownership

Conversations, messages, files, and usage are always read through the user id on the session. Another person's conversation or file is `404`, the same as a missing one.

## Conversations

- A new conversation title defaults to `New chat`. A supplied title is trimmed, must not be blank, and is at most 200 characters.
- The first user message in a new chat, or in a `New chat` that has no user message yet, sets the title from the first line of that message, trimmed to 200 characters.
- Rename and delete apply only to the owner's conversation.
- Deleting a conversation removes its messages. Usage rows stay, with `message_id` set to null, so the token total still counts. Deleting the user removes their usage rows.

## Messages

- A chat message must contain non-blank text. An attachment does not replace the text.
- Stored message text is the typed text, up to `MAX_MESSAGE_CHARS` (default 16000).
- A message can reference at most 4 files the same user already uploaded. Duplicate ids count once. A file the user does not own is `404 file_not_found`.
- The user row is saved as `complete`. The assistant row starts empty and `incomplete`. Its timestamp is one microsecond after the user row.
- The public message endpoint stores `role=user` only. Assistant rows are written by the chat stream.

## Model turn

The provider sees earlier user and assistant turns, not a raw dump of the files table.

- Text files (`txt`, `md`, `csv`, `json`) are copied into the turn, trimmed to `MAX_MESSAGE_CHARS`, with `\n[truncated]` when cut. A missing or empty text file becomes `[Attached file NAME could not be read.]`.
- Images (`png`, `jpg`, `jpeg`, `webp`, `gif`) are sent as image bytes. A missing image becomes a short note in the text.
- PDFs are sent as document bytes. The API does not extract PDF text itself. A missing PDF becomes `[Attached PDF NAME could not be read.]`.
- `web_search: true` is stored on that user message and set on the last turn. Gemini then receives the Google Search tool. OpenAI receives the web search tool. The model decides whether to use it.

## Streaming and regenerate

`POST /api/v1/chat` checks the session, CSRF, the per-user chat rate limit, and the daily token cap before it saves the new rows. The response is `text/event-stream`:

| Event | Meaning |
| --- | --- |
| `conversation` | Conversation id and title |
| `tool` | Function name, arguments, and result |
| `delta` | Next piece of assistant text |
| `done` | Saved assistant message id and status |
| `error` | Stable code and a message that omits the key and the upstream body |

A finished reply is `complete`, or `incomplete` when the model marks it incomplete. A failure mid-stream keeps the partial text as `incomplete` and emits `error`. The browser abort stores the partial text as `cancelled`.

`POST /api/v1/chat/regenerate` checks the daily cap before it changes the row. Only the latest message can be regenerated, and it must be an assistant message with a non-blank user turn before it. Otherwise the API returns `422 not_regenerable`. The same assistant row is cleared and streamed again. The model does not see the replaced reply. Web search is taken from the last user message. The old usage row for that message is deleted, then the new reply writes one row, so the total is not counted twice.

## Tools

Every model request offers two functions. The server runs them. At most 4 model rounds run for one user message.

| Function | Rule |
| --- | --- |
| `current_time` | UTC time as `YYYY-MM-DDTHH:MM:SSZ`. No arguments. |
| `calculate` | One expression: numbers, parentheses, and `+ - * /`. No names and no `**`. Longer than 100 characters, a result whose magnitude exceeds 1e12, or invalid math returns an error string. |

An unknown function returns `That tool is not available.` Invalid arguments return `The arguments were not valid.` These strings go back to the model. They do not become HTTP 500. Finished calls are stored on the assistant message as `tool_calls`.

## Files

`POST /api/v1/files` accepts one multipart file. The extension decides the type. The client `Content-Type` does not.

Allowed: `.txt`, `.md`, `.csv`, `.json`, `.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`.

An empty file is `422`. A file larger than `MAX_UPLOAD_BYTES` (default 5 MiB) is `413`. A request whose `Content-Length` exceeds `MAX_BODY_BYTES` (default 6 MiB) is `413` before the body is read. The disk name is a generated id plus the extension, under `UPLOAD_DIR/<user id>/`. Download and delete are limited to the owner. Delete removes the database row and unlinks the file.

## Usage and limits

A finished reply that reports token counts writes one `usage_events` row: provider, model, input, output, and total. `GET /api/v1/usage` sums that user's rows.

`cost_usd` is a 6-decimal string only when every provider in the total has both an input and an output USD-per-million price set. Prices are not hardcoded. With no rows, `cost_usd` is `0.000000`. Otherwise it is `null`.

`USAGE_LIMIT_TOKENS_PER_DAY` counts tokens from finished replies since 00:00 UTC. The default is 100000. `0` turns the cap off. Chat and regenerate return `429 usage_limited` when today's total is already at the cap. The check happens before the model call, so one reply can finish above the cap. The next one is refused. Yesterday's rows do not count toward today.

Separate from the daily cap, a one-minute window lives in this API process:

- Login and register: `RATE_LIMIT_AUTH_PER_MINUTE` per client address. Default 20.
- Chat and regenerate: `RATE_LIMIT_CHAT_PER_MINUTE` per user id. Default 30.

`0` disables that limiter. A second API process does not share the counts. Exceeding a limit returns `429 rate_limited`.

## Browser behavior

The page at `http://localhost:3000` keeps the session cookie on `localhost`. Opening `127.0.0.1` does not.

- JSON calls use Axios with cookies. The chat stream uses `fetch` and `AbortController`.
- Assistant text renders as Markdown. Fenced code is highlighted. User text stays plain. Raw HTML is not rendered.
- An empty finished assistant message shows `The model did not reply.`
- Search stays on until the person turns it off. A user bubble shows `Web` when that message asked for search.
- Regenerate is offered only on the latest assistant message, including an empty one, and only while nothing is streaming.
- Mic writes speech into the composer with the browser Web Speech API. Speak reads one assistant reply with speech synthesis. Neither uploads audio or calls the model. Send, regenerate, and switching conversation stop both.
- When a daily cap is set, the sidebar shows today's tokens against that cap.

## Failures the person sees

Provider failures use a `gemini_` or `openai_` code for not configured, timeout, rate limit, unavailable, or a general error. The text shown in the product is generic for both providers. Logs record the error type or status, not the response body and not the API key.
