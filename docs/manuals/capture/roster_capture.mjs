// Captures the annotated screenshots for the SAMU Roster user manuals.
//
// It signs in as each role on a THROWAWAY instance that holds the fictional demo data from roster_seed.py,
// opens the real screens, draws numbered markers on the elements the manual talks about, and saves:
//   docs/manuals/capture/out/roster/<role>/<key>.jpg
//   docs/manuals/capture/out/roster/figures.json   (role, key, file, title, intro, steps) - read by build_roster_manuals.py
//
//   node docs/manuals/capture/roster_capture.mjs            (all roles)
//   ONLY=admin node docs/manuals/capture/roster_capture.mjs (one role)
// Needs Playwright:  set PLAYWRIGHT_MODULE to its entry file if it is not installed next to this script.
import { mkdir, writeFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : "playwright-core");
const BASE = process.env.ROSTER_URL ?? "http://127.0.0.1:8765";
const OUT = join(dirname(fileURLToPath(import.meta.url)), "out", "roster");
const PASSWORD = "Demo2026!pass";
const USERS = { admin: "admin", drivers_manager: "francois.ndayisenga", phtl: "sylvie.mukandayisenga", call_center_tl: "hortense.uwamahoro" };
const ONLY = process.env.ONLY ? process.env.ONLY.split(",") : Object.keys(USERS);
const DRAFT = "2026-11-09";   // the draft week in the demo data (published week: 2026-11-02)

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch();
const figures = [];
const problems = [];

// --------------------------------------------------------------------------- helpers
async function session(role, { phone = false } = {}) {
  const ctx = await browser.newContext(phone
    ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }
    : { viewport: { width: 1100, height: 720 }, deviceScaleFactor: 1.5 });   // narrower window = larger text on the page; 1.5x keeps it sharp when zoomed
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + "/");
  await page.waitForSelector("#login-username");
  if (role) {
    await page.fill("#login-username", USERS[role]);
    await page.fill("#login-password", PASSWORD);
    await page.click("#login-submit");
    await page.waitForSelector(phone ? "#nav-menu-btn" : "#nav-tabs button", { state: "visible" });
    await page.waitForTimeout(900);
  }
  return { ctx, page };
}

async function goto(page, group, label) {
  if (await page.locator("#nav-menu-btn").isVisible()) await page.tap("#nav-menu-btn");
  const toggle = page.locator("#nav-tabs .group-toggle", { hasText: group }).first();
  if (await toggle.count()) {
    await toggle.click(); await page.waitForTimeout(250);
    await page.locator(".nav-secondary button.tab", { hasText: label }).first().click();
  } else {
    await page.locator("#nav-tabs > button.tab", { hasText: label }).first().click();
  }
  await page.waitForTimeout(1600);
}

async function shot(page, role, key, meta, marks = [], { clip = null } = {}) {
  const boxes = [];
  const missing = new Set();
  for (const [i, m] of marks.entries()) {
    try {
      const loc = typeof m === "string" ? page.locator(m).first() : m.first();
      await loc.scrollIntoViewIfNeeded({ timeout: 2500 });
      const b = await loc.boundingBox();
      if (b) boxes.push({ idx: i, ...b }); else missing.add(i);
    } catch (e) { missing.add(i); }
  }
  // numbers follow the markers that were really drawn; the legend keeps only their lines (when steps and marks pair up)
  boxes.forEach((b, n) => { b.n = n + 1; });
  let steps = meta.steps;
  if (steps.length === marks.length) steps = steps.filter((_, i) => !missing.has(i));
  else if (missing.size) problems.push(`${role}/${key}: ${missing.size} marker(s) missing and steps do not pair up`);
  if (missing.size && steps.length !== boxes.length) problems.push(`${role}/${key}: legend (${steps.length}) and markers (${boxes.length}) differ`);
  await page.evaluate((list) => {
    for (const b of list) {
      const ring = document.createElement("div"); ring.className = "guide-mark";
      Object.assign(ring.style, { position: "fixed", left: `${b.x - 4}px`, top: `${b.y - 4}px`, width: `${b.width + 8}px`, height: `${b.height + 8}px`,
        border: "3px solid #E8590C", borderRadius: "4px", zIndex: 2147483646, pointerEvents: "none", boxShadow: "0 0 0 3px rgba(255,255,255,.85)" });
      const badge = document.createElement("div"); badge.className = "guide-mark"; badge.textContent = String(b.n);
      Object.assign(badge.style, { position: "fixed", left: `${Math.max(2, b.x - 16)}px`, top: `${Math.max(2, b.y - 16)}px`, width: "26px", height: "26px",
        borderRadius: "50%", background: "#E8590C", color: "#fff", font: "700 15px/26px Arial, sans-serif", textAlign: "center",
        zIndex: 2147483647, pointerEvents: "none", boxShadow: "0 0 0 2px #fff" });
      document.body.append(ring, badge);
    }
  }, boxes);
  const dir = join(OUT, role); await mkdir(dir, { recursive: true });
  const file = `${key}.jpg`;
  await page.screenshot({ path: join(dir, file), type: "jpeg", quality: 80, ...(clip ? { clip } : {}) });
  await page.evaluate(() => document.querySelectorAll(".guide-mark").forEach((el) => el.remove()));
  figures.push({ role, key, file: `${role}/${file}`, ...meta, steps });
  return boxes.length;
}

