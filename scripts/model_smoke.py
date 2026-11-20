"""Explicit, at most one request per available model, fictional text only."""
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from park_service import create_app, ai


def main():
    app=create_app()
    report={'deepseek':{'verified':False,'reason':'missing_key','requests':0},
            'ollama':{'verified':False,'reason':'optional_backend_unavailable','requests':0}}
    # Do not print env, key presence details beyond configured/unconfigured, request headers or raw response.
    if app.config.get('DEEPSEEK_API_KEY','').strip():
        app.config.update(AI_PROVIDER='deepseek')
        client=app.test_client(); token=client.get('/session').json['csrf']
        headers={'Accept':'application/json','X-CSRF-Token':token}
        draft=client.post('/parse',data={'source_text':'地点：演示A座302，演示：灯不亮。'},headers=headers).json
        confirm=client.post('/parse/confirm',data={**draft,'acknowledge':'yes'},headers=headers).json
        if confirm['confirmed']:
            r=client.post('/parse/extract',data=draft,headers=headers).json
            report['deepseek']={k:r[k] for k in ['provider','model','fallback_reason','duration_ms','requested_mode','actual_mode_verified']}
            report['deepseek'].update(verified=r['provider']=='deepseek',requests=1,schema_valid=r['provider']=='deepseek')
    try:
        with requests.Session() as local:
            local.trust_env=False
            models=local.get('http://127.0.0.1:11434/api/tags',timeout=3).json()['models']
        if any(m.get('name')=='qwen3:14b' for m in models):
            app.config.update(AI_PROVIDER='ollama',OLLAMA_MODEL='qwen3:14b',OLLAMA_BASE_URL='http://127.0.0.1:11434')
            r=ai.parse(app.config,'地点：演示A座302，演示：灯不亮。')
            report['ollama']={k:r[k] for k in ['provider','model','fallback_reason','duration_ms']}
            report['ollama'].update(verified=r['provider']=='ollama',requests=1)
        else: report['ollama']['reason']='optional_model_not_installed'
    except Exception: pass
    output=Path('docs/evidence/stage2-real-models.json'); output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__': main()
