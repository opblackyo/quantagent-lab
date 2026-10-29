"""KuaiAPI Responses compatibility. Gateway labels do not attest upstream identity."""
import json

from .agents import AgentFailure, OpenAIProvider


def application_launch_block(provider):
    """The ordinary app cannot fund a provider call. Gateway rejection stays explicit."""
    if provider == "kuaiapi":
        return "KuaiAPI application execution requires the bounded private pilot runner"
    return "OpenAI application execution requires the default-disabled research service"


class KuaiAPIProvider(OpenAIProvider):
    provider_id = 'kuaiapi'
    endpoint = 'https://kuaiapi.net/v1/responses'
    key_env = 'KUAIAPI_API_KEY'
    require_exact_model = True  # Exact gateway echo only, never upstream proof.

    def __init__(self, config, client=None):
        if config.provider != 'kuaiapi' or config.missing():
            raise ValueError('complete explicit KuaiAPI pilot configuration required')
        input_rate,output_rate = config.usd_rates()
        super().__init__(client,model_id=config.model_id,input_rate=input_rate,output_rate=output_rate,settings=config.provider_settings(),timeout_policy=config.timeout_policy)
        self.input_bound = 32768
        self.attribution = config.attribution()
        self.pricing_attribution = {
            'unit':'balance units per 1M tokens','input':config.input_balance_units_per_million,
            'output':config.output_balance_units_per_million,'balance_units_per_usd':config.balance_units_per_usd,
            'cache_write':config.cache_write_balance_units_per_million,'cache_read':config.cache_read_balance_units_per_million,
            'quote_basis':'already group-adjusted; input reservation/usage ceiling includes full additive cache creation; no second multiplier',
            'source':config.price_source,'verified_on':str(config.price_verified_on),'evidence_sha256':config.price_evidence_sha256,
            'usage_cost_is_estimate':True,'invoiced_actual_usd':None}
        if config.pricing_unit=='runtime_usd_per_million':
            self.pricing_attribution = {
                'unit':'USD per 1M tokens','input':config.input_usd_per_million,
                'output':config.output_usd_per_million,'cache_write':config.cache_write_usd_per_million,
                'cache_read':config.cache_read_usd_per_million,'observed_multiplier':config.runtime_rate_multiplier,
                'quote_basis':'already effective USD; additive cache-write ceiling; no conversion or second multiplier',
                'source':config.price_source,'verified_on':str(config.price_verified_on),
                'evidence_sha256':config.price_evidence_sha256,'usage_cost_is_estimate':True,'invoiced_actual_usd':None}
            self.pricing_attribution['cache_tariff_scope']='published tariff derived at observed multiplier; cache deductions not independently measured'

    def reserve_cost(self, stage, payload, schema):
        if len(json.dumps(self.payload(stage,payload,schema)).encode())+4096 > self.input_bound:
            raise AgentFailure('PILOT_INPUT_BOUND_EXCEEDED')
        return (self.input_bound*self.input_rate+self.settings['max_output_tokens']*self.output_rate)/1e6

    @staticmethod
    def usage(usage):
        if not isinstance(usage,dict):
            return 0,0,False
        canonical = any(k in usage for k in ('input_tokens','output_tokens'))
        legacy = any(k in usage for k in ('prompt_tokens','completion_tokens'))
        native = {**usage}
        if not canonical and legacy:
            native.update(input_tokens=usage.get('prompt_tokens'),output_tokens=usage.get('completion_tokens'))
        if canonical and legacy:
            alias = {**usage,'input_tokens':usage.get('prompt_tokens'),'output_tokens':usage.get('completion_tokens')}
            if not OpenAIProvider.usage(alias)[2] or usage.get('input_tokens')!=usage.get('prompt_tokens') or usage.get('output_tokens')!=usage.get('completion_tokens'):
                return 0,0,False
        # Reasoning/cached tokens are subsets of these totals. Do not add them.
        return OpenAIProvider.usage(native)
