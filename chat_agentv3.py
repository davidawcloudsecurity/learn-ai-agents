"""
chat_agentv2.1_playwright.py — AI agent that talks to an Ollama backend and can
drive a real Chrome browser (via PLAYWRIGHT) to join and participate in Microsoft
Teams meetings. This is the Playwright port of chat_agentv2.1_selenium.py.

The model runs on the Ollama backend (see infra_terraform/main.tf, port 11434);
this script sends it prompts over HTTP and lets it call local Python "tools":
    get_weather, calculator, open_url, join_teams, read_teams_chat,
    send_teams_message, leave_meeting.

--------------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------------
    pip install requests playwright
    playwright install chromium

    # Point at your Ollama backend (defaults to the ALB in this file):
    set OLLAMA_HOST=http://<backend-or-alb>:11434     # Windows cmd
    set OLLAMA_MODEL=qwen2.5:1.5b                      # tool-capable model

    # Start Chrome with remote debugging so Playwright attaches to YOUR logged-in
    # session and the meeting persists after the script exits:
    chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\temp\\chrome_debug_profile

    # One-time: stop the native "Open Microsoft Teams?" popup (Chrome CLOSED):
    python chat_agentv2.1_playwright.py --setup-teams

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
    # Plain chat (one-shot):
    python chat_agentv2.1_playwright.py "Explain what an AI agent is in one sentence."

    # Interactive chat loop (tool-enabled, remembers context):
    python chat_agentv2.1_playwright.py

    # One-shot tool call:
    python chat_agentv2.1_playwright.py --tools "What is 42 * 17 and the weather in Tokyo?"

    # Config comes from .env (see .env.example). Example keys:
    #   NLB_CASE, NLB_SR_ACCOUNT, SSH_HOST, MONITOR_SCRIPT, SLACK_WEBHOOK_URL
    python chat_agentv3.py --tools "what's the patch status?"


    # Autonomous Teams mode: join the meeting, then watch the chat and reply
    # on David's behalf until someone says 'bye' (no timeout):
    python chat_agentv2.1_playwright.py --converse https://teams.microsoft.com/meet/<id>?p=<key>
    

--------------------------------------------------------------------------------
NOTES
--------------------------------------------------------------------------------
- Tool calling needs a model trained for it. smollm:1.7b is NOT reliable; prefer
  qwen2.5:1.5b/3b or llama3.1:8b.
- join_teams assesses each screen (launcher -> pre-join -> in-meeting) and uses
  JavaScript clicks so the tab-modal Teams popup can't block them.
- Browser tools run on the machine executing this script, NOT on the backend.
- Playwright connects over CDP to your existing Chrome; on exit it DISCONNECTS
  (does not close your Chrome), which also avoids the Node-driver EPIPE crash.
"""

import os
import sys
import json
import time
import atexit
import subprocess

import requests


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency).

    Reads KEY=VALUE lines from a .env file next to this script and sets them in
    os.environ (without overriding vars already set in the real environment).
    Keeps secrets (SR/account, Slack, SSH) out of the source so .env can be
    gitignored.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Real environment variables take precedence over .env.
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:  # noqa: BLE001
        print(f"[env] failed to read {env_path}: {e}")


_load_dotenv()

# --- NLB patch monitor (run over SSH on demand) ------------------------------
# When someone in the meeting asks for a status update, the agent runs:
#   ssh <SSH_HOST> "<MONITOR_SCRIPT> <NLB_CASE> <NLB_SR_ACCOUNT> --once"
# All sensitive values come from .env / environment (see .env.example).
SSH_HOST = os.environ.get("SSH_HOST", "")
MONITOR_SCRIPT = os.environ.get("MONITOR_SCRIPT", "")
NLB_CASE = os.environ.get("NLB_CASE", "")
NLB_SR_ACCOUNT = os.environ.get("NLB_SR_ACCOUNT", "")

# Where the Ollama server lives.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

# The display name this agent posts under in Teams (used to tell our own
# messages apart from other people's). Must match the join display name.
AGENT_NAME = os.environ.get("AGENT_NAME", "David's AI Agent")

# Connect/read timeout in seconds (model generation can be slow on t3.medium).
TIMEOUT = (10, 120)

# Slack workflow-trigger webhook for error alerts (from .env / environment).
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def notify_slack(text: str) -> bool:
    """Post a message to Slack via the configured webhook. No-op if unset."""
    if not SLACK_WEBHOOK_URL:
        print("[slack] SLACK_WEBHOOK_URL not set; skipping notification.")
        return False
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"message": text}, timeout=10)
        ok = 200 <= resp.status_code < 300
        print(f"[slack] {'sent' if ok else 'failed ('+str(resp.status_code)+')'}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[slack] error sending notification: {e}")
        return False


