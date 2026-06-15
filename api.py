#!/usr/bin/env python3
"""
Knowledge Bridge API
Flask backend for kb kanban board
Port: 8084
"""

import sqlite3
import json
import os
import re
import hmac
import functools
import subprocess
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape
from urllib.parse import urlparse, urljoin
from flask import Flask, request, jsonify, send_from_directory, Response
import requests
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS as DDGS_NEW
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS as DDGS_NEW
        HAS_DDGS = True
    except ImportError:
        HAS_DDGS = False

app = Flask(__name__, static_folder='.')

DB_PATH = os.environ.get('KB_DB_PATH', '/var/www/publish/kb/kb.db')
PERPLEXITY_KEY = os.environ.get('PERPLEXITY_API_KEY')  # legacy fallback only
GAMMA_API_KEY = os.environ.get('GAMMA_API_KEY', '')
GAMMA_API_URL = 'https://public-api.gamma.app/v1.0'
PASSWORD = os.environ.get('KB_PASSWORD', 'changeme')


def get_config_secret(*names):
    """Read API keys from env first, then OpenClaw config env vars."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    try:
        with open('/root/.openclaw/openclaw.json') as f:
            cfg = json.load(f)
        env_vars = cfg.get('env', {}).get('vars', {})
        for name in names:
            val = env_vars.get(name)
            if val:
                return val
        # xAI is stored one level higher in the current OpenClaw config.
        for name in names:
            val = cfg.get('env', {}).get(name)
            if val:
                return val
    except Exception:
        pass
    return None

OPENAI_KEY = get_config_secret('OPENAI_API_KEY')
XAI_KEY = get_config_secret('XAI_API_KEY')


CATEGORIES = [
    'AI Tools', 'AI Research', 'AI Business', 'Prompt Engineering',
    'LLM', 'Agents', 'Image/Video AI', 'AI Policy', 'General'
]

def normalize_tags(raw):
    """Normalize tags to a unique, lowercase JSON-safe list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = re.split(r'[,;\n]+', raw)
    if not isinstance(raw, list):
        return []
    tags = []
    seen = set()
    for tag in raw:
        tag = re.sub(r'\s+', ' ', str(tag).strip().lower())
        tag = re.sub(r'[^0-9a-zäöüéèàêâîôûç./ _-]', '', tag, flags=re.IGNORECASE).strip(' ./_-')
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag[:40])
    return tags[:20]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS kb_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            full_report TEXT NOT NULL,
            sources TEXT NOT NULL,
            category TEXT NOT NULL,
            relevance TEXT NOT NULL,
            relevance_score INTEGER DEFAULT 3,
            status TEXT DEFAULT 'backlog',
            input_text TEXT,
            input_url TEXT,
            images TEXT DEFAULT '[]',
            selected_image TEXT DEFAULT NULL,
            gamma_url TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    # Migrations: add columns if missing
    for col, default in [
        ('images', '"[]"'),
        ('selected_image', 'NULL'),
        ('gamma_url', 'NULL'),
        ('presentation_id', 'NULL'),
        ('tags', '"[]"'),
        ('notes', '""'),
    ]:
        try:
            conn.execute(f'ALTER TABLE kb_entries ADD COLUMN {col} TEXT DEFAULT {default}')
        except Exception:
            pass
    conn.execute('''
        CREATE TABLE IF NOT EXISTS kb_presentations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            gamma_id TEXT,
            gamma_url TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()


def fetch_readable_url(url: str, timeout: int = 15) -> dict:
    """Fetch a URL and extract readable text. Only returned URLs may become sources."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; KnowledgeBridge/1.0; +https://gu.kuble.com/kb/)'
        }, allow_redirects=True)
        resp.raise_for_status()
        ctype = resp.headers.get('content-type', '')
        if 'text/html' not in ctype and 'text/plain' not in ctype and 'application/xhtml' not in ctype:
            return {
                'url': resp.url,
                'title': urlparse(resp.url).netloc,
                'text': f'Non-HTML source fetched. Content-Type: {ctype}',
                'source_type': 'fetched'
            }
        soup = BeautifulSoup(resp.text[:1000000], 'html.parser')
        for tag in soup(['script', 'style', 'noscript', 'svg', 'form', 'nav', 'footer', 'aside']):
            tag.decompose()
        title = ''
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title.get('content', '').strip()
        elif soup.title and soup.title.string:
            title = soup.title.string.strip()
        main = soup.find('article') or soup.find('main') or soup.body or soup
        text = main.get_text('\n', strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        if len(text) < 300:
            text = soup.get_text('\n', strip=True)
        return {
            'url': resp.url,
            'title': title[:220] or urlparse(resp.url).netloc,
            'text': text[:14000],
            'source_type': 'fetched'
        }
    except Exception as e:
        return None


def search_grounding_sources(query: str, input_url: str = None, max_results: int = 5) -> list:
    """Find and fetch grounding sources before the LLM writes anything."""
    docs = []
    seen = set()

    def add_doc(doc):
        if not doc or not doc.get('url'):
            return
        key = doc['url'].split('#')[0].rstrip('/')
        if key in seen:
            return
        seen.add(key)
        if doc.get('text'):
            docs.append(doc)

    if input_url:
        add_doc(fetch_readable_url(input_url))

    if HAS_DDGS and query:
        try:
            with DDGS_NEW() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            for r in results:
                href = r.get('href') or r.get('url')
                if not href:
                    continue
                doc = fetch_readable_url(href, timeout=10)
                if doc:
                    # Prefer fetched title but keep search snippet as extra context.
                    snippet = r.get('body') or r.get('snippet') or ''
                    if snippet and snippet not in doc.get('text', '')[:1000]:
                        doc['text'] = (snippet + '\n\n' + doc.get('text', ''))[:14000]
                    add_doc(doc)
                else:
                    add_doc({
                        'url': href,
                        'title': r.get('title') or urlparse(href).netloc,
                        'text': r.get('body') or r.get('snippet') or '',
                        'source_type': 'search_result'
                    })
        except Exception:
            pass
    return docs[:6]


def build_grounded_prompt(topic: str, url: str, docs: list) -> str:
    source_blocks = []
    for i, doc in enumerate(docs, start=1):
        source_blocks.append(
            f"[Quelle {i}]\nTitel: {doc.get('title','')}\nURL: {doc.get('url','')}\nTextauszug:\n{doc.get('text','')[:10000]}"
        )
    sources_text = '\n\n---\n\n'.join(source_blocks) if source_blocks else 'Keine Quellen konnten direkt gelesen werden.'
    return f"""Thema/Input: {topic}
URL falls vorhanden: {url or ''}

Verfügbare geerdete Quellen, nutze ausschliesslich diese Informationen:

{sources_text}

Aufgabe: Erstelle daraus den Knowledge-Bridge-Eintrag als valides JSON. Wenn Quellen dünn sind, sage das transparent im Bericht statt Fakten zu erfinden."""


def parse_research_json(content: str, topic: str, allowed_sources: list) -> dict:
    """Parse model JSON and clamp sources/category/score to safe values."""
    content_clean = re.sub(r'^```(?:json)?\s*', '', (content or '').strip(), flags=re.MULTILINE)
    content_clean = re.sub(r'\s*```\s*$', '', content_clean.strip())
    data = None
    json_match = re.search(r'\{.*\}', content_clean, re.DOTALL)
    for candidate in ([json_match.group()] if json_match else []) + [content_clean]:
        try:
            data = json.loads(candidate)
            break
        except Exception:
            pass
    if not isinstance(data, dict):
        data = {
            'title': topic[:80],
            'summary': content_clean[:300] or 'Research konnte nicht sauber geparst werden.',
            'full_report': content_clean or 'Keine verwertbare Antwort erhalten.',
            'sources': [],
            'category': 'General',
            'relevance': 'Manual review needed',
            'relevance_score': 3,
        }

    allowed = {u.split('#')[0].rstrip('/'): u for u in allowed_sources if u}
    cleaned_sources = []
    for src in data.get('sources') or []:
        src = str(src).strip()
        key = src.split('#')[0].rstrip('/')
        if key in allowed and allowed[key] not in cleaned_sources:
            cleaned_sources.append(allowed[key])
    if not cleaned_sources:
        cleaned_sources = allowed_sources[:3]

    category = data.get('category') if data.get('category') in CATEGORIES else 'General'
    try:
        score = int(data.get('relevance_score', 3))
    except Exception:
        score = 3

    return {
        'title': str(data.get('title') or topic[:80])[:100],
        'summary': str(data.get('summary') or '')[:1200],
        'full_report': str(data.get('full_report') or data.get('summary') or ''),
        'sources': cleaned_sources,
        'category': category,
        'relevance': str(data.get('relevance') or 'Relevant für das monatliche KI-Wissens-Update.')[:800],
        'relevance_score': max(1, min(5, score)),
    }


def openai_grounded_research(topic: str, url: str, docs: list) -> dict:
    if not OPENAI_KEY:
        raise RuntimeError('OPENAI_API_KEY missing')
    system = """Du bist ein präziser Knowledge-Bridge-Researcher. Schreibe Schweizer Hochdeutsch, nie ß, immer ss.
Du darfst nur Fakten verwenden, die in den bereitgestellten Quellen stehen. Keine erfundenen Quellen, keine ungestützten Details.
Gib nur valides JSON zurück mit: title, summary, full_report, sources, category, relevance, relevance_score.
full_report: Markdown, mindestens 300 Wörter, mit klarer Einordnung und Unsicherheiten falls die Quellenlage dünn ist.
sources: Nur URLs aus den bereitgestellten Quellen."""
    payload = {
        'model': os.environ.get('KB_OPENAI_MODEL', 'gpt-4.1-mini'),
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': build_grounded_prompt(topic, url, docs)}
        ],
        'temperature': 0.2,
        'max_tokens': 2500,
        'response_format': {'type': 'json_object'}
    }
    resp = requests.post('https://api.openai.com/v1/chat/completions',
                         headers={'Authorization': f'Bearer {OPENAI_KEY}', 'Content-Type': 'application/json'},
                         json=payload, timeout=90)
    resp.raise_for_status()
    content = resp.json()['choices'][0]['message']['content']
    return parse_research_json(content, topic, [d.get('url') for d in docs])


def xai_grounded_research(topic: str, url: str, docs: list) -> dict:
    if not XAI_KEY:
        raise RuntimeError('XAI_API_KEY missing')
    system = """Du bist ein präziser Knowledge-Bridge-Researcher. Schreibe Schweizer Hochdeutsch, nie ß, immer ss.
Nutze nur die bereitgestellten Quellen. Gib nur valides JSON mit title, summary, full_report, sources, category, relevance, relevance_score zurück."""
    payload = {
        'model': os.environ.get('KB_XAI_MODEL', 'grok-4-latest'),
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': build_grounded_prompt(topic, url, docs)}
        ],
        'temperature': 0.2,
        'max_tokens': 2500,
        'response_format': {'type': 'json_object'}
    }
    resp = requests.post('https://api.x.ai/v1/chat/completions',
                         headers={'Authorization': f'Bearer {XAI_KEY}', 'Content-Type': 'application/json'},
                         json=payload, timeout=90)
    resp.raise_for_status()
    content = resp.json()['choices'][0]['message']['content']
    return parse_research_json(content, topic, [d.get('url') for d in docs])


def perplexity_research(topic: str, url: str = None) -> dict:
    """Grounded KB research. Kept name for compatibility with existing call sites."""
    search_query = ' '.join(filter(None, [topic, urlparse(url or '').netloc]))
    docs = search_grounding_sources(search_query, url)
    if not docs:
        # Last resort: preserve API behaviour but mark the weak source situation clearly.
        docs = [{
            'url': url or '',
            'title': topic[:80],
            'text': 'Keine direkt lesbaren Quellen gefunden. Der Eintrag muss manuell geprüft werden.',
            'source_type': 'manual_review'
        }]
    try:
        return openai_grounded_research(topic, url, docs)
    except Exception as openai_error:
        try:
            result = xai_grounded_research(topic, url, docs)
            result['full_report'] += f"\n\n_Hinweis: OpenAI Research war nicht verfügbar, Grok-Fallback wurde genutzt._"
            return result
        except Exception as xai_error:
            return {
                'title': topic[:80],
                'summary': 'Research konnte nicht automatisch abgeschlossen werden. Bitte manuell prüfen.',
                'full_report': f"## Research fehlgeschlagen\n\nDie Quellen wurden zwar vorbereitet, aber weder OpenAI noch Grok konnten einen Bericht erstellen.\n\nOpenAI Fehler: {openai_error}\n\nGrok Fehler: {xai_error}",
                'sources': [d.get('url') for d in docs if d.get('url')][:5],
                'category': 'General',
                'relevance': 'Manual review needed',
                'relevance_score': 3
            }

def extract_page_title(url: str) -> str:
    """Fetch a lightweight page title for pre-merge checks. Best-effort only."""
    if not url:
        return ''
    try:
        resp = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text[:200000], 'html.parser')
        title = soup.find('meta', property='og:title')
        if title and title.get('content'):
            return title.get('content', '').strip()[:200]
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:200]
    except Exception:
        pass
    return ''


def token_set(text: str) -> set:
    """Tokenize titles, summaries and URLs into comparable words."""
    STOPWORDS = {'the','a','an','and','or','of','in','to','is','are','was','were',
                 'for','on','at','by','with','from','as','that','this','it','be',
                 'der','die','das','und','oder','von','in','zu','ist','sind','war',
                 'für','auf','bei','mit','aus','als','dass','ein','eine','im','den',
                 'https','http','www','com','html','news','blog'}
    return set(re.findall(r'[a-zA-ZäöüÄÖÜéèàêâîôûç0-9]{3,}', (text or '').lower())) - STOPWORDS


def keyword_overlap(a: str, b: str) -> float:
    """Return overlap ratio between two strings based on normalized word sets."""
    words_a = token_set(a)
    words_b = token_set(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / max(1, min(len(words_a), len(words_b)))


def build_similarity_query(input_text: str, input_url: str) -> str:
    page_title = extract_page_title(input_url) if input_url else ''
    parsed = urlparse(input_url or '')
    url_bits = ' '.join(filter(None, [parsed.netloc, parsed.path.replace('-', ' ').replace('_', ' ')]))
    return ' '.join(filter(None, [input_text, input_url, page_title, url_bits]))


def find_exact_url_match(input_url: str) -> list:
    """Find open entries with the exact same source URL before creating a new item."""
    if not input_url:
        return []

    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, sources, input_url, status FROM kb_entries WHERE status IN ('backlog', 'working on')"
    ).fetchall()
    conn.close()

    matches = []
    for row in rows:
        sources = []
        try:
            sources = json.loads(row['sources'] or '[]')
        except Exception:
            sources = []

        if input_url == (row['input_url'] or '') or input_url in sources:
            matches.append({
                'id': row['id'],
                'title': row['title'],
                'status': row['status'],
                'score': 1.0,
                'url': f'https://gu.kuble.com/kb/#id-{row["id"]}'
            })

    return matches[:5]


def find_similar_open_entries(topic: str, input_url: str = '') -> list:
    """Find similar entries in backlog or working on before creating a new item."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, summary, sources, input_url, status FROM kb_entries WHERE status IN ('backlog', 'working on')"
    ).fetchall()
    conn.close()

    matches = []
    for row in rows:
        exact_url = bool(input_url and (input_url == (row['input_url'] or '') or input_url in (row['sources'] or '')))
        score = 1.0 if exact_url else max(
            keyword_overlap(topic, row['title']),
            keyword_overlap(topic, row['summary']),
            keyword_overlap(topic, f"{row['input_url'] or ''} {row['sources'] or ''}")
        )
        if score >= 0.28:
            matches.append({
                'id': row['id'],
                'title': row['title'],
                'status': row['status'],
                'score': score,
                'url': f'https://gu.kuble.com/kb/#id-{row["id"]}'
            })

    matches.sort(key=lambda x: x['score'], reverse=True)
    return matches[:5]


def find_similar_backlog(topic: str) -> list:
    """Backward-compatible alias for older callers."""
    return [m for m in find_similar_open_entries(topic) if m.get('status') == 'backlog'][:3]


def find_duplicates(title: str, summary: str) -> list:
    """Check for similar entries in done/ignored status."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, status FROM kb_entries WHERE status IN ('done', 'ignored')"
    ).fetchall()
    conn.close()

    duplicates = []
    for row in rows:
        score = keyword_overlap(title, row['title'])
        if score >= 0.4:
            duplicates.append({'id': row['id'], 'title': row['title'], 'status': row['status']})

    return duplicates


def perplexity_merge(existing: dict, new_input: str, new_url: str = None) -> dict:
    """Merge/update an existing entry with new information."""
    query = f"""Bestehender Eintrag zum Thema '{existing['title']}':

AKTUELLE ZUSAMMENFASSUNG:
{existing['summary']}

AKTUELLER BERICHT (Auszug):
{existing['full_report'][:1500]}

BISHERIGE QUELLEN:
{existing.get('sources', '[]')}

NEUE INFORMATION / NEUER LINK:
{new_url or ''} {new_input}

Aufgabe: Aktualisiere und erweitere diesen Wissenseintrag mit den neuen Informationen. 
Integriere das Neue nahtlos in den bestehenden Bericht und mache daraus einen vollständigen, sauberen Eintrag.
Berücksichtige bisherige und neue Quellen. Gib in "sources" alle relevanten Quellen zurück.
Markiere neue Erkenntnisse mit [NEU].
Schreibe auf Schweizer Hochdeutsch (kein ß, immer ss)."""

    return perplexity_research(query, new_url)


def add_to_brain(entry_id: int, title: str, summary: str, category: str, relevance: str, full_report: str = '', input_url: str = ''):
    """Push KB entry to Brain Wiki as a source via HTTP API."""
    content = f"# {title}\n\n{summary}"
    if full_report:
        content += f"\n\n{full_report}"
    if input_url:
        content += f"\n\nQuelle: {input_url}"

    cat_tag = category.lower().replace(" ", "_").replace("/", "_")
    try:
        import requests as req
        req.post('http://localhost:8088/api/entries', json={
            'content': content,
            'category': cat_tag,
            'tags': f'kb,kb-{entry_id},{cat_tag}',
            'source': 'kb'
        }, timeout=15)
    except Exception as e:
        print(f"Brain sync error: {e}")


@app.route('/kb/api/send-to-brain/<int:entry_id>', methods=['POST'])
def send_to_brain(entry_id):
    """Manual send of a KB entry to Brain."""
    conn = get_db()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    entry = dict(row)
    add_to_brain(entry['id'], entry['title'], entry['summary'], entry['category'],
                 entry.get('relevance', ''), entry.get('full_report', ''), entry.get('input_url', ''))
    return jsonify({'ok': True})


@app.route('/kb/api/brain-related/<int:entry_id>', methods=['GET'])
def brain_related(entry_id):
    """Find related Brain wiki articles for a KB entry."""
    conn = get_db()
    row = conn.execute('SELECT title, summary FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'articles': []})
    query = f"{row['title']} {row['summary'][:200]}"
    try:
        import requests as req
        res = req.get(f'http://localhost:8088/brain/api/search', params={'q': query, 'scope': 'articles', 'limit': 5}, timeout=10)
        data = res.json()
        return jsonify({'articles': data.get('results', [])})
    except Exception:
        return jsonify({'articles': []})


# ============ AUTH ============

@app.route('/kb/api/auth', methods=['POST'])
def auth():
    # Password protection removed: the board is open. Kept for backwards
    # compatibility so any cached frontend that still calls it keeps working.
    return jsonify({'ok': True})


# ============ ENTRIES ============

@app.route('/kb/api/entries', methods=['GET'])
def get_entries():
    """Return a lightweight list for the board. Full reports are loaded on demand."""
    conn = get_db()
    rows = conn.execute('''
        SELECT id, title, summary, category, relevance_score, status,
               presentation_id, tags, notes, created_at, updated_at
        FROM kb_entries
        ORDER BY created_at DESC
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/kb/api/entries/<int:entry_id>', methods=['GET'])
def get_entry(entry_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))


