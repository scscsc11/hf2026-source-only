#!/bin/bash
# diagnose.sh — Simulation 一键诊断打包（Linux 版）
#
# 用途：在出问题的用户机器上运行，自动收集系统信息、包完整性、运行依赖、
#       端口/进程状态、全部日志与崩溃痕迹，打包成一个 tar.gz。
# 用法：在发布包根目录执行：
#       ./diagnose.sh          （或 bash diagnose.sh）
# 产出：包根目录下 simulation-diagnostics-<时间戳>.tar.gz —— 请把该文件发回运维人员。
#
# 本脚本只读不写（除诊断目录外），不会影响正在运行的服务，可随时重复执行。

set -u

PACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"

# ---- 诊断输出目录（优先包内 run/logs/diagnostics，不可写则退回 /tmp） ----
DIAG_PARENT="$PACK_ROOT/run/logs/diagnostics"
if ! mkdir -p "$DIAG_PARENT" 2>/dev/null || ! touch "$DIAG_PARENT/.probe-$$" 2>/dev/null; then
    DIAG_PARENT="/tmp/simulation-diagnostics"
    mkdir -p "$DIAG_PARENT"
else
    rm -f "$DIAG_PARENT/.probe-$$"
fi
DIR="$DIAG_PARENT/simulation-diagnostics-$TS"
mkdir -p "$DIR/logs" "$DIR/sim-output"

RESULTS="$DIR/.results.tmp"
: > "$RESULTS"

add_check() {  # add_check STATUS ITEM [HINT]
    echo "$1|$2|${3:-}" >> "$RESULTS"
    case "$1" in
        PASS) echo "  [PASS] $2" ;;
        WARN) echo "  [WARN] $2"; [ -n "${3:-}" ] && echo "         -> $3" ;;
        FAIL) echo "  [FAIL] $2"; [ -n "${3:-}" ] && echo "         -> $3" ;;
    esac
}

# 采集一段命令输出到文件，单步失败不中断
cap() {  # cap FILE TITLE CMD...
    local file="$1" title="$2"; shift 2
    echo "=== $title ===" >> "$file"
    if ! "$@" >> "$file" 2>&1; then
        echo "(采集失败或命令不存在: $*)" >> "$file"
    fi
    echo "" >> "$file"
}

# 拷贝日志，超过 2MB 只保留最后 2000 行
copy_log() {  # copy_log SRC DESTDIR [PREFIX]
    local src="$1" destdir="$2" prefix="${3:-}"
    [ -f "$src" ] || return 1
    local base size dest
    base="$(basename "$src")"
    dest="$destdir/$prefix$base"
    size=$(stat -c %s "$src" 2>/dev/null || echo 0)
    if [ "$size" -gt 2097152 ]; then
        echo "[截断] 原始大小 $((size / 1048576)) MB，仅保留最后 2000 行" > "$dest"
        tail -n 2000 "$src" >> "$dest"
    else
        cp "$src" "$dest"
    fi
    return 0
}

# 在若干文件中搜索正则，命中返回 0
search_log() {  # search_log PATTERN FILE...
    local pat="$1"; shift
    local f
    for f in "$@"; do
        [ -f "$f" ] && grep -qE "$pat" "$f" 2>/dev/null && return 0
    done
    return 1
}

echo ""
echo "=== Simulation 一键诊断 ==="
echo "包根目录: $PACK_ROOT"
echo "诊断目录: $DIR"
echo ""

# ======================================================================
# 01 系统信息
# ======================================================================
echo "[1/8] 收集系统信息..."
F="$DIR/01-system.txt"
{
    echo "采集时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "包根目录: $PACK_ROOT"
    echo "路径长度: ${#PACK_ROOT} 字符"
} > "$F"
[ "${#PACK_ROOT}" -gt 120 ] && add_check WARN "包路径较长（${#PACK_ROOT} 字符）" "路径过长可能触发兼容问题，建议把包挪到更浅目录（如 ~/simulation）"
cap "$F" "操作系统" sh -c 'cat /etc/os-release 2>/dev/null | head -5; echo "内核: $(uname -srvmo)"; echo "主机名: $(hostname)"; echo "运行时长: $(uptime 2>/dev/null)"'
cap "$F" "glibc 版本" sh -c 'ldd --version 2>/dev/null | head -1'
GLIBC_VER="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0)"
# sort -VC 检查 "2.39\n$GLIBC_VER" 是否已按版本升序 —— 不是则说明 glibc < 2.39
if [ "$GLIBC_VER" != "0" ] && ! printf '%s\n%s\n' "2.39" "$GLIBC_VER" | sort -VC 2>/dev/null; then
    add_check WARN "glibc $GLIBC_VER 低于 2.39" "引擎要求 glibc >= 2.39（Ubuntu 24.04+），低版本会直接无法启动"
