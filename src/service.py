from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (NotFoundError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text,
                     require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, BOOM_DEPLOY_ROLES, BOOM_RECOVER_ROLES,
                    BOOM_VIEW_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, boom_disposition, completion_blockers,
                    escalation_required, joint_gaps, loss_ratio,
                    needs_redeploy_ids, priority_score, recovery_status,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def deploy_boom(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, BOOM_DEPLOY_ROLES)
        actor = require_text(actor, "actor", 100)
        segment_no = require_text(payload.get("segment_no"), "segment_no", 50)
        boat = require_text(payload.get("boat"), "boat", 100)
        start_time = require_timestamp(payload.get("start_time"), "start_time")
        end_time = require_timestamp(payload.get("end_time"), "end_time")
        if datetime.fromisoformat(end_time) < datetime.fromisoformat(start_time):
            raise ValidationError("end_time不能早于start_time")
        length_m = require_number(payload.get("length_m"), "length_m")
        if length_m <= 0:
            raise ValidationError("length_m必须大于0")
        start_m = require_number(payload.get("start_m"), "start_m")
        end_m = require_number(payload.get("end_m"), "end_m")
        if end_m < start_m:
            raise ValidationError("end_m不能小于start_m")
        deployment = self.repository.create_boom_deployment(
            item_id, segment_no, boat, start_time, end_time, length_m,
            start_m, end_m, actor)
        self.repository.append_audit("boom_deploy", ENTITY, item_id, actor, {
            "deployment_id": deployment["id"], "segment_no": segment_no,
            "boat": boat, "length_m": length_m, "start_m": start_m,
            "end_m": end_m,
        })
        return deployment

    def recover_boom(self, item_id: int, deployment_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BOOM_RECOVER_ROLES)
        actor = require_text(actor, "actor", 100)
        deployment = self.repository.get_boom_deployment(deployment_id)
        if deployment["item_id"] != item_id:
            raise NotFoundError("布设记录不存在")
        recovered_length_m = require_number(
            payload.get("recovered_length_m"), "recovered_length_m")
        if recovered_length_m > deployment["length_m"]:
            raise ValidationError("回收长度不能超过布设长度")
        recovered_at = payload.get("recovered_at")
        if recovered_at is not None:
            recovered_at = require_timestamp(recovered_at, "recovered_at")
        else:
            recovered_at = utc_now()
        status = recovery_status(deployment["length_m"], recovered_length_m)
        updated = self.repository.recover_boom_deployment(
            deployment_id, status, recovered_length_m, recovered_at, actor)
        self.repository.append_audit("boom_recover", ENTITY, item_id, actor, {
            "deployment_id": deployment_id, "segment_no": deployment["segment_no"],
            "recovered_length_m": recovered_length_m,
            "loss_ratio": round(loss_ratio(deployment["length_m"], recovered_length_m), 4),
            "status": status,
        })
        return updated

    def list_boom(self, item_id: int, role: str) -> list:
        ensure_role(role, BOOM_VIEW_ROLES)
        deployments = self.repository.list_boom_deployments(item_id)
        active = [d for d in deployments if d["status"] == "deployed"]
        redeploy_ids = needs_redeploy_ids(active)
        result = []
        for deployment in deployments:
            entry = dict(deployment)
            if deployment["status"] == "deployed":
                entry["disposition"] = boom_disposition(deployment["id"], redeploy_ids)
            else:
                entry["disposition"] = None
            result.append(entry)
        return result

    def boom_summary(self, item_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, BOOM_VIEW_ROLES)
        active = self.repository.list_boom_deployments(item_id, status="deployed")
        gaps = joint_gaps(active)
        redeploy_ids = needs_redeploy_ids(active)
        complete = sorted(d["segment_no"] for d in active if d["id"] not in redeploy_ids)
        pending = sorted(d["segment_no"] for d in active if d["id"] in redeploy_ids)
        return {
            "item_id": item_id,
            "active_count": len(active),
            "complete": complete,
            "complete_count": len(complete),
            "needs_redeploy": pending,
            "needs_redeploy_count": len(pending),
            "gaps": gaps,
        }

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
