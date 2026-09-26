from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='溢油应急响应与任务追踪'; ENTITY='溢油事件'; ID_PREFIX='OS'
SEVERITIES=['minor', 'moderate', 'major', 'catastrophic']; STATES=['reported', 'assessing', 'containing', 'recovering', 'monitoring', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['containing'], 'containing': ['recovering'], 'recovering': ['monitoring'], 'monitoring': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['response_commander'], 'containing': ['response_commander'], 'recovering': ['operations'], 'monitoring': ['operations'], 'closed': ['response_commander']}
CREATE_ROLES=set(['observer', 'response_commander']); RECORD_ROLES=set(['response_commander', 'operations']); AUDIT_ROLES=set(['response_commander', 'viewer']); VIEW_ROLES=set(['observer', 'response_commander', 'operations', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'major': 6.0, 'catastrophic': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'major': 8, 'catastrophic': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
BOOM_STATUSES=['deployed','recovered','scrapped']; BOOM_DEPLOY_ROLES=set(['response_commander','operations']); BOOM_RECOVER_ROLES=set(['response_commander','operations']); BOOM_VIEW_ROLES=VIEW_ROLES
MAX_JOINT_GAP_M=5.0; SCRAP_LOSS_RATIO=0.2
def loss_ratio(length_m,recovered_length_m):
    if length_m<=0: raise ValidationError("布设长度必须大于0")
    return max(0.0,(length_m-recovered_length_m)/length_m)
def recovery_status(length_m,recovered_length_m):
    return BOOM_STATUSES[2] if loss_ratio(length_m,recovered_length_m)>SCRAP_LOSS_RATIO else BOOM_STATUSES[1]
def joint_gaps(deployments):
    ordered=sorted(deployments,key=lambda d:(d["start_m"],d["end_m"],d["segment_no"]))
    gaps=[]
    for left,right in zip(ordered,ordered[1:]):
        gap=right["start_m"]-left["end_m"]
        if gap>MAX_JOINT_GAP_M: gaps.append({"left_id":left["id"],"right_id":right["id"],"left_segment":left["segment_no"],"right_segment":right["segment_no"],"gap_m":round(gap,3)})
    return gaps
def needs_redeploy_ids(deployments):
    ids=set()
    for gap in joint_gaps(deployments): ids.add(gap["left_id"]); ids.add(gap["right_id"])
    return ids
def boom_disposition(deployment_id,redeploy_ids): return 'needs_redeploy' if deployment_id in redeploy_ids else 'complete'
