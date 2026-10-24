package com.dsb.assistant;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.regex.Pattern;
import org.json.JSONArray;
import org.json.JSONObject;

/** Only the fixed, user-facing fields of a private execution report. */
final class ExecutionReport {
    private static final Pattern ID = Pattern.compile("[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}");
    private static final Pattern TOKEN = Pattern.compile("[0-9a-fA-F]{64}");

    static final class Reference {
        final String taskId;
        final String token;
        Reference(String taskId, String token) { this.taskId = taskId; this.token = token; }
    }

    static final class Summary {
        final String text;
        final boolean incomplete;
        Summary(String text, boolean incomplete) { this.text = text; this.incomplete = incomplete; }
    }

    final List<String> timeline;
    final boolean incomplete;
    private ExecutionReport(List<String> timeline, boolean incomplete) {
        this.timeline = Collections.unmodifiableList(timeline);
        this.incomplete = incomplete;
    }

    static boolean validReference(String taskId, String token) {
        return taskId != null && ID.matcher(taskId).matches() && token != null && TOKEN.matcher(token).matches();
    }

    private static Integer count(JSONObject object, String key) {
        Object value = object.opt(key);
        if (!(value instanceof Number)) return null;
        double number = ((Number) value).doubleValue();
        if (number < 0 || number > 1000000 || number != Math.floor(number)) return null;
        return (int) number;
    }

    static Summary summary(JSONObject object) {
        if (object == null) return null;
        Integer actions = count(object, "actions");
        Integer refusals = count(object, "refusals");
        Integer handoffs = count(object, "handoffs");
        String first = actions == null ? "动作记录未核实" : "做了 " + actions + " 个动作";
        String second = refusals == null ? "拒绝记录未核实" : refusals == 0
                ? "未见已关联的拒绝" : "拒绝了 " + refusals + " 次（主要原因："
                + reason(object.optString("primary_reason_code", "")) + "）";
        String third = handoffs == null ? "接管记录未核实" : handoffs + " 处需要你接管";
        boolean incomplete = !Boolean.FALSE.equals(object.opt("evidence_incomplete"))
                || actions == null || refusals == null || handoffs == null;
        Integer shadow = count(object, "shadow_would_block");
        Integer typesafe = count(object, "typesafe_unavailable_warnings");
        String hookWarnings = !Boolean.TRUE.equals(object.opt("command_hook_linked")) ? "" :
                "\n观察模式本应拦截（实际放行）：" + (shadow == null ? "未核实" : shadow + " 次")
                + "\nTypeSafe 不可用、按现有策略放行：" + (typesafe == null ? "未核实" : typesafe + " 次");
        return new Summary(first + "\n" + second + "\n" + third
                + hookWarnings
                + (incomplete ? "\n部分动作无法确认归属，以上仅为已关联记录。" : ""), incomplete);
    }

    static ExecutionReport detail(JSONObject object, String taskId) {
        if (object == null || taskId == null || !ID.matcher(taskId).matches()) return null;
        JSONObject task = object.optJSONObject("task");
        JSONObject coverage = object.optJSONObject("coverage");
        JSONArray events = object.optJSONArray("timeline");
        if (task == null || !taskId.equals(task.optString("task_id", "")) || coverage == null || events == null)
            return null;
        List<String> lines = new ArrayList<>();
        int size = Math.min(events.length(), 200);
        for (int i = 0; i < size; i++) {
            JSONObject event = events.optJSONObject(i);
            if (event == null) continue;
            String action = action(event.optString("action_type", ""));
            String result = result(event.optString("result", ""));
            String code = event.optString("reason_code", "");
            lines.add(action + " · " + result + (code.isEmpty() ? "" : " · " + reason(code)));
        }
        if (!Boolean.TRUE.equals(coverage.opt("command_hook_linked")))
            lines.add("命令层审查未覆盖本轮。");
        boolean incomplete = !Boolean.TRUE.equals(coverage.opt("lifecycle_valid"))
                || !Boolean.TRUE.equals(coverage.opt("attribution_complete"))
                || Boolean.TRUE.equals(coverage.opt("timeline_truncated")) || events.length() > size;
        return new ExecutionReport(lines, incomplete);
    }

    String displayText() {
        StringBuilder out = new StringBuilder();
        if (incomplete) out.append("部分动作无法确认归属，时间线可能不完整。\n");
        if (timeline.isEmpty()) return out.append("没有可归属的步骤记录。 ").toString().trim();
        for (String line : timeline) out.append(line).append('\n');
        return out.toString().trim();
    }

    private static String action(String code) {
        switch (code) {
            case "tap": return "点击";
            case "swipe": return "滑动";
            case "key": return "按键";
            case "scroll": return "滚动";
            case "long_press": return "长按";
            case "drag": return "拖动";
            case "type": return "输入动作";
            case "open": return "打开应用";
            case "handoff": return "人工接管";
            case "command": return "命令检查";
            case "look": return "观察画面";
            case "wait.app": case "wait.stable": case "wait.text": case "wait.text.ocr": return "等待界面";
            default: return "其他动作";
        }
    }

    private static String result(String code) {
        switch (code) {
            case "dispatched": return "已派发";
            case "opened": return "已打开";
            case "failed": return "操作未成功";
            case "refused": return "已拒绝";
            case "handoff": return "等待接管";
            case "observed": return "已观察";
            default: return "未核实";
        }
    }

    private static String reason(String code) {
        switch (code) {
            case "G1_NO_SHOT": case "G1_MISMATCH": case "G1_DISPLAY": case "G1_CONSUMED":
            case "G1_STALE": case "G1_EXPIRED": case "G1_FOREGROUND": case "G1_OTHER":
                return "当前画面凭据无法确认，动作已拦下";
            case "G2_DANGER_ZONE": case "G2B_OCR_DANGER": case "G2B_OCR_BAND":
            case "G2B_VISION_RISK": return "目标可能涉及危险操作，已拦下";
            case "G2B_KEY": case "G2B_ACK": case "G2B_OCR_UNAVAILABLE":
            case "G2B_VISION_UNAVAILABLE": return "无法确认操作安全，需人工接管";
            case "G3_EDGE": case "G3_BOUNDS": return "目标位于不可安全操作的屏幕区域";
            case "G4_BUDGET": case "G4_CONFIG": return "重复动作已熔断";
            case "G5_AUDIT_FAILED": return "留证失败，结果不能确认";
            case "R3_CHANGED": case "R3_OTHER": return "派发前目标已变化，动作未下发";
            case "VD_REQUIRED": case "VD_POLICY": return "屏幕隔离策略已拦下操作";
            case "CHECK_GATE_PRIVACY": return "命令可能暴露隐私，已暂停";
            case "CHECK_GATE_EXFILTRATION": return "命令可能外发数据，已拦下";
            case "CHECK_GATE_DESTRUCTIVE": return "命令可能不可逆地修改数据，已拦下";
            case "CHECK_GATE_SYSTEM_CHANGE": return "命令可能更改系统设置，已拦下";
            case "CHECK_GATE_OTHER": return "命令钩子已拦下操作";
            case "SHADOW_WOULD_BLOCK": return "观察模式本应拦截，实际未拦截";
            case "TYPESAFE_UNAVAILABLE": return "命令判定服务不可用，按现有策略放行";
            case "HANDOFF": return "任务停下等待人工接管";
            case "OP_APP_NOT_FOUND": return "没找到这个应用，请确认名称";
            case "OP_FAILED": return "操作未成功，请检查后重试";
            default: return "原因未核实";
        }
    }
}
