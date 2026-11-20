import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.exceptions import BadRequest

from park_service.files import MAX_FILE, safe_path
from .conftest import image_bytes
from .test_workflow import snapshot


@pytest.mark.parametrize('fmt,filename,mime',[('PNG','demo.png','image/png'),('JPEG','demo.jpeg','image/jpeg')])
def test_T12_valid_private_canonical_image(app,browser,db,fmt,filename,mime):
    browser.processing()
    raw=image_bytes(fmt)
    assert browser.action('submit_completion',{'resolution_note':'演示','photos':(io.BytesIO(raw),filename,mime)}).status_code==200
    photo=dict(db.execute('SELECT * FROM attachments').fetchone())
    response=browser.get(f"/attachments/{photo['id']}")
    assert response.status_code==200 and response.mimetype==mime
    assert response.headers['X-Content-Type-Options']=='nosniff'
    assert response.data!=raw and photo['is_placeholder']==1
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.width>=480 and image.height>=100
    assert browser.get('/uploads/'+photo['generated_name']).status_code==404
    assert browser.get('/static/uploads/'+photo['generated_name']).status_code==404


@pytest.mark.parametrize('extra',[0,1])
def test_T12_exact_five_MiB_boundary(browser,db,extra):
    browser.processing(); before=snapshot(db)
    raw=image_bytes(); padded=raw+b'\0'*(MAX_FILE-len(raw)+extra)
    response=browser.action('submit_completion',{'resolution_note':'演示','photos':(io.BytesIO(padded),'demo.png','image/png')})
    assert response.status_code==(200 if extra==0 else 400)
    if extra: assert snapshot(db)==before


def test_T12_count_and_request_limit(browser,db):
    browser.processing(); before=snapshot(db)
    assert browser.action('submit_completion',{'resolution_note':'演示','photos':[(io.BytesIO(image_bytes()),f'demo{i}.png','image/png') for i in range(4)]}).status_code==400
    assert snapshot(db)==before
    result=browser.action('submit_completion',{'resolution_note':'演示','photos':(io.BytesIO(b'x'*(16*1024*1024)),'demo.png','image/png')})
    assert result.status_code==413 and snapshot(db)==before


@pytest.mark.parametrize('name,mime,data',[
    ('fake.jpg','image/jpeg',b'hello'),('empty.png','image/png',b''),
    ('bad.png','image/png',b'\x89PNG\r\n\x1a\n'+b'bad'),
    ('demo.png','image/jpeg',image_bytes()),('fake.jpg','image/jpeg',image_bytes()),
    ('demo.svg','image/svg+xml',b'<svg/>'),('demo.html','text/html',b'<html/>'),
    ('app.exe','image/png',image_bytes()),('../demo.png','image/png',image_bytes()),
    ('C:\\demo.png','image/png',image_bytes()),('/demo.png','image/png',image_bytes()),
    ('sub\\demo.png','image/png',image_bytes()),('demo.png:extra','image/png',image_bytes())])
def test_T13_T14_bad_files(browser,db,name,mime,data):
    browser.processing(); before=snapshot(db)
    assert browser.action('submit_completion',{'resolution_note':'演示','photos':(io.BytesIO(data),name,mime)}).status_code==400
    assert snapshot(db)==before


def test_T13_pixel_limit(browser,db):
    browser.processing(); before=snapshot(db)
    raw=image_bytes(size=(5000,4001))
    assert browser.action('submit_completion',{'resolution_note':'演示','photos':(io.BytesIO(raw),'huge.png','image/png')}).status_code==400
    assert snapshot(db)==before


def test_T14_same_names_do_not_overwrite(app,browser,db):
    browser.review(); browser.role(2)
    assert browser.action('reject',{'reason':'演示返工'}).status_code==200
    browser.role(3)
    assert browser.action('resume').status_code==200
    assert browser.complete().status_code==200
    rows=db.execute('SELECT * FROM attachments').fetchall()
    assert len(rows)==2 and rows[0]['original_name']==rows[1]['original_name']
    assert rows[0]['generated_name']!=rows[1]['generated_name']
    assert len(list(Path(app.config['UPLOAD_ROOT']).iterdir()))==2
    with pytest.raises(BadRequest): safe_path(app.config['UPLOAD_ROOT'],'../escape.png')
    with pytest.raises(BadRequest): safe_path(app.config['UPLOAD_ROOT'],'C:/escape.png')


def test_T14_missing_or_changed_evidence_prevents_close(app,browser,db):
    browser.review(); browser.role(2)
    photo=db.execute('SELECT * FROM attachments').fetchone()
    path=Path(app.config['UPLOAD_ROOT'])/photo['generated_name']
    path.write_bytes(b'changed')
    before=snapshot(db)
    assert browser.action('close',{'review_confirmed':'yes'}).status_code==409
    assert snapshot(db)==before
    path.unlink()
    assert browser.action('close',{'review_confirmed':'yes'}).status_code==409
    assert snapshot(db)==before
