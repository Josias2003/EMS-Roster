# SAMU Fair Rostering System

A roster generator and decision-support platform for RBC SAMU — replaces
manual monthly scheduling with a mathematically fair, constraint-respecting
engine, served as a multi-user web app with role-based logins, ambulance-level
crew assignment, manual overrides with fairness previews, a submit → review →
publish workflow, notifications, and event-driven replanning.

## Roles

Four roles log in independently, each with its own permissions:

- **Administrator** — manages stations, employees, users, and ambulances;
  creates events; reviews and publishes rosters.
- **Drivers Manager** — assigns ambulances to stations, generates rosters,
  overrides driver assignments, participates in event planning.
- **PHTL** (Pre-Hospital Team Leader) — generates the medical roster, sets
  crew leaders (must be Pro-rated staff), overrides medical assignments,
  participates in event planning.
- **Call Center Team Leader** — generates/views rosters; events never affect
  Call Center staff (event replanning only draws from driver/medical pools).

## How the workflow fits together

1. **Admin sets up the org**: stations, employees (with skill rating and
   `is_pro` flag), ambulances assigned to stations, known leave.
2. **A manager generates a roster** (`Generate Roster`), choosing a **Day,
   Week, or Month** range around a reference date — not just full calendar
   months. The CP-SAT solver assigns medical staff to ambulance crews (every
   crew gets at least one Pro member, and crew size itself is a heavily
   weighted soft target) and a fair-rotation pass assigns drivers. **A
   staffing shortage never blocks generation** — the solver always produces
   the best roster it can with who's actually available, and reports exactly
   where it came up short (which shift, which station, how many missing) and
   who ended up overworked as a result (e.g. "NDAYISENGA Alice is
   overworking: 276h this roster — nurse average: 220.6h"), instead of an
   opaque solver error. You get back an overall/fairness/coverage/overtime
   quality score alongside those flags.
3. **Review the crew rollup** (`Crew Rollup` tab): station → ambulance →
   crew, for any day in the run. Click a name to set them as crew leader
   (Pro-rated members only). Click **⇄** next to anyone to search eligible
   replacements — each candidate shows their current shift load and why
   they are/aren't eligible, with the swap committed atomically across both
   the crew record and the day-grid.
4. **Submit → validate → publish**: the generating manager submits the draft;
   the Administrator runs a global validation pass (double-bookings, rest-rule
   violations, missing-Pro crews) and publishes. Publishing (and any later
   override to an already-published roster) notifies only the affected
   employees — in-app, and by email with their personal timetable attached
   if their email and SMTP are configured.
5. **Same-day sick call**: from the **Employees** tab, click **Mark
   unavailable today** on the person — it finds their crew slot for today
   automatically and takes you straight to the replacement-candidate search,
   no need to go hunting through Crew Rollup for the right ambulance/shift.
6. **Events**: the Administrator creates an event (type, date, location,
   priority, required ambulances/medical/drivers). Drivers Manager or PHTL
   click **Replan for this event** against a generated roster run — it fills
   the requirement in priority order (ordinary days-off first, then
   reassigning someone off an existing shift, and only as a last resort
   pulling someone off a mandated post-night rest day), and reports exactly
   who/what was disrupted.

## How medical staff and drivers are scheduled

- **Nurses, paramedics, anesthesiologists** are optimized exactly with a
  constraint solver (CP-SAT) — provably fair and provably correct for the
  rules you've configured, including the Pro/Junior crew-mix rule. They can
  be assigned to *any* active ambulance system-wide, not just their home
  station's — real staff do float between stations — but a strong soft
  preference keeps each person at their home station by default, only
  sending them elsewhere once their own station's needs are already covered
  and another station is short. Mark an employee **Floats freely** (on the
  Employees tab) to exempt them from that home-station preference entirely.
  This floating search space is much larger than the old fixed-station
  model, so generation can take several minutes at real scale (confirmed:
  ~190s for 104 staff across 15 stations) — that's expected, not a hang.
- **Drivers** float across stations and are assigned per-ambulance-per-shift
  by a fast fair-rotation algorithm rather than the solver — with dozens of
  interchangeable drivers, the solver spends its time proving optimality
  instead of finding a good answer. If there genuinely aren't enough drivers
  to cover every ambulance every shift while respecting rest rules, the app
  reports exactly where and when the gap is rather than silently overworking
  anyone.

**Known finding from testing with real numbers:** with 32 drivers, 13
stations (1 ambulance each), and a driver required on every day *and* night
shift, the math shows real shortfall — roughly 1 driver short per day on
average, because the mandatory rest-day-after-a-night-shift rule consumes
more driver-days than 32 people can sustainably provide across 13 ambulances
every day of the month. This isn't a bug — it's the same kind of finding
that would justify a staffing request. Check the coverage gap count and the
quality score's coverage component after generating; a low number is real
signal, not a scheduling mistake.

## Setup (one-time)

You need **Python 3.11+**, **Docker Desktop** (for PostgreSQL), and this repo.

1. **Start PostgreSQL** (via Docker):
   ```
   docker compose up -d db
   ```
   This runs Postgres on **port 5433** on your machine (not 5432 — that's
   often already taken by a native Postgres install, so this project maps
   around it). Data persists in a Docker volume between restarts.

2. **Create a Python virtual environment and install dependencies:**
   ```
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   source .venv/bin/activate     # macOS/Linux
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**
   ```
   copy .env.example .env        # Windows
   cp .env.example .env          # macOS/Linux
   ```
   The defaults match the Docker Postgres from step 1 and are fine for local
   use. Generate a real `JWT_SECRET` before this is reachable by anyone
   outside your own machine:
   ```
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   Email notifications are optional — without `SMTP_HOST` set, the app just
   records in-app notifications and skips email silently. To enable it, set
   in `.env`: `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`,
   `SMTP_PASSWORD`, `SMTP_FROM`.

4. **Apply the database schema:**
   ```
   alembic upgrade head
   ```

## Running it

```
uvicorn app.main:app --reload --port 8000
```

Then open **http://127.0.0.1:8000** in your browser.

### First login

The app has no users yet on a fresh database. The **first** user must be
created as an admin, and is the only user creation allowed without already
being logged in — every user after that must be created by an admin from
the **Users** tab. Create the first admin with:

```
curl -X POST http://127.0.0.1:8000/api/auth/users ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"admin\",\"password\":\"choose-a-real-password\",\"role\":\"admin\"}"
```

(macOS/Linux: same command with `\` line continuations and single quotes
instead of `^` and escaped quotes.)

Then sign in at the app with that username/password. From the **Users** tab,
create logins for your Call Center Team Leader, Drivers Manager, and PHTL
staff (`role` = `call_center_tl`, `drivers_manager`, or `phtl`).

## Suggested first run (as admin)

1. Go to **Stations** — add your real stations with their numeric codes
   (matching your D1–D13 / N1–N13 system) and crew sizes.
2. Go to **Employees** — add your drivers and medical staff, each assigned to
   a home station, with an email if you want them notified directly. Mark
   experienced staff as **Pro** — every crew needs at least one, and only a
   Pro can be picked as crew leader.
3. Go to **Ambulances** — add your fleet, assigned to stations. A station
   with no active ambulance gets zero crew — the quality score flags this.
4. Go to **Leave** — enter known annual leave, sick leave, training, etc. for
   the month you're scheduling.
5. Go to **Generate Roster** — pick the year/month and generate. Check the
   quality score and any Pro-coverage or ambulance gaps it reports.
6. Go to **Crew Rollup** — review station → ambulance → crew, set leaders,
   and use **⇄** to override specific slots if needed.
7. From **View / Export**: **Submit for review**, have an admin **Validate**
   and **Publish**. Affected employees get notified.
8. For a mid-cycle mobilization, create an **Event** as admin, then have the
   Drivers Manager or PHTL click **Replan for this event** against the
   month's roster run.

## Running the test suite safely

```
python test_pipeline.py
```

This runs an end-to-end smoke test (13 stations/ambulances, 148 staff, a
month of generation) against an **isolated test database**
(`ems_roster_test` by default) — never the database your running app uses.
It will refuse to run at all if `TEST_DATABASE_URL` and `DATABASE_URL` ever
point at the same database, so there's no way to accidentally wipe real
operational data by running it.

## Deploying as a shared service

`docker-compose.yml` builds and runs the whole stack (app + Postgres) for
deployment on a real server multiple people connect to:

```
docker compose up -d --build
```

Set a real `JWT_SECRET` (and SMTP variables, if using email) in your
environment or a `.env` file next to `docker-compose.yml` before deploying
anywhere reachable over a network — the defaults are for local development
only.

## Database migrations

Schema changes go through Alembic. After pulling changes that touch
`app/models.py`:

```
alembic upgrade head
```

To create a new migration after changing a model yourself:

```
alembic revision --autogenerate -m "describe the change"
```

Alembic-generated migration files reference `sqlmodel.sql.sqltypes...` —
the import for that is already added to the migration template
(`migrations/script.py.mako`), so autogenerated files should work as-is.

## What's intentionally not in this version

Multi-district/multi-country expansion, GIS/live travel-time routing, demand
forecasting, fatigue-risk modeling, what-if simulation, multi-month fairness
balancing beyond a single run, a dedicated standby-team rotation beyond the
basic Pro/crew model, and Call Center's own scheduling logic (the role and
its access boundaries exist; its scheduling specifics don't yet). Overtime in
the quality score is a proxy off shift-count spread, not real hours-based
contract tracking — that needs an hours/contract model this version doesn't
have yet.
