#!/usr/bin/env python3
"""Offline Text Monkey UI sandbox. No backend, accounts, credentials or transports.
Run: python tools/texty_local_demo.py --port 58123
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import mimetypes
BRAND_ASSETS = frozenset({
    'BRAND.md',
    'fonts/Bagel-Fat-One-OFL.txt',
    'fonts/DM-Sans-OFL.txt',
    'fonts/bagel-fat-one.ttf',
    'fonts/dm-sans-400.ttf',
    'fonts/dm-sans-600.ttf',
    'site.webmanifest',
    'textmonkey-app-icon-1024-rounded.png',
    'textmonkey-app-icon-1024-square.png',
    'textmonkey-brand-colors-type.png',
    'textmonkey-favicon-192.png',
    'textmonkey-favicon-512.png',
    'textmonkey-logo-horizontal-dark.png',
    'textmonkey-logo-horizontal-transparent.png',
    'textmonkey-logo-horizontal-yellow.png',
    'textmonkey-logo-stacked-dark.png',
    'textmonkey-logo-stacked-transparent.png',
    'textmonkey-logo-stacked-yellow.png',
    'textmonkey-mark-transparent.png',
    'textmonkey-wordmark-brown-transparent.png',
    'textmonkey-wordmark-white-transparent.png',
    'textmonkey-wordmark-yellow-transparent.png',
})

PUBLIC = Path(__file__).resolve().parents[1] / 'web/texty/public'
PREVIEW_ASSETS = {
    '/cloud-preview': ('texty_cloud_preview.html', 'text/html; charset=utf-8'),
    '/cloud-preview.js': ('texty_cloud_preview.mjs', 'text/javascript; charset=utf-8'),
}
ASSETS = {'app.js', 'domain.js', 'setup.js', 'setup-domain.js', 'style.css',
          'accessibility.js', 'admin-readiness.js', 'admin-notifications.js', 'planning-workflows.js', 'planning-center-review.js', 'onboarding-copy-nav.js',
          'onboarding-copy.js', 'onboarding-copy.html', 'onboarding-copy.css',
          'onboarding-copy-defaults.json', 'cloud-texting.js'}
BOOTSTRAP = b'''import './app.js';
const start = () => document.querySelector('[data-action="demo"]')?.click();
start();
new MutationObserver(start).observe(document.querySelector('#app'), {childList:true});
'''
CONFIG = {'name': 'Text Monkey Local Preview', 'provider': 'sample rules', 'connected': False,
          'aiReady': False, 'liveSms': False, 'automationEnabled': False,
          'macBridgeConfigured': False, 'macBridgeConnected': False}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, content, content_type='application/json', connect_sources="'self'"):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', f"default-src 'self'; connect-src {connect_sources}; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-src 'none'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            return self.respond(403, b'{"error":"Loopback origin required"}')
        path = urlsplit(self.path).path
        if path in PREVIEW_ASSETS:
            filename, content_type = PREVIEW_ASSETS[path]
            return self.respond(200, Path(__file__).with_name(filename).read_bytes(), content_type, connect_sources="'none'")
        if path in ('/', '/texty'):
            page = (PUBLIC / 'index.html').read_bytes().replace(b'src="/app.js"', b'src="/local-demo.js"')
            return self.respond(200, page, 'text/html; charset=utf-8')
        if path == '/api/config':
            return self.respond(200, json.dumps(CONFIG).encode())
        if path == '/local-demo.js':
            return self.respond(200, BOOTSTRAP, 'text/javascript; charset=utf-8')
        if path.startswith('/api/'):
            return self.respond(403, b'{"error":"Real APIs are disabled in the local synthetic demo"}')
        if path.startswith('/brand/') and path[len('/brand/'):] in BRAND_ASSETS:
            return self.respond(200, (PUBLIC / path[1:]).read_bytes(), mimetypes.guess_type(path)[0] or 'application/octet-stream')
        if path[1:] in ASSETS:
            return self.respond(200, (PUBLIC / path[1:]).read_bytes(),
                                (mimetypes.guess_type(path)[0] or 'application/octet-stream') + '; charset=utf-8')
        return self.respond(404, b'{"error":"Not found"}')

    def deny_write(self):
        self.respond(403, b'{"error":"No backend writes, authentication or text delivery in this demo"}')

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = deny_write


def server(host='127.0.0.1', port=58123, mode='synthetic'):
    if host != '127.0.0.1' or mode != 'synthetic':
        raise ValueError('Only 127.0.0.1 synthetic mode is supported')
    return ThreadingHTTPServer((host, port), Handler)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=['127.0.0.1'], default='127.0.0.1')
    parser.add_argument('--mode', choices=['synthetic'], default='synthetic')
    parser.add_argument('--port', type=int, default=58123)
    args = parser.parse_args()
    with server(args.host, args.port, args.mode) as demo:
        print(f'Fictional local demo: http://127.0.0.1:{demo.server_port}/texty', flush=True)
        print(f'Disconnected cloud preview: http://127.0.0.1:{demo.server_port}/cloud-preview', flush=True)
        demo.serve_forever()
