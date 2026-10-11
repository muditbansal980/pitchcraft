from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.deps import DbSession
from app.api.routes import auth, brand, canva, deck, plan
from app.core.config import settings

app = FastAPI(title=settings.app_name)
app.include_router(auth.router)
app.include_router(brand.router)
app.include_router(plan.router)
app.include_router(deck.router)
app.include_router(canva.router)

# Deck images (refs, memes, AI mocks) saved by app.services.media.
Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")


@app.get("/health")
def health(db: DbSession) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
