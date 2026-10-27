#!/usr/bin/env python3
"""Build a fail-closed, task-scoped feedback archive without persistent output."""

import argparse
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import zipfile

import task_report


MAX_SHOTS = 40
MAX_SHOT_BYTES = 20 * 1024 * 1024
MAX_INPUT_BYTES = 512 * 1024
MAX_DEPTH = 20
APP = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
ACTIVITY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+/\.[A-Za-z0-9_.$]+")
ISO_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00")
SHOT_NAME = re.compile(r"(?:shot|before|after)-([0-9]{13})\.(?:jpg|jpeg|png)")

VOCAB = task_report.VOCABULARY
ENUMS = {
    "kind": set(VOCAB["action_kinds"]) | {"task_start", "task_end", "report_task_start",
        "report_task_end", "report_attribution_conflict", "recovery", "command_gate", "read"},
    "action_type": set(VOCAB["actions"]), "result": set(VOCAB["results"]),
    "reason_code": set(VOCAB["reasons"]),
    "op": set(VOCAB["read_ops"]), "action": {"stop_for_human"},
    "status": {"completed", "refused", "failed", "running"},
    "dispatch_status": {"submitted", "command_completed", "not_submitted"},
    "terminal_status": {"completed", "cancelled", "error", "reply_received", "empty_reply",
                        "web_completed", "web_interrupted", "web_error"},
    "evidence_kind": {"web_turn_start", "web_turn_end", "bridge_admit", "bridge_reply",
                      "stop_confirmed", "runtime_error"},
    "outcome": {"completed", "user_stopped", "error", "handoff", "unknown"},
    "transport_state": {"running", "reply_received", "cancelled", "web_completed",
                        "web_interrupted", "web_error"},
    "display_kind": {"main", "virtual"}, "mode": {"enforce", "shadow"},
    "decision": {"allow", "deny", "ask"}, "would_decision": {"allow", "deny", "ask"},
    "category": {"privacy", "data_exfiltration", "destructive", "system_change"},
    "warning": {"typesafe_unavailable"}, "vd_policy": {"vd_required", "vd_active_elsewhere"},
}
BOOL_FIELDS = {"allowed", "ok", "goal_verified", "ui_reacted", "tree_changed", "pixel_changed",
               "screen_changed", "ev_advanced", "settled", "fast", "act_ok", "ack_no_zones",
               "approval_verified", "attribution_complete", "attribution_conflict", "concurrent_turns",
               "interrupted", "lifecycle_valid", "audit_linked", "command_hook_linked",
               "timeline_truncated"}
NUMBER_FIELDS = {"ts", "elapsed_s", "duration_s", "audit_offset", "display_id", "exit_code",
                 "settle_ms", "settle_polls", "ev_count_before", "ev_count_after", "schema_version",
                 "dispatched_actions", "command_hook_refusals", "command_hook_shadow_would_block",
                 "typesafe_unavailable_warnings", "handoff_points", "x", "y", "x1", "y1", "x2", "y2"}
OBJECT_FIELDS = {"task", "counts", "coverage", "vision", "r3", "shot_before_meta",
                 "shot_after_meta", "gate_refusals_by_reason"}
LIST_FIELDS = {"timeline", "points"}
APP_FIELDS = {"pkg", "fg"}
ACTIVITY_FIELDS = {"act_before", "act_after"}
TIME_FIELDS = {"started_at", "ended_at", "at"}
ID_FIELDS = {"task_id"}
FREE_TEXT_FIELDS = {"note", "expect", "reason", "explanation", "command", "task_name",
                    "shot_before", "shot_after", "app"}


def _readme(skipped, missing):
    return ("DSB feedback export\n\n"
            "Contains: this turn's execution report and audit metadata; optional re-encoded screenshots.\n"
            "Removed: free text, unknown fields, paths and image EXIF/ICC metadata. "
            "Removed strings retain only character counts. Local audit files are unchanged.\n"
            "Retained structural metadata may include task ID, display number, package and Activity names.\n"
            "Screenshots are excluded by default. If included, only the top 7% is blacked out. "
            "Screen content is NOT automatically redacted. Review every image and the archive before posting.\n"
            f"Skipped {skipped} screenshots outside this turn; {missing} referenced screenshots had expired from local retention.\n\n"
            "逗沙包反馈导出\n\n"
            "包含：本轮执行报告、审计结构元数据；仅显式选择时包含重新编码的截图。\n"
            "已去掉：自由文本、未知字段、路径及图片 EXIF/ICC 元数据；被移除的字符串只保留字符数。本机审计不变。\n"
            "保留的结构元数据可能包括任务 ID、显示屏编号、包名和 Activity 名。\n"
            "截图默认不带；带截图时只涂黑顶部 7%。画面内容不会自动脱敏，发出前请自己看一遍。\n"
            f"跳过 {skipped} 张不属于本轮的截图；{missing} 张截图已按保留期清理。\n")


