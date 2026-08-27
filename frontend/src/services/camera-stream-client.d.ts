/** 一帧解码结果(传给 onFrame 回调)。 */
export interface CameraFrame {
    frameNo: number;
    simTime: number;
    blob: Blob;
}
/** 去重/追帧决策(与 camera-frame-client 保持一致语义)。 */
export declare function shouldDisplayFrame(incomingFrameNo: number, displayedFrameNo: number): boolean;
export interface CameraStreamClientOptions {
    /** WS server URL,如 ws://host:8082。 */
    wsUrl: string;
    /** 收到某 uid 的新帧时回调(已去重)。 */
    onFrame: (uid: string, frame: CameraFrame) => void;
    /** 注入 WebSocket 构造器(测试用)。 */
    WebSocketImpl?: typeof WebSocket;
    /** 日志(默认 console.log)。 */
    log?: (msg: string) => void;
}
export declare class CameraStreamClient {
    private readonly wsUrl;
    private readonly onFrame;
    private readonly WebSocketCtor;
    private readonly log;
    private ws;
    private connecting;
    /** 已 subscribe 的 uid 集合(重连后重新订阅)。 */
    private subscribedUids;
    /** uid → 已显示的最大 frame_no(去重)。 */
    private displayedFrameNo;
    /** 重连退避(毫秒)。 */
    private reconnectDelay;
    private reconnectTimer;
    /** 是否已主动 close(阻止重连)。 */
    private closed;
    constructor(opts: CameraStreamClientOptions);
    /** 订阅指定 uid 的帧流(幂等;首次 subscribe 触发懒连接)。 */
    subscribe(uid: string): void;
    /** 取消订阅指定 uid(发 unsubscribe + 清去重状态)。 */
    unsubscribe(uid: string): void;
    /** 关闭连接 + 清状态(不再重连)。 */
    close(): void;
    /** 确保 WS 已连接(懒连接:首次 subscribe 时建)。 */
    private ensureConnected;
    /** 解析 binary 帧消息:读 header → 提取 uid/frameNo/simTime → 去重 → onFrame。 */
    private handleBinary;
    /** 指数退避重连(上限 10 秒)。 */
    private scheduleReconnect;
}
//# sourceMappingURL=camera-stream-client.d.ts.map