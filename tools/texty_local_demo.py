#!/usr/bin/env python3
"""Offline Texty UI sandbox. No backend, accounts, credentials or transports.
Run: python tools/texty_local_demo.py --port 58123
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC = Path(__file__).resolve().parents[1] / 'web/texty/public'
ASSETS = {'app.js', 'domain.js', 'setup.js', 'setup-domain.js', 'style.css'}
BOOTSTRAP = b'''import './app.js';
const start = () => document.querySelector('[data-action="demo"]')?.click();
start();
new MutationObserver(start).observe(document.querySelector('#app'), {childList:true});
'''
CONFIG = {'name': 'Texty Local Demo', 'provider': 'sample rules', 'connected': False,
          'aiReady': False, 'liveSms': False, 'automationEnabled': False,
          'macBridgeConfigured': False, 'macBridgeConnected': False}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, content, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-src 'none'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            return self.respond(403, b'{"error":"Loopback origin required"}')
        path = urlsplit(self.path).path
        if path in ('/', '/texty'):
            page = (PUBLIC / 'index.html').read_bytes().replace(b'src="/app.js"', b'src="/local-demo.js"')
            return self.respond(200, page, 'text/html; charset=utf-8')
        if path == '/api/config':
            return self.respond(200, json.dumps(CONFIG).encode())
        if path == '/local-demo.js':
            return self.respond(200, BOOTSTRAP, 'text/javascript; charset=utf-8')
        if path.startswith('/api/'):
            return self.respond(403, b'{"error":"Real APIs are disabled in the local synthetic demo"}')
        if path[1:] in ASSETS:
            return self.respond(200, (PUBLIC / path[1:]).read_bytes(), 'text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8')
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
        demo.serve_forever()
