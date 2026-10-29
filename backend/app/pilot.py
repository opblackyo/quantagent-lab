"""Private, bounded four-strategy pilot. No credentials belong in a manifest."""
import json
import math
import re
import subprocess
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, get_args, get_origin
from uuid import UUID
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from .agents import AgentFailure, INSTRUCTIONS, SCHEMAS, OpenAIProvider
from .data import ROOT, canonical_hash, sha256
from .experiments import run_experiment, schedule
from .features import snapshots
from .models import Contract, ENGINE_VERSION, FEATURE_VERSION, PROMPT_VERSION, ExperimentConfig
from .storage import now
from .http_policy import HTTPTimeoutPolicy,transport_policy
from . import budget_ledger

STRATEGIES = ("buy_hold", "ma_crossover", "single_agent", "multi_agent")


class PilotConfig(Contract):
    timeout_policy: HTTPTimeoutPolicy = Field(default_factory=HTTPTimeoutPolicy)
    provider: Literal['openai', 'kuaiapi'] = 'openai'
    provider_group: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    pricing_unit: Literal['usd_per_million', 'balance_units_per_million', 'runtime_usd_per_million'] = 'usd_per_million'
    runtime_pricing_verified: bool = False
    runtime_rate_multiplier: Annotated[float, Field(strict=True, gt=0)] | None = None
    cache_write_usd_per_million: Annotated[float, Field(strict=True, ge=0)] | None = None
    cache_read_usd_per_million: Annotated[float, Field(strict=True, ge=0)] | None = None
    input_balance_units_per_million: Annotated[float, Field(strict=True, gt=0)] | None = None
    output_balance_units_per_million: Annotated[float, Field(strict=True, gt=0)] | None = None
    cache_write_balance_units_per_million: Annotated[float, Field(strict=True, ge=0)] | None = None
    cache_read_balance_units_per_million: Annotated[float, Field(strict=True, ge=0)] | None = None
    balance_units_per_usd: Annotated[float, Field(strict=True, gt=0)] | None = None
    gateway_routing_acknowledged: bool = False
    price_evidence_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')] | None = None
    dataset_id: str = "spy-yahoo-2018-2025-raw-v1"
    dataset_hash: str = "79fe3c66ee6493b834160f5386be38c5f03823802de27ff8450f772d61878bdb"
    start_date: date = date(2023, 1, 1)
    end_date: date = date(2023, 1, 31)
    model_id: str | None = None
    input_usd_per_million: Annotated[float, Field(strict=True, gt=0)] | None = None
    output_usd_per_million: Annotated[float, Field(strict=True, gt=0)] | None = None
    total_budget_usd: Annotated[float, Field(strict=True, gt=0, le=100)] | None = None
    price_source: str | None = None
    price_verified_on: date | None = None
    max_calls: Annotated[int, Field(strict=True, ge=1, le=64)] = 32
    max_attempts_per_role: Annotated[int, Field(strict=True, ge=1, le=2)] = 2
    max_output_tokens: Annotated[int, Field(strict=True, ge=512, le=8192)] = 2048
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    temperature: Annotated[float, Field(strict=True, ge=0, le=2)] | None = None
    initial_cash: Annotated[float, Field(strict=True, gt=0)] = 100000
    fee_bps: Annotated[float, Field(strict=True, ge=0, le=100)] = 5
    slippage_bps: Annotated[float, Field(strict=True, ge=0, le=100)] = 5

    @model_validator(mode="after")
    def bounded_period_and_model(self):
        if not date(2019, 1, 1) <= self.start_date <= self.end_date < date(2025, 1, 1) or (self.end_date-self.start_date).days > 62:
            raise ValueError("pilot requires a short development/validation period before the 2025 holdout")
        if self.provider == 'openai' and self.model_id and not re.fullmatch(r"[a-zA-Z0-9.-]+-\d{4}-\d{2}-\d{2}", self.model_id):
            raise ValueError("pilot requires an exact dated model snapshot, not a moving alias")
        if self.model_id and not re.fullmatch(r'[a-zA-Z0-9._:/-]{1,120}', self.model_id):
            raise ValueError('invalid provider model label')
        domain = 'https://kuaiapi.net/' if self.provider == 'kuaiapi' else 'https://developers.openai.com/'
        if self.price_source and not self.price_source.startswith(domain):
            raise ValueError("use current official provider price evidence")
        if self.price_source and (urlsplit(self.price_source).query or urlsplit(self.price_source).fragment):
            raise ValueError('price evidence URL must not carry query credentials or fragments')
        if self.provider == 'openai':
            if self.pricing_unit != 'usd_per_million' or any(v is not None for v in (self.provider_group,self.input_balance_units_per_million,self.output_balance_units_per_million,self.balance_units_per_usd,self.cache_write_balance_units_per_million,self.cache_read_balance_units_per_million)):
                raise ValueError('OpenAI direct requires USD prices without gateway configuration')
        elif self.pricing_unit == 'runtime_usd_per_million':
            if any(v is not None for v in (self.input_balance_units_per_million,self.output_balance_units_per_million,self.balance_units_per_usd,self.cache_write_balance_units_per_million,self.cache_read_balance_units_per_million)):
                raise ValueError('runtime USD prices must not carry a native-unit conversion')
        elif self.pricing_unit != 'balance_units_per_million' or self.input_usd_per_million is not None or self.output_usd_per_million is not None:
            raise ValueError('KuaiAPI requires explicit native or runtime USD price units')
        if self.pricing_unit != 'runtime_usd_per_million' and (self.runtime_pricing_verified or any(v is not None for v in (self.runtime_rate_multiplier,self.cache_write_usd_per_million,self.cache_read_usd_per_million))):
            raise ValueError('runtime USD fields require runtime USD pricing')
        return self

    def missing(self):
        keys = ['model_id','total_budget_usd','price_source','price_verified_on']
        keys += ['input_usd_per_million','output_usd_per_million'] if self.provider=='openai' else ['provider_group','price_evidence_sha256']
        if self.provider=='kuaiapi':
            if self.pricing_unit=='runtime_usd_per_million':
                keys += ['input_usd_per_million','output_usd_per_million','cache_write_usd_per_million','cache_read_usd_per_million','runtime_rate_multiplier']
            else:
                keys += ['input_balance_units_per_million','output_balance_units_per_million','balance_units_per_usd']
        missing = [k for k in keys if getattr(self,k) is None]
        if self.provider=='kuaiapi' and not self.gateway_routing_acknowledged:
            missing.append('gateway_routing_acknowledged')
        if self.pricing_unit=='runtime_usd_per_million' and not self.runtime_pricing_verified:
            missing.append('runtime_pricing_verified')
        return missing

    def usd_rates(self):
        if self.provider=='openai':
            return self.input_usd_per_million,self.output_usd_per_million
        if self.pricing_unit=='runtime_usd_per_million':
            if self.missing():
                return None,None
            # Already effective USD prices: neither conversion nor multiplier
            # is applied again. Cache creation is reserved fully/additively.
            return (float(Decimal(str(self.input_usd_per_million))+Decimal(str(self.cache_write_usd_per_million))),
                    self.output_usd_per_million)
        if any(v is None for v in (self.input_balance_units_per_million,self.output_balance_units_per_million,self.balance_units_per_usd)):
            return None,None
        # Reserve full input PLUS cache creation even if the provider replaces,
        # rather than adds, that charge. Reconciliation records actual deductions.
        input_ceiling = Decimal(str(self.input_balance_units_per_million))+Decimal(str(self.cache_write_balance_units_per_million or 0))
        return (float(input_ceiling/Decimal(str(self.balance_units_per_usd))),
                float(Decimal(str(self.output_balance_units_per_million))/Decimal(str(self.balance_units_per_usd))))

    def attribution(self):
        return {'provider':self.provider,'endpoint':'https://kuaiapi.net/v1/responses' if self.provider=='kuaiapi' else 'https://api.openai.com/v1/responses',
                'group':self.provider_group,'requested_model':self.model_id,
                'identity_scope':'gateway-reported-label-only' if self.provider=='kuaiapi' else 'direct-provider-dated-snapshot',
                'upstream_identity_independently_verified':False,
                'group_binding':'owner-declared-key-binding-not-remotely-verified' if self.provider=='kuaiapi' else None}

    def provider_settings(self):
        settings = {"max_output_tokens": self.max_output_tokens, "store": False}
        if self.provider=='openai':
            settings['service_tier']='default'
        if self.reasoning_effort is not None:
            settings["reasoning"] = {"effort": self.reasoning_effort}
        if self.temperature is not None:
            settings["temperature"] = self.temperature
        return settings


