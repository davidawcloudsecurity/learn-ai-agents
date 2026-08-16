"""
chat_agentv2.1.py — AI agent that talks to an Ollama backend and can drive a real
Chrome browser (via Selenium) to join and participate in Microsoft Teams meetings.

The model runs on the Ollama backend (see infra_terraform/main.tf, port 11434);
this script sends it prompts over HTTP and lets it call local Python "tools":
    get_weather, calculator, open_url, join_teams, read_teams_chat,
    send_teams_message.

--------------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------------
    pip install requests selenium

    # Point at your Ollama backend (defaults to the ALB in this file):
    set OLLAMA_HOST=http://<backend-or-alb>:11434     # Windows cmd
    set OLLAMA_MODEL=qwen2.5:1.5b                      # tool-capable model

    # Start Chrome with remote debugging so Selenium attaches to YOUR logged-in
    # session and the meeting persists after the script exits:
    chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\temp\\chrome_debug_profile

    # One-time: stop the native "Open Microsoft Teams?" popup (Chrome CLOSED):
    python chat_agentv2.1.py --setup-teams

    python chat_agentv2.1.py --converse "https://teams.live.com/dl/launcher/launcher.html?url=%2F_%23%2Fmeet%2F9352978511430%3Fp%3DRczY4jt7qOsOh2xaAn%26anon%3Dtrue&type=meet&deeplinkId=1a074764-62fd-47e9-aa82-9baa4e3a24de&directDl=true&msLaunch=true&enableMobilePage=true&suppressPrompt=true"

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
    # Plain chat (one-shot):
    python chat_agentv2.1.py "Explain what an AI agent is in one sentence."

    # Interactive chat loop (tool-enabled, remembers context):
    python chat_agentv2.1.py

    # One-shot tool call:
    python chat_agentv2.1.py --tools "What is 42 * 17 and the weather in Tokyo?"

    # Autonomous Teams mode: join the meeting, then watch the chat and reply
    # on David's behalf until someone says 'bye' (no timeout):
    python chat_agentv2.1.py --converse https://teams.microsoft.com/meet/<id>?p=<key>

--------------------------------------------------------------------------------
NOTES
--------------------------------------------------------------------------------
- Tool calling needs a model trained for it. smollm:1.7b is NOT reliable; prefer
  qwen2.5:1.5b/3b or llama3.1:8b.
- join_teams assesses each screen (launcher -> pre-join -> in-meeting) and uses
  JavaScript clicks so the tab-modal Teams popup can't block them.
- Browser tools run on the machine executing this script, NOT on the backend.
"""

import os
import sys
import json
import time
import socket

import requests