async function part(name, fn) {
  try { await fn(); console.log("ok  ", name); }
  catch (e) { problems.push(`${name}: ${String(e).split("\n")[0].slice(0, 160)}`); console.log("FAIL", name, String(e).split("\n")[0].slice(0, 160)); }
}

/** Marks and legend for a form in a dialog: one marker per field, in screen order, plus the Save button. */
async function formFigure(page, role, key, title, intro, hints = {}, extraSteps = []) {
  const fields = page.locator(".modal-panel .field, .modal-panel label.filter-chip");
  const n = await fields.count();
  const labels = [];
  for (let i = 0; i < n; i++) {
    const t = (await fields.nth(i).locator("label").first().innerText().catch(() => fields.nth(i).innerText())).replace(/\s+/g, " ").trim();
    labels.push(t);
  }
  const marks = [...Array(n).keys()].map((i) => fields.nth(i));
  const steps = labels.map((l) => (hints[l] ? `${l}: ${hints[l]}` : l));
  const save = page.locator(".modal-panel button.btn", { hasText: /^(Save|Create|Add|Confirm)/ }).first();
  if (await save.count()) { marks.push(save); steps.push(`Press ${(await save.innerText()).trim()}.`); }
  await shot(page, role, key, { title, intro, steps: [...steps, ...extraSteps] }, marks);
}

async function closeModal(page) {
  const c = page.locator(".modal-close, .modal-panel button:has-text('Cancel')").first();
  if (await c.count()) await c.click().catch(() => {}); else await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
}

async function pickRoster(page, label) {
  const sel = page.locator("#main select").first();
  const opts = await sel.locator("option").allInnerTexts();
  const hit = opts.find((o) => o.includes(label));
  if (!hit) throw new Error(`no roster option contains ${label}`);
  await sel.selectOption({ label: hit });
  await page.waitForTimeout(2500);
}

// --------------------------------------------------------------------------- 0. sign in (no account needed)
if (ONLY.includes("admin") || process.env.ONLY === undefined) {
  await part("sign-in screen", async () => {
    const { ctx, page } = await session(null);
    await page.fill("#login-username", "your.username");
    await shot(page, "common", "sign-in", {
      title: "The sign-in page",
      intro: "Open the SAMU Roster address in a browser on a computer or phone.",
      steps: ["Type your username exactly as given to you (capital letters and spaces matter).", "Type your password.", "Press Sign in."],
    }, ["#login-username", "#login-password", "#login-submit"]);
    await ctx.close();
  });
}

// --------------------------------------------------------------------------- per role
const OPEN_BOARD = async (page) => { await goto(page, "Roster Board", "Roster Board"); await pickRoster(page, DRAFT); };

