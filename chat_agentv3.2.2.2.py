"""
chat_agentv3.2.2.py — AI agent that talks to Amazon Bedrock (Converse API) and can
drive a real Chrome browser (via PLAYWRIGHT) to join and participate in Microsoft
Teams meetings. This is the Bedrock port of the chat_agentv3 line (previously
Ollama-backed).

v3.2.2 change vs 3.2.1: in --converse mode, if a Teams meeting tab is ALREADY
open in the attached Chrome, the agent REUSES it (skips join_teams) instead of
opening the link again. A URL is now optional; pass one only to join a meeting
that isn't open yet. Reuse order: (1) an in-meeting tab, else (2) join the URL
if given, else (3) any open Teams tab.

The model runs on Amazon Bedrock; this script sends it prompts via the Converse
API and lets it call local Python "tools":
    get_time, open_url, join_teams, read_teams_chat,
    send_teams_message, leave_meeting, get_patch_status.

--------------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------------
    pip install boto3 requests playwright tzdata
    playwright install chromium

    # tzdata provides the IANA timezone database that get_time() needs for
    # named zones like 'Asia/Singapore'. On Windows, Python's zoneinfo has no
    # system tz database, so without tzdata get_time() falls back to a UTC
    # offset (still correct for Singapore = UTC+8, but named zones won't work).

    # Auth uses a Bedrock API key (bearer token). Generate one in the Bedrock
    # console -> "API keys", then put it in .env (see .env.example):
    set BEDROCK_API_KEY=<your-bedrock-api-key>                     # Windows cmd
    set AWS_REGION=us-east-1
    set BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
    # Model access must be enabled in the Bedrock console for that region.

    # Start Chrome with remote debugging so Playwright attaches to YOUR logged-in
    # session and the meeting persists after the script exits:
    chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\temp\\chrome_debug_profile

    # One-time: stop the native "Open Microsoft Teams?" popup (Chrome CLOSED):
    python chat_agentv3.2.2.py --setup-teams

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
    # Plain chat (one-shot):
    python chat_agentv3.2.2.py "Explain what an AI agent is in one sentence."

    # Interactive chat loop (tool-enabled, remembers context):
    python chat_agentv3.2.2.py

    # One-shot tool call:
    python chat_agentv3.2.2.py --tools "What time is it in Singapore?"

    # Config comes from .env (see .env.example). Example keys:
    #   NLB_CASE, NLB_SR_ACCOUNT, SSH_HOST, MONITOR_SCRIPT, SLACK_WEBHOOK_URL
    python chat_agentv3.2.2.py --tools "what's the patch status?"


    # Autonomous Teams mode. If a Teams meeting tab is already open, REUSE it:
    python chat_agentv3.2.2.py --converse

    # Or pass a link to join a meeting that isn't open yet:
    python chat_agentv3.2.2.py --converse https://teams.microsoft.com/meet/<id>?p=<key>
    

--------------------------------------------------------------------------------
NOTES
--------------------------------------------------------------------------------
- Bedrock's Converse API supports tool use natively. Claude Sonnet 4 is reliable
  at tool calling; the deterministic patch-status shortcut in main() is kept
  regardless so status requests never depend on the model choosing the tool.
- join_teams assesses each screen (launcher -> pre-join -> in-meeting) and uses
  JavaScript clicks so the tab-modal Teams popup can't block them.
- Browser tools run on the machine executing this script.
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


# =============================================================================
# APPLICATION LOGGING
# Make the agent behave like a real application: every run automatically writes
# two log files (no piping needed), while STILL printing to the terminal.
#   <LOG_DIR>/app.log    - everything (info): joins, replies, tool calls, the
#                          RAW monitor output, status decisions.
#   <LOG_DIR>/error.log  - warnings + errors only (SSH failures, LLM down,
#                          crashes) so problems are easy to find.
# Both files rotate (max ~2 MB x 5 backups) so they never grow without bound.
# LOG_DIR defaults to a "logs" folder next to this script; override in .env.
# =============================================================================
import logging  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402

LOG_DIR = os.environ.get(
    "LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
)

# The application logger the whole script writes through.
log = logging.getLogger("teams_agent")


def _setup_logging() -> None:
    """Create the app.log + error.log handlers and route print() through them.

    Called once at startup. After this, every existing print(...) in the code
    is also written (with a timestamp) to app.log, and anything logged at
    WARNING/ERROR also lands in error.log - all automatically.
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception as e:  # noqa: BLE001 - fall back to console-only logging
        print(f"[log] could not create log dir {LOG_DIR}: {e}")

    log.setLevel(logging.INFO)
    if log.handlers:  # already configured (e.g. re-import) -> don't double up
        return

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    # app.log: everything at INFO and above.
    try:
        app_fh = RotatingFileHandler(
            os.path.join(LOG_DIR, "app.log"),
            maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        app_fh.setLevel(logging.INFO)
        app_fh.setFormatter(fmt)
        log.addHandler(app_fh)
    except Exception as e:  # noqa: BLE001
        print(f"[log] could not open app.log: {e}")

    # error.log: only WARNING and above.
    try:
        err_fh = RotatingFileHandler(
            os.path.join(LOG_DIR, "error.log"),
            maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        err_fh.setLevel(logging.WARNING)
        err_fh.setFormatter(fmt)
        log.addHandler(err_fh)
    except Exception as e:  # noqa: BLE001
        print(f"[log] could not open error.log: {e}")

    # Route the built-in print() through the logger so all the existing
    # print(...) lines are captured in app.log without rewriting them, while
    # still showing on the terminal (real stdout).
    import builtins
    _real_print = builtins.print

    def _logging_print(*args, **kwargs):
        _real_print(*args, **kwargs)  # keep the live terminal view
        try:
            sep = kwargs.get("sep", " ")
            msg = sep.join(str(a) for a in args)
            if msg.strip():
                # Lines the code marks with warning/error words go to error.log
                # too; everything else is INFO.
                low = msg.lower()
                if (":warning:" in low or "error" in low or "failed" in low
                        or "crashed" in low or "unreachable" in low):
                    log.warning(msg)
                else:
                    log.info(msg)
        except Exception:  # noqa: BLE001 - logging must never break the app
            pass

    builtins.print = _logging_print
    log.info("=== logging started (dir: %s) ===", LOG_DIR)


# --- NLB patch monitor (run over SSH on demand) ------------------------------
# When someone in the meeting asks for a status update, the agent runs:
#   ssh <SSH_HOST> "<MONITOR_SCRIPT> <NLB_CASE> <NLB_SR_ACCOUNT> --once"
# All sensitive values come from .env / environment (see .env.example).
SSH_HOST = os.environ.get("SSH_HOST", "")
MONITOR_SCRIPT = os.environ.get("MONITOR_SCRIPT", "")
NLB_CASE = os.environ.get("NLB_CASE", "")
NLB_SR_ACCOUNT = os.environ.get("NLB_SR_ACCOUNT", "")

# --- Patch targets (one case per OS) -----------------------------------------
# A patching session usually covers two separate SR cases under the SAME SR
# account: one for the Windows fleet and one for the RHEL fleet. Each target is
# {"label", "os", "case", "account"}. The agent can then report Windows and
# RHEL separately ("what's the RHEL status?") or together ("any updates?").
#
# Config (in .env):
#   NLB_SR_ACCOUNT        shared SR account id for both cases
#   NLB_CASE_WINDOWS      SR/case number for the Windows fleet
#   NLB_CASE_RHEL         SR/case number for the RHEL fleet
#
# Backward compatible: if neither OS case is set, we fall back to a single
# unlabeled target built from the legacy NLB_CASE / NLB_SR_ACCOUNT, so older
# single-case setups behave exactly as before.
NLB_CASE_WINDOWS = os.environ.get("NLB_CASE_WINDOWS", "")
NLB_CASE_RHEL = os.environ.get("NLB_CASE_RHEL", "")


def _build_patch_targets() -> list:
    """Return the list of patch targets from .env.

    Each target: {"label": str, "os": str, "case": str, "account": str}.
    Only targets with an all-digit case are included. Falls back to a single
    legacy target (from NLB_CASE) when no OS-specific case is configured.
    """
    account = NLB_SR_ACCOUNT
    targets = []
    for label, os_key, case in (
        ("Windows", "windows", NLB_CASE_WINDOWS),
        ("RHEL", "rhel", NLB_CASE_RHEL),
    ):
        if case and str(case).isdigit() and account:
            targets.append({"label": label, "os": os_key,
                            "case": str(case), "account": str(account)})

    if not targets and NLB_CASE and str(NLB_CASE).isdigit() and account:
        # Legacy single-case mode: no OS label.
        targets.append({"label": "", "os": "", "case": str(NLB_CASE),
                        "account": str(account)})
    return targets


PATCH_TARGETS = _build_patch_targets()

# Keyword aliases used to route a request to a specific OS target.
_OS_ALIASES = {
    "windows": ("windows", "win", "wintel"),
    "rhel": ("rhel", "linux", "red hat", "redhat", "rh "),
}


def _resolve_targets(selector: str = "") -> list:
    """Pick which patch target(s) a request refers to.

    `selector` may be a chat message or an explicit value: an OS name
    ('windows', 'rhel'/'linux'), a case number, or blank. Blank returns ALL
    configured targets. Returns a list of target dicts (possibly empty).
    """
    sel = (selector or "").strip().lower()
    if not sel:
        return list(PATCH_TARGETS)

    # 1) Exact case number match (digits anywhere in the text).
    import re as _re
    for m in _re.findall(r"\d{6,}", sel):
        for t in PATCH_TARGETS:
            if t["case"] == m:
                return [t]

    # 2) OS keyword match.
    matched = []
    for t in PATCH_TARGETS:
        aliases = _OS_ALIASES.get(t["os"], (t["os"],)) if t["os"] else ()
        if any(a and a in sel for a in aliases):
            matched.append(t)
    if matched:
        return matched

    # 3) No specific match -> all targets (caller decides what to do).
    return list(PATCH_TARGETS)


def _explicit_target(text: str) -> list:
    """Return the target(s) a message EXPLICITLY names (OS keyword or case
    number), or [] if it names none. Unlike _resolve_targets, this does NOT
    fall back to 'all' - so the caller can tell a specific request ('rhel
    status') apart from a general one ('any updates?')."""
    sel = (text or "").strip().lower()
    if not sel:
        return []

    import re as _re
    for m in _re.findall(r"\d{6,}", sel):
        for t in PATCH_TARGETS:
            if t["case"] == m:
                return [t]

    matched = []
    for t in PATCH_TARGETS:
        aliases = _OS_ALIASES.get(t["os"], (t["os"],)) if t["os"] else ()
        if any(a and a in sel for a in aliases):
            matched.append(t)
    return matched

# --- Amazon Bedrock backend --------------------------------------------------
# This build uses Bedrock's Converse API instead of a self-hosted Ollama server.
# Auth uses a Bedrock API KEY (a bearer token you generate in the Bedrock
# console under "API keys"). Put it in .env as BEDROCK_API_KEY. Under the hood
# boto3 reads it from the AWS_BEARER_TOKEN_BEDROCK environment variable, so we
# copy BEDROCK_API_KEY into that var before creating the client.
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)
# Bedrock API key (bearer token). Falls back to AWS_BEARER_TOKEN_BEDROCK if the
# caller already exported that directly.
BEDROCK_API_KEY = (
    os.environ.get("BEDROCK_API_KEY")
    or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
)
# Cap on tokens per model reply (Converse inferenceConfig.maxTokens).
BEDROCK_MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "1024"))
MODEL = BEDROCK_MODEL_ID  # kept for the existing "[model: ...]" log lines

