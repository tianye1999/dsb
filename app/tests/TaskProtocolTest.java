import java.util.HashMap;
import java.util.Map;

public class TaskProtocolTest {
    static final String ID = "11111111-1111-4111-8111-111111111111";
    static int count;
    static void check(boolean ok, String name) {
        if (!ok) throw new AssertionError(name);
        count++;
    }
    static JSONObject request() { return new JSONObject().put("task_id", ID); }
    static JSONObject reply() { return request().put("protocol", 2).put("transport_settled", true).put("stop_confirmed", true); }
    public static void main(String[] args) {
        for (String route : new String[]{"/do", "/ask", "/step", "/cancel"}) {
            check(taskReceiptError(route, request(), reply()).isEmpty(), "valid " + route);
            check(!taskReceiptError(route, request(), new JSONObject().put("ok", true)).isEmpty(), "old bare ok " + route);
            check(!taskReceiptError(route, new JSONObject(), reply()).isEmpty(), "missing request ID " + route);
            for (Object id : new Object[]{"", "other", 123, true, null})
                check(!taskReceiptError(route, request(), reply().put("task_id", id)).isEmpty(), "wrong response ID " + route);
            for (Object protocol : new Object[]{"2", 1, 2.5, true, null})
                check(!taskReceiptError(route, request(), reply().put("protocol", protocol)).isEmpty(), "bad protocol " + route);
            String field = route.equals("/cancel") ? "stop_confirmed" : "transport_settled";
            for (Object flag : new Object[]{false, "true", 1, null})
                check(!taskReceiptError(route, request(), reply().put(field, flag)).isEmpty(), "bad confirmation " + route);
        }
        check(taskReceiptError("/probe", new JSONObject(), new JSONObject()).isEmpty(), "non-task route unchanged");
        System.out.println("PASS task protocol " + count + " assertions (actual validator, object stand-in; not JSON/HTTP/device E2E)");
    }
    static class JSONObject {
        final Map<String,Object> fields = new HashMap<>();
        JSONObject put(String key, Object value) { fields.put(key, value); return this; }
        Object opt(String key) { return fields.get(key); }
    }
    /* PRODUCTION_VALIDATOR */
}