def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: reads KEY=VALUE lines into os.environ (real env
    vars take precedence). Keeps secrets out of source so .env can be gitignored."""
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
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:  # noqa: BLE001
        print(f"[env] failed to read {env_path}: {e}")


_load_dotenv()

# Where the Ollama server lives (from .env / environment).
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

# The display name this agent posts under in Teams (used to tell our own
# messages apart from other people's). Must match the join display name.
AGENT_NAME = os.environ.get("AGENT_NAME", "David's AI Agent")

# Connect/read timeout in seconds (model generation can be slow on t3.medium).
TIMEOUT = (10, 120)


# =============================================================================
# 1. THE TOOLS — real Python functions the model can ask us to run.
#    The model NEVER runs these. It only emits a request; our code executes them.
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
    # Restrict the namespace so eval can't touch builtins (basic safety).
    allowed = {"__builtins__": {}}
    try:
        result = eval(expression, allowed, {})  # noqa: S307 - sandboxed above
        return str(result)
    except Exception as e:  # noqa: BLE001
        return f"Error evaluating '{expression}': {e}"


# Chrome remote-debugging endpoint. Start Chrome first with:
#   chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\temp\chrome_debug_profile
# so the browser (and your logged-in session) persists across runs. Selenium
# attaches to this via debuggerAddress.
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
    clicks, so Playwright can't press 'Continue on this browser'. Marking the
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

# Reuse a single Selenium Chrome driver across tool calls.
# Selenium is pure Python (no Node driver), so there is no EPIPE crash on exit.
_DRIVER = None


def _get_driver():
    """Return a Selenium Chrome driver, reusing one across tool calls.

    Attaches to an existing Chrome started with --remote-debugging-port=9222
    (so your logged-in session persists); if none is running, launches a new
    Chrome using CHROME_USER_DATA_DIR. Raises on failure; callers handle it.
    """
    global _DRIVER
    if _DRIVER is not None:
        return _DRIVER

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    # Is a Chrome already listening on the debug port?
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    chrome_running = sock.connect_ex(("127.0.0.1", 9222)) == 0
    sock.close()

    options = Options()
    if chrome_running:
        options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
        _DRIVER = webdriver.Chrome(options=options)
        print("  [browser] attached to existing Chrome on 127.0.0.1:9222")
    else:
        options.add_argument(f"--user-data-dir={CHROME_USER_DATA_DIR}")
        options.add_experimental_option("detach", True)  # keep Chrome open on exit
        _DRIVER = webdriver.Chrome(options=options)
        print("  [browser] launched a new Chrome window")

    return _DRIVER


def open_url(url: str) -> str:
    """Open a URL in a real Chrome browser via Selenium (new tab).

    Runs on whatever machine executes this script, NOT on the Ollama backend.
    """
    if not url.startswith(("http://", "https://")):
        return f"Refused to open non-http(s) URL: {url}"
    try:
        from selenium import webdriver  # noqa: F401
    except ImportError:
        return "Selenium is not installed. Run:\n  pip install selenium"
    try:
        driver = _get_driver()
        # Open in a new tab so we don't disturb existing tabs.
        driver.switch_to.new_window("tab")
        driver.get(url)
        return f"Opened {url} (page title: {driver.title!r})"
    except Exception as e:  # noqa: BLE001
        return f"Failed to open {url} in browser: {e}"


# --- Selectors that identify each Teams "screen" ---------------------------
# Launcher page: "Continue on this browser".
_SEL_LAUNCHER = [
    "button[data-tid='joinOnWeb']",
    "button[aria-label='Join meeting from this browser']",
]
# Pre-join page: name field + "Join now".
_SEL_PREJOIN_JOIN = [
    "button[data-tid='prejoin-join-button']",
    "#prejoin-join-button",
]
_SEL_NAME_INPUT = [
    "input[data-tid='prejoin-display-name-input']",
    "input[placeholder='Type your name']",
]
# In-meeting: compose box / chat items / leave button.
_SEL_IN_MEETING = [
    "div[data-tid='ckeditor']",
    "[data-tid='chat-pane-item']",
    "button[data-tid='hangup-main-btn']",
    "#hangup-button",
]
# Button that opens the chat rail if it is closed.
_SEL_CHAT_TOGGLE = [
    "button[data-tid='chat-button']",
    "#chat-button",
    "button[aria-label='Chat']",
]


def _first_displayed(driver, selectors):
    """Return the first visible element matching any selector, else None."""
    from selenium.webdriver.common.by import By

    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    return el
        except Exception:
            continue
    return None


def _js_click(driver, el) -> None:
    """Click via JavaScript so the tab-modal popup can't block the click."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    driver.execute_script("arguments[0].click();", el)


def _assess_teams_screen(driver) -> str:
    """Inspect the current page and report which Teams screen we are on.

    Returns one of: 'in_meeting', 'prejoin', 'launcher', 'auth', 'unknown'.
    Checked in priority order so the most-advanced screen wins.
    """
    if _first_displayed(driver, _SEL_IN_MEETING):
        return "in_meeting"
    if _first_displayed(driver, _SEL_PREJOIN_JOIN):
        return "prejoin"
    if _first_displayed(driver, _SEL_LAUNCHER):
        return "launcher"
    url = (driver.current_url or "").lower()
    if "login" in url or "sign" in url:
        return "auth"
    return "unknown"


