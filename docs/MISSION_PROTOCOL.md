# GPT → Laya → 灵弈：任务协议 v2

## 范围和分工

GPT 只提交一个结构化任务计划，选定 `task.family` 和必要的子类型、单位与目标筛选条件。桥接层依据本方实时态势绑定 GUID、坐标及目标，最多生成 16 个合法候选，另加 `hold`。Laya 通过 `POST /v1/systemone` 在候选 ID 中选一个；桥接层按白名单调用灵弈 API。Codex 调用层已实现；也可使用 JSON 文件提供固定计划。

一份计划只含一种任务类型，可在不同决策周期提交新计划。不要把七类任务及全部参数同时交给 Laya 判断。`constraints` 是给模型看的自然语言；需要机器强制执行的约束应写成结构化筛选器、有效期和设置。桥接层只允许本方实际存在的单位和本方可见的敌方目标；默认只选未分配任务的单位。任务执行是多次引擎调用，不能保证原子性。

## GPT → 桥接层计划

参见 [laya_patrol_demo.json](../overlay/LingYi/intelligent/ai/laya_patrol_demo.json)。核心结构：

```json
{
  "version": 2,
  "plan_id": "north_cap",
  "scenario": "想定名",
  "side": "红方",
  "objective": "保护北部空域",
  "constraints": ["不越区追击"],
  "valid_from_sim_seconds": 0,
  "valid_until_sim_seconds": 3600,
  "min_confidence": 0,
  "task": {
    "family": "patrol",
    "subtype": "air",
    "units": {"category": "Aircraft", "aircraft_type": "fighter", "counts": [1, 2], "unassigned_only": true},
    "areas": [{"id": "north", "center": "selected_units", "half_span_deg": 0.15, "offset_deg": [0.2, 0]}],
    "settings": {"one_third_rule": false}
  }
}
```

| `family` | 必需任务字段 | 可选 `subtype` | 灵弈创建接口 |
| --- | --- | --- | --- |
| `patrol` 巡逻 | `areas` | `air`, `anti_ship`, `anti_land`, `anti_mlp`, `anti_submarine`, `sead`, `sea_control`（必填） | `create_patrol_mission` |
| `strike` 打击 | `target` | `air`, `land`, `ship`, `submarine`（必填） | `create_strike_mission` + `strike_add_target` |
| `support` 支援 | `areas` | 无 | `create_support_mission` |
| `ferry` 转场 | `destination` | 无 | `create_ferry_mission` |
| `cargo` 运输 | `areas` | 无 | `create_cargo_mission` |
| `mine` 布雷 | `areas` | 无 | `create_mine_mission` |
| `mine_clear` 扫雷 | `areas` | 无 | `create_mineclear_mission` |

`units` 必须给出 `category`（`Aircraft`、`Ship`、`Submarine`）和 `counts`（1 到 4 个不同的数量，各为 1 到 10）。可选 `aircraft_type`（`AircraftType` 枚举名的小写形式）、`name_contains`、`unassigned_only`、`max_spread_deg`。没有匹配单位就不生成行动。选择一组地理上相近的单位；该选择不保证武器、航程、挂载或载货能力适合任务，这些应在后续能力过滤器中检查。

`areas` 提供 1 到 4 个区域，每个有唯一 `id`。固定区域用 `points: [[纬度, 经度], ...]`，至少三个点；动态区域用 `center: "selected_units"`、`half_span_deg` 及可选 `offset_deg: [纬度偏移, 经度偏移]`。`strike.target` 用 `contact_ids`、`type`（`ContactType` 枚举名的小写形式）、`name_contains` 至少一种方式过滤可见联系人；已知本方或中立联系人被排除，归属未知的联系人默认也被排除。明确需要处理未知归属时可指定 `allow_unknown_side: true`。`ferry.destination` 用 `unit_ids` 或 `name_contains` 限定本方 `Facility`（默认）或 `Ship`。

`settings` 只接受下面列出的键；所有任务可用 `active`、`start_time`、`end_time`，时间格式是 `MM/DD/YYYY HH:MM:SS`。

