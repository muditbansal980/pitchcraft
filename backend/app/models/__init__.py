# Import every model module here so Alembic autogenerate can see its tables.
from app.db.base import Base
from app.models.user import User

__all__ = ["Base", "User"]
