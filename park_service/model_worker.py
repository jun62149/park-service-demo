"""One bounded HTTP request in a killable subprocess. Never print exceptions."""
import json
import sys
import requests


def call(job):
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.post(job['url'], json=job['body'], headers=job['headers'],
                    timeout=(min(3, job['timeout']), job['timeout']), stream=True,
                    allow_redirects=False) as response:
                if response.status_code != 200:
                    code = response.status_code
                    return {'error': 'http_auth' if code in (401,403) else 'http_rate_limit' if code==429 else 'http_server' if code>=500 else 'http_error'}
                content = bytearray()
                for block in response.iter_content(1024):
                    content.extend(block)
                    if len(content)>65536:
                        return {'error':'response_too_large'}
                try:
                    return {'response':json.loads(content)}
                except (ValueError, UnicodeError):
                    return {'error':'invalid_json'}
    except requests.Timeout:
        return {'error':'timeout'}
    except Exception:
        return {'error':'network_error'}


if __name__=='__main__':
    try:
        result=call(json.loads(sys.stdin.buffer.read(100000)))
        sys.stdout.buffer.write(json.dumps(result,ensure_ascii=True).encode('ascii'))
    except Exception:
        sys.stdout.buffer.write(b'{"error":"transport_error"}')