def source_fingerprint():
    paths = [*sorted((ROOT/'backend/app').glob('*.py')), ROOT/'tools/run_pilot.py', ROOT/'uv.lock', ROOT/'frontend/package-lock.json']
    return canonical_hash({p.relative_to(ROOT).as_posix(): sha256(p.read_bytes()) for p in paths})


def make_manifest(config, dataset):
    if dataset.hash != config.dataset_hash or dataset.metadata['kind'] != 'historical':
        raise ValueError('pilot requires the explicitly selected historical dataset and exact hash')
    exp = experiment_config(config, 'single_agent', 'pending')
    features = snapshots(dataset)
    calendar = schedule(dataset, exp, features)
    eligible = [(d, x) for d, x in calendar.items() if x <= config.end_date and d in features]
    nominal = len(eligible)*4
    prompts = {s: {'instructions': INSTRUCTIONS[s], 'schema': SCHEMAS[s].model_json_schema(),
                   'prompt_hash': canonical_hash({'version': PROMPT_VERSION, 'instructions': INSTRUCTIONS[s], 'schema': SCHEMAS[s].model_json_schema()})} for s in SCHEMAS}
    # Upper bound on inputs includes full bounded predecessor strings/arrays,
    # ASCII JSON escaping, schema and protocol allowance. Enforced per request.
    input_token_bound = 32768
    input_rate,output_rate = config.usd_rates()
    per_attempt = None if input_rate is None or output_rate is None else (
        input_token_bound*input_rate+config.max_output_tokens*output_rate)/1e6
    try:
        code_commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,stderr=subprocess.DEVNULL).decode().strip()
        code_dirty = bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT).strip())
    except (OSError, subprocess.SubprocessError):
        code_commit, code_dirty = None, True
    manifest = {'pilot_version': 2, 'configuration': config.model_dump(mode='json'),
                'missing_configuration': config.missing(), 'provider': config.provider, 'provider_settings': config.provider_settings(),
                'provider_attribution':config.attribution(), 'transport_timeout_policy':config.timeout_policy.model_dump(),
                'normalized_usd_per_million':{'input':input_rate,'output':output_rate},
                'dataset': dataset.metadata, 'engine_version': ENGINE_VERSION, 'feature_version': FEATURE_VERSION,
                'prompt_version': PROMPT_VERSION, 'prompts': prompts, 'source_hash': source_fingerprint(),
                'code_commit': code_commit, 'code_dirty': code_dirty,
                'calendar': 'XNYS', 'sessions': [{'decision': str(d), 'execution': str(x),
                    'warmup_bars': sum(b.session_date <= d for b in dataset.bars),
                    'numeric_input_hash': canonical_hash(features[d].numeric_input())} for d, x in eligible],
                'estimate': {'single_nominal_calls': len(eligible), 'multi_nominal_calls': len(eligible)*3,
                    'total_nominal_calls': nominal, 'retry_attempt_ceiling': nominal*config.max_attempts_per_role,
                    'global_attempt_ceiling': config.max_calls, 'separate_smoke_calls': 0,
                    'input_token_bound_per_attempt': input_token_bound,
                    'output_token_ceiling_per_attempt': config.max_output_tokens,
                    'reservation_usd_per_attempt': per_attempt,
                    'nominal_reservation_usd': None if per_attempt is None else per_attempt*nominal,
                    'ceiling_reservation_usd': None if per_attempt is None else per_attempt*config.max_calls,
                    'billing_note': 'Usage estimates are conservative ceilings at frozen rates, including additive cache creation when configured. Confirmed actual deductions are separate. Unknown charges retain full reservations.',
                    'first_call_gate': 'KuaiAPI: first Single decision, one attempt, then pause for strict output/usage/group/actual charge reconciliation' if config.provider=='kuaiapi' else None}}
    manifest['manifest_hash'] = canonical_hash(manifest)
    return manifest


