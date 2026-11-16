"""状态快照清单业务规则：快照引用盘点、可清理判定与跨流程回写都收在这里。

盘点口径：
- 读取 STAT 状态后端的存储路径、快照频率、状态大小，校验检查点引用后写入清单；
- 清单里标记受保护文件、可清理文件和引用来源，并与清理待办一起落库；
- 同一存储路径并发盘点只生成一份清单；
- 检查点引用读取失败时不得删除文件：中止盘点/校验，不产生任何可清理判定；
- 清理结果校验只遵循最终确认的那一份清单，待恢复快照不能当成可删除文件。
"""
from __future__ import annotations

import threading
from typing import Any

from app.store import store

MODULE = "snapshot"
STATE_MODULE = "state"
CHECKPOINT_MODULE = "checkpoint"
REQUIRED_FIELDS = ["清单编号", "配置编号", "存储路径"]
STATUS_ORDER = ["待确认", "已确认", "已校验"]
ACTION_RULES = {"确认清单": "已确认", "清理校验": "已校验"}
NEGATIVE_ACTIONS = []

# 只有「已清理」的检查点会释放引用；已创建、已恢复、已过期的检查点都可能还要恢复，
# 它们引用的快照一律按待恢复处理，保持受保护，不得当成可删除文件。
CHECKPOINT_RELEASED = "已清理"

# 同一存储路径的盘点互斥锁：并发盘点只生成一份清单。
_inventory_lock = threading.Lock()


def _normalize_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _load_checkpoint_refs(storage_path: str) -> list[dict[str, Any]]:
    """读取引用该存储路径的检查点记录。

    读取失败必须抛错：调用方据此中止盘点或校验，任何文件都不得被判定为可清理。
    """
    try:
        rows = store.rows(CHECKPOINT_MODULE)
    except Exception as exc:  # pragma: no cover - 内存仓库正常不会走到
        raise RuntimeError("检查点引用读取失败") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("检查点引用读取失败")
    base = _normalize_path(storage_path)
    refs: list[dict[str, Any]] = []
    for row in rows:
        saved = str(row.get("保存路径") or "").strip()
        if not saved:
            continue
        saved = _normalize_path(saved)
        if saved == base or saved.startswith(base + "/"):
            refs.append(row)
    return refs


