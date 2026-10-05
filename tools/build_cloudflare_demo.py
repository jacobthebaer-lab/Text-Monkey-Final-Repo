"""Package the same Text Monkey UI for Cloudflare Pages.

The default is a static synthetic preview. Connected mode adds the existing
API proxy, with backend bindings supplied privately at deployment time.
The output directory must be new so packaging never removes existing work.
"""
import argparse
import json
from pathlib import Path
import shutil


def build(destination, source=None, *, connected=False):
    source = Path(source) if source else Path(__file__).resolve().parents[1] / 'web/texty/public'
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Choose a new output directory; existing files are never overwritten.')
    files = list(source.rglob('*'))
    if any(p.is_symlink() for p in files):
        raise ValueError('Public assets must be regular files, not links to private files.')
    if any(p.name.startswith('.') or p.suffix in {'.sqlite','.db'} for p in files):
        raise ValueError('Private or hidden files cannot be included in the public demo.')
    proxy = source.parent / 'worker.js'
    if connected and (proxy.is_symlink() or not proxy.is_file()):
        raise ValueError('Connected Pages requires the regular existing API proxy file.')
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
    if connected:
        (destination/'api/config.json').unlink()
        (destination/'api').rmdir()
        (destination/'_redirects').unlink()
        shutil.copyfile(proxy, destination/'_worker.js')
        (destination/'_routes.json').write_text(json.dumps({
            'version': 1, 'include': ['/api/*', '/sms/*'], 'exclude': [],
        }, indent=2)+'\n')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    parser.add_argument('--connected', action='store_true', help='Bundle the existing API proxy without credentials')
    args = parser.parse_args()
    print(build(args.destination, connected=args.connected))