# Lazily-created boto3 Bedrock runtime client (see _bedrock_client()).
_BEDROCK_CLIENT = None

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

# Default timezone for get_time when the caller doesn't specify one.
# Overridable via .env (DEFAULT_TZ). Singapore = Asia/Singapore (UTC+8).
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Asia/Singapore")


def get_time(timezone: str = "") -> str:
    """Return the current date and time in a given timezone.

    Defaults to Singapore (Asia/Singapore, UTC+8) when no timezone is given.
    Accepts either a named IANA zone (e.g. 'Asia/Singapore', 'US/Eastern') or
    a UTC offset (e.g. 'UTC+8', '+08:00', 'UTC-5', '0').
    """
    from datetime import datetime, timezone as _tz, timedelta

    tz_arg = (timezone or "").strip() or DEFAULT_TZ

    # 1) Try a named IANA zone first (Asia/Singapore, US/Eastern, ...).
    try:
        from zoneinfo import ZoneInfo  # stdlib (Python 3.9+)
        tz = ZoneInfo(tz_arg)
        now = datetime.now(tz)
        return now.strftime(f"%Y-%m-%d %H:%M:%S ({tz_arg})")
    except Exception:  # noqa: BLE001 - not a known zone / tzdata missing; try offset
        pass

    # 2) Fall back to parsing a UTC offset like 'UTC+8', '+08:00', '-5', '0'.
    #    If the caller relied on the default named zone (e.g. 'Asia/Singapore')
    #    but tzdata isn't installed so ZoneInfo failed above, we can't parse a
    #    named zone as an offset. Map the default to its offset so the tool
    #    still returns the right time instead of erroring. (Best fix is still:
    #    pip install tzdata.)
    if tz_arg == DEFAULT_TZ and "/" in tz_arg:
        tz_arg = "UTC+8"  # Asia/Singapore == UTC+8 (no DST)
    text = tz_arg.upper().replace("UTC", "").replace("GMT", "").strip()
    if text in ("", "Z"):
        offset_hours = 0.0
    else:
        try:
            sign = 1
            if text[0] in "+-":
                sign = -1 if text[0] == "-" else 1
                text = text[1:]
            if ":" in text:
                h, m = text.split(":", 1)
                offset_hours = int(h) + int(m) / 60.0
            else:
                offset_hours = float(text)
            offset_hours *= sign
        except Exception:  # noqa: BLE001
            return (
                f"Couldn't understand timezone '{timezone}'. Use a named zone "
                "like 'Asia/Singapore' or a UTC offset like 'UTC+8'."
            )

    now = datetime.now(_tz(timedelta(hours=offset_hours)))
    label = f"UTC{'+' if offset_hours >= 0 else '-'}{abs(offset_hours):g}"
    return now.strftime(f"%Y-%m-%d %H:%M:%S ({label})")


