"""
MySQL database setup. Connection string comes from the DATABASE_URL
environment variable (see .env.example) so the same code runs against the
local docker-compose MySQL and a real deployed instance without changes.
"""
import os
from dotenv import load_dotenv
from sqlmodel import SQLModel, create_engine, Session

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://ems:ems@localhost:3307/ems_roster",
)

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
