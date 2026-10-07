"""Create the local virtualenv, database, and API process for `just dev-setup`."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
ENV_PATH = ROOT / ".env"
STOCK_DATABASE_URL = re.compile(
    r"postgresql\+asyncpg://chatbot:chatbot@(localhost|127\.0\.0\.1):\d+/chatbot"
)


@dataclass(frozen=True)
class DatabaseTarget:
    user: str
    password: str
    host: str
    port: int
    database: str


def env_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.*)$", text, re.MULTILINE)
    if match is None:
        return None
    return match.group(1).strip().strip('"').strip("'")


def set_env_value(path: Path, key: str, value: str) -> None:
    text = path.read_text()
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda _match: line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    path.write_text(text)


def parse_database_url(url: str) -> DatabaseTarget:
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = parsed.hostname or "127.0.0.1"
    if host == "localhost":
        host = "127.0.0.1"
    database = parsed.path.lstrip("/") or "chatbot"
    return DatabaseTarget(
        user=parsed.username or "chatbot",
        password=parsed.password or "chatbot",
        host=host,
        port=parsed.port or 5432,
        database=database,
    )


def is_stock_database_url(url: str) -> bool:
    return STOCK_DATABASE_URL.fullmatch(url) is not None


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def db_ready(url: str) -> bool:
    import asyncio

    import asyncpg

    target = parse_database_url(url)

    async def connect() -> None:
        connection = await asyncio.wait_for(
            asyncpg.connect(
                user=target.user,
                password=target.password,
                database=target.database,
                host=target.host,
                port=target.port,
            ),
            timeout=3,
        )
        await connection.close()

    try:
        asyncio.run(connect())
    except (OSError, TimeoutError, asyncpg.PostgresError):
        return False
    return True


def venv_python_ready() -> bool:
    return VENV_PYTHON.is_file() and os.access(VENV_PYTHON, os.X_OK)


def running_in_project_venv() -> bool:
    # Compare prefixes. Resolving the interpreter follows the venv symlink
    # back to the system Python and would skip the re-exec.
    return Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()


def ensure_venv() -> None:
    if venv_python_ready():
        return
    venv_dir = ROOT / ".venv"
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    completed = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=False,
    )
    if completed.returncode != 0:
        shutil.rmtree(venv_dir, ignore_errors=True)
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
            check=True,
        )
        get_pip = subprocess.run(
            ["curl", "-fsSL", "https://bootstrap.pypa.io/get-pip.py"],
            check=True,
            capture_output=True,
        )
        subprocess.run([str(VENV_PYTHON)], input=get_pip.stdout, check=True)
    if not venv_python_ready():
        print("Could not create .venv.", file=sys.stderr)
        raise SystemExit(1)


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    shutil.copyfile(ROOT / ".env.example", ENV_PATH)
    print("Created .env from .env.example.")


def ensure_session_secret() -> None:
    current = env_value(ENV_PATH.read_text(), "SESSION_SECRET") or ""
    if current:
        return
    set_env_value(ENV_PATH, "SESSION_SECRET", secrets.token_urlsafe(48))
    print("Wrote SESSION_SECRET into .env.")


def database_url() -> str:
    url = env_value(ENV_PATH.read_text(), "DATABASE_URL")
    if not url:
        print("DATABASE_URL is missing from .env.", file=sys.stderr)
        raise SystemExit(1)
    return url


def docker_running() -> bool:
    completed = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def our_postgres_publishes(port: int) -> bool:
    completed = subprocess.run(
        ["docker", "compose", "port", "postgres", "5432"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return False
    suffix = f":{port}"
    return any(line.strip().endswith(suffix) for line in completed.stdout.splitlines())


def next_free_port() -> int:
    for port in range(5433, 5500):
        if not port_open(port):
            return port
    print("No free port from 5433 to 5499 for Postgres.", file=sys.stderr)
    raise SystemExit(1)


def start_postgres(port: int) -> None:
    env = os.environ.copy()
    env["POSTGRES_PORT"] = str(port)
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "postgres"],
        cwd=ROOT,
        env=env,
        check=True,
    )


def wait_until_ready(url: str) -> None:
    port = parse_database_url(url).port
    for attempt in range(1, 31):
        if db_ready(url):
            return
        if attempt == 30:
            print(
                f"Postgres did not accept connections on 127.0.0.1:{port}.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        time.sleep(1)


def prepare_database() -> None:
    url = database_url()
    if db_ready(url):
        port = parse_database_url(url).port
        print(f"Database is ready on 127.0.0.1:{port}.")
        return

    port = parse_database_url(url).port
    if not docker_running():
        print(
            f"Docker is not running, and PostgreSQL is not reachable on port {port}.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if port_open(port) and not our_postgres_publishes(port):
        if not is_stock_database_url(url):
            print(
                f"DATABASE_URL is not reachable, and port {port} is already in use.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        busy = port
        port = next_free_port()
        url = f"postgresql+asyncpg://chatbot:chatbot@localhost:{port}/chatbot"
        set_env_value(ENV_PATH, "DATABASE_URL", url)
        print(
            f"Port {busy} is already used by another PostgreSQL. "
            f"This project will use {port}."
        )

    print(f"Starting PostgreSQL on port {port}.")
    set_env_value(ENV_PATH, "POSTGRES_PORT", str(port))
    start_postgres(port)
    wait_until_ready(database_url())


def warn_if_gemini_key_missing() -> None:
    text = ENV_PATH.read_text()
    if re.search(r"^GEMINI_API_KEY=.+$", text, re.MULTILINE):
        return
    print(
        "GEMINI_API_KEY is empty in .env. The API will start. "
        "Chat needs a key from Google AI Studio."
    )


def ensure_api_port_free() -> None:
    if not port_open(8000):
        return
    print(
        "Port 8000 is already in use. Stop the other process, then run just dev-setup again.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def install_project() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
        cwd=ROOT,
        check=True,
    )


def migrate() -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
    )


def serve() -> None:
    os.chdir(ROOT)
    print("API http://127.0.0.1:8000")
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--reload",
            "--port",
            "8000",
        ],
    )


def main() -> None:
    if not running_in_project_venv():
        ensure_venv()
        script = str(Path(__file__).resolve())
        os.execv(VENV_PYTHON, [str(VENV_PYTHON), script, *sys.argv[1:]])

    install_project()
    ensure_env_file()
    ensure_session_secret()
    prepare_database()
    migrate()
    warn_if_gemini_key_missing()
    ensure_api_port_free()
    serve()


if __name__ == "__main__":
    main()
