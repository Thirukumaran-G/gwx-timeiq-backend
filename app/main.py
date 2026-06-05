import os
import uuid
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, select
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Never crash at import time ────────────────────────────────────
DATABASE_URL         = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
REDIS_HOST           = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT           = int(os.environ.get("REDIS_PORT", "6379"))
APP_TITLE            = os.environ.get("APP_TITLE", "timeiq-api")
CORS_ORIGINS         = os.environ.get("CORS_ORIGINS", "*").split(",")
REDIS_SOCKET_TIMEOUT = int(os.environ.get("REDIS_SOCKET_TIMEOUT", "5"))
ITEMS_CACHE_KEY      = os.environ.get("ITEMS_CACHE_KEY", "items:all")
ITEMS_CACHE_TTL      = int(os.environ.get("ITEMS_CACHE_TTL", "60"))

engine       = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
redis_client: aioredis.Redis | None = None

# ── Readiness flag — health returns ok immediately ────────────────
app_ready = False


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id:    Mapped[str] = mapped_column(String(36), primary_key=True,
                                       default=lambda: str(uuid.uuid4()))
    name:  Mapped[str] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, app_ready

    logger.info("Starting up — connecting to DB and Redis...")

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("DB connection established")
    except Exception as e:
        logger.error(f"DB connection failed: {e}")
        # Don't crash — let health endpoint still respond
        # Cloud Run will retry

    try:
        redis_client = aioredis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}",
            decode_responses=True,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
        )
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")

    app_ready = True
    logger.info("App ready")

    yield

    await engine.dispose()
    if redis_client:
        await redis_client.aclose()


app = FastAPI(title=APP_TITLE, lifespan=lifespan, root_path="/api")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ItemIn(BaseModel):
    name:  str
    notes: str | None = None


class ItemOut(BaseModel):
    id:    str
    name:  str
    notes: str | None
    model_config = {"from_attributes": True}


# ── Health returns 200 immediately — does NOT wait for DB ─────────
@app.get("/health")
async def health():
    return {"status": "ok", "ready": app_ready}


@app.get("/ping-redis")
async def ping_redis():
    try:
        pong = await redis_client.ping()
        return {"redis": "ok", "pong": pong}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Redis unreachable: {exc}")


@app.post("/items", response_model=ItemOut, status_code=201)
async def create_item(body: ItemIn):
    async with SessionLocal() as db:
        item = Item(name=body.name, notes=body.notes)
        db.add(item)
        await db.flush()
        await db.refresh(item)
        await db.commit()
        await redis_client.delete(ITEMS_CACHE_KEY)
        return item


@app.get("/items", response_model=list[ItemOut])
async def list_items():
    cached = await redis_client.get(ITEMS_CACHE_KEY)
    if cached:
        return json.loads(cached)
    async with SessionLocal() as db:
        rows = (await db.execute(select(Item))).scalars().all()
        data = [ItemOut.model_validate(r).model_dump() for r in rows]
        await redis_client.set(ITEMS_CACHE_KEY, json.dumps(data), ex=ITEMS_CACHE_TTL)
        return data


@app.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: str):
    async with SessionLocal() as db:
        item = (await db.execute(
            select(Item).where(Item.id == item_id)
        )).scalar_one_or_none()
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        await db.delete(item)
        await db.commit()
        await redis_client.delete(ITEMS_CACHE_KEY)