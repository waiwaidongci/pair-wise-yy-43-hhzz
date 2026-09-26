from __future__ import annotations
import math
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
BOOM_ENTITY='围油栏布设段'; BOOM_JOIN_COMPLETE='complete'; BOOM_JOIN_PENDING='pending_redeploy'
BOOM_GAP_TOLERANCE_M=5.0; BOOM_LOSS_SCRAP_RATIO=0.2
BOOM_DEPLOY_ROLES=set(['operations','response_commander']); BOOM_RECOVER_ROLES=set(['operations','response_commander'])
def validate_deployment_window(start_time,end_time):
    if end_time<=start_time: raise ValidationError("结束时刻必须晚于开始时刻")
def endpoint_gap_m(a_end,b_start):
    ax,ay=a_end; bx,by=b_start
    return math.hypot(bx-ax,by-ay)
def joint_status(gap_m): return BOOM_JOIN_COMPLETE if gap_m<=BOOM_GAP_TOLERANCE_M else BOOM_JOIN_PENDING
def validate_recovery_length(deployed_length,recovered_length):
    if recovered_length<0: raise ValidationError("回收长度不能为负")
    if recovered_length>deployed_length: raise ValidationError("回收长度不能超过布设长度")
def boom_loss(deployed_length,recovered_length):
    loss=round(deployed_length-recovered_length,6)
    ratio=loss/deployed_length if deployed_length>0 else 0.0
    return loss,ratio
def boom_should_scrap(deployed_length,recovered_length):
    _,ratio=boom_loss(deployed_length,recovered_length)
    return ratio>BOOM_LOSS_SCRAP_RATIO