# =============================================================================
# 1. THE TOOLS — real Python functions the model can ask us to run.
# =============================================================================

def get_weather(city: str) -> str:
    """Fake weather lookup. In real life this would call a weather API."""
    fake_db = {
        "tokyo": "22°C, clear skies",
        "seattle": "14°C, light rain",
        "singapore": "31°C, humid with afternoon thunderstorms",
    }
    return fake_db.get(city.lower(), f"No weather data for {city}.")


def calculator(expression: str) -> str:
    """Evaluate a simple arithmetic expression, e.g. '42 * 17'."""
    allowed = {"__builtins__": {}}
    try:
        result = eval(expression, allowed, {})  # noqa: S307 - sandboxed above
        return str(result)
    except Exception as e:  # noqa: BLE001
        return f"Error evaluating '{expression}': {e}"


def get_patch_status(case: str = "", sr_account: str = "") -> str:
    """Run the NLB patch monitor over SSH (one cycle) and return the summary.

    Use this when someone asks for a patching update, e.g. "any updates?",
    "how many are left?", "what's the status?". Runs:
        ssh <SSH_HOST> "<MONITOR_SCRIPT> <case> <sr_account> --once"
    and returns the cycle summary (totals + outstanding instances).
    """
    case = case or NLB_CASE
    sr_account = sr_account or NLB_SR_ACCOUNT
    if not case or not sr_account:
        return ("No case/SR account set. Provide them or set NLB_CASE and "
                "NLB_SR_ACCOUNT env vars.")

    remote_cmd = f"{MONITOR_SCRIPT} {case} {sr_account} --once"
    try:
        proc = subprocess.run(
            ["ssh", SSH_HOST, remote_cmd],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",  # monitor output has emojis
        )
    except FileNotFoundError:
        return "ssh not found on PATH. Install/enable OpenSSH client."
    except subprocess.TimeoutExpired:
        return "Patch monitor timed out (no result within 5 minutes)."

    out = (proc.stdout or "") + (proc.stderr or "")
    if not out.strip():
        return f"No output from monitor (exit {proc.returncode})."

    # Extract the concise cycle summary if present, else return the tail.
    lines = out.splitlines()
    summary = []
    capture = False
    for ln in lines:
        if "summary:" in ln.lower() and "cycle" in ln.lower():
            capture = True
            summary = [ln.strip()]
            continue
        if capture:
            if "full log" in ln.lower() or "monitor finished" in ln.lower():
                break
            if ln.strip():
                summary.append(ln.strip())

    if summary:
        return "\n".join(summary)
    return "\n".join(lines[-15:])  # fallback: last 15 lines


# Chrome remote-debugging endpoint. Start Chrome first with:
#   chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\temp\chrome_debug_profile
# so the browser (and your logged-in session) persists across runs. Playwright
# attaches to this via connect_over_cdp.
CDP_URL = "http://127.0.0.1:9222"

# The Chrome profile directory used with --user-data-dir. Used by
# --setup-teams to disable the "Open Microsoft Teams?" native popup.
CHROME_USER_DATA_DIR = os.environ.get(
    "CHROME_USER_DATA_DIR", r"C:\temp\chrome_debug_profile"
)


def setup_teams_no_prompt(user_data_dir: str = CHROME_USER_DATA_DIR) -> str:
    """Disable Chrome's 'Open Microsoft Teams?' native popup for a profile.

    The launcher page auto-fires the msteams:// deep link, which makes Chrome
    show a tab-MODAL native dialog. While it's up, the page can't receive
    clicks, so the browser can't press 'Continue on this browser'. Marking the
    scheme as excluded tells Chrome to silently decline it (no popup), so the
    launcher goes straight to the web-join buttons.

    IMPORTANT: Chrome using this profile must be CLOSED when you run this,
    or Chrome will overwrite the file on exit.
    """
    prefs_path = os.path.join(user_data_dir, "Default", "Preferences")
    if not os.path.exists(prefs_path):
        return (
            f"Preferences not found at {prefs_path}. Launch Chrome once with "
            f"--user-data-dir={user_data_dir} so the profile is created, then retry."
        )

    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)

        ph = prefs.setdefault("protocol_handler", {})
        excluded = ph.setdefault("excluded_schemes", {})
        excluded["msteams"] = True
        excluded["msteams-enterprise"] = True

        with open(prefs_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f)

        return (
            f"Done. Disabled the Teams app prompt in {prefs_path}.\n"
            "Make sure Chrome was closed; restart it with the same "
            "--user-data-dir and the popup will no longer appear."
        )
    except Exception as e:  # noqa: BLE001
        return f"Failed to update {prefs_path}: {e}"


