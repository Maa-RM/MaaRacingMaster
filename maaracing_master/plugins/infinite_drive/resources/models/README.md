# 模型权重许可声明

本目录按**用途分子目录**存放插件模型权重；**发布包默认不带这些权重**，
由可选模型包提供（安装见下节）。各权重许可以文件边界区分、互不覆盖
（项目其余代码遵循根目录 `LICENSE`，Apache-2.0）：

```
models/
├── perception/model.onnx                                    # 物品感知（YOLO，AGPL-3.0 衍生）
├── depth/relative_mnv4m_slim_spf_448_default.onnx           # 仓库内离线深度验证（DepthART MNV4-M-slim-SPF，Apache-2.0）
└── lane/plus_small_384x640.onnx                             # 线观测（TwinLiteNet+ Small，BDD100K 非商业/科研）
    └── plus_small_fp16_384x640.onnx                         # 同上权重的 FP16 版（DML 专用加速件）
```

## 安装（车道线驾驶）

发布包默认不带 ONNX 权重。车道线驾驶使用车道线和街车检测模型，模型包内容以 [模型清单](../../model_assets.py) 为准。配置面板出现「一键安装模型」时可下载、校验并安装，完成后无需重启。安装落点是 `%APPDATA%\MaaRacingMaster\data\infinite_drive\models\<清单摘要>\`。旧受管包和手动目录里通过现行校验的权重可直接复用。

手动安装仍然可用：

1. 从 GitHub 或 CNB 的 Release 下载 `MaaRacingMaster-infinite_drive-models-<版本>.zip`。
2. 打开 zip，把里面的 `perception/`、`depth/` 与 `lane/` 文件夹复制到用户数据目录的
   `data/infinite_drive/models/`（默认 `%APPDATA%\MaaRacingMaster\data\infinite_drive\models\`）。
   Windows 的「全部解压」会多套一层以 zip 命名的目录——直接从 zip 里拖文件夹过去更稳。
3. 重启 MaaRM 后生效。

校验值（代码 `model_assets.MODEL_MANIFEST` 同源）：

- `perception/model.onnx`：10,665,412 字节，SHA256
  `0ffc20ae13b334bfeccb74e0eeef00c3e6ba98378ab482170c17f33a13ef06af`
- `depth/relative_mnv4m_slim_spf_448_default.onnx`：24,155,097 字节，SHA256
  `825114de5341612386de409f82f5dfb0496bb9afa559565ac633466bf172fb71`
- `lane/plus_small_384x640.onnx`：543,156 字节，SHA256
  `bd35423f6a8a0071f908b74c1a7592210479e24c3714c7091a708eef95cb4bcc`
- `lane/plus_small_fp16_384x640.onnx`：314,482 字节，SHA256
  `abdf3d46706c0dc259d178df8a27ae23b9c3ce03f658e75aadc391b035bd9e26`
  （由上一份权重 `keep_io_types` 转 FP16，仅 DML 使用；缺失或会话失败时回退 FP32）

车道线权重缺失或校验失败时，车道线驾驶无法启动。街车权重缺失时可保持车道。

## perception/model.onnx —— 物品感知（AGPL-3.0）

- **来源**：由 Ultralytics 官方预训练权重 `yolo11n.pt` + Ultralytics 训练代码
  在自有标注数据上微调导出（见 `maaracing_master/plugins/infinite_drive/tools/training/train.py`）。
- **许可**：按 Ultralytics 的立场，本微调模型视为 AGPL-3.0 衍生作品，随发布包
  再分发需遵循 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)。
- **运行时不含 Ultralytics 代码**：YOLO 推理仅通过 ONNX Runtime 加载本 ONNX 图，
  **未再分发任何 Ultralytics / PyTorch / torchvision 代码**。
- **商业 / 闭源使用**：如需在不开放源码或商业场合使用本模型，须另行取得
  [Ultralytics Enterprise License](https://www.ultralytics.com/license)。
- 上游: <https://github.com/ultralytics/ultralytics>

## depth/relative_mnv4m_slim_spf_448_default.onnx —— 深度几何（Apache-2.0）

- **来源**：DepthART（ACM MM 2026）MobileNetV4-M-slim-SPF 相对深度权重，
  448×448 固定输入；量化与 ONNX 导出产物经离线同卷验证与合成点图回归后入库。
- **许可**：上游 Hugging Face 模型卡标注 Apache-2.0，可随发布包自由再分发。
- **运行时不依赖上游仓库代码**：推理仅通过 ONNX Runtime 加载本 ONNX 图；
  米制换算（车道线灭点行 + 地面模型 → 合成点图）为本仓库实现（`depth_geo.py`）。
- 上游: <https://github.com/xuefeng-cvr/DepthART> 、<https://huggingface.co/Fengxue93/DepthART>

## lane/plus_small_384x640.onnx —— 线观测（BDD100K 非商业/科研）

- **来源**：TwinLiteNet+ Small 车道分割模型，训练自 BDD100K；在此基础上用**自采
  游戏帧**做定向微调（只解冻车道头三层，74 张逐张目检样本，Focal+Tversky 区域损失），
  目的是补上原模型对「贴脸正中粗白虚线」的盲区。微调数据为本项目自采游戏画面，
  不含第三方标注，许可随上游不变。离线核验记录见仓库提交历史（微调与逐张目检）。
- **许可**：上游 TwinLiteNet+ 代码为 MIT；BDD100K 数据/标签适用 UC Regents 定制许可——
  教育、科研与非营利目的可复制、修改、分发（须随附版权与许可声明），商业用途仅限
  BDD/BAIR 成员。许可正文未单列“训练所得权重”，本项目按衍生作品处理：随可选模型包
  分发时附完整许可 `LICENSE-BDD100K.txt`；**商业用途须先取得授权**。
- **运行时不依赖上游仓库代码**：推理仅通过 ONNX Runtime 加载本 ONNX 图。