async function commonFigures(role, page, { board = true } = {}) {
  await part(`${role}: dashboard`, async () => {
    await goto(page, "Overview", "Dashboard");
    await shot(page, role, "dashboard", {
      title: "The Dashboard",
      intro: "Your first page after signing in. The menu on the left lists only the pages your role can open.",
      steps: ["The menu: every page you can open. Groups with an arrow open when you click them.", "Setup readiness: what is still missing before a roster can be built well.",
              "Pending review: rosters waiting to be published.", "Active exceptions: rule breaks approved by managers that still need attention."],
    }, ["#nav-tabs", page.locator(".panel", { hasText: "Setup readiness" }), page.locator(".panel", { hasText: "Pending review" }), page.locator(".panel", { hasText: "Active exceptions" })]);
  });
  await part(`${role}: menu footer`, async () => {
    await shot(page, role, "menu-footer", {
      title: "Your name, this manual and signing out",
      intro: "At the bottom of the menu, below your name.",
      steps: ["Your username and role.", "User manual: opens this manual, the one written for your role.", "Log out: ends your session."],
    }, ["#nav-username", "#manual-btn", "#logout-btn"]);
  });
  if (board) {
    await part(`${role}: roster board`, async () => {
      await OPEN_BOARD(page);
      await shot(page, role, "board", {
        title: "The Roster Board",
        intro: "One row per person and one column per day. Each cell holds a shift code.",
        steps: ["Choose the roster. The status (draft, submitted or published) is shown below.", "The grid: blue cells are day shifts, dark cells night shifts, grey cells days off, amber cells leave.",
                "Scroll sideways to see every day. The name column stays in place."],
      }, ["#main select", page.locator(".grid-wrap"), page.locator("table.roster th.name-col")]);
    });
  }
  await part(`${role}: notifications`, async () => {
    await goto(page, "Admin", "Notifications");
    const any = await page.locator("#main button", { hasText: "Mark read" }).count();
    await shot(page, role, "notifications", {
      title: "Notifications",
      intro: "Messages sent to you, for example when a roster is published or a crew change affects you.",
      steps: any ? ["Each message shows what happened and when.", "Press Mark read once you have seen it."] : ["Each message shows what happened and when. None have arrived yet."],
    }, any ? [page.locator("#main .card, #main .panel").first(), page.locator("#main button", { hasText: "Mark read" }).first()] : []);
  });
  await part(`${role}: phone menu`, async () => {
    const { ctx, page: ph } = await session(role, { phone: true });
    await shot(ph, role, "phone-menu-closed", {
      title: "On a phone: the menu button",
      intro: "The same page on a phone. The menu is hidden to leave room for the content.",
      steps: ["Tap the menu button (three lines) at the top right."],
    }, ["#nav-menu-btn"]);
    await ph.tap("#nav-menu-btn"); await ph.waitForTimeout(400);
    const groupToggle = ph.locator("#nav-tabs .group-toggle").first();
    if (await groupToggle.count()) { await groupToggle.tap(); await ph.waitForTimeout(300); }
    await shot(ph, role, "phone-menu-open", {
      title: "On a phone: the open menu",
      intro: "The menu fills the screen. Groups expand where you tap them.",
      steps: ["Tap a group to expand it, then tap the page you want. The menu closes by itself.", "User manual and Log out are at the bottom."],
    }, ["#nav-tabs", "#manual-btn"]);
    await ctx.close();
  });
}

async function boardActionFigures(role, page, { conflicts = true } = {}) {
  await part(`${role}: board actions`, async () => {
    await OPEN_BOARD(page);
    const marks = [page.locator("#main button", { hasText: "Check conflicts" }), page.locator("#main button", { hasText: "Submit for review" })];
    const steps = ["Check conflicts: lists anything that would block publishing.", "Submit for review: sends the roster on when your part is ready."];
    if (await page.locator("#main button", { hasText: "Regenerate" }).count()) { marks.push(page.locator("#main button", { hasText: "Regenerate" })); steps.push("Regenerate: rebuilds only your part of this draft."); }
    await shot(page, role, "board-actions", { title: "What you can do with a draft roster", intro: "These buttons appear above the grid while the roster is a draft.", steps }, marks);
  });
  if (conflicts) await part(`${role}: check conflicts`, async () => {
    await OPEN_BOARD(page);
    await page.locator("#main button", { hasText: "Check conflicts" }).click();
    await page.waitForTimeout(2500);
    await shot(page, role, "board-conflicts", {
      title: "Check conflicts",
      intro: "Each line says what is wrong and how to fix it. Items marked Approved exception were signed off by a manager and do not block publishing.",
      steps: ["The headline: how many conflicts and approved exceptions were found.", "Approved exception: a rule break a manager signed off. It does not block publishing.", "A conflict that blocks publishing: it names the person, the day and how to fix it."],
    }, [page.getByText(/conflicts? .*found|Can.t publish|Clear to publish|No conflicts found/).first(), page.getByText(/Approved exception/).first(), page.getByText("Leave conflict").first()]);
  });
}

