import io
import pytest
from PIL import Image

from park_service import create_app
from park_service.db import connect


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'SECRET_KEY': 'not-a-credential-test-session',
                       'DATABASE': str(tmp_path / 'test.sqlite3'), 'UPLOAD_ROOT': str(tmp_path / 'uploads')})


@pytest.fixture
def db(app):
    connection = connect(app.config['DATABASE'])
    yield connection
    connection.close()


def image_bytes(fmt='PNG', size=(24, 24)):
    buffer = io.BytesIO()
    Image.new('RGB', size, '#e0ecf8').save(buffer, format=fmt)
    return buffer.getvalue()


class Browser:
    def __init__(self, app):
        self.client = app.test_client()
        self.token = self.client.get('/session').json['csrf']

    def get(self, path):
        return self.client.get(path, headers={'Accept': 'application/json'})

    def post(self, path, data=None, **kwargs):
        if path == '/tickets':
            data = {**(data or {}), 'create_nonce': self.client.get('/session').json['create_nonce']}
        return self.client.post(path, data=data or {}, headers={
            'Accept': 'application/json', 'X-CSRF-Token': self.token}, **kwargs)

    def role(self, uid):
        response = self.post('/role', {'user_id': str(uid)})
        assert response.status_code == 200
        self.token = response.json['csrf']

    def detail(self, tid=1):
        response = self.get(f'/tickets/{tid}')
        assert response.status_code == 200
        return response.json

    def action(self, action, data=None, tid=1, version=None):
        version = version if version is not None else self.detail(tid)['ticket']['version']
        return self.post(f'/tickets/{tid}/actions/{action}', {'version': str(version), **(data or {})})

    def assign(self, engineer=3):
        self.role(2)
        response = self.action('assign', {'engineer_id': str(engineer), 'priority': '加急', 'priority_confirmed': 'yes'})
        assert response.status_code == 200, response.json

    def processing(self):
        self.assign()
        self.role(3)
        assert self.action('accept').status_code == 200

    def complete(self, tid=1):
        return self.action('submit_completion', {'resolution_note': '演示：检查并处理灯具异常',
            'photos': (io.BytesIO(image_bytes()), 'demo.png', 'image/png')}, tid=tid)

    def review(self):
        self.processing()
        assert self.complete().status_code == 200


@pytest.fixture
def browser(app):
    return Browser(app)
