"""Builds one PDF user manual per Roster role into app/manuals/<role>.pdf.

Run from the repository root:   python docs/manuals/build_roster_manuals.py
The API serves the file that matches the signed-in role (GET /api/manual); nobody receives another role's manual.
Keep the text in step with the application: menu names come from NAV_BY_ROLE / TAB_LABELS in app/static/index.html.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from manual_lib import build_manual  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app", "manuals")
APP = "SAMU Roster"
URL = "https://ems-rw.duckdns.org"

# ---------------------------------------------------------------- shared blocks
def signing_in():
    return [
        ("h1", "Signing in and getting around"),
        ("steps", [
            f"Open <b>{URL}</b> in a browser on a computer or phone.",
            "Type your <b>username</b> and <b>password</b> exactly as given to you. The username is case sensitive and can contain a space.",
            "Press <b>Sign in</b>.",
        ]),
        ("note", "warn", "After <b>5 wrong passwords</b> for the same username, sign-in is blocked for 15 minutes. You stay signed in for "
                         "12 hours, then you sign in again. There is no way to change your own password in the app: ask the administrator to set a new one."),
        ("h2", "On a phone"),
        ("bul", [
            "Tap the <b>menu button</b> (three lines, top right). Groups with an arrow open when you tap them; tap a page and the menu closes.",
            "Lists appear as cards, one per person or record, with their buttons underneath. The roster grid scrolls sideways: swipe it.",
        ]),
        ("h2", "Signing out and this manual"),
        ("p", "<b>Log out</b> and <b>User manual</b> are at the bottom of the menu, below your name. The manual you open is always the one for your role."),
    ]


def reading_the_board():
    return [
        ("h1", "Reading the roster board"),
        ("p", "The board has one row per person and one column per day. Each cell holds a code. Colours: blue-tinted cells are day shifts, "
              "dark cells are night shifts, grey cells are days off, amber cells are leave or other absence, green cells are event duty."),
        ("table", [
            ["Code", "Meaning"],
            ["D + number (D3)", "Day shift at the station with that code"],
            ["N + number (N3)", "Night shift at the station with that code"],
            ["DR / NR", "Day / night shift at the Regulation desk (call centre desk)"],
            ["DE / NE", "Day / night event duty"],
            ["O", "Off"],
            ["AL, SL, ML, PL", "Annual leave, sick leave, maternity leave, paternity leave"],
            ["TR, AD, P", "Training, administration, permission"],
            ["MS, ST, CY, CL", "Mission, stock, cyclism, circumstance leave"],
            ["DT, DM, DC, R", "Data, day meeting, day checking, roster"],
            ["SB", "Standby"],
        ], [0.3, 0.7]),
        ("p", "For fairness, a day shift counts as <b>10 hours</b> and a night shift as <b>14 hours</b>. The total on the right of each row is hours worked in the roster."),
        ("h2", "Rules the system applies when it builds a roster"),
        ("bul", [
            "One shift per person per day. Leave is respected exactly as entered.",
            "The day after a night shift is a day off. A day shift is never placed directly before a night shift.",
            "No more than <b>2 working days in a row</b>. Rest and run-length rules also continue across the border between two rosters.",
            "People marked as unable to work nights never get night shifts.",
            "Anesthesiologists and critical-care staff work only at the four middle-site stations and are the only people the Regulation desk draws from the medical team.",
            "Every crew should have at least one Pro. If people are short, the system fills what it can and reports the gap; it never refuses to build a roster.",
            "Hours are balanced fairly across the whole calendar month, not only inside one roster.",
        ]),
    ]


def roster_life_cycle(role_does):
    return [
        ("h1", "How a roster moves from draft to published"),
        ("table", [
            ["Stage", "What it means", "Who acts"],
            ["Draft", "Being built. The three parts (drivers, medical crew, dispatch desk) can each be generated into the same draft.", role_does[0]],
            ["Submitted", "Sent for review. It is locked: nobody can generate into it.", role_does[1]],
            ["Published", "Final. Every person in it is notified. Later changes go through crew replacements, which notify the people affected.", role_does[2]],
        ], [0.18, 0.5, 0.32]),
        ("p", "<b>Check conflicts</b> on the Roster Board lists anything that would block publishing: a missing rest day, a person scheduled while "
              "inactive, a night shift for someone not cleared for nights, a leave day that was overridden, a crew with no Pro, or an ambulance that is "
              "not in service. Each line says how to fix it."),
    ]


def troubleshooting(extra=()):
    rows = [["What you see", "What to do"],
            ["Sign-in is refused", "Check the username (capital letters, spaces) and password. After 5 wrong tries, wait 15 minutes."],
            ["'Session expired - please log in again'", "You were signed in for more than 12 hours. Sign in again."],
            ["'Waiting for another generation to finish'", "Only one roster is built at a time. Keep the page open or come back later."],
            ["Generation seems to take very long", "A full roster takes about 10 to 15 minutes. If the page was closed or the system restarted, start it again."]]
    rows += list(extra)
    rows.append(["Anything else", "Tell the administrator the screen name, what you pressed and the exact message."])
    return [("h1", "If something goes wrong"), ("table", rows, [0.36, 0.64])]


def handle_shortage(seat):
    """seat: 'driver' or 'medical'. Mirrors openCommandWizard() in app/static/index.html."""
    start = ("Press <b>Handle shortage</b> on the ambulance shift (it is next to the driver)." if seat == "driver"
             else "Press the <b>swap button</b> next to the crew member (hover text: <i>Handle shortage</i>).")
    return [
        ("h2", "Replacing someone or accepting a gap: Handle shortage"),
        ("steps", [
            "Open <b>Crew Operations &gt; Crew Rollup</b>, choose the roster and the day, then pick the station. Each ambulance shift is a card.",
            start + " A window opens: <b>Choose how to fill this seat</b>.",
            "Under <b>Legal picks</b>, choose a person. Candidates are ranked by fairness, least-loaded first. <b>Home station only</b> and <b>Show ineligible too</b> filter the list. "
            "Press <b>Confirm crew replacement</b>. Both people are notified if the roster is already published.",
            "If nobody is legal, <b>Risky picks - needs an override</b> lists people who would break a protected rule (for example a rest day or Pro coverage). "
            "Choose a reason, write a justification and type <b>OVERRIDE</b> to confirm.",
            "If nobody can be found, under <b>Or: leave this crew short</b> press " +
            ("<b>Approve this crew to run with no driver</b>" if seat == "driver" else "<b>Approve this crew to run short</b>") +
            ". Choose a reason and write a justification. Nobody is moved; the gap is accepted and logged.",
        ]),
        ("note", "warn", "Every risky pick and every accepted shortage is recorded under <b>Command Decisions</b> with your name, the reason and the justification. "
                         "Safety-related ones are reviewed by the administrator."),
    ]


def plan_event_coverage(who):
    return [
        ("h2", "Planning event coverage"),
        ("steps", [
            "Open <b>Events</b> and find the event. If no generated roster covers the event date, you are told to plan a roster for that week first.",
            "Press <b>Plan coverage</b> and choose <b>Roster to plan against</b>. The system computes a <b>Coverage plan</b>: ambulances, " + who + ", and any gaps (<i>Gaps remain</i> or <i>Fully covered</i>).",
            "If the plan would disrupt existing coverage (pulling an ambulance, reassigning someone from a shift or from mandated rest) it is marked <b>Risky</b>. Press <b>Continue - needs a reason</b>, choose a reason, write a justification, then <b>Confirm &amp; commit</b>.",
            "A plan with no disruption is committed with <b>Commit this plan</b>.",
        ]),
    ]


def contacts():
    return [("h2", "Who to contact"),
            ("table", [["", "Name and phone (fill in)"], ["Administrator", ""], ["Your own manager", ""]], [0.3, 0.7])]


# ---------------------------------------------------------------- ADMIN
def admin():
    return [
        ("h1", "Your role"),
        ("p", "As <b>administrator</b> you set up everything the roster depends on, build the whole roster, review it and publish it. You are the only role that can create "
              "accounts, edit staff, stations and leave, and publish."),
        ("table", [
            ["You can", "You cannot"],
            ["Set up stations, ambulances, facilities, employees, leave and users", "Change a published roster by regenerating it"],
            ["Generate drivers, medical crew and dispatch together, or any part", "Reopen a published roster (only a submitted one)"],
            ["Check conflicts, submit, reopen, publish and delete rosters", "Remove or demote the last active administrator"],
            ["Replace crew members, set crew leaders, accept a short crew", ""],
            ["Create events, review command decisions, read the activity log", ""],
        ], [0.5, 0.5]),
        ("h2", "Your menu"),
        ("table", [
            ["Group", "Pages"],
            ["Overview", "Dashboard"],
            ["Setup", "Stations, Ambulances, Facilities"],
            ["Workforce", "Employees, Leave, Users"],
            ["Planning", "Generate Roster"],
            ["Roster Board", "Roster Board"],
            ["Crew Operations", "Crew Rollup, Mark Unavailable"],
            ["Events", "Events"],
            ["Admin", "Notifications, Activity Log, Command Decisions"],
        ], [0.3, 0.7]),
    ] + signing_in() + [
        ("h1", "Setting up (do this once, then keep it current)"),
        ("p", "Set up in this order, because each step depends on the one before. The Dashboard card <b>Setup readiness</b> and the <b>Preflight check</b> on "
              "the Generate Roster page tell you what is still missing."),
        ("h2", "1. Stations"),
        ("steps", [
            "Open <b>Setup &gt; Stations</b> and press <b>Add station</b>.",
            "Fill <b>Station name</b>, <b>Code</b> (the number used in shift codes: code 3 produces D3 and N3; it must be unique), <b>Day crew size</b>, "
            "<b>Night crew size</b>, <b>Min drivers/crew</b> and <b>Runs nights</b> (turn it off for a day-only station). Press <b>Save</b>.",
        ]),
        ("note", "tip", "Minimum drivers per crew cannot be larger than the crew size. A station that already has roster history cannot be deleted: set it inactive instead."),
        ("h2", "2. Ambulances"),
        ("p", "<b>Setup &gt; Ambulances</b> &gt; add: <b>Plate no.</b>, <b>Type</b>, <b>Station</b> and <b>Status</b> (active, maintenance or out of service). "
              "Only active ambulances get crews. A crew placed on an ambulance that is later taken out of service shows up in Check conflicts."),
        ("h2", "3. Facilities (fixed posts)"),
        ("p", "<b>Setup &gt; Facilities</b> lists non-ambulance posts, for example the call centre. Each has a <b>Required headcount</b>. People posted to a facility "
              "are a fixed assignment and are not part of the ambulance day and night rotation."),
        ("h2", "4. Employees"),
        ("steps", [
            "Open <b>Workforce &gt; Employees</b> and press <b>Add employee</b>.",
            "Fill <b>Name</b>, contact details and <b>Role</b>. Choose a <b>Home station</b> <i>or</i> a <b>facility posting</b>, never both.",
            "Tick what applies: <b>Qualified driver</b>, <b>Team leader</b>, <b>Pro</b> (qualified to lead a crew; every crew needs one), "
            "<b>Floats freely</b> (may work at any station without a home-station preference), <b>Nights only (ad hoc)</b>, <b>Cleared for events</b>, "
            "and the night restriction option for anyone who may not work nights.",
            "Press <b>Save</b>.",
        ]),
        ("bul", [
            "New or imported people start with the role <i>unassigned</i>; the Dashboard shows how many <b>need a role</b>. <b>Bulk update</b> changes a role or flag for many people at once and shows a preview first.",
            "<b>Remove</b> deletes a person only if they were never used in a roster; otherwise the person is deactivated and kept for history.",
        ]),
        ("h2", "5. Leave"),
        ("steps", [
            "Open <b>Workforce &gt; Leave</b>. Choose the <b>Employee</b>, <b>From</b>, optional <b>To</b> and the <b>Type</b>.",
            "Save. One record is created for each day in the range. Leave is never overridden by the generator.",
        ]),
        ("p", "Maternity leave (96 days) and paternity leave (7 days) show a warning when a block runs longer, but are never refused. Delete a wrong day from the list."),
        ("h2", "6. Users"),
        ("steps", [
            "Open <b>Workforce &gt; Users</b> and press <b>Create user</b>.",
            "Enter <b>Username</b>, <b>Password</b> (at least 8 characters, not only digits, not a common password) and the <b>Role</b>: admin, drivers_manager, phtl or call_center_tl.",
            "To reset a password, deactivate someone, or change a role, press <b>Edit</b> on their row. The last active administrator can never be removed or demoted.",
        ]),
        ("h2", "7. Standby settings"),
        ("p", "On <b>Generate Roster</b> press <b>Standby settings</b> to set how many reserve people each group (drivers, medical, call centre) keeps per day shift and per night shift. "
              "Zero means no standby team for that group."),
        ("h1", "Generating a roster"),
        ("steps", [
            "Open <b>Planning &gt; Generate Roster</b>. Read the <b>Preflight check</b>. Fix blocking problems first; you can still choose <b>Generate anyway</b>, but the result will be poor.",
            "Choose the <b>Range</b> (Day, Week, Month or Custom range) and the <b>Reference date</b>. The page shows the exact dates it will build.",
            "Press <b>Generate roster</b>. The page shows <b>Solving - elapsed time</b>. A full roster takes about <b>10 to 15 minutes</b>. You can keep the page open or come back; "
            "the roster then appears under <b>Existing rosters</b> and on the Roster Board.",
            "When it finishes you see the number of assignments, an <b>Overall quality</b> score and an <b>Overtime</b> indicator, and any coverage gaps.",
        ]),
        ("bul", [
            "Choosing exactly the same dates as an existing draft adds to that draft. Dates that only partly overlap another roster are refused.",
            "As administrator you generate drivers, medical crew and the dispatch desk in one go. The three managers can also each generate their own part into the same draft.",
        ]),
    ] + roster_life_cycle(["Administrator and the three managers", "Anyone who is signed in; an administrator can reopen it", "Administrator only"]) + [
        ("h1", "Reviewing and publishing"),
        ("steps", [
            "Open <b>Roster Board</b> and choose the roster. Read the grid and the status line.",
            "Press <b>Check conflicts</b>. Fix everything listed under <b>Can't publish</b>. Items marked <b>Approved exception</b> were signed off by a manager and do not block.",
            "Press <b>Submit for review</b>. You are warned if a part (drivers, medical crew, dispatch desk) was never generated.",
            "Press <b>Publish</b>. Everyone in the roster is notified, including standby duty.",
            "<b>Export to Excel</b> saves the grid as a spreadsheet.",
        ]),
        ("bul", [
            "<b>Regenerate</b> rebuilds a draft (all parts). <b>Reopen to draft</b> returns a submitted roster to draft so a missing part can be generated.",
            "<b>Delete</b> is permanent. A submitted or published roster needs you to type its status to confirm.",
        ]),
        ("h1", "Changes after the roster exists"),
        ("p", "<b>Crew Operations &gt; Crew Rollup</b> shows station, ambulance and crew for a roster and a day. Besides replacing people (below):"),
        ("bul", [
            "Click a Pro member's name to make them <b>crew leader</b>.",
            "The calendar-style button next to a person is <b>Pull for event</b>: it takes them off their crew for an event and leaves the crew short, with no replacement.",
            "The red button next to a driver is <b>Mark unavailable</b>: it opens Mark Unavailable for that person.",
        ]),
    ] + handle_shortage("driver") + [
        ("p", "For a <b>medical crew member</b>, press the swap button next to their name instead; the steps are the same, and the last option reads <b>Approve this crew to run short</b>."),
        ("h2", "Mark Unavailable"),
        ("p", "For one day when someone cannot work: <b>Crew Operations &gt; Mark Unavailable</b>, choose the person and the day, press <b>Find their shift</b>, then fill the slot immediately."),
        ("h2", "Events, command decisions, activity"),
        ("bul", [
            "<b>Events</b>: only you create and delete events. The Drivers Manager and PHTL plan their coverage; the Call Center is never affected.",
            "<b>Command Decisions</b> lists every deliberate rule break with who approved it and why. Use <b>Mark reviewed</b> for safety exceptions and <b>Mark resolved</b> when closed.",
            "<b>Activity Log</b> shows who did what and when. <b>Notifications</b> lists messages sent to you; press <b>Mark read</b>.",
        ]),
    ] + reading_the_board() + troubleshooting([
        ["Station code already in use", "Codes are unique. Choose a different code."],
        ["'No employees / stations / ambulances on file yet'", "Add at least one of each before generating."],
        ["Roster for these dates already exists and is locked", "It is submitted or published. Reopen it (submitted only), or choose different dates."],
        ["Cannot publish", "Run Check conflicts and fix each line, or have a manager record an approved exception."],
    ]) + contacts()


# ---------------------------------------------------------------- DRIVERS MANAGER
def drivers_manager():
    return [
        ("h1", "Your role"),
        ("p", "As <b>Drivers Manager</b> you build and look after the <b>driver</b> part of the roster. Medical crew and the dispatch desk are never touched by what you do."),
        ("table", [
            ["You can", "You cannot"],
            ["Generate and regenerate the driver part of a roster", "Generate medical crew or the dispatch desk"],
            ["Add and edit ambulances", "Add or edit employees, stations or leave"],
            ["Check conflicts and submit a roster for review", "Publish a roster (administrator)"],
            ["Replace a driver; accept a crew running with no driver", "Replace medical crew members"],
            ["Plan driver coverage for events; review command decisions", "Create events"],
        ], [0.5, 0.5]),
        ("h2", "Your menu"),
        ("p", "Dashboard, Ambulances, Generate Roster, Roster Board, Crew Rollup, Events, Notifications and Command Decisions."),
    ] + signing_in() + [
        ("h1", "Ambulances"),
        ("p", "<b>Ambulances</b> lists the fleet. Add or edit a vehicle with <b>Plate no.</b>, <b>Type</b>, <b>Station</b> and <b>Status</b> (active, maintenance, out of service). "
              "Only active ambulances get drivers. Keep the status current: a driver placed on an ambulance that goes out of service is flagged by Check conflicts."),
        ("h1", "Generating the driver roster"),
        ("steps", [
            "Open <b>Generate Roster</b>. The page says <i>Generates drivers only - medical crew and dispatch aren't touched.</i>",
            "Read the <b>Preflight check</b>. Choose the <b>Range</b> (Day, Week, Month or Custom range) and the <b>Reference date</b>, then press <b>Generate roster</b>.",
            "Wait. The page shows elapsed time; it can take several minutes. You can keep it open or return later.",
            "The result lists any ambulance shift that could not get its drivers. Those gaps also appear when you check conflicts.",
        ]),
        ("bul", [
            "If a roster already exists for exactly those dates, your drivers are added to it. <b>Regenerate</b> on the Roster Board recomputes only the drivers.",
            "The system aims for each driver to work within one shift of the others in the same week, and balances hours over the whole month. A day shift counts as 10 hours and a night shift as 14.",
            "Each ambulance needs the minimum number of drivers set on its station for every shift it runs.",
        ]),
    ] + roster_life_cycle(["You, the other managers and the administrator", "Anyone signed in (you can submit when your part is done)", "Administrator only"]) + [
        ("h1", "Reviewing your part"),
        ("steps", [
            "Open <b>Roster Board</b> and choose the roster. You see only drivers.",
            "Press <b>Check conflicts</b>. Lines under <b>Can't publish</b> must be fixed or formally accepted.",
            "When your part is ready, press <b>Submit for review</b>. The administrator then publishes.",
        ]),
    ] + handle_shortage("driver") + plan_event_coverage("drivers") + [
        ("h2", "Command decisions and notices"),
        ("bul", [
            "<b>Pull for event</b> (the calendar-style button next to a driver in Crew Rollup) takes a driver off a crew for an event and leaves the crew short.",
            "<b>Command Decisions</b> shows every approved rule break in your area, with who approved it and why. Use <b>Mark resolved</b> when it is closed.",
            "<b>Notifications</b> shows messages for you. Press <b>Mark read</b>.",
        ]),
    ] + reading_the_board() + troubleshooting([
        ["'This date range partially overlaps an existing roster'", "Choose the exact dates of the existing draft to add to it, or dates that do not touch it."],
        ["'Can't be regenerated (status submitted/published)'", "The roster is locked. Ask the administrator to reopen a submitted one."],
        ["A shift shows no driver", "Replace a driver in Crew Rollup, or approve the shift to run with no driver."],
    ]) + contacts()


# ---------------------------------------------------------------- PHTL
def phtl():
    return [
        ("h1", "Your role"),
        ("p", "As <b>PHTL</b> you build and look after the <b>medical crew</b> part of the roster, including the medical staff who help cover the Regulation desk. Drivers and the dispatch desk are not touched by what you do."),
        ("table", [
            ["You can", "You cannot"],
            ["Generate and regenerate medical crews", "Generate drivers or the dispatch desk"],
            ["Replace crew members; make a Pro the crew leader", "Add or edit employees, stations, ambulances or leave"],
            ["Accept a crew running short or without a Pro (with reason)", "Publish a roster (administrator)"],
            ["Check conflicts and submit a roster for review", "Create events"],
            ["Plan medical coverage for events; review command decisions", ""],
        ], [0.5, 0.5]),
        ("h2", "Your menu"),
        ("p", "Dashboard, Generate Roster, Roster Board, Crew Rollup, Events, Notifications and Command Decisions."),
    ] + signing_in() + [
        ("h1", "Generating the medical roster"),
        ("steps", [
            "Open <b>Generate Roster</b>. The page says <i>Generates medical crew only - drivers and dispatch aren't touched.</i>",
            "Read the <b>Preflight check</b>, choose the <b>Range</b> and <b>Reference date</b>, and press <b>Generate roster</b>.",
            "Wait. A full medical roster is the longest part and can take 10 to 15 minutes. You can keep the page open or return later.",
            "The result shows the number of assignments, a quality score and any crew that is short.",
        ]),
        ("bul", [
            "<b>Pro coverage:</b> every crew should have at least one Pro. Crew leaders can only be Pro staff.",
            "<b>Specialists:</b> anesthesiologists and critical-care staff work only at the four middle-site stations. They are also the only medical staff who can fill the Regulation desk "
            "beyond the call centre's own people, with a weekly limit per person.",
            "<b>Home station:</b> staff stay at their home station unless marked <i>Floats freely</i>, and are sent elsewhere only when another station is short.",
            "<b>Fairness:</b> total, night and weekend hours are balanced across the whole month. A day shift counts as 10 hours and a night shift as 14.",
            "<b>Regenerate</b> on the Roster Board recomputes only the medical crew and leaves drivers and dispatch alone.",
        ]),
    ] + roster_life_cycle(["You, the other managers and the administrator", "Anyone signed in (you can submit when your part is done)", "Administrator only"]) + [
        ("h1", "Reviewing your part"),
        ("steps", [
            "Open <b>Roster Board</b> and choose the roster. You see medical staff only.",
            "Press <b>Check conflicts</b>. A crew with no Pro, a missing rest day or a night shift for someone not cleared for nights blocks publishing until fixed or formally accepted.",
            "When your part is ready, press <b>Submit for review</b>.",
        ]),
        ("h2", "Crew leader and pulling people for events"),
        ("bul", [
            "Click a Pro member's name in Crew Rollup to make them <b>crew leader</b> (only Pro staff can lead).",
            "The calendar-style button next to a crew member is <b>Pull for event</b>: it takes them off their crew and leaves it short, with no replacement.",
            "A replacement who is not a Pro, and who would leave the crew without one, needs an override.",
        ]),
    ] + handle_shortage("medical") + plan_event_coverage("medical crew") + [
        ("h2", "Command decisions and notices"),
        ("bul", [
            "<b>Command Decisions</b> lists every approved rule break with who approved it and why. <b>Standby / on-call</b> people appear in Crew Rollup.",
            "<b>Notifications</b> shows messages for you. Press <b>Mark read</b>.",
        ]),
    ] + reading_the_board() + troubleshooting([
        ["'Would leave this crew with no Pro-rated member'", "Pick a Pro as the replacement, or use an override with a written justification."],
        ["A crew is short", "Replace someone, or approve the crew to run short (reason and justification required)."],
        ["'Can't be regenerated (status submitted/published)'", "The roster is locked. Ask the administrator to reopen a submitted one."],
    ]) + contacts()


# ---------------------------------------------------------------- CALL CENTER TL
def call_center_tl():
    return [
        ("h1", "Your role"),
        ("p", "As <b>Call Center Team Leader</b> you build the <b>dispatch desk rotation</b>: who covers the Regulation desk on each day and night shift. Drivers and ambulance crews are not touched by what you do, and events never affect the call centre roster."),
        ("table", [
            ["You can", "You cannot"],
            ["Generate and regenerate the dispatch desk rotation", "Generate drivers or medical crews"],
            ["See the roster board for your own staff", "See or edit other teams' staff"],
            ["Check conflicts and submit a roster for review", "Publish a roster (administrator)"],
            ["Read your notifications", "Use Crew Rollup, Events or Command Decisions"],
        ], [0.5, 0.5]),
        ("h2", "Your menu"),
        ("p", "Dashboard, Generate Roster, Roster Board and Notifications."),
    ] + signing_in() + [
        ("h1", "Generating the dispatch desk rotation"),
        ("steps", [
            "Open <b>Generate Roster</b>. The page says <i>Generates the dispatch desk's coverage rotation only.</i>",
            "Choose the <b>Range</b> (Day, Week, Month or Custom range) and the <b>Reference date</b>, then press <b>Generate roster</b>.",
            "Wait. The page shows elapsed time. You can keep it open or return later.",
            "The result reports any shift where the desk could not be fully covered.",
        ]),
        ("bul", [
            "The system aims for <b>2 of your own staff</b> on the Regulation desk for every day shift and every night shift. The medical team's specialists top the desk up to its full headcount; that is arranged by the PHTL.",
            "Fairness is measured as shifts and hours over the whole month. A day shift counts as 10 hours and a night shift as 14.",
            "Leave entered by the administrator is respected. After a night shift the next day is a day off, and nobody works more than 2 days in a row.",
            "If a roster already exists for exactly the same dates, your rotation is added to it. <b>Regenerate</b> recomputes only the dispatch desk.",
        ]),
    ] + roster_life_cycle(["You, the other managers and the administrator", "Anyone signed in (you can submit when your part is done)", "Administrator only"]) + [
        ("h1", "Reviewing your part"),
        ("steps", [
            "Open <b>Roster Board</b> and choose the roster. You see your own staff only. The Regulation desk shows as <b>DR</b> (day) and <b>NR</b> (night).",
            "Press <b>Check conflicts</b> and fix anything under <b>Can't publish</b>. If a person must change, ask the administrator.",
            "When your part is ready, press <b>Submit for review</b>. The administrator publishes.",
            "<b>Notifications</b> shows the message sent when the roster is published. Press <b>Mark read</b>.",
        ]),
    ] + reading_the_board() + troubleshooting([
        ["'No active call_and_dispatch employees on file yet'", "Ask the administrator to add your staff with the call and dispatch role and a facility posting."],
        ["A desk shift shows fewer than 2 people", "Not enough staff were free that shift (leave or rest days). Ask the administrator or the PHTL for cover."],
    ]) + contacts()


MANUALS = {
    "admin": ("Administrator", "This manual explains everything the administrator does in SAMU Roster: setting up the system, building the roster, reviewing and publishing it, and handling changes afterwards.", admin),
    "drivers_manager": ("Drivers Manager", "This manual explains what the Drivers Manager does in SAMU Roster: keeping the fleet list, building the driver part of the roster, reviewing it and handling driver changes.", drivers_manager),
    "phtl": ("PHTL", "This manual explains what the PHTL does in SAMU Roster: building the medical crew part of the roster, reviewing it and handling crew changes and shortages.", phtl),
    "call_center_tl": ("Call Center Team Leader", "This manual explains what the Call Center Team Leader does in SAMU Roster: building the dispatch desk rotation and reviewing it.", call_center_tl),
}

if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for code, (title, intro, fn) in MANUALS.items():
        path = os.path.abspath(os.path.join(OUT_DIR, f"{code}.pdf"))
        build_manual(path, APP, title, intro, fn())
        print("written", path)
