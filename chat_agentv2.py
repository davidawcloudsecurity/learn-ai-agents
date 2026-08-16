"""
chat_agent.py — Minimal Python client to talk to your AI agent (Ollama backend),
now with a working TOOL-CALLING demo.

Your Terraform stack (infra_terraform/main.tf) provisions a backend EC2 instance
running Ollama on port 11434. This script sends prompts to that Ollama server
over its HTTP API, and can optionally let the model call local Python "tools".

Usage:
    set OLLAMA_HOST=http://<backend-ip>:11434      # Windows cmd

    # Plain chat (one-shot):
    python chat_agent.py "Explain what an AI agent is in one sentence."

    # Interactive chat loop:
    python chat_agent.py

    # Tool-calling demo (model may call get_weather / calculator):
    python chat_agent.py --tools "What is 42 * 17, and what's the weather in Tokyo?"

Requires:
    pip install requests

IMPORTANT: tool calling needs a model trained for it. smollm:1.7b is NOT reliable
at this. For the --tools demo, use something like qwen2.5:3b or llama3.1:8b:
    set OLLAMA_MODEL=qwen2.5:3b
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


def join_teams(url: str) -> str:
    """Open a Microsoft Teams meeting link and join in the browser via Selenium.

    The Teams launcher tries to open the desktop app, which makes Chrome show a
    native "Open Microsoft Teams?" popup. That popup is browser chrome, not page
    DOM, so it can't be clicked. Instead we click the launcher's own
    "Continue on this browser" button, which loads the Teams web client.

    To stop the native popup appearing at all, run once (Chrome closed):
        python chat_agentv2.py --setup-teams
    """
    if not url.startswith(("http://", "https://")):
        return f"Refused to open non-http(s) URL: {url}"
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        return "Selenium is not installed. Run:\n  pip install selenium"

    try:
        driver = _get_driver()
        driver.switch_to.new_window("tab")
        driver.get(url)

        wait = WebDriverWait(driver, 15)

        # XPaths for the launcher's web-join controls. The button may sit inside
        # an iframe, so we try the top document first, then each frame.
        xpaths = [
            "//button[contains(., 'Continue on this browser')]",
            "//*[contains(text(), 'Continue on this browser')]",
            "//*[contains(text(), 'Join on the web instead')]",
            "//*[@data-tid='joinOnWeb']",
        ]

        def try_click_in_current_context():
            for xp in xpaths:
                try:
                    el = wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
                    el.click()
                    return xp
                except Exception:
                    continue
            return None

        # 1) Try the main document.
        clicked = try_click_in_current_context()

        # 2) Fall back to searching each iframe.
        if not clicked:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            for frame in frames:
                try:
                    driver.switch_to.frame(frame)
                    clicked = try_click_in_current_context()
                finally:
                    driver.switch_to.default_content()
                if clicked:
                    break

        if clicked:
            return f"Clicked '{clicked}' - joining Teams in the browser."

        return (
            "Opened the Teams launcher but couldn't click "
            "'Continue on this browser'. The native 'Open Microsoft Teams?' "
            "popup is almost certainly blocking input. Run this once "
            "(with that Chrome closed):\n"
            "  python chat_agentv2.py --setup-teams\n"
            "then reopen Chrome and try again."
        )
    except Exception as e:  # noqa: BLE001
        return f"Failed to join Teams meeting: {e}"


def _switch_to_teams_tab(driver) -> bool:
    """Point the driver at the Teams tab. Returns True if found."""
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            if "teams.microsoft.com" in (driver.current_url or ""):
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

        items = driver.find_elements(By.CSS_SELECTOR, "[data-tid='chat-pane-item']")
        if not items:
            return "No chat messages found (chat pane may still be loading)."

        lines = []
        for item in items[-limit:]:
            text = " ".join(item.text.split())  # collapse whitespace/newlines
            if text:
                lines.append(text)

        return "\n".join(lines) if lines else "Chat pane is empty."
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


def _latest_chat_message() -> str | None:
    """Return the collapsed text of the last message in the Teams chat pane."""
    from selenium.webdriver.common.by import By

    driver = _get_driver()
    items = driver.find_elements(By.CSS_SELECTOR, "[data-tid='chat-pane-item']")
    if not items:
        return None
    return " ".join(items[-1].text.split()) or None


CONVERSE_SYSTEM_PROMPT = (
    "You are participating in a Microsoft Teams meeting chat on behalf of the user. "
    "Reply briefly and naturally (1-2 sentences) to the most recent message. "
    "Do not narrate your actions or mention that you are an AI."
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

    print(f"[converse] watching Teams chat every {poll_seconds}s. "
          "Say 'bye' in the chat to stop. Ctrl-C to abort.\n")

    messages: list = [{"role": "system", "content": CONVERSE_SYSTEM_PROMPT}]
    last_processed = None
    last_sent = None

    try:
        while True:
            latest = _latest_chat_message()

            # Nothing new, our own message, or a system line -> keep watching.
            if (
                not latest
                or latest == last_processed
                or (last_sent and last_sent in latest)
                or _is_system_line(latest)
            ):
                if latest and _is_system_line(latest):
                    last_processed = latest
                time.sleep(poll_seconds)
                continue

            print(f"them > {latest}")
            last_processed = latest

            # Stop condition: the other person said bye.
            if "bye" in latest.lower():
                farewell = "Bye! Talk to you later."
                send_teams_message(farewell)
                print(f"me   > {farewell}")
                print("\n[converse] heard 'bye' - ending conversation.")
                break

            # Generate a reply (plain chat, no tools) and send it.
            messages.append({"role": "user", "content": latest})
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
