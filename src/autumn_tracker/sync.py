from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
import hashlib
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .calendar import CalendarEventRequest, LarkCalendar
from .coremail import CoremailTodoClient, TodoRequest
from .deepseek_agent import DeepSeekMailAgent
from .lark import LarkBase, LarkRecord
from .mailbox import ImapMailbox
from .models import Classification, MailMessage
from .state import StateStore


STATUS_RANK = {
    "待确认": 0, "投递": 10, "测评&AI面": 20, "笔试": 25, "约面": 28, "技术面": 30,
    "HR面": 40, "主管面": 50, "Offer": 60,
}
TERMINAL = {"Offer", "已挂"}
FOLLOWUP_STATUSES = {"测评&AI面", "笔试", "约面", "技术面", "HR面", "主管面", "Offer", "已挂"}
TODO_STATUSES = {"测评&AI面", "笔试", "约面", "技术面", "HR面", "主管面"}
HUMAN_INTERVIEW_STATUSES = {"技术面", "HR面", "主管面"}
CALENDAR_STATUSES = HUMAN_INTERVIEW_STATUSES | {"笔试"}
CALENDAR_REVIEW_STATUSES = CALENDAR_STATUSES | {"约面"}


def should_replace_status(current: str, incoming: str, locked: bool) -> bool:
    if locked or current in TERMINAL:
        return False
    if incoming == "已挂":
        return True
    if incoming == "约面":
        return current != "约面"
    return STATUS_RANK.get(incoming, 0) > STATUS_RANK.get(current, 0)


def _record_index(records: list[LarkRecord]) -> tuple[dict[str, LarkRecord], dict[str, list[LarkRecord]]]:
    by_key: dict[str, LarkRecord] = {}
    by_company: dict[str, list[LarkRecord]] = {}
    for record in records:
        key = str(record.fields.get("同步键") or "").strip()
        company = _company_key(_cell_text(record.fields.get("公司名称") or record.fields.get("公司")))
        if key:
            by_key[key] = record
        if company:
            by_company.setdefault(company, []).append(record)
    return by_key, by_company


def _find_record(
    classification: Classification,
    by_key: dict[str, LarkRecord],
    by_company: dict[str, list[LarkRecord]],
) -> LarkRecord | None:
    if classification.source_key in by_key:
        return by_key[classification.source_key]
    if classification.company == "待确认公司":
        return None
    candidates = by_company.get(_company_key(classification.company), [])
    role = classification.role.lower()
    normalized_role = _role_key(role)
    for candidate in candidates:
        existing_role = _cell_text(candidate.fields.get("岗位名称") or candidate.fields.get("岗位")).lower()
        existing_normalized = _role_key(existing_role)
        if existing_role == role or (
            bool(normalized_role)
            and normalized_role == existing_normalized
        ) or (
            min(len(normalized_role), len(existing_normalized)) >= 6
            and (normalized_role in existing_normalized or existing_normalized in normalized_role)
        ):
            return candidate
    if len(candidates) == 1:
        existing_role = _cell_text(candidates[0].fields.get("岗位名称") or candidates[0].fields.get("岗位")).lower()
        if role == "待确认岗位" or existing_role == "待确认岗位":
            return candidates[0]
    return None


def _role_key(value: str) -> str:
    value = re.sub(r"(?:2027|27)届(?:校园招聘|校招)?", "", value.lower())
    value = re.sub(r"校招|校园招聘", "", value)
    # 招聘邮件常在中文岗位后附英文翻译；它不应让同一岗位无法匹配。
    value = re.sub(r"[（(][^）)]*[a-z][^）)]*[）)]", "", value)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def _company_key(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[（(][^）)]*[）)]", "", value)
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)
    value = re.sub(r"(?:股份有限公司|有限责任公司|集团有限公司|有限公司|股份公司|集团|公司)$", "", value)
    value = re.sub(r"科技$", "", value)
    return value


def _cell_text(value: Any) -> str:
    if isinstance(value, list):
        return _cell_text(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("value") or "")
    return str(value or "")


def _fields(message: MailMessage, classification: Classification, tz: ZoneInfo) -> dict[str, Any]:
    received = message.received_at.astimezone(tz).isoformat(timespec="seconds")
    fields: dict[str, Any] = {
        "公司名称": classification.company,
        "岗位名称": classification.role,
        "流程状态": classification.status,
        "更新时间": received,
        **({"投递时间": received} if classification.status == "投递" else {}),
    }
    if classification.company_type:
        fields["企业类型"] = classification.company_type
    if classification.deadline:
        fields["截止时间"] = classification.deadline
    return fields


