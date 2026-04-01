#!/usr/bin/env python3
"""
Knowledge Bridge API
Flask backend for the KB kanban board
Port: 8084

Configuration via environment variables:
  KB_DB_PATH        Path to SQLite DB (default: ./kb.db)
  PERPLEXITY_KEY    Perplexity API key
  GAMMA_API_KEY     Gamma API key
  KB_PASSWORD       Password for the frontend auth (default: changeme)
"""

import sqlite3
import json
import os
import re
import subprocess
from datetime import datetime
from urllib.parse import urlparse, urljoin
from flask import Flask, request, jsonify, send_from_directory
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

DB_PATH = os.environ.get('KB_DB_PATH', os.path.join(os.path.dirname(__file__), 'kb.db'))
PERPLEXITY_KEY = os.environ.get('PERPLEXITY_KEY', '')
GAMMA_API_KEY = os.environ.get('GAMMA_API_KEY', '')
GAMMA_API_URL = 'https://public-api.gamma.app/v1.0'
PASSWORD = os.environ.get('KB_PASSWORD', 'changeme')

CATEGORIES = [
    'AI Tools', 'AI Research', 'AI Business', 'Prompt Engineering',
    'LLM', 'Agents', 'Image/Video AI', 'AI Policy', 'General'
]

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


