"""
End-to-end smoke test for the roster pipeline.

Runs entirely against an isolated *test* database — never the database the
running app uses for real operational data. It refuses to run at all if
TEST_DATABASE_URL and DATABASE_URL happen to resolve to the same database,
and it creates the test database itself if it doesn't exist yet, so there's
never a reason to point this at production data.
"""
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2
from psycopg2 import sql

PROD_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://ems:ems@localhost:5433/ems_roster"
)
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://ems:ems@localhost:5433/ems_roster_test"
)

if TEST_DATABASE_URL == PROD_DATABASE_URL:
    sys.exit(
        "Refusing to run: TEST_DATABASE_URL is the same as DATABASE_URL. "
        "This test drops and recreates its tables — point it at a separate "
        "database (e.g. ems_roster_test) before running."
    )


def _pg_conn_kwargs(url: str) -> dict:
    # postgresql+psycopg2://user:pass@host:port/dbname
    rest = url.split("://", 1)[1]
    creds, hostpart = rest.split("@", 1)
    user, password = creds.split(":", 1)
    hostport, dbname = hostpart.split("/", 1)
    host, port = hostport.split(":", 1)
    return {"user": user, "password": password, "host": host, "port": port, "dbname": dbname}


def ensure_test_database_exists():
    kwargs = _pg_conn_kwargs(TEST_DATABASE_URL)
    target_db = kwargs.pop("dbname")
    conn = psycopg2.connect(dbname="postgres", **kwargs)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            if not cur.fetchone():
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
                print(f"Created test database '{target_db}'")
    finally:
        conn.close()


ensure_test_database_exists()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from sqlmodel import SQLModel
from app.database import engine
from fastapi.testclient import TestClient
from app.main import app

# start every run from a clean slate — safe, this is the isolated test DB
SQLModel.metadata.drop_all(engine)

client = TestClient(app)
client.__enter__()  # triggers startup event (creates tables)

# bootstrap the first admin user (allowed with no token only while the User table is empty)
client.post("/api/auth/users", json={"username": "admin", "password": "admin-test-pw", "role": "admin"})
login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-test-pw"})
client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
print("Logged in as admin")

# 13 stations, matching the real roster's D1..D13 / N1..N13 codes
stations = []
for i in range(1, 14):
    r = client.post("/api/stations", json={
        "name": f"Station {i}", "code": str(i),
        "day_crew_size": 3, "night_crew_size": 3, "min_drivers_per_crew": 1
    })
    stations.append(r.json())
print(f"Created {len(stations)} stations")

# one ambulance per station — matches the historical 1-ambulance-per-D/N-code setup
ambulances = []
for st in stations:
    r = client.post("/api/ambulances", json={"plate_no": f"AMB-{st['code']}", "station_id": st["id"]})
    ambulances.append(r.json())
print(f"Created {len(ambulances)} ambulances")

random.seed(42)
first_names = ["Jean","Marie","Eric","Alice","Paul","Claudine","Emmanuel","Diane","Vincent","Aline",
               "Patrick","Solange","Innocent","Immaculee","Placide","Christine","Theogene","Bernadette"]
last_names = ["Mugisha","Uwase","Habimana","Mukamana","Nkurunziza","Ingabire","Niyonzima","Uwimana",
              "Bizimana","Mukashema","Ndayisenga","Kayitesi"]

def make_name():
    return f"{random.choice(last_names).upper()} {random.choice(first_names)}"

employees = []
# 32 drivers spread across 13 stations
for i in range(32):
    st = stations[i % 13]
    r = client.post("/api/employees", json={
        "name": make_name(), "role": "driver", "station_id": st["id"],
        "is_driver": True, "is_team_leader": False, "restricted_night_shift": False, "active": True
    })
    employees.append(r.json())

# 116 nurses/paramedics/anesthesiologists spread across 13 stations
roles = ["nurse", "paramedic", "anesthesiologist"]
for i in range(116):
    st = stations[i % 13]
    role = roles[i % 3]
    restricted = (i % 30 == 0)  # a few flagged, simulating policy-restricted staff
    is_pro = (i % 3 == 0)       # enough Pro staff per station to satisfy the >=1-Pro-per-crew rule
    r = client.post("/api/employees", json={
        "name": make_name(), "role": role, "station_id": st["id"],
        "is_driver": False, "is_team_leader": (i % 20 == 0), "restricted_night_shift": restricted,
        "is_pro": is_pro, "active": True
    })
    employees.append(r.json())

print(f"Created {len(employees)} employees "
      f"({sum(1 for e in employees if e['is_driver'])} drivers, "
      f"{sum(1 for e in employees if not e['is_driver'])} medical)")

# a bit of realistic leave for January
emp_ids = [e["id"] for e in employees]
for _ in range(40):
    eid = random.choice(emp_ids)
    day = f"2026-01-{random.randint(1,28):02d}"
    code = random.choice(["AL","SL","TR"])
    client.post("/api/leave", json={"employee_id": eid, "day": day, "code": code})
print("Added leave records")

import time
t0 = time.time()
res = client.post("/api/roster/generate?start_date=2026-01-01&end_date=2026-01-31&range_type=month")
elapsed = time.time() - t0
print(f"Generate status: {res.status_code}, took {elapsed:.1f}s")
print(res.json())

if res.status_code == 200:
    run_id = res.json()["run_id"]
    roster = client.get(f"/api/roster/{run_id}")
    print("Roster rows:", len(roster.json()["rows"]))
    export = client.get(f"/api/roster/{run_id}/export")
    print("Export status:", export.status_code, "bytes:", len(export.content))
    with open("test_export.xlsx", "wb") as f:
        f.write(export.content)
    print("Saved test_export.xlsx")

client.__exit__(None, None, None)