fi
cap "$F" "硬件资源" sh -c 'echo "CPU 逻辑核数: $(nproc 2>/dev/null)"; free -h 2>/dev/null; echo ""; df -h "'"$PACK_ROOT"'" 2>/dev/null'
cap "$F" "当前用户" sh -c 'id; echo "sudo 可用: $(command -v sudo >/dev/null && echo yes || echo no)"'
cap "$F" "VERSION 文件" cat "$PACK_ROOT/VERSION"

# ======================================================================
# 02 环境变量
# ======================================================================
echo "[2/8] 收集环境变量..."
F="$DIR/02-environment.txt"
cap "$F" "Simulation 相关环境变量" sh -c 'env | grep -E "^(OPENSIM_|REDIS_|WS_PORT|CAM_|PYTHON_BIN|NODE_PATH|UE_|MINGW_)" | sort'
cap "$F" "PATH" sh -c 'echo "$PATH" | tr ":" "\n"'
cap "$F" "LD_LIBRARY_PATH" sh -c 'echo "${LD_LIBRARY_PATH:-(未设置)}"'

# ======================================================================
# 03 包完整性检查
# ======================================================================
echo "[3/8] 检查包完整性..."
F="$DIR/03-package-integrity.txt"
echo "文件清单（缺失项同时会写入 00-summary.txt）：" > "$F"
check_file() {  # check_file PATH DESC CRITICAL(1/0)
    local p="$PACK_ROOT/$1" desc="$2" critical="$3"
    if [ -f "$p" ]; then
        printf "  [存在] %-45s %12s bytes\n" "$1" "$(stat -c %s "$p" 2>/dev/null)" >> "$F"
    else
        echo "  [缺失] $1   <-- $desc" >> "$F"
        if [ "$critical" = "1" ]; then
            add_check FAIL "缺少文件: $1（$desc）" "发布包不完整（解压中断/文件损坏），请重新解压完整发布包"
        else
            add_check WARN "缺少文件: $1（$desc）" "非致命，但可能影响启动速度或前端显示"
        fi
    fi
}
check_file "opensim-sim"                     "仿真引擎"          1
check_file "opensim-render-ctl"              "渲染编排 CLI"      0
check_file "bin/node"                        "Node.js 运行时"    1
check_file "bin/redis-server"                "Redis 服务"        1
check_file "bin/redis-cli"                   "Redis 客户端"      1
check_file "visualization/dist-bridge/bridge/index.js" "bridge 服务" 1
check_file "frontend/index.html"             "前端页面"          1
check_file "frontend/bundle.js"              "前端 bundle"       1
check_file "frontend/heightmap.json"         "前端地形数据"      0
check_file "config/HeightSample.csv"         "地形高程数据"      1
check_file "config/GridDataAll_18.csv"       "地形网格数据"      1
check_file "config/terrain_bbox.json"        "地形包围盒缓存"    0
check_file "config/points.json"              "航点数据"          1
check_file "config/random_routes_20.json"    "随机路线池"        1
check_file "config/defaults.json"            "默认配置"          1
check_file "competition/__main__.py"         "competition 入口"  1
check_file "competition/sdk/core/runner.py"  "competition runner" 1
[ -x "$PACK_ROOT/opensim-sim" ] || add_check FAIL "opensim-sim 没有可执行权限" "执行 chmod +x opensim-sim bin/* 后重试"
for d in config/models competition/scenarios competition/sdk lib/node_modules; do
    n=$(find "$PACK_ROOT/$d" -type f 2>/dev/null | wc -l)
    echo "  [目录] $d  ($n 个文件)" >> "$F"
    [ "$n" -eq 0 ] && add_check FAIL "目录为空或缺失: $d" "发布包不完整，请重新解压"
