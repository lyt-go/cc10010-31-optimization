"""输出列校验数据快照的回归测试（标准库 unittest，直接跑：python -m unittest）。

覆盖：输出列解析、数量/顺序/类型判定、模板下载再载入、别名映射、重复载入幂等、
失败文件不覆盖上一可用快照、并发载入只生效一个版本、元数据存储两侧读取同一快照。
"""
from __future__ import annotations

import copy
import json
import threading
import unittest

from app.services import columnsnapshot as cs
from app.services.metastore import MetastoreService
from app.services.sqltpl import SqltplService
from app.store import Store, store


BASE_METASTORE = [
    {
        "id": 1, "status": "草稿", "pending": True, "abnormal": False,
        "表编号": "META-0001", "表名": "dwd_order_detail", "所属数仓层": "DWD",
        "字段数量": 5,
        "目标表字段": "order_id:bigint|order_no:varchar(64)|amount:decimal(18,2)|paid:boolean|created_at:timestamp",
    },
]

GOOD_SQL = "SELECT order_id, order_no, amount, paid, created_at FROM ods_order WHERE dt = ${dt}"
GOOD_TPL = {
    "模板编号": "SQLT-9001", "模板名称": "订单明细", "模板类型": "增量同步",
    "SQL片段": GOOD_SQL, "参数占位符": "dt", "参数默认值": "dt:date",
    "预期输出列": "order_id, order_no, amount, paid, created_at",
    "关联元数据表": "dwd_order_detail", "目标数仓层": "DWD",
}


def reset_store() -> None:
    """每个用例一份干净的内存仓储（保留种子里的示范快照）。"""
    store._tables = {
        "metastore": copy.deepcopy(BASE_METASTORE),
        "sqltpl": [],
        "columnsnapshot": [],
    }


class ParserTests(unittest.TestCase):
    def test_aliases_and_implicit_names(self) -> None:
        selection = cs.parse_selection(
            "SELECT a.order_id AS 订单号, b.amount 金额, c.paid, d.created_at "
            "FROM a JOIN b ON a.id=b.id"
        )
        self.assertEqual(selection.error, "")
        self.assertEqual([c.name for c in selection.columns], ["订单号", "金额", "paid", "created_at"])

    def test_expression_always_needs_alias(self) -> None:
        selection = cs.parse_selection("SELECT sum(amount) FROM orders")
        self.assertTrue(selection.error)

    def test_star_rejected(self) -> None:
        self.assertTrue(cs.parse_selection("SELECT * FROM orders").error)
        self.assertTrue(cs.parse_selection("SELECT o.* FROM orders o").error)

    def test_type_declaration_in_alias(self) -> None:
        selection = cs.parse_selection("SELECT amount:decimal(18,2) AS amt, paid FROM t")
        self.assertEqual(selection.columns[0].name, "amt")
        self.assertEqual(selection.columns[0].data_type, "decimal(18,2)")

    def test_parameter_type_filled_from_declaration(self) -> None:
        selection = cs.parse_selection("SELECT id, dt FROM t")
        cs.apply_parameter_types(selection.columns, "dt", "dt:date")
        self.assertEqual(selection.columns[1].data_type, "date")

    def test_type_normalization_and_compat(self) -> None:
        self.assertEqual(cs.normalize_type("VARCHAR(64)"), "文本")
        self.assertEqual(cs.normalize_type("DECIMAL(18, 2)"), "小数")
        self.assertEqual(cs.normalize_type("TIMESTAMPTZ"), "时间戳")
        self.assertTrue(cs.type_compatible("整数", "小数"))
        self.assertTrue(cs.type_compatible("整数", "文本"))
        self.assertFalse(cs.type_compatible("文本", "整数"))
        self.assertTrue(cs.type_compatible("未知", "文本"))  # 无法判定时交给阈值
        self.assertFalse(cs.type_compatible("整数", "文本", policy={"类型兼容": False}))

    def test_legacy_type_codes(self) -> None:
        self.assertEqual(cs.decode_type("I"), "整数")
        self.assertEqual(cs.decode_type("decimal(18,2)"), "小数")


class CompareTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.service = SqltplService()

    def target(self) -> cs.TargetTable:
        return cs.TargetTable("dwd_order_detail", "DWD", [
            cs.TargetField("order_id", "整数"),
            cs.TargetField("order_no", "文本"),
            cs.TargetField("amount", "小数"),
            cs.TargetField("paid", "布尔"),
            cs.TargetField("created_at", "时间戳"),
        ])

    def columns(self, sql: str) -> list[cs.ColumnOutput]:
        selection = cs.parse_selection(sql)
        return [cs.ColumnOutput(c.name, c.data_type) for c in selection.columns]

    def test_good_sql_passes(self) -> None:
        result = cs.compare_columns(self.columns(GOOD_SQL), self.target())
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["目标字段顺序"], ["order_id", "order_no", "amount", "paid", "created_at"])

    def test_wrong_order_fails_even_same_names(self) -> None:
        sql = "SELECT order_no, order_id, amount, paid, created_at FROM t"
        result = cs.compare_columns(self.columns(sql), self.target())
        self.assertFalse(result["passed"])
        self.assertIn("字段顺序超出阈值", result["问题"])

    def test_missing_and_extra_columns_fail_by_count(self) -> None:
        sql = "SELECT order_id, order_no, amount FROM t"
        result = cs.compare_columns(self.columns(sql), self.target())
        self.assertFalse(result["passed"])
        self.assertIn("字段数量超出阈值", result["问题"])

    def test_alias_mismatch_has_no_mapping(self) -> None:
        sql = "SELECT order_id, order_no, amount, paid, created_at, order_id AS order_key FROM t"
        result = cs.compare_columns(self.columns(sql), self.target())
        self.assertFalse(result["passed"])
        self.assertTrue(any(d["类型"] == "字段映射" for d in result["差异"]))
        self.assertEqual(result["目标字段顺序"], [])

    def test_type_range_enforced(self) -> None:
        sql = "SELECT order_id:date AS order_id, order_no, amount, paid, created_at FROM t"
        result = cs.compare_columns(self.columns(sql), self.target())
        self.assertFalse(result["passed"])
        self.assertIn("字段类型超出阈值", result["问题"])

    def test_legacy_metadata_count_only(self) -> None:
        row = {"表名": "legacy", "字段数量": 5}
        target = cs.resolve_target_table({"1": row}, "legacy")
        self.assertEqual(len(target.fields), 5)
        result = cs.compare_columns(self.columns(GOOD_SQL), target)
        # 历史数据没有字段名/类型，按占位字段只校验数量
        self.assertIn("字段数量", result)
        self.assertEqual(result["目标字段数量"], 5)


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.service = SqltplService()
        self.meta = MetastoreService()

    def create(self, **overrides) -> dict:
        values = {**GOOD_TPL, **overrides}
        entry, missing = self.service.create_entry(values)
        self.assertEqual(missing, [])
        return entry

    def snapshots(self) -> list[dict]:
        return [cs.normalize_row(row) for row in store.snapshot_rows()]

    def test_publish_creates_snapshot_and_blocks_bad_columns(self) -> None:
        entry = self.create()
        published, message, snapshot = self.service.run_action(entry["id"], "发布模板")
        self.assertIsNotNone(published, message)
        self.assertEqual(snapshot["校验状态"], "通过")
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.snapshots()[0]["字段一致性"], "一致")

        bad = self.create(模板编号="SQLT-9002", SQL片段=(
            "SELECT order_no, order_id, amount, paid, created_at FROM t"
        ))
        blocked, message, snapshot = self.service.run_action(bad["id"], "发布模板")
        self.assertIsNone(blocked)
        self.assertIn("不能保存为发布版本", message)
        self.assertEqual(snapshot["校验状态"], "失败")
        self.assertEqual(bad["status"], "草稿")

    def test_duplicate_load_creates_one_version(self) -> None:
        self.service.import_template({"template": GOOD_TPL})
        self.service.import_template({"values": GOOD_TPL})
        self.service.import_template(json.dumps(GOOD_TPL))
        self.assertEqual(len(self.snapshots()), 1)

    def test_reload_published_template_revokes_publish(self) -> None:
        entry = self.create()
        self.service.run_action(entry["id"], "发布模板")
        changed = {**GOOD_TPL, "SQL片段": (
            "SELECT order_no, order_id, amount, paid, created_at FROM t"
        )}
        result = self.service.import_template({"template": changed})
        self.assertFalse(result["ok"])
        self.assertEqual(result["entry"]["status"], "模板评审中")
        self.assertTrue(result["entry"]["abnormal"])

    def test_reload_same_content_does_not_revoke(self) -> None:
        entry = self.create()
        self.service.run_action(entry["id"], "发布模板")
        package, _ = self.service.export_template(entry["id"])
        result = self.service.import_template(package)
        self.assertTrue(result["reused"])
        self.assertEqual(self.service.get_entry(entry["id"])["status"], "已发布")

    def test_same_file_rechecks_after_target_table_fix(self) -> None:
        broken = {**GOOD_TPL, "SQL片段": "SELECT order_id, order_no, amount, paid FROM t"}
        first = self.service.import_template({"template": broken})
        self.assertFalse(first["ok"])
        self.meta.update_columns(1, {"目标表字段": (
            "order_id:bigint|order_no:varchar(64)|amount:decimal(18,2)|paid:boolean"
        )})
        second = self.service.import_template({"template": broken})
        self.assertTrue(second["ok"])
        self.assertFalse(second["reused"])
        self.assertNotEqual(second["snapshot"]["校验版本"], first["snapshot"]["校验版本"])

    def test_declared_alias_conflict_uses_sql_alias(self) -> None:
        """声明的预期输出列与 SQL 别名冲突时，以别名映射为准并留下告警。"""
        conflicted = {**GOOD_TPL, "模板编号": "SQLT-9004", "SQL片段": (
            "SELECT order_id AS order_key, order_no, amount, paid, created_at FROM t"
        )}
        entry, _ = self.service.create_entry(conflicted)
        validation = self.service.validate_entry(entry)
        self.assertFalse(validation["passed"])
        self.assertTrue(any("以 SQL 别名为准" in item for item in validation["告警"]))
        self.assertTrue(any(d["输出列"] == "order_key" for d in validation["差异"]))

    def test_declared_order_differs_from_alias_order_warns(self) -> None:
        reordered_sql = {**GOOD_TPL, "SQL片段": (
            "SELECT order_no, order_id, amount, paid, created_at FROM t"
        )}
        entry, _ = self.service.create_entry(reordered_sql)
        validation = self.service.validate_entry(entry)
        # 预期输出列还是目标表顺序，但 SQL 实际顺序变了：判定按 SQL，结论失败并提示冲突
        self.assertFalse(validation["passed"])
        self.assertTrue(validation["告警"])
        self.assertEqual(validation["输出列"], ["order_no", "order_id", "amount", "paid", "created_at"])

    def test_redownload_and_reload_keeps_single_version(self) -> None:
        entry = self.create()
        self.service.run_action(entry["id"], "发布模板")
        package, _ = self.service.export_template(entry["id"])
        first = self.service.import_template(package)
        self.assertTrue(first["reused"])
        self.assertEqual(len(self.snapshots()), 1)
        # 下载 -> 再载入 -> 再下载，仍只有一个校验版本
        second_package, _ = self.service.export_template(entry["id"])
        self.service.import_template(second_package)
        self.assertEqual(len(self.snapshots()), 1)

    def test_reload_with_changed_column_order_is_new_version(self) -> None:
        self.service.import_template({"template": GOOD_TPL})
        reordered = {**GOOD_TPL, "SQL片段": (
            "SELECT order_no, order_id, amount, paid, created_at FROM ods_order WHERE dt = ${dt}"
        ), "预期输出列": "order_no, order_id, amount, paid, created_at"}
        reordered["模板编号"] = "SQLT-9002"
        result = self.service.import_template({"template": reordered})
        self.assertFalse(result["ok"])
        self.assertEqual(result["snapshot"]["校验状态"], "失败")
        self.assertEqual(len(self.snapshots()), 2)

    def test_failed_file_does_not_cover_previous_usable_snapshot(self) -> None:
        self.service.import_template({"template": GOOD_TPL})
        usable = len(self.snapshots())
        broken = {**GOOD_TPL, "SQL片段": "SELECT order_id, order_no, amount, paid FROM t"}
        result = self.service.import_template({"template": broken})
        self.assertFalse(result["ok"])
        # 失败版本不会成为可用版本，上一可用快照仍在
        latest = cs.latest_for_template("SQLT-9001")
        previous = cs.usable_for_template("SQLT-9001")
        self.assertEqual(latest["校验状态"], "失败")
        self.assertIsNotNone(previous)
        self.assertTrue(previous["passed"])
        self.assertEqual(previous["校验版本"], 1)
        # 失败版本存在但不可用，且不会被当成回用目标
        self.assertEqual(len(self.snapshots()), usable + 1)
        repeated = self.service.import_template({"template": broken})
        self.assertTrue(repeated["reused"])
        self.assertFalse(repeated["ok"])
        self.assertEqual(len(self.snapshots()), usable + 1)

    def test_invalid_file_writes_nothing(self) -> None:
        result = self.service.import_template({"template": {"模板编号": "SQLT-X"}})
        self.assertFalse(result["ok"])
        self.assertEqual(self.snapshots(), [])

    def test_concurrent_load_only_one_version(self) -> None:
        results: list[dict] = []
        barrier = threading.Barrier(8)

        def work(index: int) -> None:
            barrier.wait()
            results.append(self.service.import_template({"template": GOOD_TPL}))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.snapshots()), 1)
        self.assertTrue(all(item["ok"] for item in results))

    def test_metastore_reads_same_snapshot_and_columns(self) -> None:
        entry = self.create(输出列类型="bigint|varchar(64)|decimal(18,2)|boolean|timestamp")
        self.service.run_action(entry["id"], "发布模板")
        template = self.service.get_entry(entry["id"])
        metastore = self.meta.get_entry(1)
        self.assertIn("SQLT-9001", metastore["引用模板"])
        reference = metastore["输出列校验快照"][0]
        self.assertEqual(reference["校验版本"], template["校验版本"])
        self.assertEqual(reference["字段一致性"], "一致")
        # 两侧读到的列顺序、类型完全一致
        snapshot = cs.active_for_template("SQLT-9001")
        meta_snapshot = cs.normalize_row(
            next(row for row in store.snapshot_rows() if row.get("关联模板") == "SQLT-9001")
        )
        self.assertEqual(snapshot["目标字段顺序"], meta_snapshot["目标字段顺序"])
        self.assertEqual(
            [cs.normalize_type(t) for t in cs.split_list(snapshot["输出列类型"])],
            ["整数", "文本", "小数", "布尔", "时间戳"],
        )
        self.assertEqual(
            [cs.normalize_type(t) for t in cs.split_list(meta_snapshot["目标表字段类型"])],
            [cs.normalize_type(t) for t in cs.split_list(snapshot["输出列类型"])],
        )

    def test_metastore_field_change_revokes_published_template(self) -> None:
        entry = self.create()
        self.service.run_action(entry["id"], "发布模板")
        self.meta.run_action(1, "确认元数据")
        updated, message, feed = self.meta.update_columns(1, {"目标表字段": (
            "order_id:bigint|order_no:varchar(64)|amount:decimal(18,2)|paid:boolean"
        )})
        self.assertIsNotNone(updated, message)
        self.assertIsNotNone(feed)
        template = self.service.get_entry(entry["id"])
        self.assertEqual(template["status"], "模板评审中")
        self.assertEqual(template["字段一致性"], "待重新校验")
        # 旧快照被标记作废，不再作为可用版本
        self.assertIsNone(cs.usable_for_template("SQLT-9001"))

    def test_confirm_metadata_blocked_on_inconsistent_snapshot(self) -> None:
        bad = {**GOOD_TPL, "模板编号": "SQLT-9003", "SQL片段": (
            "SELECT order_no, order_id, amount, paid, created_at FROM t"
        )}
        self.service.import_template({"template": bad})
        result, message = self.meta.run_action(1, "确认元数据")
        self.assertIsNone(result)
        self.assertIn("不允许确认元数据", message)


if __name__ == "__main__":
    unittest.main()
