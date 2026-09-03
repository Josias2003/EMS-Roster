"""
The actual optimizer. This is the part that replaces 6 hours of manual work.

Hard constraints enforced:
  - One shift per employee per day (day OR night OR off/leave, never both)
  - Leave/training/admin days are respected exactly as entered
  - After a night shift, the employee is off the next day (rest requirement)
  - No more than MAX_CONSECUTIVE_DAYS worked in a row before a forced day off
  - Staff flagged restricted_night_shift are never put on a night shift
  - A crew never has more people than its station's configured size

Soft constraints (weighted penalties in the objective, all reported back
explicitly rather than silently dropped or — worse — blocking the whole
roster from generating):
  - Every crew should have at least one Pro-rated member
  - Every ambulance's crew should be filled to its station's configured size
  - Medical staff should stay at their home station unless flagged floats_freely
  - Spread of total/night/weekend HOURS should be even within each role group,
    counted cumulatively for the whole calendar month, not just this run

Nothing about staffing shortage ever prevents a roster from being generated.
If there genuinely aren't enough people, the solver fills what it can, and
the shortfall — plus anyone working unusually many shifts as a result — comes
back as explicit, human-readable flags instead of an opaque solver error.

Medical staff can be assigned to any active ambulance system-wide, not just
their home station's — matching how they actually work in practice — but a
heavy soft penalty keeps them at home by default, only sending them
elsewhere once their own station's needs are already met and another
station is short. Staff flagged `floats_freely` are exempt from that penalty.
"""
import calendar
from datetime import date, timedelta
from collections import defaultdict
from ortools.sat.python import cp_model

MAX_CONSECUTIVE_DAYS = 2

# Target ceiling on total-hours spread (max - min) within a single fairness
# group — kept strictly under 10h per group request. Applied as an
# effectively-hard CP-SAT objective term for medical (see generate_roster)
# and as a hard eligibility filter with a scarcity fallback for the greedy
# driver/facility-staff allocators (see schedule_drivers) — a real shortage
# can still force it wider, but only when there's truly no other choice.
SPREAD_CAP_HOURS = 9

# How many times generate_roster()'s medical CP-SAT model gets solved (each
# with a fresh random seed, keeping whichever attempt's own objective value
# is lowest) before giving up and using the best one found — see
# generate_roster()'s docstring for the real convergence-reliability incident
# this fixes. 3 attempts at time_limit_seconds=200 each (~10 min worst case)
# reliably found the good solution in testing; a single attempt, even at
# 420s, did not.
#
# Briefly raised to 5 once Regulation-desk folding made the model bigger, but
# confirmed directly this was the wrong lever for the remaining problem: even
# at 5 x 350s (~29 min), 1-2 shifts stayed short by 1 person despite 105
# medical staff for only 18 nightly slots — no real shortage.
# CREW_SHORTFALL_PENALTY (100000) already vastly outweighs every fairness
# term, so this wasn't a weighting problem; it's that each attempt is an
# independent COLD solve (fresh random seed, no shared progress), so 5 short
# solves each explore less than 2 long ones would. Dropped back to 2 attempts
# with a much longer per-attempt budget instead (see time_limit_seconds
# default below) — sustained search time, not more cold restarts, is what
# closes a genuine coverage gap.
#
# Raised to 3 once coverage was confirmed reliable at 2 x 600s — coverage and
# rest are protected regardless of attempt count (they're the TOP tiers of
# the selection key below, always kept even if a later attempt has better
# fairness but worse coverage), so an extra attempt only ever helps the
# fairness search further without risking either hard requirement.
#
# Dropped back to 2 once the specialist-ambulance-mix constraint (see
# SPECIALIST_AMBULANCE_MIX_PENALTY) was added — same "cold restart" problem
# as the coverage incident: at 3 short-ish attempts, specialists stuck on
# Regulation all week barely improved (9 -> 8 -> 6) even at 10x the penalty
# weight, but a SINGLE much-longer solve (see time_limit_seconds default
# below) got it to 2, then 1, with no config change — confirming this was
# never a weighting problem, it's that a small, spread-out improvement
# (fixing one person's week) gets lost in cold restarts of a huge
# combinatorial space. Fewer, longer attempts give it room to actually find
# these small local wins.
GENERATE_ROSTER_ATTEMPTS = 2

# Hard weekly cap on Regulation-desk shifts per critical/anesthesiologist
# specialist — shared by generate_roster()'s CP-SAT model (the hard
# model.Add(reg_sum <= ...) constraint) AND assign_regulation_nurses()'s
# greedy top-up pass, which must respect the SAME cap or it can push someone
# straight past what CP-SAT already capped — real incident: the cap only
# lived inside the CP-SAT model at first, so the top-up pass (no knowledge
# of it) kept adding Regulation shifts on top and 8 specialists ended up at
# 3 Regulation shifts each despite a hard cap of 2. 2 * 14 shifts/week = up
# to 34 person-shifts of supply capacity across ~17 specialists, comfortably
# above the ~28-person-shift weekly demand at MEDICAL_REGULATION_CAP=2/shift,
# while forcing at least half the pool to contribute if demand is to be met
# at all — real spread, not a nudge.
MAX_REGULATION_SHIFTS_PER_SPECIALIST = 2

# For the greedy driver/facility-staff allocators specifically, the real
# target confirmed directly: within a single week with nobody on leave,
# nobody should work more than 1 extra SHIFT compared to the least-loaded
# person that week — hour totals then even out across the MONTH via the
# existing cumulative-hours carry-forward (starting_hours), not within one
# week. Shift count (unlike hours) has no day/14h-vs-night/10h granularity
# problem, so "always pick from whoever's currently at the group's lowest
# shift count" provably keeps everyone within 1 of each other whenever
# there's a real choice — only a genuine day-by-day shortage (nobody at the
# floor is otherwise eligible for a specific slot) can force it wider.
SHIFT_COUNT_SPREAD_CAP = 1

# A day shift runs 10h, a night shift 14h (together they cover the full
# 24h a station needs). Fairness is balanced in hours, not raw shift
# counts, and — critically — against each person's CUMULATIVE hours for
# the whole calendar month (carried forward from any other roster run
# already generated this month), not just the days in this particular
# run. A single week is never forced to hit some fixed weekly number on
# its own; what matters is staying close to the group average once the
# month is taken as a whole.
DAY_SHIFT_HOURS = 10
NIGHT_SHIFT_HOURS = 14
AVG_SHIFT_HOURS = (DAY_SHIFT_HOURS + NIGHT_SHIFT_HOURS) / 2  # used only to keep the 0-100 quality score's calibration meaningful now that spreads are in hours, not shift counts

# Per-person cost (times group size n, see the dev_pos terms below) for each
# hour a person's total sits above their role group's own average. Complements
# the max-min "spread" term, which alone only disciplines the single worst/best
# pair and leaves a large group's middle free to cluster near the top.
PER_PERSON_ABOVE_MEAN_PENALTY = 1


def is_work_shift_code(code) -> bool:
    """True for a real D#/N# (station) work-shift code, DR/NR (Regulation
    desk — Call & Dispatch's own shift or PHTL's gap-fill contribution,
    WHICH facility tracked separately via Assignment.facility_id, not in the
    code string), or DE/NE (event duty — see events.py's _set_event_day_code)
    — false for off/leave codes or anything else. Single shared definition
    for every place that needs to tell "this row represents actual work"
    from a raw Assignment code.
    DE/NE counting as real work is a deliberate fix, not always true here:
    event duty used to fall through to "not a work code" everywhere —
    excluded from total hours, rest-adjacency, MAX_CONSECUTIVE_DAYS streaks,
    and the day/night mix requirement — even though someone on an event is
    unambiguously working that shift. Confirmed directly: event hours must
    be calculated and included in totals like any other shift.
    DF#/NF# (an older facility-numbered format) is still recognized here for
    any rows already persisted with it before the DR/NR switch — matches the
    real paper roster's legend (LEAVE_CODES in models.py), which the
    solver-generated codes should mirror exactly rather than invent their
    own numbered variant."""
    if not code or len(code) < 2 or code[0] not in ("D", "N"):
        return False
    rest = code[1:]
    return rest.isdigit() or rest in ("R", "E") or (rest[0] == "F" and rest[1:].isdigit())


def _shift_hours(code: str) -> int:
    """10/14 for a work-shift code, 0 for anything else (off, leave, ...)."""
    if is_work_shift_code(code):
        return DAY_SHIFT_HOURS if code[0] == "D" else NIGHT_SHIFT_HOURS
    return 0


def date_range(start: date, end: date):
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(n + 1)]


def month_days(year: int, month: int):
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n + 1)]


