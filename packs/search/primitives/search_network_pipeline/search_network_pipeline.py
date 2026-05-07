#!/usr/bin/env python3
"""Resumable orchestrator for the local search-network primitive pipeline.

This runner intentionally starts after query extraction. It needs either an
existing task `--state` or a `--query` plus `--payload-json` containing the
`expand_search_request` shape. Natural-language decomposition remains a skill /LLM
handoff; everything after that is mechanical.
"""
from __future__ import annotations

import argparse, hashlib, json, os, shlex, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL = "gpt-5.4"

class Blocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20):
        self.payload, self.code = payload, code
        super().__init__(payload.get("message", "blocked"))

class Failed(Exception): pass

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def emit(x: Any) -> None: print(json.dumps(x, indent=2, sort_keys=True))
def read_json(p: Path, default=None):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except Exception: return default

def write_json(p: Path, x: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(x, indent=2, sort_keys=True)+"\n")

def parse_jsons(s: str) -> list[Any]:
    out=[]; dec=json.JSONDecoder(); i=0
    while i < len(s):
        while i < len(s) and s[i].isspace(): i += 1
        if i >= len(s): break
        try: obj,end=dec.raw_decode(s,i); out.append(obj); i=end
        except json.JSONDecodeError:
            j=s.find("{", i+1)
            if j < 0: break
            i=j
    return out

def run(cmd: list[str], *, env_file: str = ".env", timeout: int = 600) -> dict[str, Any]:
    env=dict(os.environ)
    for f in [ROOT/env_file, (ROOT/"../network-search-api/.env").resolve()]:
        if f.exists():
            for line in f.read_text(errors="ignore").splitlines():
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line: continue
                k,v=line.split("=",1)
                if k not in env and v.strip(): env[k]=v.strip().strip('"').strip("'")
    t=time.monotonic()
    p=subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout)
    js=parse_jsons(p.stdout or "")
    return {"cmd":cmd,"returncode":p.returncode,"stdout":p.stdout,"stderr":p.stderr,"elapsed_seconds":round(time.monotonic()-t,3),"json_objects":js,"json":js[-1] if js else None}

def require_ok(res: dict[str, Any], step: str) -> dict[str, Any]:
    if res["returncode"] != 0:
        raise Failed(f"{step} failed rc={res['returncode']}: {((res.get('stderr') or res.get('stdout') or '').strip())[-1200:]}")
    return res.get("json") or {}

def ledger_path_for(state: Path|None, explicit: Path|None) -> Path:
    if explicit: return explicit
    if state: return Path(str(state)+".pipeline.json")
    return ROOT/".powerpacks/runs/search-network-pipeline.json"

def load_ledger(p: Path) -> dict[str, Any]:
    x=read_json(p,{}) or {}; x.setdefault("created_at", now()); x.setdefault("steps",{}); x.setdefault("approvals",{}); x.setdefault("artifacts",{}); return x

def save(p: Path, l: dict[str, Any]) -> None: l["updated_at"]=now(); write_json(p,l)
def done(l: dict[str, Any], step: str) -> bool: return l.get("steps",{}).get(step,{}).get("status")=="completed"
def mark(p: Path, l: dict[str, Any], step: str, status: str, **kw) -> None:
    r=l.setdefault("steps",{}).setdefault(step,{"id":step}); r.update(status=status, **kw)
    if status in {"completed","skipped","failed","blocked_approval","blocked_user_action"}: r["finished_at"]=now()
    save(p,l)

def approval_id(kind: str, payload: dict[str, Any]) -> str:
    return kind+"_"+hashlib.sha1(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:12]

def is_approved(l: dict[str, Any], aid: str) -> bool: return bool(l.get("approvals",{}).get(aid,{}).get("confirmed"))

def block(lp: Path, l: dict[str, Any], args, kind: str, step: str, payload: dict[str, Any], msg: str):
    aid=approval_id(kind,payload)
    b={"primitive":"search_network_pipeline","status":"blocked_approval","approval_type":kind,"approval_id":aid,"message":msg,"payload":payload,"ledger":str(lp),"continue_command":f"python packs/search/primitives/search_network_pipeline/search_network_pipeline.py approve {kind} --ledger {shlex.quote(str(lp))} --approval-id {aid} --confirm && python packs/search/primitives/search_network_pipeline/search_network_pipeline.py continue --ledger {shlex.quote(str(lp))}"}
    l["current_block"]=b; mark(lp,l,step,"blocked_approval",summary=b); raise Blocked(b)

