"""Execution guidance for official commands and authorized team replies."""


def tool_permission_hint(provider):
    if provider != 'codex':
        return ''
    return ('Codex 실행 도구 안내: 아래에 명시된 정확한 공식 수신·제출·분류·완료·반환 CLI 명령은 '
            '로컬 HC socket과 승인된 업무 장부를 확인하므로 workspace-write sandbox에서 막힐 수 있습니다. '
            '현재 도구 정책이 허용하면 이 명령만 exec_command의 sandbox_permissions="require_escalated"로 요청하고, '
            '승인된 티켓의 신원 확인·수신·제출에 한정한다는 justification을 명시하세요. '
            '설정된 자동 승인 검토가 거절하거나 도구 정책이 금지하면 중단하고 실제 이유를 보고하세요. '
            'sandbox·프로필·승인 정책을 바꾸거나 다른 명령의 권한을 넓히지 마세요. '
            '사용자가 승인한 팀 간 회신도 HC socket 접근이 필요합니다. '
            'Operation not permitted로 실패하면 발신 세션의 도구 정책과 기존 전송 결과를 확인하세요. '
            '읽기 전용 hc get은 정확한 대상 조회만, hc send는 승인된 수신 대상과 고정된 회신 본문만 '
            '명령 단위 권한 요청 대상으로 삼으세요. 권한 요청은 전송 승인을 대신하지 않습니다. '
            '시간 초과·결과 불명은 재전송하지 말고 수신 화면·영수증을 먼저 대조하세요. '
            'HC socket 연결 실패를 core 미실행으로 단정해 core를 시작하거나 재시작하지 마세요.\n')


def leader_guidance(role):
    """One compact responsibility contract for actual team leaders."""
    if role != 'leader':
        return ''
    return ('\nTEAM LEADER: Own product decisions, decomposition, scope and result assessment. '
            'For approved implementation, tests and verification, delegate bounded subagents through '
            'the available authorized worker tools after checking account usage, execution capacity and path ownership. '
            'Record worker identity, account observation and scope in the existing ticket/resource ledger; disclose observation gaps. '
            'Parallelize independent slices with one writer per worktree/path; use one coordination mechanism. '
            'Pass only goal, completion checks, approval bounds, relevant paths and pinned source; '
            'request compact findings with artifact paths, source SHA and test outcomes instead of full histories/logs. '
            'Keep independent review assigned to a different qualified owner. '
            'If delegation is unavailable, record the waiting reason and next action rather than silently doing worker work. '
            'Preparation-only contracts remain preparation-only; create no workers until work is explicitly assigned.\n')
