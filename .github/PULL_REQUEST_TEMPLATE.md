## 变更内容

<!-- 简要说明这次 PR 做了什么，为什么做。 -->

- [ ] 新增功能 / 修复 Bug / 重构 / 文档（选一）
- [ ] 一个 PR 只做一件事（混杂改动请拆开；接受标准见 [CONTRIBUTING](https://github.com/Maa-RM/MaaRacingMaster/blob/master/CONTRIBUTING.md#4-我们接受什么样的-pr)）
- [ ] 关联 Issue：#<!-- 如 Closes #123 -->

## 改动说明

<!-- 涉及哪些文件、哪些模块、改了什么。改到规模软线（400 行 / 20 文件）以上时，说明为什么不能拆。 -->

## 自测清单

<!-- 提交前请勾选，未验证的项目请说明原因 -->

- [ ] 本地运行通过（无导入错误 / 无崩溃）
- [ ] 相关模块流程无回归（鉴宝 / 导航等）
- [ ] 已阅读对应对象的知识文档（[项目地图](https://github.com/Maa-RM/MaaRacingMaster/blob/master/docs/CODE_WIKI.md) 指路：平台核心层 / 活动域 / MaaFW 协议各见其 home）
- [ ] 新增模块走了能力接口，未直接引用高权限宿主对象
- [ ] 若新增了抽象 / 新通路：已答「第二个使用者是谁」（判据见 [AGENTS.md](https://github.com/Maa-RM/MaaRacingMaster/blob/master/AGENTS.md)「作业纪律与提交红线」中「抽公共层」那一条）
- [ ] 代码缩进 4 空格，注释为中文
- [ ] 未为让 CI 变绿而删用例、加 skip、改断言或改 workflow 门禁（若确有预期失败，已在描述里说明原因）

## 测试方式

<!-- 如何验证这个改动？给出跑过的命令与结果（如 `pytest tests/xxx -q` → 12 passed），截图更好。 -->

## AI 参与

<!-- 哪些部分由 AI 生成、你亲自验证了什么。没有 AI 参与可写「无」。 -->

## 其他

<!-- 需要维护者特别注意的地方 -->
