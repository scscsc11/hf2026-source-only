/** 单个 UE 实例的 spawn 指令(render-ctl plan 输出的一项)。 */
export interface RenderInstance {
    rendererId: string;
    gpuIndex: number;
    cwd: string;
    argv: string[];
    env: Record<string, string>;
    maxAircraft: number;
}
/** render-ctl plan 的完整解析结果。 */
export interface RenderPlan {
    gimbalUavs: string[];
    instances: RenderInstance[];
    /** 因 VRAM/capacity 不足等原因被跳过的项(human-readable)。 */
    skipped: string[];
    /** 超出 UE 渲染容量、需要发 stop 指令的 aircraft ID 列表。 */
    excessUavs: string[];
}
/** 注入的 execFile 能力(屏蔽 Node callback 细节,测试可 mock)。 */
export type ExecFileFn = (cmd: string, args: string[]) => Promise<{
    stdout: string;
    stderr: string;
    exitCode: number;
}>;
export interface PlanRenderersDeps {
    execFile: ExecFileFn;
    /** opensim-render-ctl 二进制绝对路径。 */
    renderCtlBinary: string;
    /** scenario.json 绝对路径(--config 参数)。 */
    scenarioJsonAbs: string;
    /** config/renderers 目录(--renderers 参数)。 */
    renderersDir: string;
}
/**
 * 调 opensim-render-ctl plan,解析 stdout JSON 为 RenderPlan。
 *
 * 不做降级决策(抛错即"无法拿 plan");降级由调用方处理。
 * render-ctl 自身对"GPU 不可用"等返回 exitCode=0 + skipped[](见 render_ctl.cc:80),
 * 故 skipped 非空不是错误 —— 调用方据此决定是否仍 spawn instances。
 */
export declare function planRenderers(deps: PlanRenderersDeps): Promise<RenderPlan>;
/** 生产环境 execFile:基于 node:child_process.execFile 的 Promise 包装。 */
export declare function createNodeExecFile(): ExecFileFn;
//# sourceMappingURL=render-ctl-client.d.ts.map