def _removed(value):
    if isinstance(value, str):
        chars = len(value)
    else:
        try:
            chars = len(json.dumps(value, ensure_ascii=False, default=str))
        except (OverflowError, RecursionError, ValueError):
            chars = 0
    return {"removed": True, "chars": chars}


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _package_part(value):
    if not isinstance(value, str):
        return None
    if ACTIVITY.fullmatch(value) or APP.fullmatch(value):
        return value.split("/", 1)[0]
    return None


def _redact_field(key, value, row, depth):
    if value is None and key in ENUMS.keys() | BOOL_FIELDS | NUMBER_FIELDS | APP_FIELDS | ACTIVITY_FIELDS | TIME_FIELDS:
        return None
    if key in BOOL_FIELDS and type(value) is bool:
        return value
    if key in NUMBER_FIELDS and _number(value):
        return value
    if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
        return value
    if key in ID_FIELDS and isinstance(value, str) and task_report.TASK_ID_RE.fullmatch(value):
        return value
    if key == "pkg" and isinstance(value, str) and APP.fullmatch(value) and any(
            _package_part(row.get(field)) == value for field in ("act_before", "act_after", "fg")):
        return value
    if key == "fg" and _package_part(value):
        return value
    if key in ACTIVITY_FIELDS and isinstance(value, str) and ACTIVITY.fullmatch(value):
        return value
    if key in TIME_FIELDS and isinstance(value, str) and ISO_TIME.fullmatch(value):
        return value
    if key == "points" and isinstance(value, list):
        def point(item, level):
            if level >= MAX_DEPTH:
                return _removed(item)
            if _number(item):
                return item
            if isinstance(item, list):
                return [point(part, level + 1) for part in item]
            return _removed(item)
        return [point(item, depth + 1) for item in value]
    if key in LIST_FIELDS and isinstance(value, list):
        return [redact(item, depth + 1) if isinstance(item, dict) else _removed(item) for item in value]
    if key == "gate_refusals_by_reason" and isinstance(value, dict):
        allowed = {code: number for code, number in value.items()
                   if code in VOCAB["gate_reason_codes"] and _number(number)}
        unknown = [_removed(number) for code, number in value.items()
                   if code not in VOCAB["gate_reason_codes"] or not _number(number)]
        if unknown:
            allowed["unknown_fields"] = unknown
        return allowed
    if key in OBJECT_FIELDS and isinstance(value, dict):
        return redact(value, depth + 1)
    return _removed(value)


def redact(value, depth=0):
    """Allow only named structural fields and known formats; hide unknown key names too."""
    if depth >= MAX_DEPTH:
        return _removed(value)
    if not isinstance(value, dict):
        return _removed(value)
    result = {}
    unknown = []
    known = set(ENUMS) | BOOL_FIELDS | NUMBER_FIELDS | OBJECT_FIELDS | LIST_FIELDS | APP_FIELDS | ACTIVITY_FIELDS | TIME_FIELDS | ID_FIELDS | FREE_TEXT_FIELDS
    for key, item in value.items():
        if key in known:
            result[key] = _redact_field(key, item, value, depth)
        else:
            unknown.append(_removed(item))
    if unknown:
        result["unknown_fields"] = unknown
    return result


def _shot_window(rows):
    kinds = [("report_task_start", "report_task_end"), ("task_start", "task_end")]
    for start_kind, end_kind in kinds:
        starts = [row.get("ts") for row in rows if row.get("kind") == start_kind]
        ends = [row.get("ts") for row in rows if row.get("kind") == end_kind]
        if starts or ends:
            if len(starts) == len(ends) == 1 and _number(starts[0]) and _number(ends[0]) and starts[0] <= ends[0]:
                return int(starts[0] * 1000), int(ends[0] * 1000)
            return None
    return None


