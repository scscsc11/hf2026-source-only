#!/bin/bash
# verify.sh — Simulation release 包健康检查
# 检查前端基础设施（start.sh 起的，不含引擎——引擎要点赛题后才起）。

set -u

PACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 读取 start.sh 写入的实际端口（端口可能因冲突自动顺延）
if [ -f "$PACK_ROOT/run/env.sh" ]; then
    # shellcheck disable=SC1091
    source "$PACK_ROOT/run/env.sh"
fi
OPENSIM_REDIS_PORT="${OPENSIM_REDIS_PORT:-6379}"
OPENSIM_WS_PORT="${OPENSIM_WS_PORT:-8080}"
OPENSIM_CAM_PORT="${OPENSIM_CAM_PORT:-8081}"
OPENSIM_WEB_PORT="${OPENSIM_WEB_PORT:-3000}"

PASS=0; FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

echo "=== Simulation 健康检查 ==="

# 1. Redis
if "$PACK_ROOT/bin/redis-cli" -p "$OPENSIM_REDIS_PORT" ping 2>/dev/null | grep -q PONG; then
    ok "Redis (port $OPENSIM_REDIS_PORT) PONG"
else
    fail "Redis (port $OPENSIM_REDIS_PORT) 无响应"
fi

# 2. bridge HTTP（用 /api/sim/status 探测，bridge 无专门 /health 端点）
if curl -sf "http://127.0.0.1:$OPENSIM_CAM_PORT/api/sim/status" >/dev/null 2>&1; then
    ok "bridge HTTP (port $OPENSIM_CAM_PORT) /api/sim/status 200"
else
    fail "bridge HTTP (port $OPENSIM_CAM_PORT) 无响应 — 见 run/logs/bridge.log"
fi

# 3. 前端静态服务
if curl -sf "http://127.0.0.1:$OPENSIM_WEB_PORT/" 2>/dev/null | grep -q "<html"; then
    ok "前端静态服务 (port $OPENSIM_WEB_PORT) 返回 HTML"
else
    fail "前端静态服务 (port $OPENSIM_WEB_PORT) 无响应 — 见 run/logs/frontend.log"
fi

# 4. Python 依赖
if command -v python3 >/dev/null 2>&1; then
    ok "python3 可用"
    if python3 -c "import redis, yaml" 2>/dev/null; then
        ok "Python 包 redis + pyyaml 就绪"
    else
        fail "Python 包 redis/pyyaml 缺失 — 运行 ./setup.sh 或 pip install redis pyyaml"
    fi
else
    fail "python3 不可用 — 运行 ./setup.sh"
fi

# 5. 引擎二进制存在性
if [ -x "$PACK_ROOT/opensim-sim" ]; then
    ok "opensim-sim 二进制就位"
else
    fail "opensim-sim 二进制缺失或不可执行"
fi

echo ""
echo "结果: $PASS 通过, $FAIL 失败"
[ "$FAIL" -eq 0 ] && echo "✓ 全部健康" || echo "⚠️  有失败项，详见上方"
exit $FAIL