def join_teams(url: str, display_name: str = "David's AI Agent") -> str:
    """Open a Teams meeting link and drive through EVERY screen until in-meeting.

    Rather than assuming one click joins, this assesses the current screen on
    each loop and acts accordingly:
      launcher  -> click "Continue on this browser"
      prejoin   -> type a display name (if asked) and click "Join now"
      in_meeting-> open the chat pane and finish
      auth      -> stop and report that sign-in is required

    Uses JavaScript clicks so the native "Open Microsoft Teams?" popup can't
    block input.
    """
    if not url.startswith(("http://", "https://")):
        return f"Refused to open non-http(s) URL: {url}"
    try:
        from selenium.webdriver.common.by import By  # noqa: F401
    except ImportError:
        return "Selenium is not installed. Run:\n  pip install selenium"

    try:
        driver = _get_driver()
        driver.switch_to.new_window("tab")
        driver.get(url)

        deadline = time.time() + 90
        last_screen = None

        while time.time() < deadline:
            screen = _assess_teams_screen(driver)
            if screen != last_screen:
                print(f"  [join] screen={screen} url={driver.current_url!r} "
                      f"title={driver.title!r}")
                last_screen = screen

            if screen == "in_meeting":
                # Make sure the chat rail is open so read/send work.
                if not _first_displayed(driver, ["div[data-tid='ckeditor']"]):
                    toggle = _first_displayed(driver, _SEL_CHAT_TOGGLE)
                    if toggle:
                        try:
                            _js_click(driver, toggle)
                        except Exception:
                            pass
                return "In the meeting. Chat pane ready."

            if screen == "launcher":
                btn = _first_displayed(driver, _SEL_LAUNCHER)
                if btn:
                    _js_click(driver, btn)

            elif screen == "prejoin":
                # Fill the display name if the field is present and empty.
                name_box = _first_displayed(driver, _SEL_NAME_INPUT)
                if name_box and not name_box.get_attribute("value"):
                    try:
                        name_box.clear()
                        name_box.send_keys(display_name)
                    except Exception:
                        pass
                btn = _first_displayed(driver, _SEL_PREJOIN_JOIN)
                if btn:
                    _js_click(driver, btn)

            elif screen == "auth":
                return (
                    "Reached a sign-in page. Log in to Teams in this Chrome "
                    "profile first, then re-run. "
                    f"(url={driver.current_url})"
                )

            time.sleep(1.5)

        return (
            "Timed out before reaching the meeting.\n"
            f"  Last screen: {last_screen}\n"
            f"  URL: {driver.current_url}\n"
            f"  Title: {driver.title}"
        )
    except Exception as e:  # noqa: BLE001
        return f"Failed to join Teams meeting: {e}"


_TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com", "teams.microsoft.us")


def _switch_to_teams_tab(driver) -> bool:
    """Point the driver at the Teams tab. Returns True if found.

    Matches by URL host first (a Teams /meet/ link often redirects the joined
    client to teams.live.com, not teams.microsoft.com), and falls back to any
    tab that has in-meeting / chat DOM present.
    """
    # Pass 1: match by known Teams hosts.
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            url = (driver.current_url or "").lower()
            if any(host in url for host in _TEAMS_HOSTS):
                return True
        except Exception:
            continue

    # Pass 2: match by DOM (chat compose box / chat items / meeting controls).
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            if _first_displayed(driver, _SEL_IN_MEETING) or _first_displayed(
                driver, ["[data-tid='chat-pane-item']", "div[data-tid='ckeditor']"]
            ):
                return True
        except Exception:
            continue

    return False


