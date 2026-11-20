# -*- coding: utf-8 -*-
"""无限驾驶插件包（极速狂飙 v2 后继）。

插件自包含契约：代码、几何标定、HUD 区域、模型权重（resources/models/，含各权重
许可声明 README；发布包默认不带权重，见 model_assets）全部位于本目录内，
registry 按 manifest.py 自动发现；整个目录拷走即卸载、放入即安装。

本插件承接 speedrush 归档时「游戏事实未变」的感知/几何/观测/控制基建（搬迁判据
与清单见 _archive/speedrush/RETIRED.md）；v2 的操作流程与策略层尚未接线。

**真源分立**：**游戏事实** = RULES.md（外部事实，人工复核制）；**几何标定** =
calibration/gate0.json（唯一入口 ``world_model.load_calib``）。行为参数真源
（policy/decision.json）随 v1 策略层留在归档，v2 重建时再立。
``resources/policy/hud_regions.json`` 是驾驶页区域集（v1 标定，v2 HUD 改版后需重验）。
"""

from pathlib import Path

# 插件根目录（plugins/infinite_drive/）
PLUGIN_DIR = Path(__file__).resolve().parent
# 资源根与分类目录（policy 区域集、几何标定 calibration/、模型权重 models/）
RES_DIR = PLUGIN_DIR / "resources"
POLICY_DIR = RES_DIR / "policy"
CALIB_DIR = RES_DIR / "calibration"
MODELS_DIR = RES_DIR / "models"
# 操作流程导航真源（状态机节点）与模板图目录
PIPELINE_DIR = RES_DIR / "pipeline"
IMAGE_DIR = RES_DIR / "image"
# 驾驶页 HUD 区域真源（归一化、x2/y2 排他）
HUD_REGIONS_FILE = POLICY_DIR / "hud_regions.json"
# 几何标定真源（Gate-0 收杆产物；world_model.load_calib 唯一读法）
GATE0_FILE = CALIB_DIR / "gate0.json"
# 模型权重（发布包默认不带，由可选模型包提供；仓库内保留真源与校验值。
# perception/ AGPL-3.0 衍生、depth/ Apache-2.0、lane/ BDD100K 非商业——来源与许可声明见
# models/README.md 与 THIRD_PARTY_LICENSES「模型权重」节。运行时由 model_assets
# 解析：用户数据目录优先、安装包兜底，带尺寸 + SHA256 校验）
PERCEPTION_MODEL_REL = "resources/models/perception/model.onnx"
PERCEPTION_MODEL_FILE = MODELS_DIR / "perception" / "model.onnx"
DEPTH_MODEL_REL = "resources/models/depth/relative_mnv4m_slim_spf_448_default.onnx"
DEPTH_MODEL_FILE = MODELS_DIR / "depth" / "relative_mnv4m_slim_spf_448_default.onnx"
LANE_MODEL_REL = "resources/models/lane/plus_small_384x640.onnx"
LANE_MODEL_FILE = MODELS_DIR / "lane" / "plus_small_384x640.onnx"
# DML 专用半精度版本（同一权重转 FP16，keep_io_types）：仅 DML 使用，CPU 回退用 fp32
LANE_FP16_MODEL_REL = "resources/models/lane/plus_small_fp16_384x640.onnx"
LANE_FP16_MODEL_FILE = MODELS_DIR / "lane" / "plus_small_fp16_384x640.onnx"