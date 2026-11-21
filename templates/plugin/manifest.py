# -*- coding: utf-8 -*-
"""<插件名> 插件清单：供 core/registry 自动扫描发现与注册。

契约：ID 唯一标识；MODULE_CLASS 定位模块类（plugins/<id>/<path>.py 的 <attr> 类）。
NAME / STAGE_ORDER / REQUIRES / REQUIRES_GAMEPAD_EXCLUSIVE / REQUIRED_ASSETS
从模块类读取（单一来源），不在 manifest 重复声明。
VALID_FROM / VALID_UNTIL：可选的活动有效期（两端均含，ISO 8601 带时区偏移；
缺省 = 永久有效）。
"""

ID = "sample"
MODULE_CLASS = "module.SampleModule"
# 可选：活动有效期，形如 "2026-08-06T05:00:00+08:00"；不声明即永久有效。
# VALID_FROM = "2026-08-06T05:00:00+08:00"
# VALID_UNTIL = "2026-09-23T04:59:59+08:00"

# 可选：插件自带成就目录（相对本目录的 JSON，schema 见 core/achievement.py）。
# core 另有一份自带成就（mod_id 固定为 "core"，插件声明同名 id 会被 registry 拒绝）。
# ACHIEVEMENTS = "achievements.json"

# 可选：插件自有资产安装器（发布包不含的大体积资产，如模型权重；缺省无安装入口）。
# core 只按声明加载与转发，不硬编码模块实现；后两行是安装入口的显示名词与缺失提示。
# ASSET_INSTALLER = "model_installer.ModelInstaller"
# ASSET_INSTALLER_LABEL = "模型"
# ASSET_INSTALLER_MISSING_HINT = "深度模型未安装（发布包不含权重）"
