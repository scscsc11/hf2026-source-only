"use strict";
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
exports.COMPETITION_SCENARIOS = void 0;
exports.discoverScenarios = discoverScenarios;
// Spec: competition 场景发现 —— 内置静态注册表(三个固定赛题),
// 校验 competition/scenarios/<id>/scenario.json 存在性。
// 替换原 example-discovery.ts(扫描 manifest.json 的 examples 模型)。
const fs_1 = require("fs");
const path = __importStar(require("path"));
const algorithm_discovery_1 = require("./algorithm-discovery");
/**
 * 内置 competition 场景注册表。来源:
 *   - id/scenarioJson: competition/scenarios/<id>/ 目录结构
 *   - name/description: 手工编写(参考各 scenario.json 的场景语义)
 *   - baselineAgent: competition/baselines/<file>:<Class>
 *   - defaultDuration: competition/sdk/cli.py 的 run() 默认时长
 *
 * 这三个赛题是固定的 competition 赛题,不做磁盘扫描(不同于 examples 的
 * manifest 模型)——增删赛题是低频的、需要同步改 Python 侧的改动。
 */
exports.COMPETITION_SCENARIOS = [
    {
        id: 'adversarial_swarm',
        name: '无人机群自主协同打击',
        description: '集群突防防空与干扰区域,搜索并打击目标',
        baselineAgent: 'baselines.swarm_distributed:SwarmDistributedAgent',
        defaultDuration: 600,
        scenarioJson: 'adversarial_swarm/scenario.json',
        agentBaseClasses: ['SwarmAgent', 'Agent'],
        order: 3,
        label: '赛题三',
    },
    {
        id: 'coop_decoy',
        name: '无人机群自主协同跟踪',
        description: '多无人机协同识别真实目标与诱饵',
        baselineAgent: 'baselines.coop_distributed:CoopDistributedAgent',
        defaultDuration: 600,
        scenarioJson: 'coop_decoy/scenario.json',
        agentBaseClasses: ['CoopAgent', 'Agent'],
        order: 2,
        label: '赛题二',
    },
    {
        id: 'search_track',
        name: '单无人机自主识别跟踪',
        description: '单无人机搜索并跟踪地面目标',
        baselineAgent: 'baselines.search_track_fsm:FsmAgent',
        defaultDuration: 600,
        scenarioJson: 'search_track/scenario.json',
        agentBaseClasses: ['SearchTrackAgent', 'Agent'],
        order: 1,
        label: '赛题一',
    },
];
/**
 * 返回全部 competition 场景,标注 scenario.json 是否存在。
 * 按 order 升序(赛题一在最上), 与字母序不同 —— 难度递增展示。
 */
async function discoverScenarios(scenariosDir, userAlgoRoot, pythonBin = 'python') {
    const result = [];
    for (const s of exports.COMPETITION_SCENARIOS) {
        const scenarioAbs = path.join(scenariosDir, s.scenarioJson);
        let available = true;
        try {
            await fs_1.promises.access(scenarioAbs);
        }
        catch {
            available = false;
        }
        // 组装算法清单: baseline(始终有) + 参赛者(扫描,可选)。
        const algorithms = [(0, algorithm_discovery_1.baselineEntry)(s.id, s.baselineAgent)];
        if (available && userAlgoRoot) {
            try {
                const userAlgos = await (0, algorithm_discovery_1.discoverAlgorithms)(userAlgoRoot, s.id, s.agentBaseClasses, pythonBin);
                algorithms.push(...userAlgos);
            }
            catch (e) {
                // 扫描失败不阻塞, 仅用 baseline; 但记录诊断便于排查。
                console.warn(`[discoverScenarios] user algo scan failed for ${s.id}: ${e.message}`);
            }
        }
        result.push({ ...s, available, algorithms });
    }
    result.sort((a, b) => a.order - b.order);
    return result;
}
