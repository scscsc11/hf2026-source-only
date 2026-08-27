/** Spec 025 — 实时评分浮动居中面板 (sim:score 频道消费者)。
 *
 *  该面板在屏幕中央以两种状态存在:
 *    - 折叠态: 一个 56px 圆形按钮,只显示当前总分(整数),半透明玻璃质感,
 *      点击后切换为展开态。
 *    - 展开态: 居中的窄面板(max-width 320px),显示总分大字号、通过/未通过徽章、
 *      profile 名、sim_time、维度明细(7 行条形图)、最终帧持久化路径提示,
 *      标题右侧的 × 按钮可折回折叠态。
 *
 *  设计原则:
 *    1. 构造时一次性预创建所有 DOM,update() 仅修改 textContent / width%,
 *       不重建节点 — 与 entity-info-panel 同款的"稳定结构 + 增量更新"模式,
 *       避免 10 Hz 高频更新时 click 事件被劫持或闪烁。
 *    2. 通过 .score-panel--collapsed / .score-panel--expanded CSS 类切换状态。
 *    3. 仿真器尚未发布 score / 切换到非评分示例 / 关闭仿真 三种情况下,面板
 *       走 reset() → 显示 "等待评分…" 占位文字(road 例用 showUnavailable())。
 *    4. 不直接 import RedisClient — 由调用方(redis-client.onScore 回调)
 *       推送 ScoreSnapshot 数据;面板只负责渲染。
 */
import type { ScoreSnapshot } from '../core/redis-client';
/** Spec 025: 控制面板展示形态的 CSS 类常量(便于测试断言)。 */
export declare const SCORE_PANEL_CLASSES: {
    readonly container: "score-panel";
    readonly collapsed: "score-panel--collapsed";
    readonly expanded: "score-panel--expanded";
    readonly unavailable: "score-panel--unavailable";
};
export declare class ScorePanel {
    private readonly container;
    private collapsedBtn;
    private collapsedScoreEl;
    private expandedPanel;
    private closeBtn;
    private totalEl;
    private badgeEl;
    private profileEl;
    private simTimeEl;
    private dimensionsEl;
    private finalEl;
    private evaluationPathEl;
    private unavailableEl;
    /** 当前已渲染的维度 key 顺序 — 用于稳定结构的 in-place 更新。 */
    private renderedDimKeys;
    /**
     * 当前锁定的 profile(首个非空 snapshot.profile)。一旦锁定, 来自其他
     * profile 的帧(残留进程/旧 publisher 混入)被忽略, 避免维度 key 集逐帧
     * 变化导致 renderDimensions 全量重建 → 闪烁。reset() 清空以支持合法的
     * 赛题切换(切换时 app.ts 会先调 reset)。
     */
    private renderedProfile;
    constructor(containerId?: string);
    /** 每 tick 由 redis-client.onScore 回调推送的快照。空快照 = noop。 */
    update(snapshot: ScoreSnapshot | null | undefined): void;
    /** sessionStatus === 'idle' 时调用 — 清空面板回到"等待评分…"。 */
    reset(): void;
    /** 当前示例无 score 系统(road_target 等) — 显示占位文字。 */
    showUnavailable(reason: string): void;
    /** 测试/外部代码主动展开 / 折叠。 */
    expand(): void;
    collapse(): void;
    /** 是否处于展开态(便于测试断言)。 */
    isExpanded(): boolean;
    /** 是否处于 "unavailable" 占位态。 */
    isUnavailable(): boolean;
    private buildDom;
    private setBadge;
    /** 把 evaluator 返回的 dimension_scores 字典归一为有序 DimRow[]。 */
    private parseDimensions;
    /**
     * Stable-structure incremental update: 首次按 key 顺序建行,后续只更新
     * 数值与宽度条,避免重建 DOM 让条形图"跳"。
     */
    private renderDimensions;
    private buildDimRow;
}
//# sourceMappingURL=score-panel.d.ts.map