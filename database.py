import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

default_leads_db_path = os.getenv("LEADS_DB_PATH", "leads.sqlite")
default_database_url = f"sqlite:///{default_leads_db_path}"
DATABASE_URL = os.getenv("DATABASE_URL", default_database_url)

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