def experiment_config(config, strategy, group):
    return ExperimentConfig(strategy_id=strategy, dataset_id=config.dataset_id, dataset_hash=config.dataset_hash,
        start_date=config.start_date, end_date=config.end_date, initial_cash=config.initial_cash,
        fee_bps=config.fee_bps, slippage_bps=config.slippage_bps, research_group_id=group,
        llm_mode='api' if strategy in ('single_agent','multi_agent') else 'mock',
        model_provider=config.provider if strategy in ('single_agent','multi_agent') else None,
        model_id=config.model_id if strategy in ('single_agent','multi_agent') else None,
        max_llm_calls=config.max_calls, max_llm_budget_usd=config.total_budget_usd or 0,
        max_attempts_per_role=config.max_attempts_per_role)


class PilotBudget:
    """Atomic persistent reservations shared across every run and retry."""
    def __init__(self, store, manifest, anchor_manifest_hash=None):
        from .pilot_billing import checked_manifest
        checked_manifest(manifest)
        self.store = store
        self.id = anchor_manifest_hash or manifest['manifest_hash']
        self.revision_hash = manifest['manifest_hash']
        cfg = manifest['configuration']
        if cfg['total_budget_usd'] is None:
            raise ValueError('explicit total pilot budget required')
        self.limit = Decimal(str(cfg['total_budget_usd']))
        self.max_calls = cfg['max_calls']
        with store.connect() as conn:
            budget_ledger.create_schema(conn)
            if anchor_manifest_hash and anchor_manifest_hash!=manifest['manifest_hash']:
                from .pilot_billing import require_revision
                require_revision(conn,anchor_manifest_hash,manifest)
            else:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_revisions'").fetchone():
                    if conn.execute('SELECT 1 FROM pilot_revisions WHERE pilot_id=?',(self.id,)).fetchone():
                        raise ValueError('superseded pilot pricing requires the registered revision')
                existing=conn.execute('SELECT pilot_id FROM pilot_budgets').fetchall()
                if existing and not any(r[0]==self.id for r in existing):
                    raise ValueError('existing pilot requires an anchored revision; refusing a fresh budget')
                conn.execute('INSERT OR IGNORE INTO pilot_budgets VALUES (?,?)', (self.id, json.dumps(manifest,sort_keys=True)))
            row = conn.execute('SELECT manifest_json FROM pilot_budgets WHERE pilot_id=?',(self.id,)).fetchone()
            if self.id==self.revision_hash and json.loads(row[0]) != manifest:
                raise ValueError('immutable pilot manifest mismatch')

    def _rows(self, conn):
        return budget_ledger.rows(conn,self.id)

    _charge = staticmethod(budget_ledger.charge)

    def _aggregate(self, conn):
        return budget_ledger.aggregate(conn,self.id)

    def _total(self, conn, rows, replacement=None):
        return budget_ledger.total(conn,self.id,rows,replacement)

    def reserve(self, call_id, amount):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            budget_ledger.reserve(conn,self.id,self.revision_hash,self.limit,self.max_calls,call_id,amount)

    def over_limit(self, call_id, usage_estimate):
        with self.store.read() as conn:
            rows = self._rows(conn)
            charged = self._total(conn,rows,(call_id,usage_estimate))
        return charged > self.limit

    def settle(self, call_id, usage_estimate):
        if usage_estimate is not None and (not math.isfinite(usage_estimate) or usage_estimate < 0):
            raise ValueError('invalid usage estimate')
        with self.store.connect() as conn:
            conn.execute('UPDATE pilot_attempts SET usage_estimate=?, status=? WHERE call_id=? AND pilot_id=? AND status=?',
                (str(usage_estimate) if usage_estimate is not None else None,
                 'usage_estimated' if usage_estimate is not None else 'unknown_reserved',call_id,self.id,'unknown_reserved'))

    def summary(self):
        with self.store.read() as conn:
            return budget_ledger.summary(conn,self.id,self.limit,self.max_calls)

    @staticmethod
    def read_summary(store, manifest, anchor_manifest_hash=None):
        """Validate an existing account and poll without constructor/schema writes."""
        from .pilot_billing import checked_manifest, require_revision
        checked_manifest(manifest)
        namespace=anchor_manifest_hash or manifest['manifest_hash']
        with store.read() as conn:
            row=conn.execute('SELECT manifest_json FROM pilot_budgets WHERE pilot_id=?',(namespace,)).fetchone()
            if not row:
                raise ValueError('unknown existing pilot account')
            if anchor_manifest_hash and anchor_manifest_hash!=manifest['manifest_hash']:
                require_revision(conn,namespace,manifest)
            else:
                if json.loads(row[0])!=manifest:
                    raise ValueError('immutable pilot manifest mismatch')
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_revisions'").fetchone() and conn.execute('SELECT 1 FROM pilot_revisions WHERE pilot_id=?',(namespace,)).fetchone():
                    raise ValueError('superseded pilot pricing requires the registered revision')
            cfg=manifest['configuration']
            if cfg['total_budget_usd'] is None:
                raise ValueError('explicit total pilot budget required')
            return budget_ledger.summary(conn,namespace,Decimal(str(cfg['total_budget_usd'])),cfg['max_calls'])


