export declare class OnboardingTour {
    private currentIdx;
    private active;
    private overlay;
    private card;
    private highlight;
    private boundKeydown;
    private boundReposition;
    /** 引导是否正在播放。 */
    get isActive(): boolean;
    /** 从欢迎卡开始播放引导。重复调用被忽略。 */
    start(): void;
    /** 注入引导样式(仅一次)。 */
    private injectStyle;
    /** 构建遮罩 + 高亮框 + 卡片骨架(一次性)。 */
    private buildDom;
    /** 渲染当前步卡片: 欢迎卡居中, 操作步聚光灯定位。 */
    private render;
    /** 进度点 HTML。 */
    private renderDots;
    /** 欢迎卡居中定位。 */
    private centerCard;
    /** 聚光灯定位: 高亮目标 + 卡片贴边。目标缺失时降级居中。 */
    private positionCard;
    /** 设置卡片箭头方向(指向目标一侧)。 */
    private setArrow;
    /** 绑定按钮点击 + 键盘 + resize/scroll。 */
    private bindEvents;
    /** 推进到下一步; 末步则完成。 */
    private next;
    /** 跳过引导(销毁 + 标记已读)。 */
    private skip;
    /** 完成引导(销毁 + 标记已读)。
     *  公开出口(宿主/测试可直接调用); next/skip 仅供内部事件处理器调用。 */
    complete(): void;
    /** 解绑事件 + 移除 DOM(供 skip/complete 共用, 不动 active 标志)。 */
    private teardown;
}
//# sourceMappingURL=onboarding-tour.d.ts.map