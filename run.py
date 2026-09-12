"""CLI ของบอท ProMaxx Report

ตัวอย่าง:
    python run.py inspect --launch          # เปิดโปรแกรมแล้ว dump หน้าจอล็อกอิน
    python run.py inspect --watch           # เฝ้าดู dump ใหม่ทุกครั้งที่หน้าจอเปลี่ยน
    python run.py run flows/login.yaml --dry-run
    python run.py run flows/login.yaml
    python run.py run flows/r14_102_export.yaml                 # export CSV (สายที่ใช้จริง)
    python run.py actions                   # ดูรายการ action ที่ใช้ใน YAML ได้
    python run.py stop                      # ปิดโปรแกรมทิ้ง
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from bot import app as app_mod
from bot import logging_setup
from bot.config import ROOT, Settings

# บังคับ UTF-8 ให้ stdout/stderr ตั้งแต่จุดแรกสุด ก่อนมี print() ใด ๆ ทั้งหมด
# ไม่พึ่งพา console codepage เพราะรันจาก .exe (PyInstaller) แล้วบางที cmd/task
# scheduler ไม่ inherit `chcp 65001` แบบเดียวกับตอนรัน python ตรง ๆ
# ทำที่นี่แทนที่จะทำใน logging_setup.setup() เพราะบาง command (เช่น actions)
# ไม่ได้เรียก setup() แต่ print() ภาษาไทยตรง ๆ
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_USAGE = 2
EXIT_ALREADY_RUNNING = 3

# --then-upload: uploader ข้ามรอบ (ไฟล์ไม่ได้อัปเดต) ไม่ใช่ error
# ใช้เลข 2 ซ้ำกับ EXIT_BAD_USAGE โดยตั้งใจ ให้ตรงกับ exit code ของ upload-products.mjs
# และ tools\run_and_upload.ps1 — แยกกันได้จาก log เพราะ bad usage เกิดก่อน flow ใด ๆ จะรัน
EXIT_UPLOAD_SKIPPED = 2


def _boot(args) -> tuple[Settings, "logging_setup.logging.Logger"]:
    cfg = Settings.load()
    log = logging_setup.setup(
        cfg.resolve_path("paths.logs", "logs"),
        level="DEBUG" if getattr(args, "verbose", False) else cfg.get("logging.level", "INFO"),
        secrets=cfg.secret_values(),
        max_bytes=cfg.get("logging.max_bytes", 2_000_000),
        backup_count=cfg.get("logging.backup_count", 10),
    )
    return cfg, log


# ---------------------------------------------------------------- inspect


def cmd_inspect(args) -> int:
    from bot import inspector

    cfg, log = _boot(args)
    application = app_mod.from_settings(cfg)
    out_dir = Path(args.out) if args.out else cfg.resolve_path("paths.logs", "logs") / "inspect"

    if args.launch:
        application.start_or_attach("restart" if args.restart else "attach")
    else:
        if not application.is_running():
            log.error(
                "โปรแกรมยังไม่เปิด ใส่ --launch เพื่อให้เปิดให้ หรือเปิดเองก่อนแล้วสั่งใหม่"
            )
            return EXIT_FAILED
        application.attach()

    application.wait_first_window()
    inspector.dump(application, out_dir, tag=args.tag)

    if args.watch:
        inspector.watch(application, out_dir, interval=args.interval,
                        duration=args.duration)
    return EXIT_OK


# ---------------------------------------------------------------- run


def _write_last_run(cfg, args, started, status: str, exit_code: int,
                    error: str | None, outputs: list) -> None:
    """เขียนสรุปผลรอบล่าสุดลง logs/last_run.json

    ให้สคริปต์อัปโหลดหรือ dashboard เช็คได้ว่ารอบล่าสุดสำเร็จไหม
    และได้ไฟล์อะไรออกมาบ้าง โดยไม่ต้องไปไล่อ่าน log
    """
    finished = datetime.now()
    payload = {
        "status": status,
        "exit_code": exit_code,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "flows": list(args.flows),
        "dry_run": bool(args.dry_run),
        "outputs": outputs,
        "error": error,
    }
    try:
        path = cfg.resolve_path("paths.logs", "logs") / "last_run.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        logging_setup.get_logger().info("บันทึกสรุปรอบล่าสุด: %s", path)
    except Exception as exc:
        logging_setup.get_logger().warning("เขียน last_run.json ไม่สำเร็จ: %s", exc)


def cmd_run(args) -> int:
    from bot.runner import FlowRunner, SingleInstanceLock

    cfg, log = _boot(args)
    started = datetime.now()

    lock = SingleInstanceLock("BOTR05106_promaxx_bot")
    if not args.allow_concurrent and not lock.acquire():
        log.error("มีบอทตัวอื่นกำลังรันอยู่ ยกเลิกรอบนี้")
        _write_last_run(cfg, args, started, "already_running",
                        EXIT_ALREADY_RUNNING, "มีบอทตัวอื่นกำลังรันอยู่", [])
        return EXIT_ALREADY_RUNNING

    runner = None
    try:
        runner = FlowRunner(cfg, dry_run=args.dry_run)
        for flow_path in args.flows:
            runner.run_file(flow_path)

        issues = runner.ctx.dry_run_issues
        if issues:
            # ปกติที่ dry-run จะตรวจ step ท้าย ๆ ไม่ได้ เพราะไม่ได้กดอะไรจริง
            # สถานะของโปรแกรมจึงไม่เดินหน้า - ไม่ถือว่า flow ผิด
            log.warning("dry-run: มี %d step ที่ตรวจไม่ได้ (ดูรายละเอียดด้านล่าง)",
                        len(issues))
            for issue in issues:
                log.warning("  - %s", issue.splitlines()[0])
            log.warning("ถ้า step ที่ตรวจไม่ได้เป็นพวกที่ต้องรอผลจากการกด "
                        "ถือว่าปกติ แต่ถ้าเป็น step ที่หา control ไม่เจอ "
                        "ให้แก้ locator ในไฟล์ flow")

        log.info("จบทุก flow เรียบร้อย")
        _write_last_run(cfg, args, started, "ok", EXIT_OK, None,
                        runner.ctx.outputs)

        # อัปโหลดต่อท้ายในงานเดียวกัน - เวลาที่ไฟล์ export ออกไม่แน่นอน
        # ตั้ง Task แยกไว้เวลาตายตัวแล้ว uploader จะข้ามเพราะเห็นไฟล์ของเมื่อวาน
        if getattr(args, "then_upload", False):
            if args.dry_run:
                log.info("ข้ามการอัปโหลด เพราะ flow รันแบบ --dry-run (ไม่มีไฟล์ใหม่)")
                return EXIT_OK
            from bot.upload import UPLOAD_OK, UPLOAD_SKIPPED, run_upload

            rc = run_upload(cfg, runner.ctx.outputs,
                            dry_run=getattr(args, "upload_dry_run", False))
            if rc == UPLOAD_OK:
                return EXIT_OK
            return EXIT_UPLOAD_SKIPPED if rc == UPLOAD_SKIPPED else EXIT_FAILED

        return EXIT_OK
    except Exception as exc:
        log.error("flow ล้มเหลว: %s", exc)
        log.debug("%s", traceback.format_exc())
        _write_last_run(cfg, args, started, "failed", EXIT_FAILED, str(exc),
                        runner.ctx.outputs if runner else [])
        return EXIT_FAILED
    finally:
        lock.release()


# ---------------------------------------------------------------- อื่น ๆ


def cmd_actions(args) -> int:
    from bot.actions import REGISTRY

    print(f"action ที่ใช้ได้ในไฟล์ flow YAML ({len(REGISTRY)} ตัว)\n")
    for name in sorted(REGISTRY):
        doc = (REGISTRY[name].__doc__ or "").strip().splitlines()
        print(f"  {name:<18} {doc[0] if doc else ''}")
    return EXIT_OK


def cmd_stop(args) -> int:
    cfg, log = _boot(args)
    application = app_mod.from_settings(cfg)
    if not application.is_running():
        log.info("โปรแกรมไม่ได้เปิดอยู่")
        return EXIT_OK
    application.stop()
    return EXIT_OK


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="บอท automation สำหรับ ProMaxx Report (PowerBuilder)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="log ระดับ DEBUG")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("inspect", help="dump โครงสร้าง control ของหน้าจอปัจจุบัน")
    i.add_argument("--launch", action="store_true", help="เปิดโปรแกรมให้ถ้ายังไม่เปิด")
    i.add_argument("--restart", action="store_true", help="ปิดของเดิมแล้วเปิดใหม่")
    i.add_argument("--watch", action="store_true", help="เฝ้าดูและ dump ทุกครั้งที่หน้าจอเปลี่ยน")
    i.add_argument("--interval", type=float, default=1.0, help="ความถี่ตรวจในโหมด watch (วินาที)")
    i.add_argument("--duration", type=float, default=300.0, help="เฝ้าดูนานสุดกี่วินาที")
    i.add_argument("--tag", default="snapshot", help="คำนำหน้าชื่อไฟล์ผลลัพธ์")
    i.add_argument("--out", help="โฟลเดอร์ปลายทาง (ค่าเริ่มต้น logs/inspect)")
    i.set_defaults(func=cmd_inspect)

    r = sub.add_parser("run", help="รันไฟล์ flow YAML")
    r.add_argument("flows", nargs="+", help="ไฟล์ flow เรียงตามลำดับที่จะรัน")
    r.add_argument("--dry-run", action="store_true",
                   help="หา control และตรวจ flow แต่ไม่กด/ไม่พิมพ์อะไรจริง")
    r.add_argument("--allow-concurrent", action="store_true",
                   help="ข้ามการกันบอทซ้อน (ปกติไม่ควรใช้)")
    r.add_argument("--then-upload", action="store_true",
                   help="flow สำเร็จแล้วอัปโหลดไฟล์เข้า Supabase ต่อ (ใช้ upload-products.mjs)")
    r.add_argument("--upload-dry-run", action="store_true",
                   help="ใช้คู่กับ --then-upload: ตรวจไฟล์อย่างเดียว ไม่เขียน Supabase")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("actions", help="แสดงรายการ action ที่ใช้ใน YAML ได้")
    a.set_defaults(func=cmd_actions)

    s = sub.add_parser("stop", help="ปิด promaxxreport.exe")
    s.set_defaults(func=cmd_stop)

    return p


# ---------------------------------------------------------- ดับเบิลคลิก

# flow ที่รันเมื่อถูกดับเบิลคลิก - เปลี่ยนได้ใน settings.yaml (app.default_flow)
# โดยไม่ต้อง build .exe ใหม่
DEFAULT_FLOW = "flows/r14_102_export.yaml"

# ดับเบิลคลิกแล้วอัปโหลดเข้า Supabase ต่อด้วย (settings.yaml: app.default_then_upload)
# ปิดไว้จนกว่าจะมีตัวอัปโหลดของ R14.102 - upload-products.mjs ตัวปัจจุบันเขียนไว้สำหรับ
# R05.106 (ตาราง products) และรับไฟล์ CSV ที่ flow เพิ่งสร้างซึ่งคนละหัวคอลัมน์กัน
DEFAULT_THEN_UPLOAD = False


def _double_clicked() -> bool:
    """ถูกดับเบิลคลิกจาก Explorer หรือถูกสั่งจาก terminal?

    ดับเบิลคลิก = Explorer สร้าง console ใหม่ให้ มีแค่ process ของเราเกาะอยู่
    สั่งจาก terminal = shell เกาะ console นั้นอยู่ก่อนแล้ว จึงนับได้ตั้งแต่ 2 ขึ้นไป

    ใช้แยกว่าจะเติม flow ตั้งต้นให้เองหรือไม่ - ดับเบิลคลิกใส่ argument ไม่ได้
    ส่วนตอนสั่งจาก terminal ต้องเคารพ argument ที่ผู้ใช้พิมพ์มาเสมอ
    """
    if not getattr(sys, "frozen", False):
        return False  # รันจาก python ตรง ๆ ไม่ใช่ดับเบิลคลิกแน่นอน
    try:
        from ctypes import c_uint, windll

        buf = (c_uint * 4)()
        return windll.kernel32.GetConsoleProcessList(buf, 4) == 1
    except Exception:
        return False


def _default_run_args() -> list[str]:
    """argument ที่ใช้แทนตอนถูกดับเบิลคลิก - ปรับได้ใน settings.yaml ไม่ต้อง build ใหม่"""
    flow, then_upload = DEFAULT_FLOW, DEFAULT_THEN_UPLOAD
    try:
        cfg = Settings.load()
        flow = cfg.get("app.default_flow", DEFAULT_FLOW)
        then_upload = bool(cfg.get("app.default_then_upload", DEFAULT_THEN_UPLOAD))
    except Exception:
        pass  # settings.yaml พังก็ยังต้องรันได้ด้วยค่าตั้งต้น
    args = ["run", flow]
    if then_upload:
        args.append("--then-upload")
    return args


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # ดับเบิลคลิกไม่มีทางใส่ argument ได้ จึงเติม flow ตั้งต้นให้เอง
    # ไม่งั้น argparse จะฟ้อง "required: command" แล้วหน้าต่างปิดทันทีจนอ่านไม่ทัน
    #
    # จบแล้วปิดหน้าต่างไปเลย ไม่ค้างรอกดปุ่ม - ดูผลย้อนหลังได้ที่
    # logs\last_run.json (สรุปผลรอบล่าสุด) และ logs\bot.log (log เต็ม)
    if not argv and _double_clicked():
        argv = _default_run_args()
        print(f"เปิดจากการดับเบิลคลิก - จะรัน {' '.join(argv[1:])}\n")

    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except KeyboardInterrupt:
        print("\nยกเลิกโดยผู้ใช้", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
