"""Builds one illustrated PDF user manual per Roster role into app/manuals/<role>.pdf.

    1. capture the screenshots (see capture/roster_capture.mjs; they land in capture/out/roster/)
    2. python docs/manuals/build_roster_manuals.py

The API serves the file that matches the signed-in role (GET /api/manual); nobody receives another role's manual.
Menu names come from NAV_BY_ROLE / TAB_LABELS in app/static/index.html; keep the text in step with the application.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from manual_lib import build_manual, figs, load_figures  # noqa: E402

OUT_DIR = os.path.join(HERE, "..", "..", "app", "manuals")
FIGS = load_figures(os.path.join(HERE, "capture", "out", "roster", "figures.json"))
APP = "SAMU Roster"
URL = "https://ems-rw.duckdns.org"


def F(role, *keys):
    return figs(FIGS, role, *keys)


DEMO = ("note", "tip", "The screenshots in this manual use <b>fictional demo data</b>. The names, numbers and dates on your system will differ, "
                       "but the screens and buttons are the same. The orange numbers on a picture match the numbered list under it.")


# ---------------------------------------------------------------- shared blocks
def signing_in(role):
    return [
        ("h1", "Signing in and getting around"),
        ("steps", [
            f"Open <b>{URL}</b> in a browser on a computer or phone.",
            "Type your <b>username</b> and <b>password</b> exactly as given to you. The username is case sensitive and can contain a space.",
            "Press <b>Sign in</b>.",
        ]),
    ] + F("common", "sign-in") + [
        ("note", "warn", "After <b>5 wrong passwords</b> for the same username, sign-in is blocked for 15 minutes. You stay signed in for "
                         "12 hours, then you sign in again. There is no way to change your own password in the app: ask the administrator to set a new one."),
        ("h2", "The Dashboard and your menu"),
        ("p", "After signing in you land on the Dashboard. The menu on the left lists only the pages your role can open."),
    ] + F(role, "dashboard") + [
        ("h2", "This manual and signing out"),
        ("p", "<b>User manual</b> and <b>Log out</b> are at the bottom of the menu, below your name. The manual that opens is always the one for your role."),
    ] + F(role, "menu-footer") + [
        ("h2", "On a phone"),
        ("bul", [
            "Tap the <b>menu button</b> (three lines, top right). Groups with an arrow open when you tap them; tap a page and the menu closes.",
            "Lists appear as cards, one per person or record, with their buttons underneath. The roster grid scrolls sideways: swipe it.",
        ]),
    ] + F(role, "phone-menu-closed", "phone-menu-open")


def reading_the_board(role):
    return [
        ("h1", "Reading the roster board"),
        ("p", "The board has one row per person and one column per day. Each cell holds a code. Blue-tinted cells are day shifts, "
              "dark cells are night shifts, grey cells are days off, amber cells are leave or other absence, green cells are event duty."),
    ] + F(role, "board") + [
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
        ("p", "For fairness, a day shift counts as <b>10 hours</b> and a night shift as <b>14 hours</b>. The total at the right of each row is the hours worked in the roster."),
        ("h2", "Rules the system applies when it builds a roster"),
        ("bul", [
            "One shift per person per day. Leave is respected exactly as entered.",
            "The day after a night shift is a day off. A day shift is never placed directly before a night shift.",
            "No more than <b>2 working days in a row</b>. These rest rules also continue across the border between two consecutive rosters.",
            "People marked as unable to work nights never get night shifts.",
            "Anesthesiologists and critical-care staff work only at the four middle-site stations and are the only medical staff the Regulation desk draws from.",
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
    ]


def reviewing(role, extra_note=None):
    blocks = [
        ("h1", "Checking and submitting a roster"),
        ("p", "Open <b>Roster Board</b> and choose the roster. While it is a draft you see these buttons above the grid."),
    ] + F(role, "board-actions") + [
        ("p", "<b>Check conflicts</b> lists everything that would block publishing: a missing rest day, a person scheduled while inactive, a night shift "
              "for someone not cleared for nights, a leave day that was overridden, a crew with no Pro, or an ambulance that is not in service. "
              "Each line says how to fix it."),
    ] + F(role, "board-conflicts") + [
        ("p", "An item marked <b>Approved exception</b> was a deliberate rule break signed off by a manager, with a reason. It does not block publishing."),
    ]
    if extra_note:
        blocks.append(extra_note)
    return blocks


def troubleshooting(extra=()):
    rows = [["What you see", "What to do"],
            ["Sign-in is refused", "Check the username (capital letters, spaces) and password. After 5 wrong tries, wait 15 minutes."],
            ["'Session expired - please log in again'", "You were signed in for more than 12 hours. Sign in again."],
            ["'Waiting for another generation to finish'", "Only one roster is built at a time. Keep the page open or come back later."],
            ["Generation seems to take very long", "A full roster takes about 10 to 15 minutes. If the page was closed or the system restarted, start it again."]]
    rows += list(extra)
    rows.append(["Anything else", "Tell the administrator the screen name, what you pressed and the exact message."])
    return [("h1", "If something goes wrong"), ("table", rows, [0.36, 0.64])]


def contacts():
    return [("h2", "Who to contact"),
            ("table", [["", "Name and phone (fill in)"], ["Administrator", ""], ["Your own manager", ""]], [0.3, 0.7])]


def handle_shortage(seat, role):
    """seat: 'driver' or 'medical'. Mirrors openCommandWizard() in app/static/index.html."""
    start = ("Press <b>Handle shortage</b> on the ambulance shift (it is next to the driver)." if seat == "driver"
             else "Press the <b>swap button</b> next to the crew member (hover text: <i>Handle shortage</i>).")
    return [
        ("h2", "Crew Rollup: see and change a crew"),
        ("p", "<b>Crew Operations &gt; Crew Rollup</b> shows station, ambulance and crew for one roster and one day. Choose the station to jump to it."),
    ] + F(role, "crew-rollup") + [
        ("h2", "Replacing someone or accepting a gap: Handle shortage"),
        ("steps", [
            "Open <b>Crew Rollup</b>, choose the roster and the day, then pick the station. Each ambulance shift is a card.",
            start + " A window opens: <b>Choose how to fill this seat</b>.",
            "Under <b>Legal picks</b>, choose a person. Candidates are ranked by fairness, least-loaded first. "
            "Press <b>Confirm crew replacement</b>. Both people are notified if the roster is already published.",
            "If nobody is legal, <b>Risky picks - needs an override</b> lists people who would break a protected rule (for example a rest day or Pro coverage). "
            "Choose a reason, write a justification and type <b>OVERRIDE</b> to confirm.",
            "If nobody can be found, under <b>Or: leave this crew short</b> press " +
            ("<b>Approve this crew to run with no driver</b>" if seat == "driver" else "<b>Approve this crew to run short</b>") +
            ". Choose a reason and write a justification. Nobody is moved; the gap is accepted and logged.",
        ]),
    ] + F(role, "handle-shortage", "handle-shortage-more") + [
        ("note", "warn", "Every risky pick and every accepted shortage is recorded under <b>Command Decisions</b> with your name, the reason and the justification. "
                         "Safety-related ones are reviewed by the administrator."),
    ]


def plan_event_coverage(who, role):
    return [
        ("h2", "Planning event coverage"),
        ("p", "<b>Events</b> lists events that need extra ambulances and crews. Only the administrator creates events; the Drivers Manager and PHTL plan their coverage. "
              "The Call Center is never affected."),
    ] + F(role, "events") + [
        ("steps", [
            "Choose <b>Roster to plan against</b>. It must cover the event date; if none does, you are told to plan a roster for that week first.",
            "Press <b>Plan coverage</b>. The system computes a <b>Coverage plan</b>: ambulances, " + who + ", and any gaps (<i>Gaps remain</i> or <i>Fully covered</i>).",
            "If the plan would disrupt existing coverage (pulling an ambulance, reassigning someone from a shift or from mandated rest) it is marked <b>Risky</b>. Press <b>Continue - needs a reason</b>, choose a reason, write a justification, then <b>Confirm &amp; commit</b>.",
            "A plan with no disruption is committed with <b>Commit this plan</b>.",
        ]),
    ] + F(role, "plan-coverage")


def command_decisions(role, who):
    return [
        ("h2", "Command Decisions"),
        ("p", "Every deliberate rule break " + who + ": a rest day overridden, a crew accepted short of a Pro or a driver, someone pulled off a crew. "
              "Each shows who approved it, the reason and the written justification."),
    ] + F(role, "command-decisions") + [
        ("p", "Press <b>Mark resolved</b> when a decision is closed. The administrator also marks safety decisions <b>reviewed</b>."),
    ]


def notices(role):
    return [("h2", "Notifications"),
            ("p", "<b>Notifications</b> lists messages sent to you, for example when a roster you are in is published or a crew change affects you. Press <b>Mark read</b> once you have seen one.")] + F(role, "notifications")


def generation_steps(role, one_line, note):
    return [
        ("h1", "Generating the roster"),
        ("p", one_line),
    ] + F(role, "generate") + [
        ("steps", [
            "Read the <b>Preflight check</b>. Fix blocking problems first; you can still choose <b>Generate anyway</b>, but the result will be poor.",
            "Choose the <b>Range</b> (Day, Week, Month or Custom range) and the <b>Reference date</b>. The page shows the exact dates it will build.",
            "Press <b>Generate roster</b>. The page shows <b>Solving - elapsed time</b>. A full roster takes about <b>10 to 15 minutes</b>. You can keep the page open or come back later.",
            "When it finishes you see the number of assignments, an <b>Overall quality</b> score and an <b>Overtime</b> indicator, and any coverage gaps.",
        ]),
    ] + note


# ---------------------------------------------------------------- ADMIN
def admin():
    R = "admin"
    return [
        ("h1", "Your role"),
        ("p", "As <b>administrator</b> you set up everything the roster depends on, build the whole roster, review it and publish it. You are the only role that can create "
              "accounts, edit staff, stations and leave, and publish."),
        DEMO,
        ("table", [
            ["You can", "You cannot"],
            ["Set up stations, ambulances, facilities, employees, leave and users", "Change a published roster by regenerating it"],
            ["Generate drivers, medical crew and dispatch together, or any part", "Reopen a published roster (only a submitted one)"],
            ["Check conflicts, submit, reopen, publish and delete rosters", "Remove or demote the last active administrator"],
            ["Replace crew members, set crew leaders, accept a short crew", ""],
            ["Create events, review command decisions, read the activity log", ""],
        ], [0.5, 0.5]),
        ("table", [
            ["Menu group", "Pages"],
            ["Overview", "Dashboard"], ["Setup", "Stations, Ambulances, Facilities"], ["Workforce", "Employees, Leave, Users"],
            ["Planning", "Generate Roster"], ["Roster Board", "Roster Board"], ["Crew Operations", "Crew Rollup, Mark Unavailable"],
            ["Events", "Events"], ["Admin", "Notifications, Activity Log, Command Decisions"],
        ], [0.3, 0.7]),
    ] + signing_in(R) + [
        ("h1", "Setting up (do this once, then keep it current)"),
        ("p", "Set up in this order, because each step depends on the one before. The Dashboard card <b>Setup readiness</b> and the <b>Preflight check</b> on "
              "the Generate Roster page tell you what is still missing."),
        ("h2", "1. Stations"),
        ("p", "<b>Setup &gt; Stations</b> lists the stations. Each produces its own shift codes: code 3 gives D3 and N3. Press <b>Add station</b> and fill the form."),
    ] + F(R, "stations", "station-form") + [
        ("note", "tip", "Minimum drivers per crew cannot be larger than the crew size. A station that already has roster history cannot be deleted: set it inactive instead."),
        ("h2", "2. Ambulances"),
        ("p", "<b>Setup &gt; Ambulances</b>: add a vehicle with its plate, type, station and status (active, maintenance or out of service). "
              "Only active ambulances get crews. A crew placed on an ambulance that is later taken out of service shows up in Check conflicts."),
    ] + F(R, "ambulances", "ambulance-form") + [
        ("h2", "3. Facilities (fixed posts)"),
        ("p", "<b>Setup &gt; Facilities</b> lists non-ambulance posts, for example the call centre. Each has a <b>Required headcount</b>. People posted to a facility "
              "are a fixed assignment and are not part of the ambulance day and night rotation."),
    ] + F(R, "facilities") + [
        ("h2", "4. Employees"),
        ("p", "<b>Workforce &gt; Employees</b> lists everyone who can appear in a roster. Press <b>Add employee</b>, choose a home station <i>or</i> a facility posting (never both), "
              "and tick what applies."),
    ] + F(R, "employees", "employee-form") + [
        ("bul", [
            "<b>Qualified driver</b>: scheduled by the Drivers Manager. <b>Pro</b>: qualified to lead a crew; every crew needs one. "
            "<b>Floats freely</b>: may work at any station without a home-station preference. <b>No night shifts</b>: never given a night. "
            "<b>Nights only (ad hoc)</b>: only works nights while ticked. <b>Cleared for events</b>: can be pulled for event duty.",
            "New or imported people start with the role <i>Unassigned</i>; the Dashboard shows how many <b>need a role</b>.",
            "<b>Remove</b> deletes a person only if they were never used in a roster; otherwise the person is deactivated and kept for history.",
            "<b>Bulk update</b> changes a role or flag for many people at once and shows a preview first.",
        ]),
    ] + F(R, "bulk-update") + [
        ("h2", "5. Leave"),
        ("p", "<b>Workforce &gt; Leave</b>: choose the employee, the dates and the type. One record is created for each day. Leave is never overridden by the generator."),
    ] + F(R, "leave", "leave-form") + [
        ("p", "Maternity leave (96 days) and paternity leave (7 days) show a warning when a block runs longer, but are never refused. Delete a wrong day from the list."),
        ("h2", "6. Users"),
        ("p", "<b>Workforce &gt; Users</b>: create accounts for the four roles. The password needs at least 8 characters, not only digits, not a common password. "
              "<b>Edit</b> a person to reset a password, change a role or deactivate them. The last active administrator can never be removed or demoted."),
    ] + F(R, "users", "user-form") + [
        ("h2", "7. Standby settings"),
        ("p", "On <b>Generate Roster</b> press <b>Standby settings</b> to set how many reserve people each group (drivers, medical, call centre) keeps per day shift and per night shift. Zero means no standby team for that group."),
    ] + F(R, "standby-form") + generation_steps(R, "As administrator you build drivers, medical crew and the dispatch desk in one go. The three managers can also each generate their own part into the same draft.", [
        ("bul", [
            "<b>Standby settings</b> (administrator only) sets the reserve teams.",
            "Choosing exactly the same dates as an existing draft adds to that draft. Dates that only partly overlap another roster are refused.",
        ]),
    ]) + F(R, "generate-progress", "generate-result") + roster_life_cycle(["Administrator and the three managers", "Anyone signed in; an administrator can reopen it", "Administrator only"]) + reviewing(R) + [
        ("h2", "Submitting and publishing"),
        ("steps", [
            "Press <b>Submit for review</b>. You are warned if a part (drivers, medical crew, dispatch desk) was never generated.",
            "Press <b>Publish</b>. It is refused while conflicts remain. Everyone in the roster is notified, including standby duty.",
            "<b>Reopen to draft</b> returns a submitted roster to draft so a missing part can be generated. <b>Export to Excel</b> saves the grid as a spreadsheet.",
        ]),
    ] + F(R, "board-submitted") + [
        ("bul", ["<b>Regenerate</b> rebuilds a draft (all parts).", "<b>Delete roster</b> is permanent. A submitted or published roster needs you to type its status to confirm."]),
        ("h1", "Changes after the roster exists"),
    ] + handle_shortage("driver", R) + [
        ("p", "For a <b>medical crew member</b>, press the swap button next to their name instead; the steps are the same, and the last option reads <b>Approve this crew to run short</b>. "
              "Click a Pro member's name to make them <b>crew leader</b>. The calendar-style button next to a person is <b>Pull for event</b>: it takes them off their crew and leaves it short, with no replacement."),
        ("h2", "Mark Unavailable"),
        ("p", "For one day when someone cannot work: <b>Crew Operations &gt; Mark Unavailable</b> (or the red button next to a person in Crew Rollup or Employees). "
              "Choose the person and the day, press <b>Find their shift</b>, then fill the slot immediately."),
    ] + F(R, "mark-unavailable") + plan_event_coverage("drivers and medical crew", R) + [
        ("p", "To <b>create an event</b>, press <b>Create event</b> and fill the form."),
    ] + F(R, "event-form") + command_decisions(R, "in the system") + [
        ("h2", "Activity Log and notifications"),
        ("p", "<b>Activity Log</b> shows who did what and when. Filter by person, action or dates, then press <b>Apply filters</b>."),
    ] + F(R, "activity-log") + notices(R) + reading_the_board(R) + troubleshooting([
        ["Station code already in use", "Codes are unique. Choose a different code."],
        ["'No employees / stations / ambulances on file yet'", "Add at least one of each before generating."],
        ["Roster for these dates already exists and is locked", "It is submitted or published. Reopen it (submitted only), or choose different dates."],
        ["Cannot publish", "Run Check conflicts and fix each line, or have a manager record an approved exception."],
    ]) + contacts()


# ---------------------------------------------------------------- DRIVERS MANAGER
def drivers_manager():
    R = "drivers_manager"
    return [
        ("h1", "Your role"),
        ("p", "As <b>Drivers Manager</b> you build and look after the <b>driver</b> part of the roster. Medical crew and the dispatch desk are never touched by what you do."),
        DEMO,
        ("table", [
            ["You can", "You cannot"],
            ["Generate and regenerate the driver part of a roster", "Generate medical crew or the dispatch desk"],
            ["Add and edit ambulances", "Add or edit employees, stations or leave"],
            ["Check conflicts and submit a roster for review", "Publish a roster (administrator)"],
            ["Replace a driver; accept a crew running with no driver", "Replace medical crew members"],
            ["Plan driver coverage for events; review command decisions", "Create events"],
        ], [0.5, 0.5]),
        ("p", "Your menu: Dashboard, Ambulances, Generate Roster, Roster Board, Crew Rollup, Events, Notifications and Command Decisions."),
    ] + signing_in(R) + [
        ("h1", "Ambulances"),
        ("p", "<b>Ambulances</b> lists the fleet. Add or edit a vehicle with its plate, type, station and status (active, maintenance, out of service). "
              "Only active ambulances get drivers. Keep the status current: a driver placed on an ambulance that goes out of service is flagged by Check conflicts."),
    ] + F(R, "ambulances") + generation_steps(R, "Your Generate Roster page says: <i>Generates drivers only - medical crew and dispatch aren't touched.</i>", [
        ("bul", [
            "If a roster already exists for exactly those dates, your drivers are added to it. <b>Regenerate</b> on the Roster Board recomputes only the drivers.",
            "The system aims for each driver to work within one shift of the others in the same week, and balances hours over the whole month.",
            "Each ambulance needs the minimum number of drivers set on its station for every shift it runs. Gaps are reported when it finishes.",
        ]),
    ]) + roster_life_cycle(["You, the other managers and the administrator", "Anyone signed in (you can submit when your part is done)", "Administrator only"]) + reviewing(R, (
        "note", "ok", "You see only drivers on the board. When your part is ready, press <b>Submit for review</b>. The administrator then publishes.")) + [
        ("h1", "Changes after the roster exists"),
    ] + handle_shortage("driver", R) + [
        ("p", "<b>Pull for event</b> (the calendar-style button next to a driver) takes a driver off a crew for an event and leaves the crew short."),
    ] + plan_event_coverage("drivers", R) + command_decisions(R, "in your area") + notices(R) + reading_the_board(R) + troubleshooting([
        ["'This date range partially overlaps an existing roster'", "Choose the exact dates of the existing draft to add to it, or dates that do not touch it."],
        ["'Can't be regenerated (status submitted/published)'", "The roster is locked. Ask the administrator to reopen a submitted one."],
        ["A shift shows no driver", "Replace a driver in Crew Rollup, or approve the shift to run with no driver."],
    ]) + contacts()


# ---------------------------------------------------------------- PHTL
def phtl():
    R = "phtl"
    return [
        ("h1", "Your role"),
        ("p", "As <b>PHTL</b> you build and look after the <b>medical crew</b> part of the roster, including the medical staff who help cover the Regulation desk. Drivers and the dispatch desk are not touched by what you do."),
        DEMO,
        ("table", [
            ["You can", "You cannot"],
            ["Generate and regenerate medical crews", "Generate drivers or the dispatch desk"],
            ["Replace crew members; make a Pro the crew leader", "Add or edit employees, stations, ambulances or leave"],
            ["Accept a crew running short or without a Pro (with reason)", "Publish a roster (administrator)"],
            ["Check conflicts and submit a roster for review", "Create events"],
            ["Plan medical coverage for events; review command decisions", ""],
        ], [0.5, 0.5]),
        ("p", "Your menu: Dashboard, Generate Roster, Roster Board, Crew Rollup, Events, Notifications and Command Decisions."),
    ] + signing_in(R) + generation_steps(R, "Your Generate Roster page says: <i>Generates medical crew only - drivers and dispatch aren't touched.</i>", [
        ("bul", [
            "<b>Pro coverage:</b> every crew should have at least one Pro. Crew leaders can only be Pro staff.",
            "<b>Specialists:</b> anesthesiologists and critical-care staff work only at the four middle-site stations. They are also the only medical staff who can fill the Regulation desk "
            "beyond the call centre's own people, with a weekly limit per person.",
            "<b>Home station:</b> staff stay at their home station unless marked <i>Floats freely</i>, and are sent elsewhere only when another station is short.",
            "<b>Fairness:</b> total, night and weekend hours are balanced across the whole month.",
            "<b>Regenerate</b> on the Roster Board recomputes only the medical crew and leaves drivers and dispatch alone.",
        ]),
    ]) + roster_life_cycle(["You, the other managers and the administrator", "Anyone signed in (you can submit when your part is done)", "Administrator only"]) + reviewing(R, (
        "note", "ok", "You see medical staff only on the board. When your part is ready, press <b>Submit for review</b>.")) + [
        ("h1", "Changes after the roster exists"),
    ] + handle_shortage("medical", R) + [
        ("h2", "Crew leader and pulling people for events"),
        ("bul", [
            "Click a <b>Pro</b> member's name in Crew Rollup to make them <b>crew leader</b> (only Pro staff can lead).",
            "The calendar-style button next to a crew member is <b>Pull for event</b>: it takes them off their crew and leaves it short, with no replacement.",
            "A replacement who is not a Pro, and who would leave the crew without one, needs an override.",
        ]),
    ] + plan_event_coverage("medical crew", R) + command_decisions(R, "in your area") + notices(R) + reading_the_board(R) + troubleshooting([
        ["'Would leave this crew with no Pro-rated member'", "Pick a Pro as the replacement, or use an override with a written justification."],
        ["A crew is short", "Replace someone, or approve the crew to run short (reason and justification required)."],
        ["'Can't be regenerated (status submitted/published)'", "The roster is locked. Ask the administrator to reopen a submitted one."],
    ]) + contacts()


# ---------------------------------------------------------------- CALL CENTER TL
def call_center_tl():
    R = "call_center_tl"
    return [
        ("h1", "Your role"),
        ("p", "As <b>Call Center Team Leader</b> you build the <b>dispatch desk rotation</b>: who covers the Regulation desk on each day and night shift. Drivers and ambulance crews are not touched by what you do, and events never affect the call centre roster."),
        DEMO,
        ("table", [
            ["You can", "You cannot"],
            ["Generate and regenerate the dispatch desk rotation", "Generate drivers or medical crews"],
            ["See the roster board for your own staff", "See or edit other teams' staff"],
            ["Check conflicts and submit a roster for review", "Publish a roster (administrator)"],
            ["Read your notifications", "Use Crew Rollup, Events or Command Decisions"],
        ], [0.5, 0.5]),
        ("p", "Your menu: Dashboard, Generate Roster, Roster Board and Notifications."),
    ] + signing_in(R) + generation_steps(R, "Your Generate Roster page says: <i>Generates the dispatch desk's coverage rotation only.</i>", [
        ("bul", [
            "The system aims for <b>2 of your own staff</b> on the Regulation desk for every day shift and every night shift. The medical team's specialists top the desk up to its full headcount; that is arranged by the PHTL.",
            "Fairness is measured as shifts and hours over the whole month. A day shift counts as 10 hours and a night shift as 14.",
            "Leave entered by the administrator is respected. After a night shift the next day is a day off, and nobody works more than 2 days in a row.",
            "If a roster already exists for exactly the same dates, your rotation is added to it. <b>Regenerate</b> recomputes only the dispatch desk.",
        ]),
    ]) + roster_life_cycle(["You, the other managers and the administrator", "Anyone signed in (you can submit when your part is done)", "Administrator only"]) + reviewing(R, (
        "note", "ok", "You see your own staff only. The Regulation desk shows as <b>DR</b> (day) and <b>NR</b> (night). If a person must change, ask the administrator.")) + [
        ("p", "When your part is ready, press <b>Submit for review</b>. The administrator publishes."),
    ] + notices(R) + reading_the_board(R) + troubleshooting([
        ["'No active call_and_dispatch employees on file yet'", "Ask the administrator to add your staff with the call and dispatch role and a facility posting."],
        ["A desk shift shows fewer than 2 people", "Not enough staff were free that shift (leave or rest days). Ask the administrator or the PHTL for cover."],
    ]) + contacts()


MANUALS = {
    "admin": ("Administrator", "This manual explains everything the administrator does in SAMU Roster, screen by screen: setting up the system, building the roster, reviewing and publishing it, and handling changes afterwards.", admin),
    "drivers_manager": ("Drivers Manager", "This manual explains, screen by screen, what the Drivers Manager does in SAMU Roster: keeping the fleet list, building the driver part of the roster, reviewing it and handling driver changes.", drivers_manager),
    "phtl": ("PHTL", "This manual explains, screen by screen, what the PHTL does in SAMU Roster: building the medical crew part of the roster, reviewing it and handling crew changes and shortages.", phtl),
    "call_center_tl": ("Call Center Team Leader", "This manual explains, screen by screen, what the Call Center Team Leader does in SAMU Roster: building the dispatch desk rotation and reviewing it.", call_center_tl),
}

if __name__ == "__main__":
    if not FIGS:
        sys.exit("No screenshots found. Run capture/roster_capture.mjs first (see its header).")
    os.makedirs(OUT_DIR, exist_ok=True)
    for code, (title, intro, fn) in MANUALS.items():
        path = os.path.abspath(os.path.join(OUT_DIR, f"{code}.pdf"))
        build_manual(path, APP, title, intro, fn())
        print("written", path, f"({os.path.getsize(path) // 1024} KB)")
