# -*- coding: utf-8 -*-
"""无限驾驶插件清单：供 core/registry 自动扫描发现与注册。

契约：ID 唯一标识；MODULE_CLASS 定位模块类（plugins/<id>/<path>.py 的 <attr> 类）。
NAME / STAGE_ORDER / REQUIRES / REQUIRES_GAMEPAD_EXCLUSIVE 从模块类读取（单一来源）。

未声明 VALID_FROM / VALID_UNTIL：v2 活动的开放窗口尚未取得权威口径，缺省 = 永久有效。
"""

ID = "infinite_drive"
MODULE_CLASS = "module.InfiniteDriveModule"
# 插件自有的资产安装器：core 只按本声明加载，不硬编码模型实现
ASSET_INSTALLER = "model_installer.ModelInstaller"
# 安装入口的显示名词与缺失提示：宿主只渲染声明，不硬编码模块语义
ASSET_INSTALLER_LABEL = "模型"
ASSET_INSTALLER_MISSING_HINT = "车道线模型未安装（发布包不含权重）"
