"""Read-only schema verifier for an independently executed review ticket."""
import argparse
import json
from pathlib import Path
import hashlib
import sys


def command(submission):
    source_digest = hashlib.sha256(json.dumps(submission.get('source'), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return [sys.executable, str(Path(__file__).resolve()), '{artifact}',
            '--artifact-digest', submission['artifact_digest'], '--source-digest', source_digest]


def review(path, artifact_digest, source_digest):
    import hashlib
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    data = json.loads(Path(path).read_text())
    if (data.get('artifact_digest') != artifact_digest or digest(data.get('source')) != source_digest
            or data.get('verdict') not in ('pass', 'fail') or not isinstance(data.get('findings'), list)):
        raise ValueError('review must identify the exact current artifact and source')
    blocking = [x for x in data['findings'] if x.get('blocking') is True]
    if (data['verdict'] == 'pass' and blocking) or (data['verdict'] == 'fail' and not blocking):
        raise ValueError('review verdict and blocking findings disagree')
    for finding in blocking:
        if not all(finding.get(key) for key in ('problem', 'evidence', 'impact')):
            raise ValueError('blocking review findings require problem/evidence/impact')
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('artifact')
    parser.add_argument('--artifact-digest', required=True)
    parser.add_argument('--source-digest', required=True)
    args = parser.parse_args()
    review(args.artifact, args.artifact_digest, args.source_digest)


if __name__ == '__main__':
    main()