# =============================================================================
# BROWSER LAYER (Playwright)
# Keep Playwright + browser handles at module scope so they persist across tool
# calls, and shut them down cleanly on exit to avoid the Node-driver EPIPE crash.
# =============================================================================

_PW = None
_BROWSER = None


def _connect_browser():
    """Return a Playwright Browser, reusing one across tool calls.

    Connects over CDP to a Chrome started with --remote-debugging-port=9222
    (so your logged-in session persists); if none is running, launches a new
    non-headless Chrome.
    """
    global _PW, _BROWSER
    if _BROWSER is not None:
        return _BROWSER

    from playwright.sync_api import sync_playwright

    if _PW is None:
        _PW = sync_playwright().start()

    try:
        _BROWSER = _PW.chromium.connect_over_cdp(CDP_URL)
        print("  [browser] connected to existing Chrome on 127.0.0.1:9222")
    except Exception:
        _BROWSER = _PW.chromium.launch(headless=False, channel="chrome")
        print("  [browser] launched a new Chrome window")
    return _BROWSER


def _context():
    """Return a browsing context (reuse the first one when attached over CDP)."""
    browser = _connect_browser()
    return browser.contexts[0] if browser.contexts else browser.new_context()


def _new_page():
    """Open a new tab/page in the shared context."""
    return _context().new_page()


def _shutdown_browser() -> None:
    """Cleanly tear down Playwright on exit. For a CDP-connected browser this
    DISCONNECTS (leaves your Chrome + meeting running) and prevents EPIPE."""
    global _PW, _BROWSER
    try:
        if _BROWSER is not None:
            _BROWSER.close()
    except Exception:
        pass
    try:
        if _PW is not None:
            _PW.stop()
    except Exception:
        pass
    _BROWSER = None
    _PW = None


atexit.register(_shutdown_browser)


def open_url(url: str) -> str:
    """Open a URL in a real Chrome browser via Playwright (new tab)."""
    if not url.startswith(("http://", "https://")):
        return f"Refused to open non-http(s) URL: {url}"
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return ("Playwright is not installed. Run:\n"
                "  pip install playwright\n  playwright install chromium")
    try:
        page = _new_page()
        page.goto(url, wait_until="domcontentloaded")
        return f"Opened {url} (page title: {page.title()!r})"
    except Exception as e:  # noqa: BLE001
        return f"Failed to open {url} in browser: {e}"


# --- Selectors that identify each Teams "screen" ---------------------------
_SEL_LAUNCHER = [
    "button[data-tid='joinOnWeb']",
    "button[aria-label='Join meeting from this browser']",
]
_SEL_PREJOIN_JOIN = [
    "button[data-tid='prejoin-join-button']",
    "#prejoin-join-button",
]
_SEL_NAME_INPUT = [
    "input[data-tid='prejoin-display-name-input']",
    "input[placeholder='Type your name']",
]
_SEL_IN_MEETING = [
    "div[data-tid='ckeditor']",
    "[data-tid='chat-pane-item']",
    "button[data-tid='hangup-main-btn']",
    "#hangup-button",
]
_SEL_CHAT_TOGGLE = [
    "button[data-tid='chat-button']",
    "#chat-button",
    "button[aria-label='Chat']",
]
_SEL_LEAVE = [
    "#hangup-button",
    "button[title='Leave']",
    "button[data-tid='hangup-main-btn']",
    "button[aria-label='Leave']",
]

_TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com", "teams.microsoft.us")


def _first_visible(page, selectors):
    """Return the first VISIBLE Locator matching any selector on the page,
    else None."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(n):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:
            continue
    return None


def _js_click(page, locator) -> None:
    """Click via JavaScript so the tab-modal popup can't block the click."""
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    locator.evaluate("el => el.click()")


def _has(page, selectors) -> bool:
    return _first_visible(page, selectors) is not None


def _assess_teams_screen(page) -> str:
    """Report which Teams screen the page is on.

    One of: 'in_meeting', 'prejoin', 'launcher', 'auth', 'unknown'.
    """
    if _has(page, _SEL_IN_MEETING):
        return "in_meeting"
    if _has(page, _SEL_PREJOIN_JOIN):
        return "prejoin"
    if _has(page, _SEL_LAUNCHER):
        return "launcher"
    url = (page.url or "").lower()
    if "login" in url or "sign" in url:
        return "auth"
    return "unknown"


def _teams_page():
    """Find the open Teams page (by host, then by in-meeting DOM). Or None."""
    browser = _connect_browser()
    # Pass 1: match by known Teams hosts.
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                url = (page.url or "").lower()
                if any(h in url for h in _TEAMS_HOSTS):
                    return page
            except Exception:
                continue
    # Pass 2: match by DOM.
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                if _has(page, _SEL_IN_MEETING) or _has(
                    page, ["[data-tid='chat-pane-item']", "div[data-tid='ckeditor']"]
                ):
                    return page
            except Exception:
                continue
    return None


