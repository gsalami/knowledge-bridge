# Knowledge Bridge (KB)

A personal AI knowledge base with a Kanban-style board. Drop in a topic or URL, and it uses Perplexity to research it, then organizes entries into **Backlog → Working On → Done**.

![Knowledge Bridge Screenshot](https://gu.kuble.com/kb/og.png)

## Features

- 🔍 **Perplexity-powered research** — paste a URL or topic and get a full structured report
- 📋 **Kanban board** — drag entries through Backlog → Working On → Done / Ignored
- 🔗 **Merge detection** — similar backlog entries are flagged for merging
- 🖼️ **Image search** — find and attach images to entries (article scrape + DuckDuckGo/Bing)
- 📊 **Gamma slides** — generate a Gamma presentation from any entry
- 🔒 **Password-protected frontend**

## Setup

### Requirements

```bash
pip install flask requests beautifulsoup4 duckduckgo-search
```

### Configuration

Set these environment variables (or edit defaults in `api.py`):

| Variable | Description | Default |
|---|---|---|
| `KB_DB_PATH` | Path to SQLite database | `./kb.db` |
| `PERPLEXITY_KEY` | Perplexity API key | — |
| `GAMMA_API_KEY` | Gamma API key (optional, for slide generation) | — |
| `KB_PASSWORD` | Frontend password | `changeme` |

### Run

```bash
export PERPLEXITY_KEY=your_key_here
export KB_PASSWORD=your_password
python api.py
```

The app runs on `http://localhost:8084`.

### Nginx (optional)

If you want to serve via Nginx, proxy `/kb/` to the Flask backend:

```nginx
location /kb/ {
    proxy_pass http://127.0.0.1:8084;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Serve `index.html` and `favicon.svg` as static files, or let Flask handle them.

## API

### `POST /kb/api/entries`

Create a new entry. Triggers Perplexity research.

```json
{
  "input_text": "Claude 3.5 Sonnet",
  "input_url": "https://anthropic.com/blog/..."
}
```

### `GET /kb/api/entries`

List all entries.

### `PATCH /kb/api/entries/:id/status`

Update status: `backlog | working on | done | ignored`

### `DELETE /kb/api/entries/:id`

Delete an entry.

### `POST /kb/api/entries/:id/find-images`

Find images for an entry (article scrape + web search).

### `POST /kb/api/entries/:id/generate-slide`

Generate a Gamma presentation from the entry.

## Stack

- **Backend:** Python / Flask
- **Frontend:** Vanilla JS / HTML / CSS (single file)
- **DB:** SQLite
- **Research:** Perplexity Sonar API
- **Slides:** Gamma API (optional)
- **Images:** DuckDuckGo / Bing image search

## License

MIT
