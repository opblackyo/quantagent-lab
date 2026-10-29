"""Freeze/execute a private real-data pilot; dry-run is the default, never billable."""
import argparse
import getpass
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))

from app.data import DatasetRegistry
from app.pilot import PilotConfig, execute_pilot, make_manifest
from app.storage import Store


def private_path(path):
    path = path.resolve()
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        raise ValueError('pilot artifacts/configuration must stay in an ignored project-local location') from None
    tracked = subprocess.check_output(['git','ls-files','--',relative.as_posix()],cwd=ROOT)
    ignored = subprocess.run(['git','check-ignore','-q','--',relative.as_posix()],cwd=ROOT).returncode == 0
    if tracked or not ignored:
        raise ValueError('refusing a tracked or non-ignored pilot artifact/configuration destination')
    return path


def load_key(path, provider='openai'):
    """Read only a deliberately named backend key file; never source shell code."""
    text = private_path(path).read_text(encoding='utf-8-sig').strip()
    lines = [s.strip() for s in text.splitlines() if s.strip() and not s.lstrip().startswith('#')]
    key_env = 'KUAIAPI_API_KEY' if provider=='kuaiapi' else 'OPENAI_API_KEY'
    if len(lines) != 1 or not lines[0].startswith(key_env+'='):
        raise ValueError('key file does not match the explicitly selected provider')
    key = lines[0].split('=',1)[1].strip()
    if not key or any(c.isspace() for c in key) or key[0] in ('\"', "'"):
        raise ValueError('private key file requires an unquoted nonempty API key')
    os.environ[key_env] = key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'artifacts/pilot/config.json')
    parser.add_argument('--manifest',type=Path,default=ROOT/'artifacts/pilot/dry-run.json')
    parser.add_argument('--database',type=Path,default=ROOT/'artifacts/pilot/research.sqlite3')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/pilot/report.json')
    parser.add_argument('--key-file',type=Path,help='Explicit ignored backend key file; used only with --execute')
    parser.add_argument('--provider',choices=['openai','kuaiapi'],help='Provider for --init/--set-key; must match config when executing')
    parser.add_argument('--probe-reconciliation',type=Path,help='Explicit ignored key-free first-call billing/group reconciliation JSON')
    parser.add_argument('--continuation-anchor',type=Path,help='Original frozen manifest; reuse its pilot/budget through a registered revision')
    parser.add_argument('--usage-evidence',type=Path,help='Ignored sanitized readonly usage/billing evidence; never credentials')
    parser.add_argument('--probe-window',type=Path,help='Ignored original probe execution timestamps/source commit')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--init',action='store_true',help='Write a private unconfigured template; no spending defaults')
    action.add_argument('--set-key',action='store_true',help='Masked terminal entry into ignored .env.pilot; never use chat')
    action.add_argument('--execute',action='store_true',help='Execute the previously frozen complete manifest')
    action.add_argument('--baselines',action='store_true',help='Run deterministic baselines and verify their Replay only')
    action.add_argument('--record-billing',action='store_true',help='Offline singleton charge reconciliation only; cannot infer or verify group')
    action.add_argument('--migrate-pricing',action='store_true',help='Register a reviewed price/source revision in the original budget; zero calls')
    args = parser.parse_args()
    config_path = private_path(args.config)
    if args.set_key:
        provider = args.provider or 'openai'
        key_path = private_path(ROOT/('.env.pilot.kuaiapi' if provider=='kuaiapi' else '.env.pilot'))
        if key_path.exists():
            raise ValueError('private key file already exists; edit it privately instead of overwriting')
        with warnings.catch_warnings():
            # getpass otherwise falls back to echoing stdin in a non-TTY.
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass(provider+' application API key (hidden, never a subscription credential): ')
        if not value or any(c.isspace() for c in value):
            raise ValueError('key must be nonempty without whitespace')
        with key_path.open('x',encoding='utf-8') as stream:
            stream.write(('KUAIAPI_API_KEY' if provider=='kuaiapi' else 'OPENAI_API_KEY')+'='+value+'\n')
        print('Saved ignored backend key file; no API request sent.')
        return
    if args.init:
        config_path.parent.mkdir(parents=True,exist_ok=True)
        with config_path.open('x',encoding='utf-8') as stream:
            provider = args.provider or 'openai'
            stream.write(PilotConfig(provider=provider,pricing_unit='balance_units_per_million' if provider=='kuaiapi' else 'usd_per_million').model_dump_json(indent=2)+'\n')
        print('Created private configuration template; model, prices and total budget require explicit selection.')
        return
    if args.record_billing:
        if not args.usage_evidence or not args.probe_window or args.key_file:
            raise ValueError('readonly billing evidence/window required; key loading is forbidden for this offline operation')
        from app.pilot_billing import checked_manifest,record_readonly_charge
        anchor=json.loads(private_path(args.manifest).read_bytes());checked_manifest(anchor)
        store=Store(private_path(args.database));store.initialize()
        try:
            receipt=record_readonly_charge(store,anchor['manifest_hash'],private_path(args.usage_evidence).read_bytes(),private_path(args.probe_window).read_bytes())
            output=private_path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'status':'CHARGE_RECORDED_GROUP_UNVERIFIED','actual_charge_usd':receipt['actual_charge_usd'],'provider_calls':0}))
        finally:
            store.close()
        return
    # Pydantic's normal validation error may echo offending values (including a
    # misplaced key). Keep all configuration errors category-only at the CLI.
    config = PilotConfig.model_validate_json(config_path.read_bytes())
    if args.provider and args.provider != config.provider:
        raise ValueError('command provider differs from immutable configuration')
    dataset = DatasetRegistry().load(config.dataset_id)
    manifest = make_manifest(config,dataset)
    manifest_path = private_path(args.manifest)
    if args.execute or args.migrate_pricing:
        if args.execute and config.missing():
            raise ValueError('model, current prices, price evidence and total budget must be explicit')
        if manifest['code_dirty'] or not manifest['code_commit']:
            raise ValueError('commit the reviewed local source before freezing and executing a billable pilot')
        frozen = json.loads(manifest_path.read_text(encoding='utf-8'))
        if frozen != manifest:
            raise ValueError('frozen manifest differs from current data/configuration/prompts/source; dry-run again')
        if args.execute and args.key_file:
            load_key(args.key_file,config.provider)
    elif not args.baselines:
        if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
            raise ValueError('manifest already exists with different settings; choose a new --manifest filename')
        manifest_path.parent.mkdir(parents=True,exist_ok=True)
        manifest_path.write_text(json.dumps(manifest,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        print(json.dumps({'manifest_hash':manifest['manifest_hash'],'sessions':manifest['sessions'],
                          'estimate':manifest['estimate'],'missing_configuration':config.missing()},indent=2))
        return
    store = Store(private_path(args.database))
    output_path = private_path(args.output)
    output_path.parent.mkdir(parents=True,exist_ok=True)
    store.initialize()
    try:
        anchor=None
        if args.continuation_anchor:
            from app.pilot_billing import checked_manifest
            anchor=json.loads(private_path(args.continuation_anchor).read_bytes());checked_manifest(anchor)
        if args.migrate_pricing:
            if not anchor or not args.usage_evidence or args.key_file:
                raise ValueError('original anchor and sanitized billing evidence required; no key for offline migration')
            from app.data import sha256
            from app.pilot_billing import register_price_revision
            report=register_price_revision(store,anchor,manifest,sha256(private_path(args.usage_evidence).read_bytes()))
            output_path.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
            print(json.dumps({'status':'REVISION_REGISTERED_NO_INFERENCE','pilot_id':report['pilot_id'],'missing_configuration':config.missing()}))
            return
        reconciliation = None
        if args.probe_reconciliation:
            reconciliation = json.loads(private_path(args.probe_reconciliation).read_text(encoding='utf-8'))
        report = execute_pilot(store,config,dataset,baselines_only=args.baselines,probe_reconciliation=reconciliation,
                               anchor_manifest_hash=anchor['manifest_hash'] if anchor else None)
        output_path.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        print(json.dumps({'status':report['status'],'runs':[{'strategy':r['config']['strategy_id'],'run_id':r['run_id'],
              'decision_origin':r['decision_origin'],'metrics':r['metrics']} for r in report['runs']],
              'budget':report['budget'],'replay_equality':report['replay_equality'],
              'probe_status':(report.get('probe') or {}).get('status')},indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Pilot interrupted; durable call reservations and diagnostic events retained.',file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('Pilot stopped ('+type(exc).__name__+'). Check private configuration, frozen manifest and diagnostic database; no exception values are printed.',file=sys.stderr)
        sys.exit(1)
