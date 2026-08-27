import { type AlgorithmEntry } from './algorithm-discovery';
/** 面板/API 暴露的 competition 场景视图。 */
export interface Scenario {
    id: string;
    name: string;
    /** UI 显示编号前缀, 如 "赛题一"。 */
    label: string;
    /** 显示排序(升序); 单目标追踪=1 最上, 对抗集群=3 最下。 */
    order: number;
    description: string;
    baselineAgent: string;
    defaultDuration: number;
    scenarioJson: string;
    /** scenario 文件是否真实存在(启动前校验)。 */
    available: boolean;
    /** 该赛题的算法清单(官方 baseline + 参赛者扫描)。 */
    algorithms: AlgorithmEntry[];
}
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
export declare const COMPETITION_SCENARIOS: ReadonlyArray<Omit<Scenario, 'available' | 'algorithms'> & {
    agentBaseClasses: string[];
    order: number;
    label: string;
}>;
/**
 * 返回全部 competition 场景,标注 scenario.json 是否存在。
 * 按 order 升序(赛题一在最上), 与字母序不同 —— 难度递增展示。
 */
export declare function discoverScenarios(scenariosDir: string, userAlgoRoot?: string, pythonBin?: string): Promise<Scenario[]>;
//# sourceMappingURL=scenario-discovery.d.ts.map