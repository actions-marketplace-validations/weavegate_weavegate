#!/usr/bin/env python3
"""Standalone campaign tooling, deliberately separate from weavegate CLI."""
import argparse
import sys
from pathlib import Path

# An archived analyzer must not mutate its input by importing sibling modules.
sys.dont_write_bytecode = True

from analysis import analyze
from ci import snapshot
from collector import collect, freeze
from common import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('freeze', help='validate pins and preassign all slots')
    prepare.add_argument('manifest')
    prepare.add_argument('bundle')
    run = commands.add_parser('collect', help='run one registered block, once, on its UTC date')
    run.add_argument('bundle')
    run.add_argument('--block', required=True)
    run.add_argument('--registration', required=True)
    report = commands.add_parser('analyze', help='rebuild tables from sealed raw evidence')
    report.add_argument('bundle')
    report.add_argument('output')
    check = commands.add_parser('verify', help='verify exact bundle file/hash inventory')
    check.add_argument('bundle')
    ci = commands.add_parser('ci-snapshot', help='retain all provider jobs pages for attempt 1')
    ci.add_argument('--repository', required=True)
    ci.add_argument('--run-id', type=int, required=True)
    ci.add_argument('--attempt', type=int, default=1)
    ci.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'freeze':
            freeze(args.manifest, args.bundle)
        elif args.command == 'collect':
            if not collect(args.bundle, args.block, args.registration):
                print('CAMPAIGN_COLLECTION_RESULT cleanup=unproven allocation=quarantined')
                return 1
        elif args.command == 'analyze':
            result = analyze(args.bundle, args.output)
            print('CAMPAIGN_ANALYSIS_RESULT decision=' + result['decision']['status'])
        elif args.command == 'verify':
            verify(args.bundle)
        elif args.command == 'ci-snapshot':
            snapshot(args.repository, args.run_id, args.attempt, Path(args.output))
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        print('campaign tooling: ' + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('campaign tooling: collection interrupted; retained evidence must be preserved', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