done
add_check PASS "包完整性检查完成（详见 03-package-integrity.txt）"

# ======================================================================
# 04 运行依赖检查
# ======================================================================
echo "[4/8] 检查运行依赖..."
F="$DIR/04-runtime-deps.txt"

# 4.1 引擎动态库依赖
if [ -f "$PACK_ROOT/opensim-sim" ]; then
    cap "$F" "ldd opensim-sim（缺失的库会标 not found）" ldd "$PACK_ROOT/opensim-sim"
    if ldd "$PACK_ROOT/opensim-sim" 2>/dev/null | grep -q "not found"; then
        add_check FAIL "opensim-sim 有未满足的动态库依赖" "见 04-runtime-deps.txt 中 ldd 输出的 not found 行；通常是 glibc/libstdc++ 版本过低"
    else
        add_check PASS "opensim-sim 动态库依赖完整"
    fi
else
    echo "=== ldd opensim-sim ===" >> "$F"
    echo "(opensim-sim 缺失，跳过 ldd)" >> "$F"
fi

# 4.2 Python（Linux 发布包用系统 python3）
F_PY=""
if command -v python3 >/dev/null 2>&1; then F_PY="python3";
elif [ -x "$PACK_ROOT/python/bin/python3" ]; then F_PY="$PACK_ROOT/python/bin/python3"; fi
{
    echo "=== Python 检测 ==="
    echo "使用: ${F_PY:-未找到}"
} >> "$F"
if [ -n "$F_PY" ]; then
    cap "$F" "python --version" "$F_PY" --version
    cap "$F" "import redis / yaml" "$F_PY" -c 'import redis, yaml; print("redis", redis.__version__); print("yaml OK")'
    if "$F_PY" -c 'import redis, yaml' 2>/dev/null; then
        add_check PASS "Python 依赖 redis/pyyaml 就绪（$F_PY）"
    else
        add_check FAIL "Python 缺少 redis/pyyaml（$F_PY）" "请运行 ./setup.sh（需 sudo）；或手动 pip3 install redis pyyaml"
    fi
else
    add_check FAIL "未找到 python3" "请运行 ./setup.sh（会用 apt 安装 python3/pip）"
fi

# 4.3 Redis 冒烟
[ -x "$PACK_ROOT/bin/redis-server" ] && cap "$F" "redis-server --version" "$PACK_ROOT/bin/redis-server" --version

# 4.4 scenario.json BOM/解析 + 4.5 .pyc magic（有 python 时）
if [ -n "$F_PY" ]; then
    cap "$F" "scenario.json BOM / JSON 解析检查" "$F_PY" - "$PACK_ROOT" <<'PYEOF'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for p in sorted(root.glob('competition/scenarios/*/scenario.json')):
    raw = p.read_bytes()
    bom = raw[:3] == b'\xef\xbb\xbf'
    try:
        json.loads(raw.decode('utf-8-sig'))
        st = 'OK'
    except Exception as e:
        st = 'PARSE_FAIL: %s' % e
    print('%s: bom=%s %s' % (p.parent.name, bom, st))
PYEOF
    cap "$F" ".pyc magic 版本一致性" "$F_PY" - "$PACK_ROOT" <<'PYEOF'
import importlib.util, pathlib, sys
magic = importlib.util.MAGIC_NUMBER
bad = []
root = pathlib.Path(sys.argv[1])
for p in root.glob('competition/**/*.pyc'):
    with open(p, 'rb') as fh:
        if fh.read(4) != magic:
            bad.append(str(p))
print('当前解释器 magic:', magic.hex())
print('不匹配的 .pyc 数量:', len(bad))
for b in bad[:20]:
    print(' ', b)