def _run_monitor_once(case: str, sr_account: str) -> str:
    """Run the NLB patch monitor over SSH for ONE case and return its summary.

    Runs: ssh <SSH_HOST> "<MONITOR_SCRIPT> <case> <sr_account> --once"
    Returns the cycle summary, or an 'ERROR: ...' marker on failure/timeout.
    """
    if not (case and str(case).isdigit()):
        return "ERROR: invalid or missing case number."
    if not (sr_account and str(sr_account).isdigit()):
        return "ERROR: invalid or missing SR account."

    remote_cmd = f"{MONITOR_SCRIPT} {case} {sr_account} --once"
    log.info("[monitor] running (case %s): ssh %s %r", case, SSH_HOST, remote_cmd)
    try:
        proc = subprocess.run(
            ["ssh", SSH_HOST, remote_cmd],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",  # monitor output has emojis
        )
    except FileNotFoundError:
        log.error("[monitor] ssh not found on PATH (case %s)", case)
        return "ERROR: ssh not found on PATH. Install/enable OpenSSH client."
    except subprocess.TimeoutExpired:
        log.error("[monitor] timed out after 300s (case %s)", case)
        return "ERROR: Patch monitor timed out (no result within 5 minutes)."

    out = (proc.stdout or "") + (proc.stderr or "")
    # Log the FULL raw monitor output. This is the single most useful thing for
    # diagnosing status-parsing bugs: it shows exactly what text the agent then
    # parses into "N remaining / all patched".
    log.info("[monitor] case %s exit=%s raw output:\n%s",
             case, proc.returncode, out.rstrip())

    if not out.strip():
        log.error("[monitor] no output (case %s, exit %s)", case, proc.returncode)
        return f"ERROR: No output from monitor (exit {proc.returncode})."
    if proc.returncode != 0:
        tail = "\n".join(out.splitlines()[-15:])
        log.error("[monitor] non-zero exit %s (case %s)", proc.returncode, case)
        return f"ERROR: Monitor exited {proc.returncode}.\n{tail}"

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


def get_patch_status(target: str = "", case: str = "", sr_account: str = "") -> str:
    """Get NLB patching status for one OS target or all of them.

    Use this when someone asks for a patching update, e.g. "any updates?",
    "how many are left?", "what's the RHEL status?", "how's Windows doing?".

    `target` selects which fleet: 'windows', 'rhel'/'linux', a case number, or
    blank for ALL configured targets. `case`/`sr_account` are legacy overrides
    (still honoured if a bare case number is passed).

    Returns a labeled summary. With multiple targets the reply has one line per
    OS, e.g.:
        Windows: 3 instances remaining to be patched.
        RHEL: all instances are patched - nothing remaining.
    """
    # Legacy path: an explicit numeric case (small models sometimes pass the
    # env-var NAME instead of a value, so only accept all-digit).
    if case and str(case).isdigit():
        acct = sr_account if (sr_account and str(sr_account).isdigit()) else NLB_SR_ACCOUNT
        return _format_status_reply(_run_monitor_once(str(case), acct))

    if not PATCH_TARGETS:
        return ("ERROR: No patch targets configured. Set NLB_CASE_WINDOWS / "
                "NLB_CASE_RHEL (and NLB_SR_ACCOUNT), or the legacy NLB_CASE.")

    targets = _resolve_targets(target)
    if not targets:
        return "ERROR: No matching patch target for that request."

    # Single unlabeled target (legacy mode) -> return the bare formatted line.
    if len(targets) == 1 and not targets[0]["label"]:
        t = targets[0]
        return _format_status_reply(_run_monitor_once(t["case"], t["account"]))

    results = _collect_target_status(targets)
    # One target -> bare label line; multiple -> one labeled line each.
    if len(results) == 1:
        r = results[0]
        return f"{r['target']['label']}: {r['line']}"
    return "\n".join(f"{r['target']['label']}: {r['line']}" for r in results)


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


# Selectors that indicate the chat compose box (i.e. the chat rail is open).
# Teams builds vary: older ones expose div[data-tid='ckeditor']; newer ones
# render a contenteditable textbox and/or a "Type a message" input.
_SEL_CHAT_COMPOSE = [
    "div[data-tid='ckeditor']",
    "div[data-tid='ckeditor'] [contenteditable='true']",
    "div[role='textbox'][contenteditable='true']",
    "[data-tid='newMessageCommandBar']",
    "[aria-label='Type a message']",
    "[placeholder='Type a message']",
]


