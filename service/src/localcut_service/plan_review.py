"""Review written rough-cut plans without constructing or changing a timeline."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from .persistence import ProjectError, _connect, _json
from .planning import collect_evidence, recompute_plan, validate_written_plan


def _evidence(store, plan, *, verify_sources=False, runtime=None):
    return collect_evidence(store, plan['briefId'], plan['assetId'],
        query=plan.get('query', ''), search_mode=plan.get('searchMode', 'exact'),
        verify_sources=verify_sources, runtime=runtime,output_profile=plan.get('outputProfile'),output_index=plan.get('outputIndex',0))


def _freshness(store, plan, runtime=None):
    try:
        evidence = _evidence(store, plan, runtime=runtime)
        if evidence['fingerprint'] != plan.get('evidenceFingerprint'):
            return True, 'The brief, source, transcript, searches, or analysis changed. Generate a fresh written plan.'
        validate_written_plan(plan, evidence)
    except (ValueError, KeyError, OSError) as exc:
        return True, str(exc)
    return False, None


def list_written_plans(store, brief_id=None, runtime=None):
    records = [record for record in store.list_edit_plans(brief_id)
               if record['plan'].get('planType') == 'written-rough-cut']
    for record in records:
        record['stale'], record['staleReason'] = _freshness(store, record['plan'], runtime)
    return records


def _hook(plan, reason):
    selection = next((selection for section in plan['sections'] if section['status'] != 'rejected'
                      for selection in section['selections']), None)
    plan['hook'] = {'candidateId': selection['candidateId'], 'reason': reason} if selection else None


def review_plan(store, plan_id, action, *, section_id=None, candidate_id=None,
                order=None, expected_review_version: int, runtime=None):
    if action not in {'approve', 'reject', 'replace', 'reorder', 'lock', 'unlock'}:
        raise ValueError('Unsupported written-plan review action.')
    if type(expected_review_version) is not int or expected_review_version < 0:
        raise ValueError('Provide the written-plan review version that was reviewed.')
    if not isinstance(plan_id, str) or not plan_id:
        raise ValueError('Select a written plan.')
    with _connect(store.database_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM edit_plans WHERE id=? AND project_id=?',
                                 (plan_id, store.project_id)).fetchone()
        if row is None:
            raise ProjectError('Written plan was not found.')
        plan = json.loads(row['plan_json'])
        if plan.get('planType') != 'written-rough-cut':
            raise ValueError('This review action requires a written rough-cut plan.')
        if plan.get('reviewVersion') != expected_review_version:
            raise ProjectError('The plan changed since it was reviewed. Refresh and retry.')
        evidence = _evidence(store, plan, verify_sources=True, runtime=runtime)
        if evidence['fingerprint'] != plan.get('evidenceFingerprint'):
            raise ProjectError('The planning evidence changed. Generate a fresh written plan before reviewing.')
        validate_written_plan(plan, evidence)
        sections = plan['sections']
        if action == 'reorder':
            if not isinstance(order, list) or any(not isinstance(value, str) for value in order):
                raise ValueError('Provide every section ID once in the desired order.')
            ids = [section['id'] for section in sections]
            if len(order) != len(ids) or len(set(order)) != len(order) or set(order) != set(ids):
                raise ValueError('Reordering must contain every section ID exactly once.')
            if any(section['locked'] and order[index] != section['id'] for index, section in enumerate(sections)):
                raise ProjectError('Unlock the section before moving it.')
            by_id = {section['id']:section for section in sections}
            plan['sections'] = [by_id[value] for value in order]
            _hook(plan, 'User reordered the written plan; the hook references its first active source selection. Review this opening against playback.')
        else:
            section = next((item for item in sections if item['id'] == section_id), None)
            if section is None:
                raise ValueError('Select an existing plan section.')
            if section['locked'] and action in {'reject', 'replace'}:
                raise ProjectError('Unlock the section before rejecting or replacing it.')
            if action in {'lock', 'unlock'}:
                section['locked'] = action == 'lock'
            elif action in {'approve', 'reject'}:
                section['status'] = 'approved' if action == 'approve' else 'rejected'
                _hook(plan, 'User changed a section decision; the hook references the first active source selection. Review this opening against playback.')
            else:
                replacement = next((item for item in section['alternatives'] if item['candidateId'] == candidate_id), None)
                if replacement is None:
                    raise ValueError('Choose an existing validated alternative for this section.')
                old = deepcopy(section['selections'])
                section['selections'] = [deepcopy(replacement)]
                section['alternatives'] = [item for item in section['alternatives'] if item['candidateId'] != candidate_id]
                known = {item['candidateId'] for item in section['alternatives']}
                section['alternatives'].extend(item for item in old if item['candidateId'] not in known)
                section['status'] = 'pending'
                _hook(plan, 'User replaced a source selection; the hook references the first active source selection. Review this opening against playback.')
        plan = recompute_plan(plan)
        plan['reviewVersion'] = expected_review_version + 1
        validate_written_plan(plan, evidence)
        status = 'reviewed' if all(section['status'] != 'pending' for section in plan['sections']) else 'pending'
        connection.execute('UPDATE edit_plans SET plan_json=?,status=? WHERE id=? AND project_id=?',
                           (_json(plan), status, plan_id, store.project_id))
        connection.commit()
    record = next(record for record in store.list_edit_plans() if record['id'] == plan_id)
    record.update(stale=False, staleReason=None)
    return record
