"""核心口径测试：正确汇总 / 错误对照 / 重复计数 / 口径配置 / 导出。

运行：python3 tests/test_core.py  或  python3 -m unittest discover -s tests
"""
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))

import db  # noqa: E402
import queries  # noqa: E402
import server  # noqa: E402

NO_FILTER = {'date_from': '', 'date_to': '', 'store': '', 'channel': ''}
CAL = {'refund_date_basis': 'refund_date', 'blank_channel': 'keep'}


def fresh():
    return db.build_db()


def by_key(rows, *dims):
    out = {}
    for r in rows:
        k = tuple(r[d] for d in dims)
        out[k[0] if len(k) == 1 else k] = r
    return out


class CorrectSummaryTest(unittest.TestCase):
    def test_group_by_date(self):
        con = fresh()
        res = queries.correct_summary(con, ['date'], NO_FILTER, CAL)
        rows = by_key(res['rows'], 'date')
        # 11-10：订单 120 / 退款 10（R9001 当日）/ 投放 350
        self.assertEqual(rows['2026-11-10']['order_amount'], 120.0)
        self.assertEqual(rows['2026-11-10']['order_cnt'], 4)
        self.assertEqual(rows['2026-11-10']['refund_amount'], 10.0)
        self.assertEqual(rows['2026-11-10']['spend'], 350.0)
        # 11-11：订单 30（含 0 元赠品行）/ 退款 25（R9002）/ 投放 180（含 0 记录）
        self.assertEqual(rows['2026-11-11']['order_amount'], 30.0)
        self.assertEqual(rows['2026-11-11']['refund_amount'], 25.0)
        self.assertEqual(rows['2026-11-11']['spend'], 180.0)
        # 11-12：没有订单但有退款 → 行保留；订单为「无记录」(None)，不是 0
        self.assertIn('2026-11-12', rows)
        self.assertIsNone(rows['2026-11-12']['order_amount'])
        self.assertEqual(rows['2026-11-12']['refund_amount'], 20.0)
        self.assertEqual(rows['2026-11-12']['spend'], 220.0)

    def test_missing_vs_zero(self):
        """缺少记录(None) 与 实际为零(0.0) 必须区分。"""
        con = fresh()
        res = queries.correct_summary(con, ['date', 'channel'], NO_FILTER, CAL)
        rows = by_key(res['rows'], 'date', 'channel')
        # 11-11 饿了么：赠品 0 元订单 + 0 元投放记录 → 实际为零
        self.assertEqual(rows[('2026-11-11', '饿了么')]['order_amount'], 0.0)
        self.assertEqual(rows[('2026-11-11', '饿了么')]['spend'], 0.0)
        # 11-12 饿了么：什么记录都没有 → 键不出现
        self.assertNotIn(('2026-11-12', '饿了么'), rows)
        # 11-10 空白渠道：有订单 50，无投放记录 → 投放 None
        self.assertEqual(rows[('2026-11-10', '（空白）')]['order_amount'], 50.0)
        self.assertIsNone(rows[('2026-11-10', '（空白）')]['spend'])

    def test_spend_not_split_by_store(self):
        """按门店分组时投放不可拆分，且绝不复制到每家门店。"""
        con = fresh()
        res = queries.correct_summary(con, ['date', 'store', 'channel'],
                                      NO_FILTER, CAL)
        self.assertFalse(res['spend_splittable'])
        for r in res['rows']:
            self.assertNotIn('spend', r)  # 行内根本没有投放列
        # 不按门店分组时可对齐
        res2 = queries.correct_summary(con, ['date', 'channel'], NO_FILTER, CAL)
        self.assertTrue(res2['spend_splittable'])
        total = sum(r['spend'] or 0 for r in res2['rows'])
        self.assertEqual(total, 750.0)  # 200+150+180+0+220，无复制

    def test_store_filter_ignored_for_spend(self):
        con = fresh()
        f = {**NO_FILTER, 'store': '人民广场店'}
        cards = queries.cards(con, f, CAL)
        self.assertTrue(cards['spend']['store_filter_ignored'])
        self.assertEqual(cards['spend']['amount'], 750.0)  # 全量，未按门店过滤
        self.assertEqual(cards['orders']['amount'], 135.0)  # 120-15+30

    def test_channel_filter_blank(self):
        con = fresh()
        f = {**NO_FILTER, 'channel': '（空白）'}
        res = queries.correct_summary(con, ['date'], f, CAL)
        rows = by_key(res['rows'], 'date')
        self.assertEqual(rows['2026-11-10']['order_amount'], 50.0)
        self.assertIsNone(rows['2026-11-10']['spend'])  # 空白渠道无投放记录

    def test_refund_order_date_basis(self):
        """退款按原订单日期归属：11-12 的退款回到 11-10，但 11-12 行因投放保留。"""
        con = fresh()
        cal = {**CAL, 'refund_date_basis': 'order_date'}
        res = queries.correct_summary(con, ['date'], NO_FILTER, cal)
        rows = by_key(res['rows'], 'date')
        self.assertEqual(rows['2026-11-10']['refund_amount'], 55.0)  # 10+25+20
        self.assertIsNone(rows['2026-11-11']['refund_amount'])
        self.assertIn('2026-11-12', rows)  # 投放键仍在
        self.assertIsNone(rows['2026-11-12']['refund_amount'])
        self.assertEqual(rows['2026-11-12']['spend'], 220.0)

    def test_blank_channel_exclude(self):
        con = fresh()
        cal = {**CAL, 'blank_channel': 'exclude'}
        res = queries.correct_summary(con, ['date'], NO_FILTER, cal)
        rows = by_key(res['rows'], 'date')
        self.assertEqual(rows['2026-11-10']['order_amount'], 70.0)  # 120-50


