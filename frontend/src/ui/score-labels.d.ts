/** 评分维度中文标签 —— ScorePanel 与 ResultModal 共享。
 *
 *  命名遵循《参赛手册》§4.3/§4.4 的评分维度;未识别的 key 显示 key 原文作为回退。
 *  从 score-panel.ts 抽离(原 Spec 025),避免 ResultModal 与 ScorePanel 之间循环依赖
 *  或标签逻辑重复漂移。
 */
/** 中文标签映射(命名遵循《参赛手册》§4.3/§4.4 的评分维度;未识别的 key 显示 key 原文作为回退)。 */
export declare const DIM_LABELS: Record<string, string>;
/**
 * 按 profile 覆盖维度标签。赛题一的 accuracy 是"持续目指精度"(1Hz 软命中
 * 均值), 与赛题二/三的 per-target RMSE 语义不同, 故赛题一专用更精确的标签。
 */
export declare const PROFILE_DIM_OVERRIDES: Record<string, Record<string, string>>;
/** 取某 profile 下某维度 key 的中文标签(先查覆盖, 再查默认, 最后回退 key 原文)。 */
export declare function dimLabel(profile: string | null, key: string): string;
//# sourceMappingURL=score-labels.d.ts.map