PYEOF
else
    cap "$F" "scenario.json BOM 检查（无 Python，仅查 BOM）" sh -c '
        for s in "'"$PACK_ROOT"'"/competition/scenarios/*/scenario.json; do
            [ -f "$s" ] || continue
            if [ "$(head -c3 "$s" | od -An -tx1 | tr -d " ")" = "efbbbf" ]; then bom=True; else bom=False; fi
            echo "$(basename "$(dirname "$s")"): bom=$bom"
        done'
fi

# ======================================================================
# 05 端口与进程
# ======================================================================
echo "[5/8] 检查端口与进程..."
F="$DIR/05-ports-processes.txt"
if [ -f "$PACK_ROOT/run/env.sh" ]; then
    {
        echo "=== run/env.sh（上次启动实际使用的端口）==="
        cat "$PACK_ROOT/run/env.sh"
    } > "$F"
    # shellcheck disable=SC1091
    . "$PACK_ROOT/run/env.sh"
else
    echo "(run/env.sh 不存在 —— start.sh 可能从未成功执行到写端口那一步)" > "$F"
fi
OPENSIM_REDIS_PORT="${OPENSIM_REDIS_PORT:-6379}"
OPENSIM_WS_PORT="${OPENSIM_WS_PORT:-8080}"
OPENSIM_CAM_PORT="${OPENSIM_CAM_PORT:-8081}"
OPENSIM_WEB_PORT="${OPENSIM_WEB_PORT:-3000}"

{
    echo ""
    echo "=== 端口监听状态 ==="
} >> "$F"
port_line() {  # port_line NAME PORT
    local name="$1" port="$2" info
    if command -v ss >/dev/null 2>&1; then
        info=$(ss -ltnp 2>/dev/null | grep -E "[:.]$port " | head -1)
    elif command -v netstat >/dev/null 2>&1; then
        info=$(netstat -ltnp 2>/dev/null | grep -E "[:.]$port " | head -1)
    else
        info=""
    fi
    if [ -n "$info" ]; then
        printf "  %-12s 端口 %-6s [监听中] %s\n" "$name" "$port" "$info" >> "$F"
    else
        printf "  %-12s 端口 %-6s [未监听]\n" "$name" "$port" >> "$F"
    fi
}
port_line "Redis"      "$OPENSIM_REDIS_PORT"
port_line "bridge-WS"  "$OPENSIM_WS_PORT"
port_line "bridge-HTTP" "$OPENSIM_CAM_PORT"
port_line "前端 Web"   "$OPENSIM_WEB_PORT"

{
    echo ""
    echo "=== Simulation 相关进程（含可疑孤儿进程）==="
} >> "$F"
PSOUT=$(ps -eo pid,ppid,etime,args 2>/dev/null | grep -E 'opensim-sim|opensim-render|redis-server|node|competition' | grep -v grep || true)
if [ -n "$PSOUT" ]; then echo "$PSOUT" >> "$F"; else echo "  (无 Simulation 相关进程在运行)" >> "$F"; fi
SIM_COUNT=$(echo "$PSOUT" | grep -c 'opensim-sim' || true)
if [ "$SIM_COUNT" -gt 1 ]; then
    add_check WARN "检测到 $SIM_COUNT 个 opensim-sim 进程并存" "上次异常退出残留的孤儿进程会导致仿真瞬移/卡死，请运行 ./stop.sh 清理后再启动"
fi

# ======================================================================
# 06 崩溃痕迹
# ======================================================================
echo "[6/8] 收集崩溃痕迹..."
F="$DIR/06-crash-evidence.txt"
cap "$F" "内核日志中的段错误（可能需权限）" sh -c 'dmesg 2>/dev/null | grep -iE "segfault|opensim|redis|node" | tail -20'
cap "$F" "core dump 记录" sh -c '(command -v coredumpctl >/dev/null && coredumpctl list --no-pager 2>/dev/null | grep -iE "opensim|python|redis|node" | tail -20) || ls -la "$PWD"/core* 2>/dev/null'

{
    echo "=== 引擎 stderr 日志分析（competition/scenarios/*/output/sim.stderr.log）==="
} >> "$F"
NEWEST_SIM=""
NEWEST_MTIME=0
for sl in "$PACK_ROOT"/competition/scenarios/*/output/sim.stderr.log; do
    [ -f "$sl" ] || continue
    scenario=$(basename "$(dirname "$(dirname "$sl")")")
    size=$(stat -c %s "$sl" 2>/dev/null || echo 0)
    mtime=$(stat -c %Y "$sl" 2>/dev/null || echo 0)
    printf "  %-20s %12s bytes  最后修改 %s\n" "$scenario" "$size" "$(date -d "@$mtime" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)" >> "$F"
    if [ "$mtime" -gt "$NEWEST_MTIME" ]; then NEWEST_MTIME=$mtime; NEWEST_SIM=$sl; fi
done
if [ -z "$NEWEST_SIM" ]; then
    echo "  (未找到任何 sim.stderr.log —— 用户可能从未成功启动过仿真)" >> "$F"
elif [ ! -s "$NEWEST_SIM" ]; then
    add_check FAIL "最新 sim.stderr.log 为 0 字节" "引擎在 main() 之前就崩溃：多为动态库缺失/glibc 过低/权限问题。见 04-runtime-deps.txt 的 ldd 输出与 06 中 dmesg"
fi

# ======================================================================
# 07 收集日志文件
# ======================================================================
echo "[7/8] 打包日志..."
COPIED=0
for lf in "$PACK_ROOT"/run/logs/*; do
    [ -f "$lf" ] || continue
    copy_log "$lf" "$DIR/logs" && COPIED=$((COPIED+1))
done
for of in "$PACK_ROOT"/run/sim-output/*; do
    [ -f "$of" ] || continue
    copy_log "$of" "$DIR/sim-output" && COPIED=$((COPIED+1))
done
for scenarioDir in "$PACK_ROOT"/competition/scenarios/*/; do
    [ -d "$scenarioDir" ] || continue
    sname=$(basename "$scenarioDir")
    # scenario.json 本体（供运维核对 redis_port 是否被改写）——无论 output 目录是否存在都收
    copy_log "$scenarioDir/scenario.json" "$DIR/sim-output" "${sname}_" && COPIED=$((COPIED+1))
    outDir="$scenarioDir/output"
    [ -d "$outDir" ] || continue
    for name in sim.stderr.log controller.stderr.log controller.stdout.log profile.log; do
        copy_log "$outDir/$name" "$DIR/sim-output" "${sname}_" && COPIED=$((COPIED+1))
    done
    for pf in "$outDir"/scenario_*_prepared.json; do
        [ -f "$pf" ] || continue
        copy_log "$pf" "$DIR/sim-output" "${sname}_" && COPIED=$((COPIED+1))
    done
    # 最近 3 个评分结果
    ls -t "$outDir"/*.evaluation.json 2>/dev/null | head -3 | while read -r ef; do
        copy_log "$ef" "$DIR/sim-output" "${sname}_"
    done
done
echo "共收集 $COPIED 个日志文件。超过 2MB 的文件只保留最后 2000 行。" > "$DIR/logs/_README.txt"
add_check PASS "已收集 $COPIED 个日志/输出文件"

# ======================================================================
# 08 自动分析（症状 -> 可能原因）
# ======================================================================
echo "[8/8] 自动分析日志..."
BRIDGE_ERR="$PACK_ROOT/run/logs/bridge.err"
BRIDGE_LOG="$PACK_ROOT/run/logs/bridge.log"
FRONTEND_ERR="$PACK_ROOT/run/logs/frontend.err"
CTL_ERRS=$(ls "$PACK_ROOT"/competition/scenarios/*/output/controller.stderr.log 2>/dev/null || true)
SIM_ERRS=$(ls "$PACK_ROOT"/competition/scenarios/*/output/sim.stderr.log 2>/dev/null || true)
# shellcheck disable=SC2086
{
    search_log 'EADDRINUSE' "$BRIDGE_ERR" "$BRIDGE_LOG" "$FRONTEND_ERR" && \
        add_check WARN "自动分析: 日志含 EADDRINUSE" "端口被占用导致服务启动失败。查看 05-ports-processes.txt 确认占用进程，或用 OPENSIM_*_PORT 环境变量换端口后重启 start.sh"
    search_log 'competition_crashed' "$BRIDGE_LOG" && \
        add_check WARN "自动分析: bridge 报告 competition_crashed" "competition 进程崩溃。重点查看 sim-output/*_controller.stderr.log"
    [ -n "$CTL_ERRS" ] && search_log 'JSONDecodeError' $CTL_ERRS && \
        add_check WARN "自动分析: controller 日志含 JSONDecodeError" "scenario.json 可能带 BOM 或已损坏。见 04-runtime-deps.txt 的 BOM 检查"
    [ -n "$CTL_ERRS" ] && search_log 'opensim-sim not found' $CTL_ERRS && \
        add_check WARN "自动分析: controller 日志含 opensim-sim not found" "引擎二进制缺失。重新解压发布包"
    search_log 'ready_timeout|ready timeout' "$BRIDGE_LOG" && \
        add_check WARN "自动分析: bridge 报告 ready_timeout" "controller 5 分钟内未就绪。查看 sim-output/*_sim.stderr.log 是否引擎启动卡住（常见于地形数据缺失导致全量扫描）"
    [ -n "$CTL_ERRS" ] && search_log 'ModuleNotFoundError' $CTL_ERRS && \
        add_check WARN "自动分析: controller 日志含 ModuleNotFoundError" "Python 依赖缺失或用户算法模块路径不对。见 04-runtime-deps.txt"
    { [ -n "$CTL_ERRS" ] && search_log 'Connection refused' $CTL_ERRS; } || { [ -n "$SIM_ERRS" ] && search_log 'Connection refused' $SIM_ERRS; } && \
        add_check WARN "自动分析: 日志含连接拒绝" "Redis 未就绪或端口不对。查看 logs/redis.log，并核对 scenario.json 的 redis_port 与 05 中实际端口"
    [ -n "$SIM_ERRS" ] && search_log 'route pool|路线池' $SIM_ERRS && \
        add_check WARN "自动分析: 引擎日志含路线池为空" "config/points.json / random_routes_20.json 缺失（见 03-package-integrity.txt），目标不导航会导致仿真秒结束"
    true
}

# ======================================================================
# 汇总
# ======================================================================
SUMMARY="$DIR/00-summary.txt"
PASS_N=$(grep -c '^PASS|' "$RESULTS" || true)
WARN_N=$(grep -c '^WARN|' "$RESULTS" || true)
FAIL_N=$(grep -c '^FAIL|' "$RESULTS" || true)
{
    echo "============================================================"
    echo " Simulation 诊断报告"
    echo " 生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo " 包根目录: $PACK_ROOT"
    echo "============================================================"
    echo ""
    echo "检查汇总: $PASS_N 通过 / $WARN_N 警告 / $FAIL_N 失败"
    echo ""
    while IFS='|' read -r st item hint; do
        printf '[%-4s] %s\n' "$st" "$item"
        [ -n "$hint" ] && echo "       建议: $hint"
    done < "$RESULTS"
    echo ""
    echo "--- 文件清单 ---"
    echo "01-system.txt            操作系统/glibc/硬件/权限"
    echo "02-environment.txt       环境变量"
    echo "03-package-integrity.txt 发布包文件完整性"
    echo "04-runtime-deps.txt      ldd/Python/Redis 依赖、scenario.json BOM、.pyc 版本"
    echo "05-ports-processes.txt   端口监听与进程状态"
    echo "06-crash-evidence.txt    dmesg 段错误/core dump + 引擎 stderr 分析"
    echo "logs/                    start.sh 各组件日志（redis/bridge/frontend 等）"
    echo "sim-output/              引擎与 competition 的输出日志、场景配置、评分结果"
} > "$SUMMARY"
rm -f "$RESULTS"

# 打包
ARCHIVE="$PACK_ROOT/simulation-diagnostics-$TS.tar.gz"
if tar -czf "$ARCHIVE" -C "$DIAG_PARENT" "simulation-diagnostics-$TS" 2>/dev/null; then
    echo ""
    echo "============================================================"
    echo " 诊断完成: $PASS_N 通过 / $WARN_N 警告 / $FAIL_N 失败"
    echo ""
    echo " 请将以下文件发回给运维人员："
    echo "   $ARCHIVE"
    echo "============================================================"
else
    echo ""
    echo "打包失败，请直接把整个目录发给运维人员: $DIR"
fi
exit 0