@app.route('/kb/api/entries/<int:entry_id>', methods=['PATCH'])
def update_entry(entry_id):
    """Edit the core content of an existing KB entry."""
    data = request.json or {}
    allowed_fields = {
        'title': lambda v: str(v).strip()[:300],
        'summary': lambda v: str(v).strip()[:5000],
        'full_report': lambda v: str(v).strip()[:80000],
        'category': lambda v: str(v).strip() if str(v).strip() in CATEGORIES else 'General',
        'relevance': lambda v: str(v).strip()[:5000],
        'input_text': lambda v: str(v).strip()[:10000],
        'input_url': lambda v: str(v).strip()[:2000],
    }

    updates = {}
    for field, cleaner in allowed_fields.items():
        if field in data:
            updates[field] = cleaner(data.get(field, ''))

    if 'relevance_score' in data:
        try:
            updates['relevance_score'] = max(1, min(5, int(data.get('relevance_score', 3))))
        except Exception:
            updates['relevance_score'] = 3

    if 'sources' in data:
        sources = data.get('sources') or []
        if isinstance(sources, str):
            sources = [s.strip() for s in re.split(r'[\n,]+', sources) if s.strip()]
        if not isinstance(sources, list):
            sources = []
        cleaned_sources = []
        seen = set()
        for src in sources:
            src = str(src).strip()
            if not src or src in seen:
                continue
            seen.add(src)
            cleaned_sources.append(src[:2000])
        updates['sources'] = json.dumps(cleaned_sources[:30])

    if 'tags' in data:
        updates['tags'] = json.dumps(normalize_tags(data.get('tags', [])))

    if not updates:
        return jsonify({'error': 'No editable fields provided'}), 400
    if 'title' in updates and not updates['title']:
        return jsonify({'error': 'Title darf nicht leer sein'}), 400

    updates['updated_at'] = datetime.utcnow().isoformat()
    assignments = ', '.join([f'{field} = ?' for field in updates.keys()])
    values = list(updates.values()) + [entry_id]

    conn = get_db()
    exists = conn.execute('SELECT id FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    if not exists:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    conn.execute(f'UPDATE kb_entries SET {assignments} WHERE id = ?', values)
    conn.commit()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'entry': dict(row)})


