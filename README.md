# Knowledge Bridge (KB)

Personal AI knowledge base for Gu's research workflow. The live app runs at:

- `https://gu.kuble.com/kb/`
- Server path: `/var/www/publish/kb/`
- Backend service: `kb-api.service`

KB is a Kanban-style board for links, topics and research notes. Entries move through **Backlog → Working On → Done / Ignored** and can be tagged, grouped into presentations, annotated and turned into Gamma slides.

## Features

- Kanban board with drag-and-drop status changes
- Compact tag filtering with top quick-tags plus an all-tags picker
- Tag editing and bulk tag operations
- Date filters and presentation filters
- Similar-entry merge detection
- Image search and image attachment helpers
- Gamma slide generation
- Agent API under `/kb/api/agent/*` for external coding/research agents
- Password-protected frontend

## Repository hygiene

This repo contains source code and lightweight assets only:

- `api.py`
- `index.html`
- `favicon.svg`
- docs

Do **not** commit runtime data or local artifacts:

- `kb.db`, `*.db`, `*.db-wal`, `*.db-shm`
- `__pycache__/`, `*.pyc`
- `.env`, local secret files
- server backup files like `*.bak-*`
- downloaded videos/audio/images unless intentionally added as source assets

## Requirements

```bash
pip install flask requests beautifulsoup4 ddgs duckduckgo-search
```

`ddgs` is preferred for DuckDuckGo search. `duckduckgo-search` is kept as fallback compatibility.

## Configuration

All secrets and runtime paths should come from environment variables or systemd EnvironmentFiles.

| Variable | Description | Default |
|---|---|---|
| `KB_DB_PATH` | SQLite database path | `/var/www/publish/kb/kb.db` |
| `KB_PASSWORD` | Frontend password | `changeme` |
| `GAMMA_API_KEY` | Gamma API key for slide generation | empty |
| `PERPLEXITY_API_KEY` | Legacy research fallback | empty |
| `OPENAI_API_KEY` | Grounded research provider, read from env or OpenClaw config | empty |
| `XAI_API_KEY` | Grounded research provider, read from env or OpenClaw config | empty |
| `KB_AGENT_API_TOKEN` | Bearer token for `/kb/api/agent/*` | empty |

On Gu's server, runtime secrets live outside git in root-owned EnvironmentFiles.

## Run locally

```bash
export KB_DB_PATH="$PWD/kb.db"
export KB_PASSWORD="<local-dev-password>"
python3 api.py
```

The app runs on `http://localhost:8084`.

## Live deployment notes

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for the exact server paths and verification commands.

For frontend-only changes, copying `index.html` is enough, no service restart needed. For `api.py` changes, restart `kb-api.service` after syntax checks.

## Verification

```bash
python3 - <<'PY'
from pathlib import Path
import re
html = Path('index.html').read_text()
scripts = '\n'.join(re.findall(r'<script(?:\\s[^>]*)?>(.*?)</script>', html, flags=re.S|re.I))
Path('/tmp/kb-inline.js').write_text(scripts)
print(len(scripts), 'scripts', len(scripts), 'chars')
PY
node --check /tmp/kb-inline.js
python3 -m py_compile api.py
```

## API overview

- `POST /kb/api/entries` creates an entry
- `GET /kb/api/entries` lists entries
- `GET /kb/api/entries/<id>` reads one entry
- `PATCH /kb/api/entries/<id>/status` updates status
- `PATCH /kb/api/entries/<id>/tags` updates tags
- `DELETE /kb/api/entries/<id>` deletes an entry
- `POST /kb/api/entries/<id>/find-images` finds images
- `POST /kb/api/entries/<id>/generate-slide` starts Gamma slide generation
- `GET /kb/api/agent/docs` documents the external agent API

## Stack

- Backend: Python / Flask
- Frontend: Vanilla JS / HTML / CSS
- DB: SQLite
- Research providers: OpenAI/xAI/legacy Perplexity hooks
- Slides: Gamma API
- Images: article scrape plus DuckDuckGo/Bing helpers