| 任务 | 其他可用设置 |
| --- | --- |
| 巡逻 | `one_third_rule`, `flight_size`, `check_outside_area` |
| 打击 | `preplanned_only`, `flight_size` |
| 支援 | `one_third_rule`, `flight_size`, `station_count` |
| 转场 | `behavior`（`one_way`、`cycle`、`random`）, `flight_size` |
| 运输 | `unload_mounts: [{"dbid": 65, "count": 5}]` |
| 布雷 | `one_third_rule`, `arming_delay`（天:时:分:秒）, `group_size` |
| 扫雷 | `one_third_rule`, `group_size` |

`flight_size` 和 `group_size` 均用实际数量 `1/2/3/4/6`；桥接层转换成灵弈枚举。设置失败即停止后续分配，并记失败日志。计划有效期按推演秒数判断；超时计划不会提交给 Laya。

### 飞机换装

巡逻、打击、支援和转场飞机任务可加 `task.loadout`：

```json
"loadout": {"role": "land", "allowed_ids": [27107], "allow_change": true}
```

`role` 为 `air/land/ship/submarine/support/any`；打击任务必须与 `subtype` 一致。`allowed_ids` 最多 4 个，只能取本方实时态势中同平台 DBID 飞机已出现的挂载方案。桥接层并不知道机场完整可换装目录、库存或方案的精确目标适配性，因此计划者须审查方案名称和任务角色；引擎仍可能因库存或状态拒绝换装。

双机组已有不同挂载且未显式允许换装时，不生成出动候选。允许换装时，Laya 选择的候选同时确定飞机组和目标挂载。桥接层先从旧任务撤下所选飞机，再对需要改变的飞机调用 `set_loadout`；后续态势确认全组挂载一致、处于停放状态后才创建并分配新任务。换装未完成、目标或目的地消失、单位任务被其他方改动，或等待超过 3600 推演秒，都不会创建新任务。相关日志为 `loadout_requested`、`loadout_ready`、`loadout_failed` 和 `loadout_unverified`。换装与撤任务不是原子操作；失败时飞机可能暂时没有任务，由下轮规划重新分配。

## 桥接层 → Laya 与 Laya → 桥接层

桥接层构造 Laya 的 `state + choice` 请求。`state` 包含 `protocol_version=2`、`plan_id`、阵营、推演秒数、目标、约束、任务类型、选中单位、相关目标及候选参数；`questions.next_action.criteria` 是 `{候选ID: 中文说明}`。候选 ID 形如 `patrol_2_north`；额外的 `hold` 表示不调用引擎。Laya 仅返回已有候选 ID，例如：

```json
{"answers":{"next_action":{"choice":"patrol_2_north","answer_confidence":0.83}}}
```

未知 ID、置信度不足、请求截断、HTTP 错误或 Laya 超时均按本周期 `hold` 处理。模型文字不会被作为 Python、Lua 或任意 API 方法名执行。执行前再次确认单位属于本方态势、目标仍可见、目的地仍属本方；分配逐个检查返回码。部分成功在日志中标出 `partial=true` 和已分配 GUID，下一帧核对已分配单位的任务 ID。

## 运行与验证

只在实际联通测试或推演运行时启动 Laya；阅读代码、编译和单元测试无需启动服务。静态模拟测试命令：

```powershell
cd LingYi/intelligent/ai
& '..\new_env\ray210\python.exe' -m unittest -v test_laya_command_agent.py
```

实际想定运行时，先在仓库根目录启动 `start_laya_local.ps1`，然后运行：

```powershell
& '.\LingYi\intelligent\new_env\ray210\python.exe' '.\LingYi\intelligent\ai\headless_scenario_run.py' --laya-plan '.\LingYi\intelligent\ai\laya_patrol_demo.json' --laya-url 'http://127.0.0.1:18080'
```

日志在 `LingYi/intelligent/ai/headless_runs/<时间>/events.jsonl`。`laya_request`、`laya_choice`、`mission_ready`、`unit_assignment`、`command_applied` 和下一帧 `command_observed.matched=true` 构成一次命令通路的证据。`command_failed` 会说明设置或分配失败。引擎战况另存于 `LingYi/Data/Logs`。

2026-10-21 的 v2 巡逻联通记录在 `headless_runs/20261021_143252/events.jsonl`：Laya 选择 `patrol_2_south`，引擎为两架苏-27P 分配新任务，下一帧 `matched=true`，想定在 31 次推进后结束。其余六类目前由模拟平台测试覆盖，尚无真实想定逐类验证。本想定开局的战斗机已有任务，所以演示计划显式设置 `unassigned_only: false`；一般计划默认只用未分配任务单位。
