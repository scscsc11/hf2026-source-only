# 实体模型预设目录（config-driven）

## 概述

本目录存放 OpenSim 核心**实体预设**（entity preset）模板。每个文件定义了一种实体类型的完整装配方案：

- 平台组件类型及参数（`platform`）
- 挂载的组件列表（`components`，含类型、启用状态、参数）
- 实体默认参数值（`defaults`）

`EntityFactory::create` 接收场景中的 `EntityConfig.type`（预设标签），查表 `config/models/<type>.json`，展开 `platform` + `components` 装配为一个通用 `Entity`，再用场景级 `params` / `components` 覆盖预设默认值。**新增一种实体类型只需添加一个 `<type>.json`，无需改动任何 C++ 代码**（宪法 Principle II：纯数据驱动）。

## 文件清单

| 预设文件 | 预设标签 (`type`) | 平台组件 | 说明 |
|---------|-------------------|---------|------|
| `uav.json` | `uav` | `uav_platform` | 固定翼无人机 |
| `ground_vehicle.json` | `ground_vehicle` | `ground_vehicle_platform` | 目标/地面车辆 |
| `decoy_vehicle.json` | `decoy_vehicle` | `ground_vehicle_platform` | 诱饵车（同 ground_vehicle，仅 `entity_type` 不同，spec 017） |
| `gimbal.json` | `gimbal` | （无平台，组件型实体） | 双轴云台（spec 012 D-13） |

## 使用方法

在场景配置（`config.json` 的 `entities[]`）中，把实体的 `type` 设为对应的预设标签即可。`EntityFactory` 会自动读取并展开预设：

```json
{
  "entities": [
    {
      "name": "uav1",
      "type": "uav",
      "params": {
        "initial_latitude": 26.995,
        "initial_longitude": 124.995,
        "initial_altitude": 500.0,
        "initial_heading": 0.0
      }
    }
  ]
}
```

场景级 `params` 会覆盖预设 `defaults` 中同名字段；场景级 `components` 会覆盖/追加预设中同名槽位的组件。

> 注：旧的 `FixedWingUAV` / `TargetVehicle` / `DecoyVehicle` 类型标签仍通过 `OSIM_REGISTER_ENTITY` 注册表解析（过渡保留，T4★ 后续阶段移除）。新场景推荐使用预设标签（`uav` / `ground_vehicle` / `decoy_vehicle` / `gimbal`）。

## 配置字段说明

### 平台组件 (`platform`)
- `type`: 平台组件注册名（如 `uav_platform` / `ground_vehicle_platform`，见 `OSIM_REGISTER_PLATFORM_COMPONENT`）
- `params.entity_type`: 实体类型标识字符串（`uav` / `ground_vehicle` / `decoy_vehicle`）
- `params.max_health` / `initial_health`: 生命值

### 组件 (`components`)
每个组件槽位包含：
- `type`: 组件注册名（见 `OSIM_REGISTER_COMPONENT`，如 `KinematicsComponent`、`GimbalTrackingComponent`、`TargetTrajectoryComponent`、`AStarPlannerComponent`、`PathFollowerComponent`、`DualAxisGimbalComponent`、`CommComponent`）
- `enabled`: 是否启用
- `params`: 组件参数对象（各组件参数不同）

### 默认值 (`defaults`)
实体初始化的默认参数，可被场景配置中 `entities[].params` 覆盖。
