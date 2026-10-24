package com.dsb.assistant;

import org.json.JSONObject;

import android.content.Context;

/** Task protocol v2 over the permission-gated Termux channel. No HTTP fallback. */
public class DshClient {
    public static class Result {
        public boolean ok;
        public String text = "";
        public JSONObject action;
        public String error = "";
        public String failureKind = "";
        public int statusCode;
        ExecutionReport.Summary reportSummary;
        ExecutionReport.Reference reportReference;
    }

    static String taskReceiptError(String path, JSONObject request, JSONObject response) {
        if (!path.equals("/do") && !path.equals("/ask") && !path.equals("/step") && !path.equals("/cancel")) return "";
        Object id = request.opt("task_id");
        Object protocol = response.opt("protocol");
        if (!(id instanceof String) || ((String) id).isEmpty() || !id.equals(response.opt("task_id")))
            return "Task acknowledgement identity mismatch";
        if (!(protocol instanceof Number) || ((Number) protocol).doubleValue() != 2.0)
            return "Task protocol v2 acknowledgement required";
        String proof = path.equals("/cancel") ? "stop_confirmed" : "transport_settled";
        if (!Boolean.TRUE.equals(response.opt(proof))) return "Task acknowledgement not confirmed: " + proof;
        return "";
    }

    public static Result post(Context context, String base, String path, JSONObject body, int readTimeoutMs) {
        Result r = new Result();
        try {
            JSONObject envelope = TermuxRunner.request(context, path, body, readTimeoutMs);
            int code = envelope.getInt("status");
            r.statusCode = code;
            if (code < 200 || code >= 300) {
                r.failureKind = "http";
                r.error = "HTTP " + code;
                return r;
            }
            JSONObject o = envelope.getJSONObject("body");
            if (!(o.opt("ok") instanceof Boolean)) {
                r.failureKind = "response";
                r.error = "Missing or non-boolean acknowledgement";
                return r;
            }
            r.ok = Boolean.TRUE.equals(o.opt("ok"));
            if (r.ok) {
                String invalid = taskReceiptError(path, body, o);
                if (!invalid.isEmpty()) {
                    r.ok = false;
                    r.failureKind = "response";
                    r.error = invalid;
                    return r;
                }
                if (path.equals("/do")) {
                    r.reportSummary = ExecutionReport.summary(o.optJSONObject("report_summary"));
                    String taskId = body.optString("task_id", "");
                    String token = o.optString("report_token", "");
                    if (ExecutionReport.validReference(taskId, token))
                        r.reportReference = new ExecutionReport.Reference(taskId, token);
                }
            }
            r.text = o.optString("text", "");
            if (o.has("action") && !o.isNull("action")) r.action = o.optJSONObject("action");
            if (!r.ok) {
                r.failureKind = "rejected";
                r.error = o.optString("error", o.optString("stderr", ""));
            }
            if (r.text.isEmpty()) r.text = o.optString("raw", "");
            return r;
        } catch (Throwable t) {
            r.ok = false;
            r.failureKind = t instanceof java.net.SocketTimeoutException ? "timeout"
                : t instanceof java.io.IOException ? "transport" : "response";
            r.error = String.valueOf(t);
            return r;
        }
    }

    static ExecutionReport queryReport(Context context, ExecutionReport.Reference reference) {
        if (reference == null || !ExecutionReport.validReference(reference.taskId, reference.token)) return null;
        try {
            JSONObject request = new JSONObject().put("task_id", reference.taskId)
                    .put("report_token", reference.token);
            JSONObject envelope = TermuxRunner.request(context, "/report", request, 15000);
            if (envelope.optInt("status", 0) != 200) return null;
            JSONObject body = envelope.optJSONObject("body");
            if (body == null || !Boolean.TRUE.equals(body.opt("ok"))) return null;
            return ExecutionReport.detail(body.optJSONObject("report"), reference.taskId);
        } catch (Throwable ignored) {
            return null;
        }
    }
}
