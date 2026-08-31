#!/bin/bash
# start.sh — Simulation release 包一键启动（Redis + bridge + 本地 UE + 前端静态服务）
# 复刻 start_3dweb.sh 的启动模型：UE 在新终端窗口前台跑 run.sh，bridge 通过
# Redis 发现(renderer_online)。引擎(opensim-sim)由 competition 在前端点赛题时 spawn。

set -eu

# 推导包根（脚本所在目录）
PACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PACK_ROOT"

# Python 解释器：V2.1.0 起用包内捆绑的 python-build-standalone 3.12（与打包时编译 .pyc
# 的版本一致，根治 magic number 跨版本不兼容）。优先用包内，env 覆盖留给调试。
BUNDLED_PY="$PACK_ROOT/python/bin/python3.12"
if [ -x "$BUNDLED_PY" ]; then
    export PYTHON_BIN="${PYTHON_BIN:-$BUNDLED_PY}"
else
    # 兜底：发行包损坏缺捆绑 python 时，回退系统 python（可能 magic 不匹配，但至少能启动 start.sh 自身的内联脚本）。
    export PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || echo python3)}"
    echo "⚠️  捆绑 python 缺失，回退系统 python（SDK 可能因 magic number 不匹配无法运行）"
fi

# 端口可配（默认 + env 覆盖）
OPENSIM_REDIS_PORT="${OPENSIM_REDIS_PORT:-6379}"
OPENSIM_WS_PORT="${OPENSIM_WS_PORT:-8080}"
OPENSIM_CAM_PORT="${OPENSIM_CAM_PORT:-8081}"
OPENSIM_CAM_WS_PORT="${OPENSIM_CAM_WS_PORT:-8082}"
OPENSIM_WEB_PORT="${OPENSIM_WEB_PORT:-3000}"

RUN_DIR="$PACK_ROOT/run"
LOG_DIR="$RUN_DIR/logs"
PID_DIR="$RUN_DIR/pids"

mkdir -p "$LOG_DIR" "$PID_DIR" "$RUN_DIR/redis"

