/**
 * 若 image 是 PNG,转 JPEG(quality 默认 80);否则原样返回。
 *
 * quality=80:航拍经验值,1.36MB → ~100KB,肉眼无明显块效应/色偏。
 * 转 JPEG 失败(损坏 PNG)时回退原 buffer —— 推送层宁可推大图也不丢帧。
 *
 * @param image   UE 写入 redis 的原始帧二进制(PNG/JPEG)
 * @param quality JPEG 质量 1-100,默认 80
 * @returns JPEG 二进制(PNG 输入);原 buffer(非 PNG 或转换失败)
 */
export declare function toJpegIfPng(image: Buffer, quality?: number): Promise<Buffer>;
//# sourceMappingURL=image-compress.d.ts.map