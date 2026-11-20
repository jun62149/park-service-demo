import secrets
from flask import Blueprint,abort,current_app,g,jsonify,redirect,render_template,request,session,url_for
from . import clock
from .db import get_db
from .files import safe_path
from .workflow import STATES,can_view,create_ticket
from .notifications import serialize_reset

bp=Blueprint('workspaces',__name__)


def metrics(rows,flags):
    counts={state:sum(t['status']==state for t in rows) for state in STATES}
    return dict(total=len(rows),states=counts,close_rate=round(100*counts['closed']/len(rows),1) if rows else None,
        overdue=sum(flags[t['id']]['overdue'] for t in rows),urgent_pending=sum(t['priority']=='特急' and t['status']=='pending_acceptance' for t in rows),
        phone_followup=sum(flags[t['id']]['phone_followup'] for t in rows),unconfigured=sum(t['due_at'] is None for t in rows))


@bp.get('/workspace')
def workspace():
    db=get_db(); flags=clock.evaluate(db)
    rows=[dict(t) for t in db.execute('SELECT * FROM tickets ORDER BY id DESC') if can_view(g.actor,t)]
    # Manager free-text and contacts never enter rendered work queues.
    for t in rows:
        t['source']='人工填写'
        run=db.execute('SELECT provider,fallback_reason FROM extraction_runs WHERE ticket_id=? ORDER BY id DESC LIMIT 1',(t['id'],)).fetchone()
        if run: t['source']='规则降级' if run['fallback_reason'] else {'deepseek':'DeepSeek','ollama':'Ollama','rules':'规则解析'}.get(run['provider'],'人工填写')
    result=metrics(rows,flags)
    if request.accept_mimetypes.best=='application/json': return jsonify(metrics=result,visible_ids=[t['id'] for t in rows])
    return render_template('workspace.html',tickets=rows,stats=result,flags=flags)


def seed_examples(db,start_id):
    actor=dict(db.execute('SELECT * FROM users WHERE id=1').fetchone())
    for i,(place,issue) in enumerate([('演示A座302','空调不制冷'),('演示公共走廊','照明灯不亮'),('演示B座茶水间','水龙头滴水')]):
        create_ticket(db,actor,dict(reporter_name='演示报修人甲',reporter_contact='DEMO-前台分机',company_name='演示企业',
            location=place,original_text='演示：'+issue,issue_description='演示：'+issue),in_transaction=True,seed_id=start_id+i)


@bp.route('/demo/reset-data',methods=['GET','POST'])
@serialize_reset
def reset():
    if not current_app.config['DEMO_MODE'] or g.actor['role']!='service': abort(403,'仅演示模式的企服可重置')
    if request.method=='GET':
        token=secrets.token_urlsafe(32); session['reset_token']=token
        session['reset_generation']=get_db().execute('SELECT generation FROM demo_meta').fetchone()[0]
        return render_template('reset.html',reset_token=token)
    data=request.get_json() if request.is_json else request.form
    token=data.get('reset_token','')
    if not token or token!=session.get('reset_token') or data.get('confirmation')!='重置演示数据':
        abort(400,'确认已失效或确认文字不匹配，请重新打开重置页')
    db=get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        generation=db.execute('SELECT generation FROM demo_meta').fetchone()[0]
        if generation!=session.get('reset_generation'): abort(409,'演示数据已变化，请重新确认')
        # No filesystem deletion or caller-supplied path. Only demo-tagged local DB data.
        if db.execute('SELECT count(*) FROM tickets WHERE is_demo<>1').fetchone()[0]: abort(409,'包含非演示数据，禁止重置')
        next_id=db.execute('SELECT coalesce(max(id),0)+1 FROM tickets').fetchone()[0]
        old_photos=[safe_path(current_app.config['UPLOAD_ROOT'],r[0]) for r in db.execute('SELECT generated_name FROM attachments')]
        db.execute('UPDATE tickets SET current_assignment_id=NULL')
        for table in ['notification_outbox','ticket_events','attachments','completion_submissions','extraction_runs','reminders','assignments','tickets','parse_previews','parse_drafts','clock_events']:
            db.execute('DELETE FROM '+table)
        db.execute('UPDATE demo_clock SET offset_seconds=0')
        db.execute('UPDATE demo_meta SET generation=?',(secrets.token_hex(16),))
        seed_examples(db,next_id)
        db.commit()
    except Exception:
        db.rollback(); raise
    residual=0
    for path in old_photos:
        try: path.unlink(missing_ok=True)
        except OSError: residual+=1
    session.clear()
    if request.accept_mimetypes.best=='application/json': return jsonify(ok=True,clock_reset=True,residual_photos=residual)
    return redirect(url_for('workspaces.workspace'))
