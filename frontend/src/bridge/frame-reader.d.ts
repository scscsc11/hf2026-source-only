/** 消费端所需的最小 redis 能力(ioredis 子集,可 mock)。 */
export interface CameraFrameRedis {
    /** 匹配 key 列表(本机帧数百级,v1 用 KEYS;SCAN 为后续优化)。 */
    keys(pattern: string): Promise<string[]>;
    /** 取 hash 字段的二进制值(二进制安全)。 */
    hgetBuffer(key: string, field: string): Promise<Buffer | null>;
}
/** 定位到的最新帧。 */
export interface LatestCameraFrame {
    /** 帧号(来自 key 名,单调递增)。 */
    frameNo: number;
    /** 生成时间戳(仿真时刻,一致性核验锚点)。 */
    simTime: number;
    /** JPEG 二进制。 */
    image: Buffer;
}
/**
 * 按游标读取指定 UAV 的下一帧(假设帧号单调递增)。
 * 仅当 `sync_camera:{uid}:frame:{lastFrameNo+1}` 存在且含 image 时返回。
 * @returns 下一帧;无下一帧时返回 null。
 */
export declare function readNextFrame(redis: CameraFrameRedis, uid: string, lastFrameNo: number): Promise<LatestCameraFrame | null>;
/**
 * 定位并读取指定 UAV 的最新相机帧。
 * @returns 最新帧;无帧/缺 image 字段时返回 null。
 */
export declare function readLatestFrame(redis: CameraFrameRedis, uid: string): Promise<LatestCameraFrame | null>;
//# sourceMappingURL=frame-reader.d.ts.map