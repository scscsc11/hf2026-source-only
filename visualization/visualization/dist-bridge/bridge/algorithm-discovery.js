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
exports.parsePyFile = parsePyFile;
exports.discoverAlgorithms = discoverAlgorithms;
exports.baselineEntry = baselineEntry;
// 参赛者算法发现 —— 扫描 competition/user_algorithms/<scenarioId>/*.py,
// spawn python ast 静态解析入口类(不 import 不执行参赛者代码)。
// 与 scenario-discovery.ts 对称:它发现赛题,本模块发现参赛者算法。
const fs_1 = require("fs");
const path = __importStar(require("path"));
const child_process_1 = require("child_process");
/**
 * spawn python 运行 parse_agent.py 解析单个 .py 文件。
 * @param pyFile    绝对路径
 * @param baseClasses 该赛题的基类名清单(逗号分隔给 python)
 * @param pythonBin python 可执行文件
 * @param scriptPath parse_agent.py 绝对路径
 */
function parsePyFile(pyFile, baseClasses, pythonBin, scriptPath) {
    return new Promise((resolve) => {
        const args = [scriptPath, pyFile, baseClasses.join(',')];
        const proc = (0, child_process_1.spawn)(pythonBin, args, { stdio: ['ignore', 'pipe', 'pipe'] });
        let stdout = '';
        let stderr = '';
        proc.stdout.on('data', (d) => { stdout += d.toString(); });
        proc.stderr.on('data', (d) => { stderr += d.toString(); });
        proc.on('error', () => resolve({ found: false, error: 'spawn_failed' }));
        proc.on('close', (code) => {
            // parse_agent.py 只输出一行 JSON; 容错:取最后一行非空。
            const lines = stdout.split('\n').filter((l) => l.trim());
            const last = lines[lines.length - 1];
            // 未产生任何 stdout:几乎总是脚本不存在或 python 失败,把常见信号分类上报。
            if (!last) {
                if (code === 2) {
                    resolve({ found: false, error: `script_not_found: ${stderr.slice(0, 200) || scriptPath}` });
                    return;
                }
                // python 自身报 "can't open file '<scriptPath>'" / "No such file" → 脚本不存在。
                if (/No such file|cannot open file|can't open file|找不到文件/i.test(stderr)) {
                    resolve({ found: false, error: `script_not_found: ${scriptPath}` });
                    return;
                }
                resolve({ found: false, error: `empty_output: rc=${code} stderr=${stderr.slice(0, 200)}` });
                return;
            }
            try {
                resolve(JSON.parse(last));
            }
            catch {
                resolve({ found: false, error: `bad_json: ${stderr.slice(0, 200) || last.slice(0, 200)}` });
            }
        });
    });
}
/**
 * 扫描 userAlgoRoot/<scenarioId>/*.py, 解析出参赛者算法清单。
 * 目录/文件不存在或解析失败 → 跳过,不抛错(空数组)。
 *
 * @param userAlgoRoot competition/user_algorithms 绝对路径
 * @param scenarioId   赛题 id, 决定子目录与 modulePath 前缀
 * @param baseClasses  该赛题 Agent 基类名清单
 * @param pythonBin    python 可执行文件(默认 'python')
 * @param scriptPath   parse_agent.py 绝对路径(默认与本模块同目录)
 */
async function discoverAlgorithms(userAlgoRoot, scenarioId, baseClasses, pythonBin = 'python', scriptPath = path.join(__dirname, 'parse_agent.py')) {
    const dir = path.join(userAlgoRoot, scenarioId);
    let files;
    try {
        files = (await fs_1.promises.readdir(dir)).filter((f) => f.endsWith('.py'));
    }
    catch {
        return []; // 目录不存在
    }
    const entries = [];
    for (const file of files) {
        const stem = file.slice(0, -3); // 去 .py
        const abs = path.join(dir, file);
        const parsed = await parsePyFile(abs, baseClasses, pythonBin, scriptPath);
        if (!parsed.found || !parsed.entryClass)
            continue;
        const modulePath = `user_algorithms.${scenarioId}.${stem}`;
        entries.push({
            entryClass: parsed.entryClass,
            shortName: parsed.shortName ?? '',
            docstring: parsed.docstring ?? '',
            name: parsed.shortName?.trim() || parsed.entryClass,
            modulePath,
            agentSpec: `${modulePath}:${parsed.entryClass}`,
            source: 'user',
            scenarioId,
        });
    }
    // 按文件名稳定排序。
    entries.sort((a, b) => a.modulePath.localeCompare(b.modulePath));
    return entries;
}
/**
 * 由 baselineAgent 字符串构造一个 baseline AlgorithmEntry。
 * baselineAgent 形如 'baselines.search_track_fsm:FsmAgent'。
 */
function baselineEntry(scenarioId, baselineAgent) {
    const [mod, cls] = baselineAgent.split(':');
    return {
        name: cls || baselineAgent,
        entryClass: cls || '',
        modulePath: mod || '',
        agentSpec: baselineAgent,
        docstring: '',
        shortName: '',
        source: 'baseline',
        scenarioId,
    };
}