def perplexity_research(topic: str, url: str = None) -> dict:
    """Run Perplexity Sonar deep research on topic."""
    query = topic
    if url:
        query = f"Research this topic/URL in depth: {url}\n\nContext/query: {topic}"

    payload = {
        "model": "sonar",
        "messages": [
            {
                "role": "system",
                "content": """You are a knowledge researcher. Research the given topic thoroughly and return a structured JSON report.

Return ONLY valid JSON with this exact structure:
{
  "title": "Short descriptive title (max 80 chars)",
  "summary": "2-3 sentence summary",
  "full_report": "Detailed markdown report with ## headings, bullet points, key insights. Min. 300 words.",
  "sources": ["url1", "url2", "url3"],
  "category": "one of: AI Tools, AI Research, AI Business, Prompt Engineering, LLM, Agents, Image/Video AI, AI Policy, General",
  "relevance": "1-2 sentences why this is relevant for an AI knowledge base",
  "relevance_score": 1-5
}"""
            },
            {
                "role": "user",
                "content": query
            }
        ],
        "max_tokens": 2000,
        "return_citations": True
    }

    headers = {
        'Authorization': f'Bearer {PERPLEXITY_KEY}',
        'Content-Type': 'application/json'
    }

    resp = requests.post('https://api.perplexity.ai/chat/completions',
                         headers=headers, json=payload, timeout=60)
    resp.raise_for_status()

    content = resp.json()['choices'][0]['message']['content']

    # Strip markdown code fences if present
    content_clean = re.sub(r'^```(?:json)?\s*', '', content.strip(), flags=re.MULTILINE)
    content_clean = re.sub(r'\s*```\s*$', '', content_clean.strip())

    # Extract JSON from content
    json_match = re.search(r'\{.*\}', content_clean, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(content_clean)
    except json.JSONDecodeError:
        pass

    # Fallback: extract fields via regex
    import re as _re
    def _extract_str(key, text):
        m = _re.search(rf'"{key}":\s*"((?:[^"\\]|\\.|\n)*?)"', text, _re.DOTALL)
        return m.group(1).replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\') if m else None

    def _extract_array(key, text):
        m = _re.search(rf'"{key}":\s*\[(.*?)\]', text, _re.DOTALL)
        return _re.findall(r'"([^"]+)"', m.group(1)) if m else []

    def _extract_int(key, text):
        m = _re.search(rf'"{key}":\s*(\d+)', text)
        return int(m.group(1)) if m else 3

    fallback_title = _extract_str('title', content_clean) or topic[:80]
    fallback_summary = _extract_str('summary', content_clean) or content_clean[:300]
    fr_m = _re.search(r'"full_report":\s*"(.*?)(?<!\\)"(?=\s*,\s*"(?:sources|category|relevance|relevance_score)")', content_clean, _re.DOTALL)
    fallback_full = fr_m.group(1).replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\') if fr_m else content_clean
    fallback_sources = _extract_array('sources', content_clean)
    fallback_category = _extract_str('category', content_clean) or 'General'
    fallback_relevance = _extract_str('relevance', content_clean) or 'Manual review needed'
    fallback_score = _extract_int('relevance_score', content_clean)

    if fallback_title and fallback_summary:
        return {
            "title": fallback_title,
            "summary": fallback_summary,
            "full_report": fallback_full,
            "sources": fallback_sources,
            "category": fallback_category,
            "relevance": fallback_relevance,
            "relevance_score": fallback_score
        }

    return {
        "title": topic[:80],
        "summary": content_clean[:300],
        "full_report": content_clean,
        "sources": [],
        "category": "General",
        "relevance": "Manual review needed",
        "relevance_score": 3
    }


def keyword_overlap(a: str, b: str) -> float:
    """Return overlap ratio between two strings based on word sets."""
    STOPWORDS = {'the','a','an','and','or','of','in','to','is','are','was','were',
                 'for','on','at','by','with','from','as','that','this','it','be'}
    words_a = set(a.lower().split()) - STOPWORDS
    words_b = set(b.lower().split()) - STOPWORDS
    if not words_a:
        return 0.0
    return len(words_a & words_b) / len(words_a)


def find_similar_backlog(topic: str) -> list:
    """Find similar entries in BACKLOG status — candidates for merging."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, summary, status FROM kb_entries WHERE status = 'backlog'"
    ).fetchall()
    conn.close()

    matches = []
    for row in rows:
        score = max(
            keyword_overlap(topic, row['title']),
            keyword_overlap(topic, row['summary'])
        )
        if score >= 0.35:
            matches.append({'id': row['id'], 'title': row['title'], 'score': score})

    matches.sort(key=lambda x: x['score'], reverse=True)
    return matches[:3]


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
    query = f"""Existing entry on '{existing['title']}':

CURRENT SUMMARY:
{existing['summary']}

CURRENT REPORT (excerpt):
{existing['full_report'][:1000]}

NEW INFORMATION / NEW LINK:
{new_url or ''} {new_input}

Task: Update and extend this knowledge entry with the new information.
Integrate the new content seamlessly into the existing report. Mark new insights with [NEW]."""

    return perplexity_research(query, new_url)


def add_to_brain(entry_id: int, title: str, summary: str, category: str, relevance: str):
    """Optionally add entry to a brain/semantic search tool if available."""
    content = f"[KB #{entry_id}] {title}: {summary} Relevance: {relevance}"
    try:
        subprocess.run(
            ['brain', 'add', content,
             '--category', 'kb_entry',
             '--tags', f'kb,{category.lower().replace(" ", "_")},id_{entry_id}',
             '--source', 'knowledge_bridge'],
            capture_output=True, timeout=30
        )
    except Exception:
        pass  # Non-critical


# ============ AUTH ============

@app.route('/kb/api/auth', methods=['POST'])
def auth():
    data = request.json
    if data.get('password') == PASSWORD:
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Wrong password'}), 401


# ============ ENTRIES ============

@app.route('/kb/api/entries', methods=['GET'])
def get_entries():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM kb_entries ORDER BY created_at DESC'
    ).fetchall()
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
        now = datetime.utcnow().isoformat()
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO kb_entries
            (title, summary, full_report, sources, category, relevance, relevance_score,
             status, input_text, input_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?)
        ''', (
            data.get('title', input_text[:80]),
            data.get('summary', ''),
            data.get('full_report', ''),
            json.dumps(data.get('sources', [])),
            data.get('category', 'General'),
            data.get('relevance', ''),
            data.get('relevance_score', 3),
            input_text, input_url, now, now
        ))
        entry_id = cur.lastrowid
        conn.commit()
        conn.close()
        add_to_brain(entry_id, data.get('title', ''), data.get('summary', ''),
                     data.get('category', 'General'), data.get('relevance', ''))
        return jsonify({'ok': True, 'id': entry_id})

    # === BACKLOG MERGE CHECK ===
    topic_query = f"{input_text} {input_url}"
    similar_backlog = find_similar_backlog(topic_query)
    merge_target_id = data.get('merge_with')

    merge_existing = None
    if merge_target_id:
        conn = get_db()
        row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (merge_target_id,)).fetchone()
        conn.close()
        if row:
            merge_existing = dict(row)

    if similar_backlog and not merge_target_id and not data.get('force_new'):
        return jsonify({
            'action': 'merge_candidates',
            'similar_backlog': similar_backlog,
            'message': 'Similar entries found in backlog. Merge or create new?'
        }), 200

    try:
        if merge_existing:
            report = perplexity_merge(merge_existing, input_text, input_url if input_url else None)
        else:
            report = perplexity_research(input_text, input_url if input_url else None)
    except Exception as e:
        return jsonify({'error': f'Research failed: {str(e)}'}), 500

    if merge_existing:
        now = datetime.utcnow().isoformat()
        existing_sources = json.loads(merge_existing.get('sources', '[]'))
        new_sources = list(set(existing_sources + report.get('sources', [])))
        conn = get_db()
        conn.execute('''
            UPDATE kb_entries SET
              title = ?, summary = ?, full_report = ?, sources = ?,
              category = ?, relevance = ?, relevance_score = ?,
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
        })

    duplicates = find_duplicates(report['title'], report['summary'])

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.execute('''
        INSERT INTO kb_entries
        (title, summary, full_report, sources, category, relevance, relevance_score,
         status, input_text, input_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'backlog', ?, ?, ?, ?)
    ''', (
        report['title'],
        report['summary'],
        report['full_report'],
        json.dumps(report.get('sources', [])),
        report.get('category', 'General'),
        report.get('relevance', ''),
        report.get('relevance_score', 3),
        input_text, input_url, now, now
    ))
    entry_id = cur.lastrowid
    conn.commit()
    conn.close()

    add_to_brain(entry_id, report['title'], report['summary'],
                 report.get('category', 'General'), report.get('relevance', ''))

    return jsonify({
        'ok': True,
        'id': entry_id,
        'title': report['title'],
        'category': report['category'],
        'relevance_score': report.get('relevance_score', 3),
        'duplicates': duplicates,
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


@app.route('/kb/api/entries/<int:entry_id>', methods=['DELETE'])
def delete_entry(entry_id):
    conn = get_db()
    conn.execute('DELETE FROM kb_entries WHERE id = ?', (entry_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ============ PRESENTATIONS ============

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
    gamma_id = None
    if gamma_url:
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
    images = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

        og = soup.find('meta', property='og:image')
        if og and og.get('content'):
            images.append({'url': og['content'], 'source': 'article_og', 'title': 'OG Image'})

        tw = soup.find('meta', attrs={'name': 'twitter:image'})
        if tw and tw.get('content'):
            img_url = tw['content']
            if img_url not in [i['url'] for i in images]:
                images.append({'url': img_url, 'source': 'article_twitter', 'title': 'Twitter Card'})

        for img in soup.find_all('img', src=True):
            src = img['src']
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = base + src
            elif not src.startswith('http'):
                continue
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
            alt = img.get('alt', '') or img.get('title', '') or 'Article image'
            if src not in [i['url'] for i in images]:
                images.append({'url': src, 'source': 'article', 'title': alt})
            if len(images) >= 6:
                break
    except Exception:
        pass
    return images


def search_web_images(query: str, max_results: int = 5) -> list:
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
            images.append({
                'url': url or thumb,
                'thumbnail': thumb or url,
                'source': 'web_search',
                'title': r.get('title', query),
                'source_url': r.get('url', '')
            })
    except Exception:
        try:
            images = bing_image_search(query, max_results)
        except Exception:
            pass
    return images


def bing_image_search(query: str, max_results: int = 5) -> list:
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
                    images.append({'url': url, 'thumbnail': thumb, 'source': 'bing_search', 'title': title})
            except Exception:
                continue
    except Exception:
        pass
    return images


@app.route('/kb/api/image-proxy')
def image_proxy():
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
    except Exception:
        import base64
        pixel = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
        from flask import Response
        return Response(pixel, content_type='image/png', status=200)


@app.route('/kb/api/entries/<int:entry_id>/find-images', methods=['POST'])
def find_images(entry_id):
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

    if not custom_query and entry.get('input_url'):
        article_imgs = scrape_article_images(entry['input_url'])
        images.extend(article_imgs)

    remaining = max(0, 10 - len(images))
    if remaining > 0:
        web_imgs = search_web_images(search_query, max_results=remaining)
        existing_urls = {i['url'] for i in images}
        for img in web_imgs:
            if img['url'] not in existing_urls:
                images.append(img)
                existing_urls.add(img['url'])

    return jsonify({'images': images[:10]})


@app.route('/kb/api/entries/<int:entry_id>/select-image', methods=['PATCH'])
def select_image(entry_id):
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
    conn = get_db()
    row = conn.execute('SELECT * FROM kb_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    entry = dict(row)

    data = request.json or {}
    selected_image = data.get('selected_image') or entry.get('selected_image')
    pres_name = data.get('presentation_name', '')

    pres_title = f"{pres_name} - {entry['title']}" if pres_name else entry['title']

    slide1 = f"# {pres_title}"
    img_line = f"{selected_image}\n\n" if selected_image else ""
    slide2 = f"{img_line}{entry['summary']}"
    slide3 = entry['full_report'][:600].strip()

    input_text = f"{slide1}\n\n---\n\n{slide2}\n\n---\n\n{slide3}"
    image_opts = {"source": "noImages"} if selected_image else {"source": "pexels"}

    payload = {
        "inputText": input_text,
        "textMode": "preserve",
        "format": "presentation",
        "numCards": 3,
        "cardSplit": "inputTextBreaks",
        "textOptions": {"amount": "brief", "language": "en"},
        "imageOptions": image_opts,
        "cardOptions": {"dimensions": "16x9"}
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
        return jsonify({'ok': True, 'gen_id': gen_id, 'status': 'pending',
                        'message': 'Generation started — check back shortly'})

    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute('UPDATE kb_entries SET gamma_url = ?, updated_at = ? WHERE id = ?',
                 (gamma_url, now, entry_id))
    conn.commit()
    conn.close()

    return jsonify({'ok': True, 'gamma_url': gamma_url, 'gen_id': gen_id})


@app.route('/kb/api/generations/<gen_id>/status', methods=['GET'])
def get_generation_status(gen_id):
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


# ============ STATIC ============

@app.route('/kb/', defaults={'path': ''})
@app.route('/kb/<path:path>')
def serve_static(path):
    static_dir = os.path.dirname(__file__)
    if path == '' or path == 'index.html':
        return send_from_directory(static_dir, 'index.html')
    return send_from_directory(static_dir, path)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8084, debug=False)