def _chat_pane_open(page) -> bool:
    """True if the chat compose box is visible (chat rail is open)."""
    return _has(page, _SEL_CHAT_COMPOSE)


def _ensure_chat_open(page, wait_seconds: int = 25) -> bool:
    """Make sure the chat rail is open, retrying while the meeting UI loads.

    Re-clicks the Chat button periodically (not just once) because on a fresh
    join the toolbar may not be interactive yet, so the first click is a no-op.
    Detects success via _SEL_CHAT_COMPOSE, which covers old (ckeditor) and new
    (contenteditable textbox) Teams builds.
    """
    deadline = time.time() + wait_seconds
    last_click = 0.0
    while time.time() < deadline:
        if _chat_pane_open(page):
            return True
        # Re-click at most every 3s so a not-yet-ready toolbar gets retried.
        if time.time() - last_click >= 3:
            toggle = _first_visible(page, _SEL_CHAT_TOGGLE)
            if toggle is None:
                # Toolbar may have auto-hidden; nudge it and try any present
                # (not necessarily visible) chat button via JS.
                try:
                    page.mouse.move(500, 700)
                except Exception:
                    pass
                for sel in _SEL_CHAT_TOGGLE:
                    try:
                        loc = page.locator(sel)
                        if loc.count() > 0:
                            _js_click(page, loc.first)
                            print(f"  [chat] clicked chat button via JS ({sel})")
                            break
                    except Exception:
                        continue
            else:
                try:
                    _js_click(page, toggle)
                    print("  [chat] clicked chat button to open the pane")
                except Exception:
                    pass
            last_click = time.time()
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

    If the status is an error/timeout marker (prefixed 'ERROR:'), report the
    failure plainly instead of misparsing it into a fake "0 pending".
    """
    if _is_status_error(status):
        return ("Couldn't get the patching status right now (the check failed "
                "or timed out). I'll retry.")

    import re as _re

    low_all = status.lower()

    # 1) AUTHORITATIVE completion sentence. The monitor prints this only once
    #    it has reconciled compliance for the whole fleet, so it outranks the
    #    stale "N instance(s) remaining" startup echo below.
    if ("all instances patched" in low_all
            or "monitor complete" in low_all
            or "all instances are patched" in low_all):
        return "Status: all instances are patched - nothing remaining."

    # 2) AUTHORITATIVE totals line, e.g.
    #    "Total: 38 | Patched: 38 | Pending: 0 | Failed: 0".
    #    This is the real cycle result; prefer it over the startup echo.
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

    if pending is not None or (total is not None and patched is not None):
        if pending is None and total is not None and patched is not None:
            pending = total - patched
        if pending == 0 and not failed:
            return "Status: all instances are patched - nothing remaining."
        noun = "instance" if pending == 1 else "instances"
        msg = f"Status: {pending} {noun} remaining to be patched."
        if failed:
            msg = msg[:-1] + f" ({failed} failed)."
        return msg

    # 3) "All compliant / input is empty" -> nothing left to patch.
    if "compliant" in low_all or "is empty" in low_all:
        return "Status: all instances are patched - nothing remaining."

    # 4) LAST RESORT: the "N instance(s) remaining" line. This is a startup
    #    echo printed BEFORE compliance is reconciled, so it can be stale (it
    #    still shows the pre-patch count even when everything is done). Only
    #    trust it when none of the authoritative signals above were present.
    m_remaining = _re.search(r"(\d+)\s+instance\(?s?\)?\s+remaining",
                             status, _re.IGNORECASE)
    if m_remaining is not None:
        n = int(m_remaining.group(1))
        if n == 0:
            return "Status: all instances are patched - nothing remaining."
        noun = "instance" if n == 1 else "instances"
        return f"Status: {n} {noun} remaining to be patched."

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


def _is_status_error(status: str) -> bool:
    """True when get_patch_status returned a failure marker rather than a real
    status. All failure paths in get_patch_status are prefixed with 'ERROR:'."""
    return (status or "").lstrip().upper().startswith("ERROR:")


def _is_all_patched(status: str) -> bool:
    """True when the monitor output shows nothing left to patch.

    Used by the proactive 30-min broadcast to know when to announce completion
    and stop. Only returns True on a REAL status: an explicit 'Pending: 0'
    (with no failures) or the 'all compliant / list is empty' case. Never
    treats an error/timeout result as 'all patched'.
    """
    import re as _re
    # An error/timeout is NOT completion -> don't stop the timer on it.
    if _is_status_error(status):
        return False

    low = status.lower()

    # 1) Explicit completion sentence (printed after compliance is reconciled).
    if ("all instances patched" in low or "monitor complete" in low
            or "all instances are patched" in low):
        return True
    if "compliant" in low or "is empty" in low:
        return True

    # 2) Authoritative totals line. Checked BEFORE the "N remaining" echo,
    #    which is a stale startup line printed before reconciliation.
    m_pending = _re.search(r"pending\s*[:=]\s*(\d+)", status, _re.IGNORECASE)
    m_total = _re.search(r"total\s*[:=]\s*(\d+)", status, _re.IGNORECASE)
    m_failed = _re.search(r"failed\s*[:=]\s*(\d+)", status, _re.IGNORECASE)
    if m_pending is not None and m_total is not None:
        pending = int(m_pending.group(1))
        failed = int(m_failed.group(1)) if m_failed else 0
        return pending == 0 and failed == 0

    # 3) LAST RESORT: "0 instance(s) remaining" (only if no totals line above).
    m_rem = _re.search(r"(\d+)\s+instance\(?s?\)?\s+remaining", status,
                       _re.IGNORECASE)
    if m_rem is not None:
        return int(m_rem.group(1)) == 0

    return False


def _strip_status_prefix(line: str) -> str:
    """Drop the generic 'Status:' / 'Patching update:' lead so an OS label
    reads cleanly (e.g. 'Windows: 3 instances remaining')."""
    for pfx in ("Patching update:", "Status:"):
        if line.startswith(pfx):
            return line[len(pfx):].strip()
    return line


def _collect_target_status(targets: list) -> list:
    """Run the monitor once per target and return structured results.

    Each item: {"target": <target dict>, "raw": str, "line": str,
                "done": bool, "error": bool}. This is the single source of
    truth the reply composers build on, so the SSH call and parsing live in
    one place.
    """
    results = []
    for t in targets:
        raw = _run_monitor_once(t["case"], t["account"])
        error = _is_status_error(raw)
        results.append({
            "target": t,
            "raw": raw,
            "line": _strip_status_prefix(_format_status_reply(raw)),
            "done": (not error) and _is_all_patched(raw),
            "error": error,
        })
    return results


def get_patch_status_all():
    """Run every configured target once; return (labeled_reply, all_done, any_error).

    Used by the proactive broadcast so it can (a) post one combined message,
    (b) know when EVERY target is fully patched (to announce completion and
    stop), and (c) skip posting when a check errored/timed out.
    """
    if not PATCH_TARGETS:
        return ("ERROR: No patch targets configured.", False, True)

    results = _collect_target_status(PATCH_TARGETS)
    multi = len([t for t in PATCH_TARGETS if t["label"]]) > 1

    labeled = []
    for r in results:
        if r["error"]:
            continue
        t = r["target"]
        labeled.append(f"{(t['label'] or t['case'])}: {r['line']}" if multi
                       else r["line"])

    all_done = all(r["done"] for r in results) and not any(r["error"] for r in results)
    any_error = any(r["error"] for r in results)
    return ("\n".join(labeled), all_done, any_error)


# Tracks OS labels the agent has already acknowledged as complete in the chat,
# so a general "how's the update?" only ASKS "did you mean <the remaining
# one>?" the FIRST time a fleet finishes — then it just reports the remaining
# fleet on repeats instead of nagging. Reset per process (per meeting).
_completed_acknowledged = set()


def general_status_reply() -> str:
    """Compose the reply to a GENERAL status question (no OS named).

    - Both (all) fleets still in progress -> report each, labeled.
    - Exactly one fleet outstanding, the other(s) done:
        * first time after completion -> report the outstanding fleet and ASK
          the human to confirm that's what they meant.
        * afterwards -> just report the outstanding fleet (no repeated asking).
    - All fleets done -> say so plainly.
    - Errors -> report the failure plainly (caller decides whether to post).
    """
    if not PATCH_TARGETS:
        return ("ERROR: No patch targets configured. Set NLB_CASE_WINDOWS / "
                "NLB_CASE_RHEL (and NLB_SR_ACCOUNT), or the legacy NLB_CASE.")

    # Legacy single unlabeled target -> just the bare line.
    labeled_targets = [t for t in PATCH_TARGETS if t["label"]]
    if not labeled_targets:
        t = PATCH_TARGETS[0]
        return _format_status_reply(_run_monitor_once(t["case"], t["account"]))

    results = _collect_target_status(PATCH_TARGETS)

    if all(r["error"] for r in results):
        return ("Couldn't get the patching status right now (the checks failed "
                "or timed out). I'll retry.")

    ok = [r for r in results if not r["error"]]
    outstanding = [r for r in ok if not r["done"]]
    done = [r for r in ok if r["done"]]

    # All done.
    if not outstanding:
        names = _join_labels([r["target"]["label"] for r in done])
        return f"{names} are fully patched - nothing left."

    # More than one still outstanding (or nothing done yet) -> report each.
    if len(outstanding) > 1 or not done:
        lines = [f"{r['target']['label']}: {r['line']}" for r in ok]
        return "\n".join(lines)

    # Exactly one outstanding, at least one done -> the ambiguous case.
    out = outstanding[0]
    out_label = out["target"]["label"]
    done_names = _join_labels([r["target"]["label"] for r in done])

    # Ask the confirming question only the FIRST time we hit this
    # one-done/one-outstanding situation. Key the "already asked" flag on the
    # outstanding fleet so repeats just report it instead of nagging.
    ask_key = f"asked:{out_label}"
    already_asked = ask_key in _completed_acknowledged

    if not already_asked:
        _completed_acknowledged.add(ask_key)
        return (f"{done_names} is already complete. {out_label} still has "
                f"{_remaining_phrase(out)}. Did you mean the {out_label} update?")
    # Subsequent general asks: just report the outstanding fleet.
    return f"{out_label}: {out['line']}"


def _remaining_phrase(result: dict) -> str:
    """A short 'N instance(s) remaining' phrase for the outstanding fleet."""
    import re as _re
    m = _re.search(r"(\d+)\s+instance", result["line"], _re.IGNORECASE)
    if m:
        n = int(m.group(1))
        return f"{n} instance remaining" if n == 1 else f"{n} instances remaining"
    return "instances remaining"


def _join_labels(labels: list) -> str:
    """'Windows' / 'Windows and RHEL' / 'A, B and C'."""
    labels = [l for l in labels if l]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"


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


# Phrases that signal the model punted instead of answering -> worth a retry.
_UNSURE_MARKERS = (
    "i don't have", "i do not have", "haven't run", "have not run",
    "i'm not sure", "i am not sure", "please try again", "try again",
    "i don't know", "i do not know", "no specific information",
    "unable to", "cannot provide", "can't provide", "not updated yet",
)


def _is_unsure(reply: str) -> bool:
    low = (reply or "").lower()
    return (not low.strip()) or any(m in low for m in _UNSURE_MARKERS)


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

        # 1) Prefer a visible Leave button.
        btn = _first_visible(page, _SEL_LEAVE)
        if btn:
            _js_click(page, btn)
            return "Clicked Leave - left the meeting."

        # 2) The control bar auto-hides after a few idle seconds, so the Leave
        #    button is in the DOM but reports not-visible and _first_visible
        #    skips it. Nudge the toolbar to reappear, then retry visible.
        try:
            page.mouse.move(400, 300)
            page.mouse.move(500, 700)  # bottom area where the control bar lives
        except Exception:
            pass
        time.sleep(0.5)
        btn = _first_visible(page, _SEL_LEAVE)
        if btn:
            _js_click(page, btn)
            return "Clicked Leave - left the meeting."

        # 3) Still hidden -> click the present-but-not-visible button via JS.
        #    _js_click works on elements that aren't visually rendered.
        for sel in _SEL_LEAVE:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    _js_click(page, loc.first)
                    return "Clicked Leave (via JS on hidden control bar) - left the meeting."
            except Exception:
                continue

        # 4) Last resort: keyboard shortcut (Teams web may ignore this).
        try:
            page.keyboard.press("Control+Shift+H")
            return "Sent Leave shortcut (Ctrl+Shift+H) - left the meeting."
        except Exception:
            return "Leave button not found and Ctrl+Shift+H failed."
    except Exception as e:  # noqa: BLE001
        return f"Failed to leave meeting: {e}"


# Closing message the agent posts when someone says 'bye'/'goodbye'.
# Override in .env with FAREWELL_MESSAGE=...
FAREWELL_MESSAGE = os.environ.get(
    "FAREWELL_MESSAGE",
    "Bye! Talk to you later.",
)


# Opening message the agent posts when it joins the meeting chat.
def _targets_phrase() -> str:
    """Human phrase naming the configured targets, e.g.
    'Windows (case 178761970500494) and RHEL (case 178762556000269)'.
    Empty string when there's nothing labeled (legacy single-case mode)."""
    labeled = [t for t in PATCH_TARGETS if t["label"]]
    if not labeled:
        return ""
    parts = [f"{t['label']} (case {t['case']})" for t in labeled]
    if len(parts) == 1:
        return parts[0]
    return " and ".join([", ".join(parts[:-1]), parts[-1]]) if len(parts) > 2 \
        else " and ".join(parts)