# ── 0. 关闭本包的现存进程（上次异常退出留下的孤儿），复刻 start_3dweb.sh step 1 ──
echo ""
echo "[0/5] 清理现存进程..."
# pidfile 记录的进程
for pf in "$PID_DIR"/*.pid; do
    [ -f "$pf" ] || continue
    pid=$(cat "$pf" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "  停止残留 $(basename "$pf" .pid) (PID $pid)"
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pf"
done
# 外部残留进程（bridge / opensim-sim / 本包 static-server）
for pat in "dist-bridge/bridge/index.js" "opensim-sim$" "static-server.js .*frontend"; do
    pids=$(pgrep -f "$pat" 2>/dev/null || true)
    [ -n "$pids" ] && { echo "  清理残留 $pat (PID: $pids)"; kill $pids 2>/dev/null || true; sleep 1; kill -9 $pids 2>/dev/null || true; }
done
# UE 孤儿进程（按 config/renderers/*.json 的 workdir 清理，跳过 template 与占位符）
if [ -d "$PACK_ROOT/config/renderers" ]; then
    for cfg in "$PACK_ROOT/config/renderers"/*.json; do
        [ -f "$cfg" ] || continue
        case "$(basename "$cfg")" in *.template.json) continue;; esac
        workdir=$("$PYTHON_BIN" -c "import json; d=json.load(open('$cfg')); print(d.get('executable',{}).get('workdir',''))" 2>/dev/null || true)
        [ -z "$workdir" ] && continue
        case "$workdir" in \<*\>) continue;; esac
        for cwd_link in /proc/[0-9]*/cwd; do
            [ -e "$cwd_link" ] || continue
            pid=${cwd_link#/proc/}; pid=${pid%/cwd}
            [ "$pid" != "$$" ] || continue
            cwd=$(readlink "$cwd_link" 2>/dev/null || true)
            case "$cwd" in "$workdir"|"$workdir"/*)
                echo "  清理 UE 孤儿 (PID $pid, cwd $workdir)"
                kill "$pid" 2>/dev/null || true
                ;;
            esac
        done
    done
fi
sleep 1

# 环境检测：捆绑 python 缺失或 redis/pyyaml 不可用时提示运行 setup.sh
if ! "$PYTHON_BIN" -c "import redis,yaml" 2>/dev/null; then
    echo "✗ Python 依赖不可用（捆绑 python 缺失或损坏）。请先运行: ./setup.sh"
    exit 1
fi

# 端口冲突处理：被占则自动顺延到下一个空闲端口（最多试 100 个）
# 提示走 stderr，端口号走 stdout（供 $(...) 捕获），失败 exit 1
# 检测优先 ss（最可靠），回退 lsof，再回退 bash /dev/tcp 探测
port_in_use() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -tlnH 2>/dev/null | grep -qE "[:.]$p\b"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -tiTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
    else
        # bash 内建 /dev/tcp 探测（能连上即被占）
        (echo >/dev/tcp/127.0.0.1/"$p") >/dev/null 2>&1
    fi
}
# ASSIGNED_PORTS 累积本脚本已分配出去的端口。pick_free_port 只检测系统 listen 端口,
# 看不到"本脚本即将启动但还没 listen"的端口;若不传递已分配集合,5 个端口会被分到
# 同一空闲端口(如 8080/8081 被占用时 WS 与 CAM_WS 都会落在 8082,bridge 启动后
# CameraWs listen 报 EADDRINUSE,相机画面起不来)。
ASSIGNED_PORTS=()
port_taken() {
    # $1 = 端口。已被系统 listen 或已被本脚本分配则返回 0(真)。
    local p="$1" taken
    for taken in "${ASSIGNED_PORTS[@]}"; do [ "$taken" = "$p" ] && return 0; done
    port_in_use "$p"
}
pick_free_port() {
    local start="$1" label="$2" p
    p="$start"
    while port_taken "$p" && [ "$p" -lt "$((start + 100))" ]; do
        p=$((p + 1))
    done
    if port_taken "$p"; then
        echo "✗ $label 端口 ${start}~$((start+100)) 全被占用，请用环境变量指定" >&2
        exit 1
    fi
    [ "$p" != "$start" ] && echo "  $label 端口 $start 被占用，改用 $p" >&2
    echo "$p"
}
OPENSIM_REDIS_PORT=$(pick_free_port "$OPENSIM_REDIS_PORT" "REDIS");  ASSIGNED_PORTS+=("$OPENSIM_REDIS_PORT")
OPENSIM_WS_PORT=$(pick_free_port "$OPENSIM_WS_PORT" "WS");           ASSIGNED_PORTS+=("$OPENSIM_WS_PORT")
OPENSIM_CAM_PORT=$(pick_free_port "$OPENSIM_CAM_PORT" "CAM");        ASSIGNED_PORTS+=("$OPENSIM_CAM_PORT")
OPENSIM_CAM_WS_PORT=$(pick_free_port "$OPENSIM_CAM_WS_PORT" "CAMWS"); ASSIGNED_PORTS+=("$OPENSIM_CAM_WS_PORT")
OPENSIM_WEB_PORT=$(pick_free_port "$OPENSIM_WEB_PORT" "WEB");        ASSIGNED_PORTS+=("$OPENSIM_WEB_PORT")

# 端口顺延后，必须把实际 REDIS 端口同步到所有 scenario.json。
# 引擎（opensim-sim）从 scenario.json 读 redis_port，不读环境变量；
# competition 用 --redis-port 传参（已是顺延后的值），但 competition spawn
# 的 opensim-sim 读的是 scenario.json —— 若不同步，引擎连默认 6379，
# competition 连顺延端口，两端在不同 Redis 上永远碰不到面。
echo "  同步 redis_port=$OPENSIM_REDIS_PORT 到 scenario.json..."
for sj in "$PACK_ROOT"/competition/scenarios/*/scenario.json; do
    [ -f "$sj" ] || continue
    "$PYTHON_BIN" -c "
import json
with open('$sj') as f: d = json.load(f)
d.setdefault('simulation', {})['redis_port'] = $OPENSIM_REDIS_PORT
with open('$sj', 'w') as f: json.dump(d, f, indent=2, ensure_ascii=False)
" || echo "    ⚠️  改写 $sj 失败（手动检查 redis_port）"
done

# 把实际使用的端口写入 env 文件，供 stop.sh / verify.sh 读取
# （端口可能因冲突自动顺延，stop/verify 不能假设是默认值）
cat > "$RUN_DIR/env.sh" <<EOF
OPENSIM_REDIS_PORT=$OPENSIM_REDIS_PORT
OPENSIM_WS_PORT=$OPENSIM_WS_PORT
OPENSIM_CAM_PORT=$OPENSIM_CAM_PORT
OPENSIM_CAM_WS_PORT=$OPENSIM_CAM_WS_PORT
OPENSIM_WEB_PORT=$OPENSIM_WEB_PORT
EOF

# 进程存活检测（幂等：已起则跳过）
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

echo ""
echo "=== Simulation 启动中 ==="

# 1. Redis（纯内存模式）
if ! alive "$PID_DIR/redis.pid"; then
    echo "[1/5] 启动 Redis (port $OPENSIM_REDIS_PORT, 纯内存)..."
    # --bind 0.0.0.0 --protected-mode no: 多机部署时远程 UE 需跨机连 Redis。
    # 仅绑回环的默认配置会让远程 UE 连不上(内网部署,protected-mode 关闭可接受)。
    "$PACK_ROOT/bin/redis-server" --port "$OPENSIM_REDIS_PORT" \
        --bind 0.0.0.0 --protected-mode no \
        --daemonize yes --pidfile "$PID_DIR/redis.pid" \
        --logfile "$LOG_DIR/redis.log" --dir "$RUN_DIR/redis" \
        --save "" --appendonly no
    for i in $(seq 1 20); do
        "$PACK_ROOT/bin/redis-cli" -p "$OPENSIM_REDIS_PORT" ping 2>/dev/null | grep -q PONG && break
        sleep 0.25
    done
    "$PACK_ROOT/bin/redis-cli" -p "$OPENSIM_REDIS_PORT" ping >/dev/null 2>&1 || { echo "✗ Redis 启动失败"; exit 1; }
else
    echo "[1/5] Redis 已在运行，跳过"
fi

# 2. bridge（用包内 node + 编译产物）
if ! alive "$PID_DIR/bridge.pid"; then
    echo "[2/5] 启动 bridge (WS :$OPENSIM_WS_PORT, CAM :$OPENSIM_CAM_PORT, CAMWS :$OPENSIM_CAM_WS_PORT)..."
    export NODE_PATH="$PACK_ROOT/lib/node_modules"
    export OPENSIM_RENDER_CTL_BIN="$PACK_ROOT/opensim-render-ctl"
    export OPENSIM_RENDERERS_DIR="$PACK_ROOT/config/renderers"
    export OPENSIM_SCENARIOS_DIR="$PACK_ROOT/competition/scenarios"
    export OPENSIM_SIM_BIN="$PACK_ROOT/opensim-sim"
    # PYTHON_BIN 已在脚本开头定义（指向捆绑 python），此处无需重复 export。
    export WS_PORT="$OPENSIM_WS_PORT" CAM_HTTP_PORT="$OPENSIM_CAM_PORT" CAM_WS_PORT="$OPENSIM_CAM_WS_PORT"
    export REDIS_HOST="127.0.0.1" REDIS_PORT="$OPENSIM_REDIS_PORT"
    nohup "$PACK_ROOT/bin/node" "$PACK_ROOT/visualization/dist-bridge/bridge/index.js" \
        < /dev/null > "$LOG_DIR/bridge.log" 2>&1 &
    echo $! > "$PID_DIR/bridge.pid"
    # 等待 bridge HTTP 端口起来（用 /api/sim/status 探测，bridge 无专门 /health）
    for i in $(seq 1 40); do
        curl -s "http://127.0.0.1:$OPENSIM_CAM_PORT/api/sim/status" >/dev/null 2>&1 && break
        sleep 0.25
    done
else
    echo "[2/5] bridge 已在运行，跳过"
fi

# 3. 本地 UE 渲染器（前台终端窗口跑 run.sh）
# 复刻 start_3dweb.sh step 3.5：扫 config/renderers/*.json（template/占位符不算）拿 workdir，
# 开新终端窗口前台跑 run.sh。bridge 不 spawn UE，只通过 Redis 发现(renderer_online)，
# 所以 UE 必须由本脚本拉起，否则点击无人机无视频流。
# 标准包(无 ue_testwl.json，只有 template)会因下面 for 循环找不到非 template 的 .json 而整段跳过。
echo ""
UE_STARTED=0
if [ -d "$PACK_ROOT/config/renderers" ]; then
    for cfg in "$PACK_ROOT/config/renderers"/*.json; do
        [ -f "$cfg" ] || continue
        case "$(basename "$cfg")" in *.template.json) continue;; esac
        UE_WORKDIR=$("$PYTHON_BIN" -c "import json; d=json.load(open('$cfg')); print(d.get('executable',{}).get('workdir',''))" 2>/dev/null || true)
        [ -z "$UE_WORKDIR" ] && continue
        case "$UE_WORKDIR" in \<*\>) continue;; esac
        # workdir 在打包版 ue_testwl.json 里是相对包根的路径(如 20260721-1622_Shipping/x86/Linux),
        # gnome-terminal/xterm 新窗口的工作目录不是包根,相对 cd 会失败 —— 转成绝对路径。
        case "$UE_WORKDIR" in
            /*) ;;
            *)  UE_WORKDIR="$PACK_ROOT/$UE_WORKDIR" ;;
        esac
        if [ -d "$UE_WORKDIR" ] && [ -f "$UE_WORKDIR/run.sh" ]; then
            # 同步实际 redis 端口到 UE 自己的配置。
            # UE 读 redis 端口有两处(按构建版本不同,宁可信其有,两处都同步):
            #   1. testwl/Content/Config/scenario*.json 的 simulation.redis_port(旧路径)
            #   2. testwl/Content/Config/capture_config.json 的 redis.port
            #      (新路径:Shipping 包命令行不可用,UE 从此文件读运行时配置)
            # 本包 redis 可能因端口冲突顺延(如 6382)—— 不同步则 UE 连到错误 redis,
            # bridge 永远收不到 renderer_online,无人机无视频流。
            UE_CFG_DIR="$UE_WORKDIR/testwl/Content/Config"
            if [ -d "$UE_CFG_DIR" ]; then
                for ue_sj in "$UE_CFG_DIR"/scenario*.json; do
                    [ -f "$ue_sj" ] || continue
                    "$PYTHON_BIN" -c "
import json
with open('$ue_sj') as f: d=json.load(f)
d.setdefault('simulation',{})['redis_port']=$OPENSIM_REDIS_PORT
d['simulation']['redis_host']='127.0.0.1'
with open('$ue_sj','w') as f: json.dump(d,f,indent=2,ensure_ascii=False)
" 2>/dev/null || echo "    ⚠️  改写 UE $(basename "$ue_sj") 失败(UE 可能连错 redis)"
                done
                UE_CC="$UE_CFG_DIR/capture_config.json"
                if [ -f "$UE_CC" ]; then
                    "$PYTHON_BIN" -c "
import json
with open('$UE_CC') as f: d=json.load(f)
d.setdefault('redis',{})['host']='127.0.0.1'
d['redis']['port']=$OPENSIM_REDIS_PORT
with open('$UE_CC','w') as f: json.dump(d,f,indent=2,ensure_ascii=False)
" 2>/dev/null || echo "    ⚠️  改写 UE capture_config.json 失败(UE 可能连错 redis)"
                fi
                echo "  [3/5] 已同步 redis_port=$OPENSIM_REDIS_PORT 到 UE scenario*.json + capture_config.json"
            fi
            if command -v gnome-terminal >/dev/null 2>&1; then
                gnome-terminal --title="UE Renderer (service)" -- bash -c "cd '$UE_WORKDIR' && ./run.sh 2>&1 | tee '$LOG_DIR/ue.log'; echo '[UE 已退出,按回车关闭窗口]'; read" &
                echo "  [3/5] 本地 UE 已在新终端窗口启动(workdir: $UE_WORKDIR)"
                echo "        Ctrl+C 或关窗口退出 UE;关 bridge 时 UE 自动 shutdown"
                echo "        UE 日志: tail -f $LOG_DIR/ue.log"
            elif command -v xterm >/dev/null 2>&1; then
                xterm -title "UE Renderer (service)" -e bash -c "cd '$UE_WORKDIR' && ./run.sh 2>&1 | tee '$LOG_DIR/ue.log'" &
                echo "  [3/5] 本地 UE 已在 xterm 窗口启动(workdir: $UE_WORKDIR)"
                echo "        UE 日志: tail -f $LOG_DIR/ue.log"
            else
                # 无终端模拟器(如无头服务器): nohup 后台跑,输出落 ue.log
                nohup bash -c "cd '$UE_WORKDIR' && ./run.sh" > "$LOG_DIR/ue.log" 2>&1 &
                echo "  [3/5] ⚠ 无 gnome-terminal/xterm,UE 已后台启动(PID $!)"
                echo "        UE 日志: tail -f $LOG_DIR/ue.log"
            fi
            UE_STARTED=1
            break  # 一个终端窗口跑一个 UE service 实例即可(内部按 capacity 承载多机)
        else
            echo "  [3/5] ⚠ $(basename "$cfg") 的 workdir 无效或无 run.sh,本地 UE 需手动启动"
            echo "        workdir=$UE_WORKDIR"
        fi
    done
fi
[ "$UE_STARTED" = "0" ] && echo "  [3/5] 无可用 UE 渲染器配置(标准包),跳过本地 UE 启动 —— 渲染走 Three.js 自渲染"

# 4. 前端静态服务
if ! alive "$PID_DIR/frontend.pid"; then
    echo "[4/5] 启动前端静态服务 (:$OPENSIM_WEB_PORT)..."
    nohup "$PACK_ROOT/bin/node" "$PACK_ROOT/static-server.js" "$PACK_ROOT/frontend" "$OPENSIM_WEB_PORT" \
        < /dev/null > "$LOG_DIR/frontend.log" 2>&1 &
    echo $! > "$PID_DIR/frontend.pid"
    for i in $(seq 1 20); do
        curl -s "http://127.0.0.1:$OPENSIM_WEB_PORT/" >/dev/null 2>&1 && break
        sleep 0.25
    done
else
    echo "[4/5] 前端已在运行，跳过"
fi

echo ""
echo "=========================================="
echo "  Simulation 已启动"
echo "=========================================="
echo "  浏览器访问: http://localhost:$OPENSIM_WEB_PORT"
echo "  选赛题 → 「算法」框可填 module:Class（留空用 baseline）→ 点「开始仿真」"
echo ""

# 自动打开浏览器（设 OPENSIM_NO_OPEN_BROWSER=1 可禁用，如无头服务器环境）
if [ "${OPENSIM_NO_OPEN_BROWSER:-0}" != "1" ]; then
    URL="http://localhost:$OPENSIM_WEB_PORT"
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || true
        echo "  已尝试打开浏览器：$URL"
    else
        echo "  ⚠️  无 xdg-open，请手动打开浏览器访问 $URL"
    fi
fi
echo ""
echo "  日志: tail -f $LOG_DIR/{redis,bridge,frontend}.log"
echo "  停止: ./stop.sh"
echo "  检查: ./verify.sh"
echo "=========================================="

# 恢复终端回显（后台子进程退出后终端可能不回显，复刻 start_3dweb.sh 的修复）
stty echo 2>/dev/null || true