def read_teams_chat(limit: int = 20) -> str:
    """Read the most recent messages from the open Teams meeting chat pane.

    Uses the stable data-tid hooks in the Teams web DOM (not the hashed
    fui-* classes, which change between builds).
    """
    try:
        from selenium.webdriver.common.by import By
    except ImportError:
        return "Selenium is not installed. Run:\n  pip install selenium"

    try:
        driver = _get_driver()
        if not _switch_to_teams_tab(driver):
            return "No Teams tab is open. Join a meeting first with join_teams."

        _ensure_chat_open(driver)

        parsed = _parse_chat_messages(driver, limit=limit)
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
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        return "Selenium is not installed. Run:\n  pip install selenium"

    if not message.strip():
        return "Refused to send an empty message."

    try:
        driver = _get_driver()
        if not _switch_to_teams_tab(driver):
            return "No Teams tab is open. Join a meeting first with join_teams."

        wait = WebDriverWait(driver, 15)

        # The compose box is a contenteditable CKEditor div.
        box = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "div[data-tid='ckeditor'][contenteditable='true']")
            )
        )
        box.click()
        box.send_keys(message)

        # Prefer the explicit Send button; fall back to Ctrl+Enter.
        try:
            send_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button[data-tid='newMessageCommands-send']")
                )
            )
            send_btn.click()
        except Exception:
            from selenium.webdriver.common.keys import Keys

            box.send_keys(Keys.CONTROL, Keys.ENTER)

        return f"Sent message to Teams chat: {message!r}"
    except Exception as e:  # noqa: BLE001
        return f"Failed to send Teams message: {e}"


# System/control lines in the Teams chat we should NOT reply to.
_SYSTEM_MARKERS = (
    "joined the conversation",
    "left the conversation",
    "meeting started",
    "meeting ended",
    "chat has been turned on",
    "recording",
    "transcription",
    "today",
    "yesterday",
)


def _is_system_line(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _SYSTEM_MARKERS)


# Selectors for the meeting "Leave" (hang up) button.
_SEL_LEAVE = [
    "#hangup-button",
    "button[title='Leave']",
    "button[data-tid='hangup-main-btn']",
    "button[aria-label='Leave']",
]


def leave_meeting() -> str:
    """Click the Teams 'Leave' button to hang up / end the meeting."""
    try:
        from selenium.webdriver.common.by import By  # noqa: F401
    except ImportError:
        return "Selenium is not installed. Run:\n  pip install selenium"
    try:
        driver = _get_driver()
        if not _switch_to_teams_tab(driver):
            return "No Teams tab is open."
        btn = _first_displayed(driver, _SEL_LEAVE)
        if btn:
            _js_click(driver, btn)
            return "Clicked Leave - left the meeting."

        # Fallback: the meeting control bar may be hidden. Use the Teams
        # keyboard shortcut for Leave: Ctrl+Shift+H.
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        try:
            (
                ActionChains(driver)
                .key_down(Keys.CONTROL)
                .key_down(Keys.SHIFT)
                .send_keys("h")
                .key_up(Keys.SHIFT)
                .key_up(Keys.CONTROL)
                .perform()
            )
            return "Sent Leave shortcut (Ctrl+Shift+H) - left the meeting."
        except Exception:
            return "Leave button not found and Ctrl+Shift+H failed."
    except Exception as e:  # noqa: BLE001
        return f"Failed to leave meeting: {e}"


def _chat_pane_open(driver) -> bool:
    """True if the chat compose box is visible (chat rail is open)."""
    return _first_displayed(driver, ["div[data-tid='ckeditor']"]) is not None


def _ensure_chat_open(driver, wait_seconds: int = 15) -> bool:
    """Make sure the chat rail is open, retrying while the meeting UI loads.

    The '#chat-button' may not exist yet right after joining, so we poll for it
    (up to wait_seconds), click it, then confirm the compose box appeared.
    Returns True if the chat pane is open.
    """
    deadline = time.time() + wait_seconds
    clicked = False
    while time.time() < deadline:
        if _chat_pane_open(driver):
            return True
        toggle = _first_displayed(driver, _SEL_CHAT_TOGGLE)
        if toggle and not clicked:
            try:
                _js_click(driver, toggle)
                clicked = True
                print("  [chat] clicked chat button to open the pane")
            except Exception:
                pass
        time.sleep(1)
    ok = _chat_pane_open(driver)
    if not ok:
        print("  [chat] chat pane still not open (button not found or not ready)")
    return ok