@app.route('/kb/api/entries', methods=['POST'])
def create_entry():
    """Create entry from raw input (text/url). Runs Perplexity research."""
    data = request.json
    input_text = data.get('input_text', '').strip()
    input_url = data.get('input_url', '').strip()
    manual = data.get('manual', False)

    if not input_text and not input_url:
        return jsonify({'error': 'Need input_text or input_url'}), 400

    if manual:
        # Direct entry without research. Score stays NULL when omitted so minimal
        # entries can be distinguished from explicitly-rated ones in the UI.
        if 'relevance_score' in data and data.get('relevance_score') not in (None, ''):
            try:
                manual_score = max(1, min(5, int(data.get('relevance_score'))))
            except Exception:
                manual_score = None
        else:
            manual_score = None
        now = datetime.utcnow().isoformat()
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO kb_entries
            (title, summary, full_report, sources, category, relevance, relevance_score,
             tags, status, input_text, input_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?)
        ''', (
            data.get('title', input_text[:80]),
            data.get('summary', ''),
            data.get('full_report', ''),
            json.dumps(data.get('sources', [])),
            data.get('category', 'General'),
            data.get('relevance', ''),
            manual_score,
            json.dumps(normalize_tags(data.get('tags', []))),
            input_text, input_url, now, now
        ))
        entry_id = cur.lastrowid
        conn.commit()
        conn.close()
        add_to_brain(entry_id, data.get('title', ''), data.get('summary', ''),
                     data.get('category', 'General'), data.get('relevance', ''),
                     data.get('full_report', ''), data.get('input_url', ''))
        return jsonify({
            'ok': True,
            'id': entry_id,
            'url': f'https://gu.kuble.com/kb/#id-{entry_id}'
        })

    # === EXACT URL DUPLICATE CHECK (before expensive research) ===
    merge_target_id = data.get('merge_with')  # explicit merge request from frontend

    # If frontend confirmed a merge, load existing entry
    merge_existing = None
    if merge_target_id:
        conn = get_db()
        row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (merge_target_id,)).fetchone()
        conn.close()
        if row:
            merge_existing = dict(row)

    # Only block when the exact same source URL already exists on an open entry.
    # Topic/title similarity should not gate creation.
    if input_url and not merge_target_id and not data.get('force_new'):
        same_url = find_exact_url_match(input_url)
        if same_url:
            return jsonify({
                'action': 'merge_candidates',
                'similar_open': same_url,
                'similar_backlog': same_url,  # backward-compatible frontend key
                'message': 'Eintrag mit identischer URL existiert bereits. Mergen oder neu erstellen?'
            }), 200

    # Run Perplexity research (merge or fresh)
    try:
        if merge_existing:
            report = perplexity_merge(merge_existing, input_text, input_url if input_url else None)
        else:
            report = perplexity_research(input_text, input_url if input_url else None)
    except Exception as e:
        return jsonify({'error': f'Research failed: {str(e)}'}), 500

    # If merging: update existing entry instead of creating new
    if merge_existing:
        now = datetime.utcnow().isoformat()
        existing_sources = json.loads(merge_existing.get('sources', '[]'))
        new_sources = list(set(existing_sources + report.get('sources', [])))
        # Ensure new input_url is included at the front if provided
        if input_url and input_url not in new_sources:
            new_sources = [input_url] + new_sources
        old_tags = normalize_tags(json.loads(merge_existing.get('tags') or '[]'))
        merged_tags = normalize_tags(old_tags + normalize_tags(data.get('tags', [])))
        conn = get_db()
        conn.execute('''
            UPDATE kb_entries SET
              title = ?, summary = ?, full_report = ?, sources = ?,
              category = ?, relevance = ?, relevance_score = ?, tags = ?,
              input_text = ?, input_url = ?, updated_at = ?
            WHERE id = ?
        ''', (
            report['title'],
            report['summary'],
            report['full_report'],
            json.dumps(new_sources),
            report.get('category', merge_existing['category']),
            report.get('relevance', merge_existing['relevance']),
            report.get('relevance_score', merge_existing['relevance_score']),
            json.dumps(merged_tags),
            f"{merge_existing.get('input_text','')} | {input_text}",
            input_url or merge_existing.get('input_url', ''),
            now,
            merge_target_id
        ))
        conn.commit()
        conn.close()
        return jsonify({
            'ok': True,
            'merged': True,
            'id': merge_target_id,
            'title': report['title'],
            'url': f'https://gu.kuble.com/kb/#id-{merge_target_id}'
        })

    # Check done/ignored duplicates
    duplicates = find_duplicates(report['title'], report['summary'])

    # Always include input_url as the first source if provided
    all_sources = report.get('sources', [])
    if input_url and input_url not in all_sources:
        all_sources = [input_url] + all_sources

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.execute('''
        INSERT INTO kb_entries
        (title, summary, full_report, sources, category, relevance, relevance_score,
         tags, status, input_text, input_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?)
    ''', (
        report['title'],
        report['summary'],
        report['full_report'],
        json.dumps(all_sources),
        report.get('category', 'General'),
        report.get('relevance', ''),
        report.get('relevance_score', 3),
        json.dumps(normalize_tags(data.get('tags', []))),
        input_text, input_url, now, now
    ))
    entry_id = cur.lastrowid
    conn.commit()
    conn.close()

    add_to_brain(entry_id, report['title'], report['summary'],
                 report.get('category', 'General'), report.get('relevance', ''),
                 report.get('full_report', ''), report.get('input_url', ''))

    return jsonify({
        'ok': True,
        'id': entry_id,
        'title': report['title'],
        'category': report['category'],
        'relevance_score': report.get('relevance_score', 3),
        'duplicates': duplicates,
        'url': f'https://gu.kuble.com/kb/#id-{entry_id}'
    })


@app.route('/kb/api/entries/<int:entry_id>/status', methods=['PATCH'])
def update_status(entry_id):
    data = request.json
    status = data.get('status')
    valid = ['backlog', 'working on', 'done', 'ignored']
    if status not in valid:
        return jsonify({'error': f'Invalid status. Use: {valid}'}), 400

    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('UPDATE kb_entries SET status = ?, updated_at = ? WHERE id = ?',
                 (status, now, entry_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/kb/api/entries/<int:entry_id>/tags', methods=['PATCH'])
def update_tags(entry_id):
    data = request.json or {}
    tags = normalize_tags(data.get('tags', []))
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('UPDATE kb_entries SET tags = ?, updated_at = ? WHERE id = ?',
                 (json.dumps(tags), now, entry_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'tags': tags})


@app.route('/kb/api/entries/<int:entry_id>/notes', methods=['PATCH'])
def update_notes(entry_id):
    data = request.json or {}
    notes = str(data.get('notes', ''))[:20000]
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('UPDATE kb_entries SET notes = ?, updated_at = ? WHERE id = ?',
                 (notes, now, entry_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'notes': notes})


@app.route('/kb/api/entries/<int:entry_id>', methods=['DELETE'])
def delete_entry(entry_id):
    conn = get_db()
    conn.execute('DELETE FROM kb_entries WHERE id = ?', (entry_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ============ IMAGES ============

# ============ PRESENTATIONS (old routes removed — new ones below) ============

@app.route('/kb/api/presentations', methods=['GET'])
def get_presentations():
    conn = get_db()
    rows = conn.execute('SELECT * FROM kb_presentations ORDER BY created_at DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/kb/api/presentations', methods=['POST'])
def create_presentation():
    data = request.json
    name = data.get('name', '').strip()
    gamma_url = data.get('gamma_url', '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    # Extract gamma_id from URL if provided
    gamma_id = None
    if gamma_url:
        # e.g. https://gamma.app/docs/abc123xyz -> abc123xyz
        m = re.search(r'/docs/([a-zA-Z0-9]+)', gamma_url)
        if m:
            gamma_id = m.group(1)
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO kb_presentations (name, gamma_id, gamma_url, created_at) VALUES (?, ?, ?, ?)',
        (name, gamma_id, gamma_url, now)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': cur.lastrowid})

@app.route('/kb/api/presentations/<int:pres_id>', methods=['DELETE'])
def delete_presentation(pres_id):
    conn = get_db()
    conn.execute('DELETE FROM kb_presentations WHERE id = ?', (pres_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ============ IMAGE SEARCH ============

def scrape_article_images(url: str) -> list:
    """Scrape images from article URL (og:image + all img tags)."""
    images = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

        # OG image first
        og = soup.find('meta', property='og:image')
        if og and og.get('content'):
            images.append({'url': og['content'], 'source': 'article_og', 'title': 'OG Image'})

        # Twitter card image
        tw = soup.find('meta', attrs={'name': 'twitter:image'})
        if tw and tw.get('content'):
            img_url = tw['content']
            if img_url not in [i['url'] for i in images]:
                images.append({'url': img_url, 'source': 'article_twitter', 'title': 'Twitter Card'})

        # Article body images (filter out tiny icons etc.)
        for img in soup.find_all('img', src=True):
            src = img['src']
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = base + src
            elif not src.startswith('http'):
                continue
            # Skip SVG, data URIs, tracking pixels
            if any(x in src for x in ['data:', '.svg', 'pixel', 'tracking', 'beacon', 'avatar', 'logo', 'icon']):
                continue
            w = img.get('width', '')
            h = img.get('height', '')
            try:
                if w and int(str(w).replace('px','')) < 100:
                    continue
                if h and int(str(h).replace('px','')) < 100:
                    continue
            except Exception:
                pass
            alt = img.get('alt', '') or img.get('title', '') or 'Artikelbild'
            if src not in [i['url'] for i in images]:
                images.append({'url': src, 'source': 'article', 'title': alt})
            if len(images) >= 6:
                break
    except Exception as e:
        pass
    return images


def search_web_images(query: str, max_results: int = 5) -> list:
    """Search images via DDGS (DuckDuckGo). Falls back gracefully on rate limits."""
    images = []
    if not HAS_DDGS:
        return images
    try:
        with DDGS_NEW() as ddgs:
            results = list(ddgs.images(query, max_results=max_results))
        for r in results:
            url = r.get('image', '')
            thumb = r.get('thumbnail', '')
            if not url and not thumb:
                continue
            # Prefer thumbnail for display (Bing CDN = reliable), full url for slide
            images.append({
                'url': url or thumb,
                'thumbnail': thumb or url,
                'source': 'web_search',
                'title': r.get('title', query),
                'source_url': r.get('url', '')
            })
    except Exception as e:
        # Rate limit or other error — try Bing image search as fallback
        try:
            images = bing_image_search(query, max_results)
        except Exception:
            pass
    return images


def bing_image_search(query: str, max_results: int = 5) -> list:
    """Fallback: scrape Bing image search results."""
    images = []
    try:
        params = {'q': query, 'form': 'HDRSC2', 'first': '1', 'count': str(max_results * 2)}
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        resp = requests.get('https://www.bing.com/images/search', params=params, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for item in soup.find_all('a', class_='iusc')[:max_results]:
            m_attr = item.get('m', '')
            if not m_attr:
                continue
            try:
                m = json.loads(m_attr)
                url = m.get('murl', '')
                thumb = m.get('turl', url)
                title = m.get('t', query)
                if url:
                    images.append({
                        'url': url,
                        'thumbnail': thumb,
                        'source': 'bing_search',
                        'title': title,
                    })
            except Exception:
                continue
    except Exception:
        pass
    return images


@app.route('/kb/api/image-proxy')
def image_proxy():
    """Proxy external images to bypass hotlink protection."""
    url = request.args.get('url', '')
    if not url or not url.startswith('http'):
        return jsonify({'error': 'invalid url'}), 400
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': urlparse(url).scheme + '://' + urlparse(url).netloc + '/',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        }
        resp = requests.get(url, headers=headers, timeout=10, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        from flask import Response
        return Response(resp.content, content_type=content_type,
                        headers={'Cache-Control': 'public, max-age=3600'})
    except Exception as e:
        # Return a 1x1 transparent pixel on error
        import base64
        pixel = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
        from flask import Response
        return Response(pixel, content_type='image/png', status=200)


@app.route('/kb/api/entries/<int:entry_id>/find-images', methods=['POST'])
def find_images(entry_id):
    """Find images for a KB entry: scrape article + web search. Accepts optional custom query."""
    conn = get_db()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    entry = dict(row)

    data = request.json or {}
    custom_query = data.get('query', '').strip()
    search_query = custom_query if custom_query else entry['title']

    images = []

    # 1. Scrape article images if URL available (only when no custom query)
    if not custom_query and entry.get('input_url'):
        article_imgs = scrape_article_images(entry['input_url'])
        images.extend(article_imgs)

    # 2. Web search
    remaining = max(0, 10 - len(images))
    if remaining > 0:
        web_imgs = search_web_images(search_query, max_results=remaining)
        existing_urls = {i['url'] for i in images}
        for img in web_imgs:
            if img['url'] not in existing_urls:
                images.append(img)
                existing_urls.add(img['url'])

    return jsonify({'images': images[:10]})


@app.route('/kb/api/entries/<int:entry_id>/presentation', methods=['PATCH'])
def set_presentation(entry_id):
    """Assign or remove a presentation from a KB entry."""
    data = request.json
    pres_id = data.get('presentation_id')  # None/null to unassign
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        'UPDATE kb_entries SET presentation_id = ?, updated_at = ? WHERE id = ?',
        (pres_id, now, entry_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/kb/api/entries/<int:entry_id>/select-image', methods=['PATCH'])
def select_image(entry_id):
    """Save the selected image URL for this entry."""
    data = request.json
    image_url = data.get('image_url', '')
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        'UPDATE kb_entries SET selected_image = ?, updated_at = ? WHERE id = ?',
        (image_url, now, entry_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ============ GAMMA SLIDE GENERATION ============

@app.route('/kb/api/entries/<int:entry_id>/generate-slide', methods=['POST'])
def generate_slide(entry_id):
    """Generate a Gamma presentation from this KB entry."""
    conn = get_db()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    entry = dict(row)

    data = request.json or {}
    selected_image = data.get('selected_image') or entry.get('selected_image')
    pres_name = data.get('presentation_name', '')

    # Presentation title = "Pres Name - Entry Title"
    pres_title = f"{pres_name} - {entry['title']}" if pres_name else entry['title']

    # 3 slides: Title + 2x Content
    # Slide 1: Title slide (no image)
    slide1 = f"# {pres_title}"

    # Slide 2: Image + Summary
    img_line = f"![Bild]({selected_image})\n\n" if selected_image else ""
    slide2 = f"{img_line}{entry['summary']}"

    # Slide 3: Key content from report
    report_short = entry['full_report'][:600].strip()
    slide3 = report_short

    input_text = f"{slide1}\n\n---\n\n{slide2}\n\n---\n\n{slide3}"

    # Image options: if we have a selected image, tell Gamma to use only that
    image_opts = {"source": "webAllImages"} if selected_image else {"source": "pexels"}

    payload = {
        "inputText": input_text,
        "textMode": "preserve",
        "format": "presentation",
        "numCards": 3,
        "cardSplit": "inputTextBreaks",
        "additionalInstructions": "Use the provided image URL on slide 2. Slide 1 is the title slide. Slides 2 and 3 are content slides. Keep the image as-is, do not replace it.",
        "textOptions": {
            "amount": "brief",
            "language": "de"
        },
        "imageOptions": image_opts,
        "cardOptions": {
            "dimensions": "16x9"
        }
    }

    try:
        resp = requests.post(
            f"{GAMMA_API_URL}/generations",
            headers={"X-API-KEY": GAMMA_API_KEY, "Content-Type": "application/json"},
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        gen_data = resp.json()
        gen_id = gen_data.get('generationId') or gen_data.get('id')
    except Exception as e:
        return jsonify({'error': f'Gamma API error: {str(e)}'}), 500

    if not gen_id:
        return jsonify({'error': 'No generation ID returned', 'raw': gen_data}), 500

    # Poll for completion (up to 90s)
    gamma_url = None
    for _ in range(30):
        import time
        time.sleep(3)
        try:
            poll = requests.get(
                f"{GAMMA_API_URL}/generations/{gen_id}",
                headers={"X-API-KEY": GAMMA_API_KEY},
                timeout=10
            )
            poll_data = poll.json()
            status = poll_data.get('status')
            if status == 'completed':
                gamma_url = poll_data.get('gammaUrl') or poll_data.get('resultUrl') or poll_data.get('url')
                break
            elif status == 'failed':
                return jsonify({'error': 'Gamma generation failed', 'raw': poll_data}), 500
        except Exception:
            pass

    if not gamma_url:
        # Return gen_id so frontend can poll
        return jsonify({'ok': True, 'gen_id': gen_id, 'status': 'pending',
                        'message': 'Generation started — check back shortly'})

    # Save gamma_url + presentation_id to entry
    # Find presentation_id by name if provided
    pres_id_to_set = None
    if pres_name:
        conn2 = get_db()
        prow = conn2.execute(
            'SELECT id FROM kb_presentations WHERE name = ? LIMIT 1', (pres_name,)
        ).fetchone()
        conn2.close()
        if prow:
            pres_id_to_set = prow['id']

    now = datetime.utcnow().isoformat()
    conn = get_db()
    if pres_id_to_set:
        conn.execute(
            'UPDATE kb_entries SET gamma_url = ?, presentation_id = ?, updated_at = ? WHERE id = ?',
            (gamma_url, pres_id_to_set, now, entry_id)
        )
    else:
        conn.execute('UPDATE kb_entries SET gamma_url = ?, updated_at = ? WHERE id = ?',
                     (gamma_url, now, entry_id))
    conn.commit()
    conn.close()

    return jsonify({'ok': True, 'gamma_url': gamma_url, 'gen_id': gen_id})


@app.route('/kb/api/generations/<gen_id>/status', methods=['GET'])
def get_generation_status(gen_id):
    """Poll Gamma generation status."""
    try:
        resp = requests.get(
            f"{GAMMA_API_URL}/generations/{gen_id}",
            headers={"X-API-KEY": GAMMA_API_KEY},
            timeout=10
        )
        data = resp.json()
        gamma_url = data.get('gammaUrl') or data.get('resultUrl') or data.get('url')
        return jsonify({'status': data.get('status'), 'gamma_url': gamma_url, 'raw': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============ AGENT API (token-protected, for external coding agents) ============
#
# All endpoints under /kb/api/agent/* are intended for outside agents like
# Claude Code. Authentication is a Bearer token from KB_AGENT_API_TOKEN.
# The existing UI and /kb/api/* routes above are untouched.

KB_AGENT_TOKEN_ENV = 'KB_AGENT_API_TOKEN'
VALID_STATUSES = ['backlog', 'working on', 'done', 'ignored']


def get_agent_token():
    """Return the configured agent token, or None if the API is disabled."""
    token = os.environ.get(KB_AGENT_TOKEN_ENV)
    return token.strip() if token else None


def extract_request_token():
    """Read the token from Authorization: Bearer or X-KB-Agent-Token."""
    auth = request.headers.get('Authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return (request.headers.get('X-KB-Agent-Token') or '').strip()


def require_agent_token(fn):
    """Gate an endpoint behind the agent token. 503 if token env var is missing."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        expected = get_agent_token()
        if not expected:
            return jsonify({
                'error': 'Agent API disabled',
                'code': 'agent_api_disabled',
                'detail': f'Setze die Umgebungsvariable {KB_AGENT_TOKEN_ENV}, dann sind die Endpunkte aktiv.'
            }), 503
        provided = extract_request_token()
        if not provided:
            return jsonify({
                'error': 'Missing token',
                'code': 'missing_token',
                'detail': 'Authorization: Bearer <token> oder X-KB-Agent-Token Header setzen.'
            }), 401
        if not hmac.compare_digest(provided, expected):
            return jsonify({
                'error': 'Invalid token',
                'code': 'invalid_token'
            }), 403
        return fn(*args, **kwargs)
    return wrapper


def _parse_json_field(raw, default):
    """JSON-parse a stored field, fall back to default if it cannot be parsed."""
    if raw is None or raw == '':
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def serialize_entry(row, include_full_report=True):
    """Turn a SQLite row into a clean JSON-friendly dict for agents."""
    if row is None:
        return None
    d = dict(row)
    d['sources'] = _parse_json_field(d.get('sources'), [])
    d['tags'] = _parse_json_field(d.get('tags'), [])
    d['images'] = _parse_json_field(d.get('images'), [])
    if not include_full_report:
        d.pop('full_report', None)
    return d


def parse_agent_json_body():
    """Accept a JSON body and return (data_dict, error_response_or_None)."""
    data = request.get_json(silent=True, force=True)
    if data is None:
        if request.method in ('POST', 'PATCH') and (request.data or '').strip():
            return None, (jsonify({'error': 'Request body must be valid JSON.'}), 400)
        data = {}
    if not isinstance(data, dict):
        return None, (jsonify({'error': 'JSON body must be an object.'}), 400)
    return data, None


def _truthy(val):
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on')


@app.route('/kb/api/agent/health', methods=['GET'])
def agent_health():
    """Liveness check. Always responds, never requires the token."""
    return jsonify({
        'ok': True,
        'service': 'kb-agent-api',
        'token_configured': bool(get_agent_token()),
        'time': datetime.utcnow().isoformat() + 'Z'
    })


PUBLIC_BASE_URL = os.environ.get('KB_PUBLIC_BASE_URL', 'https://gu.kuble.com/kb')


def _rfc822(iso_str):
    """Convert a stored ISO timestamp to an RFC-822 date for RSS pubDate."""
    if not iso_str:
        return format_datetime(datetime.now(timezone.utc))
    try:
        s = str(iso_str).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt)
    except Exception:
        return format_datetime(datetime.now(timezone.utc))


def build_rss(entries, title, description, self_url):
    """Render a list of entry rows as an RSS 2.0 feed (public, no token)."""
    now = format_datetime(datetime.now(timezone.utc))
    items = []
    for row in entries:
        e = serialize_entry(row, include_full_report=False)
        link = f"{PUBLIC_BASE_URL}/#id-{e['id']}"
        tags = e.get('tags') or []
        # Prefer the summary; fall back to the relevance note.
        body = (e.get('summary') or e.get('relevance') or '').strip()
        cats = ''.join(
            f"<category>{xml_escape(str(t))}</category>" for t in ([e.get('category')] + list(tags)) if t
        )
        items.append(
            "<item>"
            f"<title>{xml_escape(str(e.get('title') or 'Untitled'))}</title>"
            f"<link>{xml_escape(link)}</link>"
            f"<guid isPermaLink=\"false\">kb-entry-{e['id']}</guid>"
            f"<pubDate>{_rfc822(e.get('created_at'))}</pubDate>"
            f"<description>{xml_escape(body)}</description>"
            f"{cats}"
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '<channel>'
        f"<title>{xml_escape(title)}</title>"
        f"<link>{xml_escape(PUBLIC_BASE_URL + '/')}</link>"
        f"<description>{xml_escape(description)}</description>"
        "<language>de-CH</language>"
        f"<lastBuildDate>{now}</lastBuildDate>"
        f"<atom:link href=\"{xml_escape(self_url)}\" rel=\"self\" type=\"application/rss+xml\"/>"
        + ''.join(items) +
        "</channel></rss>"
    )
    return Response(xml, mimetype='application/rss+xml; charset=utf-8')


def _rss_query(args):
    """Shared filter parsing for the public RSS feeds."""
    status = (args.get('status') or '').strip()
    category = (args.get('category') or '').strip()
    tag = (args.get('tag') or '').strip().lower()
    q = (args.get('q') or '').strip()
    try:
        limit = int(args.get('limit', 50))
    except Exception:
        limit = 50
    limit = max(1, min(200, limit))

    where, params = [], []
    if status in VALID_STATUSES:
        where.append('status = ?')
        params.append(status)
    if category in CATEGORIES:
        where.append('category = ?')
        params.append(category)
    if q:
        where.append('(title LIKE ? OR summary LIKE ? OR full_report LIKE ?)')
        like = f'%{q}%'
        params.extend([like, like, like])

    sql = 'SELECT * FROM kb_entries'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    # Fetch a bit extra so tag filtering can still fill the feed.
    sql += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit if not tag else min(200, limit * 4))

    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if tag:
        rows = [r for r in rows if tag in [str(t).lower() for t in _parse_json_field(dict(r).get('tags'), [])]]
        rows = rows[:limit]
    return rows


@app.route('/kb/api/rss', methods=['GET'])
@app.route('/kb/rss.xml', methods=['GET'])
@app.route('/kb/feed.xml', methods=['GET'])
def public_rss():
    """Public RSS feed of KB entries for agents and readers. No token required.

    Optional query params: status, category, tag, q (search), limit (1-200).
    """
    rows = _rss_query(request.args)
    parts = []
    for key in ('status', 'category', 'tag', 'q'):
        val = (request.args.get(key) or '').strip()
        if val:
            parts.append(f'{key}={val}')
    suffix = (' · ' + ', '.join(parts)) if parts else ''
    self_url = f"{PUBLIC_BASE_URL}/api/rss"
    return build_rss(
        rows,
        title='Knowledge Bridge' + suffix,
        description='KI-Wissensbasis – neueste Eintraege' + suffix,
        self_url=self_url,
    )


AGENT_DOCS_MD = """# Knowledge Bridge Agent API

Token-geschuetzte HTTPS-API fuer externe Coding-Agenten (z.B. Claude Code),
um KB-Eintraege zu listen, durchsuchen, lesen, anlegen, bearbeiten und loeschen.

**Basis-URL:** `https://gu.kuble.com/kb/api/agent`

## Authentifizierung

Setze auf dem Server die Umgebungsvariable `KB_AGENT_API_TOKEN`. Solange sie
fehlt, geben alle Endpunkte ausser `/health`, `/docs` und `/openapi` einen
`503 agent_api_disabled` zurueck.

Der Token wird via Bearer-Header *oder* `X-KB-Agent-Token` mitgeschickt:

```
Authorization: Bearer $KB_AGENT_API_TOKEN
# oder
X-KB-Agent-Token: $KB_AGENT_API_TOKEN
```

## Endpunkte

| Methode | Pfad | Zweck |
| --- | --- | --- |
| GET    | `/health` | Liveness, kein Token noetig |
| GET    | `/docs` | Diese Markdown-Doku |
| GET    | `/openapi` | Schema-Beschreibung als JSON |
| GET    | `/entries` | Liste mit Filtern |
| GET    | `/entries/<id>` | Einzelner Eintrag mit Full-Report |
| POST   | `/entries` | Anlegen, automatisch oder manuell |
| PATCH  | `/entries/<id>` | Felder aendern (inkl. status, tags, notes) |
| DELETE | `/entries/<id>` | Eintrag loeschen |

## Oeffentlicher RSS-Feed (ohne Token)

Fuer Agenten und Feed-Reader gibt es einen oeffentlichen RSS-2.0-Feed der
neuesten Eintraege. **Kein Token noetig.**

```
https://gu.kuble.com/kb/api/rss      (auch /kb/rss.xml, /kb/feed.xml)
```

Optionale Query-Parameter (kombinierbar):

- `q` Volltext in title/summary/full_report
- `status` einer von `backlog`, `working on`, `done`, `ignored`
- `category` exakte Kategorie aus der erlaubten Liste
- `tag` exakter Tag (case-insensitive)
- `limit` 1..200 (Default 50)

```bash
curl 'https://gu.kuble.com/kb/api/rss?status=backlog&tag=agents&limit=20'
```

### GET /entries

Query-Parameter:

- `q` Volltext in title/summary/full_report (LIKE %q%)
- `status` einer von `backlog`, `working on`, `done`, `ignored`
- `category` exakte Kategorie aus der erlaubten Liste
- `tag` exakter Tag (case-insensitive)
- `limit` 1..200 (Default 50)
- `offset` >= 0
- `include_full_report` `true` um Full-Report mitzuliefern, sonst nur Meta+Summary

```bash
curl -H "Authorization: Bearer $KB_AGENT_API_TOKEN" \\
  'https://gu.kuble.com/kb/api/agent/entries?q=agents&status=backlog&limit=10'
```

### GET /entries/<id>

```bash
curl -H "Authorization: Bearer $KB_AGENT_API_TOKEN" \\
  https://gu.kuble.com/kb/api/agent/entries/42
```

### POST /entries

Zwei Modi:

**Automatisch** (Default) - die KB recherchiert das Thema selbst:

```bash
curl -X POST https://gu.kuble.com/kb/api/agent/entries \\
  -H "Authorization: Bearer $KB_AGENT_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"input_text":"Anthropic launches new Claude model","input_url":"https://example.com/article"}'
```

Antwort kann `action: merge_candidates` enthalten, wenn die URL schon existiert.
Dann entweder `force_new: true` oder `merge_with: <id>` mitschicken.

**Manuell** - du lieferst die fertigen Felder:

```bash
curl -X POST https://gu.kuble.com/kb/api/agent/entries \\
  -H "Authorization: Bearer $KB_AGENT_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "manual": true,
    "title": "Neues KI-Modell xyz",
    "summary": "Kurzfassung in Schweizer Hochdeutsch.",
    "full_report": "## Hintergrund\\n\\nLanger Markdown-Bericht ...",
    "sources": ["https://example.com/a", "https://example.com/b"],
    "category": "LLM",
    "relevance": "Relevant fuer den KI-Monatsreport.",
    "relevance_score": 4,
    "tags": ["claude", "release"]
  }'
```

### PATCH /entries/<id>

Editierbare Felder: `title`, `summary`, `full_report`, `category`, `relevance`,
`relevance_score`, `input_text`, `input_url`, `sources`, `tags`, `notes`, `status`.

```bash
curl -X PATCH https://gu.kuble.com/kb/api/agent/entries/42 \\
  -H "Authorization: Bearer $KB_AGENT_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"status":"done","tags":["claude","review"]}'
```

### DELETE /entries/<id>

```bash
curl -X DELETE https://gu.kuble.com/kb/api/agent/entries/42 \\
  -H "Authorization: Bearer $KB_AGENT_API_TOKEN"
```

## Konventionen

- Texte in Schweizer Hochdeutsch (ss statt ss-Ligatur).
- Status-Werte: `backlog`, `working on`, `done`, `ignored`.
- Erlaubte Kategorien siehe `/openapi`.
- `sources`, `tags`, `images` werden in Antworten als richtige JSON-Arrays geliefert.
"""


@app.route('/kb/api/agent/docs', methods=['GET'])
def agent_docs():
    """Markdown docs with curl examples."""
    return Response(AGENT_DOCS_MD, content_type='text/markdown; charset=utf-8')


@app.route('/kb/api/agent/openapi', methods=['GET'])
def agent_openapi():
    """Schema-ish description of the agent API."""
    return jsonify({
        'service': 'kb-agent-api',
        'base_url': 'https://gu.kuble.com/kb/api/agent',
        'auth': {
            'type': 'bearer_token',
            'env_var': KB_AGENT_TOKEN_ENV,
            'headers': ['Authorization: Bearer ***', 'X-KB-Agent-Token: <token>'],
            'disabled_response': {'status': 503, 'code': 'agent_api_disabled'}
        },
        'categories': CATEGORIES,
        'statuses': VALID_STATUSES,
        'entry_schema': {
            'id': 'integer',
            'title': 'string (<=300)',
            'summary': 'string (<=5000)',
            'full_report': 'string markdown (<=80000)',
            'sources': 'array of url strings',
            'category': f'one of {CATEGORIES}',
            'relevance': 'string (<=5000)',
            'relevance_score': 'integer 1..5',
            'status': f'one of {VALID_STATUSES}',
            'input_text': 'string',
            'input_url': 'string',
            'tags': 'array of lowercase tag strings',
            'notes': 'string (<=20000)',
            'images': 'array',
            'selected_image': 'string|null',
            'gamma_url': 'string|null',
            'presentation_id': 'integer|null',
            'created_at': 'iso8601 string',
            'updated_at': 'iso8601 string'
        },
        'endpoints': [
            {'method': 'GET', 'path': '/health', 'auth': False, 'returns': 'health json'},
            {'method': 'GET', 'path': '/docs', 'auth': False, 'returns': 'markdown'},
            {'method': 'GET', 'path': '/openapi', 'auth': False, 'returns': 'this json'},
            {
                'method': 'GET',
                'path': '/entries',
                'auth': True,
                'query': {
                    'q': 'string, LIKE search across title/summary/full_report',
                    'status': f'one of {VALID_STATUSES}',
                    'category': f'one of {CATEGORIES}',
                    'tag': 'lowercase tag, exact match',
                    'limit': 'int 1..200, default 50',
                    'offset': 'int >=0, default 0',
                    'include_full_report': 'true|false, default false'
                },
                'returns': '{entries:[], count, limit, offset, total_filtered}'
            },
            {'method': 'GET', 'path': '/entries/<id>', 'auth': True, 'returns': 'entry object'},
            {
                'method': 'POST',
                'path': '/entries',
                'auth': True,
                'body_automatic': {
                    'input_text': 'string (topic or note)',
                    'input_url': 'string url, optional',
                    'force_new': 'bool, override duplicate URL check',
                    'merge_with': 'int id, merge into existing entry',
                    'tags': 'array of strings, optional'
                },
                'body_manual': {
                    'manual': 'true',
                    'title': 'string (required)',
                    'summary': 'string',
                    'full_report': 'string markdown',
                    'sources': 'array of url strings or comma string',
                    'category': f'one of {CATEGORIES}',
                    'relevance': 'string',
                    'relevance_score': 'int 1..5',
                    'tags': 'array of strings',
                    'input_text': 'string, optional',
                    'input_url': 'string, optional'
                },
                'duplicate_response': {
                    'status': 200,
                    'action': 'merge_candidates',
                    'similar_open': '[{id,title,status,score,url}]'
                }
            },
            {
                'method': 'PATCH',
                'path': '/entries/<id>',
                'auth': True,
                'body': {
                    'title': 'string',
                    'summary': 'string',
                    'full_report': 'string',
                    'category': f'one of {CATEGORIES}',
                    'relevance': 'string',
                    'relevance_score': 'int 1..5',
                    'input_text': 'string',
                    'input_url': 'string',
                    'sources': 'array or comma string',
                    'tags': 'array of strings',
                    'notes': 'string',
                    'status': f'one of {VALID_STATUSES}'
                }
            },
            {'method': 'DELETE', 'path': '/entries/<id>', 'auth': True, 'returns': '{ok, deleted_id}'}
        ]
    })


@app.route('/kb/api/agent/entries', methods=['GET'])
@require_agent_token
def agent_list_entries():
    """List entries with simple filters. Full report optional to keep payload small."""
    q = (request.args.get('q') or '').strip()
    status = (request.args.get('status') or '').strip()
    category = (request.args.get('category') or '').strip()
    tag = (request.args.get('tag') or '').strip().lower()
    include_full = _truthy(request.args.get('include_full_report', 'false'))

    try:
        limit = int(request.args.get('limit', 50))
    except Exception:
        limit = 50
    limit = max(1, min(200, limit))
    try:
        offset = int(request.args.get('offset', 0))
    except Exception:
        offset = 0
    offset = max(0, offset)

    where = []
    params = []
    if status:
        if status not in VALID_STATUSES:
            return jsonify({'error': f'Invalid status. Use one of {VALID_STATUSES}'}), 400
        where.append('status = ?')
        params.append(status)
    if category:
        if category not in CATEGORIES:
            return jsonify({'error': f'Invalid category. Use one of {CATEGORIES}'}), 400
        where.append('category = ?')
        params.append(category)
    if q:
        where.append('(title LIKE ? OR summary LIKE ? OR full_report LIKE ?)')
        like = f'%{q}%'
        params.extend([like, like, like])

    sql = 'SELECT * FROM kb_entries'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
    params.extend([limit, offset])

    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    entries = [serialize_entry(r, include_full_report=include_full) for r in rows]

    # Tag filter happens here because tags are stored as JSON strings.
    if tag:
        entries = [e for e in entries if tag in [str(t).lower() for t in (e.get('tags') or [])]]

    return jsonify({
        'entries': entries,
        'count': len(entries),
        'limit': limit,
        'offset': offset,
        'filters': {'q': q, 'status': status, 'category': category, 'tag': tag,
                    'include_full_report': include_full}
    })


@app.route('/kb/api/agent/entries/<int:entry_id>', methods=['GET'])
@require_agent_token
def agent_get_entry(entry_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found', 'id': entry_id}), 404
    return jsonify(serialize_entry(row, include_full_report=True))


def _normalize_source_list(raw):
    """Accept array or comma/newline string, return deduped, length-capped list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [s.strip() for s in re.split(r'[\n,]+', raw) if s.strip()]
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for src in raw:
        src = str(src).strip()
        if not src or src in seen:
            continue
        seen.add(src)
        out.append(src[:2000])
    return out[:30]


@app.route('/kb/api/agent/entries', methods=['POST'])
@require_agent_token
def agent_create_entry():
    """Create entry. manual=true skips research; otherwise reuses perplexity_research."""
    data, err = parse_agent_json_body()
    if err:
        return err

    manual = _truthy(data.get('manual', False)) or data.get('manual') is True
    input_text = str(data.get('input_text', '') or '').strip()
    input_url = str(data.get('input_url', '') or '').strip()

    # --------- Manual mode ---------
    if manual:
        title = str(data.get('title', '') or '').strip()
        if not title:
            return jsonify({'error': 'Manual create benoetigt einen title.'}), 400
        summary = str(data.get('summary', '') or '').strip()
        full_report = str(data.get('full_report', '') or '').strip()
        sources = _normalize_source_list(data.get('sources'))
        category = str(data.get('category', 'General') or 'General').strip()
        if category not in CATEGORIES:
            category = 'General'
        relevance = str(data.get('relevance', '') or '').strip()[:5000]
        # Score stays NULL when omitted; explicit values are clamped to 1..5.
        if 'relevance_score' in data and data.get('relevance_score') not in (None, ''):
            try:
                relevance_score = max(1, min(5, int(data.get('relevance_score'))))
            except Exception:
                relevance_score = None
        else:
            relevance_score = None
        tags = normalize_tags(data.get('tags', []))

        now = datetime.utcnow().isoformat()
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO kb_entries
            (title, summary, full_report, sources, category, relevance, relevance_score,
             tags, status, input_text, input_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?)
        ''', (
            title[:300], summary[:5000], full_report[:80000],
            json.dumps(sources), category, relevance, relevance_score,
            json.dumps(tags), input_text, input_url, now, now
        ))
        entry_id = cur.lastrowid
        conn.commit()
        row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
        conn.close()
        add_to_brain(entry_id, title, summary, category, relevance, full_report, input_url)
        return jsonify({
            'ok': True,
            'manual': True,
            'id': entry_id,
            'entry': serialize_entry(row, include_full_report=True),
            'url': f'https://gu.kuble.com/kb/#id-{entry_id}'
        })

    # --------- Automatic mode ---------
    if not input_text and not input_url:
        return jsonify({
            'error': 'Brauche input_text oder input_url, oder setze manual=true mit title.'
        }), 400

    force_new = _truthy(data.get('force_new', False)) or data.get('force_new') is True
    merge_target_id = data.get('merge_with')

    merge_existing = None
    if merge_target_id:
        try:
            merge_target_id = int(merge_target_id)
        except Exception:
            return jsonify({'error': 'merge_with must be an integer id.'}), 400
        conn = get_db()
        row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (merge_target_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': f'merge_with target {merge_target_id} nicht gefunden.'}), 404
        merge_existing = dict(row)

    # Exact URL duplicate check, mirrors the human-facing /kb/api/entries flow.
    if input_url and not merge_existing and not force_new:
        same_url = find_exact_url_match(input_url)
        if same_url:
            return jsonify({
                'action': 'merge_candidates',
                'similar_open': same_url,
                'message': 'Eintrag mit identischer URL existiert bereits. '
                           'Setze merge_with=<id> zum Mergen oder force_new=true fuer neuen Eintrag.'
            }), 200

    try:
        if merge_existing:
            report = perplexity_merge(merge_existing, input_text, input_url or None)
        else:
            report = perplexity_research(input_text, input_url or None)
    except Exception as e:
        return jsonify({'error': f'Research failed: {e}'}), 500

    # --------- Merge path ---------
    if merge_existing:
        now = datetime.utcnow().isoformat()
        existing_sources = []
        try:
            existing_sources = json.loads(merge_existing.get('sources') or '[]')
        except Exception:
            existing_sources = []
        new_sources = list(dict.fromkeys(existing_sources + (report.get('sources') or [])))
        if input_url and input_url not in new_sources:
            new_sources = [input_url] + new_sources

        old_tags = normalize_tags(_parse_json_field(merge_existing.get('tags'), []))
        merged_tags = normalize_tags(old_tags + normalize_tags(data.get('tags', [])))

        conn = get_db()
        conn.execute('''
            UPDATE kb_entries SET
              title = ?, summary = ?, full_report = ?, sources = ?,
              category = ?, relevance = ?, relevance_score = ?, tags = ?,
              input_text = ?, input_url = ?, updated_at = ?
            WHERE id = ?
        ''', (
            report['title'],
            report['summary'],
            report['full_report'],
            json.dumps(new_sources),
            report.get('category', merge_existing['category']),
            report.get('relevance', merge_existing['relevance']),
            report.get('relevance_score', merge_existing['relevance_score']),
            json.dumps(merged_tags),
            f"{merge_existing.get('input_text','') or ''} | {input_text}".strip(' |'),
            input_url or merge_existing.get('input_url', ''),
            now,
            merge_target_id
        ))
        conn.commit()
        row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (merge_target_id,)).fetchone()
        conn.close()
        return jsonify({
            'ok': True,
            'merged': True,
            'id': merge_target_id,
            'entry': serialize_entry(row, include_full_report=True),
            'url': f'https://gu.kuble.com/kb/#id-{merge_target_id}'
        })

    # --------- Fresh create path ---------
    duplicates = find_duplicates(report['title'], report['summary'])
    all_sources = list(report.get('sources') or [])
    if input_url and input_url not in all_sources:
        all_sources = [input_url] + all_sources

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.execute('''
        INSERT INTO kb_entries
        (title, summary, full_report, sources, category, relevance, relevance_score,
         tags, status, input_text, input_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?)
    ''', (
        report['title'],
        report['summary'],
        report['full_report'],
        json.dumps(all_sources),
        report.get('category', 'General'),
        report.get('relevance', ''),
        report.get('relevance_score', 3),
        json.dumps(normalize_tags(data.get('tags', []))),
        input_text, input_url, now, now
    ))
    entry_id = cur.lastrowid
    conn.commit()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()

    add_to_brain(entry_id, report['title'], report['summary'],
                 report.get('category', 'General'), report.get('relevance', ''),
                 report.get('full_report', ''), input_url)

    return jsonify({
        'ok': True,
        'id': entry_id,
        'entry': serialize_entry(row, include_full_report=True),
        'duplicates': duplicates,
        'url': f'https://gu.kuble.com/kb/#id-{entry_id}'
    })


