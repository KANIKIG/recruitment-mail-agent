from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import time

from .config import ROOT, Settings, load_dotenv
from .calendar import LarkCalendar
from .coremail import CoremailTodoClient
from .deepseek_agent import DeepSeekMailAgent
from .lark import ENTERPRISE_TYPE_OPTIONS, FIELD_ORDER, LarkBase, STATUS_OPTIONS
from .mailbox import ImapMailbox
from .state import StateStore
from .sync import backfill_flagged_todos, repair_flagged_interviews, run_sync


LEGACY_STATUS_MAP = {
    "已投递": "投递",
    "测评": "测评&AI面",
    "笔试": "笔试",
    "面试": "技术面",
    "一面": "技术面",
    "二面": "技术面",
    "终面": "主管面",
    "意向": "Offer",
    "已签约": "Offer",
    "已拒绝": "已挂",
    "已暂停": "已挂",
}
LAUNCH_AGENT_LABEL = "local.recruitment-mail-agent.watcher"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
AUTOSTART_RUNTIME_ROOT = Path.home() / "Library" / "Application Support" / "RecruitmentMailAgent"
WATCHER_PID_PATH = AUTOSTART_RUNTIME_ROOT / "data" / "watcher.pid"
WATCHER_LOG_PATH = Path.home() / "Library" / "Logs" / "RecruitmentMailAgent" / "watcher.log"


def _set_env_values(values: dict[str, str]) -> None:
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in values:
                output.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def cmd_init_base(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=False, require_mail=False)
    lark = LarkBase(settings.lark_cli)
    app_token, table_id = lark.create_base(args.name)
    _set_env_values({"LARK_BASE_TOKEN": app_token, "LARK_TABLE_ID": table_id})
    print(json.dumps({"base_token": app_token, "table_id": table_id}, ensure_ascii=False, indent=2))
    try:
        LarkBase(settings.lark_cli, app_token, table_id).create_default_views()
        print("已创建“进展看板”视图。")
    except RuntimeError as exc:
        print(f"提示：主表已创建，但附加视图创建失败，可稍后手动建立：{exc}")
    print("已写入 .env。请在飞书中按需要添加视图或手动维护记录。")
    return 0


def cmd_init_views(_: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True, require_mail=False)
    LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id).create_default_views()
    print("已创建“进展看板”视图。")
    return 0


def _cell_text(value: object) -> str:
    if isinstance(value, list):
        return _cell_text(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "")
    return str(value or "")


