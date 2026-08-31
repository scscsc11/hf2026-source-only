# 仿真平台

版本: 2.0.1 | 平台: Linux x86-64（Ubuntu 24.04+） | 最低 glibc 2.39 (Ubuntu 24.04+)

## 下载指南
```bash
git lfs install    # 不要直接点击下载，由于文件较大，需使用LFS
git clone https://www.osredm.com/hf2026/hf2026-sim.git   # 根据实际网址填写，克隆仓库
# 如果克隆时LFS没有自动拉取，手动补一次
git lfs pull
```


## 快速开始

```bash
./setup.sh    # 检测并安装依赖（python3/pip/redis/pyyaml，需 sudo）
./start.sh    # 启动 Redis + bridge + 前端
# 浏览器打开 http://localhost:3000
#   → 选赛题 → (可选)「算法」框填 module:Class → 点「开始仿真」
```

## 系统要求

- Linux x86-64，glibc ≥ 2.39（Ubuntu 24.04+）
- apt 包管理器（setup.sh 用其装 python3）
- GPU（可选，仅 UE 渲染用；无 GPU 自动降级 Three.js 自渲染）

## 可视化渲染配置（可选项， 下载增强版才可支持）

默认使用 Three.js 自渲染。若要用 UE 真实渲染：

1. 打开`config/renderers/ue_testwl.json`
2. 把 `workdir` 改成你的 UE 打包产物路径（目前为文件夹：20260721-1622_Shipping，当前已改好）
3. 确保机器有 GPU（≥8GB VRAM，支持 Vulkan）
4. 如果想保存飞机云台拍摄的照片，需更改`20260721-1622_Shipping/x86/Linux/testwl/Content/Config/capture_config.json`的`saveimage`配置内容，将`enabled`字段改为True，并在`output_dir`配置输出照片的文件夹路径


## 端口配置

所有端口可通过环境变量覆盖：

```bash
OPENSIM_REDIS_PORT=6380 OPENSIM_WEB_PORT=3001 ./start.sh
```

| 变量 | 默认 | 用途 |
|---|---|---|
| OPENSIM_REDIS_PORT | 6379 | Redis |
| OPENSIM_WS_PORT | 8080 | bridge WebSocket |
| OPENSIM_CAM_PORT | 8081 | bridge HTTP（相机帧 + sim 控制） |
| OPENSIM_WEB_PORT | 3000 | 前端静态服务 |

## 手动停止

```bash
./stop.sh      # 停止所有进程（含UE清理）
```

## 故障排查

### 一键诊断（推荐给远程支持场景）

遇到问题且无法自行定位时，在发布包根目录运行诊断脚本，它会自动收集
系统信息、包完整性、依赖状态、端口/进程和全部日志，生成一个压缩包：

```bash
./diagnose.sh        # Linux —— 生成 simulation-diagnostics-<时间戳>.tar.gz
```

把生成的压缩包发回给运维/开发即可，无需手动翻日志。脚本只读不写，可随时重复执行。



### 手动排查

```bash
./verify.sh                          # 定位哪个组件有问题
tail -f run/logs/redis.log           # Redis 日志
tail -f run/logs/bridge.log          # bridge 日志
tail -f run/logs/frontend.log        # 前端服务日志
ls run/sim-output/                   # 引擎输出（点赛题后才有）
```

## 目录结构

| 路径 | 说明 |
|---|---|
| opensim-sim | C++ 仿真引擎（hiredis/redis++/cesium 已静态链接） |
| opensim-render-ctl | 渲染编排 CLI（UE spawn plan） |
| bin/node | 内置 Node.js v22 |
| bin/redis-server, redis-cli | 内置 Redis |
| visualization/dist-bridge/ | bridge 编译产物（Redis↔WebSocket 转发） |
| frontend/ | 前端静态文件（webpack 构建） |
| competition/ | 比赛 SDK + baseline 算法（Python） |
| examples/ | 示例YOLO算法 |
| config/ | 仿真配置 + 地形数据（CSV）+ UE 配置 |
| run/ | 运行时产物（日志/PID/输出） |