async function crewFigures(role, page, { seat }) {
  await part(`${role}: crew rollup`, async () => {
    await goto(page, "Crew Operations", "Crew Rollup");
    await pickRoster(page, DRAFT);
    await page.locator("#main input[type=date]").first().fill("2026-11-10").catch(() => {});
    await page.locator("#main button", { hasText: "Load" }).first().click().catch(() => {});
    await page.waitForTimeout(2500);
    const marks = [page.locator("#main select").first(), page.locator("#main select").nth(1)];
    const steps = ["Choose the roster.", "Choose the station to jump to it."];
    marks.push(page.locator("#main button", { hasText: "Handle shortage" }).first()); steps.push("Handle shortage on the driver: replace the driver or accept the gap.");
    if (seat === "medical" || role === "admin") {
      marks.push(page.locator('[aria-label^="Handle shortage for"]').first()); steps.push("The swap button next to a crew member: replace that person or accept a short crew.");
      marks.push(page.locator('[aria-label^="Set "]').first()); steps.push("Click a Pro's name to make them crew leader.");
    }
    marks.push(page.locator('[aria-label^="Pull "]').first()); steps.push("The calendar-style button: pull this person off the crew for an event.");
    await shot(page, role, "crew-rollup", { title: "Crew Rollup", intro: "Station, ambulance and crew for one roster and one day.", steps }, marks);
  });
  await part(`${role}: handle shortage`, async () => {
    const btn = seat === "medical" ? page.locator('#main [aria-label^="Handle shortage for"]').first() : page.locator("#main button", { hasText: "Handle shortage" }).first();
    await btn.click(); await page.waitForTimeout(2200);
    const panel = page.locator(".modal-panel");
    await panel.evaluate((el) => { el.scrollTop = 0; });
    const legal = panel.getByText("Legal picks").first();
    const none = panel.getByText(/Nobody is fully eligible/).first();
    const hasLegal = (await legal.count()) > 0;
    const marks = [], steps = [];
    if (hasLegal) {
      marks.push(legal); steps.push("Legal picks: people who break no rule, ranked by fairness (least-loaded first).");
      marks.push(panel.locator(".card button, .card .btn").first()); steps.push("Choose a person, then confirm the replacement in the next step.");
    }
    if (await none.count()) { marks.push(none); steps.push("When nobody is legal the window says so. Use a risky pick or leave the crew short (next picture)."); }
    await shot(page, role, "handle-shortage", {
      title: "Handle shortage: choose how to fill the seat",
      intro: "The window opens on the legal picks, ranked by fairness. Scroll down for the other two options.",
      steps,
    }, marks);
    await panel.evaluate((el) => { el.scrollTop = el.scrollHeight; });
    await page.waitForTimeout(400);
    await shot(page, role, "handle-shortage-more", {
      title: "Handle shortage: risky picks, or leave the crew short",
      intro: "Further down the same window.",
      steps: ["Risky picks - needs an override: people who would break a protected rule, such as a rest day. You must give a reason, write a justification and type OVERRIDE to confirm.",
              "Or: leave this crew short. Nobody is moved; the gap is accepted and logged with your reason."],
    }, [panel.getByText("Risky picks").first(), panel.getByText("Or: leave this crew short").first()]);
    await closeModal(page);
  });
}

