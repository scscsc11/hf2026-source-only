"use strict";
// Spec 028: opensim-render-ctl CLI 调用封装(纯函数 + 注入 execFile,便于测试)。
//
// opensim-render-ctl 是一个 plan-printer(不 spawn UE):
//   opensim-render-ctl plan --config <scenario.json> [--renderers <dir>]
// 输出 JSON:{ gimbal_uavs, instances[], skipped[] }
// bridge 拿到 instances 后自己 spawn UE(见 SimProcessManager)。
//
// 失败语义:render-ctl 非零退出 / stdout 非合法 JSON / 缺字段 → 抛错。
// 由调用方(SimProcessManager.start)捕获并降级(WARN + 仿真照跑)。
Object.defineProperty(exports, "__esModule", { value: true });
exports.planRenderers = planRenderers;
exports.createNodeExecFile = createNodeExecFile;
/**
 * 调 opensim-render-ctl plan,解析 stdout JSON 为 RenderPlan。
 *
 * 不做降级决策(抛错即"无法拿 plan");降级由调用方处理。
 * render-ctl 自身对"GPU 不可用"等返回 exitCode=0 + skipped[](见 render_ctl.cc:80),
 * 故 skipped 非空不是错误 —— 调用方据此决定是否仍 spawn instances。
 */
async function planRenderers(deps) {
    const { execFile, renderCtlBinary, scenarioJsonAbs, renderersDir } = deps;
    const args = ['plan', '--config', scenarioJsonAbs, '--renderers', renderersDir];
    let result;
    try {
        result = await execFile(renderCtlBinary, args);
    }
    catch (e) {
        throw new Error(`render-ctl exec failed: ${e.message}`);
    }
    if (result.exitCode !== 0) {
        // render-ctl 对"配置加载失败"返回 1 + JSON error(render_ctl.cc:75);
        // 其它非零视为硬失败。
        throw new Error(`render-ctl exited ${result.exitCode}: ${result.stderr.trim()}`);
    }
    let raw;
    try {
        raw = JSON.parse(result.stdout);
    }
    catch {
        throw new Error('render-ctl stdout is not valid JSON');
    }
    return parsePlan(raw);
}
/** 把 render-ctl 的 JSON 对象解析为强类型 RenderPlan(校验必要字段)。 */
function parsePlan(raw) {
    if (typeof raw !== 'object' || raw === null) {
        throw new Error('render-ctl plan: expected JSON object');
    }
    const obj = raw;
    const gimbalUavs = asStringArray(obj.gimbal_uavs, 'gimbal_uavs');
    const skipped = asStringArray(obj.skipped ?? [], 'skipped');
    const instancesRaw = Array.isArray(obj.instances) ? obj.instances : [];
    const instances = instancesRaw.map((inst, i) => {
        if (typeof inst !== 'object' || inst === null) {
            throw new Error(`render-ctl plan: instances[${i}] is not an object`);
        }
        const r = inst;
        return {
            rendererId: asString(r.renderer_id, `instances[${i}].renderer_id`),
            gpuIndex: asNumber(r.gpu_index, `instances[${i}].gpu_index`),
            cwd: asString(r.cwd, `instances[${i}].cwd`),
            argv: asStringArray(r.argv, `instances[${i}].argv`),
            env: asStringRecord(r.env, `instances[${i}].env`),
            maxAircraft: asNumber(r.max_aircraft, `instances[${i}].max_aircraft`),
        };
    });
    return { gimbalUavs, instances, skipped, excessUavs: asStringArray(obj.excess_uavs ?? [], 'excess_uavs') };
}
// ── 类型断言辅助 ──────────────────────────────────────────────────────
function asString(v, ctx) {
    if (typeof v !== 'string')
        throw new Error(`render-ctl plan: ${ctx} is not a string`);
    return v;
}
function asNumber(v, ctx) {
    if (typeof v !== 'number' || !Number.isFinite(v)) {
        throw new Error(`render-ctl plan: ${ctx} is not a finite number`);
    }
    return v;
}
function asStringArray(v, ctx) {
    if (!Array.isArray(v))
        throw new Error(`render-ctl plan: ${ctx} is not an array`);
    return v.map((x, i) => {
        if (typeof x !== 'string')
            throw new Error(`render-ctl plan: ${ctx}[${i}] is not a string`);
        return x;
    });
}
function asStringRecord(v, ctx) {
    if (typeof v !== 'object' || v === null) {
        throw new Error(`render-ctl plan: ${ctx} is not an object`);
    }
    const out = {};
    for (const [k, val] of Object.entries(v)) {
        out[k] = String(val);
    }
    return out;
}
/** 生产环境 execFile:基于 node:child_process.execFile 的 Promise 包装。 */
function createNodeExecFile() {
    // 延迟 require,使测试环境可不引入 child_process。
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { execFile: cpExecFile } = require('node:child_process');
    return (cmd, args) => new Promise((resolve, reject) => {
        cpExecFile(cmd, args, { maxBuffer: 1 << 20 }, (err, stdout, stderr) => {
            if (err && 'code' in err === false) {
                // spawn 级失败(ENOENT 等):无 exitCode。
                reject(err);
                return;
            }
            const exitCode = err && typeof err.code === 'number'
                ? err.code : 0;
            resolve({ stdout, stderr, exitCode });
        });
    });
}
