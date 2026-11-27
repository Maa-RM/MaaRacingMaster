# -*- coding: utf-8 -*-
"""无限驾驶模型包的一键安装器（插件自有语义，宿主只提供生命周期接缝）。

真源归属：清单/尺寸/SHA256 在 model_assets；tag、资产名、ZIP 哈希与下载地址在
构建生成的 download.json。本模块只搬运字节、校验、暂存与提交，不猜文件名，
也不接受前端传来的 URL 或目标路径。

安装落点：data/infinite_drive/models/<manifest-digest>/；旧手动解压目录与包内
权重仍是只读兜底候选，不被覆盖。临时下载/解压放 data/infinite_drive/downloads/。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

from maaracing_master.core.download import download_file, is_allowed_url
from maaracing_master.core.paths import module_data_dir
from maaracing_master.core.update_prefs import preferred_source
from maaracing_master.plugins.infinite_drive import model_assets

DESCRIPTOR_PATH = model_assets.MODELS_DIR / "download.json"
DOWNLOAD_TIMEOUT_S = 20
DOWNLOAD_DEADLINE_S = 600
# 两个发行货架的 Release 下载地址不是字节的终点：github.com 302 到
# release-assets.githubusercontent.com（带签名查询串），cnb.cool 302 到
# asset.cnb.cool/assets/t/<一次性令牌>。资产一跳是货架自己交给自己家的 CDN，
# 必须与货架同列入白名单；内容真伪仍由描述里的 SHA256 与 zip 成员白名单把关，
# 主机白名单只负责不让跳转跨到无名义的第三方。
ALLOWED_HOSTS = frozenset({
    "github.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com", "codeload.github.com",
    "cnb.cool", "asset.cnb.cool",
})
_PHASES = frozenset({
    "idle", "downloading", "verifying", "extracting", "committing",
    "done", "failed", "cancelled",
})


@dataclass(frozen=True)
class InstallStatus:
    task_id: str
    phase: str
    revision: int
    downloaded: int = 0
    total: int = 0
    source: str = ""
    error: str | None = None
    manifest_digest: str | None = None


def _url_allowed(url: object) -> bool:
    """下载源必须落在本插件声明的官方主机内（传输层只搬字节，策略留在这里）。"""
    return is_allowed_url(url, ALLOWED_HOSTS)


def _ordered_sources(sources: dict[str, str]) -> list[tuple[str, str]]:
    """按宿主更新偏好排首选源；描述里的顺序只决定「其余源」的先后。

    首选源这个参数只有一份：宿主写 config/update-prefs.json 的 source（默认 cnb），
    这里读同一个文件。源键名与宿主同名（cnb/github），不另建映射表；插件自己
    再写一套「先 cnb」的顺序，就是给同一偏好建第二份实现。
    """
    preferred = preferred_source()
    return sorted(sources.items(), key=lambda item: item[0] != preferred)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_descriptor(path: Path | None = None) -> dict:
    """读取构建生成的下载描述；缺失或字段不全时抛 ValueError（调用方回退手动安装）。"""
    path = path or DESCRIPTOR_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"没有可用的模型下载描述（{path}）：{exc}") from exc
    required = ("tag", "asset", "sha256", "size", "unpacked_size", "manifest_digest",
                "sources")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"模型下载描述缺字段: {', '.join(missing)}")
    if data["manifest_digest"] != model_assets.manifest_digest():
        raise ValueError("模型下载描述与当前模型清单不匹配，请使用手动安装")
    if not isinstance(data["sources"], dict) or not data["sources"]:
        raise ValueError("模型下载描述没有可用下载源")
    for name, url in data["sources"].items():
        if not _url_allowed(url):
            raise ValueError(f"下载源不合法（{name}）: {url}")
    return data


class ModelInstaller:
    """单飞安装任务；状态快照带单调 revision，供前端丢弃过期响应。"""

    def __init__(self, descriptor_path: Path | None = None) -> None:
        self._descriptor_path = descriptor_path
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = InstallStatus(
            task_id="", phase="idle", revision=0, manifest_digest=model_assets.manifest_digest())

    def status(self) -> InstallStatus:
        with self._lock:
            return self._status

    def needs_install(self) -> bool:
        """轻量可用性检查：缺必需模型（按尺寸）即需要补装。"""
        return bool(model_assets.missing())

    def can_remove(self) -> bool:
        """是否存在可删除的一键安装目录（包内/手动解压的权重不归本安装器清理）。"""
        return any(path.is_dir() for path in (
            model_assets.managed_pack_dir(), model_assets.previous_pack_dir()))

    def remove(self) -> tuple[bool, InstallStatus | str]:
        """删除一键安装的受管模型目录；在途安装时拒绝。

        只清安装器自己写过的目录（当前清单摘要与兼容旧摘要），不碰用户手动
        解压到 models/ 根目录的权重。删除后由 needs_install 重新判断可用性。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "安装进行中，无法删除"
            for target in (model_assets.managed_pack_dir(), model_assets.previous_pack_dir()):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
            remaining = [path for path in (
                model_assets.managed_pack_dir(), model_assets.previous_pack_dir()) if path.is_dir()]
            if remaining:
                return False, "删除失败：模型文件可能正被占用"
            model_assets.clear_cache()
            self._status = InstallStatus(
                task_id="", phase="idle", revision=self._status.revision + 1,
                manifest_digest=model_assets.manifest_digest())
            return True, self._status

    def start(self) -> tuple[bool, InstallStatus | str]:
        """启动或复用在途任务。返回 (accepted, status_or_error)。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True, self._status
            task_id = uuid.uuid4().hex
            self._cancel = threading.Event()
            self._status = InstallStatus(
                task_id=task_id, phase="downloading", revision=self._status.revision + 1,
                manifest_digest=model_assets.manifest_digest())
            self._thread = threading.Thread(
                target=self._run, args=(task_id,), name="model-install", daemon=True)
            self._thread.start()
            return True, self._status

    def cancel(self) -> InstallStatus:
        with self._lock:
            self._cancel.set()
            if self._status.phase not in _PHASES - {"done", "failed", "cancelled"}:
                return self._status
            self._status = replace(self._status, revision=self._status.revision + 1)
            return self._status

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _set(self, task_id: str, **changes) -> None:
        with self._lock:
            if self._status.task_id != task_id:
                return
            self._status = replace(
                self._status, revision=self._status.revision + 1, **changes)

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    def _run(self, task_id: str) -> None:
        staging_root = module_data_dir(model_assets.MODULE_ID, "downloads")
        extracted: Path | None = None
        archive: Path | None = None
        try:
            if all(
                model_assets.verify(model_assets.installed_path(rel), rel) is None
                for rel in model_assets.MODEL_MANIFEST
            ):
                self._set(task_id, phase="done", total=1, downloaded=1)
                return

            descriptor = load_descriptor(self._descriptor_path)
            staging_root.mkdir(parents=True, exist_ok=True)
            archive = staging_root / f"{task_id}-{descriptor['asset']}"
            errors: list[str] = []
            for name, url in _ordered_sources(descriptor["sources"]):
                if self._cancelled():
                    self._set(task_id, phase="cancelled", error="用户取消")
                    return
                try:
                    self._download(task_id, str(url), archive, int(descriptor["size"]), name)
                    if _sha256_file(archive) != descriptor["sha256"]:
                        raise ValueError("下载文件 SHA256 与下载描述不符")
                    break
                except (OSError, ValueError) as exc:
                    if self._cancelled():
                        self._set(task_id, phase="cancelled", error="用户取消")
                        return
                    # 每个源都留原因：只报最后一个源会掩盖前面的真实失败（如某一源
                    # 被白名单拒跳、另一源网络不可达），让排查从错误文本就断了线。
                    errors.append(f"{name}: {exc}")
                    archive.unlink(missing_ok=True)
            else:
                self._set(task_id, phase="failed",
                          error=f"所有下载源均失败（{'；'.join(errors)}）")
                return

            if self._cancelled():
                self._set(task_id, phase="cancelled", error="用户取消")
                return

            self._set(task_id, phase="verifying")
            extracted = staging_root / f"{task_id}-extracted"
            self._extract(archive, extracted, descriptor)
            for rel in model_assets.MODEL_MANIFEST:
                err = model_assets.verify(extracted / model_assets.packed_rel(rel), rel)
                if err:
                    raise ValueError(f"解压后校验失败: {err}")
            if self._cancelled():
                self._set(task_id, phase="cancelled", error="用户取消")
                return

            self._set(task_id, phase="committing")
            self._commit(task_id, extracted)
            model_assets.clear_cache()
            err = model_assets.check_pack()
            if err:
                raise ValueError(f"安装后复验失败: {err}")
            self._set(task_id, phase="done", downloaded=int(descriptor["size"]))
        except ValueError as exc:
            self._set(task_id, phase="failed", error=str(exc))
        except Exception as exc:  # 网络/磁盘异常不能冒泡打断宿主 worker
            self._set(task_id, phase="failed", error=f"安装失败: {exc}")
        finally:
            if self._cancelled() and self._status.phase not in {"done", "failed"}:
                self._set(task_id, phase="cancelled", error="用户取消")
            for path in (archive, extracted):
                if path is not None:
                    shutil.rmtree(path, ignore_errors=True) if path.is_dir() else \
                        path.unlink(missing_ok=True)

    def _download(self, task_id: str, url: str, dest: Path, size: int, source: str) -> None:
        """策略与落点在本插件，传输（白名单/跳转/取消/进度）交给 core.download。"""
        self._set(task_id, source=source, total=size, downloaded=0, phase="downloading")
        download_file(
            url,
            dest,
            allowed_hosts=ALLOWED_HOSTS,
            expected_size=size,
            on_progress=lambda done, total: self._set(
                task_id, downloaded=done, total=total),
            cancelled=self._cancelled,
            timeout_s=DOWNLOAD_TIMEOUT_S,
            deadline_s=DOWNLOAD_DEADLINE_S,
            user_agent="MaaRacingMaster/model-install",
        )

    @staticmethod
    def _extract(archive: Path, target: Path, descriptor: dict) -> None:
        allowed = {model_assets.packed_rel(rel) for rel in model_assets.MODEL_MANIFEST}
        allowed |= {"README.md", "LICENSE-BDD100K.txt", "SHA256SUMS"}
        target.mkdir(parents=True, exist_ok=True)
        unpacked = 0
        with zipfile.ZipFile(archive) as zf:
            seen: set[str] = set()
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                parts = name.split("/")
                if (name.startswith("/") or any(p == ".." for p in parts)
                        or ":" in parts[0] or name in seen):
                    raise ValueError(f"模型包含非法或重复成员: {name}")
                seen.add(name)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f"模型包含符号链接: {name}")
                if info.is_dir():
                    continue
                if name not in allowed:
                    raise ValueError(f"模型包含未声明成员: {name}")
                unpacked += info.file_size
                if unpacked > int(descriptor["unpacked_size"]):
                    raise ValueError("模型包展开尺寸超过下载描述声明")
                out = target / name
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

    @staticmethod
    def _commit(task_id: str, extracted: Path) -> None:
        managed = model_assets.managed_pack_dir()
        managed.mkdir(parents=True, exist_ok=True)
        moved: list[tuple[Path, Path | None]] = []
        try:
            for rel in model_assets.MODEL_MANIFEST:
                relative = model_assets.packed_rel(rel)
                src, dst = extracted / relative, managed / relative
                dst.parent.mkdir(parents=True, exist_ok=True)
                if model_assets.verify(dst, rel) is None:
                    continue
                backup = None
                if dst.exists():
                    backup = dst.with_name(f"{dst.name}.bak.{task_id}")
                    os.replace(dst, backup)
                os.replace(src, dst)
                model_assets.clear_cache()
                err = model_assets.verify(dst, rel)
                if err:
                    raise ValueError(f"提交后校验失败: {err}")
                moved.append((dst, backup))
        except Exception:
            for dst, backup in reversed(moved):
                dst.unlink(missing_ok=True)
                if backup is not None:
                    os.replace(backup, dst)
            raise
        finally:
            for _dst, backup in moved:
                if backup is not None:
                    backup.unlink(missing_ok=True)
