window.__ModuleLoader__.load({
  id: 'dsb-report',
  factory: require => {
    const React = require('react');
    const { jsx, jsxs } = require('react/jsx-runtime');
    const ACTIONS = {"tap":"点击","swipe":"滑动","key":"按键","scroll":"滚动","long_press":"长按","drag":"拖动","type":"输入动作","open":"打开应用","handoff":"人工接管","command":"命令检查","look":"观察画面","wait.app":"等待界面","wait.stable":"等待界面","wait.text":"等待界面","wait.text.ocr":"等待界面"};
    const RESULTS = {"dispatched":"已派发","refused":"已拒绝","handoff":"等待接管","observed":"已观察","unverified":"未核实","failed":"操作未成功","opened":"已打开"};
    const REASONS = {"G1_NO_SHOT":"缺少当前画面凭据，已拒绝。","G1_MISMATCH":"画面凭据不匹配，已拒绝。","G1_DISPLAY":"画面凭据不属于当前屏幕，已拒绝。","G1_CONSUMED":"旧画面凭据不能复用，已拒绝。","G1_STALE":"画面已过时，已拒绝。","G1_EXPIRED":"画面凭据已过期，已拒绝。","G1_FOREGROUND":"前台页面已变化，已拒绝。","G1_OTHER":"画面或屏幕绑定无法确认，已拒绝。","G2_DANGER_ZONE":"目标位于危险操作区域，已拒绝。","G2B_KEY":"页面结构不可见，危险按键已拦下，需人工接管。","G2B_ACK":"无法确认危险按钮，已暂停等待确认。","G2B_OCR_UNAVAILABLE":"文字识别不可用，无法确认安全，已拒绝。","G2B_OCR_DANGER":"识别到危险操作，已拒绝。","G2B_OCR_BAND":"目标落在危险文字所在区域，已拒绝。","G2B_VISION_RISK":"视觉判断目标有风险，需人工接管。","G2B_VISION_UNAVAILABLE":"危险文字存在且视觉判断不可用，已拒绝。","G3_EDGE":"目标位于系统手势边缘，已拒绝。","G3_BOUNDS":"目标超出屏幕，已拒绝。","G4_BUDGET":"重复动作预算已用尽，需人工接管。","G4_CONFIG":"动作预算配置无效，已拒绝。","G5_AUDIT_FAILED":"留证失败，结果不能确认。","R3_CHANGED":"目标区域已变化，动作没有下发。","R3_OTHER":"派发前目标复核未通过，动作没有下发。","VD_REQUIRED":"目标应用应在隔离屏幕操作。","VD_POLICY":"屏幕隔离策略已拦下操作。","CHECK_GATE_PRIVACY":"命令可能暴露隐私，已暂停等待确认。","CHECK_GATE_EXFILTRATION":"命令可能向外发送本机数据，已拦下。","CHECK_GATE_DESTRUCTIVE":"命令可能删除或不可逆地修改数据，已拦下。","CHECK_GATE_SYSTEM_CHANGE":"命令可能更改系统设置，已拦下。","CHECK_GATE_OTHER":"命令钩子已拦下操作。","SHADOW_WOULD_BLOCK":"观察模式提示可能拦截，但命令实际放行。","TYPESAFE_UNAVAILABLE":"命令判定服务不可用，按现有策略放行；需要留意。","HANDOFF":"任务停在需要人工接管的地方。","OTHER_REFUSAL":"安全检查未通过，动作没有下发。","OP_APP_NOT_FOUND":"没找到这个应用，请确认名称。","OP_FAILED":"操作未成功，请检查后重试。","BATCH_DISABLED":"批量动作尚未开启，没有执行任何步骤。","BATCH_LIMIT":"批量步骤超过上限，没有执行任何步骤。","BATCH_UNSUPPORTED":"批量含不支持的动作，没有执行任何步骤。","BATCH_STEP_INVALID":"批量步骤参数无效，动作没有下发。","BATCH_NO_REACTION":"点按后界面没有确认反应，批量已停止。","BATCH_CANCELLED":"任务已取消，批量已停止。","BATCH_WINDOW_CHANGED":"出现新窗口或弹窗，批量已停止。"};
    const GATE_CODES = new Set(["G1_NO_SHOT","G1_MISMATCH","G1_DISPLAY","G1_CONSUMED","G1_STALE","G1_EXPIRED","G1_FOREGROUND","G1_OTHER","G2_DANGER_ZONE","G2B_KEY","G2B_ACK","G2B_OCR_UNAVAILABLE","G2B_OCR_DANGER","G2B_OCR_BAND","G2B_VISION_RISK","G2B_VISION_UNAVAILABLE","G3_EDGE","G3_BOUNDS","G4_BUDGET","G4_CONFIG","R3_CHANGED","R3_OTHER","VD_REQUIRED","VD_POLICY","OTHER_REFUSAL","BATCH_DISABLED","BATCH_LIMIT","BATCH_UNSUPPORTED","BATCH_STEP_INVALID"]);
    const FEEDBACK_EXPORT = {"without_shots":"打包反馈材料（不含截图）","with_shots":"含截图打包","shot_warning":"截图里的画面内容不会自动脱敏，发出前请自己看一遍"};
    const number = value => Number.isSafeInteger(value) && value >= 0 && value <= 1000000 ? value : null;

    function view(report) {
      const c = report && report.counts || {};
      const coverage = report && report.coverage || {};
      const raw = Array.isArray(report && report.timeline) ? report.timeline : [];
      const timeline = raw.slice(0, 1000).filter(row => row &&
        Object.prototype.hasOwnProperty.call(ACTIONS, row.action_type) &&
        Object.prototype.hasOwnProperty.call(RESULTS, row.result)).map(row => ({
        action: ACTIONS[row.action_type], result: RESULTS[row.result],
        reason: Object.prototype.hasOwnProperty.call(REASONS, row.reason_code) ? REASONS[row.reason_code] : null,
      }));
      if (timeline.length === 0) return null;
      const actions = number(c.dispatched_actions);
      const handoffs = number(c.handoff_points);
      const hookRefusals = number(c.command_hook_refusals);
      const refusalsByReason = c.gate_refusals_by_reason || {};
      const entries = Object.entries(refusalsByReason).filter(([code, value]) =>
        GATE_CODES.has(code) && number(value) !== null);
      const gateRefusals = entries.reduce((sum, [, value]) => sum + value, 0);
      const refused = gateRefusals + (hookRefusals === null ? 0 : hookRefusals);
      const main = entries.sort((a, b) => b[1] - a[1])[0];
      const primary = main ? REASONS[main[0]] : hookRefusals ? '命令钩子拦截' : null;
      const warnings = [];
      if (coverage.concurrent_turns === true) warnings.push('同时有其他会话运行，动作归属不完整。');
      else if (coverage.interrupted === true) warnings.push('本轮异常中断，动作归属不完整。');
      else if (coverage.attribution_complete !== true || coverage.lifecycle_valid !== true ||
          coverage.timeline_truncated === true) warnings.push('部分动作无法确认归属，以上仅为已关联记录。');
      const shadow = number(c.command_hook_shadow_would_block);
      const typesafe = number(c.typesafe_unavailable_warnings);
      if (coverage.command_hook_linked === true) {
        warnings.push('观察模式本应拦截（实际放行）：' + (shadow === null ? '未核实' : shadow + ' 次'));
        warnings.push('TypeSafe 不可用、按现有策略放行：' + (typesafe === null ? '未核实' : typesafe + ' 次'));
      }
      return {timeline, lines: [
        actions === null ? '动作记录未核实' : '做了 ' + actions + ' 个动作',
        refused === 0 ? '未见已关联的拒绝' :
          '拒绝了 ' + refused + ' 次（主要原因：' + (primary || '安全检查') + '）',
        handoffs === null ? '接管记录未核实' : handoffs + ' 处需要你接管',
      ], warnings, detailNote: coverage.command_hook_linked === true ? null : '命令层审查未覆盖本轮。'};
    }

    function ReportCard({sessionId, turn, seq}) {
      const [report, setReport] = React.useState(null);
      const [expanded, setExpanded] = React.useState(false);
      const [retry, setRetry] = React.useState(0);
      const turnNo = turn && turn.turn;
      const closingSeq = turn?.end?.seq ?? seq;
      React.useEffect(() => {
        if (!Number.isSafeInteger(turnNo) || !Number.isSafeInteger(closingSeq)) return undefined;
        let live = true, timer, tries = 0;
        const load = () => window.fetch('/dsb-report/api?session=' + encodeURIComponent(sessionId) +
          '&turn=' + turnNo, {credentials: 'same-origin', cache: 'no-store'})
          .then(response => response.status === 200 ? response.json() :
            response.status === 202 ? {pending: true} : null)
          .then(value => {
            if (!live) return;
            if (value && value.ok === true) setReport(view(value.report));
            else if (value && value.pending && tries++ < 8) timer = window.setTimeout(load, 750);
            else if (value && value.pending) setReport({ unavailable: '执行报告仍在生成，请稍后重试。' });
            else setReport({ unavailable: '执行报告生成失败，请稍后重试。' });
          }).catch(() => { if (live) setReport({ unavailable: '执行报告查询失败，请稍后重试。' }); });
        load();
        return () => { live = false; window.clearTimeout(timer); };
      }, [sessionId, turnNo, closingSeq, retry]);
      if (!report) return null;
      const base = {fontSize: 12, lineHeight: 1.5, color: 'var(--dsw-alias-label-secondary)',
        borderTop: '1px solid var(--dsw-alias-border-l3)', padding: '8px 0', marginTop: 4};
      if (report.unavailable) return jsxs('section', {style: base, 'aria-label': '执行报告', children: [
        jsx('div', {children: report.unavailable}),
        jsx('button', {type: 'button', onClick: () => setRetry(retry + 1), children: '重试'}),
      ]});
      return jsxs('section', {style: base, 'aria-label': '执行报告', children: [
        jsx('strong', {style: {fontSize: 13, color: 'var(--dsw-alias-label-primary)'}, children: '执行报告'}),
        ...report.lines.map((line, i) => jsx('div', {children: line}, 'line-' + i)),
        ...report.warnings.map((line, i) => jsx('div', {children: line}, 'warning-' + i)),
        jsx('button', {type: 'button', onClick: () => setExpanded(!expanded),
          style: {padding: '4px 0', border: 0, background: 'none', cursor: 'pointer',
            color: 'var(--dsw-alias-label-primary)'}, children: expanded ? '收起详情' : '查看详情'}),
        expanded ? jsxs('div', {children: [
          report.detailNote ? jsx('div', {children: report.detailNote}) : null,
          jsx('ol', {style: {margin: '4px 0', paddingLeft: 20}, children:
            report.timeline.map((item, i) => jsx('li', {children:
              item.action + ' · ' + item.result + (item.reason ? ' · ' + item.reason : '')}, i))}),
          jsxs('div', {style: {display: 'flex', gap: 12, flexWrap: 'wrap'}, children: [
            jsx('a', {href: '/dsb-report/api/export?session=' + encodeURIComponent(sessionId) +
              '&turn=' + turnNo, download: true, children: FEEDBACK_EXPORT.without_shots}),
            jsx('a', {href: '/dsb-report/api/export?session=' + encodeURIComponent(sessionId) +
              '&turn=' + turnNo + '&shots=1', download: true, children: FEEDBACK_EXPORT.with_shots}),
          ]}),
          jsx('div', {children: FEEDBACK_EXPORT.shot_warning}),
        ]}) : null,
      ]});
    }
    return {inject: ['slots'], apply(ctx) {
      ctx.slots.inject('conversation.chat.turnTail', () => ctx.slots.register({
        name: 'conversation.chat.turnTail', id: 'dsb-report', order: 30,
        inject: sessionId => ({sessionId}),
      }, ReportCard));
    }};
  }
});
