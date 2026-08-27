"use strict";
// 022 — 相机帧 WebSocket 推送 server(替代 HTTP /cam/:uid/latest)。
//
// 背景:HTTP 短连接模式下,前端 30Hz × N 路拉帧会打满浏览器对单域名的
// 连接池(6 并发),偶发 700ms+ 长尾尖刺,fetch 挂起 → inFlight 锁死 →
// 该路画面停顿。10 路目标下 300 req/s 必然撑不住。
//
// 改为 WS 推模式:bridge 后台 frameStore 刷新出新帧时,主动 broadcast 给
// 订阅该 uid 的 WS 客户端。前端一条长连接,无连接池问题,bridge 控制推送节奏。
//
// 二进制协议(WS binary frame):
//   偏移  长度  字段        说明
//   0     4     frame_no    uint32 LE,帧号(前端去重依据)
//   4     8     sim_time    float64 LE,仿真时刻
//   12    2     uid_len     uint16 LE,uid UTF-8 字节数
//   14    2     format      uint16 LE,图像格式枚举:0=PNG 1=JPEG(见 FORMAT_*)
//   16    N     uid         UTF-8 字符串
//   16+N  M     image       PNG 或 JPEG 原始二进制(按 format 解析)
//
// 控制消息(text frame, JSON):
//   客户端 → server: {type:'subscribe'|'unsubscribe', uids:['10002']}
//   server → 客户端: {type:'subscribe-ack'|'error', ...}
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.HEADER_LEN = exports.CameraWsServer = void 0;
exports.encodeFrameMessage = encodeFrameMessage;
exports.encodeFrameMessageCompressed = encodeFrameMessageCompressed;
const ws_1 = require("ws");
const image_compress_1 = require("./image-compress");
/** 固定 header 长度(字节)。 */
const HEADER_LEN = 16;
exports.HEADER_LEN = HEADER_LEN;
/** header 偏移14 的 format 字段枚举(图像格式)。 */
const FORMAT_PNG = 0;
const FORMAT_JPEG = 1;
/**
 * 相机帧 WS 推送 server。
 *
 * 生命周期:由 server.ts 在 startCameraService 中创建,start() 启动,
 * stop() 关闭。推送钩子(onFrameUpdate)在 frameStore 刷新新帧时被调用。
 */
