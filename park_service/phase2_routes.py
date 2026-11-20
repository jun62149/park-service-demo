import json
from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, session, url_for
from . import ai, clock, privacy
from .db import get_db
from .workflow import can_view

bp=Blueprint('phase2',__name__)


@bp.before_request
def parse_permission():
    if request.path.startswith('/parse') and g.actor['role'] not in ('reporter','service'):
        abort(403,'当前身份不能解析报修或确认出站')


def values():
    data=request.get_json() if request.is_json else request.form
    if not isinstance(data,dict) and not hasattr(data,'get'): abort(400,'请求必须为对象')
    return data


def json_mode(): return request.accept_mimetypes.best=='application/json'


@bp.route('/parse',methods=['GET','POST'])
def parse_page():
    if g.actor['role'] not in ('reporter','service'): abort(403,'当前身份不能解析报修')
    draft=None; preview_id=None; outbound=None
    if request.method=='POST':
        data=values()
        draft=privacy.save_draft(get_db(),session['parse_session'],g.actor,data.get('source_text'),data.get('draft_id') or None)
        if ai.settings(current_app.config)['provider']=='deepseek':
            preview_id,outbound=privacy.make_preview(get_db(),draft)
        if json_mode(): return jsonify(draft_id=draft['id'],version=draft['version'],preview_id=preview_id,outbound_text=outbound)
    return render_template('parse.html',draft=draft,preview_id=preview_id,outbound=outbound,cfg=ai.settings(current_app.config),result=None)


@bp.post('/parse/confirm')
def confirm():
    data=values()
    if data.get('acknowledge')!='yes': abort(400,'请明确确认预览为虚构或已脱敏')
    try: reason=privacy.confirm(get_db(),data.get('preview_id'),session['parse_session'],g.actor,data)
    except (privacy.Forbidden,privacy.NotFound): reason='privacy_not_confirmed'
    if json_mode(): return jsonify(confirmed=reason is None,reason=reason)
    return render_template('parse_confirmed.html',preview_id=data.get('preview_id'),draft_id=data.get('draft_id'),reason=reason,reasons=ai.REASONS)


@bp.post('/parse/extract')
def extract():
    data=values()
    run_id,result,draft=privacy.extract(get_db(),data.get('draft_id'),data.get('preview_id'),session['parse_session'],g.actor,data)
    if json_mode(): return jsonify(run_id=run_id,**result)
    return redirect(url_for('new_ticket',run_id=run_id))


@bp.get('/reminders')
def reminders():
    db=get_db(); flags=clock.evaluate(db)
    visible={row['id'] for row in db.execute('SELECT * FROM tickets') if can_view(g.actor,row)}
    return jsonify(flags={str(t):v for t,v in flags.items() if t in visible},effective_at=clock.iso_now(db),
        offset_seconds=db.execute('SELECT offset_seconds FROM demo_clock WHERE id=1').fetchone()[0],
        note='站内演示轮询；没有页面评估时不承诺实时推送，不自动拨号')


@bp.post('/demo/advance')
def advance():
    clock.advance(get_db(),g.actor,values().get('seconds'))
    if json_mode(): return jsonify(ok=True,effective_at=clock.iso_now())
    return redirect(url_for('tickets'))