def state_has_step(state: Path, step: str) -> bool:
    s=read_json(state,{}) or {}; return any(x.get("id")==step for x in s.get("steps",[]))

def init_state(args, lp: Path, l: dict[str, Any]) -> Path:
    if args.state:
        state=Path(args.state); l["state"]=str(state); save(lp,l); return state
    if l.get("state"):
        return Path(l["state"])
    if not args.query or not args.payload_json:
        b={"primitive":"search_network_pipeline","status":"blocked_user_action","message":"Need --state, or --query plus --payload-json from extract-search-query.","ledger":str(lp)}
        l["current_block"]=b; mark(lp,l,"init_state","blocked_user_action",summary=b); raise Blocked(b,21)
    cmd=[sys.executable,str(ROOT/"packs/search/primitives/task_state/task_state.py"),"init","--query",args.query]
    res=run(cmd, env_file=args.env_file, timeout=args.timeout); out=require_ok(res,"task_state init")
    state=Path(out["state"]); l["state"]=str(state); l.setdefault("artifacts",{})["state"]=str(state)
    mark(lp,l,"init_state","completed",summary=out,command=" ".join(cmd))
    payload=read_json(Path(args.payload_json))
    cmd=[sys.executable,str(ROOT/"packs/search/primitives/task_state/task_state.py"),"record-step","--state",str(state),"--step-id","expand_search_request","--status","completed","--output-json",json.dumps(payload)]
    out=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout),"record expand_search_request")
    mark(lp,l,"record_expand_search_request","completed",summary=out,command=" ".join(cmd))
    save(lp,l); return state

def maybe_payload_filters(state: Path) -> dict[str, Any]:
    s=read_json(state,{}) or {}
    for step in reversed(s.get("steps",[])):
        if step.get("id")=="expand_search_request":
            return ((step.get("output") or {}).get("role_search_filters") or {})
    return {}

