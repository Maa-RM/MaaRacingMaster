import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

globalThis.window = {};
const require = createRequire(import.meta.url);
require('../../apps/MaaRacingMaster.Shell/frontend/js/number-pill.js');
const NUM = window.MaaRM.numberPill.model;

test('夹取：越界收到边界，非数值回落到 min', () => {
  assert.equal(NUM.clamp(135, 10, 120), 120);
  assert.equal(NUM.clamp(5, 10, 600), 10);
  assert.equal(NUM.clamp(300, 30, 900), 300);
  assert.equal(NUM.clamp('abc', 10, 600), 10);
  assert.equal(NUM.clamp(120, 10, null), 120);   // 无上限时不封顶
});

test('列数：按 max 的整数位数，缺 max 不给列', () => {
  assert.equal(NUM.placesOf(99), 2);      // 循环轮数
  assert.equal(NUM.placesOf(600), 3);     // 等待游戏窗口 600 秒
  assert.equal(NUM.placesOf(900), 3);     // 等待进入大厅 900 秒
  assert.equal(NUM.placesOf(null), 0);
  assert.equal(NUM.placesOf(99.9), 2);    // 小数上限按整位算
});

test('能否滚动：只认非负整数的上下限与步长', () => {
  assert.equal(NUM.canRoll(0, 99, 1), true);      // 循环轮数
  assert.equal(NUM.canRoll(10, 600, 10), true);   // 等待游戏窗口
  assert.equal(NUM.canRoll(30, 900, 30), true);   // 等待进入大厅
  assert.equal(NUM.canRoll(0, null, 1), false);   // 缺上限 = 没有列位
  assert.equal(NUM.canRoll(0, 99, 0.5), false);   // 小数步长滚不了
  assert.equal(NUM.canRoll(-5, 99, 1), false);    // 列位里没有符号位
  assert.equal(NUM.canRoll(0, 99, 0), false);     // 非正步长
});

test('读屏文本：0 可读成不限，其余带单位', () => {
  assert.equal(NUM.valueText(0, '轮', '不限'), '不限');
  assert.equal(NUM.valueText(10, '轮', '不限'), '10 轮');
  assert.equal(NUM.valueText(120, '秒', ''), '120 秒');
  assert.equal(NUM.valueText(0, '秒', ''), '0 秒');
});