def _shot_paths(rows, shots_dir):
    seen = set()
    root = Path(shots_dir).resolve()
    window = _shot_window(rows)
    for row in rows:
        for key in ("shot_before", "shot_after"):
            raw = row.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            source = Path(raw)
            if not source.is_absolute() or ".." in source.parts or source.parent.resolve() != root:
                raise ValueError("invalid screenshot path")
            match = SHOT_NAME.fullmatch(source.name)
            if not match:
                raise ValueError("invalid screenshot path")
            if source.name in seen:
                continue
            seen.add(source.name)
            if window is None or not window[0] <= int(match.group(1)) <= window[1]:
                yield source.name, False
            else:
                yield source.name, True


def build_zip(audit_rows, report, task_id, include_shots=False, shots_dir=None,
              max_shots=MAX_SHOTS, max_shot_bytes=MAX_SHOT_BYTES):
    if not isinstance(task_id, str) or not task_report.TASK_ID_RE.fullmatch(task_id):
        raise ValueError("invalid task_id")
    if not isinstance(report, dict):
        raise ValueError("invalid report")
    rows = [row for row in audit_rows if isinstance(row, dict) and row.get("task_id") == task_id]
    images = []
    skipped = missing = total = 0
    if include_shots:
        from PIL import Image, ImageDraw

        if shots_dir is None:
            shots_dir = Path(__file__).with_name("safe_tap_shots")
        root = Path(shots_dir).resolve()
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            root_fd = None
        try:
            for name, in_window in _shot_paths(rows, shots_dir):
                if not in_window:
                    skipped += 1
                    continue
                if root_fd is None:
                    missing += 1
                    continue
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
                except FileNotFoundError:
                    missing += 1
                    continue
                except OSError as error:
                    raise ValueError("invalid screenshot path") from error
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                        raise ValueError("invalid screenshot path")
                    if info.st_size > max_shot_bytes - total:
                        raise ValueError("screenshot bytes exceed 20 MB")
                    try:
                        image = Image.open(source)
                    except Image.DecompressionBombError as error:
                        raise ValueError("screenshot dimensions too large") from error
                    with image:
                        if image.width * image.height > 20_000_000:
                            raise ValueError("screenshot dimensions too large")
                        image.load()
                        image = image.convert("RGB")
                        image.info.clear()
                        top = min(image.height, math.ceil(image.height * .07))
                        ImageDraw.Draw(image).rectangle((0, 0, image.width - 1, top - 1), fill="black")
                        encoded = io.BytesIO()
                        image.save(encoded, format="JPEG", quality=85, exif=b"", icc_profile=None)
                        data = encoded.getvalue()
                total += len(data)
                if total > max_shot_bytes:
                    raise ValueError("screenshot bytes exceed 20 MB")
                images.append(data)
                if len(images) > max_shots:
                    raise ValueError("screenshot count exceeds 40")
        finally:
            if root_fd is not None:
                os.close(root_fd)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", _readme(skipped, missing))
        archive.writestr("report.json", json.dumps(redact(report), ensure_ascii=False, allow_nan=False) + "\n")
        archive.writestr("audit.jsonl", "".join(json.dumps(redact(row), ensure_ascii=False, allow_nan=False) + "\n" for row in rows))
        for index, data in enumerate(images, 1):
            archive.writestr(f"screenshots/{index:02d}.jpg", data)
    return output.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--audit-offset", required=True, type=int)
    parser.add_argument("--shots", action="store_true")
    parser.add_argument("--shots-dir", type=Path, default=Path(__file__).with_name("safe_tap_shots"))
    args = parser.parse_args(argv)
    try:
        size = args.audit.stat().st_size
        if args.audit_offset < 0 or args.audit_offset > size or size - args.audit_offset > task_report.MAX_AUDIT_WINDOW:
            raise ValueError("invalid audit window")
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("report too large")
        report = json.loads(data)
        with args.audit.open("rb") as stream:
            stream.seek(args.audit_offset)
            rows = [json.loads(line, parse_constant=task_report._invalid_constant)
                    for line in stream if line.strip()]
        sys.stdout.buffer.write(build_zip(rows, report, args.task_id, args.shots, args.shots_dir))
    except ValueError as error:
        if str(error).startswith(("screenshot count exceeds", "screenshot bytes exceed",
                                  "screenshot dimensions too large")):
            parser.exit(4, "screenshot_limit_exceeded\n")
        parser.exit(3, "feedback export unavailable; no raw content printed\n")
    except (OSError, TypeError, UnicodeError, OverflowError, RecursionError):
        parser.exit(3, "feedback export unavailable; no raw content printed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
