"""经营分析示例服务：纯标准库（http.server + sqlite3 + zipfile）。

启动：python3 app/server.py  →  http://localhost:8000
"""
import io
import json
import threading
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import db
import queries

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'

CON = db.build_db()
LOCK = threading.Lock()

MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8'}


def parse_common(args):
    """解析公共参数：模式 / 筛选 / 分组 / 口径。"""
    def one(name, default=''):
        return args.get(name, [default])[0].strip()

    mode = one('mode', 'correct')
    if mode not in ('correct', 'wrong'):
        mode = 'correct'
    gb = [d for d in queries.DIMS if d in set(one('group_by', 'date').split(','))]
    if not gb:
        gb = ['date']
    caliber = {
        'refund_date_basis': one('refund_date_basis', 'refund_date'),
        'blank_channel': one('blank_channel', 'keep'),
    }
    if caliber['refund_date_basis'] not in ('refund_date', 'order_date'):
        caliber['refund_date_basis'] = 'refund_date'
    if caliber['blank_channel'] not in ('keep', 'exclude'):
        caliber['blank_channel'] = 'keep'
    filters = {
        'date_from': one('date_from'), 'date_to': one('date_to'),
        'store': one('store'), 'channel': one('channel'),
    }
    return mode, gb, filters, caliber


def summary_payload(con, mode, gb, filters, caliber):
    if mode == 'wrong':
        cmp_ = queries.compare(con, gb, filters, caliber)
        return {'mode': mode, 'group_by': gb, 'compare': cmp_,
                'spend_splittable': cmp_['spend_splittable'],
                'cards': queries.cards(con, filters, caliber),
                'notes': queries.CALIBER_NOTES}
    res = queries.correct_summary(con, gb, filters, caliber)
    daily, daily_sql = queries.daily_series(con, filters, caliber)
    steps = res['steps'] + [
        {'title': '⑤ 趋势图：同一口径按日期汇总（投放仍忽略门店筛选）',
         'sql': daily_sql}]
    return {'mode': mode, 'group_by': gb,
            'spend_splittable': res['spend_splittable'],
            'rows': res['rows'], 'daily': daily,
            'cards': queries.cards(con, filters, caliber),
            'sql_steps': steps, 'notes': queries.CALIBER_NOTES}


