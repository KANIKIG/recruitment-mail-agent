from __future__ import annotations

import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .deepseek_agent import DeepSeekMailAgent
from .lark import LarkBase, LarkRecord
from .mailbox import ImapMailbox
from .models import Classification, MailMessage
from .state import StateStore


STATUS_RANK = {
    "待确认": 0, "投递": 10, "测评&AI面": 20, "笔试": 25, "技术面": 30,
    "HR面": 40, "主管面": 50, "Offer": 60,
}
TERMINAL = {"Offer", "已挂"}
FOLLOWUP_STATUSES = {"测评&AI面", "笔试", "技术面", "HR面", "主管面", "Offer", "已挂"}


def should_replace_status(current: str, incoming: str, locked: bool) -> bool:
    if locked or current in TERMINAL:
        return False
    if incoming == "已挂":
        return True
    return STATUS_RANK.get(incoming, 0) > STATUS_RANK.get(current, 0)


def _record_index(records: list[LarkRecord]) -> tuple[dict[str, LarkRecord], dict[str, list[LarkRecord]]]:
    by_key: dict[str, LarkRecord] = {}
    by_company: dict[str, list[LarkRecord]] = {}
    for record in records:
        key = str(record.fields.get("同步键") or "").strip()
        company = _cell_text(record.fields.get("公司名称") or record.fields.get("公司")).strip().lower()
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
    candidates = by_company.get(classification.company.lower(), [])
    role = classification.role.lower()
    normalized_role = _role_key(role)
    for candidate in candidates:
        existing_role = _cell_text(candidate.fields.get("岗位名称") or candidate.fields.get("岗位")).lower()
        existing_normalized = _role_key(existing_role)
        if existing_role == role or (
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
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


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
    if classification.deadline:
        fields["截止时间"] = classification.deadline
    return fields


def run_sync(
    settings: Settings,
    dry_run: bool = False,
    initial_records: list[LarkRecord] | None = None,
) -> dict[str, int]:
    state = StateStore(settings.database_path)
    stats = {
        "fetched": 0, "llm_batches": 0, "relevant": 0,
        "created": 0, "updated": 0, "flagged": 0, "skipped": 0,
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
                    by_company.setdefault(result.company.lower(), []).append(created)
            synced.append((message, result))

        # 先成功写入飞书，再标记原邮件；任一步失败都不推进 UID 游标，方便重试。
        followup_uids = [message.uid for message, result in synced if result.status in FOLLOWUP_STATUSES]
        if not dry_run:
            stats["flagged"] = ImapMailbox(settings).mark_flagged(followup_uids)
            for message, result in synced:
                state.mark_processed(message.message_id, message.uid, result.status)

        if max_uid is not None and not dry_run:
            state.set_last_uid(max_uid)
        return stats
    finally:
        state.close()
