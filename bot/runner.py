"""อ่านไฟล์ flow YAML แล้วรันทีละ step"""

from __future__ import annotations

import time
import traceback
from pathlib import Path

import win32api
import win32event
import winerror

from . import actions, win
from .app import from_settings
from .config import ROOT, load_flow
from .context import Context, StepError
from .logging_setup import get_logger
from .watchdog import Watchdog

log = get_logger()

FLOW_KEYS = {"name", "description", "on_error", "timeout", "steps", "_path"}
# กันไฟล์ flow เรียกกันเองวนเป็นวงกลม - สาย R14.102 ซ้อนกันจริง 8 ชั้น
# (export → confirm → year → warehouse_select → warehouse → generate → r14_102 → r14_1 → login)
# จึงเผื่อไว้ 12 ทุก flow เรียก run_flow เป็นขั้นแรก ถ้าวนจะชนเพดานก่อนกดอะไรจริง
MAX_FLOW_DEPTH = 12


class FlowError(Exception):
    pass


class SingleInstanceLock:
    """กันบอทสองตัวรันทับกันตอน Task Scheduler ยิงซ้อน"""

    def __init__(self, name: str):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        self.handle = win32event.CreateMutex(None, True, self.name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle:
            try:
                win32event.ReleaseMutex(self.handle)
                win32api.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


# ------------------------------------------------------------------ ตรวจไฟล์


def validate_flow(flow: dict) -> list[dict]:
    unknown = set(flow) - FLOW_KEYS
    if unknown:
        raise FlowError(f"ไฟล์ flow มีคีย์ที่ไม่รู้จักที่ระดับบนสุด: {sorted(unknown)}")

    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise FlowError("ไฟล์ flow ต้องมี 'steps' เป็นลิสต์ที่ไม่ว่าง")

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise FlowError(f"step ที่ {i} ต้องเป็น mapping ไม่ใช่ {type(step).__name__}")
        name = step.get("action")
        if not name:
            raise FlowError(f"step ที่ {i} ไม่มีคีย์ 'action'")
        if name not in actions.REGISTRY:
            raise FlowError(
                f"step ที่ {i} ใช้ action {name!r} ที่ไม่มีอยู่จริง\n"
                f"  action ที่ใช้ได้: {sorted(actions.REGISTRY)}"
            )
        extra = set(step) - actions.ALLOWED_KEYS[name]
        if extra:
            raise FlowError(
                f"step ที่ {i} (action: {name}) มีคีย์ที่ใช้ไม่ได้: {sorted(extra)}\n"
                f"  คีย์ที่ใช้ได้กับ action นี้: {sorted(actions.ALLOWED_KEYS[name])}"
            )
    return steps


# ------------------------------------------------------------------ ตัวรัน


def _run_steps(ctx: Context, flow: dict) -> None:
    steps = validate_flow(flow)
    on_error = str(flow.get("on_error", "abort")).lower()
    flow_name = str(flow.get("name", "flow"))
    prev_flow, prev_index, prev_step_name = ctx.flow_name, ctx.step_index, ctx.step_name
    ctx.flow_name = flow_name

    log.info("=" * 70)
    log.info("เริ่ม flow %r (%d ขั้นตอน)%s", flow_name, len(steps),
             "  [dry-run: ไม่กดอะไรจริง]" if ctx.dry_run else "")
    if flow.get("description"):
        log.info("  %s", flow["description"])
    log.info("=" * 70)

    try:
        for i, step in enumerate(steps):
            ctx.step_index = i
            ctx.step_name = str(step.get("name", ""))
            label = f"[{i}/{len(steps) - 1}] {step['action']}"
            if ctx.step_name:
                label += f" - {ctx.step_name}"
            log.info("%s", label)

            try:
                _run_one(ctx, step)
            except Exception as exc:
                if step.get("optional"):
                    log.warning("  ข้าม step นี้ (optional): %s", exc)
                    continue
                if ctx.dry_run:
                    # dry-run ไม่กดอะไรจริง หลายสถานะจึงไปถึงไม่ได้
                    # บันทึกไว้แล้วไปต่อ เพื่อให้ตรวจ step ที่เหลือได้ครบ
                    issue = f"{flow_name} step {i} ({step['action']}): {exc}"
                    ctx.dry_run_issues.append(issue)
                    log.warning("  dry-run ตรวจไม่ผ่าน: %s", exc)
                    continue
                # flow ซ้อนกันหลายชั้น ข้อผิดพลาดจะไหลผ่านทุกชั้นขึ้นมา
                # จึงรายงานหลักฐานแค่ชั้นในสุดชั้นเดียว ไม่งั้น log จะซ้ำหลายรอบ
                if not getattr(exc, "botr_reported", False):
                    _report_failure(ctx, step, exc)

                if on_error == "continue":
                    log.warning("  on_error: continue - ไปต่อ step ถัดไป")
                    continue

                message = (f"flow {flow_name!r} หยุดที่ step {i} "
                           f"(action: {step['action']})\n{exc}")
                note = crash_note(ctx)
                if note and note.strip() not in message:
                    message += note

                error = FlowError(message)
                error.botr_reported = True
                raise error from exc

            pause = float(step.get("pause", ctx.cfg.get("runner.step_pause", 0.2)))
            if pause > 0:
                time.sleep(pause)

        log.info("flow %r สำเร็จ", flow_name)
    finally:
        ctx.flow_name, ctx.step_index, ctx.step_name = (
            prev_flow, prev_index, prev_step_name
        )


def _run_one(ctx: Context, step: dict) -> None:
    fn = actions.REGISTRY[step["action"]]
    retry = step.get("retry") or {}
    times = int(retry.get("times", 0))
    delay = float(retry.get("delay", 0.5))

    last: Exception | None = None
    for attempt in range(times + 1):
        try:
            fn(ctx, step)
            return
        except Exception as exc:
            last = exc
            if attempt < times:
                log.warning("  ล้มเหลว (ครั้งที่ %d/%d): %s - ลองใหม่ใน %.1fs",
                            attempt + 1, times + 1, exc, delay)
                time.sleep(delay)
    raise last  # type: ignore[misc]


def crash_note(ctx: Context) -> str:
    """ข้อความอธิบายเพิ่ม ถ้าโปรแกรมตายไปแล้วระหว่าง flow กำลังทำงาน

    ตอนโปรแกรม crash ข้อผิดพลาดที่โผล่มาจะเป็นระดับ Win32 เช่น
    'Invalid window handle' ซึ่งอ่านแล้วไม่รู้ว่าเกิดอะไรขึ้น
    """
    try:
        if ctx.app.pid is not None and not ctx.app.is_running():
            return (
                "\n  *** โปรแกรม ProMaxx ปิดไปแล้วหรือ crash ระหว่าง flow กำลังทำงาน ***\n"
                "  ข้อผิดพลาดข้างบนเป็นผลพลอยได้จากการสั่งงานหน้าต่างที่ตายไปแล้ว\n"
                "  ไม่ใช่สาเหตุที่แท้จริง - ดูภาพล่าสุดใน screenshots/ "
                "ว่าหน้าจอค้างตรงไหนก่อนตาย"
            )
    except Exception:
        pass
    return ""


def _report_failure(ctx: Context, step: dict, exc: Exception) -> None:
    """เก็บหลักฐานตอนพัง: ภาพหน้าจอ + โครงสร้าง control"""
    log.error("  %s ล้มเหลว: %s", step.get("action"), exc)
    log.debug("%s", traceback.format_exc())

    note = crash_note(ctx)
    if note:
        log.error("%s", note)
        return  # โปรแกรมตายแล้ว ไม่มีอะไรให้ถ่ายภาพหรือ dump

    try:
        if ctx.app.pid is None:
            return
        for hwnd in win.top_windows(ctx.app.pid):
            if not win.is_visible(hwnd):
                continue
            ctx.screenshot(hwnd, f"ERROR_{step.get('action')}")
            log.error("  โครงสร้าง control ตอนพัง (%s):\n%s",
                      hex(hwnd), win.format_tree(win.tree(hwnd)))
    except Exception as inner:
        log.debug("เก็บหลักฐานตอนพังไม่สำเร็จ: %s", inner)


def run_flow_in_context(ctx: Context, path: str | Path) -> None:
    """ใช้โดย action run_flow เพื่อเรียก flow ซ้อน"""
    if ctx.depth >= MAX_FLOW_DEPTH:
        raise FlowError(f"เรียก flow ซ้อนเกิน {MAX_FLOW_DEPTH} ชั้น สงสัยว่าวนเป็นวงกลม")
    ctx.depth += 1
    try:
        _run_steps(ctx, load_flow(path))
    finally:
        ctx.depth -= 1


class FlowRunner:
    def __init__(self, cfg, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.app = from_settings(cfg)
        self.ctx = Context(cfg=cfg, app=self.app, dry_run=dry_run)
        self.watchdog = Watchdog(cfg, self.app, self.ctx)

    def run_file(self, path: str | Path) -> None:
        flow = load_flow(path)
        log.info("โหลด flow จาก %s", flow.get("_path", path))
        self.watchdog.start()
        try:
            _run_steps(self.ctx, flow)
        finally:
            self.watchdog.stop()