def _turn_off_camera_and_mic(page) -> None:
    """On the pre-join screen, turn the camera OFF and mute the mic.

    The toggles are switches whose TITLE reveals current state:
      camera ON  -> title "Turn camera off"  (data-tid=toggle-video)
      mic ON     -> title "Mute mic"          (data-tid=toggle-mute)
    So if the title says it can be turned off/muted, it's currently on and we
    click it. The switch <input> is often visually hidden, so we click by
    JavaScript and don't require visibility.
    """
    for tid, on_title in (("toggle-video", "turn camera off"),
                          ("toggle-mute", "mute mic")):
        try:
            el = page.locator(f"input[data-tid='{tid}']").first
            if el.count() == 0:
                continue
            title = (el.get_attribute("title") or "").lower()
            if on_title in title:
                el.evaluate("e => e.click()")
                print(f"  [prejoin] turned off {tid}")
        except Exception:
            continue


def join_teams(url: str, display_name: str = "David's AI Agent") -> str:
    """Open a Teams meeting link and drive through EVERY screen until in-meeting.

      launcher   -> click "Continue on this browser"
      prejoin    -> type a display name (if asked) and click "Join now"
      in_meeting -> open the chat pane and finish
      auth       -> stop and report that sign-in is required
    """
    if not url.startswith(("http://", "https://")):
        return f"Refused to open non-http(s) URL: {url}"
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return ("Playwright is not installed. Run:\n"
                "  pip install playwright\n  playwright install chromium")

    try:
        page = _new_page()
        page.goto(url, wait_until="domcontentloaded")

        deadline = time.time() + 90
        last_screen = None
        av_off_done = False  # only toggle camera/mic once on the prejoin screen

        while time.time() < deadline:
            screen = _assess_teams_screen(page)
            if screen != last_screen:
                print(f"  [join] screen={screen} url={page.url!r} "
                      f"title={page.title()!r}")
                last_screen = screen

            if screen == "in_meeting":
                if not _has(page, ["div[data-tid='ckeditor']"]):
                    toggle = _first_visible(page, _SEL_CHAT_TOGGLE)
                    if toggle:
                        try:
                            _js_click(page, toggle)
                        except Exception:
                            pass
                return "In the meeting. Chat pane ready."

            if screen == "launcher":
                btn = _first_visible(page, _SEL_LAUNCHER)
                if btn:
                    _js_click(page, btn)

            elif screen == "prejoin":
                name_box = _first_visible(page, _SEL_NAME_INPUT)
                if name_box:
                    try:
                        if not name_box.input_value():
                            name_box.fill(display_name)
                    except Exception:
                        pass
                if not av_off_done:
                    _turn_off_camera_and_mic(page)  # join muted, camera off
                    av_off_done = True
                btn = _first_visible(page, _SEL_PREJOIN_JOIN)
                if btn:
                    _js_click(page, btn)

            elif screen == "auth":
                return (
                    "Reached a sign-in page. Log in to Teams in this Chrome "
                    f"profile first, then re-run. (url={page.url})"
                )

            time.sleep(1.5)

        return (
            "Timed out before reaching the meeting.\n"
            f"  Last screen: {last_screen}\n"
            f"  URL: {page.url}\n"
            f"  Title: {page.title()}"
        )
    except Exception as e:  # noqa: BLE001
        return f"Failed to join Teams meeting: {e}"


def _chat_pane_open(page) -> bool:
    """True if the chat compose box is visible (chat rail is open)."""
    return _has(page, ["div[data-tid='ckeditor']"])


def _ensure_chat_open(page, wait_seconds: int = 15) -> bool:
    """Make sure the chat rail is open, retrying while the meeting UI loads."""
    deadline = time.time() + wait_seconds
    clicked = False
    while time.time() < deadline:
        if _chat_pane_open(page):
            return True
        toggle = _first_visible(page, _SEL_CHAT_TOGGLE)
        if toggle and not clicked:
            try:
                _js_click(page, toggle)
                clicked = True
                print("  [chat] clicked chat button to open the pane")
            except Exception:
                pass
        time.sleep(1)
    ok = _chat_pane_open(page)
    if not ok:
        print("  [chat] chat pane still not open (button not found or not ready)")
    return ok