def _rebalance_hours(assignments, state, employee_ids, days, driver_by_slot=None,
                      cap=SHIFT_COUNT_SPREAD_CAP, max_iterations=1500):
    """
    Post-pass local-search rebalancing for the greedy driver/facility-staff
    allocators, ranked on SHIFT COUNT (not hours) — confirmed directly: within
    a single leave-free week, nobody should work more than 1 extra shift
    versus the least-loaded person; hour totals then even out across the
    MONTH via the existing cumulative-hours carry-forward, not within one
    week. The per-slot picker above can't guarantee a bounded FINAL spread on
    its own even for shift count — comparing each pick against a live,
    monotonically-rising group floor only bounds the gap at the moment of
    picking, not at week's end: someone picked early, while the floor was
    still low, can still end up ahead of it by the time the floor catches up,
    even though every individual pick was legal when it happened. This pass
    looks at the actual final shift-count distribution and repeatedly moves a
    single shift from someone with more shifts to someone with fewer,
    wherever that's legal for the receiving person (not already working that
    day, doesn't break their own rest-after-night or consecutive-days limit)
    — trying several of the most- and least-loaded people, not just the
    single top/bottom pair, so one person's genuinely fixed schedule doesn't
    stall progress for everyone else. Stops once the spread is at or under
    the cap, or once no further legal swap exists — a real shortage (or a
    week where nobody's schedule has room to move) can still leave the cap
    unmet, same as everywhere else in this file. Mutates `assignments` and
    `state` in place.
    """
    days_sorted = sorted(days)
    by_emp_day = defaultdict(dict)
    for a in assignments:
        if a["employee_id"] in employee_ids:
            by_emp_day[a["employee_id"]][a["day"]] = a

    # Reverse index into driver_by_slot ((ambulance_id, day, shift) -> driver
    # employee_id) so a swap can keep it in sync — it's what main.py uses to
    # set CrewAssignment.driver_employee_id, and would otherwise disagree
    # with the Assignment row a swap just changed.
    slot_by_emp_day = {}
    if driver_by_slot is not None:
        for (amb_id, d, shift), eid in driver_by_slot.items():
            slot_by_emp_day[(eid, d)] = (amb_id, shift)

    def codes_for(eid):
        return {d: row["code"] for d, row in by_emp_day[eid].items()}

    def streak_if_worked(eid, d):
        codes = codes_for(eid)
        idx = days_sorted.index(d)
        streak = 1
        i = idx - 1
        while i >= 0 and is_work_shift_code(codes.get(days_sorted[i])):
            streak += 1; i -= 1
        i = idx + 1
        while i < len(days_sorted) and is_work_shift_code(codes.get(days_sorted[i])):
            streak += 1; i += 1
        return streak

    def eligible_for_swap(eid, d, shift_type):
        codes = codes_for(eid)
        if codes.get(d) != "O":
            return False
        idx = days_sorted.index(d)
        if idx > 0:
            prev_code = codes.get(days_sorted[idx - 1])
            if prev_code and prev_code.startswith("N") and is_work_shift_code(prev_code):
                return False
        if shift_type == "night" and idx < len(days_sorted) - 1:
            next_code = codes.get(days_sorted[idx + 1])
            if next_code and is_work_shift_code(next_code):
                return False
        # Rest BEFORE a night too, not just after — a Day pick landing
        # immediately before an already-fixed Night the next day is the
        # same forbidden adjacency, just from the other side.
        if shift_type == "day" and idx < len(days_sorted) - 1:
            next_code = codes.get(days_sorted[idx + 1])
            if next_code and next_code.startswith("N") and is_work_shift_code(next_code):
                return False
        # Same rule, other direction: a NIGHT pick landing immediately after
        # an already-fixed Day the day before is the identical forbidden
        # D->N adjacency — real bug found via full-pipeline verification
        # (48 adjacency violations survived this pass because only the
        # prev-was-Night case was checked here, never prev-was-Day).
        if shift_type == "night" and idx > 0:
            prev_code = codes.get(days_sorted[idx - 1])
            if prev_code and is_work_shift_code(prev_code):
                return False
        return streak_if_worked(eid, d) <= MAX_CONSECUTIVE_DAYS

    def apply_swap(hi_eid, lo_eid, d):
        hi_row, lo_row = by_emp_day[hi_eid][d], by_emp_day[lo_eid][d]
        hrs = _shift_hours(hi_row["code"])
        is_night = hi_row["code"][0] == "N"
        code, station_id = hi_row["code"], hi_row["station_id"]
        hi_row["code"], hi_row["station_id"] = "O", None
        lo_row["code"], lo_row["station_id"] = code, station_id
        slot = slot_by_emp_day.pop((hi_eid, d), None)
        if slot is not None and driver_by_slot is not None:
            amb_id, shift = slot
            driver_by_slot[(amb_id, d, shift)] = lo_eid
            slot_by_emp_day[(lo_eid, d)] = slot
        state[hi_eid]["hours"] -= hrs; state[hi_eid]["total"] -= 1
        state[lo_eid]["hours"] += hrs; state[lo_eid]["total"] += 1
        if is_night:
            state[hi_eid]["nights"] -= 1; state[hi_eid]["night_hours"] -= hrs
            state[lo_eid]["nights"] += 1; state[lo_eid]["night_hours"] += hrs
        if d.weekday() >= 5:
            state   [hi_eid]["weekends"] -= 1; state[hi_eid]["weekend_hours"] -= hrs
            state[lo_eid]["weekends"] += 1; state[lo_eid]["weekend_hours"] += hrs

    for _ in range(max_iterations):
        ranked = sorted(employee_ids, key=lambda e: -state[e]["total"])
        if state[ranked[0]]["total"] - state[ranked[-1]]["total"] <= cap:
            break
        # lo_true FIRST, exhausted against EVERY hi_candidate, before ever
        # widening to a different low earner — real bug found by testing on
        # the medical hours-based version of this same pattern: looping
        # hi-then-lo let the first hi_candidate happily swap into whichever
        # lo_candidate it had a legal opening for, even when that wasn't
        # lo_true — leaving the TRUE bottleneck untouched for many
        # iterations while the algorithm kept "succeeding" at swaps that
        # helped people who weren't actually the problem. Swap that
        # doesn't touch the actual bottleneck doesn't shrink the spread
        # even if it's legal, since spread is max minus the global min.
        lo_true = ranked[-1]
        hi_candidates = [e for e in ranked if state[e]["total"] - state[lo_true]["total"] > cap]
        # Widen closest-to-minimum FIRST, not furthest — real bug found by
        # testing: ranked[-10:-1] in forward order tries the FURTHEST-from-
        # minimum person second and the person actually TIED with lo_true
        # (ranked[-2]) dead last, so a tied-minimum person could get
        # perpetually skipped every iteration in favor of "easier" people
        # further up the list, even though they're just as much the real
        # bottleneck as lo_true itself. Reversed so ties-for-minimum get
        # tried right after lo_true, before falling back to people who
        # aren't actually part of the worst-off group.
        lo_candidates = [lo_true] + list(reversed(ranked[-10:-1]))
        moved = False
        for lo_eid in lo_candidates:
            for hi_eid in hi_candidates:
                if hi_eid == lo_eid or state[hi_eid]["total"] - state[lo_eid]["total"] <= cap:
                    continue
                for d in sorted(by_emp_day[hi_eid]):
                    row = by_emp_day[hi_eid][d]
                    if not is_work_shift_code(row["code"]):
                        continue
                    shift_type = "night" if row["code"][0] == "N" else "day"
                    if eligible_for_swap(lo_eid, d, shift_type):
                        apply_swap(hi_eid, lo_eid, d)
                        moved = True
                        break
                if moved:
                    break
            if moved:
                break
        if not moved:
            break  # no legal swap left among the widest candidates — a real limit, not a bug

    # Phase 2: shift COUNT is now as balanced as legally possible, but two
    # people at the identical count can still land far apart on HOURS if
    # one's shifts skew toward nights (14h) and the other's toward days
    # (10h) — confirmed directly as a real remaining gap even at count
    # spread=1. Close it by trading a night-shift day for a day-shift day
    # between a night-heavy and a day-heavy person who are AT THE SAME
    # shift count: two linked single-shift moves (the same apply_swap used
    # above, run twice) that leave both people's shift counts exactly where
    # they were — only their hours converge. Only ever trades within a tied
    # count, so it can't undo phase 1's balancing.
    for _ in range(max_iterations):
        ranked = sorted(employee_ids, key=lambda e: -state[e]["hours"])
        if state[ranked[0]]["hours"] - state[ranked[-1]]["hours"] <= SPREAD_CAP_HOURS:
            break
        moved = False
        for lo_eid in reversed(ranked):
            if state[ranked[0]]["hours"] - state[lo_eid]["hours"] <= SPREAD_CAP_HOURS:
                break  # ranked ascending from the end — nobody further up needs this trade either
            for hi_eid in ranked:
                if hi_eid == lo_eid or state[hi_eid]["hours"] - state[lo_eid]["hours"] <= SPREAD_CAP_HOURS:
                    continue
                if state[hi_eid]["total"] != state[lo_eid]["total"]:
                    continue  # only trade between people the shift-count cap already ties together
                night_day = next((d for d in sorted(by_emp_day[hi_eid])
                                   if by_emp_day[hi_eid][d]["code"].startswith("N")
                                   and is_work_shift_code(by_emp_day[hi_eid][d]["code"])
                                   and eligible_for_swap(lo_eid, d, "night")), None)
                if night_day is None:
                    continue
                day_day = next((d for d in sorted(by_emp_day[lo_eid])
                                 if by_emp_day[lo_eid][d]["code"].startswith("D")
                                 and is_work_shift_code(by_emp_day[lo_eid][d]["code"])
                                 and d != night_day
                                 and eligible_for_swap(hi_eid, d, "day")), None)
                if day_day is None:
                    continue
                apply_swap(hi_eid, lo_eid, night_day)
                apply_swap(lo_eid, hi_eid, day_day)
                moved = True
                break
            if moved:
                break
        if not moved:
            break


def rebalance_medical_hours(assignment_rows, days, cap=SPREAD_CAP_HOURS, max_iterations=20000, mix_exempt=None,
                             regulation_eligible_ids=None):
    """
    Final hours-based cleanup pass across the WHOLE medical team (every
    specialty combined, including Regulation-desk shifts) — confirmed
    directly: total hours (field crew + Regulation together) should end up
    within `cap` across everyone, not just within each specialty and not
    just the field-crew portion. Runs AFTER both the medical CP-SAT pass and
    the Regulation-desk pass, on their COMBINED persisted output, because
    those are two separate optimizations (CP-SAT balances the field
    schedule; Regulation is filled in afterward, least-hours-first from
    whoever's lightest at that moment) that don't perfectly compose into one
    globally-tight spread on their own — real incident: each individually
    reasonable, together still left a 20h gap.

    `assignment_rows` are DICTS shaped {"employee_id", "day", "code",
    "station_id", "facility_id"} — same shape the rest of this file uses,
    NOT the CrewAssignment/CrewMember tables, so a swap only ever changes
    what shift (if any) someone works that day, never who's assigned as a
    station's crew LEADER or which specific ambulance a Regulation pick
    belongs to (Regulation has no per-ambulance concept to keep in sync,
    unlike schedule_drivers's driver_by_slot).

    Same swap mechanics as _rebalance_hours: move a single shift from the
    currently most-loaded person to the currently least-loaded one, only
    when legal for the receiver (not already working that day, doesn't
    break their own rest-after-night or consecutive-days limit) — tries
    several of the most/least loaded people, not just the single top/bottom
    pair, so one person's fixed schedule doesn't stall everyone else.
    Mutates `assignment_rows` in place and returns it.
    """
    days_sorted = sorted(days)
    by_emp_day = defaultdict(dict)
    for row in assignment_rows:
        by_emp_day[row["employee_id"]][row["day"]] = row
    employee_ids = list(by_emp_day.keys())
    if len(employee_ids) < 2:
        return assignment_rows

    hours = {eid: sum(_shift_hours(r["code"]) for r in rows.values()) for eid, rows in by_emp_day.items()}
    mix_exempt = mix_exempt or set()
    regulation_eligible_ids = regulation_eligible_ids or set()

    def codes_for(eid):
        return {d: row["code"] for d, row in by_emp_day[eid].items()}

    def type_counts(eid):
        day_n = night_n = 0
        for row in by_emp_day[eid].values():
            if is_work_shift_code(row["code"]):
                if row["code"][0] == "N":
                    night_n += 1
                else:
                    day_n += 1
        return day_n, night_n

    def donation_breaks_mix(hi_eid, shift_type):
        # Donating a person's LAST shift of one type while they keep shifts
        # of the other type collapses them into an all-day/all-night pattern
        # — the exact thing CP-SAT's MIX_PENALTY exists to prevent. This pass
        # runs after CP-SAT though, so it must preserve that invariant itself
        # rather than assume it — real bug: 3 people ended up all-night
        # because a donor's only remaining day shift got swapped away here.
        if hi_eid in mix_exempt:
            return False
        day_n, night_n = type_counts(hi_eid)
        if shift_type == "day":
            return day_n == 1 and night_n > 0
        return night_n == 1 and day_n > 0

    def streak_if_worked(eid, d):
        codes = codes_for(eid)
        idx = days_sorted.index(d)
        streak = 1
        i = idx - 1
        while i >= 0 and is_work_shift_code(codes.get(days_sorted[i])):
            streak += 1; i -= 1
        i = idx + 1
        while i < len(days_sorted) and is_work_shift_code(codes.get(days_sorted[i])):
            streak += 1; i += 1
        return streak

    def eligible_for_swap(eid, d, shift_type):
        codes = codes_for(eid)
        if codes.get(d) != "O":
            return False
        idx = days_sorted.index(d)
        if idx > 0:
            prev_code = codes.get(days_sorted[idx - 1])
            if prev_code and prev_code.startswith("N") and is_work_shift_code(prev_code):
                return False
        if shift_type == "night" and idx < len(days_sorted) - 1:
            next_code = codes.get(days_sorted[idx + 1])
            if next_code and is_work_shift_code(next_code):
                return False
        # Rest BEFORE a night too, not just after — a Day pick landing
        # immediately before an already-fixed Night the next day is the
        # same forbidden adjacency, just from the other side.
        if shift_type == "day" and idx < len(days_sorted) - 1:
            next_code = codes.get(days_sorted[idx + 1])
            if next_code and next_code.startswith("N") and is_work_shift_code(next_code):
                return False
        # Same rule, other direction: a NIGHT pick landing immediately after
        # an already-fixed Day the day before is the identical forbidden
        # D->N adjacency — real bug found via full-pipeline verification
        # (48 adjacency violations survived this pass because only the
        # prev-was-Night case was checked here, never prev-was-Day).
        if shift_type == "night" and idx > 0:
            prev_code = codes.get(days_sorted[idx - 1])
            if prev_code and is_work_shift_code(prev_code):
                return False
        return streak_if_worked(eid, d) <= MAX_CONSECUTIVE_DAYS

    def apply_swap(hi_eid, lo_eid, d):
        hi_row, lo_row = by_emp_day[hi_eid][d], by_emp_day[lo_eid][d]
        hrs = _shift_hours(hi_row["code"])
        code = hi_row["code"]
        station_id = hi_row.get("station_id")
        facility_id = hi_row.get("facility_id")
        hi_row["code"], hi_row["station_id"], hi_row["facility_id"] = "O", None, None
        lo_row["code"], lo_row["station_id"], lo_row["facility_id"] = code, station_id, facility_id
        hours[hi_eid] -= hrs
        hours[lo_eid] += hrs

    # Real bug found via full-pipeline verification: when the TRUE minimum
    # person's own fixed shifts make every one of their off days illegal to
    # receive into (e.g. two shifts already back-to-back at the
    # MAX_CONSECUTIVE_DAYS cap, plus a night elsewhere needing rest on both
    # sides — a permanent, self-inflicted block independent of any donor),
    # the loop kept "succeeding" via OTHER low earners each iteration
    # (never hitting the not-moved break) while the true bottleneck sat
    # frozen for the entire iteration budget — confirmed by tracing: same
    # person, same hours, unchanged across 20000 iterations. Track how long
    # the true minimum has gone unchanged and bail out once it's clearly a
    # real floor rather than burning the whole budget chasing it.
    lo_true_stuck_at = None
    lo_true_stuck_count = 0
    STUCK_LIMIT = 300

    for _ in range(max_iterations):
        ranked = sorted(employee_ids, key=lambda e: -hours[e])
        if hours[ranked[0]] - hours[ranked[-1]] <= cap:
            break
        lo_true = ranked[-1]
        if lo_true == lo_true_stuck_at:
            lo_true_stuck_count += 1
            if lo_true_stuck_count >= STUCK_LIMIT:
                break  # real floor for this person, not a bug — stop wasting iterations
        else:
            lo_true_stuck_at = lo_true
            lo_true_stuck_count = 0
        hi_candidates = [e for e in ranked if hours[e] - hours[lo_true] > cap]
        # lo_true FIRST, exhausted against EVERY hi_candidate, before ever
        # widening to a different low earner — real bug found by testing:
        # the loop used to go hi-then-lo, so the very first hi_candidate
        # (the single highest earner) would happily swap into whichever
        # lo_candidate it had a legal opening for, even when that wasn't
        # lo_true — leaving the TRUE bottleneck untouched for thousands of
        # iterations while the algorithm kept "succeeding" at swaps that
        # helped people who weren't actually the problem (confirmed: 25
        # legal hi_candidate-to-lo_true swaps existed and were never tried
        # because a different, easier lo_candidate kept winning first).
        # Widen closest-to-minimum FIRST, not furthest — same fix as
        # _rebalance_hours: real bug found by tracing a live case (two
        # people tied at the actual minimum) where the tied person
        # (ranked[-2]) sat LAST in this list, so 13 "easier" secondary
        # candidates kept winning the swap search every single iteration
        # before she was ever tried — 20000 fresh iterations, zero progress
        # for her, even though a legal donor existed on her very first
        # eligible day. Reversed so ties-for-minimum get first crack.
        #
        # Widen window covers the WHOLE tied-at-minimum group, not a fixed
        # 15 — second real bug found by tracing a live case with 41 people
        # tied at the exact minimum: a fixed window only ever samples the
        # same ~14 of them (a stable sort keeps ties in the same relative
        # order every iteration, since nothing changes without a successful
        # swap), so the other ~27 — some of whom DID have a legal receiving
        # day — were never tried at all, iteration after iteration. Counting
        # everyone within TIE_TOLERANCE of lo_true's hours, capped only to
        # bound worst-case work per iteration, not to arbitrarily exclude
        # most of a large tie group.
        TIE_TOLERANCE = 1
        WIDEN_CAP = 60
        tied_low = [e for e in ranked if e != lo_true and hours[e] <= hours[lo_true] + TIE_TOLERANCE]
        lo_candidates = [lo_true] + list(reversed(tied_low))[:WIDEN_CAP]
        moved = False
        for lo_eid in lo_candidates:
            for hi_eid in hi_candidates:
                if hi_eid == lo_eid or hours[hi_eid] - hours[lo_eid] <= cap:
                    continue
                for d in sorted(by_emp_day[hi_eid]):
                    row = by_emp_day[hi_eid][d]
                    if not is_work_shift_code(row["code"]):
                        continue
                    # Regulation (DR/NR) is restricted to critical/
                    # anesthesiologist — real bug found via full-pipeline
                    # verification: a swap moving an hi_eid's DR/NR shift
                    # onto a lo_eid nurse silently put nurses back on
                    # Regulation, undoing that restriction after the CP-SAT
                    # solve had already correctly enforced it.
                    if row["code"] in ("DR", "NR") and lo_eid not in regulation_eligible_ids:
                        continue
                    shift_type = "night" if row["code"][0] == "N" else "day"
                    if donation_breaks_mix(hi_eid, shift_type):
                        continue
                    if eligible_for_swap(lo_eid, d, shift_type):
                        apply_swap(hi_eid, lo_eid, d)
                        moved = True
                        if lo_eid == lo_true:
                            lo_true_stuck_at = None  # true bottleneck moved — reset the stuck counter
                        break
                if moved:
                    break
            if moved:
                break
        if not moved:
            break  # no legal swap left among the widest candidates — a real limit, not a bug

    return assignment_rows


