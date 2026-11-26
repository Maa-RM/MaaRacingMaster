# -*- coding: utf-8 -*-
"""策略判据层：三套「能不能下」的判据，纯函数、无网络、前后端共用。

P0 = v0.32.1 出厂快照（只有发行货架主机），P1 = 当前生产白名单（货架 + 自家资产
CDN），P2 = 待验证的替代判据（货架首发 + 跳转只要 https 公网、真伪交给 SHA256）。

判据只回答「这条 URL 链允不允许走」，不回答「资产在不在」——后者由探针的
HTTP 状态单独记录，两者混在一起就分不清是策略拒绝还是发行缺件。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# v0.32.1-dev.1（9b2ba6c）出厂时的 ALLOWED_HOSTS 原样快照：历史口径，不得跟着
# 现役白名单一起改，否则这套「当时能不能下」的判定会随修复一起变绿，实验失去基线。
P0_HOSTS = frozenset({
    "github.com", "objects.githubusercontent.com", "codeload.github.com", "cnb.cool",
})
# 发行货架首发主机：P1/P2 都要求首跳落在这些主机上。
SHELF_HOSTS = P0_HOSTS

_PRIVATE_HOST_SUFFIX = (".local", ".internal", ".localhost")


def _parts(url: str):
    return urlsplit(url)


def _basic_allowed(url: str) -> bool:
    """https（或回环 http，供本地演练）+ 不带用户名口令；主机名单另判。"""
    parts = _parts(url)
    if parts.username or parts.password:
        return False
    if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}:
        return True
    return parts.scheme == "https"


def policy_static(url: str, hosts) -> bool:
    """P0/P1 共用的静态名单口径：每一跳的主机都必须在名单里。"""
    if not _basic_allowed(url):
        return False
    parts = _parts(url)
    if parts.scheme == "http":
        return parts.hostname in {"127.0.0.1", "localhost"}
    return parts.hostname in hosts


def policy_public_https(url: str) -> bool:
    """P2：首跳限发行货架，跳转只要求 https + 公网主机，不维护 CDN 名单。"""
    if not _basic_allowed(url):
        return False
    parts = _parts(url)
    host = (parts.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIX):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def chain_verdict(urls, policy) -> bool:
    """整条链逐跳过判据；空链（首跳就失败）时只判给定的那些 URL。"""
    return bool(urls) and all(policy(url) for url in urls)
