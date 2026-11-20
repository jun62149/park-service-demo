import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.exceptions import HTTPException

from .db import close_db, get_db, initialize
from .files import safe_path
from .workflow import ACTIONS, PRIORITIES, STATES, apply_action, can_view, create_ticket, get_ticket, require_view
from . import ai, clock


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(DATABASE=str(Path(app.instance_path) / 'park.sqlite3'),
        UPLOAD_ROOT=str(Path(app.instance_path) / 'uploads'), MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', DEBUG=False)
    app.config.update({key:os.environ.get(key,default) for key,default in {
        'AI_PROVIDER':'deepseek','DEEPSEEK_API_KEY':'','DEEPSEEK_BASE_URL':'https://api.deepseek.com',
        'DEEPSEEK_MODEL':'deepseek-flash','OLLAMA_BASE_URL':'http://127.0.0.1:11434',
        'OLLAMA_MODEL':'qwen3:14b','AI_TIMEOUT_SECONDS':'20',
        'FEISHU_ENABLED':'false','FEISHU_APP_ID':'','FEISHU_APP_SECRET':'',
        'FEISHU_TIMEOUT_SECONDS':'5','FEISHU_BINDINGS_FILE':''}.items()})
    app.config['DEMO_MODE']=os.environ.get('DEMO_MODE','true').lower()=='true'
    app.config['PARSE_BOOT_ID']=secrets.token_urlsafe(24)
    if config:
        app.config.update(config)
    if not app.config['FEISHU_BINDINGS_FILE']:
        app.config['FEISHU_BINDINGS_FILE'] = str(Path(app.config['DATABASE']).parent / 'feishu_bindings.json')
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config['UPLOAD_ROOT']).mkdir(parents=True, exist_ok=True)
    if not app.config.get('SECRET_KEY'):
        key_path = Path(app.instance_path) / 'session_key'
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with key_path.open('xb') as stream:
                stream.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        app.config['SECRET_KEY'] = key_path.read_bytes()
    app.teardown_appcontext(close_db)
    initialize(app)
    from .notifications import init_app as init_notifications, enabled as feishu_enabled
    init_notifications(app)
    from .phase2_routes import bp
    app.register_blueprint(bp)
    from .workspaces import bp as workspace_bp
    app.register_blueprint(workspace_bp)

    def wants_json():
        return request.accept_mimetypes.best == 'application/json'

    def payload():
        if request.is_json:
            value = request.get_json()
            if not isinstance(value, dict):
                abort(400, '请求必须为对象')
            return value
        return request.form

    @app.before_request
    def identity_and_csrf():
        if request.endpoint == 'static':
            return
        generation=get_db().execute('SELECT generation FROM demo_meta WHERE id=1').fetchone()[0]
        if session.get('demo_generation') not in (None,generation):
            session.clear()
        session['demo_generation']=generation
        if 'actor_id' not in session:
            session['actor_id'] = 1
        user = get_db().execute('SELECT * FROM users WHERE id=? AND active=1', (session['actor_id'],)).fetchone()
        if not user:
            abort(403, '演示身份无效，请清除本地会话后重试')
        g.actor = dict(user)
        session.setdefault('csrf', secrets.token_urlsafe(32))
        session.setdefault('create_nonce', secrets.token_urlsafe(32))
        session.setdefault('parse_session',secrets.token_urlsafe(32))
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('X-CSRF-Token') or request.form.get('csrf', '')
            if not isinstance(token, str) or not token.isascii() or not hmac.compare_digest(token, session['csrf']):
                abort(400, '操作校验失败，请刷新页面后重试')

    @app.context_processor
    def common():
        flags=clock.evaluate(get_db())
        return dict(actor=getattr(g, 'actor', None), csrf=session.get('csrf'), create_nonce=session.get('create_nonce'), states=STATES,
            timing_flags=flags,demo_offset=get_db().execute('SELECT offset_seconds FROM demo_clock WHERE id=1').fetchone()[0],demo_mode=app.config['DEMO_MODE'],feishu_enabled=feishu_enabled(app.config),
            actions=ACTIONS, priorities=PRIORITIES,
            users=[dict(row) for row in get_db().execute('SELECT * FROM users WHERE active=1 ORDER BY id')])

    @app.template_filter('localtime')
    def localtime(value):
        if not value:
            return '—'
        return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')

    def visible(ticket):
        ticket = dict(ticket)
        if g.actor['role'] == 'manager':
            ticket['reporter_contact'] = '***（管理只读视图脱敏）'
            ticket['original_text'] = '管理只读视图不展示原文'
            # Free text may also contain contact details; do not expose it in this view.
            for key in ('issue_description', 'summary'):
                ticket[key] = '管理只读视图不展示自由文本'
        return ticket

    @app.get('/')
    def index():
        return redirect(url_for('workspaces.workspace'))

    @app.get('/session')
    def current_session():
        return jsonify(actor=g.actor, csrf=session['csrf'], create_nonce=session['create_nonce'])

    @app.post('/role')
    def switch_role():
        try:
            uid = int(payload().get('user_id', ''))
        except (ValueError, TypeError):
            abort(400, '演示身份无效')
        if not get_db().execute('SELECT 1 FROM users WHERE id=? AND active=1', (uid,)).fetchone():
            abort(400, '演示身份无效')
        session.clear()
        session.update(actor_id=uid, csrf=secrets.token_urlsafe(32))
        if wants_json():
            return jsonify(ok=True, csrf=session['csrf'])
        return redirect(url_for('tickets'))

    @app.route('/tickets', methods=['GET', 'POST'])
    def tickets():
        if request.method == 'POST':
            data = payload()
            nonce = data.get('create_nonce', '')
            if not isinstance(nonce, str) or not nonce.isascii() or not hmac.compare_digest(nonce, session['create_nonce']):
                abort(409, '报修表单已失效或已经提交，请刷新后重试')
            run_id=data.get('extraction_run_id') or None
            if run_id:
                run=get_db().execute('SELECT * FROM extraction_runs WHERE id=? AND actor_id=? AND owner_session=?',
                    (run_id,g.actor['id'],session['parse_session'])).fetchone()
                if not run: abort(403,'解析记录不属于当前演示会话')
            tid = create_ticket(get_db(), g.actor, data, request_id=nonce,extraction_run_id=run_id)
            session['create_nonce'] = secrets.token_urlsafe(32)
            if wants_json():
                return jsonify(id=tid), 201
            flash('演示工单已创建，等待企服分派')
            return redirect(url_for('detail', ticket_id=tid))
        result = [visible(row) for row in get_db().execute('SELECT * FROM tickets ORDER BY id DESC') if can_view(g.actor, row)]
        if wants_json():
            return jsonify(tickets=result)
        return render_template('list.html', tickets=result)

    @app.get('/tickets/new')
    def new_ticket():
        if g.actor['role'] not in ('reporter', 'service'):
            abort(403, '当前身份不能创建工单')
        run_id=request.args.get('run_id'); context={}
        if run_id:
            run=get_db().execute('SELECT * FROM extraction_runs WHERE id=? AND actor_id=? AND owner_session=?',
                    (run_id,g.actor['id'],session['parse_session'])).fetchone()
            if not run: abort(403,'解析记录不属于当前演示会话')
            result=json.loads(run['normalized_result'])
            context=dict(ticket={**result['data'],'original_text':run['source_text']},extraction=result,extraction_run_id=run['id'])
        return render_template('new.html',**context)

    @app.get('/tickets/<int:ticket_id>')
    def detail(ticket_id):
        db = get_db()
        ticket = get_ticket(db, ticket_id)
        require_view(g.actor, ticket)
        events = [dict(r) for r in db.execute('SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id', (ticket_id,))]
        if g.actor['role'] == 'manager':
            for event in events:
                event.update(before_values='{}', after_values='{}', reason='管理视图隐藏自由文本')
        assignments = [dict(r) for r in db.execute('SELECT * FROM assignments WHERE ticket_id=? ORDER BY sequence', (ticket_id,))]
        completions = [dict(r) for r in db.execute('SELECT * FROM completion_submissions WHERE ticket_id=? ORDER BY round', (ticket_id,))]
        for c in completions:
            c['photos'] = [dict(r) for r in db.execute('SELECT id,original_name,is_placeholder FROM attachments WHERE submission_id=?', (c['id'],))]
            if g.actor['role'] == 'manager':
                c.update(resolution_note='管理视图隐藏自由文本', reject_reason='管理视图隐藏自由文本' if c['reject_reason'] else None)
        if g.actor['role'] == 'manager':
            for assignment in assignments:
                assignment['reason'] = '管理视图隐藏自由文本'
        flags=clock.evaluate(db)
        runs=[dict(r) for r in db.execute('SELECT provider,model,fallback_reason,duration_ms FROM extraction_runs WHERE ticket_id=?',(ticket_id,))]
        for run in runs:
            run['label']=('规则降级：'+ai.REASONS.get(run['fallback_reason'],'解析失败')) if run['fallback_reason'] else ai.LABELS.get(run['provider'],'人工填写')
        result = dict(ticket=visible(ticket), events=events, assignments=assignments, completions=completions,
                      timing=flags[ticket_id],extraction_history=runs)
        if wants_json():
            return jsonify(result)
        return render_template('detail.html', **result)

    @app.post('/tickets/<int:ticket_id>/actions/<action>')
    def action(ticket_id, action):
        apply_action(get_db(), app.config['UPLOAD_ROOT'], g.actor, ticket_id, action, payload(), request.files.getlist('photos'))
        if wants_json():
            return jsonify(ok=True, id=ticket_id)
        flash('操作成功，状态与事件已保存')
        return redirect(url_for('detail', ticket_id=ticket_id))

    @app.get('/attachments/<int:attachment_id>')
    def attachment(attachment_id):
        photo = get_db().execute('SELECT * FROM attachments WHERE id=?', (attachment_id,)).fetchone()
        if not photo:
            abort(404, '附件不存在')
        require_view(g.actor, get_ticket(get_db(), photo['ticket_id']))
        path = safe_path(app.config['UPLOAD_ROOT'], photo['generated_name'])
        if not path.is_file():
            abort(404, '附件文件不存在')
        return send_file(path, mimetype=photo['detected_type'], download_name='demo-evidence' + path.suffix, max_age=0)

    @app.after_request
    def secure_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = '请求体超过 16 MiB 上限' if error.code == 413 else error.description
        if wants_json():
            return jsonify(error=message), error.code
        return render_template('error.html', message=message, code=error.code), error.code

    @app.errorhandler(Exception)
    def internal_error(error):
        # No input, headers, environment or exception bodies in logs/responses.
        app.logger.error('操作失败，类型=%s', type(error).__name__)
        if wants_json():
            return jsonify(error='保存失败，已回滚；请刷新后重试'), 500
        return render_template('error.html', message='保存失败，已回滚；请刷新后重试', code=500), 500

    return app