class WrongModeTest(unittest.TestCase):
    def test_fanout_numbers(self):
        con = fresh()
        res = queries.wrong_summary(con, ['date'], NO_FILTER, CAL)
        rows = by_key(res['rows'], 'date')
        # 订单：O1001 两行各被 2 笔退款扇出 → 120+35=155
        self.assertEqual(rows['2026-11-10']['order_amount'], 155.0)
        # 退款：R9001/R9002 各被 2 条明细扇出，R9003 挂到原单日 → 10*2+25*2+20=90
        self.assertEqual(rows['2026-11-10']['refund_amount'], 90.0)
        # 投放：美团 200 被 5 个连接行扇出 + 饿了么 150 → 1150
        self.assertEqual(rows['2026-11-10']['spend'], 1150.0)
        # 11-12 没有订单 → 整行丢失
        self.assertNotIn('2026-11-12', rows)

    def test_duplication_and_dropped(self):
        con = fresh()
        dup = queries.duplication(con, NO_FILTER, CAL)
        by_label = {d['label']: d for d in dup['duplicated']}
        self.assertEqual(by_label['O1001 · 招牌奶茶']['times'], 2)
        self.assertEqual(by_label['O1001 · 加料珍珠']['times'], 2)
        self.assertEqual(by_label['O1001 · 洒漏部分退款']['times'], 2)
        self.assertEqual(by_label['O1001 · 二次投诉补退']['times'], 2)
        self.assertEqual(by_label['2026-11-10 · 美团']['times'], 5)
        dropped_labels = [d['label'] for d in dup['dropped']]
        self.assertIn('2026-11-12 · 美团', dropped_labels)  # 无订单承接被丢弃

    def test_compare_missing_keys(self):
        con = fresh()
        cmp_ = queries.compare(con, ['date'], NO_FILTER, CAL)
        self.assertIn(['2026-11-12'], cmp_['missing_keys'])
        self.assertEqual(cmp_['phantom_keys'], [])


class DetailTest(unittest.TestCase):
    def test_order_detail_bucket(self):
        con = fresh()
        bucket = {'date': '2026-11-10', 'store': '人民广场店', 'channel': '美团'}
        d = queries.detail(con, 'orders', bucket, NO_FILTER, CAL, 'correct')
        self.assertEqual(len(d['records']), 3)  # O1001 两行 + O1002 一行
        self.assertEqual(sum(r['amount'] for r in d['records']), 55.0)
        self.assertIn('SELECT', d['sql'])

    def test_spend_unsplittable_by_store(self):
        con = fresh()
        d = queries.detail(con, 'spend', {'store': '人民广场店'},
                           NO_FILTER, CAL, 'correct')
        self.assertFalse(d['splittable'])
        self.assertEqual(d['records'], [])

    def test_wrong_detail_times(self):
        con = fresh()
        d = queries.detail(con, 'orders', {'date': '2026-11-10'},
                           NO_FILTER, CAL, 'wrong')
        times = {r['line_id']: r['times'] for r in d['records']}
        self.assertEqual(times[1], 2)  # O1001 招牌奶茶被 2 笔退款扇出
        self.assertEqual(times[3], 1)


class ExportTest(unittest.TestCase):
    def test_export_zip(self):
        con = server.CON  # 服务级连接
        import threading
        with server.LOCK:
            data, name = server.build_export(
                con, 'correct', ['date'], NO_FILTER, CAL)
        self.assertTrue(name.endswith('.zip'))
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(set(z.namelist()), {'经营分析结果.csv', '口径与筛选条件.json'})
        doc = json.loads(z.read('口径与筛选条件.json').decode('utf-8'))
        self.assertEqual(doc['mode'], '正确口径（先汇总再对齐）')
        self.assertEqual(doc['filters'], NO_FILTER)
        self.assertEqual(doc['group_by'], ['日期'])
        self.assertEqual(doc['totals']['orders'], 150.0)
        self.assertTrue(doc['sql'])
        csv_text = z.read('经营分析结果.csv').decode('utf-8-sig')
        self.assertIn('订单金额', csv_text.splitlines()[0])
        self.assertIn('120.00', csv_text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