def _parse_chat_messages(page, limit: int = 20):
    """Parse recent chat items into {kind, author, text, is_self} (old->new)."""
    out = []
    items = page.query_selector_all("[data-tid='chat-pane-item']")
    for item in items[-limit:]:
        try:
            body = item.query_selector("[data-tid='chat-pane-message']")
            if body:
                author_el = item.query_selector("[data-tid='message-author-name']")
                author = author_el.inner_text().strip() if author_el else ""
                content_el = item.query_selector("[id^='content-']")
                raw = content_el.inner_text() if content_el else body.inner_text()
                text = " ".join(raw.split())
                is_self = (
                    item.query_selector("[class*='ChatMyMessage']") is not None
                    or author == AGENT_NAME
                )
                if text:
                    out.append({"kind": "message", "author": author,
                                "text": text, "is_self": is_self})
                continue

            ctrl = item.query_selector("[data-tid='control-message-renderer']")
            if ctrl:
                text = " ".join(ctrl.inner_text().split())
                if text:
                    out.append({"kind": "control", "author": "",
                                "text": text, "is_self": False})
        except Exception:
            continue
    return out


# Phrases that mean "give me a patching status update".
_STATUS_KEYWORDS = (
    "update", "updates", "status", "how many", "left", "remaining",
    "progress", "patched", "done yet", "any news",
)


def _is_status_request(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in _STATUS_KEYWORDS)


def _format_status_reply(status: str) -> str:
    """Build a short, deterministic status line from the monitor output.

    Avoids the model padding/hallucinating. Parses the totals line, e.g.:
        - Total: 2 | Patched: 0 | Pending: 2 | Failed: 0
    and returns e.g. "Patching update: 0/2 patched, 2 pending, 0 failed."
    """
    import re as _re
    total = patched = pending = failed = None
    for key in ("total", "patched", "pending", "failed"):
        m = _re.search(rf"{key}\s*[:=]\s*(\d+)", status, _re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if key == "total":
                total = val
            elif key == "patched":
                patched = val
            elif key == "pending":
                pending = val
            elif key == "failed":
                failed = val

    if total is not None and patched is not None:
        parts = [f"{patched}/{total} patched"]
        if pending is not None:
            parts.append(f"{pending} pending")
        if failed:
            parts.append(f"{failed} failed")
        return "Patching update: " + ", ".join(parts) + "."

    # "All compliant" case: the monitor's input list is empty, meaning there
    # is nothing left to patch.
    low_all = status.lower()
    if "compliant" in low_all or "is empty" in low_all:
        return "All instances are patched - nothing pending."

    # Fallback: couldn't parse totals -> return a meaningful line, skipping
    # monitor log noise (timestamps, banners, config echoes).
    noise = ("===", "monitor starting", "monitor finished", "cycle",
             "work dir", "deadline", "interval", "case:", "full log",
             "single cycle")
    for ln in status.splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if s.startswith("[") or any(n in low for n in noise):
            continue
        return s
    return "No patching status available yet."


import re  # noqa: E402

# Fixed correction when someone greets/addresses David directly.
DAVID_CORRECTION = (
    "Hi! I'm David's assistant, not David himself - happy to help or relay a "
    "message."
)

# Matches greetings aimed at David: "hi david", "hello david", "hey david",
# "david?", "you there david", etc.
_GREET_DAVID_RE = re.compile(
    r"\b(hi|hello|hey|yo|hiya|morning|afternoon|evening)\b[\s,]*david\b",
    re.IGNORECASE,
)


def _is_greeting_to_david(text: str) -> bool:
    return bool(_GREET_DAVID_RE.search(text or ""))


def _strip_name_prefix(reply: str, names=None) -> str:
    """Remove a leading 'Name, ' / 'Name: ' salutation the model may add
    (e.g. 'David, ...' or 'David AKC: ...') so replies don't address by name.

    Only strips known names (the sender + 'David'), NOT generic openers like
    'Yes,' or 'Sure,'.
    """
    if not reply:
        return reply
    known = {"david", "david akc"}
    for n in (names or []):
        if n:
            known.add(n.lower())
    m = re.match(r"^\s*([A-Za-z][A-Za-z.'\- ]*?)\s*[,:]\s+", reply)
    if m and m.group(1).strip().lower() in known:
        return reply[m.end():]
    return reply


def _latest_incoming_message(page):
    """Most recent real message from someone OTHER than this agent, or None."""
    for msg in reversed(_parse_chat_messages(page, limit=20)):
        if msg["kind"] == "message" and not msg["is_self"]:
            return msg
    return None


def read_teams_chat(limit: int = 20) -> str:
    """Read the most recent messages from the open Teams meeting chat pane."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return ("Playwright is not installed. Run:\n"
                "  pip install playwright\n  playwright install chromium")

    try:
        page = _teams_page()
        if page is None:
            return "No Teams tab is open. Join a meeting first with join_teams."

        _ensure_chat_open(page)

        parsed = _parse_chat_messages(page, limit=limit)
        if not parsed:
            return "No chat messages found (chat pane may still be loading)."

        lines = []
        for m in parsed:
            if m["kind"] == "control":
                lines.append(f"* {m['text']}")
            else:
                lines.append(f"{m['author'] or 'Unknown'}: {m['text']}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"Failed to read Teams chat: {e}"


def send_teams_message(message: str) -> str:
    """Type a message into the Teams meeting chat and send it."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return ("Playwright is not installed. Run:\n"
                "  pip install playwright\n  playwright install chromium")

    if not message.strip():
        return "Refused to send an empty message."

    try:
        page = _teams_page()
        if page is None:
            return "No Teams tab is open. Join a meeting first with join_teams."

        box = page.locator("div[data-tid='ckeditor'][contenteditable='true']").first
        box.wait_for(state="visible", timeout=15000)
        box.click()
        page.keyboard.type(message)

        # Prefer the explicit Send button; fall back to Ctrl+Enter.
        try:
            page.locator("button[data-tid='newMessageCommands-send']").first.click(
                timeout=5000
            )
        except Exception:
            page.keyboard.press("Control+Enter")

        return f"Sent message to Teams chat: {message!r}"
    except Exception as e:  # noqa: BLE001
        return f"Failed to send Teams message: {e}"


