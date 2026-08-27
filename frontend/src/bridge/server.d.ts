export interface BridgeConfig {
    wsPort: number;
    redisHost: string;
    redisPort: number;
    redisPassword?: string;
    /** 相机帧 HTTP 端点端口(默认 8081)。 */
    camHttpPort?: number;
    /** 相机帧 WebSocket 推送端口(默认 8082)。WS 推送替代 HTTP 拉取,支持多路。 */
    camWsPort?: number;
    scenariosDir?: string;
    userAlgorithmsDir?: string;
    pythonBin?: string;
    stopGrace?: number;
    renderCtlBinary?: string;
    renderersDir?: string;
    /**
     * 对外广播的 Redis 地址(OPENSIM_ADVERTISE_REDIS_HOST,默认回退 redisHost)。
     * SET sim:scenario 时写进 simulation.redis_host,远程 UE 拉到的想定自带此回连地址。
     */
    advertiseRedisHost?: string;
    /** UE load_scenario ack 超时毫秒(OPENSIM_UE_LOAD_TIMEOUT_MS,默认 60000)。 */
    ueLoadTimeoutMs?: number;
    /** UE shutdown 等待毫秒(OPENSIM_UE_SHUTDOWN_GRACE_MS,默认 15000;UE 优雅退出 ~11s)。 */
    ueShutdownGraceMs?: number;
}
export declare class RedisWebSocketBridge {
    private wss;
    private redisClient;
    private camHttpServer;
    private camRedis;
    private frameStore;
    private cameraWsServer;
    private clients;
    private subscriptions;
    private config;
    private simManager;
    private renderScheduler;
    private renderService;
    constructor(config: BridgeConfig);
    start(): Promise<void>;
    /**
     * 启动相机帧服务:
     *   - camHttpServer(:8081):仅服务 /api/ 仿真控制端点(原 camera HTTP 端点
     *     已迁移到 WS 推送,见下方 cameraWsServer)。
     *   - frameStore:后台游标追帧,WS 推送的数据源。
     *   - cameraWsServer(:8082):相机帧 WS 推送,frameStore 新帧 → broadcast。
     */
    private startCameraService;
    stop(): Promise<void>;
    private handleClientMessage;
    private handleSubscribe;
    private handleUnsubscribe;
    private handlePublish;
    private broadcastToSubscribers;
    /** Spec 024 (T047): 向所有已连接 WS 客户端主动广播(会话状态推送,不经 Redis)。 */
    private broadcastToAll;
    private removeClientFromSubscriptions;
}
//# sourceMappingURL=server.d.ts.map