def cmd_simplify_table(_: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True, require_mail=False)
    lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
    fields = {item["name"]: item for item in lark.list_fields() if item.get("name") and item.get("id")}
    records = lark.list_records()

    updates: dict[str, dict[str, object]] = {}
    for record in records:
        patch: dict[str, object] = {}
        if "投递项目" in fields:
            company = _cell_text(record.fields.get("公司") or record.fields.get("公司名称"))
            if company:
                patch["投递项目"] = company
        status_field = "当前进展" if "当前进展" in fields else "流程状态"
        current = _cell_text(record.fields.get(status_field))
        mapped = LEGACY_STATUS_MAP.get(current)
        if mapped:
            patch[status_field] = mapped
        effective_status = mapped or current
        if "投递时间" in fields and effective_status == "投递" and not record.fields.get("投递时间"):
            submitted_at = record.fields.get("更新时间") or record.fields.get("最近邮件时间")
            if submitted_at:
                patch["投递时间"] = submitted_at
        if patch:
            updates[record.record_id] = patch
    lark.batch_update_records(updates)

    views = {item.get("name"): item.get("id") for item in lark.list_views()}
    if views.get("待人工确认"):
        lark.delete_view(str(views["待人工确认"]))

    if "投递项目" in fields:
        lark.update_field(str(fields["投递项目"]["id"]), {"name": "公司名称", "type": "text"})
    if "岗位" in fields:
        lark.update_field(str(fields["岗位"]["id"]), {"name": "岗位名称", "type": "text"})
    status_source = fields.get("当前进展") or fields.get("流程状态")
    if status_source:
        lark.update_field(str(status_source["id"]), {
            "name": "流程状态",
            "type": "select",
            "multiple": False,
            "options": STATUS_OPTIONS,
        })
    time_source = fields.get("最近邮件时间")
    if time_source:
        lark.update_field(str(time_source["id"]), {
            "name": "更新时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"},
        })
    if "投递时间" not in fields:
        lark.create_fields([{"name": "投递时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}}])
    if "截止时间" not in fields:
        lark.create_fields([{"name": "截止时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}}])
    if "企业类型" not in fields:
        lark.create_fields([{"name": "企业类型", "type": "select", "options": ENTERPRISE_TYPE_OPTIONS}])

    # 刷新字段列表，保证重复执行时只删仍然存在的旧列。
    fields = {item["name"]: item for item in lark.list_fields() if item.get("name") and item.get("id")}
    removable = [
        "公司", "手动锁定", "最近邮件主题", "发件人", "自动识别置信度",
        "待人工确认", "同步键", "邮件 Message-ID", "最近同步时间", "备注",
    ]
    for name in removable:
        item = fields.get(name)
        if item:
            lark.delete_field(str(item["id"]))
            time.sleep(2)
    remaining = [str(item.get("name")) for item in lark.list_fields()]
    expected = FIELD_ORDER
    if set(remaining) != set(expected):
        raise RuntimeError(f"表格字段未完全精简，当前字段：{remaining}")
    for view in lark.list_views():
        if view.get("id") and view.get("type") in {"grid", "kanban"}:
            lark.set_view_sort(str(view["id"]), "更新时间", descending=True)
        if view.get("id") and view.get("type") == "grid":
            lark.set_view_visible_fields(str(view["id"]), FIELD_ORDER)
    print(f"表格已精简为 7 列，保留并迁移 {len(records)} 行。")
    return 0


def cmd_backfill_company_types(_: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True, require_mail=False)
    lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
    fields = {item.get("name"): item for item in lark.list_fields() if item.get("name")}
    definition = {
        "name": "企业类型",
        "type": "select",
        "multiple": False,
        "options": ENTERPRISE_TYPE_OPTIONS,
    }
    existing_field = fields.get("企业类型")
    if existing_field and existing_field.get("id"):
        lark.update_field(str(existing_field["id"]), definition)
    else:
        lark.create_fields([definition])

    records = lark.list_records()
    pending: list[tuple[str, str]] = []
    for record in records:
        company = _cell_text(record.fields.get("公司名称") or record.fields.get("公司")).strip()
        current_type = _cell_text(record.fields.get("企业类型")).strip()
        if company and company != "待确认公司" and not current_type:
            pending.append((record.record_id, company))

    companies = list(dict.fromkeys(company for _, company in pending))
    company_types = DeepSeekMailAgent(settings).classify_company_types(companies)
    updates = {
        record_id: {"企业类型": company_types[company]}
        for record_id, company in pending
    }
    lark.batch_update_records(updates)
    print(json.dumps({
        "records": len(records),
        "companies_classified": len(companies),
        "records_updated": len(updates),
        "records_skipped": len(records) - len(updates),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_arrange_fields(_: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True, require_mail=False)
    lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
    existing = {str(item.get("name")) for item in lark.list_fields()}
    missing = [name for name in FIELD_ORDER if name not in existing]
    if missing:
        raise RuntimeError(f"无法调整列顺序，缺少字段：{missing}")
    updated = 0
    for view in lark.list_views():
        if view.get("id") and view.get("type") == "grid":
            lark.set_view_visible_fields(str(view["id"]), FIELD_ORDER)
            updated += 1
    if not updated:
        raise RuntimeError("未找到可调整列顺序的表格视图")
    print(f"已调整 {updated} 个表格视图的列顺序：{' → '.join(FIELD_ORDER)}")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    problems: list[str] = []
    load_dotenv(ROOT / ".env")
    try:
        settings = Settings.from_env(require_targets=False, require_mail=False)
    except ValueError as exc:
        print(f"[FAIL] 配置：{exc}")
        return 1
    cli_path = ROOT / settings.lark_cli if settings.lark_cli.startswith("./") else Path(settings.lark_cli)
    cli_exists = cli_path.exists() or shutil.which(settings.lark_cli) is not None
    print(f"[{'OK' if cli_exists else 'FAIL'}] lark-cli: {settings.lark_cli}")
    if not cli_exists:
        problems.append("请执行 npm install")
    if not settings.email:
        print("[FAIL] IMAP 邮箱：尚未配置")
        problems.append("执行 ./tracker configure-email 你的邮箱地址")
    else:
        try:
            socket.getaddrinfo(settings.imap_host, settings.imap_port)
            print(f"[OK] IMAP 地址：{settings.imap_host}:{settings.imap_port}")
        except OSError as exc:
            print(f"[FAIL] IMAP 地址：{exc}")
            problems.append("检查网络或 IMAP 主机")
        try:
            count = ImapMailbox(settings).check_connection()
            print(f"[OK] IMAP 邮箱登录：INBOX 共 {count} 封邮件（只读拉取）")
        except Exception as exc:
            print(f"[FAIL] IMAP 邮箱登录：{exc}")
            problems.append("确认已开启 IMAP，并使用客户端专用密码")
        if settings.coremail_todo_enabled:
            try:
                CoremailTodoClient(settings).login()
                print("[OK] Coremail 原生截止待办")
            except Exception as exc:
                print(f"[FAIL] Coremail 原生截止待办：{exc}")
                problems.append("检查 COREMAIL_WEB_URL，且确认该密码也可登录网页邮箱")
    if settings.deepseek_api_key:
        print(f"[OK] DeepSeek Agent：{settings.deepseek_model}")
    else:
        print("[FAIL] DeepSeek Agent：缺少 DEEPSEEK_API_KEY")
        problems.append("在 .env 中填写 DEEPSEEK_API_KEY")
    if cli_exists:
        completed = subprocess.run([str(cli_path), "auth", "status"], capture_output=True, text=True)
        print(f"[{'OK' if completed.returncode == 0 else 'FAIL'}] 飞书授权")
        if completed.returncode != 0:
            problems.append("执行 npm run lark -- config init --new 和 npm run lark -- auth login --recommend")
    targets = bool(settings.lark_base_token and settings.lark_table_id)
    if targets:
        try:
            records = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id).list_records()
            print(f"[OK] 飞书智能表格：当前 {len(records)} 条记录")
        except Exception as exc:
            print(f"[FAIL] 飞书智能表格：{exc}")
            problems.append("检查飞书授权或表格 ID")
    else:
        print("[WAIT] 智能表格 ID")
    if cli_exists and settings.lark_calendar_enabled:
        try:
            LarkCalendar(settings).check_connection()
            print("[OK] 飞书主日历")
        except Exception as exc:
            print(f"[FAIL] 飞书主日历：{exc}")
            problems.append("补充飞书日历读写授权")
    if problems:
        print("\n待处理：")
        for problem in problems:
            print(f"- {problem}")
        return 1
    return 0


def cmd_configure_email(args: argparse.Namespace) -> int:
    address = args.email.strip().lower()
    if "@" not in address:
        raise ValueError("请输入完整邮箱地址")
    example = ROOT / ".env.example"
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    _set_env_values({
        "IMAP_EMAIL": address,
    })
    print("邮箱地址已写入 .env。请在 .env 中填写 IMAP_PASSWORD 和 IMAP_HOST。")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True)
    stats = run_sync(settings, dry_run=args.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_backfill_todos(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True)
    if not settings.coremail_todo_enabled:
        raise ValueError("请先启用 COREMAIL_TODO_ENABLED")
    stats = backfill_flagged_todos(settings, dry_run=args.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_repair_interviews(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True)
    if not settings.lark_calendar_enabled:
        raise ValueError("请先在 .env 中启用 LARK_CALENDAR_ENABLED=true")
    lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
    fields = {item.get("name"): item for item in lark.list_fields() if item.get("name")}
    status_field = fields.get("流程状态") or fields.get("当前进展")
    if not status_field or not status_field.get("id"):
        raise RuntimeError("飞书表格缺少流程状态字段")
    if not args.dry_run:
        lark.update_field(str(status_field["id"]), {
            "name": "流程状态",
            "type": "select",
            "multiple": False,
            "options": STATUS_OPTIONS,
        })
    stats = repair_flagged_interviews(settings, dry_run=args.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("重建会删除飞书现有记录和本地游标；确认后请加 --yes")
    settings = Settings.from_env(require_targets=True)
    lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
    records = lark.list_records()
    lark.delete_records([record.record_id for record in records])
    state = StateStore(settings.database_path)
    try:
        state.reset()
    finally:
        state.close()
    print(f"已删除飞书 {len(records)} 行并清空本地分类游标，开始重新识别。", flush=True)
    # 删除后飞书列表接口可能短暂返回已删除记录；已知目标为空时直接从空索引重建。
    stats = run_sync(settings, initial_records=[])
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _watcher_pid() -> int | None:
    try:
        return int(WATCHER_PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _watcher_command(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _watcher_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    command = _watcher_command(pid)
    if command:
        return "autumn_tracker.cli watch" in command
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # 受限宿主不允许探测进程，但 PID 存在且权限被拒绝时进程仍在。
        return True
    except ProcessLookupError:
        return False


def _launch_agent_service() -> str:
    return f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"


def _launch_agent_definition(interval: int) -> dict[str, object]:
    python_bin = os.getenv("TRACKER_PYTHON")
    if not python_bin:
        python_bin = "/opt/homebrew/bin/python3" if Path("/opt/homebrew/bin/python3").exists() else sys.executable
    executable_dirs = [str(Path(python_bin).parent)]
    node_bin = shutil.which("node")
    if node_bin:
        executable_dirs.append(str(Path(node_bin).parent))
    executable_dirs.extend(["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    launch_path = ":".join(dict.fromkeys(executable_dirs))
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            python_bin,
            "-m",
            "autumn_tracker.cli",
            "watch",
            "--interval",
            str(interval),
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(WATCHER_LOG_PATH),
        "StandardErrorPath": str(WATCHER_LOG_PATH),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(AUTOSTART_RUNTIME_ROOT / "src"),
            "LARK_CLI": str(AUTOSTART_RUNTIME_ROOT / "node_modules" / ".bin" / "lark-cli"),
            "PATH": launch_path,
        },
    }


def _launch_agent_loaded() -> bool:
    completed = subprocess.run(
        ["launchctl", "print", _launch_agent_service()],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _bootout_launch_agent() -> None:
    if _launch_agent_loaded():
        subprocess.run(
            ["launchctl", "bootout", _launch_agent_service()],
            capture_output=True,
            text=True,
        )


def _stop_watcher_process() -> None:
    pid = _watcher_pid()
    if _watcher_is_running(pid) and pid is not None:
        os.kill(pid, signal.SIGTERM)
    WATCHER_PID_PATH.unlink(missing_ok=True)


def cmd_watch(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_targets=True)
    interval = args.interval or settings.sync_interval_seconds
    if interval < 60:
        raise ValueError("同步间隔不得少于 60 秒")
    current_pid = os.getpid()
    existing = _watcher_pid()
    if existing != current_pid and _watcher_is_running(existing):
        raise RuntimeError(f"项目 watcher 已在运行（PID {existing}）")
    WATCHER_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHER_PID_PATH.write_text(f"{current_pid}\n", encoding="utf-8")
    print(f"项目 watcher 已启动：每 {interval} 秒增量同步一次。", flush=True)
    try:
        while True:
            started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            try:
                stats = run_sync(settings)
                print(json.dumps({"time": started_at, **stats}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(f"[{started_at}] 同步失败：{exc}", file=sys.stderr, flush=True)
            time.sleep(interval)
    finally:
        if _watcher_pid() == current_pid:
            WATCHER_PID_PATH.unlink(missing_ok=True)


def cmd_start(args: argparse.Namespace) -> int:
    existing = _watcher_pid()
    if _watcher_is_running(existing):
        print(f"项目 watcher 已在运行（PID {existing}）。")
        return 0
    settings = Settings.from_env(require_targets=True)
    interval = args.interval or settings.sync_interval_seconds
    if interval < 60:
        raise ValueError("同步间隔不得少于 60 秒")
    WATCHER_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    with WATCHER_LOG_PATH.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "autumn_tracker.cli", "watch", "--interval", str(interval)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=environment,
        )
    WATCHER_PID_PATH.write_text(f"{process.pid}\n", encoding="utf-8")
    print(f"项目 watcher 已在后台启动（PID {process.pid}，每 {interval} 秒）。")
    print(f"日志：{WATCHER_LOG_PATH}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    pid = _watcher_pid()
    if _watcher_is_running(pid):
        print(f"项目 watcher 正在运行（PID {pid}）。")
        return 0
    print("项目 watcher 未运行。")
    return 1


def cmd_stop(_: argparse.Namespace) -> int:
    if _launch_agent_loaded():
        _bootout_launch_agent()
        WATCHER_PID_PATH.unlink(missing_ok=True)
        print("项目 watcher 已停止；登录自启配置仍保留，下次登录时会再次启动。")
        return 0
    pid = _watcher_pid()
    if not _watcher_is_running(pid):
        WATCHER_PID_PATH.unlink(missing_ok=True)
        print("项目 watcher 未运行。")
        return 0
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    WATCHER_PID_PATH.unlink(missing_ok=True)
    print(f"项目 watcher 已停止（PID {pid}）。")
    return 0


def cmd_install_autostart(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise RuntimeError("install-autostart 当前仅支持 macOS launchd")
    settings = Settings.from_env(require_targets=True)
    interval = args.interval or settings.sync_interval_seconds
    if interval < 60:
        raise ValueError("同步间隔不得少于 60 秒")
    _bootout_launch_agent()
    _stop_watcher_process()
    runtime_database = AUTOSTART_RUNTIME_ROOT / "data" / "tracker.sqlite3"
    runtime_database.parent.mkdir(parents=True, exist_ok=True)
    if settings.database_path.exists() and not runtime_database.exists():
        shutil.copy2(settings.database_path, runtime_database)
    _set_env_values({"TRACKER_DATABASE_PATH": str(runtime_database)})
    AUTOSTART_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    runtime_source = AUTOSTART_RUNTIME_ROOT / "src"
    runtime_node_modules = AUTOSTART_RUNTIME_ROOT / "node_modules"
    shutil.rmtree(runtime_source, ignore_errors=True)
    shutil.rmtree(runtime_node_modules, ignore_errors=True)
    shutil.copytree(ROOT / "src", runtime_source)
    shutil.copytree(ROOT / "node_modules", runtime_node_modules, symlinks=True)
    runtime_env = AUTOSTART_RUNTIME_ROOT / ".env"
    shutil.copy2(ROOT / ".env", runtime_env)
    runtime_env.chmod(0o600)
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_AGENT_PATH.open("wb") as handle:
        plistlib.dump(_launch_agent_definition(interval), handle, sort_keys=True)
    LAUNCH_AGENT_PATH.chmod(0o644)
    completed = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"无法加载登录自启：{detail or 'launchctl bootstrap 失败'}")
    subprocess.run(
        ["launchctl", "enable", _launch_agent_service()],
        capture_output=True,
        text=True,
    )
    print(f"已安装登录自启：{LAUNCH_AGENT_PATH}")
    print(f"本地运行目录：{AUTOSTART_RUNTIME_ROOT}")
    print(f"启动日志：{WATCHER_LOG_PATH}")
    print(f"项目 watcher 每 {interval} 秒运行一次；系统仅负责登录后启动和异常拉起。")
    return 0


def cmd_autostart_status(_: argparse.Namespace) -> int:
    installed = LAUNCH_AGENT_PATH.exists()
    loaded = _launch_agent_loaded()
    pid = _watcher_pid()
    running = _watcher_is_running(pid)
    print(json.dumps({
        "installed": installed,
        "loaded": loaded,
        "watcher_running": running,
        "pid": pid if running else None,
        "plist": str(LAUNCH_AGENT_PATH),
    }, ensure_ascii=False, indent=2))
    return 0 if installed and loaded and running else 1


def cmd_uninstall_autostart(_: argparse.Namespace) -> int:
    _bootout_launch_agent()
    _stop_watcher_process()
    LAUNCH_AGENT_PATH.unlink(missing_ok=True)
    print("已卸载登录自启并停止项目 watcher。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IMAP 邮箱 → 飞书招聘流程追踪")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="检查邮箱、飞书和本地配置")
    doctor.set_defaults(handler=cmd_doctor)
    configure_email = subparsers.add_parser("configure-email", help="配置 IMAP 邮箱地址（不保存密码）")
    configure_email.add_argument("email")
    configure_email.set_defaults(handler=cmd_configure_email)
    init_base = subparsers.add_parser("init-base", help="创建飞书智能表格和字段")
    init_base.add_argument("--name", default="招聘流程管理")
    init_base.set_defaults(handler=cmd_init_base)
    init_views = subparsers.add_parser("init-views", help="为现有主表补建默认视图")
    init_views.set_defaults(handler=cmd_init_views)
    simplify = subparsers.add_parser("simplify-table", help="迁移并精简为七列表格")
    simplify.set_defaults(handler=cmd_simplify_table)
    company_types = subparsers.add_parser("backfill-company-types", help="新增企业类型列并补齐现有记录")
    company_types.set_defaults(handler=cmd_backfill_company_types)
    arrange_fields = subparsers.add_parser("arrange-fields", help="按默认顺序排列飞书表格字段")
    arrange_fields.set_defaults(handler=cmd_arrange_fields)
    sync = subparsers.add_parser("sync", help="执行一次增量同步")
    sync.add_argument("--dry-run", action="store_true", help="只分类并显示动作，不写飞书或本地游标")
    sync.set_defaults(handler=cmd_sync)
    backfill_todos = subparsers.add_parser("backfill-todos", help="为起始日期后的已标记邮件补建截止待办")
    backfill_todos.add_argument("--dry-run", action="store_true", help="只统计候选邮件，不创建邮箱待办")
    backfill_todos.set_defaults(handler=cmd_backfill_todos)
    repair_interviews = subparsers.add_parser(
        "repair-interviews",
        help="兼容命令：重识别星标笔试/面试邮件并补建日程",
    )
    repair_interviews.add_argument("--dry-run", action="store_true", help="只重新识别并统计，不写表格、待办或日历")
    repair_interviews.set_defaults(handler=cmd_repair_interviews)
    repair_calendar = subparsers.add_parser(
        "repair-calendar",
        help="重识别星标笔试/面试邮件并补建日程",
    )
    repair_calendar.add_argument("--dry-run", action="store_true", help="只重新识别并统计，不写表格、待办或日历")
    repair_calendar.set_defaults(handler=cmd_repair_interviews)
    rebuild = subparsers.add_parser("rebuild", help="清空记录并从起始日期重新识别")
    rebuild.add_argument("--yes", action="store_true", help="确认删除当前飞书记录和本地同步状态")
    rebuild.set_defaults(handler=cmd_rebuild)
    watch = subparsers.add_parser("watch", help="在前台按固定间隔持续增量同步")
    watch.add_argument("--interval", type=int, default=None, help="同步间隔秒数，默认读取 .env")
    watch.set_defaults(handler=cmd_watch)
    start = subparsers.add_parser("start", help="启动项目内后台 watcher")
    start.add_argument("--interval", type=int, default=None, help="同步间隔秒数，默认读取 .env")
    start.set_defaults(handler=cmd_start)
    status = subparsers.add_parser("status", help="查看项目 watcher 状态")
    status.set_defaults(handler=cmd_status)
    stop = subparsers.add_parser("stop", help="停止项目 watcher")
    stop.set_defaults(handler=cmd_stop)
    install_autostart = subparsers.add_parser("install-autostart", help="安装 macOS 登录自启")
    install_autostart.add_argument("--interval", type=int, default=None, help="同步间隔秒数，默认读取 .env")
    install_autostart.set_defaults(handler=cmd_install_autostart)
    autostart_status = subparsers.add_parser("autostart-status", help="查看 macOS 登录自启状态")
    autostart_status.set_defaults(handler=cmd_autostart_status)
    uninstall_autostart = subparsers.add_parser("uninstall-autostart", help="卸载 macOS 登录自启")
    uninstall_autostart.set_defaults(handler=cmd_uninstall_autostart)
    return parser


def main() -> None:
    try:
        args = build_parser().parse_args()
        raise SystemExit(args.handler(args))
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