def _default_intro() -> str:
    phrase = _targets_phrase()
    if phrase:
        return (
            "Hello, I'm David's AI assistant sitting in for David. "
            f"Today we're patching {phrase}. "
            "I'll post a status update for each every 30 mins until all "
            "instances are patched. "
            "Feel free to chat with me here - say 'bye' when you're done."
        )
    return (
        "Hello, I'm David's AI assistant sitting in for David. "
        "I'll update the status of those instances every 30 mins until all "
        "instances are patched. "
        "Feel free to chat with me here - say 'bye' when you're done."
    )


# Explicit INTRO_MESSAGE in .env still wins; otherwise build it from targets.
INTRO_MESSAGE = os.environ.get("INTRO_MESSAGE", "") or _default_intro()


def _intro_already_posted(page, lookback: int = 40) -> bool:
    """True if the intro text is already present earlier in this chat.

    When reusing an already-open meeting (v3.2.2), we don't want to spam the
    intro again. We scan recent chat for a message whose text matches the intro
    signature, so a restart mid-meeting stays quiet.

    NOTE: we deliberately do NOT require is_self. The agent posts under the
    logged-in user's identity (e.g. "David AKC"), not AGENT_NAME, so is_self is
    unreliable for our own messages. The intro text itself is a distinctive
    fingerprint - if it appears in the recent chat at all, it's ours.
    """
    try:
        msgs = _parse_chat_messages(page, limit=lookback)
    except Exception:
        return False

    # A stable signature from the start of the intro (first ~40 chars),
    # normalised so spacing/case differences don't cause a miss.
    def _norm(s: str) -> str:
        return " ".join((s or "").split()).lower()

    intro_sig = _norm(INTRO_MESSAGE)[:40]
    if not intro_sig:
        return False

    for m in msgs:
        if m.get("kind") != "message":
            continue
        if _norm(m.get("text", "")).startswith(intro_sig):
            return True
    return False


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
    "There may be separate Windows and RHEL fleets: if someone asks about a "
    "specific OS (e.g. 'RHEL status', 'how's Windows?'), pass that OS as the "
    "tool's `target`; if they ask generally, leave `target` blank to report "
    "all fleets. "
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
        if _intro_already_posted(page):
            print("[converse] intro already posted earlier - skipping it.")
        else:
            send_teams_message(INTRO_MESSAGE)
            print(f"me   > {INTRO_MESSAGE}")

    print(f"[converse] watching Teams chat every {poll_seconds}s "
          f"(agent name: {AGENT_NAME!r}). "
          "Say 'bye' in the chat to stop. Ctrl-C to abort.\n")

    messages: list = [{"role": "system", "content": CONVERSE_SYSTEM_PROMPT}]

    # Seed last_processed with whatever is ALREADY the latest incoming message
    # so we don't reply to old, pre-startup chat. This matters when reusing a
    # meeting after a restart/reboot: the window still has past messages, but
    # only genuinely NEW ones (arriving after we start watching) should get a
    # reply. Without this, last_processed=None makes the first poll answer the
    # last thing said before we started.
    last_processed = None
    try:
        _seed = _latest_incoming_message(page)
        if _seed:
            last_processed = f"{_seed['author'] or 'Someone'}|{_seed['text']}"
            print("[converse] baseline set to current last message - will only "
                  "reply to NEW messages from here on.")
    except Exception:
        pass

    chat_fail_count = 0

    # Periodically confirm the LLM is still reachable during the meeting so an
    # outage alerts Slack instead of silently failing every reply.
    llm_check_interval = 120  # seconds
    next_llm_check = time.time() + llm_check_interval
    llm_down_notified = False

    # Proactive patch-status broadcast: every STATUS_BROADCAST_INTERVAL seconds
    # (default 30 min) post the current pending count to the chat without being
    # asked. The 3s poll above is only for *reading/replying*; this is a
    # separate, slower timer for *pushing* updates. Once everything is patched
    # we announce completion once and stop broadcasting.
    status_broadcast_interval = int(
        os.environ.get("STATUS_BROADCAST_INTERVAL", str(30 * 60))
    )
    # First proactive update goes out one full interval after the intro (the
    # intro already told everyone the cadence).
    next_status_broadcast = time.time() + status_broadcast_interval
    all_patched_announced = False

    try:
        while True:
            # --- Proactive 30-min status broadcast (independent of chat poll) --
            if (not all_patched_announced
                    and time.time() >= next_status_broadcast):
                next_status_broadcast = time.time() + status_broadcast_interval
                print("  [converse] scheduled 30-min update -> get_patch_status_all")
                reply, all_done, any_error = get_patch_status_all()
                # A timeout/SSH failure on ANY target is NOT a clean status.
                # If every target failed, skip this cycle entirely. If only
                # some failed, still post what we have but alert Slack.
                if any_error:
                    print(f"  [converse] a status check failed: {reply}")
                    notify_slack(f":warning: A patch status check failed:\n{reply}")
                    if not reply.strip() or _is_status_error(reply):
                        continue
                send_teams_message(reply)
                messages.append({"role": "assistant", "content": reply})
                print(f"me   > {reply}")
                # Only announce completion + stop when EVERY target is done.
                if all_done and not any_error:
                    done = "All instances (Windows and RHEL) are now patched. I'll stop the updates here."
                    send_teams_message(done)
                    messages.append({"role": "assistant", "content": done})
                    print(f"me   > {done}")
                    all_patched_announced = True

            if time.time() >= next_llm_check:
                next_llm_check = time.time() + llm_check_interval
                ok, detail = check_llm_ready()
                if not ok and not llm_down_notified:
                    print(f"[converse] LLM unreachable: {detail}")
                    notify_slack(f":warning: Teams AI agent lost the LLM. {detail}")
                    llm_down_notified = True
                elif ok and llm_down_notified:
                    print("[converse] LLM reachable again.")
                    notify_slack(":white_check_mark: Teams AI agent LLM is back.")
                    llm_down_notified = False

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
                farewell = FAREWELL_MESSAGE
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
                explicit = _explicit_target(latest)
                if explicit:
                    # Named a specific OS/case -> answer just that fleet.
                    print(f"  [converse] status request (explicit: "
                          f"{[t['label'] or t['case'] for t in explicit]})")
                    reply = get_patch_status(target=latest)
                else:
                    # General ask -> report all, or (if one fleet is done and
                    # one outstanding) report the outstanding one and confirm.
                    print("  [converse] status request (general)")
                    reply = general_status_reply()
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

            # Try up to 3 times: small models sometimes punt ("I haven't run
            # it yet") or return nothing. Retry before giving up.
            reply = ""
            for attempt in range(3):
                reply, messages = agent(user_turn, messages)
                reply = _strip_name_prefix((reply or "").strip(), [author])
                if not _is_unsure(reply):
                    break
                print(f"  [converse] unsure reply, retrying ({attempt + 1}/3)")

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
    "get_time": get_time,
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
            "name": "get_time",
            "description": (
                "Get the current date and time. Use when someone asks 'what "
                "time is it?', 'what's the date?', etc. Defaults to Singapore "
                "time (Asia/Singapore, UTC+8) if no timezone is given."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {"type": "string",
                                 "description": "Optional. Named zone like 'Asia/Singapore' or UTC offset like 'UTC+8'. Blank = Singapore."}
                },
                "required": [],
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
                "'any updates?', 'how many are left?', 'what's the RHEL status?'. "
                "There may be separate Windows and RHEL fleets. Returns a "
                "labeled summary (remaining/pending per OS)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string",
                               "description": "Which fleet: 'windows', 'rhel' (or 'linux'), or a case number. Leave blank to report ALL fleets."},
                },
                "required": [],
            },
        },
    },
]


