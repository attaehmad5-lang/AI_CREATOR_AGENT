import json
from pathlib import Path
from datetime import datetime

BASE = Path('/storage/emulated/0/AI_CREATOR_AGENT')
INPUT = BASE / 'projects/generation/job_dependencies.json'
EXECUTION_REPORT = BASE / 'projects/generation/execution_report.json'
STATE_FILE = BASE / 'projects/generation/generation_state.json'
OUTPUT = BASE / 'projects/generation/dependency_execution_plan.json'

with open(INPUT, 'r', encoding='utf-8') as f:
    data = json.load(f)
with open(EXECUTION_REPORT, 'r', encoding='utf-8') as f:
    execution = json.load(f)
with open(STATE_FILE, 'r', encoding='utf-8') as f:
    state_data = json.load(f)

jobs = data.get('dependencies', [])
report_jobs = {j.get('job_id'): j for j in execution.get('jobs', [])}
state_jobs = {j.get('job_id'): j for j in state_data.get('states', [])}
known_ids = set(report_jobs) | set(state_jobs) | {j.get('job_id') for j in jobs}

SUCCESS_STATES = {'COMPLETED', 'SUCCESS', 'SUCCEEDED'}
execution_plan = []

for job in jobs:
    job_id = job.get('job_id')
    dependencies = job.get('depends_on', [])
    dependency_format_invalid = not isinstance(dependencies, list)
    if dependency_format_invalid:
        dependencies = []
    missing = ['INVALID_DEPENDENCY_FORMAT'] if dependency_format_invalid else [d for d in dependencies if d not in known_ids]
    dependency_details = []
    all_complete = True

    for dep in dependencies:
        report_state = report_jobs.get(dep, {}).get('status')
        manager_state = state_jobs.get(dep, {}).get('state')
        asset_exists = any((BASE / 'projects/generation').glob(f'{dep}.*'))
        verified = report_state in SUCCESS_STATES and (manager_state in SUCCESS_STATES or asset_exists)
        if not verified:
            all_complete = False
        dependency_details.append({'job_id': dep, 'execution_report_state': report_state, 'generation_state': manager_state, 'verified_complete': verified})

    if missing:
        state = 'DEPENDENCY_INVALID'
    elif not dependencies:
        state = 'READY'
    elif all_complete:
        state = 'READY'
    else:
        state = 'WAITING_FOR_DEPENDENCY'

    execution_plan.append({'job_id': job_id, 'scene': job.get('scene'), 'media_type': job.get('media_type'), 'depends_on': dependencies, 'dependency_count': len(dependencies), 'missing_dependencies': missing, 'dependency_details': dependency_details, 'execution_state': state, 'can_execute': state == 'READY'})

ready = sum(x['can_execute'] for x in execution_plan)
waiting = sum(x['execution_state'] == 'WAITING_FOR_DEPENDENCY' for x in execution_plan)
invalid = sum(x['execution_state'] == 'DEPENDENCY_INVALID' for x in execution_plan)

if invalid:
    status = 'BLOCKED'
elif waiting:
    status = 'DEGRADED'
else:
    status = 'READY'

result = {'executor': 'DEPENDENCY_AWARE_EXECUTOR', 'strategy': 'VERIFIED_DEPENDENCY_GATED_EXECUTION', 'total_jobs': len(execution_plan), 'ready_jobs': ready, 'waiting_jobs': waiting, 'invalid_dependency_jobs': invalid, 'execution_plan': execution_plan, 'status': status, 'created_at': datetime.now().isoformat()}

with open(OUTPUT, 'w', encoding='utf-8') as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print('DEPENDENCY-AWARE EXECUTOR v2.0')
print('Total Jobs:', result['total_jobs'])
print('Ready Jobs:', result['ready_jobs'])
print('Waiting Jobs:', result['waiting_jobs'])
print('Invalid Dependency Jobs:', result['invalid_dependency_jobs'])
print('STATUS:', result['status'])
