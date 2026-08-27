#!/bin/bash
# stop.sh — 停止 Simulation release 包所有进程（前端→bridge→competition/sim→UE→redis）
# 含 UE 孤儿进程兜底清理（复刻 start_3dweb.sh 的 kill_renderer_workdir_procs）。

set -u

PACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$PACK_ROOT/run/pids"

# 读取 start.sh 写入的实际端口（端口可能因冲突自动顺延，不能假设默认值）
# 环境变量优先；否则读 run/env.sh；最后回退默认值。
if [ -f "$PACK_ROOT/run/env.sh" ]; then
    # shellcheck disable=SC1091
    source "$PACK_ROOT/run/env.sh"
fi
OPENSIM_REDIS_PORT="${OPENSIM_REDIS_PORT:-6379}"

# 两段式 kill：SIGTERM → 等 3 秒 → SIGKILL
kill_pidfile() {
    local name="$1" pidfile="$2"
    if [ -f "$pidfile" ]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "  ✓ 停止 $name (PID $pid)"
            for _ in 1 2 3; do sleep 1; kill -0 "$pid" 2>/dev/null || break; done
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null && echo "    (SIGKILL 兜底)"
        fi
        rm -f "$pidfile"
    fi
}

echo "=== 停止 Simulation 进程 ==="

# 1. 前端
kill_pidfile "前端静态服务" "$PID_DIR/frontend.pid"

# 2. bridge（会带走其子进程 competition → opensim-sim）
kill_pidfile "bridge" "$PID_DIR/bridge.pid"

# 3. competition / opensim-sim 残留兜底
for pat in "competition run" "opensim-sim"; do
    pids=$(pgrep -f "$pat" 2>/dev/null || true)
    [ -n "$pids" ] && { echo "  清理残留 $pat (PID: $pids)"; kill $pids 2>/dev/null; sleep 1; kill -9 $pids 2>/dev/null || true; }
done

# 4. UE 孤儿进程兜底（按 config/renderers/*.json 的 workdir 清理）
if [ -d "$PACK_ROOT/config/renderers" ]; then
    for cfg in "$PACK_ROOT/config/renderers"/*.json; do
        [ -f "$cfg" ] || continue
        # 跳过模板文件
        case "$(basename "$cfg")" in *.template.json) continue;; esac
        workdir=$(python3 -c "import json,sys; d=json.load(open('$cfg')); print(d.get('executable',{}).get('workdir',''))" 2>/dev/null || true)
        [ -z "$workdir" ] && continue
        case "$workdir" in \<*\>) continue;; esac  # 占位符跳过
        for cwd_link in /proc/[0-9]*/cwd; do
            [ -e "$cwd_link" ] || continue
            pid=${cwd_link#/proc/}; pid=${pid%/cwd}
            [ "$pid" != "$$" ] || continue
            cwd=$(readlink "$cwd_link" 2>/dev/null || true)
            case "$cwd" in
                "$workdir"|"$workdir"/*)
                    echo "  清理 UE 孤儿 (PID $pid, cwd $workdir)"
                    kill "$pid" 2>/dev/null || true
                    ;;
            esac
        done
    done
fi

# 5. Redis（最后停）—— 不依赖 pidfile，直接 redis-cli shutdown（redis 是 daemon，独立存活）
REDIS_STOPPED=0
# 优先用 redis-cli 优雅关闭（redis 的标准管理方式）
if "$PACK_ROOT/bin/redis-cli" -p "$OPENSIM_REDIS_PORT" shutdown nosave 2>/dev/null; then
    REDIS_STOPPED=1
fi
# pidfile 兜底：redis-cli 失败时按 pidfile kill
if [ "$REDIS_STOPPED" -eq 0 ] && [ -f "$PID_DIR/redis.pid" ]; then
    kill_pidfile "redis" "$PID_DIR/redis.pid" && REDIS_STOPPED=1
fi
# 最终兜底：端口仍被占用则按 ss 找 PID kill（用 ss 而非 pgrep -f，避免误匹配命令行含端口的 shell）
if [ "$REDIS_STOPPED" -eq 0 ] && command -v ss >/dev/null 2>&1; then
    rpid=$(ss -tlnpH 2>/dev/null | grep ":$OPENSIM_REDIS_PORT\b" | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
    if [ -n "$rpid" ]; then
        kill "$rpid" 2>/dev/null
        sleep 1
        kill -0 "$rpid" 2>/dev/null && kill -9 "$rpid" 2>/dev/null
        REDIS_STOPPED=1
    fi
fi
rm -f "$PID_DIR/redis.pid"
[ "$REDIS_STOPPED" -eq 1 ] && echo "  ✓ 停止 redis (port $OPENSIM_REDIS_PORT)" || echo "  redis 未在运行（无需停止）"

echo "=== 停止完成 ==="
