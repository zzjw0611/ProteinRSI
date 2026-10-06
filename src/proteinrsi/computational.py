# SPDX-License-Identifier: MIT
"""Iterate on computed evidence without manufacturing observations or lab queries."""
from proteinrsi.audit import snapshot
from proteinrsi.contracts import TaskSpec
from proteinrsi.tasks import validate_candidate


def run_computational(campaign, *, guarded=True):
    task = TaskSpec.model_validate(campaign.state['task'])
    if task.execution_mode != 'computational':
        raise ValueError('This runner is only for explicitly computational objectives')
    if guarded:
        from proteinrsi.replay.broker import GuardedTeam, GuardedMetaAgent
        old = campaign.team
        campaign.team = GuardedTeam.from_team(old)
        campaign.meta_agent = GuardedMetaAgent(campaign.team)
    with campaign.store.lock():
        state = campaign.state
        if state['pending_batch']:
            raise ValueError('Cannot compute through a pending experimental batch')
        while state['round_index'] < task.max_rounds and state['status'] != 'complete':
            campaign.methods.assert_plannable(state)
            view = campaign.view(state)
            snapshot(campaign.store, 'computational_iteration_input', view)
            previous_jobs = set(campaign.store.all('tool_jobs'))
            candidates = campaign.team.run(view)
            for candidate in candidates:
                validate_candidate(task, candidate)
            results = [{'job_ref': key, 'result': record.get('result'), 'state': record['state']}
                for key, record in campaign.store.all('tool_jobs').items() if key not in previous_jobs]
            protocol_results = [r["protocol_result"] for r in campaign.store.all("research_runs").values()
                if r.get("runner") == "resource-protocol-v1" and r.get("round") == state["round_index"]
                and r.get("status") == "complete"]
            summary = {'protocol_results': protocol_results, 'round': state['round_index'], 'kind': 'computational_iteration',
                'candidates': [c.model_dump(mode='json') for c in candidates], 'tool_results': results,
                'charged_experimental_queries': 0, 'experimental_claim': False}
            # Previous computed outputs, errors and candidates become next-round context.
            # They never enter the experimental Observation table or Meta's measured gate.
            state['history'].append(summary)
            state['round_index'] += 1
            state['status'] = 'complete' if state['round_index'] >= task.max_rounds else 'ready'
            campaign.store.put('computational_iterations', str(state['round_index']), summary, immutable=True)
            campaign.store.put('campaign', 'state', state)
            campaign.store.event('computational_iteration_completed', summary)
            snapshot(campaign.store, 'computational_iteration_committed', campaign.view(state))
        campaign.store.event('campaign_completed', {'rounds': state['round_index'], 'mode': 'computational'})
    return campaign.report()
