"""
chat_agent.py — Minimal Python client to talk to your AI agent (Ollama backend).

Your Terraform stack (infra_terraform/main.tf) provisions a backend EC2 instance
running Ollama on port 11434 with the model `smollm:1.7b`. This script sends
prompts to that Ollama server over its HTTP API.

Usage:
    # Point at your backend (use the Terraform output "backend_private_ip",
    # or "localhost" if you're running this ON the backend / via SSH tunnel):
    set OLLAMA_HOST=http://<backend-ip>:11434      # Windows cmd
    python chat_agent.py "Explain what an AI agent is in one sentence."

    # Or start an interactive chat loop (no argument):
    python chat_agent.py

Requires:
    pip install requests
"""

import os
import sys
import json

import requests

# Where the Ollama server lives. Override with the OLLAMA_HOST env var.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://demo-project-alb-1989896788.us-east-1.elb.amazonaws.com")
MODEL = os.environ.get("OLLAMA_MODEL", "smollm:1.7b")

# Connect/read timeout in seconds (model generation can be slow on t3.medium).
TIMEOUT = (10, 120)


def ask(prompt: str, history: list | None = None) -> str:
    """Send a single message to the agent and return its reply.

    Uses Ollama's /api/chat endpoint so we can keep a running conversation
    via the `history` list of {"role": ..., "content": ...} messages.
    """
    history = history or []
    messages = history + [{"role": "user", "content": prompt}]

    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,  # set True to stream tokens as they are generated
    }

    resp = requests.post(url, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"]


def chat_loop() -> None:
    """Simple interactive REPL that remembers the conversation."""
    print(f"Connected to {OLLAMA_HOST} (model: {MODEL})")
    print("Type your message. Use 'exit' or Ctrl-C to quit.\n")

    history: list[dict] = []
    try:
        while True:
            prompt = input("you > ").strip()
            if not prompt:
                continue
            if prompt.lower() in {"exit", "quit"}:
                break

            reply = ask(prompt, history)
            print(f"\nagent > {reply}\n")

            # Keep the exchange in history for context on the next turn.
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": reply})
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")


def main() -> None:
    try:
        if len(sys.argv) > 1:
            # One-shot mode: everything after the script name is the prompt.
            prompt = " ".join(sys.argv[1:])
            print(ask(prompt))
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
