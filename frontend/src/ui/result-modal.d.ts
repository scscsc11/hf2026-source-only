/** 仿真结束结果总结弹窗 (result-modal)。
 *
 *  触发时机: sim:score 末帧(final=true)到达时由 app.ts 调 show()。
 *  解决的痛点: 仿真自然到时停止后, 旧逻辑在 session:idle 时无差别 scorePanel.reset()
 *  把最终分清掉, 用户"看不到得分"; 且 evaluation.json 保存在场景 output 目录、文件名是
 *  unix 时间戳, 用户既不知道在哪也找不到。本弹窗在结束时居中弹出, 醒目展示总分/
 *  PASS-FAIL/各维度, 并把完整 evaluation_path 可选中复制地展示出来。
 *
 *  设计与 ScorePanel 一致:
 *    1. 构造时一次性预创建所有 DOM, show() 仅改 textContent / width%, 不重建节点。
 *    2. 通过 .result-modal--open CSS 类切换显隐(默认 display:none)。
 *    3. 不直接 import RedisClient — 由调用方(app.ts onScore 回调)推送 ScoreSnapshot。
 *    4. 关闭途径三选一: 点「关闭」按钮 / 按 ESC / 点遮罩空白区。
 *    5. 维度中文标签复用 score-labels.ts(与 ScorePanel 同源, 不漂移)。
 */
import type { ScoreSnapshot } from '../core/redis-client';
/** 控制弹窗形态的 CSS 类(便于测试断言)。 */
export declare const RESULT_MODAL_CLASSES: {
    readonly container: "result-modal";
    readonly open: "result-modal--open";
};
/** 弹窗按钮回调注入接口(app.ts 提供「关闭」与「再跑一次」的真实行为)。 */
export interface ResultModalCallbacks {
    /** 用户点了「再跑一次」(或按对应快捷键) — 用上次会话参数重新启动仿真。 */
    onRerun?: () => void;
}
export declare class ResultModal {
    private readonly container;
    private overlay;
    private card;
    private totalEl;
    private badgeEl;
    private profileEl;
    private dimensionsEl;
    private evaluationPathEl;
    private closeBtn;
    private rerunBtn;
    private readonly callbacks;
    /** 当前已渲染的维度 key 顺序 — 用于稳定结构的 in-place 更新(同 ScorePanel)。 */
    private renderedDimKeys;
    /** 当前锁定的 profile(首个非空 snapshot.profile), 防跨 profile 闪烁。 */
    private renderedProfile;
    /** ESC 键监听句柄(show 时绑, hide 时解, 避免泄漏/重复触发)。 */
    private escHandler;
    constructor(containerId?: string, callbacks?: ResultModalCallbacks);
    /** 由 app.ts onScore 回调在 final 帧调用 — 填充并显示弹窗。空快照 = noop。 */
    show(snapshot: ScoreSnapshot | null | undefined): void;
    /** 关闭弹窗(点关闭/ESC/遮罩)。不重置已填内容 — 下次 show 会重新 fill。 */
    hide(): void;
    /** 是否处于打开态(便于测试断言)。 */
    isOpen(): boolean;
    /** 用快照数据填充卡片内容(总分/徽章/profile/维度/路径)。 */
    private fillCard;
    private buildDom;
    private setBadge;
    private parseDimensions;
    /**
     * Stable-structure incremental update(同 ScorePanel): 首次按 key 顺序建行,
     * 后续只更新数值与宽度条, 避免重建 DOM 让条形图"跳"。
     */
    private renderDimensions;
    private buildDimRow;
    /** show() 时绑定 ESC 关闭; hide() 时解绑(避免弹窗关闭后仍拦截键盘)。 */
    private bindEsc;
    private unbindEsc;
}
//# sourceMappingURL=result-modal.d.ts.map