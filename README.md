# 仿真平台

版本: 2.0.1 | 平台: Windows 10/11 x64 | PowerShell 5.1+

## 下载指南
```powershell
git lfs install    # 不要直接点击下载，由于文件较大，需使用LFS
git clone https://www.osredm.com/hf2026/hf2026-sim.git   # 根据实际网址填写，克隆仓库
# 如果克隆时LFS没有自动拉取，手动补一次
git lfs pull
```


## 快速开始

在包根目录打开 PowerShell：

```powershell
.\setup.ps1    # 检测并安装依赖（VC++运行库/python/pip/redis/pyyaml）
.\start.ps1    # 启动 Redis + bridge + 前端（UE 版包会同时后台启动 UE 渲染器）
# 浏览器打开 http://localhost:3000（start.ps1 会自动尝试打开）
#   → 选赛题 → (可选)「算法」框填 module:Class → 点「开始仿真」
```

> 若提示"禁止运行脚本"，先执行一次：
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```
> 或改用：`powershell -ExecutionPolicy Bypass -File .\setup.ps1`

## 系统要求

- Windows 10/11 x64，PowerShell 5.1+（系统自带）
- Microsoft Visual C++ 2015-2022 Redistributable (x64)（setup.ps1 会用包内 vc_redist.x64.exe 自动安装，可能需要管理员权限）
- GPU（可选，仅 UE 渲染用；无 GPU 自动降级 Three.js 自渲染）

## 可视化渲染配置（可选项， 下载增强版才可支持）

默认使用 Three.js 自渲染。若要用 UE 真实渲染：

1. 打开`config/renderers/ue_testwl.json`
2. 把 `workdir` 改成你的 UE 打包产物路径（目前为文件夹：ue-renderer\Windows，当前已改好）
3. 确保机器有 GPU（≥8GB VRAM）
4. 如果想保存飞机云台拍摄的照片，需更改`ue-renderer\Windows\testwl\Content\Config\capture_config.json`的`saveimage`配置内容，将`enabled`字段改为True，并在`output_dir`配置输出照片的文件夹路径

> UE 渲染器由 start.ps1 自动后台启动（无窗口），首次加载地图需数分钟，
> 加载完点「开始仿真」即有相机画面。查看帧率/调试：`Get-Content run\logs\ue.log -Wait`

## 端口配置

所有端口可通过环境变量覆盖（PowerShell 语法）：

```powershell
$env:OPENSIM_REDIS_PORT=6380; $env:OPENSIM_WEB_PORT=3001; .\start.ps1
```

> 端口被占用时 start.ps1 会自动顺延到空闲端口，一般无需手动指定。

| 变量 | 默认 | 用途 |
|---|---|---|
| OPENSIM_REDIS_PORT | 6379 | Redis |
| OPENSIM_WS_PORT | 8080 | bridge WebSocket |
| OPENSIM_CAM_PORT | 8081 | bridge HTTP（相机帧 + sim 控制） |
| OPENSIM_CAM_WS_PORT | 8082 | bridge 相机 WebSocket |
| OPENSIM_WEB_PORT | 3000 | 前端静态服务 |

## 手动停止

```powershell
.\stop.ps1      # 停止所有进程（含UE清理）
```

## 故障排查

### 一键诊断（推荐给远程支持场景）

遇到问题且无法自行定位时，在发布包根目录运行诊断脚本，它会自动收集
系统信息、包完整性、依赖状态、端口/进程和全部日志，生成一个压缩包：

```powershell
.\diagnose.ps1     # Windows —— 生成 simulation-diagnostics-<时间戳>.zip
```

把生成的压缩包发回给运维/开发即可，无需手动翻日志。脚本只读不写，可随时重复执行。



### 手动排查

```powershell
.\verify.ps1                            # 定位哪个组件有问题
.\preflight-check.ps1                   # 环境预检（开端口前的完整检查清单）
Get-Content run\logs\redis.log -Wait    # Redis 日志
Get-Content run\logs\bridge.log -Wait   # bridge 日志
Get-Content run\logs\frontend.log -Wait # 前端服务日志
Get-ChildItem competition\scenarios\*\output\   # 引擎输出（点赛题后才有）
```

## 目录结构

| 路径 | 说明 |
|---|---|
| opensim-sim.exe | C++ 仿真引擎（hiredis/redis++/cesium 已静态链接） |
| opensim-render-ctl.exe | 渲染编排 CLI（UE spawn plan） |
| bin/node.exe | 内置 Node.js v22 |
| bin/redis-server.exe, redis-cli.exe | 内置 Redis（Windows 版） |
| python/ | 内置 Python 3.12（不依赖目标机系统 Python） |
| vc_redist.x64.exe | VC++ 2015-2022 运行库安装包（setup.ps1 自动调用） |
| visualization/dist-bridge/ | bridge 编译产物（Redis↔WebSocket 转发） |
| frontend/ | 前端静态文件（webpack 构建） |
| competition/ | 比赛 SDK + baseline 算法（Python） |
| examples/ | 示例YOLO算法 |
| config/ | 仿真配置 + 地形数据（CSV）+ UE 配置 |
| run/ | 运行时产物（日志/PID/输出） |