def leave_meeting() -> str:
    """Click the Teams 'Leave' button to hang up / end the meeting."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return ("Playwright is not installed. Run:\n"
                "  pip install playwright\n  playwright install chromium")
    try:
        page = _teams_page()
        if page is None:
            return "No Teams tab is open."
        btn = _first_visible(page, _SEL_LEAVE)
        if btn:
            _js_click(page, btn)
            return "Clicked Leave - left the meeting."
        # Fallback: the control bar may be hidden -> keyboard shortcut.
        try:
            page.keyboard.press("Control+Shift+H")
            return "Sent Leave shortcut (Ctrl+Shift+H) - left the meeting."
        except Exception:
            return "Leave button not found and Ctrl+Shift+H failed."
    except Exception as e:  # noqa: BLE001
        return f"Failed to leave meeting: {e}"


# Opening message the agent posts when it joins the meeting chat.
INTRO_MESSAGE = os.environ.get(
    "INTRO_MESSAGE",
    "Hello, I'm David's AI assistant sitting in for David. "
    "I'll update the status of those instances every 30 mins until all instances are patched. "
    "Feel free to chat with me here - say 'bye' when you're done.",
)


CONVERSE_SYSTEM_PROMPT = (
    "You are David's assistant, standing in for David in a Microsoft Teams "
    "meeting chat. You are NOT David. "
    "Reply briefly and naturally (1-2 sentences) to the most recent message. "
    "Do NOT begin your reply with the sender's name or address them by name; "
    "just answer the message directly. "
    "If someone addresses you as David or greets David directly (e.g. "
    "'hello David', 'hey David'), politely correct them: clarify that you are "
    "David's assistant, not David, and offer to help or relay a message. "
    "When asked for a patching update/status or how many instances are left, "
    "you MUST call the get_patch_status tool and reply with the real numbers - "
    "never guess or say it's 'being updated' without checking. "
    "Do not narrate your actions."
)


def converse_teams(poll_seconds: int = 3, max_chat_fails: int = 3) -> None:
    """Autonomously watch the Teams chat and reply until someone says 'bye'.

    If the chat pane fails to open `max_chat_fails` times in a row, send a Slack
    message with the reason, leave the meeting, and stop.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("Playwright is not installed. Run:\n"
              "  pip install playwright\n  playwright install chromium")
        return

    page = _teams_page()
    if page is None:
        print("No Teams tab is open. Join a meeting first (join_teams).")
        return

    if _ensure_chat_open(page):
        send_teams_message(INTRO_MESSAGE)
        print(f"me   > {INTRO_MESSAGE}")

    print(f"[converse] watching Teams chat every {poll_seconds}s "
          f"(agent name: {AGENT_NAME!r}). "
          "Say 'bye' in the chat to stop. Ctrl-C to abort.\n")

    messages: list = [{"role": "system", "content": CONVERSE_SYSTEM_PROMPT}]
    last_processed = None
    chat_fail_count = 0

    try:
        while True:
            # Re-acquire the Teams page in case tabs changed, and keep chat open.
            page = _teams_page() or page
            if page is None:
                time.sleep(poll_seconds)
                continue
            if not _chat_pane_open(page):
                if not _ensure_chat_open(page):
                    chat_fail_count += 1
                    print(f"[converse] chat pane not open "
                          f"({chat_fail_count}/{max_chat_fails})")
                    if chat_fail_count >= max_chat_fails:
                        reason = (
                            f"Teams chat pane failed to open after "
                            f"{max_chat_fails} attempts (agent: {AGENT_NAME}). "
                            "Leaving the meeting."
                        )
                        print(f"[converse] {reason}")
                        notify_slack(f":warning: {reason}")
                        print(f"[converse] {leave_meeting()}")
                        break
                    time.sleep(poll_seconds)
                    continue
                # Chat opened successfully -> reset the failure counter.
                chat_fail_count = 0

            incoming = _latest_incoming_message(page)
            if not incoming:
                time.sleep(poll_seconds)
                continue

            author = incoming["author"] or "Someone"
            latest = incoming["text"]
            fingerprint = f"{author}|{latest}"
            if fingerprint == last_processed:
                time.sleep(poll_seconds)
                continue

            print(f"{author} > {latest}")
            last_processed = fingerprint

            # HARD TRIGGER: greeted as David -> send the fixed correction.
            if _is_greeting_to_david(latest):
                print("  [converse] greeted as David -> correcting identity")
                send_teams_message(DAVID_CORRECTION)
                messages.append({"role": "assistant", "content": DAVID_CORRECTION})
                print(f"me   > {DAVID_CORRECTION}")
                time.sleep(poll_seconds)
                continue

            # Stop condition: the other person said bye/goodbye.
            low = latest.lower()
            if "bye" in low or "goodbye" in low:
                farewell = "Bye! Talk to you later."
                send_teams_message(farewell)
                print(f"me   > {farewell}")
                time.sleep(2)  # let the farewell post before we leave
                print(f"[converse] {leave_meeting()}")
                print("[converse] heard 'bye' - ending conversation.")
                break

            # HARD TRIGGER: small models often won't call the tool, so if the
            # message clearly asks for a status update, run get_patch_status
            # ourselves and have the model just phrase the REAL numbers.
            if _is_status_request(latest):
                print("  [converse] status request -> running get_patch_status")
                status = get_patch_status()
                # Send a concise, deterministic summary - do NOT let the small
                # model rephrase/pad it (it waffles and invents detail).
                reply = _format_status_reply(status)
                messages.append({"role": "assistant", "content": reply})
                send_teams_message(reply)
                print(f"me   > {reply}")
                time.sleep(poll_seconds)
                continue

            # Otherwise: normal reply, routed through agent() so it can still
            # call tools if the model decides to.
            transcript = read_teams_chat(limit=20)
            user_turn = (
                "Recent Teams chat (oldest to newest):\n"
                f"{transcript}\n\n"
                f"Latest message: {latest!r}\n"
                "Write a direct reply. Do not start with a name or greeting."
            )

            reply, messages = agent(user_turn, messages)
            reply = _strip_name_prefix((reply or "").strip(), [author])
            if not reply:
                time.sleep(poll_seconds)
                continue

            send_teams_message(reply)
            print(f"me   > {reply}")

            time.sleep(poll_seconds)
    except (KeyboardInterrupt, EOFError):
        print("\n[converse] aborted.")
    except Exception as e:  # noqa: BLE001
        # e.g. Playwright TargetClosedError when the meeting tab/browser was
        # closed. Alert Slack instead of dying with a bare traceback.
        msg = (f"[converse] crashed: {type(e).__name__}: {e}. "
               "The Teams tab/browser may have been closed.")
        print(msg)
        notify_slack(f":warning: Teams AI agent stopped. {msg}")


