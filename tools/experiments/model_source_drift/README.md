status: active

# model_source_drift — 一键安装模型的下载地址到底经历几次转手

## 背景

v0.32.4-dev.2 面板报「安装失败 · 所有下载源均失败（cnb: 拒绝下载重定向到未知地址：…）」。
第一轮定位已确认：`ModelInstaller.ALLOWED_HOSTS` 只列发行货架主机，而两个货架都把
`/-/releases/download/...` 302 给自家资产 CDN（`asset.cnb.cool` / `release-assets.githubusercontent.com`），
白名单外的第一跳就被 `core.download` 的跳转守卫拦死（修复见 master 提交 `7027785`）。

本实验要回答的是那一轮没回答的三件事：

1. **出厂过一键安装的版本，是不是一版都下不下来**（还是只有 dev.2 坏了）；
2. **每一跳到底落在谁家**，两个源是否同构；
3. **发版时地址再变会不会重新炸**，静态主机名单这条判据还要不要留。

## 要回答 / 判据

判据分两层，不混用：

- **资产层**：HTTP 200/206 且拿到总字节数 = 有字节；404/410 = 发行缺件；连不上 = 未验成（不是缺件）。
- **策略层**：三套「这条链允不允许走」的纯函数（`source_policy.py`），只看 URL 链，不看字节：
  - **P0**：v0.32.1 出厂快照（`github.com` / `objects.githubusercontent.com` / `codeload.github.com` / `cnb.cool`），历史基线，不随后续修复改动；
  - **P1**：当前生产 `ALLOWED_HOSTS`（P0 + 两个资产 CDN）；
  - **P2**：候选判据——首发必须落在货架，跳转只要求 https + 公网主机（不带口令、非回环/内网/链路本地），真伪交给描述里的 SHA256 与 zip 成员白名单（生产已强制）。

「出厂过一键安装」的判定不看印象：`git show <tag>:.github/workflows/release.yml` 里传了
`-ModelDescriptor` 才会把 `download.json` 打进主包。v0.30.0~v0.32.0 只产模型 ZIP 不注入描述，
那一代只有手动安装，拿它们的 Release 缺件当故障是栽赃。

## 怎么跑

```bash
python tools/experiments/model_source_drift/probe.py                 # 打 JSON 到 stdout
python tools/experiments/model_source_drift/probe.py --write-report tools/experiments/model_source_drift/report_out
python tools/experiments/model_source_drift/server.py --port 8765    # 起页面 + /api/matrix
```

后端每次探测对每源发一次 `Range: bytes=0-15` 请求（拿状态、跳转链、总字节），传输失败按 1s 退避重试，
不写任何生产状态。前端是一张表：tag × 源 → 资产 / 字节 / 跳转链 / P0·P1·P2 判定，另有一段
「假想漂移」用例（不改网络也能判策略差异）。

## 结果（2026-11-26 实测）

出厂过一键安装的 5 个 tag（`v0.32.1-dev.1` 起）：10 条链路里 6 条真拿到字节，
**这 6 条 P0 全部拒绝**；另外 4 条本机连不上 github，判不了，记「未验成」。

| tag | 源 | 资产 | 跳转 | P0 | P1 | P2 |
|---|---|---|---|---|---|---|
| v0.32.4-dev.2 | cnb | 206 · 9933629 B | 302 → `asset.cnb.cool/assets/t/<令牌>` | REJECT | PASS | PASS |
| v0.32.4-dev.2 | github | 未验成（连接被重置） | — | — | — | — |
| v0.32.3-dev.1 | cnb | 206 | 302 → `asset.cnb.cool` | REJECT | PASS | PASS |
| v0.32.3-dev.1 | github | 未验成 | — | — | — | — |
| v0.32.2-dev.2 | cnb | 206 | 302 → `asset.cnb.cool` | REJECT | PASS | PASS |
| v0.32.2-dev.2 | github | 未验成 | — | — | — | — |
| v0.32.2-dev.1 | cnb | 206 | 302 → `asset.cnb.cool` | REJECT | PASS | PASS |
| v0.32.2-dev.1 | github | 未验成 | — | — | — | — |
| v0.32.1-dev.1 | cnb | 206 | 302 → `asset.cnb.cool` | REJECT | PASS | PASS |
| v0.32.1-dev.1 | github | 206 | 302 → `release-assets.githubusercontent.com` | REJECT | PASS | PASS |

三条结论：

1. **cnb 侧 5/5 都是 302 → `asset.cnb.cool`**，P0 全部拒绝。出厂至今（v0.32.1 → v0.32.4-dev.2）
   **没有任何一版的一键安装能成功**，dev.2 只是第一个被用户点到的版本。
2. 「资产」和「判据」必须分开看：没拿到字节的链路只判到首跳，把它算成策略 PASS 会把
   「连不上」误读成「策略放行、所以是资产的问题」；所以面板对这些行不判，记「未验成」。
3. github 侧在生产同栈（urllib，同 UA）下 5 次探测有 4 次连接被重置，cnb 5/5 成功。
   这不是策略问题，但正好说明**双源回退的价值**：主源取 cnb、github 作回退，而不是反过来。

## 假想漂移（判据层，非真实 CDN 行为）

| 场景 | P0 | P1 | P2 |
|---|---|---|---|
| 货架直链（今天第一跳） | PASS | PASS | PASS |
| github 当前资产 CDN | REJECT | PASS | PASS |
| cnb 当前资产 CDN | REJECT | PASS | PASS |
| **未来换成未知 CDN** | REJECT | **REJECT** | PASS |
| 跳转被降级成 http | REJECT | REJECT | REJECT |
| 跳转指向内网元数据 `169.254.169.254` | REJECT | REJECT | REJECT |
| URL 里带口令 | REJECT | REJECT | REJECT |

要读的是第三行：**P1 是把这次的 CDN 名字固化进判据**，货架下次换域名（GitHub 2024 年就换过一次：
`objects.githubusercontent.com` → `release-assets.githubusercontent.com`）就会原样复现事故；
P2 把「主机可信」降级为「首发可信 + 真伪交给哈希」，换 CDN 不再需要改代码，同时仍拒绝 http 降级、
内网地址与带口令 URL。

P2 是否采纳属于设计决策（判据从「主机名单」换成「首发 + 哈希约束」），本实验只给出判据层差异；
采纳前须独立复核（涉及下载安全边界）。在采纳之前，生产口径是 P1 + 本次修复。

## 局限

- 资产层只验到「有字节、总字节数」，没有对 5 个 tag 各下 10MB 去比 SHA256；哈希绑定已由
  `tests/` 与 v0.32.4-dev.2 的两源全量下载（字节一致、成员校验全过）覆盖。
- 漂移用例是手写 URL 的纯函数判定，不代表任何真实 CDN 未来行为。
- github 侧连通性抖动属本机网络现象，样本 5 条，不足以单独成结论；它只用来解释为什么矩阵里
  有几行是「未验成」。
