"""Server-side HTTPX phase limits, never model payload or total deadlines."""
from typing import Annotated

import httpx
from pydantic import BaseModel,ConfigDict,Field

Seconds=Annotated[float,Field(strict=True,gt=0,le=300)]


class HTTPTimeoutPolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True,allow_inf_nan=False,validate_default=True)
    connect:Seconds=30.0
    read:Seconds=30.0
    write:Seconds=30.0
    pool:Seconds=30.0

    def httpx_timeout(self):
        return httpx.Timeout(**self.model_dump())


def transport_policy(provider):
    policy=getattr(provider,'timeout_policy',None)
    return policy.model_dump() if isinstance(policy,HTTPTimeoutPolicy) else None


def timeout_category(exc):
    for cls,phase in ((httpx.ConnectTimeout,'connect'),(httpx.ReadTimeout,'read'),
                      (httpx.WriteTimeout,'write'),(httpx.PoolTimeout,'pool')):
        if isinstance(exc,cls):return phase,cls.__name__
    return 'unspecified','TimeoutException'
