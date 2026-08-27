export interface CameraPipOptions {
    /** bridge 相机帧 WebSocket URL(如 ws://host:8082)。所有窗口共享一条连接。 */
    camWsUrl: string;
    /** 注入 WebSocket 构造器(测试用)。 */
    WebSocketImpl?: typeof WebSocket;
    /** 同时打开的最大窗口数(默认 4,布局左 2 右 2 对称)。 */
    maxWindows?: number;
    /**
     * 单窗关闭回调(× 按钮 / Esc)。携带被关 uid —— app 据此清掉该 uid 的
     * selection,避免下个 state tick 的 syncCameraPip 又把它开回来。
     */
    onClose?: (uid: string) => void;
}
export declare class CameraPip {
    private readonly opts;
    private readonly maxWindows;
    /** uid → 窗口。保持插入顺序(用 Map 的迭代序)以稳定分配布局槽位。 */
    private windows;
    /** 最后一次 open 的 uid(app.ts 用 currentUavId 判断是否已在看)。 */
    private lastUid;
    /** 所有窗口共享的 WS 拉流客户端(懒创建:首个窗口 open 时建)。 */
    private sharedClient;
    constructor(opts: CameraPipOptions);
    /** 懒创建共享 WS 客户端(首个窗口 open 时建,onFrame 按 uid 路由到窗口)。 */
    private ensureSharedClient;
    /** 兼容旧接口:返回最后 open 的 uid。 */
    get currentUavId(): string | null;
    /** 是否已为该 uid 开窗。 */
    isOpen(uid: string): boolean;
    /** 为指定 UAV 开窗(已开则忽略;达上限则 warn 拒绝)。 */
    open(uid: string): void;
    /** 关闭指定 uid 的窗(停流 + 释放位图 + 移除 DOM)。 */
    close(uid: string): void;
    /** 关闭全部窗口(仿真结束/重置时调用)。 */
    closeAll(): void;
    /** 释放窗口资源 + 移除 DOM。 */
    private teardownWindow;
    private createWindow;
    private bindWindowEvents;
    private applyFrame;
    /**
     * 把当前位图画到 canvas。canvas 内部分辨率为 1024×768(与 UE 推送帧同比例 4:3),
     * bitmap 也是 1024×768 → 直接全铺,无 contain 居中黑边。canvas 的 CSS 显示尺寸
     * 由窗口容器按比例缩放,故拖拽缩放窗口不产生黑边。
     * 若 bitmap 比例偶有偏差(非 4:3),用 cover 模式裁切填满,宁可裁切不留黑边。
     */
    private drawBitmap;
    private updateInfoBar;
    /** 找第一个未被占用的槽位 index(关闭窗口后其槽位被释放,可被新窗口复用)。 */
    private nextFreeSlot;
    /**
     * 按 win.slotIndex 把单个窗口定位到对应槽位(不动其他窗口)。
     * 偏移动态计算(按视口宽度),保证:
     *   - 外槽(rank 0)紧贴同侧 HUD 面板内侧(SIDE_PANEL_W + GAP)
     *   - 内槽(rank 1)越过居中评分窗半宽(SCORE_HALF_W + GAP),与评分窗不重叠
     * 相邻同侧窗口间距 = GAP(外槽右缘 + GAP = 内槽左缘)。
     */
    private positionWindow;
    private applyDefaultRect;
    private setCollapsed;
    private toggleFullscreen;
    private onDragStart;
    private onResizeStart;
}
//# sourceMappingURL=camera-pip.d.ts.map