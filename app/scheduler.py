"""定时调度器 —— 一个后台线程，每 30 秒检查一次有没有任务到点。

设计取舍：
1. **不引 APScheduler**：需求就是「每天/每小时跑一次」，一条线程 + cron 解析足够；
2. **不补跑历史**：服务停了两天，重启后只补跑一次，而不是把错过的十几次全补上 ——
   运营日报补十条没有意义，看最新一条就行；
3. **单进程假设**：uvicorn 单 worker 场景下安全。多 worker 部署需要加分布式锁，
   这一点写在 README 的已知限制里，不装作解决了。
"""

from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime

from app import db
from app.core.cron import Cron, CronError


class Scheduler:
    def __init__(self, tick_seconds: int = 30):
        self.tick_seconds = tick_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = "stopped"
        self.last_tick: str | None = None
        self.last_error: str = ""
        self.executed_count = 0

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ecom-scheduler", daemon=True)
        self._thread.start()
        self.state = "running"

    def stop(self) -> None:
        self._stop.set()
        self.state = "stopped"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 线程不能因为单次异常而退出
                self.last_error = traceback.format_exc(limit=2)[-300:]
            self._stop.wait(self.tick_seconds)

    # ---- 核心 ----
    def tick(self) -> list[dict]:
        """检查所有启用中的任务，到点就执行。返回本次执行记录。"""
        if not self._lock.acquire(blocking=False):
            return []  # 上一轮还没跑完，跳过本轮
        executed: list[dict] = []
        try:
            now = datetime.now().replace(second=0, microsecond=0)
            self.last_tick = now.isoformat(timespec="seconds")

            for sched in db.list_schedules():
                if not sched["enabled"]:
                    continue
                try:
                    cron = Cron(sched["cron"])
                except CronError as exc:
                    self.last_error = f"任务 {sched['name']} cron 非法: {exc}"
                    continue

                anchor = self._anchor(sched)
                next_at = cron.next_after(anchor)
                if not next_at or next_at > now:
                    continue

                executed.append(self._execute(sched, now))
            return executed
        finally:
            self._lock.release()

    @staticmethod
    def _anchor(sched: dict) -> datetime:
        """计算下次执行时的参照点：上次执行时间，没有就用创建时间。

        cron 表达的是「本地时间」，而库里的时间可能带时区（UTC）。
        这里统一转成本地 naive，否则 next_at - now 会直接抛
        "can't subtract offset-naive and offset-aware datetimes"。
        """
        raw = sched.get("last_run_at") or sched.get("created_at")
        if not raw:
            return datetime.now()
        try:
            dt = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return datetime.now()
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt

    def _execute(self, sched: dict, now: datetime) -> dict:
        from app.orchestrator import run_stages

        full = db.get_schedule(sched["id"])
        payload = (full or {}).get("payload") or {}
        if not payload:
            # 任务没存 payload = 每次跑内置演示数据（演示任务用）
            from app.demo import build_demo_payload

            payload = build_demo_payload()
        started_at = now.isoformat(timespec="seconds")
        try:
            result = run_stages(
                payload,
                schedule_id=sched["id"],
                trigger="scheduler",
            )
            db.touch_schedule(sched["id"], result["run_id"], started_at)
            self.executed_count += 1
            self.last_error = ""
            return {
                "schedule_id": sched["id"],
                "name": sched["name"],
                "run_id": result["run_id"],
                "status": result["status"],
                "at": started_at,
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001
            db.touch_schedule(sched["id"], "", started_at)  # 推进时间，避免每分钟重试
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {
                "schedule_id": sched["id"],
                "name": sched["name"],
                "run_id": None,
                "status": "error",
                "at": started_at,
                "ok": False,
                "error": self.last_error,
            }

    # ---- 状态 ----
    def status(self) -> dict:
        tasks = []
        now = datetime.now().replace(second=0, microsecond=0)
        for sched in db.list_schedules():
            try:
                cron = Cron(sched["cron"])
                next_at = cron.next_after(self._anchor(sched))
                describe = cron.describe()
                valid = True
            except CronError as exc:
                next_at, describe, valid = None, str(exc), False

            tasks.append(
                {
                    **sched,
                    "describe": describe,
                    "valid": valid,
                    "next_run_at": next_at.isoformat(timespec="seconds") if next_at else None,
                    "seconds_to_next": int((next_at - now).total_seconds()) if next_at else None,
                }
            )

        return {
            "state": self.state,
            "tick_seconds": self.tick_seconds,
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "executed_count": self.executed_count,
            "server_time": now.isoformat(timespec="seconds"),
            "tasks": tasks,
            "due_count": sum(
                1 for t in tasks if t.get("enabled") and t.get("seconds_to_next") == 0
            ),
        }


_scheduler = Scheduler()


def get_scheduler() -> Scheduler:
    return _scheduler


def autostart() -> None:
    """服务启动时调用。设 ECOM_SCHEDULER=off 可关闭（测试环境用）。"""
    if (os.getenv("ECOM_SCHEDULER") or "on").strip().lower() in ("off", "0", "false", "no"):
        return
    _scheduler.start()
