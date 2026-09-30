"""存储层：内存仓库 + JSON 快照持久化。

- 送达台账与审计日志只追加、不删除，保证留痕。
- 快照整体读写，写文件采用临时文件 + 原子替换。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import serde
from .models import (
    AuditEntry,
    Batch,
    Case,
    DeliveryRecord,
    Entrance,
    MisinfoLead,
    PendingEvent,
    Publication,
    Report,
)


class Store:
    def __init__(self) -> None:
        self.reports: dict[str, Report] = {}
        self.cases: dict[str, Case] = {}
        self.case_ids_by_subject: dict[str, list[str]] = {}
        self.events: dict[str, PendingEvent] = {}
        self.publications: dict[str, Publication] = {}
        self.deliveries: list[DeliveryRecord] = []
        self.entrances: dict[str, Entrance] = {}
        self.batches: dict[str, Batch] = {}
        self.misinfo_leads: dict[str, MisinfoLead] = {}
        self.audit: list[AuditEntry] = []
        self.places: dict[str, dict[str, Any]] = {}  # 已知地点/风险场所（含区域、特征）
        self._seq = 0

    # ---- 通用 ----------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    def log(self, at: datetime, actor: str, action: str, detail: str) -> None:
        self.audit.append(
            AuditEntry(id=self.next_id("audit"), at=at, actor=actor, action=action, detail=detail)
        )

    # ---- 个案索引 ------------------------------------------------------

    def add_case(self, case: Case) -> None:
        self.cases[case.id] = case
        self.case_ids_by_subject.setdefault(case.subject_token, []).append(case.id)

    def cases_of_subject(self, subject_token: str) -> list[Case]:
        return [self.cases[cid] for cid in self.case_ids_by_subject.get(subject_token, [])]

    # ---- 序列化 --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "reports": [serde.encode(r) for r in self.reports.values()],
            "cases": [serde.encode(c) for c in self.cases.values()],
            "events": [serde.encode(e) for e in self.events.values()],
            "publications": [serde.encode(p) for p in self.publications.values()],
            "deliveries": [serde.encode(d) for d in self.deliveries],
            "entrances": [serde.encode(e) for e in self.entrances.values()],
            "batches": [serde.encode(b) for b in self.batches.values()],
            "misinfo_leads": [serde.encode(m) for m in self.misinfo_leads.values()],
            "audit": [serde.encode(a) for a in self.audit],
            "places": self.places,
            "seq": self._seq,
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> "Store":
        store = cls()
        for raw in data.get("reports", []):
            report = serde.decode(Report, raw)
            store.reports[report.id] = report
        for raw in data.get("cases", []):
            store.add_case(serde.decode(Case, raw))
        for raw in data.get("events", []):
            event = serde.decode(PendingEvent, raw)
            store.events[event.id] = event
        for raw in data.get("publications", []):
            pub = serde.decode(Publication, raw)
            store.publications[pub.id] = pub
        store.deliveries = [serde.decode(DeliveryRecord, d) for d in data.get("deliveries", [])]
        for raw in data.get("entrances", []):
            entrance = serde.decode(Entrance, raw)
            store.entrances[entrance.id] = entrance
        for raw in data.get("batches", []):
            batch = serde.decode(Batch, raw)
            store.batches[batch.id] = batch
        for raw in data.get("misinfo_leads", []):
            lead = serde.decode(MisinfoLead, raw)
            store.misinfo_leads[lead.id] = lead
        store.audit = [serde.decode(AuditEntry, a) for a in data.get("audit", [])]
        store.places = dict(data.get("places", {}))
        store._seq = int(data.get("seq", 0))
        return store

    def save_json(self, path: str | Path) -> None:
        path = Path(path)
        payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @classmethod
    def load_json(cls, path: str | Path) -> "Store":
        return cls.restore(json.loads(Path(path).read_text(encoding="utf-8")))
