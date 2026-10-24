"""Reads every roster from a running instance and checks the hard rules across ALL of them together,
including the border between two consecutive rosters.

    python docs/manuals/capture/check_rosters.py [base_url] [username] [password]

Rules checked: nobody works the day after a night shift; no day shift directly before a night shift;
no more than 2 worked days in a row; nobody works on a leave day; restricted-night staff get no nights.
"""
import collections
import json
import sys
import urllib.request
from datetime import date, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
USER = sys.argv[2] if len(sys.argv) > 2 else "admin"
PWD = sys.argv[3] if len(sys.argv) > 3 else "Demo2026!pass"


def call(path, body=None, token=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    return json.load(urllib.request.urlopen(req))


tok = call("/api/auth/login", {"username": USER, "password": PWD})["access_token"]
runs = sorted(call("/api/roster", token=tok), key=lambda r: r["start_date"])
employees = {e["id"]: e for e in call("/api/employees", token=tok)}
lo, hi = runs[0]["start_date"], runs[-1]["end_date"]
leave = {(l["employee_id"], l["day"]) for l in call(f"/api/leave?start_date={lo}&end_date={hi}", token=tok)}

codes = collections.defaultdict(dict)       # employee_id -> {date: code}
owner = {}                                   # date -> run id (to say which roster a violation is in)
for r in runs:
    for row in call(f"/api/roster/{r['id']}", token=tok)["rows"]:
        for d, c in row["days"].items():
            codes[row["employee_id"]][date.fromisoformat(d)] = c
            owner[date.fromisoformat(d)] = r["id"]


def work(c): return bool(c) and c[0] in "DN" and len(c) > 1 and (c[1:].isdigit() or c[1:] in ("R", "E"))


problems = collections.Counter()
examples = {}
for eid, days in codes.items():
    ds = sorted(days)
    streak = 0
    for d in ds:
        c = days[d]
        prev = days.get(d - timedelta(days=1))
        if work(c) and prev and work(prev) and prev[0] == "N":
            problems["work the day after a night"] += 1; examples.setdefault("work the day after a night", (employees[eid]["name"], str(d), prev, c, f"roster {owner[d]}"))
        if work(c) and c[0] == "N" and prev and work(prev) and prev[0] == "D":
            problems["night directly after a day shift"] += 1; examples.setdefault("night directly after a day shift", (employees[eid]["name"], str(d), prev, c))
        streak = streak + 1 if work(c) else 0
        if streak > 2:
            problems["more than 2 worked days in a row"] += 1; examples.setdefault("more than 2 worked days in a row", (employees[eid]["name"], str(d), f"roster {owner[d]}"))
        if work(c) and (eid, str(d)) in leave:
            problems["worked on a leave day"] += 1
        if work(c) and c[0] == "N" and employees[eid]["restricted_night_shift"]:
            problems["night for someone restricted from nights"] += 1

print(f"{len(runs)} rosters, {lo} .. {hi}, {len(codes)} people")
if not problems:
    print("OK: no hard-rule violations")
for k, v in problems.items():
    print(f"  {v:4}  {k}   e.g. {examples.get(k)}")
sys.exit(1 if problems else 0)