def _active_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仍持有引用的检查点：已清理才算释放，已过期仍按待恢复保护。"""
    return [row for row in refs if str(row.get("status") or "") != CHECKPOINT_RELEASED]


def _compute_sets(
    config_code: str,
    storage_path: str,
    refs: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """根据检查点引用计算受保护文件、可清理文件与引用来源。"""
    base = _normalize_path(storage_path)
    protected = [base, f"{base}/_metadata", f"{base}/shared"]
    cleanable: list[str] = []
    sources = [config_code] if config_code else []
    for ref in refs:
        checkpoint_file = f"{base}/{ref.get('检查点ID')}"
        if str(ref.get("status") or "") == CHECKPOINT_RELEASED:
            cleanable.append(checkpoint_file)
            continue
        protected.append(checkpoint_file)
        code = str(ref.get("记录编号") or "").strip()
        if code and code not in sources:
            sources.append(code)
    cleanable.extend([f"{base}/tmp", f"{base}/orphan-20260901"])
    return protected, cleanable, sources


class SnapshotService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("清单编号", "")) or keyword in str(row.get("存储路径", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def _find_by_path(self, storage_path: str) -> dict[str, Any] | None:
        base = _normalize_path(storage_path)
        for row in store.rows(MODULE):
            if _normalize_path(str(row.get("存储路径") or "")) == base:
                return row
        return None

    def inventory_state_backend(
        self, state_entry: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """盘点一个状态后端配置：读取存储路径、快照频率、状态大小，校验检查点引用后写清单。"""
        storage_path = str(state_entry.get("存储路径") or "").strip()
        if not storage_path:
            return None, "后端配置缺少存储路径，无法生成状态快照数据清单"
        config_code = str(state_entry.get("配置编号") or "").strip()
        try:
            refs = _load_checkpoint_refs(storage_path)
        except RuntimeError as exc:
            # 检查点引用读取失败：不得删除文件，直接中止，不产生任何可清理判定
            return None, f"{exc}，已中止盘点：全部文件保持受保护，未生成可清理判定"
        with _inventory_lock:
            existing = self._find_by_path(storage_path)
            if existing is not None:
                return existing, (
                    f"存储路径 {storage_path} 已盘点过，"
                    f"并发盘点只保留清单 {existing.get('清单编号')}"
                )
            protected, cleanable, sources = _compute_sets(config_code, storage_path, refs)
            rows = store.rows(MODULE)
            next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            entry: dict[str, Any] = {
                "id": next_id,
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "清单编号": f"SNAP-{next_id:04d}",
                "配置编号": config_code,
                "存储路径": _normalize_path(storage_path),
                "快照频率": state_entry.get("快照频率"),
                "状态大小": state_entry.get("状态大小"),
                "受保护文件": protected,
                "可清理文件": cleanable,
                "引用来源": sources,
                # 清单与清理待办写在同一条记录里，一次 append 一起落库
                "清理待办": [
                    {"文件": file, "判定": "可清理", "状态": "待清理", "结论": ""}
                    for file in cleanable
                ],
                "处理结论": [
                    f"{config_code} 快照盘点完成：受保护 {len(protected)} 个、"
                    f"可清理 {len(cleanable)} 个，引用来源 {'、'.join(sources)}"
                ],
                "清单状态": STATUS_ORDER[0],
            }
            rows.append(entry)
            return entry, (
                f"清单 {entry['清单编号']} 已生成并与清理待办一起落库："
                f"受保护 {len(protected)} 个文件、可清理 {len(cleanable)} 个文件"
            )

    def sync_checkpoint_reference(
        self, checkpoint_entry: dict[str, Any], action: str
    ) -> str | None:
        """跨流程回写：检查点动作生效后，同步更新同一份状态快照数据清单。"""
        storage_path = str(checkpoint_entry.get("保存路径") or "").strip()
        if not storage_path:
            return None
        record_code = str(checkpoint_entry.get("记录编号") or "").strip()
        with _inventory_lock:
            entry = self._find_by_path(storage_path)
            if entry is None:
                return None
            conclusions: list[str] = entry.setdefault("处理结论", [])
            try:
                refs = _load_checkpoint_refs(storage_path)
            except RuntimeError:
                # 引用读取失败：维持原判定，任何文件都不得改成可清理
                conclusions.append(
                    f"{record_code} {action}：检查点引用读取失败，清单维持原判定，文件不得删除"
                )
                return f"{record_code} 已回写清单 {entry.get('清单编号')}：引用读取失败，维持原判定"
            protected, cleanable, sources = _compute_sets(
                str(entry.get("配置编号") or ""), storage_path, refs
            )
            entry["受保护文件"] = protected
            entry["可清理文件"] = cleanable
            entry["引用来源"] = sources
            protected_set = set(protected)
            todos: list[dict[str, Any]] = entry.setdefault("清理待办", [])
            for todo in todos:
                if str(todo.get("文件") or "") in protected_set:
                    todo["判定"] = "受保护"
                    todo["状态"] = "不可清理"
                    todo["结论"] = f"{record_code} {action} 后仍被引用，待恢复快照不可删除"
            known = {str(todo.get("文件") or "") for todo in todos}
            for file in cleanable:
                if file not in known:
                    todos.append({"文件": file, "判定": "可清理", "状态": "待清理", "结论": ""})
            conclusion = (
                f"{record_code} 已{action}：{storage_path} 按最新引用重算，"
                f"受保护 {len(protected)} 个、可清理 {len(cleanable)} 个"
            )
            conclusions.append(conclusion)
            return f"已同步回写清单 {entry.get('清单编号')}：{conclusion}"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"状态快照清单 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于状态快照清单可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or "")
        if action == "确认清单" and current != "待确认":
            return None, f"清单 {entry.get('清单编号')} 当前为「{current}」，只有待确认清单才能确认"
        if action == "清理校验" and current != "已确认":
            return None, (
                f"清单 {entry.get('清单编号')} 当前为「{current}」，"
                "清理校验只能遵循最终确认的状态快照数据清单"
            )
        if action == "清理校验":
            message, passed = self._validate_cleanup(entry)
            if not passed:
                return None, message
        else:
            message = f"清单 {entry.get('清单编号')} 已确认，后续清理校验只遵循这一份清单"
            entry.setdefault("处理结论", []).append(message)
        entry["status"] = target
        entry["清单状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, message

    def _validate_cleanup(self, entry: dict[str, Any]) -> tuple[str, bool]:
        """清理结果校验：只遵循这一份最终确认的清单，待恢复快照不得当成可删除文件。"""
        storage_path = str(entry.get("存储路径") or "")
        try:
            refs = _load_checkpoint_refs(storage_path)
        except RuntimeError as exc:
            return f"{exc}，清理校验中止：不得删除任何文件", False
        base = _normalize_path(storage_path)
        # 仍被引用（含已过期待恢复）的文件一律保持受保护，处理来源冲突时以保护为准
        hold = {f"{base}/{ref.get('检查点ID')}" for ref in _active_refs(refs)}
        hold.add(base)
        protected_set = set(entry.get("受保护文件") or []) | hold
        cleanable_set = set(entry.get("可清理文件") or []) - hold
        confirmed = 0
        kept = 0
        for todo in entry.get("清理待办", []):
            file = str(todo.get("文件") or "")
            if file in protected_set:
                todo["判定"] = "受保护"
                todo["状态"] = "不可清理"
                todo["结论"] = "仍被检查点引用（含待恢复快照），遵循最终确认清单不得删除"
                kept += 1
            elif file in cleanable_set:
                todo["判定"] = "可清理"
                todo["状态"] = "确认可清理"
                todo["结论"] = "最终确认清单判定可清理，且未被任何待恢复检查点引用"
                confirmed += 1
            else:
                # 清单未列为可清理的目录：以最终确认清单为准，保持受保护
                todo["判定"] = "受保护"
                todo["状态"] = "不可清理"
                todo["结论"] = "未被最终确认清单列为可清理，保持受保护"
                kept += 1
        message = (
            f"清单 {entry.get('清单编号')} 清理校验完成：{confirmed} 个确认可清理、"
            f"{kept} 个保持受保护（仅遵循最终确认的状态快照数据清单）"
        )
        entry.setdefault("处理结论", []).append(message)
        return message, True


snapshot_service = SnapshotService()
