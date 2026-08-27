/** 算法条目(与 bridge AlgorithmEntry 对齐, 取 UI 需要的字段)。 */
export interface AlgorithmInfo {
    name: string;
    entryClass?: string;
    modulePath?: string;
    agentSpec: string;
    docstring?: string;
    shortName?: string;
    source: 'baseline' | 'user';
    scenarioId?: string;
}
export interface ScenarioInfo {
    id: string;
    name: string;
    label?: string;
    order?: number;
    description: string;
    baselineAgent: string;
    defaultDuration: number;
    scenarioJson: string;
    available: boolean;
    algorithms: AlgorithmInfo[];
}
export interface SimStatus {
    status: string;
    scenario: string | null;
    sessionId: string | null;
    error: string | null;
}
export interface SimControlResult {
    ok: boolean;
    httpStatus: number;
    sessionId?: string;
    scenario?: string;
    status?: string;
    error?: string;
}
export interface SimControlConfig {
    /** bridge HTTP 基址,如 http://host:8081。 */
    baseUrl: string;
    fetchImpl?: typeof fetch;
}
export interface PerceptionOverrides {
    mode?: 'train' | 'eval';
    photoMode?: 'auto' | 'on' | 'off';
    yoloModel?: string;
}
export declare class SimControlClient {
    private cfg;
    private fetchImpl;
    constructor(cfg: SimControlConfig);
    getScenarios(): Promise<ScenarioInfo[]>;
    /** 拉取单个赛题的算法清单(刷新用)。GET /api/algorithms/:scenarioId → {scenarioId, algorithms}。 */
    getAlgorithms(scenarioId: string): Promise<AlgorithmInfo[]>;
    start(scenario: string, agent?: string, perception?: PerceptionOverrides, routeSeed?: number): Promise<SimControlResult>;
    pause(): Promise<SimControlResult>;
    resume(): Promise<SimControlResult>;
    stop(): Promise<SimControlResult>;
    /** 写回指定赛题想定文件的 weather 字段(就地改 scenario.json)。 */
    setWeather(scenario: string, weather: string): Promise<SimControlResult>;
    getStatus(): Promise<SimStatus>;
    private post;
}
//# sourceMappingURL=sim-control-client.d.ts.map