class PilotProvider(OpenAIProvider):
    require_exact_model = True

    def __init__(self, config, client=None):
        super().__init__(client, model_id=config.model_id,input_rate=config.input_usd_per_million,
                         output_rate=config.output_usd_per_million,settings=config.provider_settings(),timeout_policy=config.timeout_policy)
        self.input_bound = 32768
        self.attribution = config.attribution()

    def reserve_cost(self, stage, payload, schema):
        if len(json.dumps(self.payload(stage,payload,schema)).encode())+4096 > self.input_bound:
            raise AgentFailure('PILOT_INPUT_BOUND_EXCEEDED')
        return (self.input_bound*self.input_rate+self.settings['max_output_tokens']*self.output_rate)/1e6


def _finite_number(value, *, allow_none):
    """Bool is not a weight or a price. None is allowed only where the caller says so."""
    if value is None:
        if allow_none:
            return
        raise ValueError('MALFORMED_NUMERIC')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('MALFORMED_NUMERIC')
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError('MALFORMED_NUMERIC')


def _numeric_fields(row, names, *, allow_none, required=()):
    if not isinstance(row, dict):
        raise ValueError('MALFORMED_NUMERIC')
    for name in required:
        if name not in row:
            raise ValueError('MALFORMED_NUMERIC')
    for name in names:
        if name in row:
            _finite_number(row[name], allow_none=allow_none)


