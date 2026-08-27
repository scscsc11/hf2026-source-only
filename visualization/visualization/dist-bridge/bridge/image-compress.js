"use strict";
// 相机帧图像压缩 —— PNG → JPEG,根治 WS 多路推送吞吐瓶颈。
//
// 背景:UE 产的相机帧是未压缩 PNG(1024×768 ≈ 1.36MB/帧)。WS 推送模式下
// 4 路 × 30fps × 1.36MB = 163MB/s,远超本机 WS 实际消化能力 → TCP 反压 →
// 有效帧率从 30fps 塌缩到 ~2fps/路(画面"卡卡的"),严重时 recv 归零("不更新")。
//
// 修复:bridge 推前把 PNG 转 JPEG。航拍照片内容 q=80 约 10× 压缩
// (1.36MB → ~100KB),163MB/s → ~16MB/s,落在管道能力内。sharp(native libvips)
// 单帧 3-5ms,热路径无忧。缓存层仍存原始 PNG,只在 encode 出口压缩一次。
//
// 仅 PNG 转 JPEG;非 PNG(如 UE 未来直接产 JPEG)原样返回。
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.toJpegIfPng = toJpegIfPng;
const sharp_1 = __importDefault(require("sharp"));
/** PNG magic bytes:0x89 0x50 0x4e 0x47。 */
function isPng(buf) {
    return buf.length >= 2 && buf[0] === 0x89 && buf[1] === 0x50;
}
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
async function toJpegIfPng(image, quality = 80) {
    if (!isPng(image))
        return image;
    try {
        return await (0, sharp_1.default)(image).jpeg({ quality }).toBuffer();
    }
    catch {
        // 损坏 PNG / sharp 不支持的变体:回退原图(可能解码端也失败,但至少不阻断推送)。
        return image;
    }
}
