"""在镜像构建阶段安装 Camoufox 浏览器内核。

`python -m camoufox fetch` 在同步失败、未找到版本、或预发布需要确认时
仍会以退出码 0 结束，导致 Docker 构建“成功”但运行时报
`official/stable is not installed`。这里改走编程接口，并在可执行文件
不存在时让构建失败。
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    from camoufox.addons import DefaultAddons, maybe_download_addons
    from camoufox.geolocation import ALLOW_GEOIP, download_mmdb
    from camoufox.pkgman import (
        CamoufoxFetcher,
        INSTALL_DIR,
        installed_verstr,
        launch_path,
        list_available_versions,
    )

    versions = list_available_versions(include_prerelease=False)
    channel = "official/stable"
    if not versions:
        versions = list_available_versions(include_prerelease=True)
        channel = "official/prerelease"
    if not versions:
        print(
            "未找到可用的 Camoufox 浏览器 Release。请确认构建环境能访问 GitHub API。",
            file=sys.stderr,
        )
        return 1

    selected = versions[0]
    print(
        f"正在安装 {channel}: {selected.display}\n  {selected.url}",
        flush=True,
    )
    CamoufoxFetcher(selected_version=selected).install()

    if ALLOW_GEOIP:
        download_mmdb()
    maybe_download_addons(list(DefaultAddons))

    version = installed_verstr()
    executable = launch_path()
    if not os.path.isfile(executable):
        print(f"Camoufox 可执行文件不存在: {executable}", file=sys.stderr)
        return 1

    print(f"Camoufox 已安装: {version}", flush=True)
    print(f"INSTALL_DIR={INSTALL_DIR}", flush=True)
    print(f"executable={executable}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