# Map tool NAME -> the actual Python function to run.
TOOL_REGISTRY = {
    "get_weather": get_weather,
    "calculator": calculator,
    "open_url": open_url,
    "join_teams": join_teams,
    "read_teams_chat": read_teams_chat,
    "send_teams_message": send_teams_message,
    "leave_meeting": leave_meeting,
    "get_patch_status": get_patch_status,
}


# =============================================================================
# 2. THE TOOL SCHEMAS — the JSON description sent to the model.
# =============================================================================

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a given city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string",
                             "description": "The city name, e.g. 'Tokyo'."}
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate a basic arithmetic expression like '42 * 17'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string",
                                   "description": "A math expression using + - * / and numbers."}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open a web link (URL) in the user's default browser. "
                "Use this for generic websites or dashboards."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "The full http(s) URL to open."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "join_teams",
            "description": (
                "Open a Microsoft Teams meeting link and join it in the web "
                "browser by clicking 'Continue on this browser'. Use this "
                "specifically for teams.microsoft.com meeting links."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "The full Teams meeting URL."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_teams_chat",
            "description": (
                "Read the most recent messages from the currently open "
                "Microsoft Teams meeting chat pane."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer",
                              "description": "How many recent messages to read (default 20)."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_teams_message",
            "description": (
                "Type and send a message into the currently open Microsoft "
                "Teams meeting chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string",
                                "description": "The message text to send."}
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leave_meeting",
            "description": (
                "Leave/hang up the current Microsoft Teams meeting by clicking "
                "the Leave button. Use when the conversation is over."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_patch_status",
            "description": (
                "Get the latest NLB patching status by running the monitor over "
                "SSH. Use whenever someone asks for an update on patching, e.g. "
                "'any updates?', 'how many are left?', 'what's the status?'. "
                "Returns totals (patched/pending/failed) and outstanding instances."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "case": {"type": "string",
                             "description": "SR/case number (optional; defaults to NLB_CASE)."},
                    "sr_account": {"type": "string",
                                   "description": "SR account id (optional; defaults to NLB_SR_ACCOUNT)."},
                },
                "required": [],
            },
        },
    },
]


