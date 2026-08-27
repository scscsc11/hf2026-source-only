# 参赛者算法目录

本目录用于放置参赛者自己开发的算法。平台按赛题分子目录扫描，自动发现可用算法并显示在前端右下角「赛题与算法」面板的**参赛者算法**组里。

## 目录结构

按赛题 id 建子目录，每个 `.py` 文件是一个算法：

```
competition/user_algorithms/
├── search_track/          # 单目标追踪
│   ├── my_fast_agent.py
│   └── another_agent.py
├── coop_decoy/            # 协同诱饵识别
│   └── my_coop_agent.py
└── adversarial_swarm/     # 对抗集群搜索
    └── my_swarm_agent.py
```

## 规则

1. **一个文件 = 一个算法**。每个 `.py` 文件里定义一个继承该赛题 Agent 基类的类，并 **import 该基类**：
   ```python
   from competition.sdk.scenarios.search_track.agent import SearchTrackAgent

   class MyAgent(SearchTrackAgent):
       ...
   ```
   - `search_track` → 继承 `SearchTrackAgent`（或 `Agent`）
   - `coop_decoy` → 继承 `CoopAgent`（或 `Agent`）
   - `adversarial_swarm` → 继承 `SwarmAgent`（或 `Agent`）

   > 子目录（如 `search_track/`）已含 `__init__.py`，可直接放文件；新增赛题子目录时记得也放一个空 `__init__.py`。

2. **入口类自动识别**：平台用 AST 静态分析找出文件里第一个继承赛题基类的类作为入口类（按源码顺序，深度优先），无需手动声明。若一个文件里有多个匹配类，取源码顺序的第一个。

3. **展示名取自 docstring**：建议给入口类写 docstring，首行会作为算法的展示名，其余行作为描述：
   ```python
   class MyFastAgent(SearchTrackAgent):
       """我的快速追踪算法

       使用改进的 EMA 滤波与环绕控制律。"""
       ...
   ```

4. **文件名即模块名**：`my_fast_agent.py` → 模块路径 `user_algorithms.search_track.my_fast_agent:MyFastAgent`。文件名须是合法 Python 标识符（字母/数字/下划线，不以数字开头）。

5. **纯静态分析**：平台只读文件 AST，**不 import、不执行**你的算法代码——所以顶层有副作用的代码不会在扫描时触发。算法只在真正「开始仿真」时才被 competition 引擎加载执行。

## 使用流程

1. 把你的 `.py` 算法文件放进对应赛题子目录。
2. 在前端右下角「赛题与算法」面板展开该赛题，点右上角 **↻ 刷新** 按钮。
3. 你的算法出现在「参赛者算法」组，选中后点「开始仿真」。

> 也可使用「自定义路径」选项直接填 `module:Class` 调试，无需放文件。

## 赛题基类参考

各赛题 Agent 基类定义见 `competition/sdk/scenarios/<赛题id>/agent.py`：

| 赛题 | 基类 | 示例算法 |
|---|---|---|
| `adversarial_swarm` | `SwarmAgent` | `competition/baselines/swarm_distributed.py` |
| `coop_decoy` | `CoopAgent` | `competition/baselines/coop_distributed.py` |
| `search_track` | `SearchTrackAgent` | `competition/baselines/search_track_fsm.py` |

参考这些 baseline 实现来编写你自己的算法。
