"""Run a reproducible local pipeline check; default Agent mode is Mock."""
import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset",default="synthetic-spy-v1")
    parser.add_argument("--start",default="2024-01-01")
    parser.add_argument("--end",default="2024-12-31")
    parser.add_argument("--api",default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--output",type=Path,default=Path("artifacts/pipeline-comparison.json"))
    args=parser.parse_args()
    run_ids=[]
    with httpx.Client(base_url=args.api+"/",timeout=30) as client:
        for strategy in ("buy_hold","ma_crossover","single_agent","multi_agent"):
            response=client.post("experiments",json={"dataset_id":args.dataset,"start_date":args.start,"end_date":args.end,"strategy_id":strategy,"llm_mode":"mock"},headers={"X-Idempotency-Key":str(uuid4())})
            response.raise_for_status()
            run_id=response.json()["run_id"]
            run_ids.append(run_id)
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                status=client.get("experiments/"+run_id)
                status.raise_for_status()
                row=status.json()
                if row["status"]=="FAILED":
                    raise RuntimeError(row["error_message"])
                if row["status"]=="COMPLETED":
                    print(strategy,run_id,"completed; real LLM calls:",row["metrics"]["llm_calls"])
                    break
                time.sleep(.2)
            else:
                raise TimeoutError("research worker did not complete within five minutes")
        comparison=client.post("experiments/compare",json={"run_ids":run_ids})
        comparison.raise_for_status()
        result={"purpose":"pipeline-validation-only","decision_mode":"mock","historical_llm_experiment":False,**comparison.json()}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        print("Saved:",args.output.resolve())


if __name__=="__main__":
    main()
