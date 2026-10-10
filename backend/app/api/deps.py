from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from langchain_chroma import Chroma
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User
from app.vectorstore import get_services_store, get_vector_store

DbSession = Annotated[Session, Depends(get_db)]
VectorStore = Annotated[Chroma, Depends(get_vector_store)]
ServicesStore = Annotated[Chroma, Depends(get_services_store)]


def get_current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie(alias=settings.auth_cookie_name)] = None,
) -> User:
    """Accept the JWT as `Authorization: Bearer <token>` (Next.js server) or as the auth cookie."""
    token = access_token
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]

    user_id = decode_access_token(token) if token and settings.jwt_secret_key else None
    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