def _parse_chat_messages(driver, limit: int = 20):
    """Parse recent chat items into structured dicts.

    Returns a list of {kind, author, text, is_self} (oldest to newest):
      kind    = 'message' (a real chat post) or 'control' (system line)
      author  = sender display name ('' for control lines)
      text    = message body / control text
      is_self = True if posted by this agent (fui-ChatMyMessage or AGENT_NAME)
    """
    from selenium.webdriver.common.by import By

    out = []
    items = driver.find_elements(By.CSS_SELECTOR, "[data-tid='chat-pane-item']")
    for item in items[-limit:]:
        # Real message?
        body = item.find_elements(By.CSS_SELECTOR, "[data-tid='chat-pane-message']")
        if body:
            author_els = item.find_elements(
                By.CSS_SELECTOR, "[data-tid='message-author-name']"
            )
            author = author_els[0].text.strip() if author_els else ""
            content_els = item.find_elements(By.CSS_SELECTOR, "[id^='content-']")
            text = " ".join(
                (content_els[0].text if content_els else body[0].text).split()
            )
            is_self = (
                bool(item.find_elements(By.CSS_SELECTOR, "[class*='ChatMyMessage']"))
                or author == AGENT_NAME
            )
            if text:
                out.append(
                    {"kind": "message", "author": author,
                     "text": text, "is_self": is_self}
                )
            continue

        # Control/system line?
        ctrl = item.find_elements(By.CSS_SELECTOR, "[data-tid='control-message-renderer']")
        if ctrl:
            text = " ".join(ctrl[0].text.split())
            if text:
                out.append(
                    {"kind": "control", "author": "",
                     "text": text, "is_self": False}
                )
    return out


def _latest_incoming_message(driver):
    """Return the most recent real message from someone OTHER than this agent,
    as {author, text}, or None."""
    for msg in reversed(_parse_chat_messages(driver, limit=20)):
        if msg["kind"] == "message" and not msg["is_self"]:
            return msg
    return None


# Opening message the agent posts when it joins the meeting chat.
INTRO_MESSAGE = os.environ.get(
    "INTRO_MESSAGE",
    "Hello, I'm David's AI assistant sitting in for David. "
    "Thank you very much for your continued patience. I'll update the status of those instances every 30 mins until all instances are patched."
    "Feel free to chat with me here - say 'bye' when you're done.",
)


CONVERSE_SYSTEM_PROMPT = (
    "You are David's assistant, standing in for David in a Microsoft Teams "
    "meeting chat. You are NOT David. "
    "Reply briefly and naturally (1-2 sentences) to the most recent message. "
    "If someone addresses you as David or greets David directly (e.g. "
    "'hello David', 'hey David'), politely correct them: clarify that you are "
    "David's assistant, not David, and offer to help or relay a message. "
    "Do not narrate your actions."
)


def converse_teams(poll_seconds: int = 3) -> None:
    """Autonomously watch the Teams chat and reply until someone says 'bye'.

    Runs with no timeout: polls the chat every `poll_seconds`, and for each new
    incoming message (ignoring system/control lines and our own messages) it
    asks the model for a reply and sends it. Stops when an incoming message
    contains 'bye'.
    """
    try:
        from selenium.webdriver.common.by import By  # noqa: F401
    except ImportError:
        print("Selenium is not installed. Run:\n  pip install selenium")
        return

    driver = _get_driver()
    if not _switch_to_teams_tab(driver):
        print("No Teams tab is open. Join a meeting first (join_teams).")
        return

    if _ensure_chat_open(driver):
        # Post an introduction once, right after joining and opening the chat.
        send_teams_message(INTRO_MESSAGE)
        print(f"me   > {INTRO_MESSAGE}")

    print(f"[converse] watching Teams chat every {poll_seconds}s "
          f"(agent name: {AGENT_NAME!r}). "
          "Say 'bye' in the chat to stop. Ctrl-C to abort.\n")

    messages: list = [{"role": "system", "content": CONVERSE_SYSTEM_PROMPT}]
    last_processed = None

    try:
        while True:
            # Chat rail can be closed (or not yet rendered); keep it open.
            if not _chat_pane_open(driver):
                _ensure_chat_open(driver)

            incoming = _latest_incoming_message(driver)

            # No new message from someone else -> keep watching. We identify the
            # sender by author name, so our own posts are ignored automatically.
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

            # Give the model the recent chat transcript (up to 20 messages) as
            # context, then ask it to reply to the latest message. This lets it
            # answer with awareness of the whole visible conversation.
            transcript = read_teams_chat(limit=20)
            user_turn = (
                "Recent Teams chat (oldest to newest):\n"
                f"{transcript}\n\n"
                f"{author} just said: {latest!r}\n"
                "Reply to them."
            )

            # Generate a reply (plain chat, no tools) and send it.
            messages.append({"role": "user", "content": user_turn})
            reply = _chat(messages).get("content", "").strip()
            if not reply:
                time.sleep(poll_seconds)
                continue
            messages.append({"role": "assistant", "content": reply})

            send_teams_message(reply)
            last_sent = reply
            print(f"me   > {reply}")

            time.sleep(poll_seconds)
    except (KeyboardInterrupt, EOFError):
        print("\n[converse] aborted.")


