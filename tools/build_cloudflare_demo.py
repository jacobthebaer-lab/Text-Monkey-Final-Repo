"""Package the same Text Monkey UI as a public, static synthetic preview.

No backend URL, auth keys, database, worker, or messaging service is bundled.
The output directory must be new so packaging never removes existing work.
"""
import json
from pathlib import Path
import shutil
import sys


def build(destination, source=None):
    source = Path(source) if source else Path(__file__).resolve().parents[1] / 'web/texty/public'
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Choose a new output directory; existing files are never overwritten.')
    files = list(source.rglob('*'))
    if any(p.is_symlink() for p in files):
        raise ValueError('Public assets must be regular files, not links to private files.')
    if any(p.name.startswith('.') or p.suffix in {'.sqlite','.db'} for p in files):
        raise ValueError('Private or hidden files cannot be included in the public demo.')
    shutil.copytree(source, destination)
    (destination/'api').mkdir()
    (destination/'api/config.json').write_text(json.dumps({
        'name':'Text Monkey','publicDemo':True,'connected':False,
        'provider':'sample rules','aiReady':False,'liveSms':False,
        'allowTextSignup':True,'simulatorOnly':True,'adminReplyAvailable':False,
        'humanConfirmationRequired':False,'productMode':'preview','automationEnabled':False,
    },indent=2)+'\n')
    (destination/'_redirects').write_text('/api/config /api/config.json 200\n')
    (destination/'_headers').write_text("""/*
  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
/api/*
  Cache-Control: no-store
""")
    # A real 404 prevents unknown API/account routes falling back to the app.
    (destination/'404.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Text Monkey demo</title><h1>This is the synthetic Text Monkey preview.</h1><p>Connected account and live texting actions are unavailable here.</p><a href="/">Open the demo</a></html>\n')
    return destination


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python tools/build_cloudflare_demo.py NEW_OUTPUT_DIRECTORY')
    print(build(sys.argv[1]))
