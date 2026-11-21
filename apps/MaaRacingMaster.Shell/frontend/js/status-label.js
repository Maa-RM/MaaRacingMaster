/* 底栏 / PEEP 标题栏的「当前 X」文案（两个窗口同源，不各写一遍）。
 *
 * 只取两样东西：模块自报的阶段名，与运行项名（模块或内置步骤的 NAME）。
 * **不取调试流归属**：debug_stage 是导航图此刻在试的节点（如
 * startup.popup.close_marketing），含义是「正在匹配哪个控件」——排障用的内部细节，
 * 界面上读不出东西。它仍在 PEEP 画面底带与调试索引 index.jsonl 里，没丢。
 *
 * 阶段为空时必须回落到运行项名；返回空串是「有活干却取不到标签」的信号，
 * 调用方据此显示「停止中...」——内置步骤没有阶段，落回空串就会整条启动链都这样。
 */
(function () {
  'use strict';

  function activeLabel(status) {
    if (!status) return '';
    if (status.current_stage) return String(status.current_stage);
    var run = status.queue_run;
    if (run && run.current_name) return String(run.current_name);
    return '';
  }

  var api = { activeLabel: activeLabel };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.MaaRMStatusLabel = api;
})();
