"use strict";
// 022 UAV 相机视角实时视频流 — 消费端帧读取原语。
//
// 消费本机 Redis 中外部源写入的真实帧数据(实测格式,2026-06 变更):
//   hash key  sync_camera:{uav_id}:frame:{frame_no}
//   field     sim_time = 仿真时刻(double 字符串,单调递增)
//   field     image    = JPEG 二进制(以 0xFF 0xD8 0xFF 0xE0 开头,约 700KB)
// 无 latest/seq 指针 → 消费端 SCAN/KEYS 取最大帧号定位最新帧。
//
// 设计为纯逻辑 + 最小 redis 接口(便于注入 mock 测试)。二进制 image 用
// ioredis 的 hgetBuffer(返回 Buffer,二进制安全)。
Object.defineProperty(exports, "__esModule", { value: true });
exports.readNextFrame = readNextFrame;
exports.readLatestFrame = readLatestFrame;
const FRAME_KEY_RE = /^sync_camera:([^:]+):frame:(\d+)$/;
/**
 * 按游标读取指定 UAV 的下一帧(假设帧号单调递增)。
 * 仅当 `sync_camera:{uid}:frame:{lastFrameNo+1}` 存在且含 image 时返回。
 * @returns 下一帧;无下一帧时返回 null。
 */
async function readNextFrame(redis, uid, lastFrameNo) {
    const nextNo = lastFrameNo + 1;
    const key = `sync_camera:${uid}:frame:${nextNo}`;
    const image = await redis.hgetBuffer(key, 'image');
    if (!image)
        return null; // 下一帧尚未写入或已过期
    const simBuf = await redis.hgetBuffer(key, 'sim_time');
    const simTime = simBuf ? parseFloat(simBuf.toString('utf8')) : NaN;
    return { frameNo: nextNo, simTime, image };
}
/**
 * 定位并读取指定 UAV 的最新相机帧。
 * @returns 最新帧;无帧/缺 image 字段时返回 null。
 */
async function readLatestFrame(redis, uid) {
    const keys = await redis.keys(`sync_camera:${uid}:frame:*`);
    let maxNo = -1;
    let maxKey = null;
    for (const key of keys) {
        const m = key.match(FRAME_KEY_RE);
        const keyUid = m?.[1];
        const keyNo = m?.[2];
        if (!keyUid || keyUid !== uid || keyNo === undefined)
            continue; // 跳过非本 uid / 非帧 key
        const n = parseInt(keyNo, 10);
        if (n > maxNo) {
            maxNo = n;
            maxKey = key;
        }
    }
    if (maxKey === null)
        return null;
    const image = await redis.hgetBuffer(maxKey, 'image');
    if (!image)
        return null; // 缺 image 字段(JPEG)→ 视为不可用
    const simBuf = await redis.hgetBuffer(maxKey, 'sim_time');
    const simTime = simBuf ? parseFloat(simBuf.toString('utf8')) : NaN;
    return { frameNo: maxNo, simTime, image };
}