async function eventFigures(role, page) {
  await part(`${role}: events`, async () => {
    await goto(page, "Events", "Events");
    const marks = [page.locator("#main select").first(), page.locator("#main button", { hasText: "Plan coverage" }).first()];
    const steps = ["Choose the roster to plan against: it must cover the event date.", "Plan coverage: the system proposes who covers the event."];
    if (await page.locator("#main button", { hasText: "Create event" }).count()) { marks.push(page.locator("#main button", { hasText: "Create event" })); steps.push("Create event (administrator only)."); }
    await shot(page, role, "events", { title: "Events", intro: "Events that need extra ambulances and crews.", steps }, marks);
  });
  await part(`${role}: plan coverage`, async () => {
    await page.locator("#main button", { hasText: "Plan coverage" }).first().click();
    await page.waitForSelector("text=Coverage plan", { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(800);
    await shot(page, role, "plan-coverage", {
      title: "Plan coverage",
      intro: "The proposed plan shows who covers the event and what is still missing.",
      steps: ["The headline says Fully covered, or how many ambulances, drivers or medical crew are still short.", "A plan that disrupts regular coverage is marked Risky and needs a reason before it is committed.",
              "Commit this plan when you accept it."],
    }, [page.locator(".modal-panel", { hasText: "Coverage plan" }).locator("text=/Fully covered|Gaps remain/").first(), page.locator(".modal-panel", { hasText: "Coverage plan" }).locator("text=/Risky/").first(), page.locator(".modal-panel button.btn", { hasText: /Commit|Continue/ }).first()]);
    await closeModal(page);
  });
}

async function exceptionFigure(role, page) {
  await part(`${role}: command decisions`, async () => {
    await goto(page, "Admin", "Command Decisions");
    await shot(page, role, "command-decisions", {
      title: "Command Decisions",
      intro: "Every deliberate rule break: who approved it, why, and what it cost.",
      steps: ["Filters: narrow by status or severity.", "Each decision shows the reason and the written justification.", "Mark resolved when it is closed; the administrator also marks safety decisions reviewed."],
    }, [page.locator("#main button", { hasText: "Apply filters" }), page.locator("#main .card").first(), page.locator("#main button", { hasText: /Mark resolved|Mark reviewed/ }).first()]);
  });
}

async function generateFigures(role, page, intro) {
  await part(`${role}: generate page`, async () => {
    await goto(page, "Planning", "Generate Roster");
    const marks = [page.locator("#main select").first(), page.locator("#main input[type=date]").first(), page.locator("#main button", { hasText: /^Generate roster$/ })];
    const steps = ["Range: Day, Week, Month or Custom range.", "Reference date: the range is built around this date; the exact dates are shown below.", "Generate roster: starts the build."];
    if (await page.locator("#main button", { hasText: "Standby settings" }).count()) { marks.push(page.locator("#main button", { hasText: "Standby settings" })); steps.push("Standby settings (administrator): how many reserve people each group keeps."); }
    await shot(page, role, "generate", { title: "Generate Roster", intro, steps }, marks);
  });
}

// ---- admin
async function adminOnly(page) {
  await part("admin: stations", async () => {
    await goto(page, "Setup", "Stations");
    await shot(page, "admin", "stations", { title: "Stations", intro: "Each station produces its own shift codes (code 3 gives D3 and N3).", steps: ["Add station.", "Edit a row, then Save.", "Remove a station only if it never had roster history; otherwise set it inactive."] },
      [page.locator("#main button", { hasText: "Add station" }), page.locator("#main button", { hasText: "Save" }).first(), page.locator("#main button", { hasText: "Remove" }).first()]);
    await page.locator("#main button", { hasText: "Add station" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "station-form", "Add a station", "Fill the form and press Save.", {
      "Code": "the number used in shift codes; it must be unique", "Min drivers/crew": "cannot be larger than the crew size", "Runs nights": "turn off for a day-only station" });
    await closeModal(page);
  });
  await part("admin: ambulances", async () => {
    await goto(page, "Setup", "Ambulances");
    await shot(page, "admin", "ambulances", { title: "Ambulances", intro: "Only active ambulances get crews.", steps: ["Add ambulance.", "Edit: change the plate, type, station or status (active, maintenance, out of service)."] },
      [page.locator("#main button", { hasText: "Add ambulance" }), page.locator("#main button", { hasText: "Edit" }).first()]);
    await page.locator("#main button", { hasText: "Add ambulance" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "ambulance-form", "Add an ambulance", "Fill the form and press Save.");
    await closeModal(page);
  });
  await part("admin: facilities", async () => {
    await goto(page, "Setup", "Facilities");
    await shot(page, "admin", "facilities", { title: "Facilities (fixed posts)", intro: "People posted to a facility, such as the call centre, are not part of the ambulance rotation.", steps: ["Add facility.", "Required headcount: how many people the post needs on every shift."] },
      [page.locator("#main button", { hasText: "Add facility" }), page.locator("text=Required headcount").first()]);
  });
  await part("admin: employees", async () => {
    await goto(page, "Workforce", "Employees");
    await shot(page, "admin", "employees", { title: "Employees", intro: "Everyone who can appear in a roster.", steps: ["Filter by name.", "Bulk update: change a role or flag for many people, with a preview first.", "Add employee.", "On each row: Edit, Mark unavailable (opens the Mark Unavailable page for that person) or Remove."] },
      [page.locator("#main input").first(), page.locator("#main button", { hasText: "Bulk update" }), page.locator("#main button", { hasText: "Add employee" }), page.locator("#main button", { hasText: "Edit" }).first()]);
    await page.locator("#main button", { hasText: "Add employee" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "employee-form", "Add an employee", "Choose a home station or a facility posting, never both.", {
      "Qualified driver": "scheduled by the Drivers Manager", "Pro": "can lead a crew; every crew needs one", "Floats freely": "may work at any station", "Nights only (ad hoc)": "only works nights while ticked", "Cleared for events": "can be pulled for event duty" });
    await closeModal(page);
    await page.locator("#main button", { hasText: "Bulk update" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "bulk-update", "Bulk update", "Choose who to change and what to set. A preview shows who would change before anything is saved.");
    await closeModal(page);
  });
  await part("admin: leave", async () => {
    await goto(page, "Workforce", "Leave");
    await shot(page, "admin", "leave", { title: "Leave and unavailability", intro: "Leave is respected exactly by the roster generator.", steps: ["Filter by employee and dates, then Apply filters.", "Add leave.", "Delete a wrong day."] },
      [page.locator("#main select").first(), page.locator("#main button", { hasText: "Add leave" }), page.locator("#main button", { hasText: "Delete" }).first()]);
    await page.locator("#main button", { hasText: "Add leave" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "leave-form", "Add leave", "One record is created for each day in the range.", { "To (optional)": "leave empty for a single day" });
    await closeModal(page);
  });
  await part("admin: users", async () => {
    await goto(page, "Workforce", "Users");
    await shot(page, "admin", "users", { title: "Users", intro: "Accounts for the four roles.", steps: ["Create user.", "Edit: reset a password, change a role, or deactivate someone."] },
      [page.locator("#main button", { hasText: "Create user" }), page.locator("#main button", { hasText: "Edit" }).first()]);
    await page.locator("#main button", { hasText: "Create user" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "user-form", "Create a user", "The password needs at least 8 characters, not only digits, not a common password.");
    await closeModal(page);
  });
  await part("admin: standby settings", async () => {
    await goto(page, "Planning", "Generate Roster");
    await page.locator("#main button", { hasText: "Standby settings" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "standby-form", "Standby settings", "How many reserve people each group keeps for each day and night shift. Zero means none.");
    await closeModal(page);
  });
  await part("admin: mark unavailable", async () => {
    await goto(page, "Crew Operations", "Mark Unavailable");
    await shot(page, "admin", "mark-unavailable", { title: "Mark Unavailable", intro: "For a single day when someone cannot work.", steps: ["Choose the person.", "Choose the day.", "Find their shift: shows their slot so you can fill it straight away."] },
      [page.locator("#main select").first(), page.locator("#main input[type=date]").first(), page.locator("#main button", { hasText: "Find their shift" })]);
  });
  await part("admin: events form", async () => {
    await goto(page, "Events", "Events");
    await page.locator("#main button", { hasText: "Create event" }).click(); await page.waitForTimeout(500);
    await formFigure(page, "admin", "event-form", "Create an event", "Only the administrator creates events.");
    await closeModal(page);
  });
  await part("admin: activity log", async () => {
    await goto(page, "Admin", "Activity Log");
    await shot(page, "admin", "activity-log", { title: "Activity Log", intro: "Who did what and when.", steps: ["Filter by person, action or dates, then Apply filters.", "Each line is one action, written as a sentence."] },
      [page.locator("#main button", { hasText: "Apply filters" }), page.locator("#main table, #main .card").first()]);
  });
  // generation: start a new week, show the progress, then the result and the publish buttons
  await part("admin: generation progress and result", async () => {
    await goto(page, "Planning", "Generate Roster");
    await page.locator("#main select").first().selectOption({ label: "Week" });
    await page.locator("#main input[type=date]").first().fill("2026-11-18");
    await page.locator("#main button", { hasText: /^Generate roster$/ }).click();
    await page.waitForSelector("text=/Solving/", { timeout: 20000 });
    await page.waitForTimeout(5000);
    await shot(page, "admin", "generate-progress", { title: "While the roster is being built", intro: "A full roster takes about 10 to 15 minutes. You can keep this page open or come back later.", steps: ["The elapsed time. Do not press Generate again."] },
      [page.locator("text=/Solving/").first()]);
    await page.waitForSelector("text=/Roster generated/", { timeout: 240000 });
    await page.waitForTimeout(800);
    await shot(page, "admin", "generate-result", { title: "When it finishes", intro: "The result shows how good the roster is and what is still missing.", steps: ["How many assignments were created.", "Overall quality and an overtime indicator."] },
      [page.locator("text=/Roster generated/").first(), page.locator("text=Overall quality").first()]);
  });
  await part("admin: submit and publish", async () => {
    await goto(page, "Roster Board", "Roster Board");
    await pickRoster(page, "2026-11-16");
    await page.locator("#main button", { hasText: "Submit for review" }).click(); await page.waitForTimeout(2500);
    await shot(page, "admin", "board-submitted", { title: "A submitted roster", intro: "Once submitted, the roster is locked. The administrator publishes it, or reopens it if a part is missing.",
      steps: ["Publish: makes the roster final and notifies everyone in it. It is refused while conflicts remain.", "Reopen to draft: returns it to draft so a missing part can be generated.", "Export to Excel."] },
      [page.locator("#main button", { hasText: "Publish" }), page.locator("#main button", { hasText: "Reopen to draft" }), page.locator("#main button", { hasText: "Export to Excel" })]);
  });
}

// --------------------------------------------------------------------------- run
if (ONLY.includes("admin")) {
  const { ctx, page } = await session("admin");
  await commonFigures("admin", page);
  await adminOnly(page);                                                    // creates records (a third week) - keep it last for admin
  await boardActionFigures("admin", page);
  await crewFigures("admin", page, { seat: "driver" });
  await eventFigures("admin", page);
  await exceptionFigure("admin", page);
  await generateFigures("admin", page, "Admin builds drivers, medical crew and the dispatch desk together.");
  await ctx.close();
}
if (ONLY.includes("drivers_manager")) {
  const { ctx, page } = await session("drivers_manager");
  await commonFigures("drivers_manager", page);
  await part("drivers_manager: ambulances", async () => {
    await goto(page, "Setup", "Ambulances");
    await shot(page, "drivers_manager", "ambulances", { title: "Ambulances", intro: "You keep the fleet list current.", steps: ["Add ambulance.", "Edit: change the plate, type, station or status."] },
      [page.locator("#main button", { hasText: "Add ambulance" }), page.locator("#main button", { hasText: "Edit" }).first()]);
  });
  await generateFigures("drivers_manager", page, "Generates drivers only: medical crew and dispatch are not touched.");
  await boardActionFigures("drivers_manager", page);
  await crewFigures("drivers_manager", page, { seat: "driver" });
  await eventFigures("drivers_manager", page);
  await exceptionFigure("drivers_manager", page);
  await ctx.close();
}
if (ONLY.includes("phtl")) {
  const { ctx, page } = await session("phtl");
  await commonFigures("phtl", page);
  await generateFigures("phtl", page, "Generates medical crew only: drivers and dispatch are not touched.");
  await boardActionFigures("phtl", page);
  await crewFigures("phtl", page, { seat: "medical" });
  await eventFigures("phtl", page);
  await exceptionFigure("phtl", page);
  await ctx.close();
}
if (ONLY.includes("call_center_tl")) {
  const { ctx, page } = await session("call_center_tl");
  await commonFigures("call_center_tl", page);
  await generateFigures("call_center_tl", page, "Generates the dispatch desk's coverage rotation only.");
  await boardActionFigures("call_center_tl", page);
  await ctx.close();
}

await writeFile(join(OUT, ONLY.length === 4 ? "figures.json" : `figures-${ONLY.join("+")}.json`), JSON.stringify(figures, null, 2));
console.log(`\n${figures.length} figures`);
if (problems.length) { console.log("\nPROBLEMS:"); problems.forEach((p) => console.log(" -", p)); }
await browser.close();