def _annotation_is_number(annotation):
    if annotation in (int, float):
        return True
    if get_origin(annotation) is Literal:
        args = get_args(annotation)
        return bool(args) and all(type(item) in (int, float) for item in args)
    return False


def _role_output(value, model):
    """Check schema-declared numeric fields. Booleans and null role outputs stay as declared."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError('MALFORMED_NUMERIC')
    for name, field in model.model_fields.items():
        if not _annotation_is_number(field.annotation):
            continue
        if name not in value:
            raise ValueError('MALFORMED_NUMERIC')
        _finite_number(value[name], allow_none=False)
    try:
        model.model_validate(value)
    except ValidationError:
        raise ValueError('MALFORMED_NUMERIC') from None


def validate_semantic_numbers(result):
    """Shared precondition for semantic equality. It does not recompute a ledger."""
    if not isinstance(result, dict):
        raise ValueError('MALFORMED_NUMERIC')
    decisions, trades, equity, metrics = (result.get(name) for name in ('decisions', 'trades', 'equity', 'metrics'))
    if not isinstance(decisions, list) or not isinstance(trades, list) or not isinstance(equity, list) or not isinstance(metrics, dict):
        raise ValueError('MALFORMED_NUMERIC')
    for decision in decisions:
        _numeric_fields(decision, ('requested_weight', 'approved_weight'), allow_none=True)
        for key, model in (('analysis_output', SCHEMAS['analysis']), ('critic_output', SCHEMAS['critic']), ('decision_output', SCHEMAS['decision'])):
            _role_output(decision.get(key), model)
    for trade in trades:
        _numeric_fields(trade, ('quantity', 'reference_open', 'execution_price', 'notional', 'fee', 'slippage_cost', 'cash_after', 'shares_after', 'weight_after'), allow_none=False)
    for point in equity:
        _numeric_fields(point, ('equity', 'cash', 'shares', 'close', 'drawdown'), allow_none=False, required=('equity', 'cash', 'shares'))
    for key, value in metrics.items():
        if not key.startswith(('llm_', 'agent_', 'mock_', 'uncertain_', 'reserved_')):
            _finite_number(value, allow_none=True)


def semantic_result(result):
    validate_semantic_numbers(result)
    decision_keys = ('decision_session','execution_session','analysis_output','critic_output','decision_output',
                     'requested_weight','approved_weight','status','reason_codes')
    financial = {k:v for k,v in result['metrics'].items() if not k.startswith(('llm_','agent_','mock_','uncertain_','reserved_'))}
    return {'decisions':[{k:d[k] for k in decision_keys} for d in result['decisions']],
            'trades':[{k:v for k,v in t.items() if k not in ('trade_id','decision_id')} for t in result['trades']],
            'equity':result['equity'],'metrics':financial}


def save_run(store, config, dataset, *, source=None, provider=None, budget=None, manifest=None, key=None, initial_agent_results=None):
    run_id, fresh = store.create(config,key)
    if not fresh:
        row = store.get(run_id,True)
        if row['status'] != 'COMPLETED':
            raise ValueError('prior pilot run interrupted or failed; inspect diagnostic evidence before starting a new pilot')
        return row
    try:
        store.update(run_id,status='RUNNING')
        result = run_experiment(config,dataset,lambda n:store.update(run_id,progress=n),source,provider,
            lambda kind,payload:store.record_event(run_id,kind,payload),budget,initial_agent_results)
        if manifest:
            result['pilot_manifest'] = manifest
        store.update(run_id,status='COMPLETED',progress=100,result=result)
    except BaseException:
        store.update(run_id,status='FAILED',error='PILOT_INTERRUPTED_OR_FAILED')
        raise
    return store.get(run_id,True)


def execute_pilot(store, config, dataset, *, baselines_only=False, provider=None, probe_reconciliation=None, anchor_manifest_hash=None):
    manifest = make_manifest(config,dataset)
    if not baselines_only and config.missing():
        raise ValueError('pilot configuration incomplete: '+', '.join(config.missing()))
    # Constructing the provider checks key presence but sends no request.
    if not baselines_only and provider is None:
        if config.provider=='kuaiapi':
            from .gateway import KuaiAPIProvider
            provider = KuaiAPIProvider(config)
        else:
            provider = PilotProvider(config)
    if baselines_only:
        provider = None
    elif transport_policy(provider)!=config.timeout_policy.model_dump():
        raise ValueError('provider timeout policy differs from frozen pilot configuration')
    group = anchor_manifest_hash or manifest['manifest_hash']
    budget = None if baselines_only else PilotBudget(store,manifest,anchor_manifest_hash)
    if baselines_only and anchor_manifest_hash:
        from .pilot_billing import require_revision
        with store.connect() as conn:
            require_revision(conn,anchor_manifest_hash,manifest)
    seed=None
    probe=None
    if not baselines_only and config.provider=='kuaiapi':
        from .pilot_probe import first_decision_gate
        probe,seed=first_decision_gate(store,manifest,experiment_config(config,'single_agent',group),dataset,provider,budget,probe_reconciliation,anchor_manifest_hash)
        if seed is None:
            return {'status':'PAUSED_FOR_FIRST_CALL_RECONCILIATION','manifest':manifest,'runs':[],
                    'probe':probe,'budget':budget.summary(),'replay_equality':[],
                    'limitations':['No further inference until strict output, usage, key group and actual charge are reconciled.']}
    rows = []
    replay_evidence = []
    for strategy in STRATEGIES[:2] if baselines_only else STRATEGIES:
        if strategy in ('single_agent','multi_agent') and (
                getattr(provider,'attribution',None) != config.attribution() or provider.settings != config.provider_settings() or
                transport_policy(provider)!=config.timeout_policy.model_dump()):
            raise ValueError('provider/group/settings changed after pilot freeze')
        exp = experiment_config(config,strategy,group)
        row = save_run(store,exp,dataset,provider=provider,budget=budget,manifest=manifest,key=group+':'+strategy,
                       initial_agent_results={manifest['sessions'][0]['decision']:seed} if strategy=='single_agent' and seed else None)
        rows.append(row)
        replay_config = exp.model_copy(update={'llm_mode':'replay','replay_source_run_id':UUID(row['run_id'])})
        replay = save_run(store,replay_config,dataset,source=row['result'],manifest=manifest,key=group+':replay:'+strategy)
        equal = semantic_result(row['result'])==semantic_result(replay['result'])
        if not equal or replay['metrics']['llm_calls'] or replay['metrics']['llm_cost_usd']:
            raise RuntimeError('Replay semantic equality or isolation failed')
        replay_evidence.append({'strategy':strategy,'source_run_id':row['run_id'],'replay_run_id':replay['run_id'],
                                'semantic_sha256':canonical_hash(semantic_result(row['result'])),'equal':equal,
                                'provider_calls':0,'replay_cost_usd':0})
    return {'status':'BASELINES_ONLY_CONFIGURATION_PENDING' if baselines_only else 'COMPLETED',
            'manifest':manifest,'runs':rows,'replay_equality':replay_evidence,
            'budget':budget.summary() if budget else None,'created_at':now(),'probe':probe,
            'limitations':['Price-only; dividends and interest excluded.','Historical knowledge may exist in model training.',
                           'Single and Multi use unequal inference budgets.','This short pilot tests integration, not investment effectiveness.']}