def build_export(con, mode, gb, filters, caliber):
    """导出 zip：结果 CSV + 本次口径与筛选条件 JSON。"""
    payload = summary_payload(con, mode, gb, filters, caliber)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    def row_key(r):
        return tuple(r.get(d) for d in gb)

    if mode == 'wrong':
        cmp_ = payload['compare']
        c_map = {row_key(r): r for r in cmp_['correct_rows']}
        w_map = {row_key(r): r for r in cmp_['wrong_rows']}
        head = ([queries.DIM_LABEL[d] for d in gb]
                + [f'{m}({s})' for m in ('订单金额', '退款金额', '投放花费')
                   for s in ('正确', '错误')])
        lines = [','.join(head)]
        for k in sorted(set(c_map) | set(w_map)):
            c, w = c_map.get(k, {}), w_map.get(k, {})
            cells = list(k)
            for f in ('order_amount', 'refund_amount', 'spend'):
                cells.append('' if c.get(f) is None else f"{c.get(f):.2f}")
                cells.append('' if w.get(f) is None else f"{w.get(f):.2f}")
            lines.append(','.join(str(x) for x in cells))
        result_csv = '\n'.join(lines)
        sql_steps = cmp_['correct_steps'] + [
            {'title': '错误示例：直接关联（实际执行）', 'sql': cmp_['wrong_sql']}]
    else:
        head = [queries.DIM_LABEL[d] for d in gb] + ['订单金额', '订单数', '退款金额',
                                                     '退款笔数', '投放花费']
        lines = [','.join(head)]
        for r in payload['rows']:
            cells = [r.get(d) for d in gb]
            for f in ('order_amount', 'order_cnt', 'refund_amount', 'refund_cnt',
                      'spend'):
                v = r.get(f)
                if f == 'spend' and not payload['spend_splittable']:
                    cells.append('不可拆分')
                elif v is None:
                    cells.append('')
                elif isinstance(v, float):
                    cells.append(f'{v:.2f}')
                else:
                    cells.append(str(v))
            lines.append(','.join(str(x) for x in cells))
        result_csv = '\n'.join(lines)
        sql_steps = payload['sql_steps']

    caliber_doc = {
        'exported_at': datetime.now().isoformat(timespec='seconds'),
        'mode': {'correct': '正确口径（先汇总再对齐）', 'wrong': '错误示例（直接关联）'}[mode],
        'caliber': {
            'refund_date_basis': {'refund_date': '按退款发生日',
                                  'order_date': '按原订单日期'}[caliber['refund_date_basis']],
            'blank_channel': {'keep': '空白渠道归入（空白）',
                              'exclude': '剔除空白渠道记录'}[caliber['blank_channel']],
        },
        'filters': filters,
        'group_by': [queries.DIM_LABEL[d] for d in gb],
        'spend_splittable_by_store': payload.get('spend_splittable'),
        'totals': {m: payload['cards'][m]['amount'] for m in ('orders', 'refunds', 'spend')},
        'notes': queries.CALIBER_NOTES,
        'sql': [{'step': s['title'], 'sql': s['sql']} for s in sql_steps],
    }
    if mode == 'wrong':
        cmp_ = payload['compare']
        caliber_doc['duplicated_records'] = cmp_['duplicated']
        caliber_doc['dropped_records'] = cmp_['dropped']
        caliber_doc['missing_keys_in_wrong_mode'] = cmp_['missing_keys']

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('经营分析结果.csv', '\ufeff' + result_csv)  # BOM 便于 Excel 打开
        z.writestr('口径与筛选条件.json',
                   json.dumps(caliber_doc, ensure_ascii=False, indent=2))
    return buf.getvalue(), f'经营分析_{ts}.zip'


class Handler(BaseHTTPRequestHandler):
    server_version = 'BizAnalysis/1.0'

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        if not path.is_file() or STATIC not in path.resolve().parents:
            self._json({'error': 'not found'}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', MIME.get(path.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, args = parsed.path, parse_qs(parsed.query)
        try:
            with LOCK:
                if path == '/':
                    self._file(STATIC / 'index.html')
                elif path.startswith('/static/'):
                    self._file((STATIC / path[len('/static/'):]).resolve())
                elif path == '/api/meta':
                    m = db.meta(CON)
                    m['notes'] = queries.CALIBER_NOTES
                    self._json(m)
                elif path == '/api/summary':
                    mode, gb, filters, caliber = parse_common(args)
                    self._json(summary_payload(CON, mode, gb, filters, caliber))
                elif path == '/api/compare':
                    mode, gb, filters, caliber = parse_common(args)
                    self._json({'mode': 'wrong', 'group_by': gb,
                                **queries.compare(CON, gb, filters, caliber)})
                elif path == '/api/detail':
                    mode, gb, filters, caliber = parse_common(args)
                    metric = args.get('metric', [''])[0]
                    if metric not in queries.METRIC_LABEL:
                        self._json({'error': 'unknown metric'}, 400)
                        return
                    bucket = {d: args.get(f'b_{d}', [None])[0] for d in queries.DIMS}
                    self._json({'metric': metric, 'bucket': bucket,
                                **queries.detail(CON, metric, bucket, filters,
                                                 caliber, mode)})
                elif path == '/api/export':
                    mode, gb, filters, caliber = parse_common(args)
                    data, name = build_export(CON, mode, gb, filters, caliber)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Disposition',
                                     "attachment; filename=export.zip; "
                                     f"filename*=UTF-8''{quote(name)}")
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._json({'error': 'not found'}, 404)
        except Exception as exc:  # noqa: BLE001 - 演示服务，错误直接回显
            self._json({'error': f'{type(exc).__name__}: {exc}'}, 500)

    def log_message(self, fmt, *a):
        pass


def main():
    import os
    port = int(os.environ.get('PORT', '8000'))
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'经营分析示例 → http://localhost:{port}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
