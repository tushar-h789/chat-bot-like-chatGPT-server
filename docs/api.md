# API

Base path: `/api/v1`

The browser sends cookies. It does not send the session token in `Authorization`. Bruno does the same: the cookie jar stores `session` and `csrf_token`.

## Errors

```json
{
  "error": {
    "code": "invalid_credentials",
    "message": "Invalid email or password."
  }
}
```

| Code | Status | When |
| --- | --- | --- |
| `validation_error` | 422 | Body failed validation. The response does not echo the password. |
| `csrf_failed` | 403 | `X-CSRF-Token` does not match the `csrf_token` cookie. |
| `email_already_registered` | 409 | Register used an email that already exists. |
| `invalid_credentials` | 401 | Login email or password is wrong. Both cases use this body. |
| `unauthenticated` | 401 | Session cookie is missing, expired, or revoked. |
| `not_found` | 404 | Conversation is missing, or it belongs to someone else. |
| `database_unavailable` | 503 | Health check could not query PostgreSQL. |
| `rate_limited` | 429 | Too many login, register, or chat requests in one minute. |
| `usage_limited` | 429 | Today's finished replies already used `USAGE_LIMIT_TOKENS_PER_DAY` tokens. |
| `payload_too_large` | 413 | The request body is larger than `MAX_BODY_BYTES`, or one file is larger than `MAX_UPLOAD_BYTES`. |
| `unsupported_file` | 422 | The upload extension is not in the allowlist. |
| `file_not_found` | 404 | The file is missing, or it belongs to someone else. |
| `not_regenerable` | 422 | The reply is not the latest message, or it has no user turn before it. |
| `internal_error` | 500 | Unexpected server failure. |

## Cookies

| Cookie | HttpOnly | Purpose |
| --- | --- | --- |
| `session` | yes | Current login. The database stores only a SHA-256 hash. |
| `csrf_token` | no | Double-submit token for POST requests. |

In local development the cookies are not `Secure`. Production sets `Secure`. `SameSite` is `Lax`. A session lasts 7 days.

## GET /api/v1/health/live

Response 200 when the process is up. It does not query PostgreSQL.

```json
{ "status": "ok" }
```

## GET /api/v1/health

Response 200:

```json
{ "status": "ok" }
```

## GET /api/v1/auth/csrf

Response 200:

```json
{ "csrf_token": "url-safe-random-token" }
```

Call this before register, login, or logout. Send the token back as `X-CSRF-Token`.

## POST /api/v1/auth/register

Creates the user, stores an Argon2id password hash, and sets the session cookie.

Body:

```json
{
  "email": "ada@example.com",
  "password": "correct-horse-battery"
}
```

Password length is 12 to 128 characters. Email is stored in lowercase.

Response 201:

```json
{
  "id": "6f1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a11",
  "email": "ada@example.com",
  "created_at": "2026-10-06T15:40:00.000000Z"
}
```

## POST /api/v1/auth/login

Body: same shape as register.

Response 200: same user shape as register, plus a new session cookie.

## POST /api/v1/auth/logout

Requires the session cookie and the CSRF header. No body.

Response 204 with an empty body. The session row is revoked and the session cookie is cleared.

## GET /api/v1/auth/me

Requires the session cookie.

Response 200: the same user shape as register.

## POST /api/v1/conversations

Requires the session cookie and `X-CSRF-Token`.

Body. `title` may be omitted:

```json
{ "title": "Phase 4 notes" }
```

An omitted title is stored as `New chat`. A title that is blank after trimming is `422`. The stored title is at most 200 characters.

Response 201:

```json
{
  "id": "6f1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a11",
  "title": "Phase 4 notes",
  "created_at": "2026-10-06T15:40:00.000000Z",
  "updated_at": "2026-10-06T15:40:00.000000Z"
}
```

## GET /api/v1/conversations

Requires the session cookie. Returns only the signed-in user's conversations, newest `updated_at` first.

Response 200: an array of the conversation object above.

## GET /api/v1/conversations/{id}

Requires the session cookie. Response 200 is one conversation object.

Missing and other users' ids both return `404` `not_found`.

## PATCH /api/v1/conversations/{id}

Requires the session cookie and `X-CSRF-Token`.

```json
{ "title": "Renamed in Bruno" }
```

Response 200 is the conversation object. This also updates `updated_at`.

## DELETE /api/v1/conversations/{id}

Requires the session cookie and `X-CSRF-Token`. Response 204 with an empty body. Messages in that conversation are deleted with it.

## POST /api/v1/conversations/{id}/messages

Requires the session cookie and `X-CSRF-Token`. Stores a user message and bumps the conversation's `updated_at`.

```json
{ "content": "Hello from Bruno" }
```

The server sets `role` to `user` and `status` to `complete`. A `role` sent by the client is ignored. Blank content is `422`. The default maximum length is 16000 characters (`MAX_MESSAGE_CHARS`).

Response 201:

```json
{
  "id": "8a1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a22",
  "conversation_id": "6f1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a11",
  "role": "user",
  "content": "Hello from Bruno",
  "status": "complete",
  "created_at": "2026-10-06T15:41:00.000000Z",
  "updated_at": "2026-10-06T15:41:00.000000Z"
}
```

This route stores a user message only. Assistant messages are written by `POST /api/v1/chat`. Message responses include `files`, `web_search`, and `tool_calls`. Each is empty or false when the message did not use that feature. `tool_calls` lists functions the model ran, with the arguments and the result.

## Files

