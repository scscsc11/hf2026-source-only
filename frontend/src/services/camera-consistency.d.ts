import type { SimulationState } from '../core/state-manager';
export interface GimbalPose {
    pan: number;
    tilt: number;
}
export declare class CameraConsistency {
    private readonly byUid;
    private readonly maxEntries;
    constructor(maxEntries?: number);
    /** 收到一帧 sim:state → 记录各 UAV 的 sim_time → gimbal 姿态。 */
    update(state: SimulationState): void;
    /** 对齐:给定帧 sim_ts → 最近 sim_time 的云台姿态;sim_ts 无效或无记录 → null(不可核验)。 */
    alignPose(uid: string, simTs: number): GimbalPose | null;
}
//# sourceMappingURL=camera-consistency.d.ts.map