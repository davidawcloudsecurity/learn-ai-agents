"""
chat_agent.py — Chat agent using the official `anthropic` Python SDK,
backed by Ollama instead of api.anthropic.com.

Why this works:
    Ollama now implements the Anthropic Messages API (/v1/messages-compatible
    endpoint at its root, e.g. http://<host>:11434). That means you can point
    the same `anthropic` SDK you'd use for Claude Code / the Claude API at
    your Ollama server, and get identical request/response shapes — including
    proper `tools=[...]` and `tool_use` / `tool_result` blocks.

    This satisfies a "use the Claude SDK/API for tools" requirement while
    keeping inference on your own Ollama-backed EC2 instance.

Terraform context:
    Your infra_terraform/main.tf provisions a backend EC2 instance running
    Ollama on port 11434. Point OLLAMA_HOST at that instance (or an ALB in
    front of it, per your existing script).

IMPORTANT MODEL CAVEAT:
    Tool calling requires a model that actually supports it. `smollm:1.7b`
    is tiny and unreliable for tool use. For real tool-calling behavior, pull
    a model with tool support instead, e.g.:
        ollama pull qwen2.5:7b
        ollama pull llama3.1:8b
    and set OLLAMA_MODEL accordingly.

Usage:
    set OLLAMA_HOST=http://<backend-ip>:11434      # Windows cmd
    set OLLAMA_MODEL=qwen2.5:7b
    python chat_agent.py "What's the weather in San Francisco?"

    # Or interactive:
    python chat_agent.py

Requires:
    pip install anthropic
"""

import os
import sys
import json

import anthropic

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")  # pick a tool-capable model

# The anthropic SDK just needs *some* non-empty api_key string; Ollama
# doesn't check it, but the SDK will refuse to send requests without one.
client = anthropic.Anthropic(base_url=OLLAMA_HOST, api_key="ollama")


# ---------------------------------------------------------------------------
# Example tool. Add more of these as needed — this is the same `tools`
# schema you'd use against the real Anthropic API.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_time",
        "description": "Get the current date and time.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def run_tool(name: str, tool_input: dict) -> str:
    """Dispatch a tool call to its actual implementation."""
    if name == "get_time":
        from datetime import datetime

        return datetime.now().isoformat()
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Core chat loop with tool-use handling
# ---------------------------------------------------------------------------


def ask(prompt: str, history: list | None = None) -> tuple[str, list]:
    """Send a message, resolving any tool calls the model makes.

    Returns (final_text_reply, updated_history).
    """
    history = history or []
    messages = history + [{"role": "user", "content": prompt}]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        # If the model wants to use a tool, run it and feed the result back.
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            continue  # let the model see the tool result and respond again

        # Otherwise, extract the final text reply.
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        messages.append({"role": "assistant", "content": response.content})
        return text, messages


def chat_loop() -> None:
    print(f"Connected to {OLLAMA_HOST} (model: {MODEL})")
    print("Type your message. Use 'exit' or Ctrl-C to quit.\n")

    history: list = []
    try:
        while True:
            prompt = input("you > ").strip()
            if not prompt:
                continue
            if prompt.lower() in {"exit", "quit"}:
                break

            reply, history = ask(prompt, history)
            print(f"\nagent > {reply}\n")
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")


def main() -> None:
    try:
        if len(sys.argv) > 1:
            prompt = " ".join(sys.argv[1:])
            reply, _ = ask(prompt)
            print(reply)
        else:
            chat_loop()
    except anthropic.APIConnectionError:
        print(
            f"ERROR: Could not reach Ollama at {OLLAMA_HOST}.\n"
            "  - Check the backend EC2 is running and Ollama is up (ollama serve).\n"
            "  - Set OLLAMA_HOST to the backend IP, e.g. http://172.168.2.x:11434\n"
            "  - Security group must allow port 11434 from where you run this.\n"
            "  - Confirm your Ollama version supports the Anthropic-compatible API.",
            file=sys.stderr,
        )
        sys.exit(1)
    except anthropic.APIStatusError as e:
        print(f"ERROR: Ollama/Anthropic-compat endpoint returned an error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()