Uploads are stored on disk under `UPLOAD_DIR` and owned by the signed-in user. The saved name is generated. The original name is only a display label. Allowed extensions: `txt`, `md`, `csv`, `json`, `pdf`, `png`, `jpg`, `jpeg`, `webp`, `gif`. The declared content type is ignored. A message can reference at most 4 files. Text files (`txt`, `md`, `csv`, `json`) are included in the model turn, trimmed to `MAX_MESSAGE_CHARS`. Images (`png`, `jpg`, `jpeg`, `webp`, `gif`) are sent as image input. PDFs are sent as document input. The saved message text stays the typed text.

### POST /api/v1/files

Requires the session cookie and `X-CSRF-Token`. Multipart field name: `file`. Response 201:

```json
{
  "id": "1b2c3d4e-8a0d-4f0a-9d4a-1e6d0c2b9a33",
  "name": "notes.txt",
  "media_type": "text/plain",
  "size_bytes": 10,
  "created_at": "2026-10-07T00:50:00.000000Z"
}
```

### GET /api/v1/files

Requires the session cookie. Response 200 is a list of the same object, newest first.

### GET /api/v1/files/{id}

Requires the session cookie. Response 200 is one file object.

### GET /api/v1/files/{id}/content

Requires the session cookie. Response 200 is the file bytes, with `Content-Disposition` set to the display name.

### DELETE /api/v1/files/{id}

Requires the session cookie and `X-CSRF-Token`. Response 204. The disk file is removed.

## POST /api/v1/chat

Requires the session cookie and `X-CSRF-Token`. A missing session is `401` before the CSRF check. Another user's conversation is `404`.

```json
{ "content": "Hi there", "conversation_id": null, "file_ids": [], "web_search": false }
```

`conversation_id` is optional. Omit it, or send `null`, to start a new conversation. `file_ids` is optional. Each id must belong to the signed-in user. The ids are stored on the user message. Text from those files is added to the model turn. Image bytes are sent as image input. PDF bytes are sent as document input. `web_search` is optional. When true, Gemini may use Google Search and OpenAI may use web search for that reply. The choice is stored on the user message and reused by regenerate. The title is the first line of the message, trimmed to 200 characters. Sending into a conversation still titled `New chat` with no earlier user message renames it the same way.

A valid request returns `200` with `Content-Type: text/event-stream`. Each event is `event: <name>` then `data: <json>` and a blank line.

```text
event: conversation
data: {"id":"6f1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a11","title":"Hi there"}

event: tool
data: {"name":"calculate","arguments":{"expression":"2+2"},"result":"4"}

event: delta
data: {"text":"Hello"}

event: done
data: {"message_id":"8a1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a22","status":"complete"}
```

`tool` is sent when the model calls `current_time` or `calculate`. The server runs the function. `delta` repeats as text arrives. `done.status` is `complete` or `incomplete`. If the model fails after some text, the stream ends with:

```text
event: error
data: {"code":"openai_timeout","message":"The model timed out."}
```

The partial assistant message stays in the conversation with status `incomplete`. If the browser aborts the request, the API cancels the model stream and saves status `cancelled`. It does not send `done` after a disconnect.

Validation, auth, and ownership failures are normal JSON error bodies, not SSE. The Bruno collection does not call this route, because a request with a configured key would spend API credit. Pytest covers it with a fake provider.

## POST /api/v1/chat/regenerate

Requires the session cookie and `X-CSRF-Token`. Same rate limit and event stream as `POST /api/v1/chat`.

```json
{
  "conversation_id": "6f1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a11",
  "message_id": "8a1c1c4e-8a0d-4f0a-9d4a-1e6d0c2b9a22"
}
```

`message_id` is the assistant reply to replace. It must be the latest message in that conversation, and a user message must come before it. The same row is cleared and streamed again. Earlier turns stay. The old reply text is not sent to the model. Attached text, images, and PDFs are included again. The previous token row for that reply is replaced by the new one.

Another user's conversation or a missing message is `404`. A reply that is not last is `422` `not_regenerable`.

## GET /api/v1/conversations/{id}/messages

Requires the session cookie. Response 200 is an array of message objects, oldest first.

## GET /api/v1/usage

Requires the session cookie. Totals come from finished model replies for the signed-in user. Deleting a conversation does not remove those totals. Deleting the user does.

`cost_usd` is a decimal string when both the input and output price for every provider in the total are set (`GEMINI_INPUT_USD_PER_MILLION` and the matching output price, or the OpenAI pair). Otherwise it is `null`. An account with no replies returns `0.000000`. Prices are not built in.

`tokens_today` counts tokens from finished replies since 00:00 UTC. `daily_token_limit` is `USAGE_LIMIT_TOKENS_PER_DAY`, or `null` when that value is `0`. A chat or regenerate request returns `429` `usage_limited` when `tokens_today` is already at the cap. The reply that crosses the cap is kept. The next one is refused. One reply can finish above the cap.

```json
{
  "input_tokens": 2,
  "output_tokens": 3,
  "total_tokens": 5,
  "replies": 1,
  "cost_usd": null,
  "tokens_today": 5,
  "daily_token_limit": 100000
}
```

## Bruno

Collection folder: `bruno/` in this repository.

The top-right environment menu has **Local** and **Production**. Both define `BASE_URL`, `PASSWORD`, `EMAIL`, `CSRF_TOKEN`, `USER_ID`, `CONVERSATION_ID`, and `MESSAGE_ID`.

Local points at `http://127.0.0.1:8000`. Production's `BASE_URL` is the placeholder `https://api.example.com` until a real host exists. Do not put a real password in Production.

Folders follow the public/private split:

- `Public/Health`
- `Public/Auth` for CSRF, register, login, and the failure requests
- `Private/Conversations` for create, list, rename, messages, and delete
- `Private/Auth` for the current user and logout

From the collection directory:

```bash
bru run --env Local
```