def run_pipeline(args) -> dict[str, Any]:
    lp=ledger_path_for(Path(args.state) if args.state else None, Path(args.ledger) if args.ledger else None)
    l=load_ledger(lp); l["current_block"]=None; save(lp,l)
    state=init_state(args,lp,l)
    common={"--state":str(state),"--env-file":args.env_file}
    steps=[("resolve_set_operators",[sys.executable,str(ROOT/"packs/search/primitives/resolve_set_operators/resolve_set_operators.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"])]
    f=maybe_payload_filters(state)
    if f.get("company_names") or f.get("company_ids") or f.get("current_company_names"):
        steps.append(("resolve_companies",[sys.executable,str(ROOT/"packs/search/primitives/resolve_companies/resolve_companies.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]))
    if f.get("education_names"):
        steps.append(("resolve_education",[sys.executable,str(ROOT/"packs/search/primitives/resolve_education/resolve_education.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]))
    if f.get("investor_names"):
        steps.append(("resolve_investors",[sys.executable,str(ROOT/"packs/search/primitives/resolve_investors/resolve_investors.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]))
    steps += [
        ("apply_prefilters",[sys.executable,str(ROOT/"packs/search/primitives/apply_prefilters/apply_prefilters.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]),
        ("execute_role_search",[sys.executable,str(ROOT/"packs/search/primitives/execute_role_search/execute_role_search.py"),"--state",str(state),"--env-file",args.env_file,"--write-state","--limit",str(args.limit),"--top-k",str(args.top_k)]),
        ("hydrate_people",[sys.executable,str(ROOT/"packs/search/primitives/hydrate_people/hydrate_people.py"),"--state",str(state),"--env-file",args.env_file,"--write-state"]),
    ]
    for step,cmd in steps:
        if done(l,step) and not args.force: continue
        mark(lp,l,step,"running",command=" ".join(shlex.quote(x) for x in cmd))
        out=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout),step)
        mark(lp,l,step,"completed",summary=out,command=" ".join(shlex.quote(x) for x in cmd))
    if not args.search_only:
        payload={"state":str(state),"model":args.model,"mode":"filter_rerank"}; aid=approval_id("llm",payload)
        if not is_approved(l,aid) and not args.confirm_llm:
            block(lp,l,args,"llm","llm_filter_rerank",payload,"Run LLM filter + rerank for this search? This may spend OpenAI credits.")
        for step,cmd in [
            ("llm_filter_candidates",[sys.executable,str(ROOT/"packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py"),"--state",str(state),"--profile-scope","auto","--write-state"]),
            ("llm_rerank_candidates",[sys.executable,str(ROOT/"packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py"),"--state",str(state),"--concurrency",str(args.rerank_concurrency),"--write-state"]),
        ]:
            if done(l,step) and not args.force: continue
            mark(lp,l,step,"running",command=" ".join(shlex.quote(x) for x in cmd))
            out=require_ok(run(cmd, env_file=args.env_file, timeout=args.llm_timeout),step)
            mark(lp,l,step,"completed",summary=out,command=" ".join(shlex.quote(x) for x in cmd))
    if not done(l,"persist_search_results") or args.force:
        cmd=[sys.executable,str(ROOT/"packs/search/primitives/persist_search_results/results_io.py"),"export","--state",str(state)]
        mark(lp,l,"persist_search_results","running",command=" ".join(cmd)); out=require_ok(run(cmd, env_file=args.env_file, timeout=args.timeout),"persist_search_results"); mark(lp,l,"persist_search_results","completed",summary=out,command=" ".join(cmd))
    return {"primitive":"search_network_pipeline","status":"completed","ledger":str(lp),"state":str(state),"artifacts":l.get("artifacts",{})}

def cmd_run(args):
    try: emit(run_pipeline(args)); return 0
    except Blocked as e: emit(e.payload); return e.code
    except Exception as e: emit({"primitive":"search_network_pipeline","status":"failed","error":str(e)}); return 1

def cmd_status(args):
    lp=ledger_path_for(Path(args.state) if args.state else None, Path(args.ledger) if args.ledger else None); l=load_ledger(lp)
    emit({"primitive":"search_network_pipeline","status":"ok","ledger":str(lp),"current_block":l.get("current_block"),"step_counts":{s:sum(1 for r in l.get('steps',{}).values() if r.get('status')==s) for s in sorted({r.get('status') for r in l.get('steps',{}).values()})}}); return 0

def cmd_approve(args):
    if not args.confirm: emit({"status":"blocked","error":"pass --confirm"}); return 2
    lp=ledger_path_for(Path(args.state) if args.state else None, Path(args.ledger) if args.ledger else None); l=load_ledger(lp); cur=l.get("current_block") or {}; aid=args.approval_id or cur.get("approval_id")
    if not aid: emit({"status":"failed","error":"no approval_id"}); return 1
    l.setdefault("approvals",{})[aid]={"confirmed":True,"type":args.kind,"approved_at":now(),"payload":cur.get("payload",{})}; l["current_block"]=None; save(lp,l); emit({"primitive":"search_network_pipeline","status":"ok","approval_id":aid}); return 0

def add_run(p):
    p.add_argument("--ledger"); p.add_argument("--state"); p.add_argument("--query"); p.add_argument("--payload-json"); p.add_argument("--env-file",default=".env"); p.add_argument("--limit",type=int,default=0,help="Max unique people to keep locally after retrieval; 0 means keep full retrieved frontier"); p.add_argument("--top-k",type=int,default=10000); p.add_argument("--search-only",action="store_true"); p.add_argument("--confirm-llm",action="store_true"); p.add_argument("--model",default=DEFAULT_MODEL); p.add_argument("--rerank-concurrency",type=int,default=200); p.add_argument("--timeout",type=int,default=600); p.add_argument("--llm-timeout",type=int,default=3600); p.add_argument("--force",action="store_true")

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    r=sub.add_parser("run"); add_run(r); r.set_defaults(func=cmd_run)
    c=sub.add_parser("continue"); add_run(c); c.set_defaults(func=cmd_run)
    s=sub.add_parser("status"); s.add_argument("--ledger"); s.add_argument("--state"); s.set_defaults(func=cmd_status)
    a=sub.add_parser("approve"); a.add_argument("kind",choices=["llm"]); a.add_argument("--ledger"); a.add_argument("--state"); a.add_argument("--approval-id"); a.add_argument("--confirm",action="store_true"); a.set_defaults(func=cmd_approve)
    args=ap.parse_args(); raise SystemExit(args.func(args))
if __name__ == "__main__": main()
