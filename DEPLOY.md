# Deploying AgentLens

AgentLens is built to run **locally with Ollama**. A public website can't run a local model on a free host, so there are three practical options:

| Option | Model | Best for | Effort |
|---|---|---|---|
| **S. Streamlit Community Cloud** (easiest) | Groq-hosted open model | a free permanent link, no Docker | 10 min |
| **A. Local + public tunnel** | your Ollama model | an interview demo, a quick share | 5 min |
| **B. Render + Groq** (always-on link) | Groq-hosted open model (Llama) | a permanent demo link for your resume | 15 min |
| **C. Docker** | Ollama on the host, or Groq | your own server | 10 min |

## Settings for any public deployment

All are environment variables and all are optional (local use is unchanged):

| Variable | Effect |
|---|---|
| `GROQ_API_KEY` | Use Groq's hosted open models. Free key at <https://console.groq.com>. The app auto-selects `llama-3.1-8b-instant` when no Ollama is found |
| `AGENTLENS_READONLY=1` | **Public demo mode**: no uploads, deletes or index changes; Evals and Red team run on the offline simulator so nobody can spend your API quota |
| `AGENTLENS_RATE_LIMIT=20` | Model-calling requests allowed per visitor per minute |
| `AGENTLENS_PASSWORD=...` | The whole site asks for a password (any username). Use this if you don't want strangers using it |

## Option S: Streamlit Community Cloud (easiest free link)

`streamlit_app.py` is a Streamlit edition of AgentLens: the same router, retrieval, web search, SQL tool, answer verification, evals and red team behind a simpler screen (Chat, Evals, Red team, Knowledge). It is themed with the project's emerald/charcoal palette in `.streamlit/config.toml`.

1. Push the repo to GitHub.
2. Get a free Groq key at <https://console.groq.com> (API Keys, Create).
3. Go to <https://share.streamlit.io>, sign in with GitHub and choose **Create app** (deploy from a GitHub repo).
4. Repository: `hackercloudy/AgentLens`, Branch: `main`, **Main file path: `streamlit_app.py`**.
5. Open **Advanced settings**, choose Python 3.12, and paste into **Secrets**:
   ```toml
   GROQ_API_KEY = "gsk_your_key_here"
   ```
   Optional: add `AGENTLENS_PASSWORD = "choose-a-password"` to put a password in front of the app.
6. Click **Deploy**. After a few minutes you get a link like `https://your-app.streamlit.app`.

Run it locally the same way: `streamlit run streamlit_app.py`.

Notes: free apps go to sleep when nobody visits and need a click to wake; web search may be blocked from the cloud (it falls back to Wikipedia); Evals and Red team default to the offline simulator so a visitor can't spend your API quota (a checkbox switches to the real model).

## Option A: local + public link (best for the interview)

Your laptop does the work, so the link only works while the server is running.

```powershell
# terminal 1: start the app (set a password first if you will share the link)
$env:AGENTLENS_PASSWORD = "choose-a-password"
.venv\Scripts\activate
python -m uvicorn app.main:app --port 8000

# terminal 2 (one-time install, then reopen the terminal)
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

`cloudflared` prints a link like `https://something-random.trycloudflare.com`. Open it from any device. It changes every time and stops when you close the terminal.

## Option B: Render + Groq (permanent link)

1. Push the project to GitHub (see the README).
2. Get a free Groq key at <https://console.groq.com> (API Keys, Create).
3. On <https://render.com> choose **New, then Blueprint**, pick your repo and accept the settings in `render.yaml`. (Or New, Web Service, runtime **Docker**, with the same environment variables.)
4. In the service's **Environment** tab add `GROQ_API_KEY`. Optionally add `AGENTLENS_PASSWORD`.
5. Wait for the build. Render gives you a URL like `https://agentlens-xxxx.onrender.com`.

Things to know about the free plan:
- The service **sleeps after about 15 minutes idle**, and the first visit afterwards takes 30 to 60 seconds to wake. Open it a minute before showing anyone.
- The disk is temporary and memory is small (512 MB). If the build or the app runs out of memory, use a paid instance.
- Web search may be blocked more often from a cloud IP. The app falls back to Wikipedia.
- Answers come from the Groq model, so speed and quality differ from your local model. Say which one you are showing.

## Option C: Docker

```bash
docker compose up --build        # http://localhost:8000, uses Ollama running on the host
```

The `Dockerfile` listens on `$PORT` (default 8000), so it also works on Railway, Fly.io and similar hosts.

## Checklist before sharing a public link

- [ ] No secrets in the repo (`.env` is git-ignored; keys go in the host's environment settings).
- [ ] `AGENTLENS_READONLY=1` and `AGENTLENS_RATE_LIMIT` are set.
- [ ] You opened the link once yourself and asked a question.
- [ ] You know which model it uses and can say so.
