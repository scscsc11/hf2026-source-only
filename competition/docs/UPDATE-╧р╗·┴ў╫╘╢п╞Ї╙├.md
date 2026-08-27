# 相机流自动启用（photo_mode = auto）

> 本说明面向参赛选手。请按本文为准调整代码；旧文档里 "需 `--photo` 启用相机" 的描述已过时。

## 一句话变化

**`obs.self.photo` 现在默认自动可用。** 在带 UE 渲染的标准运行环境里，只要 UE 把某架无人机的相机画面写入 Redis，SDK 会自动把最新一帧注入 `obs.self.photo`，选手**无需加任何开关或参数**。

之前必须显式传 `--photo` 才会拉取相机帧；现在默认就会拉取。

---

## 1. 默认行为（photo_mode = auto）

新的默认值是 `auto`，行为如下：

| 运行方式 | `obs.self.photo` |
|---|---|
| 带 UE 的标准启动（网页点"开始仿真" / 命令行不带额外参数） | UE 写入该机帧 → 自动注入 PNG bytes |
| UE 暂未给该机分配渲染 / Redis 暂无帧 | `None`（正常降级，不报错） |
| `dry_run` 模式 | `None`（dry_run 无 UE，不启动相机拉取） |

> 这意味着：网页上能看到某架无人机的相机画面时，你的算法里 `obs.self.photo` 就能拿到同一帧图片。两边看到的是同一份渲染结果。

---

## 2. 图片编码格式：PNG（重要）

`obs.self.photo` 是 **PNG** 编码的字节流（magic bytes `89 50 4E 47`，约 700KB），**不是 JPEG**。

请按 PNG 解码：

```python
import numpy as np
import cv2

def decide(self, obs, dt):
    if obs.self.photo is None:
        return []  # 本帧无画面
    # cv2.imdecode 配 IMREAD_COLOR 会自动嗅探格式，PNG/JPEG 都能解
    img = cv2.imdecode(np.frombuffer(obs.self.photo, np.uint8), cv2.IMREAD_COLOR)
    # ... 你的视觉推理 ...
```

或用 Pillow：

```python
from PIL import Image
import io

def decide(self, obs, dt):
    if obs.self.photo is None:
        return []
    img = Image.open(io.BytesIO(obs.self.photo))
    # ...
```

> 不要按 JPEG 解析（不要用 `cv2.IMREAD_JPEG`，也不要假定 magic 是 `FF D8 FF`）。

---

## 3. 新的 CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--photo-mode {auto,on,off}` | `auto` | 相机帧拉取模式（见下表） |
| `--photo` | — | （废弃别名）等价于 `--photo-mode on`，保留向后兼容 |
| `--no-photo` | — | 等价于 `--photo-mode off`，显式关闭 |

**三态语义：**

| 模式 | dry_run | 行为 |
|---|---|---|
| `auto`（默认） | True | 不启动相机拉取（无 UE） |
| `auto`（默认） | False | 自动启动；Redis 有帧 → 注入，无帧 → `None` |
| `on` | True | 不启动 |
| `on` | False | 启动（同 auto 非 dry_run） |
| `off` | 任意 | 不启动，`obs.self.photo` 恒 `None` |

典型用法：

```bash
# 默认（auto）：带 UE 的环境自动拿图
python -m competition run --scenario search_track --agent my_pkg:MyAgent --duration 600

# 显式关闭相机（纯状态量算法，省资源）
python -m competition run --scenario search_track --agent my_pkg:MyAgent --no-photo --duration 600

# 旧脚本照常工作（--photo 仍是 on）
python -m competition run --scenario search_track --agent my_pkg:MyAgent --photo --duration 600
```

---

## 4. HTTP API（前端 / 自动化）

`POST /api/sim/start` 新增 `photoMode` 字段：

```json
{
  "scenario": "search_track",
  "agent": "my_pkg:MyAgent",
  "photoMode": "auto"
}
```

- 未传 `photoMode` → 默认 `auto`。
- 旧字段 `photo: true/false` 仍兼容（`true → on`，`false → off`）。
- 同时传时 `photoMode` 优先。

网页前端已默认发送 `photoMode: "auto"`，选手在网页点"开始仿真"即可自动拿到相机画面，无需任何手动操作。

---

## 5. 何时 `obs.self.photo` 为 `None`

以下任一满足即为 `None`（这是正常降级，不是错误）：

1. `dry_run` 模式（无 UE 渲染）。
2. 显式设了 `photo_mode=off`（或旧 `photo=false`）。
3. UE 还没给当前这架无人机分配渲染（多机渲染门控：UE 一次只 assign 一架出图）。
4. Redis 里暂时还没有该机的帧（刚启动、帧还没产生）。

> 多机赛题（赛题二 3 机 / 赛题三 10 机）受 UE 单架 assign 与带宽约束，未渲染的无人机会拿到 `None`。做视觉推理时务必先判 `if obs.self.photo is None`。

---

## 6. 代码示例

### 自研感知（覆盖 `sensor()`）

```python
from competition.sdk.core.agent import Agent
from competition.sdk.core.observation import Detection

class MyAgent(Agent):
    def sensor(self, obs, dt):
        photo = obs.self.photo   # PNG bytes（默认 auto 自动注入）
        if photo is None:
            return None          # 无帧 → 回退默认识别器
        boxes = self.my_model.detect(photo)
        return [Detection(detected=True, confidence=b.conf,
                          target_lat=b.lat, target_lon=b.lon) for b in boxes]
```

### 端到端（直接在 `decide()` 用图）

```python
class MyAgent(Agent):
    def sensor(self, obs, dt):
        return SKIP_DETECTION    # 端到端：不跑默认识别器

    def decide(self, obs, dt):
        photo = obs.self.photo   # PNG bytes
        if photo is None:
            return []
        action = self._policy(photo)
        return [action.to_command()]
```

---

## 7. 向后兼容

- 旧的 `--photo` 命令行参数：仍有效，等价 `--photo-mode on`。
- 旧的 `run(photo_enabled=True)` Python 调用：仍有效，映射为 `photo_mode=on`。
- 旧的 HTTP `photo: true/false`：仍兼容。
- **唯一的语义变化**：以前不传任何 photo 参数 = 关闭；现在不传 = `auto`（自动开启）。如果你之前的算法不期望收到图片（例如纯状态量控制），且不希望有相机拉取开销，请显式加 `--no-photo`。

如有疑问，以本说明和 [`perception-guide.md`](perception-guide.md) 为准。