class CameraWsServer {
    constructor(opts) {
        this.wss = null;
        /** uid → 订阅它的客户端集合。 */
        this.subscriptions = new Map();
        /** ws → 它订阅的 uid 集合(反向索引,断开时 O(1) 清理)。 */
        this.clientUids = new Map();
        /** 推送钩子解绑函数(start 时挂到 frameStore)。 */
        this.unbindHook = null;
        /** 诊断:统计推送次数/字节数(日志节流输出)。 */
        this.pushedFrames = 0;
        this.pushedBytes = 0;
        this.lastStatsAt = Date.now();
        this.port = opts.port;
        this.frameStore = opts.frameStore;
        this.redis = opts.redis;
        this.log = opts.log ?? ((m) => console.log(m));
    }
    /** 启动 WS server 并挂上 frameStore 推送钩子。 */
    start() {
        return new Promise((resolve, reject) => {
            try {
                this.wss = new ws_1.WebSocketServer({ port: this.port });
            }
            catch (e) {
                reject(e);
                return;
            }
            this.wss.on('connection', (ws) => this.handleConnection(ws));
            this.wss.on('listening', () => {
                this.log(`[CameraWs] listening on :${this.port}`);
                resolve();
            });
            this.wss.on('error', (err) => {
                this.log(`[CameraWs] server error: ${err.message}`);
            });
            // 挂推送钩子:frameStore 每次更新某 uid 缓存帧 → 广播给订阅者。
            // 异步(pushFrame 内含 sharp 压缩);fire-and-forget + 错误兜底,
            // 防止压缩 reject 漏到全局 unhandledRejection。
            this.unbindHook = this.frameStore.hookFrameUpdate((uid, frame) => {
                void this.pushFrame(uid, frame).catch((e) => this.log(`[CameraWs] pushFrame error: ${e.message}`));
            });
        });
    }
    /** 关闭 WS server + 解绑推送钩子。 */
    stop() {
        if (this.unbindHook) {
            this.unbindHook();
            this.unbindHook = null;
        }
        return new Promise((resolve) => {
            if (!this.wss) {
                resolve();
                return;
            }
            // 关闭所有客户端连接(触发各 ws 的 close,清理订阅)。
            for (const ws of this.clientUids.keys()) {
                try {
                    ws.close();
                }
                catch { /* ignore */ }
            }
            this.wss.close(() => {
                this.wss = null;
                this.subscriptions.clear();
                this.clientUids.clear();
                resolve();
            });
        });
    }
    // ── 连接 / 消息处理 ──────────────────────────────────────
    handleConnection(ws) {
        this.clientUids.set(ws, new Set());
        this.log(`[CameraWs] client connected (total=${this.clientUids.size})`);
        ws.on('message', (data, isBinary) => {
            // 只处理 text 控制消息;忽略意外 binary(客户端不应发)。
            if (isBinary)
                return;
            void this.handleTextMessage(ws, data.toString());
        });
        ws.on('close', () => this.handleDisconnect(ws));
        ws.on('error', () => this.handleDisconnect(ws));
    }
    async handleTextMessage(ws, text) {
        let msg;
        try {
            msg = JSON.parse(text);
        }
        catch {
            this.sendControl(ws, { type: 'error', error: 'invalid_json' });
            return;
        }
        if (msg.type === 'subscribe') {
            const uids = Array.isArray(msg.uids) ? msg.uids.filter((u) => typeof u === 'string') : [];
            for (const uid of uids)
                this.addSubscription(ws, uid);
            this.sendControl(ws, { type: 'subscribe-ack', uids });
            // 立即为每个新订阅 uid 推一帧已有缓存(若有),避免等下一次 refresh。
            for (const uid of uids) {
                void this.pushInitialFrame(ws, uid);
            }
        }
        else if (msg.type === 'unsubscribe') {
            const uids = Array.isArray(msg.uids) ? msg.uids.filter((u) => typeof u === 'string') : [];
            for (const uid of uids)
                this.removeSubscription(ws, uid);
            this.sendControl(ws, { type: 'unsubscribe-ack', uids });
        }
        else {
            this.sendControl(ws, { type: 'error', error: 'unknown_type' });
        }
    }
    handleDisconnect(ws) {
        const uids = this.clientUids.get(ws);
        if (!uids)
            return;
        for (const uid of uids) {
            const subs = this.subscriptions.get(uid);
            if (subs) {
                subs.delete(ws);
                if (subs.size === 0)
                    this.subscriptions.delete(uid);
            }
        }
        this.clientUids.delete(ws);
        this.log(`[CameraWs] client disconnected (total=${this.clientUids.size})`);
    }
    // ── 订阅管理 ────────────────────────────────────────────
    addSubscription(ws, uid) {
        let subs = this.subscriptions.get(uid);
        if (!subs) {
            subs = new Set();
            this.subscriptions.set(uid, subs);
        }
        subs.add(ws);
        this.clientUids.get(ws)?.add(uid);
    }
    removeSubscription(ws, uid) {
        const subs = this.subscriptions.get(uid);
        if (subs) {
            subs.delete(ws);
            if (subs.size === 0)
                this.subscriptions.delete(uid);
        }
        this.clientUids.get(ws)?.delete(uid);
    }
    // ── 推送 ────────────────────────────────────────────────
    /** 把一帧推给订阅该 uid 的所有 OPEN 客户端(异步:含 PNG→JPEG 压缩)。 */
    async pushFrame(uid, frame) {
        const subs = this.subscriptions.get(uid);
        if (!subs || subs.size === 0)
            return; // 无人订阅,跳过编码
        const msg = await encodeFrameMessageCompressed(frame, uid);
        let sent = 0;
        for (const ws of subs) {
            if (ws.readyState === ws_1.WebSocket.OPEN) {
                ws.send(msg);
                sent++;
            }
        }
        if (sent > 0) {
            this.pushedFrames++;
            this.pushedBytes += msg.length * sent;
            this.maybeLogStats();
        }
    }
    /** subscribe 后立即推已有缓存帧(若 frameStore 有)。 */
    async pushInitialFrame(ws, uid) {
        if (ws.readyState !== ws_1.WebSocket.OPEN)
            return;
        const cached = this.frameStore.get(uid);
        let frame;
        if (cached === undefined || cached === null) {
            // frameStore 无缓存,回退直接读一次(首帧延迟可接受)。
            // 注:这里不 import readLatestFrame 避免循环依赖风险,用 redis 接口。
            const { readLatestFrame } = await Promise.resolve().then(() => __importStar(require('./frame-reader')));
            frame = await readLatestFrame(this.redis, uid);
        }
        else {
            frame = cached;
        }
        if (frame && ws.readyState === ws_1.WebSocket.OPEN) {
            ws.send(await encodeFrameMessageCompressed(frame, uid));
        }
    }
    /** 节流统计日志:每 5 秒打一次推送量。 */
    maybeLogStats() {
        const now = Date.now();
        if (now - this.lastStatsAt < 5000)
            return;
        const secs = (now - this.lastStatsAt) / 1000;
        const mb = (this.pushedBytes / 1024 / 1024).toFixed(1);
        this.log(`[CameraWs] stats: ${this.pushedFrames} frames / ${secs.toFixed(1)}s, ${mb}MB pushed, subs=${this.subscriptions.size}`);
        this.pushedFrames = 0;
        this.pushedBytes = 0;
        this.lastStatsAt = now;
    }
    sendControl(ws, payload) {
        if (ws.readyState === ws_1.WebSocket.OPEN) {
            ws.send(JSON.stringify(payload));
        }
    }
}
exports.CameraWsServer = CameraWsServer;
/**
 * 编码一帧为 WS binary message(同步,不压缩,format=PNG)。
 * 格式见文件头注释(16 字节 header + uid + image)。
 * 保留同步版本供现有单测;生产推送走异步 encodeFrameMessageCompressed。
 */
