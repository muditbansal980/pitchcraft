from fastapi import FastAPI
from sqlalchemy import text

from app.api.deps import DbSession
from app.api.routes import brand, plan
from app.core.config import settings

app = FastAPI(title=settings.app_name)
app.include_router(brand.router)
app.include_router(plan.router)


@app.get("/health")
def health(db: DbSession) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
