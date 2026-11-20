"""Explicit localhost HTTP acceptance probe; uses virtual demo data only."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:5000')
    parser.add_argument('--output', default='docs/evidence/http-smoke.json')
    args = parser.parse_args()
    if urlparse(args.base).hostname != '127.0.0.1':
        parser.error('仅允许本机回环地址')
    session = requests.Session()
    steps = []
    def request(method,path,expected=200,**kwargs):
        headers={'Accept':'application/json',**kwargs.pop('headers',{})}
        if method=='POST':
            headers['X-CSRF-Token']=session.get(args.base+'/session',timeout=5).json()['csrf']
        response=session.request(method,args.base+path,headers=headers,timeout=10,**kwargs)
        steps.append(dict(method=method,path=path,status=response.status_code))
        assert response.status_code==expected,(path,response.status_code,response.text[:200])
        return response
    def role(uid): request('POST','/role',data={'user_id':str(uid)})
    def detail(): return request('GET',f'/tickets/{tid}').json()
    def action(name,data=None,expected=200,version=None,**kwargs):
        values={'version':str(version if version is not None else detail()['ticket']['version']),**(data or {})}
        return request('POST',f'/tickets/{tid}/actions/{name}',expected,data=values,**kwargs)
    def photo_submit(note):
        with Path('tests/fixtures/demo-placeholder.png').open('rb') as photo:
            action('submit_completion',{'resolution_note':note},files={'photos':('demo.png',photo,'image/png')})
    nonce=request('GET','/session').json()['create_nonce']
    form=dict(create_nonce=nonce,reporter_name='演示HTTP报修人',reporter_contact='DEMO-HTTP',
        company_name='',location='演示HTTP公共区',original_text='演示：HTTP验证灯不亮',issue_description='演示：灯不亮')
    tid=request('POST','/tickets',201,data=form).json()['id']
    request('POST','/tickets',409,data=form)
    role(2)
    action('assign',dict(engineer_id='3',priority='普通',priority_confirmed='yes'))
    action('reassign',dict(engineer_id='4',priority='加急',priority_confirmed='yes',reason='演示改派'))
    role(3)
    request('GET',f'/tickets/{tid}',403)
    action('accept',expected=403,version=3)
    role(4)
    action('accept')
    action('accept',expected=409,version=3)
    action('submit_completion',{'resolution_note':'演示缺照片'},expected=400)
    photo_submit('演示：第一轮处理')
    first_photo=detail()['completions'][0]['photos'][0]['id']
    role(3)
    request('GET',f'/attachments/{first_photo}',403)
    role(2)
    action('reject',{'reason':'演示：补充复核'})
    role(4)
    action('resume')
    photo_submit('演示：第二轮复核完成')
    role(2)
    action('close',{'review_confirmed':'yes'})
    final=detail()
    assert final['ticket']['status']=='closed'
    assert len(final['assignments'])==2 and len(final['completions'])==2
    assert len(final['events'])==9
    action('reject',{'reason':'终态不得退回'},expected=409)
    html=request('GET',f'/tickets/{tid}',headers={'Accept':'text/html'}).text
    assert '已关闭' in html and '第 2 轮' in html and '演示数据' in html
    request('GET',f'/attachments/{first_photo}')
    role(5)
    assert 'DEMO-HTTP' not in detail().__str__()
    result=dict(result='passed',ticket_id=tid,number=final['ticket']['number'],
        final_status='closed',version=final['ticket']['version'],events=len(final['events']),
        assignments=2,completion_rounds=2,http_checks=len(steps),steps=steps)
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='steps'},ensure_ascii=False))


if __name__=='__main__':
    main()