def schedule_drivers(drivers, station_ambulance_pairs, leaves, days, starting_hours=None, boundary_state=None,
                      time_limit_seconds=90):
    """
    CP-SAT allocator for the floating driver pool, one slot per active
    ambulance per shift (not per station — a station with 2 ambulances needs
    2 drivers, not 1). Mirrors generate_roster()'s medical CP-SAT model
    almost exactly (same hard-constraint shapes, same soft/never-blocks
    philosophy) — same reason: seeing the whole week at once lets the solver
    choose EACH driver's day-off pattern jointly with everyone else's,
    instead of fixing it greedily day-by-day and hoping a later local swap
    can patch any resulting unfairness.

    This replaces an earlier greedy least-loaded-first allocator, dropped for
    a confirmed real limit: even with a shift-count cap and a two-phase
    local-swap rebalancer, two drivers at the identical shift count could
    still land 16-18h apart in hours whenever their (already-fixed) days-off
    happened to block every legal night/day trade — exhaustively confirmed
    against real data (2 legal swaps existed among 29 four-shift drivers,
    neither touching the actual extremes). A greedy pass can only trade
    shifts after the fact; it can never reconsider which days someone is off
    in the first place. CP-SAT can.

    starting_hours: optional {employee_id: {"total","night","weekend"}} carried
    forward from other roster runs already generated this calendar month —
    seeds each driver's hour tally so fairness reflects the whole month, not
    just this run's days.
    boundary_state: optional {employee_id: {"was_night_before": bool, "consecutive_before": int}}
    — what this driver was doing the day immediately before `days[0]`, from
    ANY other roster run, so the rest-after-night and max-consecutive-days
    rules extend across the boundary the same way the medical CP-SAT does.

    Returns (assignments, gaps, driver_by_slot, total_shifts_by_driver, total_hours_by_driver).
    total_hours_by_driver is cumulative (starting_hours + this run).
    """
    starting_hours = starting_hours or {}
    boundary_state = boundary_state or {}

    # generate_roster() calls this unconditionally on every scope, including
    # a medical-only pass where `drivers` is deliberately passed in empty and
    # the result thrown away — must stay near-instant for that case, not pay
    # CP-SAT model/solver startup cost for zero variables.
    if not drivers:
        return [], [], {}, {}, {}

    model = cp_model.CpModel()

    work_day, work_night, assign = {}, {}, {}
    for d in drivers:
        for day in days:
            wd = model.NewBoolVar(f"dwd_{d['id']}_{day}")
            wn = model.NewBoolVar(f"dwn_{d['id']}_{day}")
            work_day[(d["id"], day)] = wd
            work_night[(d["id"], day)] = wn
            for shift, var in (("day", wd), ("night", wn)):
                shift_assigns = []
                for st, amb in station_ambulance_pairs:
                    if shift == "night" and not st.get("night_active", True):
                        continue
                    v = model.NewBoolVar(f"da_{d['id']}_{day}_{shift}_{amb['id']}")
                    assign[(d["id"], day, shift, amb["id"])] = v
                    shift_assigns.append(v)
                model.Add(sum(shift_assigns) == var)  # exactly one ambulance per driver per worked shift

    for d in drivers:
        for day in days:
            wd, wn = work_day[(d["id"], day)], work_night[(d["id"], day)]
            if (d["id"], day) in leaves:
                model.Add(wd == 0)
                model.Add(wn == 0)
            else:
                model.Add(wd + wn <= 1)

    for d in drivers:
        for i, day in enumerate(days[:-1]):
            nxt = days[i + 1]
            model.Add(work_day[(d["id"], nxt)] == 0).OnlyEnforceIf(work_night[(d["id"], day)])
            model.Add(work_night[(d["id"], nxt)] == 0).OnlyEnforceIf(work_night[(d["id"], day)])
            # Rest BEFORE a night shift too, not just after — same fix as
            # the medical model: a Day shift immediately followed by a
            # Night shift was never actually blocked before this.
            model.Add(work_day[(d["id"], day)] == 0).OnlyEnforceIf(work_night[(d["id"], nxt)])
        if boundary_state.get(d["id"], {}).get("was_night_before"):
            model.Add(work_day[(d["id"], days[0])] == 0)
            model.Add(work_night[(d["id"], days[0])] == 0)

    for d in drivers:
        for i in range(len(days) - MAX_CONSECUTIVE_DAYS):
            window = days[i:i + MAX_CONSECUTIVE_DAYS + 1]
            worked = [work_day[(d["id"], day)] + work_night[(d["id"], day)] for day in window]
            model.Add(sum(worked) <= MAX_CONSECUTIVE_DAYS)
        consec_before = boundary_state.get(d["id"], {}).get("consecutive_before", 0)
        if consec_before > 0:
            allowed_more = max(MAX_CONSECUTIVE_DAYS - consec_before, 0)
            lead_window = days[:min(allowed_more + 1, len(days))]
            if lead_window:
                worked = [work_day[(d["id"], day)] + work_night[(d["id"], day)] for day in lead_window]
                model.Add(sum(worked) <= allowed_more)

    # Per-slot demand is a heavily-weighted SOFT constraint, same as
    # everywhere else in this file — a genuine driver shortage still
    # produces a (clearly reported) best-effort roster instead of failing
    # to generate at all.
    SHORTFALL_PENALTY = 100000
    shortfall_vars = []  # (station_id, ambulance_id, day, shift, needed, shortfall IntVar)
    for st, amb in station_ambulance_pairs:
        need = st["min_drivers_per_crew"]
        for day in days:
            for shift in ("day", "night"):
                if shift == "night" and not st.get("night_active", True):
                    continue
                slot_assigns = [assign[(d["id"], day, shift, amb["id"])] for d in drivers]
                shortfall = model.NewIntVar(0, need, f"dshort_{st['id']}_{amb['id']}_{day}_{shift}")
                model.Add(shortfall == need - sum(slot_assigns))
                shortfall_vars.append((st["id"], amb["id"], day, shift, need, shortfall))

    # ---- Fairness objective ----
    weekend_dates = {day for day in days if day.weekday() >= 5}
    fairness_terms = []

    totals, nights, weekends = [], [], []
    start_totals, start_nights, start_weekends = [], [], []
    shift_counts = []
    for d in drivers:
        sh = starting_hours.get(d["id"], {})
        start_total, start_night, start_weekend = sh.get("total", 0), sh.get("night", 0), sh.get("weekend", 0)
        tot = start_total + sum(work_day[(d["id"], day)] * DAY_SHIFT_HOURS
                                 + work_night[(d["id"], day)] * NIGHT_SHIFT_HOURS for day in days)
        nig = start_night + sum(work_night[(d["id"], day)] * NIGHT_SHIFT_HOURS for day in days)
        wke = start_weekend + sum(work_day[(d["id"], day)] * DAY_SHIFT_HOURS
                                   + work_night[(d["id"], day)] * NIGHT_SHIFT_HOURS for day in weekend_dates)
        shifts = sum(work_day[(d["id"], day)] + work_night[(d["id"], day)] for day in days)
        totals.append(tot); nights.append(nig); weekends.append(wke); shift_counts.append(shifts)
        start_totals.append(start_total); start_nights.append(start_night); start_weekends.append(start_weekend)

    upper_total = max(start_totals, default=0) + len(days) * NIGHT_SHIFT_HOURS
    upper_night = max(start_nights, default=0) + len(days) * NIGHT_SHIFT_HOURS
    upper_weekend = max(start_weekends, default=0) + len(weekend_dates) * NIGHT_SHIFT_HOURS
    upper_shifts = len(days)

    n = len(drivers)

    # Shift-count spread — the confirmed real target: within a single
    # leave-free week, nobody should work more than SHIFT_COUNT_SPREAD_CAP
    # extra shifts versus the least-loaded driver. Effectively-hard (heavy
    # overage penalty, capped by group size so it can never outrank
    # SHORTFALL_PENALTY), same technique as generate_roster()'s medical
    # total-hours spread cap.
    mx_shift = model.NewIntVar(0, upper_shifts, "max_shift_count")
    mn_shift = model.NewIntVar(0, upper_shifts, "min_shift_count")
    model.AddMaxEquality(mx_shift, shift_counts)
    model.AddMinEquality(mn_shift, shift_counts)
    shift_spread = model.NewIntVar(0, upper_shifts, "shift_count_spread")
    model.Add(shift_spread == mx_shift - mn_shift)
    shift_overage = model.NewIntVar(0, upper_shifts, "shift_count_overage")
    model.Add(shift_overage >= shift_spread - SHIFT_COUNT_SPREAD_CAP)
    SHIFT_COUNT_OVERAGE_PENALTY = 200
    fairness_terms.append(shift_overage * SHIFT_COUNT_OVERAGE_PENALTY * min(n, 25))

    # Per-person-above-mean on shift count keeps the WHOLE distribution
    # tight, not just the two extremes — same n-scaled dev_pos technique as
    # generate_roster()'s medical fairness objective.
    if n >= 2:
        sum_shifts = model.NewIntVar(0, upper_shifts * n, "sum_shift_count")
        model.Add(sum_shifts == sum(shift_counts))
        for idx, v in enumerate(shift_counts):
            dev = model.NewIntVar(-upper_shifts * n, upper_shifts * n, f"dev_shift_{idx}")
            model.Add(dev == v * n - sum_shifts)
            dev_pos = model.NewIntVar(0, upper_shifts * n, f"devpos_shift_{idx}")
            model.AddMaxEquality(dev_pos, [dev, 0])
            fairness_terms.append(dev_pos * PER_PERSON_ABOVE_MEAN_PENALTY)

    # Hour spread is a SOFTER secondary term, deliberately with no overage
    # cap — nudges toward tighter day/night MIX (the actual fix for the
    # 56h/40h real-incident gap: CP-SAT can put fewer nights on whoever
    # would otherwise be locked into an all-night week, by choosing their
    # whole week's pattern jointly) without ever fighting the shift-count
    # cap above, which stays the dominant, effectively-hard target.
    for label, series, weight, upper in [("total", totals, 3, upper_total),
                                          ("night", nights, 2, upper_night),
                                          ("weekend", weekends, 2, upper_weekend)]:
        mx = model.NewIntVar(0, upper, f"max_{label}_driver")
        mn = model.NewIntVar(0, upper, f"min_{label}_driver")
        model.AddMaxEquality(mx, series)
        model.AddMinEquality(mn, series)
        spread = model.NewIntVar(0, upper, f"spread_{label}_driver")
        model.Add(spread == mx - mn)
        fairness_terms.append(spread * weight)

    shortfall_penalty_terms = [s * SHORTFALL_PENALTY for (_, _, _, _, _, s) in shortfall_vars]
    model.Minimize(sum(fairness_terms) + sum(shortfall_penalty_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    assignments, gaps, driver_by_slot = [], [], {}

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        station_code = {st["id"]: st["code"] for st, _ in station_ambulance_pairs}
        amb_station = {amb["id"]: st["id"] for st, amb in station_ambulance_pairs}

        driver_by_amb_slot = defaultdict(list)  # (ambulance_id, day, shift) -> [driver_id]
        worked_ambulance = {}  # (driver_id, day, shift) -> ambulance_id
        for (did, day, shift, amb_id), var in assign.items():
            if solver.Value(var):
                driver_by_amb_slot[(amb_id, day, shift)].append(did)
                worked_ambulance[(did, day, shift)] = amb_id

        for d in drivers:
            for day in days:
                if (d["id"], day) in leaves:
                    assignments.append({"employee_id": d["id"], "day": day,
                                         "code": leaves[(d["id"], day)], "station_id": None})
                elif solver.Value(work_day[(d["id"], day)]):
                    amb_id = worked_ambulance[(d["id"], day, "day")]
                    st_id = amb_station[amb_id]
                    assignments.append({"employee_id": d["id"], "day": day,
                                         "code": f"D{station_code[st_id]}", "station_id": st_id})
                elif solver.Value(work_night[(d["id"], day)]):
                    amb_id = worked_ambulance[(d["id"], day, "night")]
                    st_id = amb_station[amb_id]
                    assignments.append({"employee_id": d["id"], "day": day,
                                         "code": f"N{station_code[st_id]}", "station_id": st_id})
                else:
                    assignments.append({"employee_id": d["id"], "day": day, "code": "O", "station_id": None})

        for (amb_id, day, shift), did_list in driver_by_amb_slot.items():
            driver_by_slot[(amb_id, day, shift)] = min(did_list)  # primary driver of record for this slot

        for (st_id, amb_id, day, shift, needed, s) in shortfall_vars:
            filled = needed - solver.Value(s)
            if filled < needed:
                gaps.append({"station_id": st_id, "ambulance_id": amb_id, "day": day, "shift": shift,
                             "needed": needed, "filled": filled})
    else:
        # Never actually expected — every hard constraint here is trivially
        # satisfiable by "nobody works anything" — but don't assume a clean
        # solve, same discipline as the rest of this file.
        for st, amb in station_ambulance_pairs:
            need = st["min_drivers_per_crew"]
            for day in days:
                for shift in ("day", "night"):
                    if shift == "night" and not st.get("night_active", True):
                        continue
                    gaps.append({"station_id": st["id"], "ambulance_id": amb["id"], "day": day, "shift": shift,
                                 "needed": need, "filled": 0})
        for d in drivers:
            for day in days:
                code = leaves[(d["id"], day)] if (d["id"], day) in leaves else "O"
                assignments.append({"employee_id": d["id"], "day": day, "code": code, "station_id": None})

    total_shifts_by_driver = {}
    total_hours_by_driver = {}
    for d in drivers:
        starting_total = starting_hours.get(d["id"], {}).get("total", 0)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            shift_count = sum(solver.Value(work_day[(d["id"], day)]) + solver.Value(work_night[(d["id"], day)])
                               for day in days)
            hour_total = starting_total + sum(
                solver.Value(work_day[(d["id"], day)]) * DAY_SHIFT_HOURS
                + solver.Value(work_night[(d["id"], day)]) * NIGHT_SHIFT_HOURS for day in days)
        else:
            shift_count = 0
            hour_total = starting_total
        total_shifts_by_driver[d["id"]] = shift_count
        total_hours_by_driver[d["id"]] = hour_total

    return assignments, gaps, driver_by_slot, total_shifts_by_driver, total_hours_by_driver


def schedule_facility_staff(staff, facility, leaves, days, starting_hours=None, boundary_state=None, own_target=None):
    """
    Fair round-robin day/night desk-coverage rotation for one Facility's fixed
    staff pool (currently just the Call Center dispatch desk) — same
    least-loaded-first mechanics as schedule_drivers(), but for a single
    facility needing `own_target` (falling back to facility["required_headcount"]
    if not given) people on each of the day and night shift, rather than one
    driver per ambulance. Produces DR/NR Regulation codes (see
    is_work_shift_code) instead of station D#/N# codes — which facility
    tracked via the returned dict's facility_id, not the code itself,
    matching the real paper roster's legend.

    own_target: Call & Dispatch's OWN independent staffing target per shift,
    confirmed directly as 2 — separate from facility["required_headcount"]
    (the desk's COMBINED total, e.g. 5), which is what PHTL's medical
    critical/anesthesiologist pool fills the REST of (see
    generate_roster's regulation-folding). Without this, Call & Dispatch's
    own scheduler would try to independently reach the full combined total
    from its own pool alone, with no concept of medical's contribution —
    real gap found via direct verification: it would put ~5 of its own 6
    people on EVERY shift, not the intended 2.

    Returns (assignments, gaps, total_shifts_by_employee, total_hours_by_employee).
    total_hours_by_employee is cumulative (starting_hours + this run).
    """
    starting_hours = starting_hours or {}
    boundary_state = boundary_state or {}
    assignments = []
    gaps = []
    state = {
        e["id"]: {
            "total": 0, "nights": 0, "weekends": 0,
            "consec": boundary_state.get(e["id"], {}).get("consecutive_before", 0),
            "resting": boundary_state.get(e["id"], {}).get("was_night_before", False),
            # worked a DAY shift the day immediately before — blocks a NIGHT
            # pick today specifically (rest BEFORE a night, not just after;
            # a Day-then-Night with no rest between is the same forbidden
            # adjacency as Night-then-Day). No boundary-run seed for this
            # one (boundary_state doesn't track "was day before"), same
            # known scope limit as elsewhere in this file.
            "worked_day_yesterday": False,
            "hours": starting_hours.get(e["id"], {}).get("total", 0),
            "night_hours": starting_hours.get(e["id"], {}).get("night", 0),
            "weekend_hours": starting_hours.get(e["id"], {}).get("weekend", 0),
        }
        for e in staff
    }
    need = own_target if own_target is not None else facility["required_headcount"]

    for day in days:
        is_weekend = day.weekday() >= 5
        on_leave_today = {e["id"] for e in staff if (e["id"], day) in leaves}
        day_assigned, night_assigned = set(), set()

        def eligible_pool(used, shift_name):
            return [e for e in staff
                    if e["id"] not in on_leave_today
                    and e["id"] not in used
                    and not state[e["id"]]["resting"]
                    and not (shift_name == "night" and state[e["id"]]["worked_day_yesterday"])
                    and state[e["id"]]["consec"] < MAX_CONSECUTIVE_DAYS]

        for shift_name, target in [("day", day_assigned), ("night", night_assigned)]:
            used = day_assigned | night_assigned
            pool = eligible_pool(used, shift_name)
            # Same shift-count spread cap as schedule_drivers — see its
            # comment for why shift count, not hours.
            floor_shifts = min((state[e["id"]]["total"] for e in staff), default=0)
            capped_pool = [e for e in pool if state[e["id"]]["total"] < floor_shifts + SHIFT_COUNT_SPREAD_CAP]
            if capped_pool:
                pool = capped_pool
            pool.sort(key=lambda e: (
                state[e["id"]]["total"],
                state[e["id"]]["hours"],
                state[e["id"]]["night_hours"] if shift_name == "night" else 0,
                state[e["id"]]["weekend_hours"] if is_weekend else 0,
            ))
            chosen = pool[:need]
            if len(chosen) < need:
                gaps.append({"facility_id": facility["id"], "day": day, "shift": shift_name,
                             "needed": need, "filled": len(chosen)})
            for e in chosen:
                target.add(e["id"])
                code = "DR" if shift_name == "day" else "NR"
                assignments.append({"employee_id": e["id"], "day": day, "code": code, "facility_id": facility["id"]})
                hrs = DAY_SHIFT_HOURS if shift_name == "day" else NIGHT_SHIFT_HOURS
                state[e["id"]]["total"] += 1
                state[e["id"]]["hours"] += hrs
                if shift_name == "night":
                    state[e["id"]]["nights"] += 1
                    state[e["id"]]["night_hours"] += hrs
                if is_weekend:
                    state[e["id"]]["weekends"] += 1
                    state[e["id"]]["weekend_hours"] += hrs

        for e in staff:
            eid = e["id"]
            if eid in on_leave_today:
                assignments.append({"employee_id": eid, "day": day, "code": leaves[(eid, day)], "facility_id": None})
                state[eid]["consec"] = 0
                state[eid]["resting"] = False
            elif eid in day_assigned or eid in night_assigned:
                state[eid]["consec"] += 1
                state[eid]["resting"] = eid in night_assigned
            else:
                assignments.append({"employee_id": eid, "day": day, "code": "O", "facility_id": None})
                state[eid]["consec"] = 0
                state[eid]["resting"] = False
            state[eid]["worked_day_yesterday"] = eid in day_assigned

    _rebalance_hours(assignments, state, [e["id"] for e in staff], days)

    total_shifts_by_employee = {e["id"]: state[e["id"]]["total"] for e in staff}
    total_hours_by_employee = {e["id"]: state[e["id"]]["hours"] for e in staff}
    return assignments, gaps, total_shifts_by_employee, total_hours_by_employee


def assign_standby(employees, need_day, need_night, leaves, days, worked_assignments, group):
    """
    Fair round-robin selection of one group's shared, district-wide standby
    pool (group is "driver" | "medical" | "call_center" — see
    STANDBY_GROUPS in models.py). Global, not per-station: real SAMU standby
    is one reserve team per group, not each station holding its own copy.
    Drawn from anyone in `employees` not already working that day/shift and
    not on leave — never violates working-hour, rest, or leave rules because
    it only ever pulls from people who are already free that shift. Rotates
    fairly using the same least-picked-first approach as the driver
    allocator; need_day=need_night=0 means no standby team for this group at
    all (opt-in, same as before).

    Returns a list of {group, day, shift, employee_id} dicts.
    """
    if need_day <= 0 and need_night <= 0:
        return []

    working_today_shift = defaultdict(set)  # (day, shift) -> {employee_id}
    for a in worked_assignments:
        code = a["code"]
        if is_work_shift_code(code):
            shift = "day" if code[0] == "D" else "night"
            working_today_shift[(a["day"], shift)].add(a["employee_id"])

    standby_count = {e["id"]: 0 for e in employees}
    result = []
    for day in days:
        on_leave_today = {e["id"] for e in employees if (e["id"], day) in leaves}
        for shift, need in (("day", need_day), ("night", need_night)):
            if need <= 0:
                continue
            already_working = working_today_shift.get((day, shift), set())
            pool = [e for e in employees
                    if e["active"] and e["id"] not in on_leave_today
                    and e["id"] not in already_working
                    and not (shift == "night" and e["restricted_night_shift"])
                    and not (shift == "day" and e["night_only"])]
            pool.sort(key=lambda e: standby_count[e["id"]])
            chosen = pool[:need]
            for e in chosen:
                result.append({"group": group, "day": day, "shift": shift, "employee_id": e["id"]})
                standby_count[e["id"]] += 1
    return result


def assign_regulation_nurses(nurses, facility, call_center_committed, leaves, days, worked_assignments):
    """
    PHTL's contribution to the Regulation desk — fills the gap between what
    Call & Dispatch actually staffed a shift and the desk's target headcount
    (facility["required_headcount"]): target = max(0, headcount -
    call_center_committed[(day, shift)]). "If C&D sent 2, PHTL sends 1" is
    just this gap-fill relationship at whatever headcount is configured, not
    a hardcoded 2:1 pair.

    Draws from the WHOLE medical pool passed in (`nurses` — name kept from
    when this was nurse-only) — confirmed directly: Regulation is just
    another shift like any other, no clinical reason to keep anesthesiologist
    /critical off it, and excluding them was actively working against the
    confirmed goal of similar total hours across the whole medical team,
    since nurses alone were absorbing 100% of this extra work. Produces real
    DR/NR Regulation work-shift codes (same as schedule_facility_staff), not
    a standby reserve.

    Everyone's primary schedule already comes from the separate CP-SAT medical
    model by the time this runs, so `worked_assignments` (that combined,
    already-fixed output) is the source of truth for who's free. Rest-after-
    night (the 0NR0/DNR0 pattern) is enforced by checking the day before and
    after directly against that fixed map — the day-before check also sees
    this function's OWN prior picks (days are processed chronologically,
    updating the map as we go), so two Regulation nights can't land back to
    back either.

    Candidates are picked least-TOTAL-HOURS-first, seeded from each nurse's
    own already-fixed medical-crew hours this run (via worked_assignments) —
    not by how many times they've personally been picked for Regulation.
    Real incident: the old "times picked for Regulation" count starts at 0
    for everyone, so on a tie (which is every single pick, since almost
    nobody had been picked yet) the sort fell back to list order, letting the
    same handful of nurses who happened to sort first — and stay eligible
    day after day — repeatedly win Regulation shifts. The CP-SAT medical
    model had already produced a tightly-balanced schedule (its own fairness
    objective verified independently); this then piled 24h of EXTRA
    Regulation work onto some of those already-balanced nurses while others
    got none, recreating the exact 24h-vs-58h spread the medical fairness
    pass was built to prevent. Balancing against real hours instead means
    Regulation duty preferentially fills in whoever's already lightest
    loaded overall, not whoever wins an arbitrary tie.
    """
    code_by_employee_day = {(a["employee_id"], a["day"]): a["code"] for a in worked_assignments}
    nurse_hours = defaultdict(int)
    # Seeded from worked_assignments (CP-SAT's own picks) so this pass
    # respects the SAME weekly cap the CP-SAT model enforces — real incident:
    # without this, a person already at the CP-SAT cap could still get MORE
    # Regulation shifts piled on by this top-up pass, which has no other way
    # to know the cap exists.
    nurse_reg_count = defaultdict(int)
    for a in worked_assignments:
        nurse_hours[a["employee_id"]] += _shift_hours(a["code"])
        if a["code"] in ("DR", "NR"):
            nurse_reg_count[a["employee_id"]] += 1
    result = []

    for day_idx, day in enumerate(days):
        on_leave_today = {e["id"] for e in nurses if (e["id"], day) in leaves}
        prev_day = days[day_idx - 1] if day_idx > 0 else None
        next_day = days[day_idx + 1] if day_idx < len(days) - 1 else None

        for shift in ("day", "night"):
            target = max(0, facility["required_headcount"] - call_center_committed.get((day, shift), 0))
            if target <= 0:
                continue

            def eligible(e):
                eid = e["id"]
                if eid in on_leave_today:
                    return False
                today_code = code_by_employee_day.get((eid, day))
                if today_code and is_work_shift_code(today_code):
                    return False  # already working (original schedule, or already picked earlier today)
                if shift == "night" and e["restricted_night_shift"]:
                    return False
                if shift == "day" and e["night_only"]:
                    return False
                # Rest-after-night applies no matter which shift we're filling
                # today — real incident: this used to only be checked when
                # picking a NEW night shift, so someone who worked a real N#
                # night (from the normal CP-SAT schedule) could still be
                # picked for a DAY Regulation shift the very next morning,
                # an impossible N-then-D back-to-back with zero rest.
                prev_code = prev_day and code_by_employee_day.get((eid, prev_day))
                if prev_code and prev_code[0] == "N":
                    return False  # no shift at all the day after any night, including a previous Regulation night
                if shift == "night":
                    next_code = next_day and code_by_employee_day.get((eid, next_day))
                    if next_code and is_work_shift_code(next_code):
                        return False  # tomorrow's already-fixed shift must stay clear for the mandated rest day
                # Rest BEFORE a night too, not just after — a Day Regulation
                # pick landing immediately before an already-fixed Night the
                # next day is the same forbidden adjacency from the other
                # side.
                if shift == "day":
                    next_code = next_day and code_by_employee_day.get((eid, next_day))
                    if next_code and next_code[0] == "N" and is_work_shift_code(next_code):
                        return False
                # Same rule, other direction: a NIGHT Regulation pick landing
                # immediately after an already-fixed Day the day before is
                # the identical forbidden D->N adjacency — real bug found via
                # full-pipeline verification (only prev-was-Night was ever
                # checked above, never prev-was-Day).
                if shift == "night" and prev_code and is_work_shift_code(prev_code):
                    return False
                # Hard weekly cap, shared with the CP-SAT model — see
                # MAX_REGULATION_SHIFTS_PER_SPECIALIST's docstring.
                if nurse_reg_count[eid] >= MAX_REGULATION_SHIFTS_PER_SPECIALIST:
                    return False
                return True

            pool = [e for e in nurses if eligible(e)]
            pool.sort(key=lambda e: nurse_hours[e["id"]])
            chosen = pool[:target]
            for e in chosen:
                code = "DR" if shift == "day" else "NR"
                result.append({"employee_id": e["id"], "day": day, "code": code, "facility_id": facility["id"]})
                nurse_hours[e["id"]] += DAY_SHIFT_HOURS if shift == "day" else NIGHT_SHIFT_HOURS
                nurse_reg_count[e["id"]] += 1
                code_by_employee_day[(e["id"], day)] = code
    return result


def _overwork_flags(employees, hours_by_employee):
    """
    Flags anyone working notably more hours than their peers — compared
    against their own group's average (drivers vs. each medical role).
    `hours_by_employee` is cumulative for the whole calendar month (this
    run's hours plus anything carried forward from other runs already
    generated this month), not just this run's own date range — so a
    lighter week is never flagged in isolation, and a month that's been
    generated piecemeal, week by week, still balances out by month end.
    """
    emp_name = {e["id"]: e["name"] for e in employees}
    by_group = defaultdict(list)
    for e in employees:
        group = "driver" if e["is_driver"] else e["role"]
        by_group[group].append(e["id"])

    flags = []
    for group, ids in by_group.items():
        if len(ids) < 2:
            continue
        hours = {i: hours_by_employee.get(i, 0) for i in ids}
        avg = sum(hours.values()) / len(ids)
        if avg <= 0:
            continue
        for i in ids:
            n = hours[i]
            if n > avg * 1.25 and (n - avg) >= DAY_SHIFT_HOURS:
                flags.append({
                    "employee_id": i, "name": emp_name.get(i, f"#{i}"), "group": group,
                    "hours": n, "group_average_hours": round(avg, 1),
                    "message": f"{emp_name.get(i, 'Employee #'+str(i))} is overworking: {n}h this month so far "
                               f"({group} average: {round(avg, 1)}h).",
                })
    return flags


def _quality_score(fairness_terms_values, driver_gaps_count, driver_slots_total,
                    stations_without_ambulances, stations_total,
                    pro_gap_count, pro_slots_total,
                    medical_shortfall_count, medical_slots_total,
                    overwork_count):
    """
    Heuristic 0-100 breakdown. Fairness/overtime are derived from the same
    role-group hour-spread the solver minimizes (there's no independently-
    computed overtime concept — overtime is reported as a proxy off the
    fairness spread, labeled as such). fairness_terms_values are in hours;
    normalized by AVG_SHIFT_HOURS before scoring so the 0-100 scale stays
    calibrated the same way it was back when spreads were shift counts.
    Coverage blends driver-slot fill rate, ambulance-provisioning gaps, Pro
    coverage, and medical crew-size fill rate.
    """
    total_penalty = (sum(fairness_terms_values) / AVG_SHIFT_HOURS) if fairness_terms_values else 0
    fairness_score = round(100 / (1 + total_penalty / 20))

    driver_fill_rate = 1.0 if driver_slots_total == 0 else 1 - (driver_gaps_count / driver_slots_total)
    ambulance_provision_rate = 1.0 if stations_total == 0 else 1 - (stations_without_ambulances / stations_total)
    pro_fill_rate = 1.0 if pro_slots_total == 0 else 1 - (pro_gap_count / pro_slots_total)
    medical_fill_rate = 1.0 if medical_slots_total == 0 else 1 - (medical_shortfall_count / medical_slots_total)
    coverage_score = round(100 * (0.35 * driver_fill_rate + 0.15 * ambulance_provision_rate
                                   + 0.2 * pro_fill_rate + 0.3 * medical_fill_rate))

    overtime_score = fairness_score  # proxy off the same spread until hour-based contracts exist
    if overwork_count:
        overtime_score = max(0, overtime_score - 10 * overwork_count)

    overall = round(0.4 * fairness_score + 0.4 * coverage_score + 0.2 * overtime_score)
    return {
        "overall": overall,
        "fairness": fairness_score,
        "coverage": coverage_score,
        "overtime": overtime_score,
        "overtime_is_proxy": True,
    }


def generate_roster(employees, stations, ambulances, leaves, start_date, end_date,
                     time_limit_seconds=1200, starting_hours=None, boundary_state=None, scope="all",
                     middle_site_codes=None, regulation_facilities=None, call_center_committed=None):
    """
    time_limit_seconds is a PER-ATTEMPT budget — see GENERATE_ROSTER_ATTEMPTS
    below for why this solves multiple times rather than once. Real incident:
    the ~81-person nurse fairness group did not converge reliably at a single
    240s or even a single 420s attempt — the SAME code and inputs produced a
    10h spread on some runs and a 30-34h spread on others, because CP-SAT's
    parallel search can land in a meaningfully worse local optimum within a
    fixed budget depending on essentially arbitrary timing, not because the
    objective weighting was wrong (it already favors a tight spread whenever
    the solver finds one). Retrying with a fresh random seed and keeping
    whichever attempt's own reported objective value is lowest reliably
    surfaces the good solution instead of gambling on one long attempt.

    scope: "all" | "drivers" | "medical" — Drivers Manager and PHTL generate
    independently now, each only computing their own half; admin can still
    generate "all" in one pass. Restricting the employee pool up front (empty
    driver or medical list) makes every downstream constraint/objective
    trivially satisfied for the skipped half — see the caller in main.py for
    how the two scopes' output gets merged into the same roster run without
    one clobbering the other.
    employees:  list of dicts {id, name, role, is_driver, is_team_leader, station_id,
                                restricted_night_shift, night_only, is_pro, floats_freely}
    stations:   list of dicts {id, code, day_crew_size, night_crew_size, min_drivers_per_crew}
    ambulances: list of dicts {id, station_id, status}
    leaves:     dict[(employee_id, date)] -> leave_code
    middle_site_codes: optional set of Station.code values — when given, medical
    staff with role in ("anesthesiologist", "critical") are HARD-restricted to
    ambulances at those stations only (they're the specialists cleared for tough
    interventions, kept positioned centrally rather than scattered). A soft
    objective term additionally nudges each day/shift toward having at least one
    of them present somewhere in that set, so they don't cluster into one
    shift/station while another goes without. None/empty means no restriction.
    regulation_facilities / call_center_committed: fold PHTL's Regulation-desk
    gap-fill directly into this same solve instead of a separate greedy pass
    afterward — confirmed as the real fix after extensive tracing: a
    CP-SAT-only field schedule plus a layered-on-afterward Regulation fill
    plus a local swap cleanup could not reliably close the combined-hours
    spread below ~14-18h, because by the time Regulation hours get added the
    field pattern (who's off which days) is already fixed, and some people's
    fixed pattern structurally has zero legal days left to absorb more hours
    (e.g. two shifts already at the MAX_CONSECUTIVE_DAYS cap plus a night
    needing rest on both sides) — a real, provably permanent block, not a bug
    in the swap search. Solving field + Regulation jointly lets the solver
    place someone's days off with Regulation hours already in mind.
    regulation_facilities: list of dicts {id, required_headcount}.
    call_center_committed: dict {(facility_id, date, "day"|"night"): int} —
    how many Call & Dispatch staff are ALREADY covering that facility's shift
    (read fresh from the DB before this solve); PHTL's medical pool only
    needs to cover required_headcount minus that. None/empty means no
    Regulation-desk folding (e.g. a "drivers"-only scope).
    start_date, end_date: inclusive date range to schedule (a day, a week, a month — any span)
    starting_hours: optional {employee_id: {"total","night","weekend"}} carried forward
    from other roster runs already generated this calendar month (see main.py's
    _starting_hours_for_month) — both the fairness objective and the overwork
    flags balance against this month-to-date total, not just this run's own days.
    boundary_state: optional {employee_id: {"was_night_before": bool, "consecutive_before": int}}
    — what this person was doing the day immediately before start_date, from
    ANY other roster run (see main.py's _boundary_state_for_run). Without this,
    the rest-after-night and max-consecutive-days rules only see days inside
    THIS run, so generating week by week could schedule a day shift the
    morning after a night shift just because that night shift happened to
    fall in the previous run — a real rule violation, not a cosmetic one.
    Both hard constraints below extend across that boundary.
    Returns: (status_str, assignments, gaps, crew_assignments, quality, standby_assignments)

    Medical staff (nurses/paramedics/anesthesiologists/etc.) are optimized with
    CP-SAT, assigned to ambulance crews — exact math, provably fair. Drivers
    are handled by a separate fair round-robin allocator (see schedule_drivers)
    for the same reason as before: too many interchangeable drivers for
    CP-SAT to search efficiently at this problem size.
    """
    boundary_state = boundary_state or {}
    starting_hours = starting_hours or {}
    days = date_range(start_date, end_date)
    model = cp_model.CpModel()

    drivers = [e for e in employees if e["is_driver"]] if scope in ("all", "drivers") else []
    medical = [e for e in employees if not e["is_driver"]] if scope in ("all", "medical") else []

    ambulances_by_station = defaultdict(list)
    active_ambulances = []
    for a in ambulances:
        if a["status"] == "active" and a["station_id"] is not None:
            ambulances_by_station[a["station_id"]].append(a)
            active_ambulances.append(a)

    station_code_by_id = {st["id"]: st["code"] for st in stations}
    SPECIALIST_ROLES = ("anesthesiologist", "critical")

    work_day = {}
    work_night = {}
    assign = {}  # (employee_id, day, shift, ambulance_id) -> BoolVar
    reg_assign = {}  # (employee_id, day, shift, facility_id) -> BoolVar
    regulation_facilities = regulation_facilities or []
    call_center_committed = call_center_committed or {}

    # Medical staff can be assigned to ANY active ambulance system-wide (not just
    # their home station's), same as drivers already float. A heavy soft penalty
    # keeps them at home by default — floats_freely=True employees are exempt.
    HOME_STATION_PENALTY = 50
    home_penalty_terms = []

    for e in medical:
        for d in days:
            wd = model.NewBoolVar(f"wd_{e['id']}_{d}")
            wn = model.NewBoolVar(f"wn_{e['id']}_{d}")
            work_day[(e["id"], d)] = wd
            work_night[(e["id"], d)] = wn
            for shift, var in (("day", wd), ("night", wn)):
                shift_assigns = []
                for amb in active_ambulances:
                    v = model.NewBoolVar(f"a_{e['id']}_{d}_{shift}_{amb['id']}")
                    assign[(e["id"], d, shift, amb["id"])] = v
                    shift_assigns.append(v)
                    if (middle_site_codes and e["role"] in SPECIALIST_ROLES
                            and station_code_by_id.get(amb["station_id"]) not in middle_site_codes):
                        model.Add(v == 0)
                    # Specialists hard-restricted to the middle-site set (above) are
                    # exempt from the home-station penalty entirely — real incident:
                    # most anesthesiologists/critical staff have a home_station that
                    # ISN'T one of the 4 allowed stations, so every legal placement
                    # they could ever get already counted as "away from home" and
                    # got penalized, while nurses (unrestricted, often already home
                    # at a middle site or floats_freely) filled the same seats for
                    # free. That stacked with the hard restriction to leave 20
                    # specialists averaging ~11h/week while nurses averaged 46h —
                    # the whole point of positioning them centrally is that they're
                    # expected to move between those 4 stations as needed, not stay
                    # anchored to whichever one happens to match their old home.
                    is_restricted_specialist = middle_site_codes and e["role"] in SPECIALIST_ROLES
                    away_from_home = e["station_id"] is not None and amb["station_id"] != e["station_id"]
                    if away_from_home and not e["floats_freely"] and not is_restricted_specialist:
                        home_penalty_terms.append(v * HOME_STATION_PENALTY)
                # Regulation-desk is just another destination for this same
                # worked shift, same as an ambulance — no site restriction
                # (it's a call-center desk, not tied to any station) and no
                # home-station penalty. Restricted to critical/anesthesiologist
                # ONLY — confirmed directly, correcting an earlier "open to
                # the whole medical pool" decision: PHTL's Regulation
                # contribution must be a specialist, not a plain nurse. Tying
                # it into the same shift_assigns/== var equality means an
                # employee can land on AT MOST ONE of (an ambulance, a
                # Regulation desk) per shift, automatically.
                if e["role"] in SPECIALIST_ROLES:
                    for fac in regulation_facilities:
                        rv = model.NewBoolVar(f"reg_{e['id']}_{d}_{shift}_{fac['id']}")
                        reg_assign[(e["id"], d, shift, fac["id"])] = rv
                        shift_assigns.append(rv)
                model.Add(sum(shift_assigns) == var)  # exactly one ambulance/desk per employee per worked shift

    for e in medical:
        for d in days:
            wd, wn = work_day[(e["id"], d)], work_night[(e["id"], d)]
            if (e["id"], d) in leaves:
                model.Add(wd == 0)
                model.Add(wn == 0)
            else:
                model.Add(wd + wn <= 1)
            if e["restricted_night_shift"]:
                model.Add(wn == 0)
            if e["night_only"]:
                model.Add(wd == 0)

    for e in medical:
        for i, d in enumerate(days[:-1]):
            nxt = days[i + 1]
            model.Add(work_day[(e["id"], nxt)] == 0).OnlyEnforceIf(work_night[(e["id"], d)])
            model.Add(work_night[(e["id"], nxt)] == 0).OnlyEnforceIf(work_night[(e["id"], d)])
            # Rest BEFORE a night shift too, not just after — confirmed
            # directly: a night must have an off day immediately before AND
            # after, never a straight Day-then-Night with no rest between
            # (back-to-back Night-then-Night, up to MAX_CONSECUTIVE_DAYS, is
            # still fine — that's a separate, already-confirmed rule; this
            # is specifically about never mixing D and N adjacently in
            # either direction). Real gap: only the "after" half of this was
            # ever enforced, so a Day shift immediately followed by a Night
            # shift was never actually blocked.
            model.Add(work_day[(e["id"], d)] == 0).OnlyEnforceIf(work_night[(e["id"], nxt)])
        # same rest rule, extended across the run boundary: if this person
        # worked a night shift the day before start_date (in some OTHER
        # run), day 1 of THIS run must be off too.
        if boundary_state.get(e["id"], {}).get("was_night_before"):
            model.Add(work_day[(e["id"], days[0])] == 0)
            model.Add(work_night[(e["id"], days[0])] == 0)

    for e in medical:
        for i in range(len(days) - MAX_CONSECUTIVE_DAYS):
            window = days[i:i + MAX_CONSECUTIVE_DAYS + 1]
            worked = [work_day[(e["id"], d)] + work_night[(e["id"], d)] for d in window]
            model.Add(sum(worked) <= MAX_CONSECUTIVE_DAYS)
        # extend the cap across the boundary: if they already arrived at
        # day 1 having worked `consecutive_before` days in a row (from some
        # OTHER run), they can only work MAX_CONSECUTIVE_DAYS - that many
        # more before a forced day off — same rule, just carried forward.
        consec_before = boundary_state.get(e["id"], {}).get("consecutive_before", 0)
        if consec_before > 0:
            allowed_more = max(MAX_CONSECUTIVE_DAYS - consec_before, 0)
            lead_window = days[:min(allowed_more + 1, len(days))]
            if lead_window:
                worked = [work_day[(e["id"], d)] + work_night[(e["id"], d)] for d in lead_window]
                model.Add(sum(worked) <= allowed_more)

    # Soft nudge toward a genuine day/night MIX per person, not just a
    # balanced total across the group — real incident: the hours-fairness
    # objective alone was satisfied by several people working ALL-day and
    # several others ALL-night that same week, with nobody flagged
    # restricted_night_shift/night_only, since total hours can land the same
    # way regardless of shift-type mix. Only applies to whichever type
    # someone isn't hard-restricted from — someone genuinely restricted to
    # one type is correctly exempt, not penalized for something they can't
    # do. Doesn't distort anyone whose OPTIMAL total ends up at exactly 1
    # shift (they'd be missing one type either way, so the penalty is a
    # constant that doesn't bias which type they get) — it only bites once
    # someone has 2+ shifts and could plausibly cover both types.
    MIX_PENALTY = 900
    mix_penalty_terms = []
    mix_violation_vars = []  # raw BoolVars (not weighted) — used by attempt selection below
    specialist_reg_violation_vars = []  # tracked SEPARATELY — own tier in attempt selection below
    for e in medical:
        day_sum = sum(work_day[(e["id"], d)] for d in days)
        night_sum = sum(work_night[(e["id"], d)] for d in days)
        if not e["night_only"]:
            has_day = model.NewBoolVar(f"hasday_{e['id']}")
            model.Add(day_sum >= 1).OnlyEnforceIf(has_day)
            model.Add(day_sum == 0).OnlyEnforceIf(has_day.Not())
            mix_penalty_terms.append(has_day.Not() * MIX_PENALTY)
            mix_violation_vars.append(has_day.Not())
        if not e["restricted_night_shift"]:
            has_night = model.NewBoolVar(f"hasnight_{e['id']}")
            model.Add(night_sum >= 1).OnlyEnforceIf(has_night)
            model.Add(night_sum == 0).OnlyEnforceIf(has_night.Not())
            mix_penalty_terms.append(has_night.Not() * MIX_PENALTY)
            mix_violation_vars.append(has_night.Not())

    # Specialists (critical/anesthesiologist) must ALSO work ambulance duty
    # as team leads at the middle sites, never exclusively Regulation — real
    # incident this fixes: capping how many specialists Regulation pulls PER
    # SHIFT (see MEDICAL_REGULATION_CAP below) wasn't enough on its own — 9
    # specialists still ended up 100% Regulation for the ENTIRE week, zero
    # ambulance shifts ever, because nothing looked at any one person's
    # whole-week pattern, only the per-shift headcount. Same soft-but-heavy,
    # never-blocks-generation mix mechanism as day/night above, for
    # ambulance-vs-Regulation instead. Only fires for someone who actually
    # worked Regulation at all this week — someone who never touched it
    # isn't "Regulation-only" just because they had few/no ambulance shifts
    # (e.g. mostly on leave), so this never manufactures a false penalty.
    # Weighted well above MIX_PENALTY (900) — real incident: at the SAME
    # weight as day/night mix, this barely moved the needle (9 -> 8
    # violations), meaning the competing pressure from Regulation's own
    # shortfall-avoidance is stronger here than it is for day/night mix.
    # Kept below CREW_SHORTFALL_PENALTY (100000) so a genuine coverage need
    # still always wins, but above PRO_GAP_PENALTY/SPREAD_OVERAGE so it
    # actually dominates the fairness/spread terms it was losing to.
    SPECIALIST_AMBULANCE_MIX_PENALTY = 10000
    if regulation_facilities:
        for e in medical:
            if e["role"] not in SPECIALIST_ROLES:
                continue
            ambulance_sum = sum(assign[(e["id"], d, shift, amb["id"])]
                                 for d in days for shift in ("day", "night") for amb in active_ambulances)
            reg_sum = sum(reg_assign[(e["id"], d, shift, fac["id"])]
                          for d in days for shift in ("day", "night") for fac in regulation_facilities)
            # HARD cap on regulation shifts per specialist per week — the
            # real fix, not just a nudge. Real incident: the reg_only_violation
            # penalty below only checks "did they touch an ambulance AT ALL",
            # which the solver could satisfy cheaply by having MOST
            # specialists do 100% ambulance (never touching Regulation, so
            # never triggering the penalty) while concentrating Regulation
            # onto a small sacrificial group instead — confirmed directly:
            # lowering MEDICAL_REGULATION_CAP (per-shift headcount) made this
            # WORSE (6 -> 7 regulation-only people), because it left MORE
            # total weekly Regulation demand needing to be covered by fewer
            # effective slots, so the solver leaned harder into concentration.
            # Nothing before this actually LIMITED how much Regulation one
            # person could absorb — this does, forcing real distribution
            # across the pool instead of relying on a penalty that a
            # concentrate-onto-a-few strategy could route around.
            model.Add(reg_sum <= MAX_REGULATION_SHIFTS_PER_SPECIALIST)
            has_ambulance = model.NewBoolVar(f"hasamb_{e['id']}")
            model.Add(ambulance_sum >= 1).OnlyEnforceIf(has_ambulance)
            model.Add(ambulance_sum == 0).OnlyEnforceIf(has_ambulance.Not())
            worked_reg = model.NewBoolVar(f"workedreg_{e['id']}")
            model.Add(reg_sum >= 1).OnlyEnforceIf(worked_reg)
            model.Add(reg_sum == 0).OnlyEnforceIf(worked_reg.Not())
            reg_only_violation = model.NewBoolVar(f"regonly_{e['id']}")
            model.Add(reg_only_violation >= worked_reg - has_ambulance)
            mix_penalty_terms.append(reg_only_violation * SPECIALIST_AMBULANCE_MIX_PENALTY)
            specialist_reg_violation_vars.append(reg_only_violation)

    # Per-ambulance crew requirement. Both the target crew size AND the
    # Pro/Junior mix rule (>=1 Pro per crew) are heavily-weighted SOFT
    # constraints, not hard ones: a tight day/station with too few available
    # people (leave, rest rules, real staffing shortage) would otherwise make
    # the ENTIRE roster fail to generate. Understaffing is strongly
    # discouraged and reported explicitly — never silently dropped, and
    # never allowed to block the roster from being produced at all.
    PRO_GAP_PENALTY = 1000
    # Effectively-hard cap on how far apart total monthly hours can sit within
    # a single role group — see the fairness objective below for how it's
    # applied. Weighted well above PRO_GAP_PENALTY so the solver strongly
    # resists exceeding it, but still below CREW_SHORTFALL_PENALTY so a real
    # coverage shortage is never made worse just to protect the spread.
    SPREAD_OVERAGE_PENALTY = 200
    # Every shift should come out at its own station's configured size
    # (day_crew_size/night_crew_size) whenever the eligible pool makes that
    # possible at all. This has to outweigh not just one fairness/pro-gap
    # trade-off but the SUM of every other penalty term the solver could rack
    # up elsewhere in the same run (148+ people, month-long fairness spreads
    # easily reach the thousands) — otherwise a mathematically "cheaper"
    # solution could leave a seat empty to shave a little fairness spread
    # somewhere else, even when a fully-staffed alternative exists. Kept as a
    # soft constraint, not a hard one, so a genuine staffing shortage still
    # produces a (clearly reported) best-effort roster instead of failing to
    # generate at all.
    CREW_SHORTFALL_PENALTY = 100000
    pro_gap_vars = []       # (station_id, ambulance_id, day, shift, violation BoolVar)
    shortfall_vars = []     # (station_id, ambulance_id, day, shift, needed, shortfall IntVar)
    stations_without_ambulances = 0
    medical_slots_total = 0
    for st in stations:
        amb_list = ambulances_by_station.get(st["id"], [])
        if not amb_list:
            stations_without_ambulances += 1
            continue
        crew = medical  # any medical staffer is eligible system-wide; HOME_STATION_PENALTY keeps it realistic
        med_day_needed = max(st["day_crew_size"] - st["min_drivers_per_crew"], 0)
        # night_active=False stations (day-shift-only) never get a night crew, regardless of their configured night_crew_size
        med_night_needed = max(st["night_crew_size"] - st["min_drivers_per_crew"], 0) if st.get("night_active", True) else 0
        for amb in amb_list:
            for d in days:
                day_assigns = [assign[(e["id"], d, "day", amb["id"])] for e in crew]
                night_assigns = [assign[(e["id"], d, "night", amb["id"])] for e in crew]
                model.Add(sum(day_assigns) <= med_day_needed)
                model.Add(sum(night_assigns) <= med_night_needed)
                for shift_name, shift_assigns, needed in (("day", day_assigns, med_day_needed),
                                                            ("night", night_assigns, med_night_needed)):
                    if needed == 0:
                        continue
                    medical_slots_total += needed
                    shortfall = model.NewIntVar(0, needed, f"short_{st['id']}_{amb['id']}_{d}_{shift_name}")
                    model.Add(shortfall == needed - sum(shift_assigns))
                    shortfall_vars.append((st["id"], amb["id"], d, shift_name, needed, shortfall))

                    pro_terms = [a for a, e in zip(shift_assigns, crew) if e["is_pro"]]
                    if not pro_terms:
                        continue  # station has zero Pro staff at all — nothing the solver can do about it
                    gap = model.NewBoolVar(f"progap_{st['id']}_{amb['id']}_{d}_{shift_name}")
                    model.Add(sum(pro_terms) + gap >= 1)
                    pro_gap_vars.append((st["id"], amb["id"], d, shift_name, gap))

    # Regulation-desk target per facility/day/shift: PHTL's medical pool only
    # needs to cover whatever Call & Dispatch hasn't already committed — but
    # capped at MEDICAL_REGULATION_CAP regardless of how big that gap gets.
    # Real incident this fixes: with an uncapped target, a night Call &
    # Dispatch couldn't staff at all (target = the full desk headcount) pulled
    # EVERY available critical/anesthesiologist onto Regulation to chase the
    # CREW_SHORTFALL_PENALTY-scale shortfall — leaving zero specialists on
    # any middle-site ambulance that night. Confirmed directly: critical/
    # anesthesiologist staff also serve as ambulance team leads at the middle
    # sites (see MIDDLE_SITE_CODES) and must never be fully absorbed by
    # Regulation. The shortfall is still measured against the FULL gap (not
    # the capped one), so a genuine desk shortage beyond what's safe to pull
    # from the specialist pool is still honestly reported, not silently
    # hidden — it just isn't chased at the cost of ambulance coverage.
    # Confirmed directly, lowered from 3 to 2 — a real contributor to the
    # regulation-only problem: at cap 3, the demand for specialist-Regulation
    # coverage across the week was high enough that the same handful of
    # people kept getting picked shift after shift, leaving little room for
    # anyone to also get an ambulance shift. Cap 2 reduces that demand.
    MEDICAL_REGULATION_CAP = 2
    regulation_shortfall_vars = []  # (facility_id, day, shift, full_target, shortfall IntVar)
    for fac in regulation_facilities:
        for d in days:
            for shift_name in ("day", "night"):
                committed = call_center_committed.get((fac["id"], d, shift_name), 0)
                full_target = max(0, fac["required_headcount"] - committed)
                medical_cap = min(full_target, MEDICAL_REGULATION_CAP)
                reg_vars = [reg_assign[(e["id"], d, shift_name, fac["id"])]
                            for e in medical if e["role"] in SPECIALIST_ROLES]
                if not reg_vars:
                    continue
                if medical_cap == 0:
                    # Only reachable when full_target is itself 0 (medical_cap
                    # = min(full_target, MEDICAL_REGULATION_CAP), and CAP > 0)
                    # — nothing to fill, nothing to flag as short.
                    for v in reg_vars:
                        model.Add(v == 0)
                    continue
                model.Add(sum(reg_vars) <= medical_cap)
                shortfall = model.NewIntVar(0, full_target, f"regshort_{fac['id']}_{d}_{shift_name}")
                model.Add(shortfall == full_target - sum(reg_vars))
                regulation_shortfall_vars.append((fac["id"], d, shift_name, full_target, shortfall))
                # NOT added to medical_slots_total — confirmed directly:
                # Regulation is a call-center desk, not an ambulance crew
                # seat, and must never be folded into ambulance coverage
                # numbers or count toward "each shift/station has a full
                # crew". Tracked and reported entirely separately below.

    # ---- Fairness objective ----
    weekend_dates = {d for d in days if d.weekday() >= 5}
    fairness_terms = []
    # Subset of fairness_terms used to compute the displayed 0-100 fairness
    # score: just the max-min spreads, kept at the same scale the score's
    # calibration was tuned against. The per-person above-mean penalty below
    # is scaled by group size (n) to give it real bite in the objective, which
    # would otherwise blow up the score's raw-penalty-magnitude math even
    # when the real-world hour spread gets *better* — so it shapes the
    # solve but isn't fed into the score.
    spread_terms_for_score = []
    # Per-role total-hours spread vars, kept aside from the objective so a
    # multi-attempt solve (see below) can pick the attempt with the best
    # actual fairness spread directly, rather than trusting the raw
    # objective value as a proxy for it — real incident: the objective also
    # bundles home-station preference, Pro-gap, and shortfall penalties,
    # which can vary enough between attempts to make "lowest total
    # objective" pick a solution with WORSE nurse-group spread than a
    # higher-objective alternative had.
    total_spread_by_role = {}
    totals_by_role = {}  # role -> list of per-person total-hours LinearExprs, for the outlier-count selection metric below

    # ALL medical staff are ONE fairness group, regardless of specialty —
    # confirmed directly: hour totals should end up similar across the whole
    # medical team, not just within each specialty. This used to split by
    # role (anesthesiologist vs critical vs nurse balanced separately), which
    # kept each specialty tight among its own peers but left anesthesiologist/
    # critical — hard-restricted to only 4 stations (see middle_site_codes
    # above) — sitting at a collectively much LOWER hour range than nurses
    # (who can work any of the 9 stations) even though nothing about that was
    # "unfair" under a role-scoped definition. A single combined group makes
    # the solver actively pull specialist hours up (and/or nurse hours down)
    # toward the same overall range instead of only ever comparing each
    # specialty to itself.
    by_role = {"all_medical": list(medical)} if medical else {}

    for role, group in by_role.items():
        if len(group) < 2:
            continue
        totals, nights, weekends = [], [], []
        start_totals, start_nights, start_weekends = [], [], []
        for e in group:
            sh = starting_hours.get(e["id"], {})
            start_total, start_night, start_weekend = sh.get("total", 0), sh.get("night", 0), sh.get("weekend", 0)
            # Hours, not shift counts, and seeded with whatever this person
            # already worked elsewhere in the month — so the solver balances
            # cumulative monthly hours, not just this run's own days.
            tot = start_total + sum(work_day[(e["id"], d)] * DAY_SHIFT_HOURS
                                     + work_night[(e["id"], d)] * NIGHT_SHIFT_HOURS for d in days)
            nig = start_night + sum(work_night[(e["id"], d)] * NIGHT_SHIFT_HOURS for d in days)
            wke = start_weekend + sum(work_day[(e["id"], d)] * DAY_SHIFT_HOURS
                                       + work_night[(e["id"], d)] * NIGHT_SHIFT_HOURS for d in weekend_dates)
            totals.append(tot); nights.append(nig); weekends.append(wke)
            start_totals.append(start_total); start_nights.append(start_night); start_weekends.append(start_weekend)

        upper_total = max(start_totals, default=0) + len(days) * NIGHT_SHIFT_HOURS
        upper_night = max(start_nights, default=0) + len(days) * NIGHT_SHIFT_HOURS
        upper_weekend = max(start_weekends, default=0) + len(weekend_dates) * NIGHT_SHIFT_HOURS

        for label, series, weight, upper in [("total", totals, 10, upper_total),
                                              ("night", nights, 5, upper_night),
                                              ("weekend", weekends, 5, upper_weekend)]:
            mx = model.NewIntVar(0, upper, f"max_{label}_{role}")
            mn = model.NewIntVar(0, upper, f"min_{label}_{role}")
            model.AddMaxEquality(mx, series)
            model.AddMinEquality(mn, series)
            spread = model.NewIntVar(0, upper, f"spread_{label}_{role}")
            model.Add(spread == mx - mn)
            fairness_terms.append(spread * weight)
            spread_terms_for_score.append(spread * weight)
            if label == "total":
                total_spread_by_role[role] = spread
                totals_by_role[role] = totals

            # Effectively-hard cap on total-hours spread within a role group:
            # any hour of spread beyond SPREAD_CAP_HOURS costs far more than
            # the soft spread term above (but still less than a real crew
            # shortfall), so the solver treats "keep the group's gap under the
            # cap" as close to non-negotiable, only exceeding it when there's
            # truly no other way to cover a shift — same "soft but very
            # strongly weighted, never blocks generation outright" philosophy
            # as CREW_SHORTFALL_PENALTY/PRO_GAP_PENALTY above.
            #
            # Scaled by group size (n, capped), same reason the per-person-
            # above-mean term below is: real incident, this used to be a flat
            # weight and worked fine for the small anesthesiologist/critical
            # groups (10, 7 people) but was completely swamped for the
            # 81-person nurse group, where the per-person-above-mean term
            # alone (itself n-scaled) runs into the thousands — a flat
            # penalty here had no real influence on that group's outcome at
            # all. The cap on n keeps this from ever outranking
            # CREW_SHORTFALL_PENALTY for a very large group — a real coverage
            # gap must always still win over a perfect spread.
            if label == "total":
                overage = model.NewIntVar(0, upper, f"spread_overage_{role}")
                model.Add(overage >= spread - SPREAD_CAP_HOURS)
                fairness_terms.append(overage * SPREAD_OVERAGE_PENALTY * min(len(group), 25))

        # The spread term above only constrains the single worst-vs-best pair —
        # for a large single role group (~100+ people, as in the real
        # anesthesiologist_nurse pool) that leaves the entire middle of the
        # distribution free to cluster near the top, since nobody but the two
        # extremes is penalized. Add a per-person penalty for sitting above the
        # group's own average total hours, so every individual is pulled toward
        # the mean, not just whoever happens to be the single max. n*value vs
        # sum comparisons avoid fractional averages in the integer CP-SAT model.
        #
        # ALSO penalize sitting BELOW the mean, symmetrically — real gap found
        # via direct tracing: this used to only push people DOWN toward the
        # mean from above, never UP from below, so someone whose week's shifts
        # happened to land in a pattern that structurally traps them at a low
        # total (e.g. two shifts already at the MAX_CONSECUTIVE_DAYS cap plus
        # an isolated night needing rest on both sides — confirmed a real,
        # provably permanent case this way) had nothing in the OBJECTIVE
        # steering the solver away from ever producing that pattern in the
        # first place. A post-hoc swap pass can't fix it after the fact
        # either, since it can only trade EXISTING shifts, never reshape which
        # days someone is off — this term is the only lever that can actually
        # prevent the trap, by making the solver see it coming during the
        # solve itself.
        n = len(totals)
        if n >= 2:
            sum_total = model.NewIntVar(0, upper_total * n, f"sum_total_{role}")
            model.Add(sum_total == sum(totals))
            for idx, v in enumerate(totals):
                dev = model.NewIntVar(-upper_total * n, upper_total * n, f"dev_total_{role}_{idx}")
                model.Add(dev == v * n - sum_total)
                dev_pos = model.NewIntVar(0, upper_total * n, f"devpos_total_{role}_{idx}")
                model.AddMaxEquality(dev_pos, [dev, 0])
                fairness_terms.append(dev_pos * PER_PERSON_ABOVE_MEAN_PENALTY)
                dev_neg = model.NewIntVar(0, upper_total * n, f"devneg_total_{role}_{idx}")
                model.AddMaxEquality(dev_neg, [-dev, 0])
                fairness_terms.append(dev_neg * PER_PERSON_ABOVE_MEAN_PENALTY)

    pro_gap_penalty_terms = [gap * PRO_GAP_PENALTY for (_, _, _, _, gap) in pro_gap_vars]
    shortfall_penalty_terms = [s * CREW_SHORTFALL_PENALTY for (_, _, _, _, _, s) in shortfall_vars]
    shortfall_penalty_terms += [s * CREW_SHORTFALL_PENALTY for (_, _, _, _, s) in regulation_shortfall_vars]

    # Soft nudge: at least one anesthesiologist/critical present somewhere
    # across the middle sites each day/shift, when any are scheduled at all
    # that day — same "gap variable + moderate penalty" trick as the Pro-gap
    # terms above. Deliberately soft and deliberately no opposing "too many in
    # one shift" cap: the hard site restriction above already concentrates
    # this small pool into 4 stations, so an additional hard anti-clustering
    # constraint risks making the model infeasible on a real shortage day.
    SPECIALIST_SPREAD_PENALTY = 300
    specialist_gap_terms = []
    if middle_site_codes:
        specialists = [e for e in medical if e["role"] in SPECIALIST_ROLES]
        middle_ambulances = [amb for amb in active_ambulances
                              if station_code_by_id.get(amb["station_id"]) in middle_site_codes]
        if specialists and middle_ambulances:
            for d in days:
                for shift in ("day", "night"):
                    present_vars = [assign[(e["id"], d, shift, amb["id"])]
                                     for e in specialists for amb in middle_ambulances]
                    none_present = model.NewBoolVar(f"nospec_{d}_{shift}")
                    model.Add(sum(present_vars) + none_present >= 1)
                    specialist_gap_terms.append(none_present * SPECIALIST_SPREAD_PENALTY)

    model.Minimize(sum(fairness_terms) + sum(pro_gap_penalty_terms)
                    + sum(shortfall_penalty_terms) + sum(home_penalty_terms)
                    + sum(specialist_gap_terms) + sum(mix_penalty_terms))

    # Multiple shorter attempts with different random seeds, keeping
    # whichever converges to the lowest objective value — a real incident
    # showed a SINGLE attempt isn't reliable at any practical time budget:
    # the identical model/inputs produced a 10h nurse-group spread on some
    # runs and a 30-34h spread on others, at both 240s and 420s single
    # attempts, because CP-SAT's parallel search can land in a meaningfully
    # worse local optimum within a fixed budget depending on essentially
    # arbitrary timing. Solving several times and comparing the actual
    # achieved objective value (CP-SAT reports this directly — no need to
    # separately recompute spread here) reliably surfaces the good solution
    # instead of gambling on one long attempt.
    # Outlier-count metric for attempt selection: how many people land more
    # than FAIRNESS_BAND_HALF_WIDTH hours from their own group's average —
    # confirmed directly as the right secondary metric: max-min "spread" is
    # dominated by a single low and single high person and can't distinguish
    # "everyone's spread out" from "91% of people are tightly clustered and
    # 2-3 outliers are dragging the number up" — which turned out to be
    # exactly what was happening. Minimizing THIS count, not just the raw
    # spread, is what actually shrinks the outlier group run over run.
    FAIRNESS_BAND_HALF_WIDTH = 5

    best_solver, best_status, best_key = None, None, None
    for attempt in range(GENERATE_ROSTER_ATTEMPTS):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_limit_seconds
        solver.parameters.num_search_workers = 8
        solver.parameters.random_seed = attempt + 1
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            continue
        # Selection key, in priority order, confirmed directly: (1) total
        # shortfall across BOTH ambulance crews and Regulation-desk — real
        # coverage always wins over fairness, no exceptions; (2) specialist
        # Regulation-only violations (a critical/anesthesiologist worked
        # Regulation all week with ZERO ambulance shifts) — kept as its OWN
        # tier, separate from day/night mix, after a real regression: when
        # bundled into the same mix_violations count, an attempt with 0
        # day/night-mix but 8 specialist violations beat one with 0 and 1
        # specialist violation just because its hour spread happened to be
        # tighter — the combined count couldn't tell "which kind" of
        # violation was cheaper to trade away; (3) day/night mix violations
        # — a standing hard requirement ("we want to mix"); (4) worst
        # (max-min) role-group hour spread; (5) count of people outside the
        # tight band around their group's own average; (6) total objective
        # value as a final tiebreaker. Each tier only breaks ties within the
        # tier above it.
        total_shortfall = (sum(solver.Value(s) for (_, _, _, _, _, s) in shortfall_vars)
                            + sum(solver.Value(s) for (_, _, _, _, s) in regulation_shortfall_vars))
        specialist_reg_violations = sum(solver.Value(v) for v in specialist_reg_violation_vars)
        mix_violations = sum(solver.Value(v) for v in mix_violation_vars)
        worst_spread = max((solver.Value(v) for v in total_spread_by_role.values()), default=0)
        outside_band = 0
        for role, totals in totals_by_role.items():
            if not totals:
                continue
            vals = [solver.Value(v) for v in totals]
            mean = sum(vals) / len(vals)
            outside_band += sum(1 for v in vals if abs(v - mean) > FAIRNESS_BAND_HALF_WIDTH)
        key = (total_shortfall, specialist_reg_violations, mix_violations, worst_spread, outside_band,
               solver.ObjectiveValue())
        if best_key is None or key < best_key:
            best_solver, best_status, best_key = solver, status, key
        if (status == cp_model.OPTIMAL and total_shortfall == 0 and specialist_reg_violations == 0
                and mix_violations == 0 and worst_spread <= SPREAD_CAP_HOURS):
            break  # proven optimal, fully staffed, clean mix, and within the fairness cap — no reason to keep searching
    solver = best_solver if best_solver is not None else solver
    status = best_status if best_status is not None else status

    status_name = solver.StatusName(status)
    assignments = []
    gaps = []
    crew_assignments = []
    quality = None

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        station_code = {st["id"]: st["code"] for st in stations}
        amb_station = {a["id"]: a["station_id"] for a in active_ambulances}

        # medical crew per ambulance/day/shift, and the reverse lookup — which
        # ambulance (and therefore which station) each employee actually worked,
        # since floating means that's no longer always their home station
        medical_by_slot = defaultdict(list)  # (ambulance_id, day, shift) -> [employee_id]
        worked_ambulance = {}  # (employee_id, day, shift) -> ambulance_id
        for (eid, d, shift, amb_id), var in assign.items():
            if solver.Value(var):
                medical_by_slot[(amb_id, d, shift)].append(eid)
                worked_ambulance[(eid, d, shift)] = amb_id

        worked_regulation = {}  # (employee_id, day, shift) -> facility_id
        for (eid, d, shift, fac_id), var in reg_assign.items():
            if solver.Value(var):
                worked_regulation[(eid, d, shift)] = fac_id

        for e in medical:
            for d in days:
                if (e["id"], d) in leaves:
                    assignments.append({"employee_id": e["id"], "day": d,
                                         "code": leaves[(e["id"], d)], "station_id": None})
                elif solver.Value(work_day[(e["id"], d)]):
                    if (e["id"], d, "day") in worked_ambulance:
                        amb_id = worked_ambulance[(e["id"], d, "day")]
                        st_id = amb_station[amb_id]
                        assignments.append({"employee_id": e["id"], "day": d,
                                             "code": f"D{station_code[st_id]}", "station_id": st_id})
                    else:
                        fac_id = worked_regulation[(e["id"], d, "day")]
                        assignments.append({"employee_id": e["id"], "day": d,
                                             "code": "DR", "station_id": None, "facility_id": fac_id})
                elif solver.Value(work_night[(e["id"], d)]):
                    if (e["id"], d, "night") in worked_ambulance:
                        amb_id = worked_ambulance[(e["id"], d, "night")]
                        st_id = amb_station[amb_id]
                        assignments.append({"employee_id": e["id"], "day": d,
                                             "code": f"N{station_code[st_id]}", "station_id": st_id})
                    else:
                        fac_id = worked_regulation[(e["id"], d, "night")]
                        assignments.append({"employee_id": e["id"], "day": d,
                                             "code": "NR", "station_id": None, "facility_id": fac_id})
                else:
                    assignments.append({"employee_id": e["id"], "day": d, "code": "O", "station_id": None})

        station_ambulance_pairs = [(st, amb) for st in stations for amb in ambulances_by_station.get(st["id"], [])]
        driver_assignments, driver_gaps, driver_by_slot, driver_shifts, driver_hours = schedule_drivers(
            drivers, station_ambulance_pairs, leaves, days, starting_hours, boundary_state
        )
        assignments.extend(driver_assignments)

        pro_gaps = [
            {"station_id": st_id, "ambulance_id": amb_id, "day": d.isoformat(), "shift": shift}
            for (st_id, amb_id, d, shift, gap) in pro_gap_vars if solver.Value(gap)
        ]
        medical_shortfalls = [
            {"station_id": st_id, "ambulance_id": amb_id, "day": d.isoformat(), "shift": shift,
             "needed": needed, "filled": needed - solver.Value(s)}
            for (st_id, amb_id, d, shift, needed, s) in shortfall_vars if solver.Value(s) > 0
        ]
        # Regulation-desk shortfalls reported SEPARATELY from ambulance-crew
        # shortfalls — confirmed directly: Regulation is a call-center desk,
        # not an ambulance seat, and must never be folded into "coverage" or
        # "each shift/station has a full crew", even though an unfilled desk
        # slot is still tracked and still costs CREW_SHORTFALL_PENALTY in the
        # objective (same seriousness, just a different reporting bucket).
        regulation_shortfalls = [
            {"facility_id": fac_id, "day": d.isoformat(), "shift": shift,
             "needed": target, "filled": target - solver.Value(s)}
            for (fac_id, d, shift, target, s) in regulation_shortfall_vars if solver.Value(s) > 0
        ]
        gaps = driver_gaps  # kept as the primary "coverage_gaps" field for backward compatibility

        all_slots = set(medical_by_slot) | set(driver_by_slot)
        for amb_id, d, shift in all_slots:
            crew_assignments.append({
                "ambulance_id": amb_id, "day": d, "shift": shift,
                "driver_employee_id": driver_by_slot.get((amb_id, d, shift)),
                "member_employee_ids": medical_by_slot.get((amb_id, d, shift), []),
            })

        # Cumulative hours for the month: this run's hours (from the solved
        # model) plus whatever was already carried forward in starting_hours.
        # driver_hours from schedule_drivers is already cumulative (it was
        # seeded with starting_hours internally), so it's used as-is.
        medical_hours = {
            e["id"]: starting_hours.get(e["id"], {}).get("total", 0)
            + sum(solver.Value(work_day[(e["id"], d)]) * DAY_SHIFT_HOURS
                  + solver.Value(work_night[(e["id"], d)]) * NIGHT_SHIFT_HOURS for d in days)
            for e in medical
        }
        hours_by_employee = {**medical_hours, **driver_hours}
        # schedule_drivers() above runs unconditionally regardless of scope
        # (its output feeds driver_by_slot for the crew-assignment merge
        # either way) — but for a "medical"-only pass that driver solve is a
        # throwaway never written to the database, not what's actually
        # scheduled. Showing it here as if it were real overwork would be
        # actively misleading (real incident: PHTL generating medical crew
        # saw a list of "overworking" drivers that didn't reflect the real
        # roster at all). Only flag overwork for whichever group this pass
        # actually persists.
        overwork_pool = employees if scope == "all" else medical if scope == "medical" else drivers if scope == "drivers" else []
        overwork_flags = _overwork_flags(overwork_pool, hours_by_employee)

        driver_slots_total = len(station_ambulance_pairs) * len(days) * 2  # day + night
        fairness_values = [solver.Value(t) for t in spread_terms_for_score] if spread_terms_for_score else []

        # A scope that wasn't attempted this pass (e.g. drivers weren't
        # scheduled when scope="medical") would otherwise look like a 100%
        # shortfall — not a real gap, just "not this pass's job". Score and
        # report only the scope actually computed; main.py merges this with
        # whatever the OTHER scope already has on file for the same run.
        if scope == "medical":
            driver_gaps_out, driver_gaps_for_score, driver_slots_for_score = [], [], 0
        else:
            driver_gaps_out, driver_gaps_for_score, driver_slots_for_score = driver_gaps, driver_gaps, driver_slots_total
        if scope == "drivers":
            pro_gaps_out, pro_gaps_for_score, pro_slots_for_score = [], [], 0
            medical_shortfalls_out, medical_shortfalls_for_score, medical_slots_for_score = [], [], 0
            regulation_shortfalls_out = []
        else:
            pro_gaps_out, pro_gaps_for_score, pro_slots_for_score = pro_gaps, pro_gaps, len(pro_gap_vars)
            medical_shortfalls_out = medical_shortfalls
            medical_shortfalls_for_score, medical_slots_for_score = medical_shortfalls, medical_slots_total
            regulation_shortfalls_out = regulation_shortfalls

        quality = _quality_score(fairness_values, len(driver_gaps_for_score), driver_slots_for_score,
                                  stations_without_ambulances, len(stations),
                                  len(pro_gaps_for_score), pro_slots_for_score,
                                  len(medical_shortfalls_for_score), medical_slots_for_score,
                                  len(overwork_flags))
        quality["scope"] = scope
        quality["stations_without_ambulances"] = stations_without_ambulances
        quality["pro_coverage_gaps"] = pro_gaps_out
        quality["medical_coverage_gaps"] = medical_shortfalls_out
        # Kept separate from medical_coverage_gaps — confirmed directly:
        # Regulation is a call-center desk, not an ambulance seat, and must
        # never be counted as if it were missing ambulance crew.
        quality["regulation_coverage_gaps"] = regulation_shortfalls_out
        quality["overwork_flags"] = overwork_flags
        gaps = driver_gaps_out

    # Standby is computed by main.py itself now, per global group (driver/
    # medical/call_center — see _recompute_standby_group), not per-station
    # here, since it's no longer tied to this function's per-station model
    # at all. The 6th return slot is kept (always empty) so this function's
    # signature doesn't need to change for its one caller.
    standby_assignments = []

    return status_name, assignments, gaps, crew_assignments, quality, standby_assignments
