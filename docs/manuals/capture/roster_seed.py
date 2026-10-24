"""Fills a THROWAWAY Roster instance with fictional demo data, so the manual screenshots look like real use.

    # 1. a scratch server (never the production database):
    DATABASE_URL=sqlite:///demo.db MEDICAL_SOLVE_SECONDS=30 DRIVER_SOLVE_SECONDS=45 \\
        python -m uvicorn app.main:app --port 8765
    # 2. this script (about 3 minutes: it runs the real solver twice):
    python docs/manuals/capture/roster_seed.py
    # 3. node docs/manuals/capture/roster_capture.mjs

All names are invented. Nothing here touches real staff data.
"""
import json
import random
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
PASSWORD = "Demo2026!pass"
random.seed(7)


def call(method, path, body=None, token=None, ok=(200,)):
    req = urllib.request.Request(
        BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        if e.code in ok:
            return None
        sys.exit(f"{method} {path} -> {e.code}: {detail}")


for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/"); break
    except Exception:
        time.sleep(0.5)

call("POST", "/api/auth/users", {"username": "admin", "password": PASSWORD, "role": "admin"})
tok = call("POST", "/api/auth/login", {"username": "admin", "password": PASSWORD})["access_token"]
A = lambda m, p, b=None: call(m, p, b, tok)

FIRST = ["Alice", "Jean", "Marie", "Eric", "Diane", "Patrick", "Claudine", "Emmanuel", "Grace", "Olivier", "Josiane", "Vincent", "Chantal",
         "Samuel", "Immaculee", "David", "Solange", "Theogene", "Beatrice", "Fabrice", "Esperance", "Innocent", "Noella", "Gilbert", "Divine",
         "Pacifique", "Yvonne", "Aime", "Clarisse", "Jacques", "Sandrine", "Felix", "Judith", "Moise", "Aline", "Kevin", "Anitha", "Bosco"]
LAST = ["Mukamana", "Uwimana", "Habimana", "Niyonsenga", "Ishimwe", "Mugisha", "Uwase", "Nsengimana", "Mutesi", "Bizimana", "Iradukunda",
        "Nkurunziza", "Umutoni", "Hakizimana", "Mukeshimana", "Ndayisaba", "Uwera", "Kamanzi", "Tuyisenge", "Rutayisire", "Nyirahabimana", "Munyaneza"]
used = set()


def name():
    while True:
        n = f"{random.choice(FIRST)} {random.choice(LAST)}"
        if n not in used:
            used.add(n); return n


# ---- setup data ---------------------------------------------------------------------------------
stations = {}
for code, nm in [("1", "CHUK"), ("2", "Remera"), ("3", "Muhima"), ("4", "Nyamirambo"), ("5", "Kacyiru"), ("7", "Kicukiro")]:
    stations[code] = A("POST", "/api/stations", {"name": nm, "code": code, "day_crew_size": 3, "night_crew_size": 3,
                                                  "min_drivers_per_crew": 1})
    A("POST", "/api/ambulances", {"plate_no": f"RAD {100 + int(code)}A", "type": "standard", "station_id": stations[code]["id"]})
call_center = A("POST", "/api/facilities", {"name": "Call Center", "required_headcount": 5})
codes = list(stations)


def emp(role, n, **kw):
    out = []
    for i in range(n):
        st = stations[codes[i % len(codes)]]["id"] if kw.get("home", True) else None
        body = {"name": name(), "phone": f"07{random.randint(10000000, 99999999)}", "role": role, "is_driver": role == "driver",
                "station_id": st, "skill_score": random.randint(1, 5)}
        body.update({k: v for k, v in kw.items() if k != "home"})
        out.append(A("POST", "/api/employees", body))
    return out


drivers = emp("driver", 36)
critical = emp("critical", 8, is_pro=True)
anesth = emp("anesthesiologist", 4, is_pro=True)
nurses = emp("nurse", 40)
for i, n in enumerate(nurses):
    if i % 4 != 3:   # three nurses in four are Pro
        A("PUT", f"/api/employees/{n['id']}", {"is_pro": True})
A("PUT", f"/api/employees/{nurses[5]['id']}", {"restricted_night_shift": True})
A("PUT", f"/api/employees/{nurses[11]['id']}", {"restricted_night_shift": True})
dispatch = []
for i in range(6):
    dispatch.append(A("POST", "/api/employees", {"name": name(), "phone": f"07{random.randint(10000000, 99999999)}",
                                                   "role": "call_and_dispatch", "facility_id": call_center["id"]}))
A("PUT", "/api/standby-settings", {"driver_day": 1, "driver_night": 1, "medical_day": 2, "medical_night": 2, "call_center_day": 1, "call_center_night": 1})

for username, role, e in [("francois.ndayisenga", "drivers_manager", drivers[0]), ("sylvie.mukandayisenga", "phtl", nurses[0]),
                          ("hortense.uwamahoro", "call_center_tl", dispatch[0])]:
    A("POST", "/api/auth/users", {"username": username, "password": PASSWORD, "role": role, "employee_id": e["id"]})

# leave before generation (the solver respects it)
pool = drivers[1:] + nurses[1:] + critical
for e, code, d1, d2 in [(random.choice(pool), "AL", "2026-11-03", "2026-11-06") for _ in range(6)] + \
                       [(random.choice(pool), "SL", "2026-11-10", "2026-11-11") for _ in range(3)] + \
                       [(random.choice(pool), "TR", "2026-11-12", "2026-11-12") for _ in range(3)]:
    call("POST", "/api/leave", {"employee_id": e["id"], "day": d1, "end_day": d2, "code": code}, tok, ok=(200, 400))


# ---- rosters --------------------------------------------------------------------------------------
def generate(start, end):
    job = A("POST", f"/api/roster/generate?start_date={start}&end_date={end}&range_type=week")
    t0 = time.time()
    while True:
        j = A("GET", f"/api/roster/jobs/{job['job_id']}")
        if j["status"] in ("done", "failed"):
            break
        time.sleep(3)
    print(f"{start}..{end}: {j['status']} in {time.time() - t0:.0f}s", (j.get("error") or ""))
    if j["status"] != "done":
        sys.exit("generation failed")
    return job["run_id"]


run1 = generate("2026-11-02", "2026-11-08")
# a manager accepts the driver gaps the solver could not fill (recorded as approved shortages), then the week is published
issues = A("GET", f"/api/roster/{run1}/validate")["issues"]
crew_ids = {(a["ambulance"]["id"], sh["day"], sh["shift"]): sh["crew_assignment_id"]
            for st in A("GET", f"/api/roster/{run1}/crew") for a in st["ambulances"] for sh in a["shifts"]}
for iss in issues:
    if iss["type"] == "no_driver_assigned":
        A("POST", f"/api/crew/{crew_ids[(iss['ambulance_id'], iss['day'], iss['shift'])]}/approve-shortage",
          {"rule_broken": "no_driver", "reason": "staff_shortage",
           "justification": "Two drivers on leave this week and no rested replacement is available; the shift runs with a medical crew only."})
    elif iss["type"] == "no_pro_on_crew":
        A("POST", f"/api/crew/{crew_ids[(iss['ambulance_id'], iss['day'], iss['shift'])]}/approve-shortage",
          {"rule_broken": "pro_coverage", "reason": "staff_shortage",
           "justification": "No Pro-rated nurse is rested and free for this night; the crew is accepted as is and will be reviewed."})
A("POST", f"/api/roster/{run1}/submit")
A("POST", f"/api/roster/{run1}/publish")
run2 = generate("2026-11-09", "2026-11-15")

# a leave entered after generation -> a real conflict on the draft
rows = A("GET", f"/api/roster/{run2}")["rows"]
victim = next(r for r in rows if r["role"] == "nurse" and r["days"].get("2026-11-11", "O")[0] in "DN" and r["days"]["2026-11-11"][1:].isdigit())
call("POST", "/api/leave", {"employee_id": victim["employee_id"], "day": "2026-11-11", "code": "AL"}, tok, ok=(200, 400))

# a recorded command decision: put someone on a crew despite a protected rule, with a justification
crews = A("GET", f"/api/roster/{run2}/crew?day=2026-11-10")
done = 0
for st in crews:
    for amb in st["ambulances"]:
        for sh in amb["shifts"]:
            if done or not sh["members"]:
                continue
            old = sh["members"][0]["id"]
            cands = A("GET", f"/api/crew/{sh['crew_assignment_id']}/candidates?slot=member&exclude_employee_id={old}")
            risky = next((c for c in cands if c.get("overridable")), None)
            if risky:
                call("POST", f"/api/crew/{sh['crew_assignment_id']}/replace",
                     {"slot": "member", "old_employee_id": old, "new_employee_id": risky["employee_id"], "override": True,
                      "reason": "staff_shortage", "justification": "Colleague called in sick an hour before the shift; no rested replacement was available."},
                     tok, ok=(200, 409))
                done += 1
print("override recorded:", bool(done))

for nm_, typ, day, loc, pri in [("Amahoro Stadium league match", "football_match", "2026-11-12", "Amahoro Stadium", 3),
                                 ("Liberation Day celebration", "national_celebration", "2026-11-14", "Kigali Convention Centre", 4)]:
    A("POST", "/api/events", {"name": nm_, "type": typ, "date": day, "time": "15:00", "location": loc, "priority": pri,
                              "ambulances_required": 2, "medical_crew_required": 4, "drivers_required": 2})
print("demo data ready; rosters:", run1, run2)
