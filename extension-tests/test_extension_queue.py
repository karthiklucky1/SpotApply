"""The apply queue, run by the real extension against a stub SpotApply server.

Founder decision 2026-11-13 (docs/AUTO_APPLY_RESEARCH.md §8): the user approves
a LIST of applications once and the extension's background worker applies to
them one at a time. tests/test_apply_queue.py proves the server's half; this
file loads the real MV3 extension and proves the worker's:

  Q   a mixed list — one the form lets through, a CAPTCHA, an unanswered
      required question, a board the queue only fills — runs ONE AT A TIME, in
      its own window; only the first is submitted (and only after the
      employer's confirmation page), the other three are parked with their
      reasons and their tabs left open
  P   the parked-tab limit pauses the run and it resumes by itself when the
      person clears one
  S   Stop lets the application in progress finish and starts no more
  B   a queue item the server flagged may be submitted while its tab is not on
      screen; one it did not flag waits until the tab is looked at. HEADED
      ONLY: headless Chromium reports every tab as visible, so there this
      block is skipped (the rule itself is pinned, both ways, by
      tests/test_extension_rules.py). To run it:
          SPOTAPPLY_HEADED=1 xvfb-run -a python extension-tests/test_extension_queue.py

The stub plays the server: it hands out one item per /next, refuses to hand
out another while one is unfinished (and records the attempt as a violation),
and records every outcome and every /submit.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
EXT = str(HERE.parent / "extension")
PORT = 8923
BASE = f"http://127.0.0.1:{PORT}"

_CHROME = os.environ.get("SPOTAPPLY_CHROMIUM") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
_LAUNCH_KW = {"executable_path": _CHROME} if _CHROME else {}

RESUME_BYTES = b"PK\x03\x04" + b"harness-resume-docx-payload" * 8

LOCK = threading.Lock()
# The stub's queue: what /next hands out, in order.
SCRIPT: dict = {"items": [], "pos": 0, "limits": {}, "open": None}
PACKS: dict = {}          # app_id -> pack (what GET /api/fill-pack/{id} answers)
OUTCOMES: list = []       # (item_id, reason)
SUBMITS: list = []        # app_id
NEXT_CALLS: list = []     # request bodies
VIOLATIONS: list = []     # /next asked while an item was unfinished


def make_pack(app_id: int, page: str, **over) -> dict:
    pack = {
        "app_id": app_id,
        "job_title": "Senior Engineer",
        "company": f"QueueCo {app_id}",
        "apply_url": f"{BASE}/{page}",
        "first_name": "Alexandra",
        "last_name": "Nguyen",
        "email": "alexandra.nguyen@example.com",
        "phone": "+1 415 555 0199",
        "location": "San Francisco, CA",
        "residence_country": "United States",
        "linkedin_url": "https://linkedin.com/in/alexnguyen-test",
        "github_url": "https://github.com/alexnguyen-test",
        "portfolio_url": "https://alexnguyen.dev",
        "current_title": "Senior Software Engineer",
        "years_experience": 7,
        "work_authorization": "US Citizen",
        "requires_sponsorship": False,
        "gender": "Decline to self-identify",
        "ethnicity": "Decline to self-identify",
        "veteran_status": "I am not a protected veteran",
        "disability_status": "No, I do not have a disability",
        "cover_letter": "Dear Hiring Team, I am excited to apply.",
        "resume_text": "Alexandra Nguyen - Senior Software Engineer",
        "ai_answers": {},
        "work_experience": [{"company": "Globali20 India", "title": "ML Engineer"}],
        "spotapply_url": BASE,
        "auth_token": "harness-test-token",
        # The server's verdict for a queue item it may submit.
        "auto_submit": True, "auto_submit_reason": "ok", "auto_submit_hosts": ["127.0.0.1"],
        "auto_submit_background": True,
    }
    pack.update(over)
    return pack


def load_script(items, **limits):
    """items: [(item_id, pack)]"""
    with LOCK:
        SCRIPT.update(items=list(items), pos=0, open=None,
                      limits=dict({"gap_seconds": 5, "timeout_seconds": 90, "max_parked_tabs": 4}, **limits))
        PACKS.clear()
        for _item_id, pack in items:
            PACKS[pack["app_id"]] = pack
        del OUTCOMES[:], SUBMITS[:], NEXT_CALLS[:], VIOLATIONS[:]


class Stub(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def guess_type(self, path):
        base = super().guess_type(path)
        return "text/html; charset=utf-8" if str(path).endswith((".html", ".htm")) else base

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/resume"):
            import base64
            return self._json({"filename": "Alexandra_Nguyen_Resume.docx",
                               "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               "base64": base64.b64encode(RESUME_BYTES).decode()})
        if "/api/fill-pack/" in path:
            # As the real server answers the content script's routine refresh:
            # a plain GET carries NO approval and knows nothing of the queue.
            try:
                pack = dict(PACKS[int(path.rsplit("/", 1)[-1])])
            except Exception:
                return self._json({"detail": "not found"}, 404)
            pack.update(auto_submit=False, auto_submit_reason="not_requested")
            pack.pop("queue_item_id", None)
            pack.pop("auto_submit_background", None)
            return self._json(pack)
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        path = self.path.split("?")[0]
        if path == "/api/apply-queue/next":
            with LOCK:
                NEXT_CALLS.append(body)
                limits = dict(SCRIPT["limits"])
                if SCRIPT["open"] is not None:
                    VIOLATIONS.append(("next-while-open", SCRIPT["open"]))
                    return self._json(dict(limits, done=False, wait=True, retry_seconds=5))
                if SCRIPT["pos"] >= len(SCRIPT["items"]):
                    return self._json(dict(limits, done=True, reason="empty",
                                           summary={"submitted": len(SUBMITS), "needs_you": len(OUTCOMES)}))
                item_id, pack = SCRIPT["items"][SCRIPT["pos"]]
                SCRIPT["pos"] += 1
                SCRIPT["open"] = (item_id, pack["app_id"])
                out = dict(pack, queue_item_id=item_id)
                return self._json(dict(limits, done=False, remaining=len(SCRIPT["items"]) - SCRIPT["pos"],
                                       pack=out, item={"id": item_id, "application_id": pack["app_id"],
                                                       "company": pack["company"], "title": pack["job_title"],
                                                       "mode": "submit" if pack.get("auto_submit") else "fill_only"}))
        if path.startswith("/api/apply-queue/items/") and path.endswith("/outcome"):
            item_id = int(path.split("/")[-2])
            with LOCK:
                OUTCOMES.append((item_id, body.get("reason")))
                if SCRIPT["open"] and SCRIPT["open"][0] == item_id:
                    SCRIPT["open"] = None
            return self._json({"ok": True, "status": "needs_you"})
        if path.startswith("/application/") and path.endswith("/submit"):
            app_id = int(path.split("/")[-2])
            with LOCK:
                SUBMITS.append(app_id)
                if SCRIPT["open"] and SCRIPT["open"][1] == app_id:
                    SCRIPT["open"] = None
            return self._json({"success": True, "application_id": app_id})
        if "recall-answers" in path:
            return self._json({"answers": []})
        return self._json({"ok": True})


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True)


def worker(ctx):
    """The CURRENT service worker (Chrome may restart an idle MV3 worker)."""
    for _ in range(80):
        if ctx.service_workers:
            return ctx.service_workers[0]
        ctx.pages[0].wait_for_timeout(250)
    raise RuntimeError("no service worker")


def runner(ctx) -> dict:
    for _ in range(3):
        try:
            return worker(ctx).evaluate(
                "() => chrome.storage.local.get(['spotapply_queue']).then(s => s.spotapply_queue || {})")
        except Exception:
            time.sleep(0.5)
    return {}


def wait_for(ctx, pred, seconds, every=0.5):
    """Poll until pred() is truthy; returns its value (or the last falsy one)."""
    end = time.time() + seconds
    val = pred()
    while not val and time.time() < end:
        ctx.pages[0].wait_for_timeout(int(every * 1000))
        val = pred()
    return val


def dashboard(ctx):
    """A page on the dashboard origin that records what the extension tells it."""
    page = ctx.new_page()
    page.goto(f"{BASE}/trigger.html")
    page.wait_for_timeout(1500)
    page.evaluate("""() => {
        window.__states = [];
        window.__alive = true;
        window.addEventListener('message', (e) => {
            if (e.data && e.data.type === 'SPOTAPPLY_QUEUE_STATE') window.__states.push(e.data);
        });
    }""")
    return page


def ask(page, kind, pack=None):
    return page.evaluate("""(a) => new Promise((resolve) => {
        const onMsg = (e) => {
            if (e.data && e.data.type === 'SPOTAPPLY_QUEUE_STATE') {
                window.removeEventListener('message', onMsg);
                resolve(e.data);
            }
        };
        window.addEventListener('message', onMsg);
        window.postMessage({ type: 'SPOTAPPLY_QUEUE_' + a.kind, pack: a.pack }, '*');
        setTimeout(() => resolve({ timeout: true }), 6000);
    })""", {"kind": kind, "pack": pack})


def tabs(ctx):
    return worker(ctx).evaluate(
        "() => chrome.tabs.query({}).then(ts => ts.map(t => ({id: t.id, url: t.url, windowId: t.windowId, active: t.active})))")


def close_queue_tabs(ctx, keep):
    """Close every page except the dashboard one (and Chromium's first tab)."""
    for pg in list(ctx.pages):
        if pg is keep or pg is ctx.pages[0]:
            continue
        try:
            pg.close()
        except Exception:
            pass
    ctx.pages[0].wait_for_timeout(1500)


def main():
    handler = partial(Stub, directory=str(HERE / "www"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        ctx = p.chromium.launch_persistent_context(
            profile, headless=os.environ.get("SPOTAPPLY_HEADED") != "1",
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--disable-features=DisableLoadExtensionCommandLineSwitch"],
            **_LAUNCH_KW)
        worker(ctx)
        dash = dashboard(ctx)
        creds = {"auth_token": "harness-test-token"}

        # ── Q: a mixed list, one at a time ──────────────────────────────────
        print("\nA mixed list runs one at a time (submit / CAPTCHA / question / fill-only)")
        load_script([
            (1, make_pack(101, "greenhouse-submit.html")),
            (2, make_pack(102, "greenhouse-captcha.html")),
            (3, make_pack(103, "greenhouse-submit.html", phone="")),
            (4, make_pack(104, "greenhouse.html", auto_submit=False, auto_submit_reason="host_not_allowed",
                          auto_submit_background=False)),
        ])
        dash_window = next(t["windowId"] for t in tabs(ctx) if "trigger.html" in (t["url"] or ""))
        started = ask(dash, "START", creds)
        check("Q1 the dashboard's Start is answered and the run is on",
              started.get("ok") is True and (started.get("queue") or {}).get("state") == "running", json.dumps(started)[:200])
        check("Q1b the answer never carries a token", "harness-test-token" not in json.dumps(started))

        wait_for(ctx, lambda: 101 in SUBMITS, 90)
        check("Q2 the first job is submitted, after the employer's confirmation page", SUBMITS == [101], str(SUBMITS))
        wait_for(ctx, lambda: len(OUTCOMES) >= 3, 150)
        done = wait_for(ctx, lambda: runner(ctx).get("state") == "idle", 60)
        state = runner(ctx)
        check("Q3 the run ends by itself when the list is empty",
              bool(done) and state.get("reason") == "empty", json.dumps({k: state.get(k) for k in ("state", "reason")}))
        reasons = dict(OUTCOMES)
        check("Q4 the CAPTCHA job is parked as a CAPTCHA, never pressed", reasons.get(2) == "captcha", str(OUTCOMES))
        check("Q5 the job with an unanswered required field is parked for the person",
              reasons.get(3) == "fields_need_you", str(OUTCOMES))
        check("Q6 the board the queue only fills is parked with the server's reason",
              reasons.get(4) == "host_not_allowed", str(OUTCOMES))
        check("Q7 only the job the form let through was submitted", SUBMITS == [101], str(SUBMITS))
        check("Q7b ...and it was never reported as waiting", 1 not in reasons, str(OUTCOMES))
        check("Q8 one at a time: the worker never asked for more while a job was unfinished",
              not VIOLATIONS, str(VIOLATIONS))
        check("Q9 it asks as an idle worker (so the server can release an orphan)",
              bool(NEXT_CALLS) and all(b.get("idle") is True for b in NEXT_CALLS), str(NEXT_CALLS[:2]))
        check("Q10 the worker's own count agrees", state.get("stats") == {"submitted": 1, "parked": 3},
              json.dumps(state.get("stats")))
        open_tabs = tabs(ctx)
        urls = [t["url"] or "" for t in open_tabs]
        check("Q11 the three parked forms are still open for the person",
              sum("greenhouse-captcha.html" in u for u in urls) == 1 and
              sum("greenhouse-submit.html" in u for u in urls) == 1 and
              sum(u.endswith("/greenhouse.html") for u in urls) == 1, str(urls))
        check("Q11b ...and the submitted one closed itself", not any("thanks.html" in u for u in urls), str(urls))
        queue_windows = {t["windowId"] for t in open_tabs
                         if any(x in (t["url"] or "") for x in ("greenhouse", "thanks"))}
        check("Q12 the queue works in its own window, not the dashboard's",
              len(queue_windows) == 1 and dash_window not in queue_windows, f"{queue_windows} vs {dash_window}")
        filled = None
        for pg in ctx.pages:
            if pg.url.endswith("/greenhouse.html"):
                filled = pg.eval_on_selector("#first_name", "el => el.value")
        check("Q13 the fill-only form was filled", filled == "Alexandra", repr(filled))
        heard = dash.evaluate("() => ({n: window.__states.length, last: (window.__states.slice(-1)[0] || {}).queue, alive: window.__alive})")
        check("Q14 the dashboard heard the run and was never reloaded under the person",
              heard["alive"] is True and heard["n"] >= 3 and (heard["last"] or {}).get("state") == "idle",
              json.dumps(heard)[:200])
        close_queue_tabs(ctx, dash)
        check("Q15 closing the parked tabs empties the worker's list", runner(ctx).get("parked") == [],
              json.dumps(runner(ctx).get("parked")))

        # ── P: the parked-tab limit ─────────────────────────────────────────
        print("\nToo many tabs waiting pauses the run; clearing one resumes it")
        load_script([
            (11, make_pack(111, "greenhouse-captcha.html")),
            (12, make_pack(112, "greenhouse-submit.html")),
        ], max_parked_tabs=1)
        ask(dash, "START", creds)
        wait_for(ctx, lambda: OUTCOMES == [(11, "captcha")], 60)
        paused = wait_for(ctx, lambda: runner(ctx).get("state") == "paused", 30)
        check("P1 with one tab waiting and a limit of one, the run pauses",
              bool(paused) and runner(ctx).get("reason") == "parked_limit", json.dumps(runner(ctx).get("reason")))
        calls = len(NEXT_CALLS)
        ctx.pages[0].wait_for_timeout(8000)
        check("P2 ...and opens nothing more while it waits", len(NEXT_CALLS) == calls and not SUBMITS,
              f"{len(NEXT_CALLS)} vs {calls}, {SUBMITS}")
        for pg in list(ctx.pages):
            if "greenhouse-captcha.html" in pg.url:
                pg.close()
        wait_for(ctx, lambda: 112 in SUBMITS, 90)
        check("P3 clearing the waiting tab resumes the run by itself", SUBMITS == [112], str(SUBMITS))
        wait_for(ctx, lambda: runner(ctx).get("state") == "idle", 40)
        close_queue_tabs(ctx, dash)

        # ── S: Stop ─────────────────────────────────────────────────────────
        print("\nStop finishes the application in progress and starts no more")
        load_script([
            (21, make_pack(121, "greenhouse-submit.html")),
            (22, make_pack(122, "greenhouse-submit.html")),
        ])
        ask(dash, "START", creds)
        wait_for(ctx, lambda: bool(runner(ctx).get("current")), 30)
        stopped = ask(dash, "STOP")
        check("S1 Stop is answered: the job in progress is allowed to finish",
              (stopped.get("queue") or {}).get("state") == "stopping", json.dumps(stopped)[:160])
        wait_for(ctx, lambda: 121 in SUBMITS, 90)
        idle = wait_for(ctx, lambda: runner(ctx).get("state") == "idle", 40)
        ctx.pages[0].wait_for_timeout(9000)
        check("S2 the job in progress was submitted", SUBMITS == [121], str(SUBMITS))
        check("S3 ...and the next one was never opened",
              bool(idle) and len(NEXT_CALLS) == 1 and runner(ctx).get("reason") == "stopped",
              f"next calls: {len(NEXT_CALLS)}, reason: {runner(ctx).get('reason')}")
        close_queue_tabs(ctx, dash)

        # ── B: submitting while the tab is not on screen ────────────────────
        print("\nA tab nobody is looking at: only a queue item the server flagged goes ahead")

        def cover_current():
            """Put another tab in front of the job's tab (same window)."""
            cur = wait_for(ctx, lambda: runner(ctx).get("current"), 30)
            if not cur:
                return None
            worker(ctx).evaluate(
                """(tabId) => chrome.tabs.get(tabId).then(t =>
                       chrome.tabs.create({ windowId: t.windowId, url: 'about:blank', active: true }))""",
                cur["tabId"])
            return cur["tabId"]

        load_script([(31, make_pack(131, "greenhouse-submit.html", auto_submit_background=True))],
                    timeout_seconds=150)
        ask(dash, "START", creds)
        covered = cover_current()
        hidden = None
        for pg in ctx.pages:
            if "greenhouse-submit.html" in pg.url:
                hidden = pg.evaluate("() => document.visibilityState")
        if hidden != "hidden":
            print("  SKIP  B1-B4: this browser reports a covered tab as visible (headless). "
                  "Run headed (see the docstring) to cover it.", flush=True)
            wait_for(ctx, lambda: runner(ctx).get("state") == "idle", 90)
        else:
            wait_for(ctx, lambda: 131 in SUBMITS, 140)
            check("B1 the job's tab really was hidden behind another", covered is not None, repr(hidden))
            check("B2 an approved queue item is submitted while its tab is not on screen",
                  SUBMITS == [131], str(SUBMITS))
            wait_for(ctx, lambda: runner(ctx).get("state") == "idle", 40)
            close_queue_tabs(ctx, dash)

            load_script([(32, make_pack(132, "greenhouse-submit.html", auto_submit_background=False))],
                        timeout_seconds=240)
            ask(dash, "START", creds)
            covered = cover_current()
            ctx.pages[0].wait_for_timeout(45000)
            check("B3 without the server's flag the countdown waits for the tab to be seen",
                  not SUBMITS and not OUTCOMES, f"{SUBMITS} {OUTCOMES}")
            try:
                worker(ctx).evaluate("(tabId) => chrome.tabs.update(tabId, { active: true })", covered)
            except Exception as e:
                print(f"  (could not bring the tab forward: {e})", flush=True)
            wait_for(ctx, lambda: 132 in SUBMITS, 60)
            check("B4 ...and goes ahead once the person looks at it", SUBMITS == [132], str(SUBMITS))

        ctx.close()
    httpd.shutdown()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
