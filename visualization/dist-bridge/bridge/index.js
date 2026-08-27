"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const fs_1 = __importDefault(require("fs"));
const path_1 = __importDefault(require("path"));
const server_1 = require("./server");
// 默认基于仓库结构推断(本文件在 visualization/src/bridge/,上溯三级
// = repo 根),使 `npm run bridge` 开箱即用 —— 无需手动设 OPENSIM_SCENARIOS_DIR。
// env 仍可覆盖。
const REPO_ROOT = path_1.default.resolve(__dirname, '..', '..', '..');
// 默认的 opensim-render-ctl(GPU 探测 + spawn 计划)二进制,同 build 目录。
// bridge 调用它拿 spawn 计划,然后由 bridge(而非 competition)spawn/监控 UE 渲染器,
// 使 competition/sim 崩溃不再孤儿化 UE。若二进制不存在则渲染器子系统休眠(降级)。
const DEFAULT_RENDER_CTL_BINARY = path_1.default.join(REPO_ROOT, 'build', process.platform === 'win32' ? 'opensim-render-ctl.exe' : 'opensim-render-ctl');
// Windows 上 `python` 常触发坏的 MS Store stub;优先用已知安装路径,回退到 PATH。
const DEFAULT_PYTHON = (() => {
    if (process.platform === 'win32') {
        for (const p of ['C:\\Python314\\python.exe', 'C:\\Python313\\python.exe', 'C:\\Python312\\python.exe']) {
            if (fs_1.default.existsSync(p))
                return p;
        }
    }
    return 'python';
})();
function loadConfig() {
    return {
        wsPort: parseInt(process.env.WS_PORT || '8080'),
        redisHost: process.env.REDIS_HOST || 'localhost',
        redisPort: parseInt(process.env.REDIS_PORT || '6379'),
        redisPassword: process.env.REDIS_PASSWORD || undefined,
        camHttpPort: parseInt(process.env.CAM_HTTP_PORT || '8081'),
        camWsPort: parseInt(process.env.CAM_WS_PORT || '8082'),
        // competition 场景目录(默认 <repo>/competition/scenarios)。
        scenariosDir: process.env.OPENSIM_SCENARIOS_DIR ||
            path_1.default.join(REPO_ROOT, 'competition', 'scenarios'),
        // 参赛者算法目录(默认 <repo>/competition/user_algorithms)。
        userAlgorithmsDir: process.env.OPENSIM_USER_ALGORITHMS_DIR ||
            path_1.default.join(REPO_ROOT, 'competition', 'user_algorithms'),
        pythonBin: process.env.PYTHON_BIN || DEFAULT_PYTHON,
        stopGrace: parseInt(process.env.SIM_STOP_GRACE || '5'),
        // UE 渲染器编排。OPENSIM_RENDER_CTL_BIN 指向 opensim-render-ctl;
        // 未设则探测 <repo>/build/opensim-render-ctl,仍不存在则 undefined(渲染休眠)。
        renderCtlBinary: process.env.OPENSIM_RENDER_CTL_BIN ||
            (fs_1.default.existsSync(DEFAULT_RENDER_CTL_BINARY) ? DEFAULT_RENDER_CTL_BINARY : undefined),
        renderersDir: process.env.OPENSIM_RENDERERS_DIR ||
            path_1.default.join(REPO_ROOT, 'config', 'renderers'),
        // service 模式想定下发:SET sim:scenario 时写进 simulation.redis_host 的地址。
        // 多机部署必须设为本机 LAN IP(远程 UE 回连用);缺省回退 REDIS_HOST。
        advertiseRedisHost: process.env.OPENSIM_ADVERTISE_REDIS_HOST ||
            process.env.REDIS_HOST || undefined,
        // UE load_scenario ack 超时(默认 60s)/ shutdown 等待(默认 15s,优雅退出 ~11s)。
        ueLoadTimeoutMs: parseInt(process.env.OPENSIM_UE_LOAD_TIMEOUT_MS || '60000'),
        ueShutdownGraceMs: parseInt(process.env.OPENSIM_UE_SHUTDOWN_GRACE_MS || '15000'),
    };
}
async function main() {
    const config = loadConfig();
    console.log(`Starting Redis WebSocket bridge...`);
    console.log(`  Redis: ${config.redisHost}:${config.redisPort}`);
    console.log(`  WebSocket: ws://localhost:${config.wsPort}`);
    console.log(`  Scenarios dir: ${config.scenariosDir}`);
    console.log(`  User algorithms dir: ${config.userAlgorithmsDir}`);
    // 确认 UE 渲染器编排是否武装(renderCtlBinary 缺省则休眠)。
    console.log(`  Render-ctl: ${config.renderCtlBinary ?? '(disabled — opensim-render-ctl not found)'}`);
    console.log(`  Renderers dir: ${config.renderersDir ?? '(default)'}`);
    console.log(`  Advertise Redis: ${config.advertiseRedisHost ?? '(fallback to REDIS_HOST)'}:${config.redisPort}`);
    // Spec 020: 把启动用 python 一并打到 banner,排查"前端刷新不出参赛者算法"
    // 时立刻能看出用的是包内捆绑 python 还是系统 python(script 不存在会静默丢)。
    console.log(`  Python: ${config.pythonBin}`);
    const bridge = new server_1.RedisWebSocketBridge(config);
    process.on('SIGINT', async () => {
        console.log('\nShutting down bridge...');
        await bridge.stop();
        process.exit(0);
    });
    process.on('SIGTERM', async () => {
        console.log('\nShutting down bridge...');
        await bridge.stop();
        process.exit(0);
    });
    try {
        await bridge.start();
        console.log('Bridge is running. Press Ctrl-C to stop.');
    }
    catch (error) {
        console.error('Failed to start bridge:', error);
        process.exit(1);
    }
}
main();
