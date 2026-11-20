"""App Bot transport. No raw responses, credentials or recipient IDs in errors/logs."""
import json
import threading
import time

import requests

TOKEN_URL = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
MESSAGE_URL = 'https://open.feishu.cn/open-apis/im/v1/messages'


class SendError(Exception):
    def __init__(self, code, retry=False, uncertain=False):
        self.code, self.retry, self.uncertain = code, retry, uncertain
        super().__init__(code)


class AppBot:
    def __init__(self, config):
        self.config = config
        self._token = None
        self._expires = 0
        self._token_lock = threading.Lock()

    def _post(self, url, body, headers=None, sending=False):
        try:
            timeout = float(self.config.get('FEISHU_TIMEOUT_SECONDS', 5))
            if not 0 < timeout <= 5:
                raise SendError('invalid_timeout')
            # No redirects, environment proxies, request/response debug logging or retries.
            with requests.Session() as session:
                session.trust_env = False
                with session.post(url, json=body, headers=headers,
                        timeout=(min(2, timeout), timeout), allow_redirects=False, stream=True) as r:
                    if r.status_code == 429:
                        raise SendError('rate_limited', retry=True)
                    if r.status_code in (401, 403):
                        self._token = None
                        raise SendError('authentication_failed')
                    if r.status_code >= 500:
                        raise SendError('server_error', retry=not sending, uncertain=sending)
                    data = bytearray()
                    for block in r.iter_content(4096):
                        data.extend(block)
                        if len(data) > 65536:
                            raise SendError('invalid_response', uncertain=sending)
                    try:
                        value = json.loads(data)
                    except (ValueError, UnicodeError):
                        raise SendError('invalid_response', uncertain=sending) from None
                    if not isinstance(value, dict) or type(value.get('code')) is not int:
                        raise SendError('invalid_response', uncertain=sending)
                    code = value['code']
                    if code:
                        if code in (99991400, 99991401, 230020):
                            raise SendError('rate_limited', retry=True)
                        if code in (99991661, 99991663, 99991664, 99991668, 99991671, 10003):
                            self._token = None
                            raise SendError('authentication_failed')
                        if code == 230013:
                            raise SendError('recipient_unavailable')
                        if code in (99991672, 99991679, 230035, 230053):
                            raise SendError('permission_denied')
                        if code in (230034, 99992351):
                            raise SendError('invalid_recipient')
                        raise SendError('api_rejected')
                    if r.status_code != 200:
                        raise SendError('http_rejected')
                    return value
        except SendError:
            raise
        except requests.ConnectTimeout:
            raise SendError('connect_timeout', retry=True) from None
        except requests.Timeout:
            raise SendError('send_timeout' if sending else 'token_timeout', retry=not sending, uncertain=sending) from None
        except requests.RequestException:
            raise SendError('send_network_error' if sending else 'token_network_error', retry=not sending, uncertain=sending) from None
        except Exception:
            raise SendError('client_error', uncertain=sending) from None

    def token(self):
        with self._token_lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            app_id = self.config.get('FEISHU_APP_ID', '').strip()
            secret = self.config.get('FEISHU_APP_SECRET', '').strip()
            if not app_id or not secret:
                raise SendError('missing_credentials')
            result = self._post(TOKEN_URL, {'app_id': app_id, 'app_secret': secret})
            token, expiry = result.get('tenant_access_token'), result.get('expire')
            if not isinstance(token, str) or not token or type(expiry) is not int or expiry <= 0:
                raise SendError('invalid_token_response')
            self._token, self._expires = token, time.monotonic() + max(0, expiry - 60)
            return token

    def send(self, recipient, text, key):
        token = self.token()
        result = self._post(MESSAGE_URL + '?receive_id_type=open_id', {
            'receive_id': recipient, 'msg_type': 'text',
            'content': json.dumps({'text': text}, ensure_ascii=False), 'uuid': key},
            {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json; charset=utf-8'}, sending=True)
        if not isinstance(result.get('data'), dict) or not isinstance(result['data'].get('message_id'), str) or not result['data']['message_id']:
            raise SendError('invalid_send_response', uncertain=True)
        # API acceptance is not client receipt. Never persist the returned message envelope.