# Map tool NAME -> the actual Python function to run.
TOOL_REGISTRY = {
    "get_weather": get_weather,
    "calculator": calculator,
    "open_url": open_url,
    "join_teams": join_teams,
    "read_teams_chat": read_teams_chat,
    "send_teams_message": send_teams_message,
    "leave_meeting": leave_meeting,
}


# =============================================================================
# 2. THE TOOL SCHEMAS — the JSON description sent to the model.
#    This (not the source code above) is what the model actually "reads".
#    The `description` fields are how the model decides when to call each tool.
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
                    "city": {
                        "type": "string",
                        "description": "The city name, e.g. 'Tokyo'.",
                    }
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
                    "expression": {
                        "type": "string",
                        "description": "A math expression using + - * / and numbers.",
                    }
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
                    "url": {
                        "type": "string",
                        "description": "The full http(s) URL to open.",
                    }
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
                    "url": {
                        "type": "string",
                        "description": "The full Teams meeting URL.",
                    }
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
                    "limit": {
                        "type": "integer",
                        "description": "How many recent messages to read (default 20).",
                    }
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
                    "message": {
                        "type": "string",
                        "description": "The message text to send.",
                    }
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
]


# =============================================================================
# 3. LOW-LEVEL CALL — one round trip to Ollama's /api/chat.
# =============================================================================

def _chat(messages: list, tools: list | None = None) -> dict:
    """Send messages (and optionally tool schemas) to Ollama; return the message."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
    }
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
    """Run one tool-calling turn and return (reply, messages).

      1. Send prompt + tool schemas to the model.
      2. If the model returns tool_calls, RUN them here in Python.
      3. Feed the results back and ask again.
      4. Repeat until the model answers with plain text (no more tool calls).

    Pass the returned `messages` back in on the next call to keep multi-turn
    conversation context (used by the interactive loop).
    """
    if messages is None:
        messages = []
    messages.append({"role": "user", "content": prompt})

    for step in range(max_steps):
        message = _chat(messages, tools=TOOLS_SCHEMA)
        messages.append(message)  # keep the model's turn in history

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            # No tool requested -> this is the final natural-language answer.
            return message.get("content", ""), messages

        # The model asked to call one or more tools. Execute each locally.
        for call in tool_calls:
            fn_name = call["function"]["name"]
            fn_args = call["function"]["arguments"]  # already a dict in Ollama

            print(f"  [tool call] {fn_name}({fn_args})")

            fn = TOOL_REGISTRY.get(fn_name)
            if fn is None:
                result = f"Error: unknown tool '{fn_name}'"
            else:
                result = fn(**fn_args)

            print(f"  [tool result] {result}")

            # Send the tool's output back to the model as a 'tool' message.
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
        # Optional: join a meeting first if a URL is given.
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
                # --tools with no prompt -> interactive tool-enabled REPL.
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
