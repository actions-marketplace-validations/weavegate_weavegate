"""GitHub Actions attempt-specific provider evidence and interval arithmetic."""
import re
import subprocess

from common import read_json, timestamp, write_json

TASKS = ('go', 'java', 'explore', 'replay', 'rollback', 'death', 'cancellation',
         'accounting', 'upload')


def validate_inventory(inventory):
    if not isinstance(inventory, dict):
        raise ValueError('CI workflow/task/ruleset inventory required')
    repository, revision = inventory.get('repository'), inventory.get('execution_sha')
    if (not isinstance(repository, str) or
            not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)):
        raise ValueError('CI repository must use owner/repo')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('CI execution_sha must be a full lowercase commit SHA')
    tasks = inventory.get('tasks')
    if not isinstance(tasks, dict) or set(tasks) != {'baseline', 'candidate'}:
        raise ValueError('CI task mappings required for both arms')
    for arm, mappings in tasks.items():
        if not isinstance(mappings, dict) or not set(TASKS) <= set(mappings):
            raise ValueError('incomplete frozen logical acceptance bundle: ' + arm)
        for task, mapping in mappings.items():
            if (not isinstance(task, str) or not task.strip() or not isinstance(mapping, dict) or
                    any(not isinstance(mapping.get(key), str) or not mapping[key].strip()
                        for key in ('job', 'step'))):
                raise ValueError('CI tasks require nonempty job/step names: ' + arm)


def snapshot(repository, run_id, attempt, output):
    if type(attempt) is not int or attempt != 1:
        raise ValueError('primary collection accepts original attempt 1 only')
    def api(path):
        try:
            result = subprocess.run(['gh', 'api', path], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            raise ValueError('gh api failed for %s (exit %s)' % (path, error.returncode)) from None
        import json
        return json.loads(result.stdout)
    prefix = 'repos/%s/actions/runs/%s/attempts/%s' % (repository, run_id, attempt)
    run = api(prefix)
    pages = []
    page = 1
    while True:
        body = api(prefix + '/jobs?per_page=100&page=%d' % page)
        pages.append(body)
        if len(body['jobs']) < 100:
            break
        page += 1
    write_json(output, {'repository': repository, 'run': run, 'pages': pages})


def workflow_bounds(snapshot):
    """Provider pairing instants, using the same conservative end as the cap.

    Call only after endpoint has validated the complete attempt's telemetry.
    """
    run = snapshot['run']
    ends = [timestamp(j['completed_at']) for page in snapshot['pages'] for j in page['jobs']
            if j.get('started_at') is not None and j['conclusion'] != 'skipped']
    return timestamp(run['created_at']), max([timestamp(run['updated_at'])] + ends)


def endpoint(snapshot, inventory):
    run = snapshot['run']
    if type(run.get('id')) is not int or run['id'] <= 0:
        raise ValueError('CI run ID must be a positive integer')
    if (run.get('status') != 'completed' or type(run.get('run_attempt')) is not int
            or run['run_attempt'] != 1 or run['head_sha'] != inventory['execution_sha'] or
            snapshot['repository'] != inventory['repository']):
        raise ValueError('CI attempt/repository/revision mismatch')
    pages = snapshot['pages']
    if any(type(p.get('total_count')) is not int or p['total_count'] < 0 for p in pages):
        raise ValueError('CI jobs total_count must be a nonnegative integer on every page')
    if not pages or any(p['total_count'] != pages[0]['total_count'] for p in pages):
        raise ValueError('inconsistent jobs pages')
    jobs = [job for page in pages for job in page['jobs']]
    if any(type(j.get(key)) is not int or j[key] <= 0 for j in jobs for key in ('id', 'run_id')):
        raise ValueError('CI job and run IDs must be positive integers')
    if len(jobs) != pages[0]['total_count'] or len({j['id'] for j in jobs}) != len(jobs):
        raise ValueError('missing or duplicated jobs pages')
    if any(j['run_id'] != run['id'] or type(j.get('run_attempt')) is not int or j['run_attempt'] != 1 or
           j['head_sha'] != inventory['execution_sha'] for j in jobs):
        raise ValueError('jobs belong to another run/attempt/revision')
    if not set(TASKS) <= set(inventory['tasks']):
        raise ValueError('incomplete frozen logical acceptance bundle')
    completed = []
    for task, mapping in inventory['tasks'].items():
        found = [j for j in jobs if j['name'] == mapping['job']]
        if len(found) != 1 or found[0]['conclusion'] != 'success':
            raise ValueError('required logical job incomplete: ' + task)
        job = found[0]
        if not job.get('started_at'):
            raise ValueError('missing required job start: ' + task)
        if not job.get('completed_at'):
            raise ValueError('missing required job completion: ' + task)
        steps = [s for s in job['steps'] if s['name'] == mapping['step']]
        if len(steps) != 1 or steps[0]['conclusion'] != 'success' or not steps[0].get('completed_at'):
            raise ValueError('required logical step incomplete: ' + task)
        completed.append(timestamp(job['completed_at']))
    intervals = []
    for job in jobs:
        # A skipped job never acquired a runner, even if the API supplies timestamps.
        if job['conclusion'] == 'skipped':
            continue
        if job.get('started_at') is None:
            continue
        if type(job.get('runner_id')) is not int or job['runner_id'] <= 0:
            raise ValueError('missing/invalid started-job runner identity')
        if not job.get('completed_at'):
            raise ValueError('censored runner use')
        start, end = timestamp(job['started_at']), timestamp(job['completed_at'])
        if end < start:
            raise ValueError('negative provider duration')
        intervals.append((start, end))
    if not intervals:
        raise ValueError('no started jobs')
    first, bundle = min(a for a, _ in intervals), max(completed)
    # Completed Actions runs expose updated_at rather than a workflow completed_at.
    # Retain the acceptance endpoint, but censor using the whole attempt's boundary.
    created, finished = workflow_bounds(snapshot)
    if created > first or bundle < first or (finished - created).total_seconds() > 5400:
        raise ValueError('invalid/capped workflow boundaries')
    events = [(a, 1) for a, b in intervals if b > a] + [(b, -1) for a, b in intervals if b > a]
    active = peak = 0
    for _, delta in sorted(events):  # End (-1) precedes start (+1): half-open intervals.
        active += delta
        peak = max(peak, active)
    return {'validation_seconds': (bundle - first).total_seconds(),
            'queue_inclusive_seconds': (bundle - created).total_seconds(),
            'initial_queue_seconds': (first - created).total_seconds(),
            'runner_minutes': sum((b - a).total_seconds() / 60 for a, b in intervals),
            'peak_runner_concurrency': peak}