function encodeFrameMessage(frame, uid) {
    return encodeWithFormat(frame, uid, FORMAT_PNG, frame.image);
}
/**
 * 编码一帧并按需压缩:PNG → JPEG(10× 缩),非 PNG 原样。
 * 生产推送路径(pushFrame / pushInitialFrame)用此函数。每帧/uid 只压一次,
 * 多订阅者复用同一 buffer。压缩在 encode 出口,缓存层仍存原始 PNG。
 */
async function encodeFrameMessageCompressed(frame, uid) {
    const compressed = await (0, image_compress_1.toJpegIfPng)(frame.image);
    const format = compressed === frame.image ? FORMAT_PNG : FORMAT_JPEG;
    return encodeWithFormat(frame, uid, format, compressed);
}
/** 内部:用指定 format + image 拼一个 WS binary message。 */
function encodeWithFormat(frame, uid, format, image) {
    const uidBytes = Buffer.from(uid, 'utf8');
    if (uidBytes.length > 0xffff) {
        throw new Error(`uid too long (${uidBytes.length} bytes)`);
    }
    const buf = Buffer.allocUnsafe(HEADER_LEN + uidBytes.length + image.length);
    buf.writeUInt32LE(frame.frameNo >>> 0, 0);
    buf.writeDoubleLE(frame.simTime, 4);
    buf.writeUInt16LE(uidBytes.length, 12);
    buf.writeUInt16LE(format, 14); // format:0=PNG 1=JPEG
    uidBytes.copy(buf, HEADER_LEN);
    image.copy(buf, HEADER_LEN + uidBytes.length);
    return buf;
}
