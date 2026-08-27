/** 面板/API 暴露的算法条目。 */
export interface AlgorithmEntry {
    name: string;
    entryClass: string;
    modulePath: string;
    agentSpec: string;
    docstring: string;
    shortName: string;
    source: 'baseline' | 'user';
    scenarioId: string;
}
/** python parse_agent.py 的输出契约。 */
interface ParseResult {
    found: boolean;
    entryClass?: string;
    shortName?: string;
    docstring?: string;
    error?: string;
}
/**
 * spawn python 运行 parse_agent.py 解析单个 .py 文件。
 * @param pyFile    绝对路径
 * @param baseClasses 该赛题的基类名清单(逗号分隔给 python)
 * @param pythonBin python 可执行文件
 * @param scriptPath parse_agent.py 绝对路径
 */
export declare function parsePyFile(pyFile: string, baseClasses: string[], pythonBin: string, scriptPath: string): Promise<ParseResult>;
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
export declare function discoverAlgorithms(userAlgoRoot: string, scenarioId: string, baseClasses: string[], pythonBin?: string, scriptPath?: string): Promise<AlgorithmEntry[]>;
/**
 * 由 baselineAgent 字符串构造一个 baseline AlgorithmEntry。
 * baselineAgent 形如 'baselines.search_track_fsm:FsmAgent'。
 */
export declare function baselineEntry(scenarioId: string, baselineAgent: string): AlgorithmEntry;
export {};
//# sourceMappingURL=algorithm-discovery.d.ts.map