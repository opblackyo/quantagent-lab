"""Shared existing pilot accounting on an already-owned SQLite transaction.

These helpers neither admit a study nor initialize a schema. Callers must obtain
their authority/caps from durable admission, never from a browser request.
"""
import json
import math
from decimal import Decimal

from .agent_contracts import AgentFailure


def create_schema(conn):
    """Explicit initialization only; shared with legacy pilot initialization."""
    conn.execute('CREATE TABLE IF NOT EXISTS pilot_budgets (pilot_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS pilot_attempts (call_id TEXT PRIMARY KEY, pilot_id TEXT NOT NULL, reserved TEXT NOT NULL, usage_estimate TEXT, status TEXT NOT NULL)')
    if 'actual_charge' not in {r[1] for r in conn.execute('PRAGMA table_info(pilot_attempts)')}:
        conn.execute('ALTER TABLE pilot_attempts ADD COLUMN actual_charge TEXT')


def rows(conn, namespace):
    columns={r[1] for r in conn.execute('PRAGMA table_info(pilot_attempts)')}
    if not {'call_id','pilot_id','reserved','usage_estimate','status'}<=columns:
        raise ValueError('pilot attempt storage is unavailable')
    # Old schemas have no actual-charge evidence. Project NULL, never ALTER on read.
    projection='*' if 'actual_charge' in columns else '*,NULL AS actual_charge'
    return conn.execute('SELECT '+projection+' FROM pilot_attempts WHERE pilot_id=?',(namespace,)).fetchall()


def charge(row):
    if row['status'] == 'usage_estimated_held':
        return Decimal(row['reserved'])
    return Decimal(row['actual_charge'] if row['actual_charge'] is not None else row['usage_estimate'] if row['usage_estimate'] is not None else row['reserved'])


def aggregate(conn, namespace):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_usage_reconciliations'").fetchone():
        return None
    row=conn.execute('SELECT receipt_json FROM pilot_usage_reconciliations WHERE pilot_id=?',(namespace,)).fetchone()
    return json.loads(row[0]) if row else None


def total(conn, namespace, attempts, replacement=None):
    charges={r['call_id']:charge(r) for r in attempts}
    if replacement:
        call_id,amount=replacement
        if call_id in charges and amount is not None:
            charges[call_id]=Decimal(str(amount))
    receipt=aggregate(conn,namespace)
    if receipt:
        covered=set(receipt['covered_call_ids'])
        return (max(sum((v for k,v in charges.items() if k in covered),Decimal(0)),
                    Decimal(receipt['actual_charge_usd']))+
                sum((v for k,v in charges.items() if k not in covered),Decimal(0)))
    return sum(charges.values(),Decimal(0))


def reserve(conn, namespace, revision_hash, limit, max_calls, call_id, amount):
    if not conn.in_transaction:
        raise ValueError('explicit reservation transaction required')
    if not math.isfinite(amount) or amount < 0:
        raise ValueError('invalid reservation')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_revisions'").fetchone():
        active=conn.execute('SELECT revision_hash FROM pilot_revisions WHERE pilot_id=?',(namespace,)).fetchone()
        if active and active[0]!=revision_hash:
            raise ValueError('superseded pilot pricing cannot reserve another call')
    attempts=rows(conn,namespace)
    if len(attempts)>=max_calls or total(conn,namespace,attempts)+Decimal(str(amount))>limit:
        raise AgentFailure('PILOT_BUDGET_EXHAUSTED')
    conn.execute('INSERT INTO pilot_attempts(call_id,pilot_id,reserved,usage_estimate,status) VALUES (?,?,?,NULL,?)',
                 (call_id,namespace,str(amount),'unknown_reserved'))


def settle_estimate(conn, namespace, call_id, amount):
    """Record a token estimate while retaining the complete unverified liability."""
    if not conn.in_transaction:
        raise ValueError('explicit settlement transaction required')
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    if not amount.is_finite() or amount < 0:
        raise ValueError('invalid usage estimate')
    row = conn.execute('SELECT reserved,usage_estimate,status,actual_charge FROM pilot_attempts WHERE call_id=? AND pilot_id=?', (call_id, namespace)).fetchone()
    if row is None:
        raise ValueError('unknown reservation')
    if row['actual_charge'] is not None:
        raise ValueError('actual charge is not allocated per call')
    if row['status'] in ('usage_estimated', 'usage_estimated_held'):
        if Decimal(row['usage_estimate']) != amount:
            raise ValueError('usage estimate is immutable')
        return
    if row['status'] != 'unknown_reserved':
        raise ValueError('attempt is not an open reservation')
    if amount > Decimal(row['reserved']):
        raise ValueError('usage exceeds reservation')
    receipt = aggregate(conn, namespace)
    if receipt and call_id in set(receipt.get('covered_call_ids') or []):
        raise ValueError('covered attempt cannot be resettled')
    updated = conn.execute("UPDATE pilot_attempts SET usage_estimate=?, status='usage_estimated_held' WHERE call_id=? AND pilot_id=? AND status='unknown_reserved' AND actual_charge IS NULL",
                           (str(amount), call_id, namespace)).rowcount
    if updated != 1:
        raise ValueError('reservation disappeared')


def summary(conn, namespace, limit, max_calls):
    attempts=rows(conn,namespace)
    receipt=aggregate(conn,namespace)
    charged=total(conn,namespace,attempts)
    estimated=sum((Decimal(r['usage_estimate']) for r in attempts if r['usage_estimate'] is not None),Decimal(0))
    unknown=sum((Decimal(r['reserved']) for r in attempts if r['actual_charge'] is None and (r['usage_estimate'] is None or r['status']=='usage_estimated_held')),Decimal(0))
    actual=sum((Decimal(r['actual_charge']) for r in attempts if r['actual_charge'] is not None),Decimal(0))
    if receipt:
        covered=set(receipt['covered_call_ids'])
        actual=Decimal(receipt['actual_charge_usd'])+sum((Decimal(r['actual_charge']) for r in attempts if r['actual_charge'] is not None and r['call_id'] not in covered),Decimal(0))
    return {'attempts':len(attempts),'max_calls':max_calls,'limit_usd':float(limit),
            'usage_estimated_usd':float(estimated),'unknown_reserved_usd':float(unknown),
            'charged_or_reserved_usd':float(charged),'confirmed_actual_usd':float(actual),
            'actual_charge_scope':'key-usage-aggregate-with-matching-calls' if receipt else 'individually-reconciled-calls',
            'invoiced_actual_usd':None}
