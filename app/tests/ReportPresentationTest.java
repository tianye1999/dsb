package com.dsb.assistant;

import android.content.Context;
import org.json.JSONArray;
import org.json.JSONObject;

public final class ReportPresentationTest {
    private static final String ID = "11111111-1111-4111-8111-111111111111";
    private static final String OTHER = "22222222-2222-4222-8222-222222222222";
    private static final String TOKEN = "a".repeat(64);
    private static final String SECRET = "CHAT_ORDER_COMMAND_COORDINATE_SECRET";

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) {
        JSONObject summary = new JSONObject().put("actions", 3).put("refusals", 2)
                .put("primary_reason_code", "G2B_ACK").put("handoffs", 1)
                .put("evidence_incomplete", true).put("lines", new JSONArray().put(SECRET));
        JSONObject reply = new JSONObject().put("ok", true).put("protocol", 2).put("task_id", ID)
                .put("transport_settled", true).put("text", "已回复")
                .put("report_summary", summary).put("report_token", TOKEN);
        TermuxRunner.envelope = new JSONObject().put("status", 200).put("body", reply);
        DshClient.Result result = DshClient.post(new Context(), "", "/do",
                new JSONObject().put("task_id", ID), 1000);
        check(result.ok && result.reportReference != null, "successful task keeps its private reference");
        check(result.reportSummary.text.contains("做了 3 个动作"), "action summary");
        check(result.reportSummary.text.contains("拒绝了 2 次"), "refusal summary");
        check(result.reportSummary.text.contains("1 处需要你接管"), "handoff summary");
        check(result.reportSummary.text.contains("部分动作无法确认归属"), "coverage warning");
        check(!result.reportSummary.text.contains("观察模式本应拦截"), "unlinked shadow hidden");
        check(!result.reportSummary.text.contains("TypeSafe 不可用"), "unlinked TypeSafe hidden");
        check(!result.reportSummary.text.contains(SECRET), "ignore server-provided free text");
        summary.put("shadow_would_block", 2).put("typesafe_unavailable_warnings", 1);
        check(!ExecutionReport.summary(summary).text.contains("观察模式本应拦截"),
                "unlinked hook hides counts even if present");
        summary.put("command_hook_linked", true);
        check(ExecutionReport.summary(summary).text.contains("观察模式本应拦截（实际放行）：2 次"),
                "shadow warning separate from refusals");
        check(ExecutionReport.summary(summary).text.contains("TypeSafe 不可用、按现有策略放行：1 次"),
                "TypeSafe warning separate from refusals");
        summary.put("evidence_incomplete", false);
        check(!ExecutionReport.summary(summary).text.contains("归属"),
                "normal attribution does not show a generic warning");

        JSONObject event = new JSONObject().put("action_type", "tap").put("result", "refused")
                .put("reason_code", "G2B_ACK").put("note", SECRET).put("expect", SECRET)
                .put("vision", SECRET).put("coordinates", SECRET).put("command", SECRET)
                .put("explanation", SECRET);
        JSONObject report = new JSONObject().put("task", new JSONObject().put("task_id", ID))
                .put("coverage", new JSONObject().put("lifecycle_valid", true)
                        .put("attribution_complete", false).put("timeline_truncated", false))
                .put("timeline", new JSONArray().put(event));
        TermuxRunner.envelope = new JSONObject().put("status", 200)
                .put("body", new JSONObject().put("ok", true).put("report", report));
        ExecutionReport detail = DshClient.queryReport(new Context(), result.reportReference);
        check(detail != null && detail.displayText().contains("点击 · 已拒绝"), "whitelisted timeline");
        check(detail.displayText().contains("部分动作无法确认归属"), "detail coverage warning");
        check(detail.displayText().contains("命令层审查未覆盖本轮。"), "unlinked hook detail notice");
        report.optJSONObject("coverage").put("attribution_complete", true);
        check(!ExecutionReport.detail(report, ID).displayText().contains("归属"),
                "normal detail does not show a generic warning");
        report.optJSONObject("coverage").put("command_hook_linked", true);
        check(!ExecutionReport.detail(report, ID).displayText().contains("命令层审查未覆盖"),
                "linked hook has no gap notice");
        report.optJSONArray("timeline").put(new JSONObject().put("action_type", "open")
                .put("result", "opened"));
        report.optJSONArray("timeline").put(new JSONObject().put("action_type", "open")
                .put("result", "failed").put("reason_code", "OP_APP_NOT_FOUND"));
        report.optJSONArray("timeline").put(new JSONObject().put("action_type", "open")
                .put("result", "failed").put("reason_code", "OP_FUTURE"));
        String detailText = ExecutionReport.detail(report, ID).displayText();
        check(detailText.contains("打开应用 · 已打开"), "opened result");
        check(detailText.contains("打开应用 · 操作未成功 · 没找到这个应用"), "operation failure reason");
        check(detailText.contains("打开应用 · 操作未成功 · 原因未核实"), "unknown reason is neutral");
        check(!detail.displayText().contains(SECRET), "no sensitive fields in timeline");
        check("/report".equals(TermuxRunner.lastPath), "private report route");
        check(TermuxRunner.lastBody.length() == 2 && ID.equals(TermuxRunner.lastBody.optString("task_id"))
                && TOKEN.equals(TermuxRunner.lastBody.optString("report_token")), "only task-bound query fields");

        TermuxRunner.envelope = new JSONObject().put("status", 200)
                .put("body", new JSONObject().put("ok", true).put("report", new JSONObject()
                        .put("task", new JSONObject().put("task_id", OTHER))
                        .put("coverage", new JSONObject()).put("timeline", new JSONArray())));
        check(DshClient.queryReport(new Context(), result.reportReference) == null, "reject cross-task report");
        TermuxRunner.envelope = new JSONObject().put("status", 503).put("body", new JSONObject().put("ok", false));
        check(DshClient.queryReport(new Context(), result.reportReference) == null, "query failure degrades");
        check(ExecutionReport.detail(new JSONObject(), null) == null, "null task id degrades");
        TermuxRunner.fail = true;
        check(DshClient.queryReport(new Context(), result.reportReference) == null, "transport failure degrades");
        TermuxRunner.fail = false;
        int before = TermuxRunner.calls;
        check(DshClient.queryReport(new Context(), new ExecutionReport.Reference(ID, "bad")) == null
                && before == TermuxRunner.calls, "invalid token never leaves app");
        TermuxRunner.envelope = new JSONObject().put("status", 200).put("body", reply.put("ok", false));
        check(DshClient.post(new Context(), "", "/do", new JSONObject().put("task_id", ID), 1000)
                .reportReference == null, "failed task receives no token");
        System.out.println("REPORT_PRESENTATION_OK");
    }
}
