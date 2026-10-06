import time
from threading import Lock

from app.core.errors import AppError


class RateLimiter:
    """Fixed one-minute window kept in this process.

    A second API process does not share the counts. That is enough for one
    server. A shared store can replace this class later without changing routes.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = Lock()

    def check(self, key: str, limit: int) -> None:
        if limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            recent = [hit for hit in self._hits.get(key, []) if now - hit < 60]
            if len(recent) >= limit:
                raise AppError(
                    code="rate_limited",
                    message="Too many requests. Try again shortly.",
                    status_code=429,
                )
            recent.append(now)
            self._hits[key] = recent
