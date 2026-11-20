"""Explicit operator-only single event smoke. Never retry or print identities."""
import argparse
import json
import sys
import time
from pathlib import Path
from werkzeug.datastructures import FileStorage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from park_service import create_app
from park_service.db import get_db
from park_service.feishu import SendError
from park_service.notifications import enabled
from park_service.workflow import apply_action, create_ticket


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send-one', action='store_true', required=True)
    parser.add_argument('--event', choices=('assigned', 'completed'), default='assigned')
    parser.add_argument('--ticket-id', type=int, default=0)
    args = parser.parse_args()
    # Keep a one-message experiment away from the original Demo's running DB.
    app = create_app({'DATABASE': str(Path('instance/feishu-smoke.sqlite3').resolve()),
                      'UPLOAD_ROOT': str(Path('instance/feishu-smoke-uploads').resolve())})
    dispatcher = app.extensions['feishu_dispatcher']
    result = {'api_accepted': False, 'client_receipt_confirmed': False, 'outbox_attempts': 0,
              'isolated_smoke_database': True, 'event': args.event}
    try:
        if not enabled(app.config) or not app.config['FEISHU_APP_ID'] or not app.config['FEISHU_APP_SECRET']:
            raise SendError('missing_credentials')
        # Own the same process lease, import only backend bindings, but start no polling loop.
        dispatcher.stop_event.set()
        dispatcher.start()
        if not enabled(app.config):
            raise SendError('invalid_mapping_config')
        with app.app_context():
            db = get_db()
            target = 3 if args.event == 'assigned' else 2
            if not db.execute('SELECT 1 FROM feishu_bindings WHERE local_user_id=? AND enabled=1', (target,)).fetchone():
                raise SendError('mapping_missing')
            reporter = dict(db.execute('SELECT * FROM users WHERE id=1').fetchone())
            service = dict(db.execute('SELECT * FROM users WHERE id=2').fetchone())
            if args.event == 'assigned':
                tid = create_ticket(db, reporter, {'reporter_name': '演示报修人', 'reporter_contact': 'DEMO-ONLY',
                    'location': '演示公共走廊', 'original_text': '演示：飞书单次联调，照明灯不亮。', 'issue_description': '演示：照明灯不亮'})
                apply_action(db, app.config['UPLOAD_ROOT'], service, tid, 'assign', {
                    'version': 1, 'priority': '普通', 'priority_confirmed': 'yes', 'engineer_id': 3})
            else:
                tid = args.ticket_id
                ticket = db.execute('SELECT * FROM tickets WHERE id=?', (tid,)).fetchone()
                prior = db.execute("SELECT 1 FROM notification_outbox WHERE ticket_id=? AND kind='assigned' AND status='sent'", (tid,)).fetchone()
                if not ticket or ticket['is_demo'] != 1 or ticket['assigned_engineer_id'] != 3 or not prior:
                    raise SendError('invalid_smoke_ticket')
                if db.execute("SELECT 1 FROM notification_outbox WHERE ticket_id=? AND kind='completed'", (tid,)).fetchone():
                    raise SendError('completion_already_tested')
                if ticket['status'] not in ('pending_acceptance', 'in_progress'):
                    raise SendError('invalid_smoke_ticket')
                engineer = dict(db.execute('SELECT * FROM users WHERE id=3').fetchone())
                if ticket['status'] == 'pending_acceptance':
                    apply_action(db, app.config['UPLOAD_ROOT'], engineer, tid, 'accept', {'version': ticket['version']})
                version = db.execute('SELECT version FROM tickets WHERE id=?', (tid,)).fetchone()[0]
                with Path('tests/fixtures/demo-placeholder.png').open('rb') as photo:
                    apply_action(db, app.config['UPLOAD_ROOT'], engineer, tid, 'submit_completion',
                        {'version': version, 'resolution_note': '演示：照明检查完成，仅供飞书验收通知联调。'},
                        [FileStorage(photo, filename='demo-placeholder.png', content_type='image/png')])
            row = db.execute('SELECT id FROM notification_outbox WHERE ticket_id=? AND kind=?', (tid, args.event)).fetchone()
            dispatcher.dispatch_one(row['id'])
            saved = db.execute('SELECT status,attempts,error_code FROM notification_outbox WHERE id=?', (row['id'],)).fetchone()
            result.update(api_accepted=saved['status'] == 'sent', ticket_id=tid,
                business_state=db.execute('SELECT status FROM tickets WHERE id=?', (tid,)).fetchone()[0], notification_status=saved['status'],
                outbox_attempts=saved['attempts'], error_code=saved['error_code'])
    except SendError as error:
        result['error_code'] = error.code
    except Exception:
        result['error_code'] = 'smoke_error'
    finally:
        dispatcher.close()
    output = Path('docs/evidence/' + ('stage5-feishu-real.json' if args.event == 'assigned' else 'stage5-feishu-completion-real.json'))
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output = output.with_name(output.stem + '-attempt-' + str(time.time_ns()) + '.json')
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['api_accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
