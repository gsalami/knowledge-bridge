# Deployment Notes

Canonical runtime on Gu's server:

- Live URL: `https://gu.kuble.com/kb/`
- Live app directory: `/var/www/publish/kb/`
- Backend service: `kb-api.service`
- Flask backend: `/var/www/publish/kb/api.py`
- Frontend: `/var/www/publish/kb/index.html`
- SQLite database: `/var/www/publish/kb/kb.db`
- Nginx config: `/etc/nginx/sites-enabled/publish`

This repository should contain only source code and lightweight assets. Runtime data, local backups, videos, pycache and SQLite files must not be committed.

## Deploy current repo to live

```bash
sudo install -o cc-runner -g cc-runner -m 664 index.html /var/www/publish/kb/index.html
sudo install -o cc-runner -g cc-runner -m 664 api.py /var/www/publish/kb/api.py
sudo install -o root -g cc-runner -m 664 favicon.svg /var/www/publish/kb/favicon.svg
sudo python3 -m py_compile /var/www/publish/kb/api.py
sudo systemctl restart kb-api.service
curl -sS -I https://gu.kuble.com/kb/ | head
```

For frontend-only changes, no service restart is needed. Still run the JS syntax check below.

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
