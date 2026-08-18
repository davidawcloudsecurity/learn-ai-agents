# Vision: Deploy open source llm in vscode using cli / claude-code

Ollama runs as a systemd service, so `journalctl` reads its logs by unit name. Run these on the backend EC2 (via SSH or SSM Session Manager). Most need `sudo`.

**The essentials**

```bash
# All logs for the ollama service
sudo journalctl -u ollama

# Follow live (like tail -f) — great while you hit the API
sudo journalctl -u ollama -f

# Last 100 lines, newest at the bottom
sudo journalctl -u ollama -n 100

# Jump to the end
sudo journalctl -u ollama -e
```

**Filter by time** (useful for debugging a boot-time issue like your `$HOME` panic)

```bash
# Only since the last reboot
sudo journalctl -u ollama -b

# Since a relative time
sudo journalctl -u ollama --since "10 min ago"
sudo journalctl -u ollama --since today

# A specific window
sudo journalctl -u ollama --since "2026-08-16 02:20" --until "2026-08-16 02:30"
```

**Filter by severity** (only warnings/errors)

```bash
sudo journalctl -u ollama -p err        # error and worse
sudo journalctl -u ollama -p warning    # warning and worse
```

**Handy combos**

```bash
# Follow, no pager, with readable timestamps
sudo journalctl -u ollama -f -o short-iso

# Search for a keyword
sudo journalctl -u ollama | grep -i "gpu\|panic\|error"
```

A couple of notes specific to your setup:

- The `$HOME is not defined` panic you saw earlier came from the **`ollama pull` CLI call in cloud-init**, not the ollama *service*. So that particular error won't appear in `journalctl -u ollama` — it lives in the cloud-init logs instead:
  ```bash
  sudo journalctl -u cloud-final          # the module that runs user_data scripts
  sudo cat /var/log/cloud-init-output.log # full user_data stdout/stderr (easiest)
  ```
  That `cloud-init-output.log` is where the whole panic + goroutine dump you pasted actually came from.

- Quick service health checks alongside the logs:
  ```bash
  systemctl status ollama          # running? enabled? recent log lines
  systemctl is-active ollama
  curl -s http://localhost:11434/api/tags   # is the API answering + what's pulled
  ```

So: use `journalctl -u ollama` for the running server (crashes, request handling, model loading), and `cloud-init-output.log` for anything that happened during first-boot `user_data`.

### How to deploy llm in aws ec2
```
https://dev.to/aws-builders/deploy-your-llm-on-aws-ec2-2ig3
```
# learn-ai-agents
### How to observe ai agents
```
https://strandsagents.com/latest/documentation/docs/user-guide/observability-evaluation/observability/
https://opentelemetry.io/
https://www.langchain.com/langsmith
https://langfuse.com/
https://docs.ragas.io/en/stable/
https://arize.com/
```
### How to let ai agents see
```
https://towardsdatascience.com/building-visual-agents-that-can-navigate-the-web-autonomously-1184efbfe895/
```
### How to browse the web for ai agents
```
https://docs.browser-use.com/introduction
https://github.com/browser-use/browser-use
```
### Resources
```
https://www.langchain.com/langgraph
https://strandsagents.com/latest/
https://www.crewai.com/
```
### Demostrate understanding of LLM/AI
```
Build Text Generation
ChatBots
Classification
Translation
Summarization
```
### What is prompt engineering

### What is context engineering

### practical advice for developers on building effective agents.
```
https://www.anthropic.com/engineering/building-effective-agents
```
### synergizes reasoning and acting in language models | whitepaper
```
https://arxiv.org/pdf/2210.03629
```
### how to turn this into youtube videos
```
https://aws.amazon.com/what-is/ai-agents/#what-are-ai-agents--njg10c
```