# =============================================================================
# 3. LOW-LEVEL CALL — one round trip to Amazon Bedrock's Converse API.
# =============================================================================
#
# The rest of this file speaks "Ollama shape":
#   - assistant message: {"role","content", "tool_calls":[{"id","function":
#       {"name","arguments":dict}}] or None}
#   - tool result:        {"role":"tool","content":str}
#   - system prompt:      {"role":"system","content":str}
#   - tool schema:        OpenAI-style {"type":"function","function":{...}}
#
# Bedrock Converse uses a different shape, so _chat() translates in both
# directions. This keeps agent(), ask(), and converse_teams() unchanged.

def _bedrock_client():
    """Return a cached boto3 bedrock-runtime client authenticated by API key.

    The Bedrock API key is a bearer token. boto3 (>= 1.39) reads it from the
    AWS_BEARER_TOKEN_BEDROCK environment variable, so we export BEDROCK_API_KEY
    into that var here before creating the client. No IAM access keys or
    ~/.aws profile are required.
    """
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        if not BEDROCK_API_KEY:
            raise RuntimeError(
                "No Bedrock API key. Set BEDROCK_API_KEY in .env (generate one "
                "in the Bedrock console under 'API keys')."
            )
        # boto3's bearer-token auth is picked up from this env var.
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = BEDROCK_API_KEY
        import boto3  # lazy so the module imports without boto3 for --setup-teams
        _BEDROCK_CLIENT = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _BEDROCK_CLIENT


