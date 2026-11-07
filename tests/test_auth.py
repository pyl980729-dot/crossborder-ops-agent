"""身份与权限测试。

重点验证三件事：
1. 密码不能明文存、不能错配
2. 权限矩阵的边界（运营不能改核心阈值这种）
3. 多店铺数据不串（这是最容易出事的隔离点）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.core import auth

db.init_db()


class TestPassword(unittest.TestCase):
    def test_hash_is_not_plaintext(self):
        h = auth.hash_password("secret123")
        self.assertNotIn("secret123", h)
        self.assertIn("$", h)

    def test_verify(self):
        h = auth.hash_password("secret123")
        self.assertTrue(auth.verify_password("secret123", h))
        self.assertFalse(auth.verify_password("secret124", h))
        self.assertFalse(auth.verify_password("", h))

    def test_salt_is_random(self):
        """同样的密码两次哈希必须不同 —— 否则彩虹表能批量破解。"""
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_malformed_stored_hash(self):
        self.assertFalse(auth.verify_password("x", "not-a-valid-hash"))


class TestPermissions(unittest.TestCase):
    def test_owner_can_everything(self):
        for perm in auth.PERMISSIONS:
            self.assertTrue(auth.can("owner", perm), perm)

    def test_operator_restrictions(self):
        self.assertTrue(auth.can("operator", "run"))
        self.assertTrue(auth.can("operator", "upload"))
        self.assertTrue(auth.can("operator", "config_content"))
        # 这几个是负责人的保留权限
        self.assertFalse(auth.can("operator", "config_core"))
        self.assertFalse(auth.can("operator", "delete_data"))
        self.assertFalse(auth.can("operator", "manage_members"))
        self.assertFalse(auth.can("operator", "approve_action"))

    def test_viewer_read_only(self):
        self.assertTrue(auth.can("viewer", "view"))
        for perm in ("run", "upload", "config_core", "config_content", "delete_data"):
            self.assertFalse(auth.can("viewer", perm), perm)

    def test_config_group_mapping(self):
        """广告/市场/定价的阈值会改变「谁被判失败」，必须是 core。"""
        self.assertEqual(auth.permission_for_config_group("广告诊断"), "config_core")
        self.assertEqual(auth.permission_for_config_group("市场调研"), "config_core")
        self.assertEqual(auth.permission_for_config_group("选品定价"), "config_core")
        # 文案类只影响产出形态
        self.assertEqual(auth.permission_for_config_group("Listing"), "config_content")
        self.assertEqual(auth.permission_for_config_group("短视频"), "config_content")

    def test_principal_helpers(self):
        p = auth.Principal(role="operator")
        self.assertFalse(p.is_admin)
        self.assertEqual(p.role_label, "运营")
        self.assertTrue(p.can("run"))
        self.assertFalse(p.can("manage_members"))

    def test_anonymous_is_viewer(self):
        p = auth.anonymous()
        self.assertEqual(p.role, "viewer")
        self.assertFalse(p.authenticated)
        self.assertEqual(p.via, "anonymous")


class TestSession(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(auth.session_valid(auth.session_expiry(1)))

    def test_expired(self):
        self.assertFalse(auth.session_valid("2000-01-01T00:00:00"))

    def test_garbage(self):
        self.assertFalse(auth.session_valid("not a date"))


class TestStoreIsolation(unittest.TestCase):
    """多店铺隔离：这是最容易出事的地方。"""

    def setUp(self):
        self.s1, self.s2 = "test_store_a", "test_store_b"
        for sid in (self.s1, self.s2):
            if not db.get_store(sid):
                db.create_store(sid, f"测试店 {sid}", "shopee_br")

    def tearDown(self):
        import sqlite3

        conn = sqlite3.connect(db.DB_PATH)
        for sid in (self.s1, self.s2):
            conn.execute("DELETE FROM runs WHERE store_id = ?", (sid,))
            conn.execute("DELETE FROM stores WHERE id = ?", (sid,))
        conn.commit()
        conn.close()

    def test_runs_do_not_leak_across_stores(self):
        rid1 = "run_for_a_" + db.new_run_id()[:6]
        rid2 = "run_for_b_" + db.new_run_id()[:6]
        for rid, sid in ((rid1, self.s1), (rid2, self.s2)):
            db.save_run(rid, "测试", "ok", ["market"], {}, {"run_id": rid, "store_id": sid},
                        10, "rule", store_id=sid)

        a_ids = [r["id"] for r in db.list_runs(limit=50, store_id=self.s1)]
        b_ids = [r["id"] for r in db.list_runs(limit=50, store_id=self.s2)]
        self.assertIn(rid1, a_ids)
        self.assertNotIn(rid2, a_ids)
        self.assertIn(rid2, b_ids)
        self.assertNotIn(rid1, b_ids)

    def test_settings_are_per_store(self):
        db.save_settings({"ads.ctr_bench": 0.09}, store_id=self.s1)
        db.save_settings({"ads.ctr_bench": 0.01}, store_id=self.s2)
        self.assertAlmostEqual(float(db.get_settings(self.s1)["ads.ctr_bench"]), 0.09)
        self.assertAlmostEqual(float(db.get_settings(self.s2)["ads.ctr_bench"]), 0.01)
        # 清掉，避免影响其他测试
        db.delete_setting("ads.ctr_bench", self.s1)
        db.delete_setting("ads.ctr_bench", self.s2)

    def test_dataset_cross_store_read_denied(self):
        did = "ds_" + db.new_run_id()[:6]
        db.save_dataset(did, "ads", "t.csv", [{"campaign": "x"}], {}, store_id=self.s1)
        self.assertIsNotNone(db.load_dataset_rows(did, self.s1))
        # 用另一个店铺的身份读，必须读不到
        self.assertIsNone(db.load_dataset_rows(did, self.s2))
        db.delete_dataset(did, store_id=self.s1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
