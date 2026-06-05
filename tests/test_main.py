import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.main import app, Base

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def client(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()
    mock_redis.delete = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    import app.main as m
    m.redis_client = mock_redis
    m.SessionLocal = factory

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ping_redis(client):
    r = await client.get("/ping-redis")
    assert r.status_code == 200
    assert r.json()["redis"] == "ok"


@pytest.mark.asyncio
async def test_create_item(client):
    r = await client.post("/items", json={"name": "test item", "notes": "hello"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "test item"
    assert body["notes"] == "hello"
    assert "id" in body


@pytest.mark.asyncio
async def test_list_items(client):
    r = await client.get("/items")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_delete_item(client):
    r = await client.post("/items", json={"name": "to delete"})
    item_id = r.json()["id"]
    r = await client.delete(f"/items/{item_id}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_delete_not_found(client):
    r = await client.delete("/items/nonexistent-id")
    assert r.status_code == 404