# =============================================================================
# 3. LOW-LEVEL CALL — one round trip to Ollama's /api/chat.
# =============================================================================

def _chat(messages: list, tools: list | None = None) -> dict:
    """Send messages (and optionally tool schemas) to Ollama; return the message."""
    payload = {"model": MODEL, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools

    resp = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["message"]


# =============================================================================
# 4. PLAIN CHAT (no tools)
# =============================================================================

def ask(prompt: str, history: list | None = None) -> str:
    """Send a single message to the agent and return its reply (no tools)."""
    history = history or []
    messages = history + [{"role": "user", "content": prompt}]
    return _chat(messages)["content"]


# =============================================================================
# 5. THE AGENT LOOP — this is where tool calling actually happens.
# =============================================================================

def agent(prompt: str, messages: list | None = None, max_steps: int = 5):
    """Run one tool-calling turn and return (reply, messages)."""
    if messages is None:
        messages = []
    messages.append({"role": "user", "content": prompt})

    for step in range(max_steps):
        message = _chat(messages, tools=TOOLS_SCHEMA)
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return message.get("content", ""), messages

        for call in tool_calls:
            fn_name = call["function"]["name"]
            fn_args = call["function"]["arguments"]

            print(f"  [tool call] {fn_name}({fn_args})")

            fn = TOOL_REGISTRY.get(fn_name)
            result = fn(**fn_args) if fn else f"Error: unknown tool '{fn_name}'"

            print(f"  [tool result] {result}")
            messages.append({"role": "tool", "content": str(result)})

    return "Stopped: reached max tool-calling steps without a final answer.", messages


# =============================================================================
# 6. CLI
# =============================================================================

def chat_loop() -> None:
    """Interactive REPL with tool access and persistent conversation history."""
    print(f"Connected to {OLLAMA_HOST} (model: {MODEL})")
    print("Tools enabled: open_url, join_teams, read_teams_chat, send_teams_message, ...")
    print("Type your message. Use 'exit' or Ctrl-C to quit.\n")

    messages: list = []
    try:
        while True:
            prompt = input("you > ").strip()
            if not prompt:
                continue
            if prompt.lower() in {"exit", "quit"}:
                break
            reply, messages = agent(prompt, messages)
            print(f"\nagent > {reply}\n")
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")


def main() -> None:
    args = sys.argv[1:]

    # One-off setup: disable the "Open Microsoft Teams?" native popup.
    if args and args[0] == "--setup-teams":
        print(setup_teams_no_prompt())
        return

    # Autonomous mode: watch the Teams chat and reply until someone says 'bye'.
    if args and args[0] == "--converse":
        rest = args[1:]
        if rest and rest[0].startswith(("http://", "https://")):
            print(join_teams(rest[0]))
            time.sleep(5)  # let the web client load before watching
        try:
            converse_teams()
        except requests.exceptions.ConnectionError:
            print(f"ERROR: Could not reach Ollama at {OLLAMA_HOST}.", file=sys.stderr)
            sys.exit(1)
        return

    use_tools = False
    if args and args[0] == "--tools":
        use_tools = True
        args = args[1:]

    try:
        if use_tools:
            if not args:
                chat_loop()
                return
            prompt = " ".join(args)
            print(f"[model: {MODEL}]  running agent with tools...\n")
            reply, _ = agent(prompt)
            print(reply)
        elif args:
            print(ask(" ".join(args)))
        else:
            chat_loop()
    except requests.exceptions.ConnectionError:
        print(
            f"ERROR: Could not reach Ollama at {OLLAMA_HOST}.\n"
            "  - Check the backend EC2 is running and Ollama is up.\n"
            "  - Set OLLAMA_HOST to the backend IP, e.g. http://172.168.2.x:11434\n"
            "  - Security group must allow port 11434 from where you run this.",
            file=sys.stderr,
        )
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"ERROR: Ollama returned an HTTP error: {e}", file=sys.stderr)
        print(f"Response: {e.response.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
