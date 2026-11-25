# -*- coding: utf-8 -*-
"""关于页彩蛋的落点锁。

**为什么是静态锁**：「彩蛋挂在哪个元素上」是纯代码关系，浏览器能验但没必要；回归代价却
很高——曾经把 1% 的乱码效果挂到全局可见的标题栏版本号上，等于让每次切页都可能抖一下
版本号。这里锁的是**落点**：两种效果都只对着关于页 hero 的「开发中」标签，版本号不参与。

**空过防护**：抽取不到锚点串时报错，而不是静默通过。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "app.js"
INDEX = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "index.html"


def test_dev_tag_is_the_only_egg_entry():
    """连点入口与掉落文字都挂在关于页 hero 的「开发中」标签上；hero 不再展示版本号。"""
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="about-dev-tag"' in html, "关于页缺少彩蛋入口（开发中标签）"
    assert 'id="about-version"' not in html, "hero 又出现了版本号标签：彩蛋入口会被它抢走"

    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function initVersionEgg()")
    body = src[start:start + 900]
    assert "'about-dev-tag'" in body, "连点入口没有指向开发中标签"


def test_glitch_effect_targets_the_dev_tag():
    """1% 乱码效果跟着彩蛋一起迁到标签上：它不再碰标题栏那个全局可见的版本号。"""
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("function glitchDevTag()")
    glitch = src[start:start + 700]
    assert "$('about-dev-tag')" in glitch, "乱码效果没有落在开发中标签上"
    assert "scrambleNumber" in glitch, "乱码效果被换掉了（应与第 10 条同款手法）"
    assert "app-version" not in glitch, "乱码效果又摸到标题栏版本号了"

    # 版本号只允许被写入文本：不允许挂监听或加类
    assert "$('app-version').addEventListener" not in src, "有人在标题栏版本号上挂了监听"
    assert "$('app-version').classList" not in src, "有人在标题栏版本号上加了类（会被动效选中）"