def _tools_to_bedrock(tools: list) -> dict:
    """Convert OpenAI-style TOOLS_SCHEMA -> Bedrock toolConfig."""
    specs = []
    for t in tools:
        fn = t["function"]
        specs.append({
            "toolSpec": {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "inputSchema": {"json": fn.get("parameters", {"type": "object",
                                                              "properties": {}})},
            }
        })
    return {"tools": specs}


def _messages_to_bedrock(messages: list) -> tuple[list, list]:
    """Translate Ollama-shaped messages -> (bedrock_messages, system_blocks).

    - "system"    -> collected into top-level system=[{"text":...}]
    - "user"      -> {"role":"user","content":[{"text":...}]}
    - "assistant" -> {"role":"assistant","content":[{"text":...} and/or
                       {"toolUse":{"toolUseId","name","input":arguments}}]}
    - "tool"      -> {"role":"user","content":[{"toolResult":{"toolUseId",
                       "content":[{"text":result}]}}]}

    Consecutive tool results are merged into a single user turn so Bedrock's
    strict user/assistant alternation is preserved.
    """
    system_blocks: list = []
    bedrock: list = []
    # Track toolUse ids emitted by the previous assistant turn so tool results
    # can be paired back to them positionally.
    pending_tool_ids: list = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "system":
            if content:
                system_blocks.append({"text": content})
            continue

        if role == "user":
            bedrock.append({"role": "user", "content": [{"text": content}]})
            continue

        if role == "assistant":
            blocks = []
            if content:
                blocks.append({"text": content})
            pending_tool_ids = []
            for call in (msg.get("tool_calls") or []):
                tuid = call.get("id") or f"tool_{len(pending_tool_ids)}"
                pending_tool_ids.append(tuid)
                blocks.append({
                    "toolUse": {
                        "toolUseId": tuid,
                        "name": call["function"]["name"],
                        "input": call["function"].get("arguments", {}) or {},
                    }
                })
            if not blocks:
                blocks.append({"text": ""})
            bedrock.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            tuid = pending_tool_ids.pop(0) if pending_tool_ids else "tool_0"
            result_block = {
                "toolResult": {
                    "toolUseId": tuid,
                    "content": [{"text": str(content)}],
                }
            }
            # Merge into the previous user turn if it already holds tool results.
            if (bedrock and bedrock[-1]["role"] == "user"
                    and all("toolResult" in b for b in bedrock[-1]["content"])):
                bedrock[-1]["content"].append(result_block)
            else:
                bedrock.append({"role": "user", "content": [result_block]})
            continue

    return bedrock, system_blocks


