# 公共卫生事件监测后端（隐翅虫聚集暴露观察）

医院、学校、社区在同一夜上报相近皮损与河边活动经历时，本系统支撑疾控值班：

- 多机构用**脱敏身份**记录时间、地点、接触方式、皮损范围、共同活动与就医去向；
- 自述、医护观察、已核实事实**三层证据严格分开**，线上照片与民间说法（如"牙膏止痒"）不得当作确诊依据；
- 重复上报合并病例，但每条报告的来源机构与证据层级永久保留；
- **儿童身份信息只向实际处置者开放**，访问与拒绝均审计留痕；
- 空间半径、时间窗、共同活动达到**可配置阈值**时生成"待研判事件"，由人工研判——系统不自动诊断；
- 处置材料带版本与有效期，证据不足可降级或撤回；旧提醒已送达即留痕；
- 错误科普更正必须在**所有曾送达的公开入口**逐一确认更新后才能关闭。

## 技术与运行

仅依赖 Python 3.12 标准库（WSGI + SQLite），测试依赖固定在 `requirements.txt`。

```bash
pip install -r requirements.txt
python3 -m pytest                       # 25 个端到端用例
python3 -m unittest discover -s tests   # 原种子结构检查同样通过
MONITOR_DB=monitor.db python3 -m monitor.http   # 启动 HTTP 服务，默认 127.0.0.1:8080
```

认证采用请求头 `X-User-Id`（演示用，种子账号见 `monitor/db.py`）：
`u-reporter-hosp`/`u-doctor`（医院）、`u-reporter-school`（学校校医）、
`u-reporter-comm`（社区）、`u-dispatch`（值班员）、`u-invest`（流调）、`u-admin`（管理员）。

## 模块划分

| 文件 | 职责 |
| --- | --- |
| `monitor/db.py` | SQLite 建表、种子机构/账号/渠道、阈值配置、错误说法库 |
| `monitor/config.py` | 角色、证据层级、发布状态、默认阈值 |
| `monitor/security.py` | RBAC、处置者名单、儿童身份信息裁剪与审计 |
| `monitor/reports.py` | 脱敏身份哈希、地点复用、单条/夜间批量上报、跨机构去重、证据登记核实 |
| `monitor/events.py` | 时间窗+空间半径+共同活动连通分量扫描，幂等生成待研判事件、事实核查清单、人工研判 |
| `monitor/advisories.py` | 材料版本/有效期/降级/撤回、送达快照、发布守卫、错误科普更正闭环 |
| `monitor/http.py` | 零依赖 WSGI 路由 |

## 一条完整剧情（对应端到端测试）

### 1. 夜间集中上报与跨机构去重

- `POST /api/batches`：一个机构一个批次提交多条记录，逐条接受/拒绝并返回下标；
- 不同机构提交同一脱敏令牌（`identity_token`，后端盐化哈希，不接触明文身份）→ 复用同一病例，但各自产生独立报告（`POST /api/reports`）；
- 无法自动匹配的重复病例可由值班员 `POST /api/cases/merge` 合并，报告来源不丢；
- 地点按归一化名复用（`POST /api/locations`）。

证据三层在 `evidence_items.level`：

1. `self_report`：居民自述/线上照片转述，随上报自动入层，**不是诊断**；
2. `clinician_obs`：仅医护角色可登记；
3. `verified_fact`：仅流调/管理员经核实流程（`POST /api/evidence/{id}/verify`、`POST /api/fact-checks/{id}/resolve`）可升级。

### 2. 儿童信息保护

- 病例列表与事件构成只暴露脱敏化名 `pseudonym`；
- `GET /api/cases/{id}` 默认不含年龄/性别；带 `?identity=1` 时，仅
  `case_handlers` 名单内的实际处置者（接诊医护、到场流调，或经其登记的人员）可读；
- 每次成功查看与每次拒绝都写 `audit_log`。

### 3. 阈值触发与值班员视图

- `POST /api/events/scan`：在配置的 `time_window_hours` 内，用"地点距离 ≤
  spatial_radius_m（无坐标时要求同点）**或**共同活动相同且涉及病例数达标"连边，
  连通分量中独立病例 ≥ `min_cases`、独立来源机构 ≥ `min_sources` 即生成
  `pending_review` 事件；扫描幂等（构成报告集合签名 + 病例集合覆盖去重）。
- `GET /api/events/{id}`：值班员一次打开即可看到
  构成它的每条独立报告（机构、时间、地点、接触方式、皮损范围、各层证据计数）、
  触发维度（time/space/common_activity/sources）与阈值快照、
  以及 `fact_checks` 中**尚待核实**的事实清单。
- `POST /api/events/{id}/review` 只能由值班员/流调人工 `confirmed`/`dismissed`；
  阈值可由管理员 `PUT /api/thresholds` 调整。

### 4. 处置材料发布纪律

- `POST /api/advisories` → `POST /api/advisories/{id}/versions`（草稿，带 `valid_until`）
  → `POST /api/versions/{id}/publish`（生效并作废旧当前版）；
- 发布守卫强制：必须含明确就医指引；禁止"缓解后不用就医"等表述；
  正文引用错误说法时必须同时附上错误说法库中的正确口径原文；
- `POST /api/advisories/{id}/downgrade|withdraw` 以**新版本**降级/撤回，
  旧版本状态与 `deliveries` 送达快照不改写；撤回/降级/过期版本在
  `GET /api/public/advisories` 不可见，过期材料可经 `GET /api/advisories/expired` 续期。

### 5. 错误科普更正闭环

发现"牙膏止痒"等错误说法后（`POST /api/misinfo-claims` 入库）：

1. `POST /api/corrections` 开更正单并发布纠正版（受同一发布守卫约束）；
2. 凡**送达过旧口径的公开入口**自动列入待确认清单（内部渠道不在其列）；
3. 各入口责任人 `POST /api/corrections/{id}/confirm` 逐一确认；
4. 全部确认后更正单才 `resolved`；`GET /api/corrections/{id}` 的
   `all_public_entries_updated` 用来核验"所有公开入口均已更新"。

## 边界与非目标

- 本系统是监测与处置协同工具，不输出疾病诊断结论；
- 演示鉴权（固定用户头）与脱敏令牌约定上线前应替换为机构真实身份体系；
- `fixtures/seed.json` 与 `project_data.load_seed` 保留为领域样例读取入口。
