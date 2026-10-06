import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.ai.provider import ChatDocument, ChatImage, ChatTurn, StreamItem
from tests.test_auth import _csrf, _register
from tests.test_chat_stream import FakeProvider, _postgres_dsn


async def _delete_user(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute("DELETE FROM users WHERE email = $1", email)
    finally:
        await connection.close()


async def _user_parts(conversation_id: str) -> object:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        return await connection.fetchval(
            """
            SELECT content_parts
            FROM messages
            WHERE conversation_id = $1 AND role = 'user'
            """,
            conversation_id,
        )
    finally:
        await connection.close()


@pytest.fixture
def emails() -> Iterator[tuple[str, str]]:
    values = (f"files-{uuid4()}@example.com", f"files-{uuid4()}@example.com")
    yield values
    for value in values:
        asyncio.run(_delete_user(value))


def _app(tmp_path: Path, provider: FakeProvider | None = None):
    settings = get_settings().model_copy(update={"upload_dir": tmp_path})
    return create_app(settings=settings, ai_provider=provider)


def test_upload_list_download_and_delete(
    emails: tuple[str, str], tmp_path: Path
) -> None:
    with TestClient(_app(tmp_path)) as client:
        _register(client, emails[0])
        uploaded = client.post(
            "/api/v1/files",
            files={"file": ("notes.txt", b"hello file", "application/octet-stream")},
            headers={"X-CSRF-Token": _csrf(client)},
        )
        assert uploaded.status_code == 201
        body = uploaded.json()
        assert body["name"] == "notes.txt"
        assert body["media_type"] == "text/plain"
        assert body["size_bytes"] == len(b"hello file")
        file_id = body["id"]

        listed = client.get("/api/v1/files")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == file_id

        metadata = client.get(f"/api/v1/files/{file_id}")
        assert metadata.status_code == 200
        assert metadata.json()["name"] == "notes.txt"

        downloaded = client.get(f"/api/v1/files/{file_id}/content")
        assert downloaded.status_code == 200
        assert downloaded.content == b"hello file"
        assert "notes.txt" in downloaded.headers["content-disposition"]

        removed = client.delete(
            f"/api/v1/files/{file_id}",
            headers={"X-CSRF-Token": _csrf(client)},
        )
        assert removed.status_code == 204
        assert client.get(f"/api/v1/files/{file_id}").status_code == 404

    assert list(tmp_path.rglob("*")) == [] or all(
        path.is_dir() for path in tmp_path.rglob("*")
    )


def test_other_user_cannot_read_a_file(emails: tuple[str, str], tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as owner:
        _register(owner, emails[0])
        uploaded = owner.post(
            "/api/v1/files",
            files={"file": ("secret.txt", b"private", "text/plain")},
            headers={"X-CSRF-Token": _csrf(owner)},
        )
        file_id = uploaded.json()["id"]

    with TestClient(_app(tmp_path)) as stranger:
        _register(stranger, emails[1])
        missing = stranger.get(f"/api/v1/files/{file_id}")
        download = stranger.get(f"/api/v1/files/{file_id}/content")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "file_not_found"
    assert download.status_code == 404


def test_unsupported_and_empty_files_are_rejected(
    emails: tuple[str, str],
    tmp_path: Path,
) -> None:
    with TestClient(_app(tmp_path)) as client:
        _register(client, emails[0])
        headers = {"X-CSRF-Token": _csrf(client)}
        rejected = client.post(
            "/api/v1/files",
            files={"file": ("../run.exe", b"nope", "text/plain")},
            headers=headers,
        )
        empty = client.post(
            "/api/v1/files",
            files={"file": ("empty.txt", b"", "text/plain")},
            headers=headers,
        )

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "unsupported_file"
    assert empty.status_code == 422


def test_chat_sends_text_file_contents_to_the_model(
    emails: tuple[str, str],
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [
            StreamItem(type="delta", text="ok"),
            StreamItem(type="end", status="complete", usage=None),
        ]
    )
    with TestClient(_app(tmp_path, provider)) as client:
        _register(client, emails[0])
        headers = {"X-CSRF-Token": _csrf(client)}
        uploaded = client.post(
            "/api/v1/files",
            files={"file": ("notes.txt", b"hello file", "text/plain")},
            headers=headers,
        )
        file_id = uploaded.json()["id"]
        response = client.post(
            "/api/v1/chat",
            json={"content": "See the file", "file_ids": [file_id]},
            headers=headers,
        )
        conversation_id = response.text.split('"id":"')[1].split('"')[0]
        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")

    assert response.status_code == 200
    assert provider.turns == [
        ChatTurn(
            role="user",
            content="See the file\n\n[Attached file notes.txt]\nhello file",
        )
    ]
    user_message = messages.json()[0]
    assert user_message["files"] == [
        {
            "id": file_id,
            "name": "notes.txt",
            "media_type": "text/plain",
            "size_bytes": len(b"hello file"),
        }
    ]
    stored = asyncio.run(_user_parts(conversation_id))
    if isinstance(stored, str):
        import json

        stored = json.loads(stored)
    assert stored[0]["file_id"] == file_id
    assert user_message["content"] == "See the file"
    assert "hello file" not in response.text


def test_chat_sends_image_bytes_to_the_model(
    emails: tuple[str, str],
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [
            StreamItem(type="delta", text="ok"),
            StreamItem(type="end", status="complete", usage=None),
        ]
    )
    with TestClient(_app(tmp_path, provider)) as client:
        _register(client, emails[0])
        headers = {"X-CSRF-Token": _csrf(client)}
        uploaded = client.post(
            "/api/v1/files",
            files={"file": ("pic.png", b"png-bytes", "image/png")},
            headers=headers,
        )
        response = client.post(
            "/api/v1/chat",
            json={"content": "See the picture", "file_ids": [uploaded.json()["id"]]},
            headers=headers,
        )

    assert response.status_code == 200
    assert provider.turns is not None
    assert provider.turns[0].content == "See the picture"
    assert provider.turns[0].images == [
        ChatImage(mime_type="image/png", data=b"png-bytes")
    ]


def test_chat_sends_pdf_bytes_to_the_model(
    emails: tuple[str, str],
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [
            StreamItem(type="delta", text="ok"),
            StreamItem(type="end", status="complete", usage=None),
        ]
    )
    with TestClient(_app(tmp_path, provider)) as client:
        _register(client, emails[0])
        headers = {"X-CSRF-Token": _csrf(client)}
        uploaded = client.post(
            "/api/v1/files",
            files={"file": ("notes.pdf", b"%PDF-1.4", "application/octet-stream")},
            headers=headers,
        )
        response = client.post(
            "/api/v1/chat",
            json={"content": "See the PDF", "file_ids": [uploaded.json()["id"]]},
            headers=headers,
        )

    assert response.status_code == 200
    assert provider.turns is not None
    assert provider.turns[0].content == "See the PDF"
    assert provider.turns[0].documents == [
        ChatDocument(name="notes.pdf", mime_type="application/pdf", data=b"%PDF-1.4")
    ]
    assert "%PDF-1.4" not in response.text
