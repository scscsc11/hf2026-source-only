export interface HudPanelOptions {
    /** 允许拖动面板（改 left/top）。默认 true。 */
    draggable?: boolean;
    /** 初始即为折叠态。 */
    defaultCollapsed?: boolean;
}
/**
 * 装配一个 HUD 面板的折叠+拖拽行为。在 app 启动时对每个 [data-hud-panel] 调用。
 * 事件委托/折叠记忆等面板内部逻辑不受影响 —— 本函数只管 header 的折叠与拖拽。
 */
export declare function initHudPanel(panel: HTMLElement, opts?: HudPanelOptions): void;
//# sourceMappingURL=hud-panel.d.ts.map