from pathlib import Path
import subprocess

from park_service import create_app


def test_T39_environment_only_and_example(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    assert 'DEEPSEEK_API_KEY=\n' in (root/'.env.example').read_text(encoding='utf-8')
    assert '!.env.example' in (root/'.gitignore').read_text(encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    (tmp_path/'.env').write_text('DEEPSEEK_API_KEY=unused_simulation_marker',encoding='utf-8')
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.delenv('DEEPSEEK_MODEL',raising=False)
    app=create_app(dict(TESTING=True,SECRET_KEY='config-test-only',DATABASE=str(tmp_path/'a.db'),UPLOAD_ROOT=str(tmp_path/'photos')))
    assert app.config['DEEPSEEK_API_KEY']=='' and app.config['DEEPSEEK_MODEL']=='deepseek-flash'
    monkeypatch.setenv('DEEPSEEK_API_KEY','environment_simulation_marker')
    app2=create_app(dict(TESTING=True,SECRET_KEY='config-test-only',DATABASE=str(tmp_path/'b.db'),UPLOAD_ROOT=str(tmp_path/'photos2')))
    assert app2.config['DEEPSEEK_API_KEY']=='environment_simulation_marker'


def test_T39_git_ignores_runtime_not_template():
    ignored=subprocess.run(['git','check-ignore','.env','instance/session_key','instance/park.sqlite3','instance/uploads/demo.png'],capture_output=True,text=True)
    assert ignored.returncode==0 and len(ignored.stdout.splitlines())==4
    template=subprocess.run(['git','check-ignore','.env.example'],capture_output=True)
    assert template.returncode==1
