public class LegacyEntryTest {
    String lastCmd = "", status = "";
    int fallthrough, logs;
    void log(String text) { logs++; }
    void setStatus(String text) { status = text; }
    /* COMMAND_ADMISSION */
    static void check(boolean ok, String message) { if (!ok) throw new AssertionError(message); }
    public static void main(String[] args) {
        int count = 0;
        for (String name : new String[]{"点击", "长按", "滑动", "无障碍做", "盲操"}) {
            for (String suffix : new String[]{"", " 100 200", "\t100 200", ":100 200", "：100 200", " 100 200\nextra"}) {
                LegacyEntryTest s = new LegacyEntryTest();
                s.handleCommand("  " + name + suffix + "  ");
                check(s.fallthrough == 0, "legacy command reached dispatch: " + name + suffix);
                check(s.status.contains("旧执行入口已停用"), "missing explicit refusal: " + name + suffix);
                count++;
            }
        }
        for (String command : new String[]{"停止", "取消任务", "重试停止", "观测 nonce", "观测图:nonce",
                "橙", "蓝", "剪贴板 synthetic", "打开设置", "点击打开设置", "滑动查看更多", "界面元素"}) {
            LegacyEntryTest s = new LegacyEntryTest(); s.handleCommand(command);
            check(s.fallthrough == 1, "unrelated command refused: " + command);
            check(s.status.isEmpty(), "unrelated command reported refusal"); count++;
        }
        for (String command : new String[]{null, "", "  "}) {
            LegacyEntryTest s = new LegacyEntryTest(); s.handleCommand(command);
            check(s.fallthrough == 0 && s.logs == 0, "empty command dispatched"); count++;
        }
        System.out.println("PASS " + count + " real command-admission cases");
    }
}
