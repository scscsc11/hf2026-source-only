export interface PanelAlgorithm {
    name: string;
    agentSpec: string;
    source: 'baseline' | 'user';
    scenarioId: string;
    docstring?: string;
}
export interface PanelScenario {
    id: string;
    name: string;
    label?: string;
    description: string;
    available?: boolean;
    algorithms?: PanelAlgorithm[];
}
export interface ScenarioSelectorEvents {
    onStart?: (id: string, agent?: string, routeSeed?: number) => void;
    onPauseResume?: () => void;
    onClose?: () => void;
    onRefresh?: (scenarioId: string) => void;
    /** 天气变更(启动前写回 scenario.json)。 */
    onWeatherChange?: (weather: string) => void;
    /** 车辆随机路线种子变更(字符串, 用户输入; 仅正整数会被透传)。 */
    onRouteSeedChange?: (seed: number | null) => void;
}
export declare class ScenarioSelectorPanel {
    private events;
    private scenarios;
    /** 当前选中的赛题 id(取代原来的 expandedId)。 */
    private selectedId;
    /** 算法模式: baseline(官方示例) 或 user(参赛者)。 */
    private mode;
    /** user 模式下选中的参赛者算法 agentSpec。 */
    private selectedAlgo;
    private status;
    private pct;
    private phase;
    private errorMsg;
    private stallTimer;
    /** 刷新按钮恢复函数(请求完成后重置按钮状态)。 */
    private refreshBtnReset;
    private weather;
    /** 车辆随机路线种子(字符串输入, 内部 parse 为 number 或 null)。 */
    private routeSeedInput;
    constructor(containerId: string);
    private render;
    private bindEvents;
    /** 解析种子输入框: 正整数才返回(后端按 seed%30 选路线), 否则 null。 */
    private parsedRouteSeed;
    /**
     * 解析当前应启动的 agent。
     * @returns agentSpec 字符串; null 表示不可启动(user 模式但未选具体参赛者算法)。
     */
    private resolveAgent;
    private baselineSpec;
    setScenarios(list: PanelScenario[]): void;
    /** 刷新单个赛题的算法列表(user 模式下保留仍存在的选中, 否则清空选中)。 */
    refreshAlgos(scenarioId: string, algorithms: PanelAlgorithm[]): void;
    private renderList;
    private renderScenario;
    private renderAlgoPicker;
    selectById(id: string): void;
    updateProgress(pct: number, phase?: string): void;
    updateStatus(status: string, error?: string | null): void;
    /**
     * 根据当前 status 切换天气 select / 种子 input 的 disabled 态。
     * 锁定条件与 start 按钮的启用条件互补: 内核非存活(idle/error)才开放。
     */
    private updateConfigLockState;
    private updateButtonStates;
    private armStallTimer;
    private clearStallTimer;
    getWeather(): string;
    /** 当前解析后的种子(正整数才返回); 未填或非法返回 null。 */
    getRouteSeed(): number | null;
    on<K extends keyof ScenarioSelectorEvents>(event: K, callback: NonNullable<ScenarioSelectorEvents[K]>): void;
}
//# sourceMappingURL=scenario-selector-panel.d.ts.map