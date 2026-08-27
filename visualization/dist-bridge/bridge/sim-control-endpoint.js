"use strict";
// 仿真控制 HTTP 端点(DI 工厂,仿 camera-endpoint.ts)。
//
// 路由(挂在 bridge camHttpServer,基址 http://host:8081):
//   GET  /api/scenarios    → competition 场景清单(scenario-discovery)
//   GET  /api/sim/status   → 当前会话状态
//   POST /api/sim/start    → 启动 competition 进程
//   POST /api/sim/pause    → 暂停推演
//   POST /api/sim/resume   → 恢复推演
//   POST /api/sim/stop     → 关闭并清理
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.createSimControlHandler = createSimControlHandler;
const path = __importStar(require("path"));
const fs = __importStar(require("fs"));
const scenario_discovery_1 = require("./scenario-discovery");
const algorithm_discovery_1 = require("./algorithm-discovery");
const CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
};
function jsonRes(res, status, body) {
    res.writeHead(status, { ...CORS, 'Content-Type': 'application/json' });
    res.end(body === null || body === undefined ? '' : JSON.stringify(body));
}
async function readJsonBody(req) {
    const chunks = [];
    for await (const c of req) {
        chunks.push(c);
        if (chunks.reduce((a, b) => a + b.length, 0) > 1 << 20)
            break; // 1MB cap
    }
    const raw = Buffer.concat(chunks).toString('utf8').trim();
    if (!raw)
        return {};
    try {
        return JSON.parse(raw);
    }
    catch {
        return {};
    }
}
/** 构造 HTTP 请求处理函数(注入 manager + 配置,便于测试)。 */
function createSimControlHandler(deps) {
    return async (req, res) => {
        if (req.method === 'OPTIONS') {
            jsonRes(res, 204, null);
            return;
        }
        const url = req.url || '';
        const method = req.method || 'GET';
        try {
            if (method === 'GET' && url === '/api/scenarios') {
                const scenarios = await (0, scenario_discovery_1.discoverScenarios)(deps.scenariosDir, deps.userAlgorithmsDir, deps.pythonBin);
                jsonRes(res, 200, { scenarios });
                return;
            }
            if (method === 'GET' && url.startsWith('/api/algorithms/')) {
                // 去掉可能的 query string(?foo=bar)再解码, 防止 scenarioId 带上查询串导致 find 落空。
                const raw = url.slice('/api/algorithms/'.length).split('?')[0] ?? '';
                const scenarioId = decodeURIComponent(raw);
                const meta = scenario_discovery_1.COMPETITION_SCENARIOS.find((s) => s.id === scenarioId);
                if (!meta) {
                    jsonRes(res, 404, { ok: false, error: 'scenario_not_found' });
                    return;
                }
                const algorithms = [(0, algorithm_discovery_1.baselineEntry)(meta.id, meta.baselineAgent)];
                if (deps.userAlgorithmsDir) {
                    try {
                        algorithms.push(...await (0, algorithm_discovery_1.discoverAlgorithms)(deps.userAlgorithmsDir, meta.id, meta.agentBaseClasses, deps.pythonBin));
                    }
                    catch (e) {
                        console.warn(`[/api/algorithms] scan failed for ${meta.id}: ${e.message}`);
                    }
                }
                jsonRes(res, 200, { scenarioId, algorithms });
                return;
            }
            if (method === 'GET' && url === '/api/sim/status') {
                jsonRes(res, 200, deps.manager.getState());
                return;
            }
            if (method === 'POST' && url === '/api/sim/start') {
                const body = await readJsonBody(req);
                const id = typeof body.scenario === 'string' ? body.scenario : null;
                if (!id) {
                    jsonRes(res, 400, { ok: false, error: 'scenario_required' });
                    return;
                }
                const agent = typeof body.agent === 'string' && body.agent.trim() ? body.agent.trim() : undefined;
                // Spec 033 + 相机流自动启用: 感知参数白名单/类型守卫。
                // photoMode 三态优先；旧 body.photo 布尔回退映射（true→on, false→off）；
                // 二者都未提供 → 'auto'（带 UE 标准环境自动拉取相机帧）。
                const mode = body.mode === 'eval' ? 'eval'
                    : body.mode === 'train' ? 'train' : undefined;
                const photoMode = body.photoMode === 'auto' || body.photoMode === 'on' || body.photoMode === 'off'
                    ? body.photoMode
                    : typeof body.photo === 'boolean'
                        ? (body.photo ? 'on' : 'off')
                        : 'auto';
                const yoloModel = typeof body.yoloModel === 'string' && body.yoloModel.trim()
                    ? body.yoloModel.trim() : undefined;
                // 防真值泄漏钳制：accuracy ∈ [0, 0.9]（杜绝 acc=1.0 退化等价真值），
                // noiseSigma ≥ 30（杜绝 noise=0 位置等于真值）。非数 → undefined（用默认）。
                const accuracy = typeof body.accuracy === 'number' && Number.isFinite(body.accuracy)
                    ? Math.min(Math.max(body.accuracy, 0), 0.9) : undefined;
                const noiseSigma = typeof body.noiseSigma === 'number' && Number.isFinite(body.noiseSigma)
                    ? Math.max(body.noiseSigma, 30) : undefined;
                // 路线种子(前端 generate 产生): 正整数才透传(0/非数 → undefined,不追加 --seed)。
                const routeSeed = typeof body.routeSeed === 'number' && Number.isFinite(body.routeSeed)
                    && body.routeSeed > 0 ? Math.floor(body.routeSeed) : undefined;
                // start 只需校验赛题存在 + 取 baselineAgent/scenarioJson, 不读 algorithms。
                // 故传 undefined 跳过参赛者算法 python 扫描, 避免热路径上无用的子进程 spawn。
                const scenarios = await (0, scenario_discovery_1.discoverScenarios)(deps.scenariosDir, undefined, deps.pythonBin);
                const sc = scenarios.find((s) => s.id === id);
                if (!sc || !sc.available) {
                    jsonRes(res, 400, { ok: false, error: 'scenario_not_found' });
                    return;
                }
                try {
                    const state = await deps.manager.start({
                        id: sc.id,
                        baselineAgent: sc.baselineAgent,
                        defaultDuration: sc.defaultDuration,
                        scenarioJson: sc.scenarioJson,
                        agent, // 选手算法（undefined 则回退 baselineAgent）
                        mode, photoMode, yoloModel, // 感知参数透传（photoMode 三态，默认 auto）
                        accuracy, noiseSigma, // 防泄漏钳制后的 accuracy/noise（可选）
                        routeSeed, // 路线种子(可选): --seed 透传给后端
                    }, {
                        pythonBin: deps.pythonBin,
                        scenariosDir: deps.scenariosDir,
                        // render-ctl plan 需要 scenario.json 绝对路径。
                        scenarioJsonAbs: path.join(deps.scenariosDir, sc.scenarioJson),
                        // 透传渲染器编排配置(缺省则无 UE spawn,仿真照跑)。
                        renderCtlBinary: deps.renderCtlBinary,
                        renderersDir: deps.renderersDir,
                        // service 模式想定下发:SET sim:scenario 时改写 redis_host。
                        advertiseRedisHost: deps.advertiseRedisHost,
                    });
                    jsonRes(res, 200, { ok: true, sessionId: state.sessionId, scenario: sc.id });
                }
                catch (e) {
                    const msg = e.message;
                    if (msg === 'session_already_active') {
                        jsonRes(res, 409, { ok: false, error: msg, status: deps.manager.getState().status });
                    }
                    else {
                        jsonRes(res, 503, { ok: false, error: msg });
                    }
                }
                return;
            }
            // 写回当前赛题想定的 weather 字段(就地改 scenario.json)。
            // bridge 启动仿真时会原样拷贝该文件到 UE,UE 读 weather 字段渲染。
            if (method === 'POST' && url === '/api/scenario/weather') {
                const body = await readJsonBody(req);
                const id = typeof body.scenario === 'string' ? body.scenario : null;
                const weather = typeof body.weather === 'string' ? body.weather.trim() : null;
                if (!id || !weather) {
                    jsonRes(res, 400, { ok: false, error: 'scenario_and_weather_required' });
                    return;
                }
                const meta = scenario_discovery_1.COMPETITION_SCENARIOS.find((s) => s.id === id);
                if (!meta) {
                    jsonRes(res, 400, { ok: false, error: 'scenario_not_found' });
                    return;
                }
                const scenarioPath = path.join(deps.scenariosDir, meta.scenarioJson);
                try {
                    const raw = await fs.promises.readFile(scenarioPath, 'utf8');
                    const scenario = JSON.parse(raw);
                    scenario.weather = { type: weather };
                    await fs.promises.writeFile(scenarioPath, JSON.stringify(scenario, null, 2) + '\n', 'utf8');
                    jsonRes(res, 200, { ok: true, scenario: id, weather });
                }
                catch (e) {
                    jsonRes(res, 500, { ok: false, error: `weather_write_failed: ${e.message}` });
                }
                return;
            }
            if (method === 'POST' && url === '/api/sim/pause') {
                try {
                    const s = await deps.manager.pause();
                    jsonRes(res, 200, { ok: true, status: s.status });
                }
                catch (e) {
                    jsonRes(res, 409, { ok: false, error: e.message, status: deps.manager.getState().status });
                }
                return;
            }
            if (method === 'POST' && url === '/api/sim/resume') {
                try {
                    const s = await deps.manager.resume();
                    jsonRes(res, 200, { ok: true, status: s.status });
                }
                catch (e) {
                    jsonRes(res, 409, { ok: false, error: e.message, status: deps.manager.getState().status });
                }
                return;
            }
            if (method === 'POST' && url === '/api/sim/stop') {
                const s = await deps.manager.stop();
                jsonRes(res, 200, { ok: true, status: s.status });
                return;
            }
            jsonRes(res, 404, { ok: false, error: 'not_found' });
        }
        catch (e) {
            jsonRes(res, 500, { ok: false, error: 'internal', detail: e.message });
        }
    };
}
