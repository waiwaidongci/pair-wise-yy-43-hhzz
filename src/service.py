from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_coordinate, require_number,
                     require_text, require_time)
from .repository import Repository
from .rules import (AUDIT_ROLES, BOOM_DEPLOY_ROLES, BOOM_ENTITY,
                    BOOM_JOIN_COMPLETE, BOOM_JOIN_PENDING, BOOM_RECOVER_ROLES,
                    CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE, VIEW_ROLES,
                    boom_loss, boom_should_scrap, completion_blockers,
                    endpoint_gap_m, escalation_required, joint_status,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_deployment_window, validate_recovery_length,
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

    def deploy_boom(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BOOM_DEPLOY_ROLES)
        actor = require_text(actor, "actor", 100)
        segment_no = require_text(payload.get("segment_no"), "segment_no", 50)
        vessel = require_text(payload.get("vessel"), "vessel", 100)
        start_time = require_time(payload.get("start_time"), "start_time")
        end_time = require_time(payload.get("end_time"), "end_time")
        validate_deployment_window(start_time, end_time)
        length = require_number(payload.get("length"), "length", 0.000001)
        start_x = require_coordinate(payload.get("start_x"), "start_x")
        start_y = require_coordinate(payload.get("start_y"), "start_y")
        end_x = require_coordinate(payload.get("end_x"), "end_x")
        end_y = require_coordinate(payload.get("end_y"), "end_y")
        segment = self.repository.get_boom_segment(segment_no)
        if segment is not None and segment["status"] == "scrapped":
            raise ConflictError(f"段号{segment_no}已报废，不能再布设")
        occupant = self.repository.active_boom_deployment(segment_no)
        if occupant is not None:
            raise ConflictError(
                f"段号{segment_no}已有未撤收布设：事件#{occupant['id']}"
                f"（布设船{occupant['vessel']}，{occupant['start_time']}起）")
        self.repository.ensure_boom_segment(segment_no)
        previous = self.repository.previous_active_deployment(start_time)
        join_status = BOOM_JOIN_COMPLETE
        gap = None
        if previous is not None:
            gap = endpoint_gap_m((previous["end_x"], previous["end_y"]), (start_x, start_y))
            join_status = joint_status(gap)
        deployment = self.repository.create_boom_deployment(
            segment_no, vessel, start_time, end_time, length,
            start_x, start_y, end_x, end_y, join_status, actor)
        if previous is not None and join_status == BOOM_JOIN_PENDING:
            self.repository.set_boom_join_status(previous["id"], BOOM_JOIN_PENDING)
        self.repository.append_audit("boom_deploy", BOOM_ENTITY, deployment["id"], actor, {
            "segment_no": segment_no, "vessel": vessel, "length": length,
            "join_status": join_status,
            "gap_to_previous_m": None if gap is None else round(gap, 3),
            "previous_deployment_id": None if previous is None else previous["id"],
        })
        return deployment

    def recover_boom(self, deployment_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, BOOM_RECOVER_ROLES)
        actor = require_text(actor, "actor", 100)
        deployment = self.repository.get_boom_deployment(deployment_id)
        if deployment["status"] != "deployed":
            raise ConflictError("该布设记录已撤收")
        recovered_length = require_number(payload.get("recovered_length"), "recovered_length")
        validate_recovery_length(deployment["length"], recovered_length)
        loss, ratio = boom_loss(deployment["length"], recovered_length)
        updated = self.repository.recover_boom_deployment(
            deployment_id, recovered_length, loss, actor)
        scrapped = boom_should_scrap(deployment["length"], recovered_length)
        if scrapped:
            self.repository.scrap_boom_segment(deployment["segment_no"])
        self.repository.append_audit("boom_recover", BOOM_ENTITY, deployment_id, actor, {
            "segment_no": deployment["segment_no"],
            "recovered_length": recovered_length,
            "loss": loss,
            "loss_ratio": round(ratio, 4),
            "scrapped": scrapped,
        })
        result = dict(updated)
        result["loss_ratio"] = round(ratio, 4)
        result["scrapped"] = scrapped
        return result

    def list_boom_deployments(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in ("deployed", "recovered"):
            raise ValidationError("status必须是deployed或recovered")
        return self.repository.list_boom_deployments(status)

    def boom_summary(self, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.boom_summary()

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
