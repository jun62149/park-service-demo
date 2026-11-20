"""Local HTTP probe; deliberately never confirms a cloud preview."""
import json
from pathlib import Path
import requests


def main():
    base='http://127.0.0.1:5000'; s=requests.Session(); results=[]
    def get(path):
        r=s.get(base+path,headers={'Accept':'application/json'},timeout=5)
        assert r.status_code==200,(path,r.status_code)
        results.append({'path':path,'status':r.status_code})
        return r.json()
    def post(path,data,code=200):
        csrf=get('/session')['csrf']
        r=s.post(base+path,data=data,headers={'Accept':'application/json','X-CSRF-Token':csrf},timeout=5)
        assert r.status_code==code,(path,r.status_code)
        results.append({'path':path,'status':r.status_code})
        return r.json()
    d=post('/parse',{'source_text':'地点：演示A座302，联系人：演示甲，电话：13900001111，演示：灯不亮。'})
    assert '13900001111' not in (d.get('outbound_text') or '')
    r=post('/parse/extract',d)
    assert r['provider']=='rules'
    if d['preview_id']: assert r['fallback_reason']=='privacy_not_confirmed'
    session=get('/session')
    created=post('/tickets',dict(create_nonce=session['create_nonce'],extraction_run_id=r['run_id'],reporter_name='演示甲',reporter_contact='DEMO-HTTP-STAGE2',location='演示A座302',
        original_text='地点：演示A座302，联系人：演示甲，电话：13900001111，演示：灯不亮。',issue_description='人工修正：演示灯不亮'),code=201)
    tid=created['id']
    post('/role',{'user_id':'2'})
    version=get(f'/tickets/{tid}')['ticket']['version']
    post(f'/tickets/{tid}/actions/assign',dict(version=version,engineer_id='3',priority='特急',priority_confirmed='yes'))
    assert not get('/reminders')['flags'][str(tid)]['phone_followup']
    post('/demo/advance',{'seconds':'299'})
    assert not get('/reminders')['flags'][str(tid)]['phone_followup']
    post('/demo/advance',{'seconds':'1'})
    assert get('/reminders')['flags'][str(tid)]['phone_followup']
    get('/reminders'); get('/reminders')
    post('/role',{'user_id':'3'})
    version=get(f'/tickets/{tid}')['ticket']['version']
    post(f'/tickets/{tid}/actions/accept',{'version':version})
    assert not get('/reminders')['flags'][str(tid)]['phone_followup']
    result=dict(result='passed',ticket_id=tid,provider=r['provider'],fallback_reason=r['fallback_reason'],
        threshold_299=False,threshold_300=True,accepted_clears=True,steps=results,
        note='Never confirmed cloud preview; no paid request in this HTTP probe.')
    Path('docs/evidence/stage2-http.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='steps'},ensure_ascii=False))


if __name__=='__main__':main()