def _bedrock_to_message(output_message: dict) -> dict:
    """Translate a Bedrock output.message -> Ollama-shaped assistant message."""
    text_parts = []
    tool_calls = []
    for block in output_message.get("content", []):
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append({
                "id": tu.get("toolUseId"),
                "function": {
                    "name": tu.get("name"),
                    "arguments": tu.get("input", {}) or {},
                },
            })
    return {
        "role": "assistant",
        "content": "".join(text_parts),
        "tool_calls": tool_calls or None,
    }


def _chat(messages: list, tools: list | None = None) -> dict:
    """Send messages (and optionally tool schemas) to Bedrock; return the message.

    Returns the same Ollama-shaped dict the rest of the code expects:
      {"role":"assistant","content":str,"tool_calls":[...] or None}
    """
    bedrock_messages, system_blocks = _messages_to_bedrock(messages)

    kwargs = {
        "modelId": BEDROCK_MODEL_ID,
        "messages": bedrock_messages,
        "inferenceConfig": {"maxTokens": BEDROCK_MAX_TOKENS},
    }
    if system_blocks:
        kwargs["system"] = system_blocks
    if tools:
        kwargs["toolConfig"] = _tools_to_bedrock(tools)

    resp = _bedrock_client().converse(**kwargs)
    return _bedrock_to_message(resp["output"]["message"])


def check_llm_ready() -> tuple[bool, str]:
    """Verify Bedrock is reachable and the configured model can be invoked.

    Call this at startup, before any side effects (joining a meeting, posting
    an intro message), so we fail fast with a clear message instead of crashing
    in front of meeting participants. Sends a tiny converse ping.
    """
    if not BEDROCK_API_KEY:
        return False, (
            "No Bedrock API key. Set BEDROCK_API_KEY in .env (generate one in "
            "the Bedrock console under 'API keys')."
        )
    try:
        client = _bedrock_client()
    except Exception as e:  # noqa: BLE001 - boto3 missing or key/env error
        return False, (
            f"Could not create Bedrock client (region {AWS_REGION}). "
            f"Is boto3 installed and BEDROCK_API_KEY valid? {e}"
        )

    try:
        client.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
        return True, f"Bedrock ready in {AWS_REGION} (model: {BEDROCK_MODEL_ID})."
    except Exception as e:  # noqa: BLE001
        # botocore raises ClientError for access/validation issues; keep the
        # message generic so we don't need to import botocore just for typing.
        name = type(e).__name__
        return False, (
            f"Bedrock readiness check failed ({name}) for model "
            f"{BEDROCK_MODEL_ID} in {AWS_REGION}: {e}"
        )


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
    print(f"Connected to Bedrock in {AWS_REGION} (model: {BEDROCK_MODEL_ID})")
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
    _setup_logging()  # start app.log + error.log before anything else
    args = sys.argv[1:]

    # One-off setup: disable the "Open Microsoft Teams?" native popup.
    if args and args[0] == "--setup-teams":
        print(setup_teams_no_prompt())
        return

    # Autonomous mode: watch the Teams chat and reply until someone says 'bye'.
    if args and args[0] == "--converse":
        # Confirm the LLM is reachable BEFORE joining/announcing ourselves.
        ok, detail = check_llm_ready()
        if not ok:
            print(f"ERROR: {detail}", file=sys.stderr)
            notify_slack(f":warning: Teams AI agent not started. {detail}")
            sys.exit(1)
        print(f"[llm] {detail}")
        rest = args[1:]
        url = rest[0] if rest and rest[0].startswith(("http://", "https://")) else None

        # v3.2.2: prefer an ALREADY-OPEN Teams meeting tab. Only join a link
        # when we're not already in a meeting.
        existing = _teams_page()
        already_in_meeting = (
            existing is not None
            and _assess_teams_screen(existing) == "in_meeting"
        )

        if already_in_meeting:
            print("[converse] reusing the Teams meeting already open in Chrome "
                  "(skipping join).")
        elif url:
            if existing is not None:
                print("[converse] a Teams tab is open but not in a meeting; "
                      "joining the provided link.")
            print(join_teams(url))
            time.sleep(5)  # let the web client load before watching
        elif existing is not None:
            # Teams tab open (launcher/prejoin/unknown) but no URL to join with.
            print("[converse] found an open Teams tab (not in-meeting) and no "
                  "link was given; will try to use it as-is.")
        else:
            print("ERROR: No Teams meeting is open and no link was provided.\n"
                  "  Open the meeting in the attached Chrome, or pass a link:\n"
                  "  python chat_agentv3.2.2.py --converse <teams-meeting-url>",
                  file=sys.stderr)
            sys.exit(1)

        try:
            converse_teams()
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: Bedrock call failed: {e}", file=sys.stderr)
            sys.exit(1)
        return

    use_tools = False
    if args and args[0] == "--tools":
        use_tools = True
        args = args[1:]

    # Fail fast if the LLM backend isn't reachable/ready.
    ok, detail = check_llm_ready()
    if not ok:
        print(f"ERROR: {detail}", file=sys.stderr)
        sys.exit(1)
    print(f"[llm] {detail}")

    try:
        if use_tools:
            if not args:
                chat_loop()
                return
            prompt = " ".join(args)
            # Deterministic shortcut: if it's a patch-status question, run the
            # tool directly instead of relying on the small model to call it.
            if _is_status_request(prompt):
                print("[tools] status request -> running get_patch_status directly")
                if _explicit_target(prompt):
                    print(get_patch_status(target=prompt))
                else:
                    print(general_status_reply())
                return
            print(f"[model: {MODEL}]  running agent with tools...\n")
            reply, _ = agent(prompt)
            print(reply)
        elif args:
            print(ask(" ".join(args)))
        else:
            chat_loop()
    except Exception as e:  # noqa: BLE001
        print(
            f"ERROR: Bedrock call failed for model {BEDROCK_MODEL_ID} in "
            f"{AWS_REGION}: {e}\n"
            "  - Check BEDROCK_API_KEY in .env is set and not expired.\n"
            "  - Confirm the API key's permissions allow this model.\n"
            "  - Confirm model access is enabled in the Bedrock console for "
            f"{AWS_REGION}.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