@app.route('/kb/api/agent/entries/<int:entry_id>', methods=['PATCH'])
@require_agent_token
def agent_update_entry(entry_id):
    """Edit allowed fields plus status/tags/notes. Mirrors UI PATCH plus extras."""
    data, err = parse_agent_json_body()
    if err:
        return err
    if not data:
        return jsonify({'error': 'Empty JSON body.'}), 400

    allowed_fields = {
        'title': lambda v: str(v).strip()[:300],
        'summary': lambda v: str(v).strip()[:5000],
        'full_report': lambda v: str(v).strip()[:80000],
        'category': lambda v: str(v).strip() if str(v).strip() in CATEGORIES else 'General',
        'relevance': lambda v: str(v).strip()[:5000],
        'input_text': lambda v: str(v).strip()[:10000],
        'input_url': lambda v: str(v).strip()[:2000],
    }

    updates = {}
    for field, cleaner in allowed_fields.items():
        if field in data:
            updates[field] = cleaner(data.get(field, ''))

    if 'relevance_score' in data:
        try:
            updates['relevance_score'] = max(1, min(5, int(data.get('relevance_score', 3))))
        except Exception:
            return jsonify({'error': 'relevance_score must be an integer 1..5.'}), 400

    if 'status' in data:
        status = str(data.get('status', '') or '').strip()
        if status not in VALID_STATUSES:
            return jsonify({'error': f'Invalid status. Use one of {VALID_STATUSES}'}), 400
        updates['status'] = status

    if 'sources' in data:
        updates['sources'] = json.dumps(_normalize_source_list(data.get('sources')))

    if 'tags' in data:
        updates['tags'] = json.dumps(normalize_tags(data.get('tags', [])))

    if 'notes' in data:
        updates['notes'] = str(data.get('notes', '') or '')[:20000]

    if not updates:
        return jsonify({'error': 'No editable fields provided.'}), 400
    if 'title' in updates and not updates['title']:
        return jsonify({'error': 'Title darf nicht leer sein.'}), 400

    updates['updated_at'] = datetime.utcnow().isoformat()
    assignments = ', '.join([f'{field} = ?' for field in updates.keys()])
    values = list(updates.values()) + [entry_id]

    conn = get_db()
    exists = conn.execute('SELECT id FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    if not exists:
        conn.close()
        return jsonify({'error': 'Not found', 'id': entry_id}), 404
    conn.execute(f'UPDATE kb_entries SET {assignments} WHERE id = ?', values)
    conn.commit()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    return jsonify({
        'ok': True,
        'id': entry_id,
        'updated_fields': sorted(k for k in updates.keys() if k != 'updated_at'),
        'entry': serialize_entry(row, include_full_report=True)
    })


@app.route('/kb/api/agent/entries/<int:entry_id>', methods=['DELETE'])
@require_agent_token
def agent_delete_entry(entry_id):
    conn = get_db()
    exists = conn.execute('SELECT id FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    if not exists:
        conn.close()
        return jsonify({'error': 'Not found', 'id': entry_id}), 404
    conn.execute('DELETE FROM kb_entries WHERE id = ?', (entry_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted_id': entry_id})


# ============ STATIC ============

@app.route('/kb/', defaults={'path': ''})
@app.route('/kb/<path:path>')
def serve_static(path):
    if path == '' or path == 'index.html':
        return send_from_directory('/var/www/publish/kb', 'index.html')
    return send_from_directory('/var/www/publish/kb', path)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8084, debug=False)
