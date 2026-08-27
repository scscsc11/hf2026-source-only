#!/bin/bash
# setup.sh — Simulation release 包环境检测与依赖自动安装
# 检测 python3/pip/redis(python)/pyyaml 等依赖，缺失则自动 apt/pip 安装。
# 幂等：已装的跳过，重复运行安全。

set -eu

NEED_APT=()
HAS_APT=0
command -v apt-get >/dev/null 2>&1 && HAS_APT=1

# 1. 系统命令（Ubuntu 一般都有，缺了装对应的包）
command -v lsof   >/dev/null 2>&1 || NEED_APT+=(lsof)
command -v curl   >/dev/null 2>&1 || NEED_APT+=(curl)
command -v pgrep  >/dev/null 2>&1 || NEED_APT+=(procps)

# 2. Python 解释器 —— V2.1.0 起捆绑 python-build-standalone 3.12，不再依赖系统 python。
#    redis-py/pyyaml 已随捆绑 python 进包，无需 apt 装 python3-pip 或 pip 装包。

# 3. glibc 版本检测（引擎要求 >= 2.35，即 Ubuntu 22.04+；不满足警告但不阻断）
GLIBC_VER=$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}')
warn_glibc() {
    if [ -n "${GLIBC_VER:-}" ]; then
        major=$(echo "$GLIBC_VER" | cut -d. -f1)
        minor=$(echo "$GLIBC_VER" | cut -d. -f2)
        if [ "${major:-0}" -lt 2 ] 2>/dev/null || { [ "${major:-0}" -eq 2 ] 2>/dev/null && [ "${minor:-0}" -lt 35 ] 2>/dev/null; }; then
            echo "⚠️  glibc $GLIBC_VER < 2.35，引擎二进制可能无法运行（需 Ubuntu 22.04+）"
        fi
    fi
}

# 6. 汇总与安装
if [ ${#NEED_APT[@]} -gt 0 ]; then
    echo "缺少系统包: ${NEED_APT[*]}"
    if [ "$HAS_APT" -eq 1 ]; then
        echo "使用 apt-get 安装（需要 sudo 权限）..."
        SUDO=""
        [ "$(id -u)" -ne 0 ] && SUDO="sudo"
        $SUDO apt-get update -qq && $SUDO apt-get install -y "${NEED_APT[@]}"
    else
        echo "✗ 非 Debian/Ubuntu 系统（无 apt-get），请手动安装上述包后重试。"
        echo "  Ubuntu/Debian: sudo apt-get install ${NEED_APT[*]}"
        exit 1
    fi
fi

# V2.1.0: 不再需要 pip 安装 —— Python + redis-py + pyyaml 均随捆绑 python 进包。
# 验证捆绑 python 可用（自检，缺失则提示发行包损坏）。
PACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$PACK_ROOT/python/bin/python3.12" ]; then
    "$PACK_ROOT/python/bin/python3.12" -c "import redis, yaml" 2>/dev/null || \
        echo "⚠️  捆绑 python 的 redis/pyyaml 不可用，发行包可能损坏"
else
    echo "✗ 捆绑 python 缺失（python/bin/python3.12），发行包不完整"
    exit 1
fi

warn_glibc
echo "✓ 环境就绪，可运行 ./start.sh"
