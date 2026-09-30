# 隐翅虫暴露事件观察 · 公共卫生事件监测后端

多机构（医院 / 学校 / 社区）脱敏上报暴露记录，系统在**不作医学诊断**的前提下完成：
信息分层核实 → 跨机构去重 → 阈值生成待研判事件 → 区域科学处置材料发布与留痕 → 值班员研判视图。

## 运行测试

```bash
python3 -m unittest discover -s tests   # 或 pytest
```

## 启动 HTTP 服务（仅标准库，无第三方依赖）

```bash
python3 -m surveillance.api   # 默认 127.0.0.1:8080
```

请求头 `X-Actor-Id` / `X-Actor-Role` 标识操作者（reporter / duty_officer / handler / verifier / publisher / analyst）。

## 核心设计

| 关注点 | 实现 |
| --- | --- |
| 脱敏身份 | `crypto.pseudonymize`（HMAC 令牌），后端只存令牌；含原始身份字段的上报直接拒收 |
| 记录内容 | 时间、地点、接触方式、皮损范围、就医去向（`models.Report`） |
| 信息分层 | 自述 / 医护观察 / 已核实事实（`InfoLayer`）；仅线上照片、民间"牙膏止痒"说法**不能**核实为事实，偏方自动登记为待更正线索 |
| 跨机构去重 | 同一脱敏令牌在情节窗口内合并为个案（`dedup.py`），各自来源永久保留；夜间集中上报按批次幂等 |
| 儿童保护 | 儿童个案明细仅"实际处置者"可见，其余角色（含值班员）只见结构化汇总；查看留审计 |
| 阈值事件 | 空间（已知场所/网格）+ 时间窗 + 共同活动 + 最少个案数 + 最少机构数均可配（`ThresholdConfig`）；达标生成**待研判事件**，措辞只含"疑似/待核实" |
| 发布管理 | 版本 + 有效期；证据不足可降级/撤回；送达台账只增不删，旧提醒永久留痕；内容校验强制"就医建议不可替代"、禁确诊措辞、偏方不得作为建议 |
| 公开入口 | 公告栏/通知群/公众号按系列记录展示版本；更正发布后逐入口核对"是否全部已更新" |
| 值班员视图 | `open_alert`：事件由哪些独立报告构成、哪些事实尚待核实、各入口更新状态一览 |

## 主要接口

```
POST /reports                       单条上报（幂等）
POST /batches                       夜间集中上报（批次幂等）
POST /reports/{id}/statements/{sid}/verify   核实为事实（仅核实员）
POST /detection/run                 执行一次阈值检测
GET  /events/{id}/alert             值班员预警视图
POST /events/{id}/material          起草区域处置材料
POST /publications/{id}/publish     发布（含内容校验）
POST /series/{id}/downgrade         降级（证据不足）
POST /series/{id}/retract           撤回（入口收通知，送达留痕）
POST /corrections                   错误科普更正
GET  /series/{id}/dissemination     各公开入口更新状态
POST /seed/import                   导入既有地点与暴露记录
```

## 目录

```
surveillance/
  models.py        领域模型（报告/个案/事件/发布/入口/台账/审计）
  serde.py         dataclass <-> JSON 序列化
  crypto.py        脱敏令牌
  store.py         内存仓库 + JSON 快照（原子写）
  access.py        角色与儿童个案脱敏视图
  dedup.py         跨机构去重合并
  detection.py     阈值事件检测
  publications.py  发布生命周期与送达留痕
  service.py       应用服务门面
  api.py           stdlib HTTP 接口
tests/             51 个用例，含"入秋同一夜"端到端场景
fixtures/seed.json 既有地点与暴露记录样例
```
