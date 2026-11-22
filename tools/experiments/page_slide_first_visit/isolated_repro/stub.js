// 测试桩（仅存在于临时目录的 frontend 副本里）：模拟 WebView2 桥。
// get_initial_state 返回可渲染的最小数据集（模板只需模块 id），其余调用回空对象。
// 响应异步化（setTimeout 8ms）以贴近真实 RPC 时序。
(function () {
  'use strict';
  const listeners = new Set();
  function respond(callId, method) {
    let data = {};
    if (method === 'get_initial_state') {
      data = {
        version: '0.0.0-test', is_running: false, model_ok: true,
        modules: [{ id: 'treasure', name: '鉴宝', expired: false, valid_until: '' }],
        queue: { order: ['treasure'], enabled: { treasure: true } },
        breakpoints: {}, module_assets: {}, cover_default: '',
      };
    } else if (method === 'get_status') {
      data = { is_running: false, round_result: null, module_assets: {}, cfg: {} };
    }
    const evt = { data: { type: 'response', callId, ok: true, data } };
    setTimeout(() => { listeners.forEach((fn) => fn(evt)); }, 8);
  }
  window.chrome = {
    webview: {
      postMessage(msg) { if (msg && msg.type === 'call') respond(msg.callId, msg.method); },
      addEventListener(t, fn) { if (t === 'message') listeners.add(fn); },
    },
  };
})();
