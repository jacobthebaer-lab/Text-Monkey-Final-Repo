"""Publish the sanitized feed using a status-only credential kept in CI secrets."""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def urlopen(request, timeout):
    # Never forward the status-only credential to a redirect destination.
    return build_opener(NoRedirect).open(request, timeout=timeout)


def publish(path, url, key):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'text-monkey-universe-dev.jacobthebaer.workers.dev' or parsed.path != '/api/status/publish'
            or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment):
        raise ValueError('Invalid status publishing destination')
    if len(key) < 32:
        raise ValueError('Status publishing credential unavailable')
    body = path.read_bytes()
    data = json.loads(body)
    if (len(body) > 512 * 1024 or not isinstance(data, dict) or data.get('schemaVersion') != 1
            or not isinstance(data.get('features'), dict) or not data['features']):
        raise ValueError('Invalid status feed')
    request = Request(url, data=body, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'User-Agent': 'TextMonkeyFeatureStatus/1.0 (+https://github.com/jacobthebaer-lab/text-monkey)'}, method='POST')
    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise ValueError('Status publishing did not succeed')
                receipt = json.load(response)
                if not isinstance(receipt, dict) or receipt.get('ok') is not True:
                    raise ValueError('Status publishing acknowledgment missing')
                return len(data['features'])
        except HTTPError as error:
            if error.code not in {429, 502, 503, 504} or attempt == 3:
                raise RuntimeError(f'Status publishing failed with HTTP {error.code}; publication could not be confirmed.') from None
        except URLError:
            if attempt == 3:
                raise RuntimeError('Status publishing could not reach the service; publication could not be confirmed.') from None
        time.sleep(2 ** attempt)
    raise RuntimeError('Status publishing failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    try:
        count = publish(args.input, os.environ.get('STATUS_PUBLISH_URL', ''), os.environ.get('STATUS_PUBLISH_KEY', ''))
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    print(f'Published {count} sanitized feature statuses.')


if __name__ == '__main__':
    main()
