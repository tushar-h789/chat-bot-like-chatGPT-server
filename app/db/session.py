from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_CONNECT_TIMEOUT_SECONDS = 5


def create_engine_and_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory
