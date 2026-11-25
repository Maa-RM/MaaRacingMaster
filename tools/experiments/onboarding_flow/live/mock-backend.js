/* 实验用 mock 后端：模拟 WebView2 桥，让真实前端在没有 Python sidecar 时也能跑。
   只在实验目录使用，不进入生产。 */
(function () {
  'use strict';

  var MODULES = [
    {
      id: 'start_game', name: '启动游戏', stages: [], kind: 'builtin',
      requires_gamepad_exclusive: false, expired: false, valid_until: null,
      has_dashboard: false, cover: '', config_defaults: {}
    },
    {
      id: 'close_game', name: '关闭游戏', stages: [], kind: 'builtin',
      requires_gamepad_exclusive: false, expired: false, valid_until: null,
      has_dashboard: false, cover: '', config_defaults: {}
    }
  ];

  var queue = { order: [], enabled: {} };
  var listeners = [];

  function readQuery() {
    return new URLSearchParams(
      location.search.replace(/^\?/, '') + '&' + location.hash.replace(/^#/, '')
    );
  }

  // 截图/演示预设：?queue=start_game,close_game
  (function presetQueue() {
    var q = readQuery();
    var raw = q.get('queue');
    if (!raw) return;
    var ids = raw.split(',').map(function (s) { return s.trim(); })
      .filter(function (id) { return MODULES.some(function (m) { return m.id === id; }); });
    queue.order = ids;
    ids.forEach(function (id) { queue.enabled[id] = true; });
  })();

  function configFor(id) {
    return { config: {}, _ui: { groups: [] }, _rev: 1, _choices: {}, _summary: [], _status: '' };
  }

  var handlers = {
    get_initial_state: function () {
      return {
        version: '1.0.0-demo',
        modules: MODULES,
        cover_default: '',
        queue: queue,
        breakpoints: {},
        module_assets: {},
        model_ok: true,
        is_running: false
      };
    },
    get_status: function () {
      return {
        is_running: false, worker_active: false, model_ok: true,
        queue_run: null, current_module: '', current_stage: '',
        module_assets: {}, round_result: null
      };
    },
    get_debug_state: function () {
      return {
        debug_mode: false, peep_enabled: false, emergency_stop_enabled: false,
        file_logging: false, mute_game: false, boost_priority: false,
        intent_mode: false, click_mode: 'real'
      };
    },
    fetch_logs: function () {
      return { schema_version: 1, events: [], truncated: false };
    },
    get_module_config: function (p) {
      return configFor(p && p.module_id);
    },
    set_run_queue: function (p) {
      var order = Array.isArray(p && p.order) ? p.order.filter(function (id) {
        return MODULES.some(function (m) { return m.id === id; });
      }) : [];
      var enabled = {};
      order.forEach(function (id) { enabled[id] = true; });
      queue = { order: order, enabled: enabled };
      return queue;
    },
    get_registry_optimizations: function () { return { items: [] }; },
    get_optional_dependencies: function () { return { items: [] }; },
    get_data_usage: function () { return { root: '', total: 0, dirs: [] }; },
    get_achievements: function () { return { groups: [], summary: {}, state: 'ok' }; },
    get_module_dashboard: function () { return null; },
    get_module_info: function () { return null; },
    set_debug_mode: function () { return {}; },
    set_emergency_stop: function () { return {}; },
    set_file_logging: function () { return {}; },
    set_intent_mode: function () { return {}; },
    set_mute_game: function () { return {}; },
    set_boost_priority: function () { return {}; },
    set_click_mode: function () { return {}; },
    open_user_data_folder: function () { return {}; },
    open_external_url: function () { return {}; }
  };

  function dispatch(msg) {
    listeners.forEach(function (fn) {
      try { fn({ data: msg }); } catch (e) { console.error(e); }
    });
  }

  window.chrome = window.chrome || {};
  window.chrome.webview = {
    addEventListener: function (type, fn) {
      if (type === 'message' && typeof fn === 'function') listeners.push(fn);
    },
    removeEventListener: function (type, fn) {
      listeners = listeners.filter(function (x) { return x !== fn; });
    },
    postMessage: function (msg) {
      if (!msg || msg.type !== 'call') return;   // warmup-done / win-action 等直接忽略
      setTimeout(function () {
        var fn = handlers[msg.method];
        if (typeof fn !== 'function') {
          dispatch({ type: 'response', callId: msg.callId, ok: true, data: {} });
          return;
        }
        try {
          dispatch({ type: 'response', callId: msg.callId, ok: true, data: fn(msg.params || {}) });
        } catch (e) {
          dispatch({ type: 'response', callId: msg.callId, ok: false, error: String(e && e.message || e) });
        }
      }, 0);
    }
  };
})();