def _todo_request(
    message: MailMessage,
    classification: Classification,
    tz: ZoneInfo,
) -> TodoRequest | None:
    if classification.status not in TODO_STATUSES or not classification.deadline:
        return None
    try:
        due_at = datetime.fromisoformat(classification.deadline)
    except ValueError:
        return None
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=tz)
    due_date = due_at.astimezone(tz).date()
    if due_date < datetime.now(tz).date():
        return None
    return TodoRequest(
        message_id=message.message_id,
        subject=message.subject,
        sender_address=message.sender_address,
        received_at=message.received_at.isoformat(),
        due_date=due_date,
    )


def _calendar_event_request(
    message: MailMessage,
    classification: Classification,
    tz: ZoneInfo,
) -> CalendarEventRequest | None:
    if classification.status not in CALENDAR_STATUSES:
        return None
    fixed_written_exam = classification.status == "笔试" and bool(classification.written_exam_start)
    deadline_written_exam = classification.status == "笔试" and not fixed_written_exam
    if fixed_written_exam:
        start_text = classification.written_exam_start
        end_text = classification.written_exam_end
        event_label = "笔试"
    elif deadline_written_exam:
        start_text = classification.deadline
        end_text = None
        event_label = "笔试截止"
    else:
        start_text = classification.interview_start
        end_text = classification.interview_end
        event_label = classification.status
    if not start_text:
        return None
    try:
        start = datetime.fromisoformat(start_text)
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    start = start.astimezone(tz)
    try:
        end = datetime.fromisoformat(end_text) if end_text else None
    except ValueError:
        end = None
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    if end:
        end = end.astimezone(tz)
    if not end or end <= start:
        end = start + (timedelta(minutes=30) if deadline_written_exam else timedelta(hours=1))
    if start <= datetime.now(tz):
        return None

    def clean(value: str) -> str:
        return re.sub(r"[\r\n]+", " ", value).strip()[:300]

    summary = f"{event_label}｜{clean(classification.company)}｜{clean(classification.role)}"
    description = [
        "由招聘邮件 Agent 自动创建。",
        f"- 公司：{clean(classification.company)}",
        f"- 岗位：{clean(classification.role)}",
        f"- 流程：{classification.status}",
        f"- 邮件主题：{clean(message.subject)}",
    ]
    if deadline_written_exam:
        description.append("- 时间依据：邮件给出的最晚完成时间（非固定开考时刻）")
    if classification.meeting_link:
        description.append(f"- 面试入口：{classification.meeting_link}")
    if classification.interview_location:
        description.append(f"- 面试地点：{clean(classification.interview_location)}")
    event_key = hashlib.sha256(
        f"{message.message_id}|{event_label}|{start.isoformat()}".encode("utf-8")
    ).hexdigest()[:32]
    return CalendarEventRequest(
        event_key=event_key,
        source_message_id=message.message_id,
        summary=summary,
        start_at=start.isoformat(timespec="minutes"),
        end_at=end.isoformat(timespec="minutes"),
        description="\n".join(description),
        location=classification.interview_location,
    )


