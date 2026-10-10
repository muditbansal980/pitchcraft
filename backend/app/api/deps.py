from typing import Annotated

from fastapi import Depends
from langchain_chroma import Chroma
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.vectorstore import get_services_store, get_vector_store

DbSession = Annotated[Session, Depends(get_db)]
VectorStore = Annotated[Chroma, Depends(get_vector_store)]
ServicesStore = Annotated[Chroma, Depends(get_services_store)]