def _sync_pending_todos(settings: Settings, state: StateStore) -> tuple[int, int]:
    if not settings.coremail_todo_enabled:
        return 0, 0
    pending = state.pending_todos()
    if not pending:
        return 0, 0
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    expired = {todo.message_id for todo in pending if todo.due_date < today}
    if expired:
        state.mark_todos_done(expired)
    active = [todo for todo in pending if todo.message_id not in expired]
    if not active:
        return 0, 0
    requested = {todo.message_id for todo in active}
    try:
        completed = CoremailTodoClient(settings).create_todos(active)
        state.mark_todos_done(completed)
        missing = requested - completed
        state.mark_todos_failed(missing, "未在 Coremail 最近邮件中匹配到原邮件")
        return len(completed), len(missing)
    except Exception as exc:
        state.mark_todos_failed(requested, str(exc))
        print(json.dumps({"phase": "mail_todo", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 0, len(requested)


def _sync_pending_calendar_events(settings: Settings, state: StateStore) -> tuple[int, int]:
    if not settings.lark_calendar_enabled:
        return 0, 0
    pending = state.pending_calendar_events()
    if not pending:
        return 0, 0
    now = datetime.now(ZoneInfo(settings.timezone))
    expired = {
        event.event_key
        for event in pending
        if datetime.fromisoformat(event.end_at).astimezone(now.tzinfo) <= now
    }
    if expired:
        state.mark_calendar_events_done({event_key: "expired" for event_key in expired})
    active = [event for event in pending if event.event_key not in expired]
    if not active:
        return 0, 0
    requested = {event.event_key for event in active}
    try:
        created = LarkCalendar(settings).create_events(active)
        state.mark_calendar_events_done(created)
        missing = requested - set(created)
        state.mark_calendar_events_failed(missing, "飞书日历未返回 event_id")
        return len(created), len(missing)
    except Exception as exc:
        state.mark_calendar_events_failed(requested, str(exc))
        print(json.dumps({"phase": "calendar_event", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 0, len(requested)


def backfill_flagged_todos(settings: Settings, dry_run: bool = False) -> dict[str, int]:
    """为起始日期后的已标记邮件补建待办，不改变增量 UID 游标或飞书数据。"""
    state = StateStore(settings.database_path)
    stats = {
        "flagged_fetched": 0,
        "cached": 0,
        "deadline_repaired": 0,
        "llm_batches": 0,
        "eligible": 0,
        "todos_created": 0,
        "todos_pending": 0,
        "table_deadlines_updated": 0,
        "calendar_created": 0,
        "calendar_pending": 0,
        "skipped": 0,
    }
    try:
        messages = ImapMailbox(settings).fetch_flagged()
        stats["flagged_fetched"] = len(messages)
        agent = DeepSeekMailAgent(settings)
        timezone = ZoneInfo(settings.timezone)
        lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
        records = lark.list_records()
        by_key, by_company = _record_index(records)
        results: dict[str, Classification] = {}
        needs_agent: list[MailMessage] = []

        for message in messages:
            cached = state.get_agent_result(message.message_id, settings.deepseek_model)
            if cached:
                repaired = agent.repair_missing_deadline(message, cached)
                results[message.message_id] = repaired
                stats["cached"] += 1
                if repaired != cached:
                    stats["deadline_repaired"] += 1
                    if not dry_run:
                        state.save_agent_result(message.message_id, settings.deepseek_model, repaired)
            else:
                needs_agent.append(message)

        for offset in range(0, len(needs_agent), settings.deepseek_batch_size):
            batch = needs_agent[offset : offset + settings.deepseek_batch_size]
            batch_results = agent.classify_batch(batch)
            stats["llm_batches"] += 1
            results.update(batch_results)
            if not dry_run:
                for message in batch:
                    state.save_agent_result(
                        message.message_id,
                        settings.deepseek_model,
                        batch_results[message.message_id],
                    )

        table_updates: dict[str, dict[str, Any]] = {}
        for message in messages:
            result = results[message.message_id]
            if not result.relevant or result.confidence < settings.min_confidence:
                stats["skipped"] += 1
                continue
            todo = _todo_request(message, result, timezone)
            if not todo:
                stats["skipped"] += 1
                continue
            stats["eligible"] += 1
            if not dry_run:
                state.enqueue_todo(todo)
                record = _find_record(result, by_key, by_company)
                if record and not record.fields.get("截止时间"):
                    table_updates[record.record_id] = {"截止时间": result.deadline}

        if not dry_run:
            lark.batch_update_records(table_updates)
            stats["table_deadlines_updated"] = len(table_updates)
            stats["todos_created"], stats["todos_pending"] = _sync_pending_todos(settings, state)
            stats["calendar_created"], stats["calendar_pending"] = _sync_pending_calendar_events(settings, state)
        return stats
    finally:
        state.close()


def repair_flagged_interviews(settings: Settings, dry_run: bool = False) -> dict[str, int]:
    """重新识别星标面试/笔试邮件，修正状态并补建日程。"""
    state = StateStore(settings.database_path)
    stats = {
        "flagged_fetched": 0,
        "calendar_candidates": 0,
        "llm_batches": 0,
        "table_records_updated": 0,
        "todos_created": 0,
        "todos_pending": 0,
        "calendar_eligible": 0,
        "calendar_created": 0,
        "calendar_pending": 0,
        "unmatched_candidates": 0,
        "calendar_eligible_unmatched": 0,
    }
    try:
        messages = ImapMailbox(settings).fetch_flagged()
        stats["flagged_fetched"] = len(messages)
        candidates: list[MailMessage] = []
        for message in messages:
            cached = state.get_agent_result(message.message_id, settings.deepseek_model)
            subject = message.subject.lower()
            if (
                (cached and cached.status in CALENDAR_REVIEW_STATUSES)
                or any(hint in subject for hint in ("面试", "约面", "预约", "interview", "笔试", "机考", "考试"))
            ):
                candidates.append(message)
        stats["calendar_candidates"] = len(candidates)

        agent = DeepSeekMailAgent(settings)
        results: dict[str, Classification] = {}
        for offset in range(0, len(candidates), settings.deepseek_batch_size):
            batch = candidates[offset : offset + settings.deepseek_batch_size]
            batch_results = agent.classify_batch(batch)
            stats["llm_batches"] += 1
            results.update(batch_results)
            if not dry_run:
                for message in batch:
                    state.save_agent_result(
                        message.message_id,
                        settings.deepseek_model,
                        batch_results[message.message_id],
                    )

        lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
        records = lark.list_records()
        by_key, by_company = _record_index(records)
        latest_actions: dict[str, tuple[MailMessage, Classification, LarkRecord | None]] = {}
        for message in candidates:
            result = results[message.message_id]
            if not result.relevant or result.confidence < settings.min_confidence:
                continue
            record = _find_record(result, by_key, by_company)
            action_key = (
                f"record:{record.record_id}"
                if record
                else f"unmatched:{_company_key(result.company)}|{_role_key(result.role)}"
            )
            latest_actions[action_key] = (message, result, record)

        stats["unmatched_candidates"] = sum(
            1 for _, _, record in latest_actions.values() if record is None
        )

        table_updates: dict[str, dict[str, Any]] = {}
        timezone = ZoneInfo(settings.timezone)
        for message, result, record in latest_actions.values():
            if record is None:
                continue
            current = _cell_text(record.fields.get("流程状态")) or "待确认"
            if current in TERMINAL:
                continue
            patch: dict[str, Any] = {}
            subject_and_body = f"{message.subject}\n{message.body[:4000]}".lower()
            force_ai_correction = (
                result.status == "测评&AI面"
                and re.search(r"ai\s*面|ai面试", subject_and_body) is not None
            )
            if result.status == "约面" or force_ai_correction:
                patch["流程状态"] = result.status
                patch["截止时间"] = result.deadline
            elif result.status in CALENDAR_STATUSES and (
                current == "约面" or should_replace_status(current, result.status, False)
            ):
                patch["流程状态"] = result.status
            if result.status in HUMAN_INTERVIEW_STATUSES and result.interview_start:
                patch["截止时间"] = result.interview_start
            elif result.status == "笔试" and result.deadline:
                patch["截止时间"] = result.deadline

            if patch:
                table_updates[record.record_id] = patch

        # 邮箱待办和日历是邮件识别结果的直接下游，不得依赖飞书表格行匹配。
        # 表格匹配失败时仍创建可执行提醒，并显式记录告警供后续排查。
        for message, result, record in latest_actions.values():
            if record is not None:
                current = _cell_text(record.fields.get("流程状态")) or "待确认"
                if current in TERMINAL:
                    continue
            todo = _todo_request(message, result, timezone)
            if todo and not dry_run:
                state.enqueue_todo(todo)
            calendar_event = _calendar_event_request(message, result, timezone)
            if calendar_event:
                stats["calendar_eligible"] += 1
                if record is None:
                    stats["calendar_eligible_unmatched"] += 1
                    print(json.dumps({
                        "phase": "calendar_repair",
                        "warning": "table_record_unmatched",
                        "company": result.company,
                        "role": result.role,
                        "subject": message.subject,
                        "start_at": calendar_event.start_at,
                    }, ensure_ascii=False), flush=True)
                if not dry_run:
                    state.enqueue_calendar_event(calendar_event)

        if not dry_run:
            lark.batch_update_records(table_updates)
            stats["table_records_updated"] = len(table_updates)
            stats["todos_created"], stats["todos_pending"] = _sync_pending_todos(settings, state)
            stats["calendar_created"], stats["calendar_pending"] = _sync_pending_calendar_events(settings, state)
        return stats
    finally:
        state.close()


def run_sync(
    settings: Settings,
    dry_run: bool = False,
    initial_records: list[LarkRecord] | None = None,
) -> dict[str, int]:
    state = StateStore(settings.database_path)
    stats = {
        "fetched": 0, "llm_batches": 0, "relevant": 0,
        "created": 0, "updated": 0, "flagged": 0,
        "todos_created": 0, "todos_pending": 0,
        "calendar_created": 0, "calendar_pending": 0, "skipped": 0,
    }
    try:
        lark = LarkBase(settings.lark_cli, settings.lark_base_token, settings.lark_table_id)
        records = lark.list_records() if initial_records is None else initial_records
        by_key, by_company = _record_index(records)
        messages, max_uid = ImapMailbox(settings).fetch(state.get_last_uid())
        stats["fetched"] = len(messages)
        agent = DeepSeekMailAgent(settings)
        timezone = ZoneInfo(settings.timezone)

        results: dict[str, Classification] = {}
        needs_agent: list[MailMessage] = []
        synced: list[tuple[MailMessage, Classification]] = []
        for message in messages:
            if state.was_processed(message.message_id):
                continue
            cached = state.get_agent_result(message.message_id, settings.deepseek_model)
            if cached:
                results[message.message_id] = cached
            else:
                needs_agent.append(message)
        for offset in range(0, len(needs_agent), settings.deepseek_batch_size):
            batch = needs_agent[offset : offset + settings.deepseek_batch_size]
            batch_results = agent.classify_batch(batch)
            stats["llm_batches"] += 1
            print(json.dumps({
                "phase": "classify",
                "batch": stats["llm_batches"],
                "messages": len(batch),
            }, ensure_ascii=False), flush=True)
            results.update(batch_results)
            if not dry_run:
                for message in batch:
                    state.save_agent_result(message.message_id, settings.deepseek_model, batch_results[message.message_id])

        for message in messages:
            if state.was_processed(message.message_id):
                stats["skipped"] += 1
                continue
            result = results[message.message_id]
            if not result.relevant or result.confidence < settings.min_confidence:
                stats["skipped"] += 1
                if not dry_run:
                    state.mark_processed(message.message_id, message.uid, "ignored")
                continue
            stats["relevant"] += 1
            record = _find_record(result, by_key, by_company)
            fields = _fields(message, result, timezone)
            action = "create"
            if record:
                action = "update"
                current_status = _cell_text(record.fields.get("流程状态") or record.fields.get("当前进展")) or "待确认"
                # 保留人工维护值，但允许后续正文把“待确认岗位”补全为明确岗位。
                fields.pop("公司名称", None)
                if record.fields.get("企业类型"):
                    fields.pop("企业类型", None)
                existing_role = _cell_text(record.fields.get("岗位名称") or record.fields.get("岗位"))
                if existing_role != "待确认岗位" or result.role == "待确认岗位":
                    fields.pop("岗位名称", None)
                if not should_replace_status(current_status, result.status, False):
                    fields.pop("流程状态", None)
                if record.fields.get("投递时间") or result.status != "投递":
                    fields.pop("投递时间", None)

            print(json.dumps({
                "action": action,
                "subject": message.subject,
                "company": result.company,
                "role": result.role,
                "status": result.status,
                "deadline": result.deadline,
                "confidence": result.confidence,
                "flag_email": result.status in FOLLOWUP_STATUSES,
                "create_calendar": bool(_calendar_event_request(message, result, timezone)),
            }, ensure_ascii=False))
            if dry_run:
                continue
            if record:
                lark.update_record(record.record_id, fields)
                record.fields.update(fields)
                stats["updated"] += 1
            else:
                record_id = lark.create_record(fields)
                stats["created"] += 1
                if record_id:
                    created = LarkRecord(record_id, fields)
                    by_key[result.source_key] = created
                    by_company.setdefault(_company_key(result.company), []).append(created)
            synced.append((message, result))
            todo = _todo_request(message, result, timezone)
            if todo:
                state.enqueue_todo(todo)
            calendar_event = _calendar_event_request(message, result, timezone)
            if calendar_event:
                state.enqueue_calendar_event(calendar_event)

        # 先成功写入飞书，再标记原邮件；任一步失败都不推进 UID 游标，方便重试。
        followup_uids = [message.uid for message, result in synced if result.status in FOLLOWUP_STATUSES]
        if not dry_run:
            stats["flagged"] = ImapMailbox(settings).mark_flagged(followup_uids)
            stats["todos_created"], stats["todos_pending"] = _sync_pending_todos(settings, state)
            stats["calendar_created"], stats["calendar_pending"] = _sync_pending_calendar_events(settings, state)
            for message, result in synced:
                state.mark_processed(message.message_id, message.uid, result.status)

        if max_uid is not None and not dry_run:
            state.set_last_uid(max_uid)
        return stats
    finally:
        state.close()
