#!/usr/bin/env python3
"""Resumable Sales Nav local/tool-call handoff orchestrator.

The script owns local run state and exits with explicit `blocked_tool_call`
instructions for the harness/agent. The harness calls the named MCP tool through
its native tool layer, saves the JSON response to `save_response_to`, then reruns
the provided continue command.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shlex, subprocess, sys, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[4]
DEFAULT_BASE=Path(".powerpacks/sales-nav/runs")

def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def emit(x): print(json.dumps(x,indent=2,sort_keys=True))
def read_json(p:Path,default=None):
    if not p.exists(): return default
    try: return json.loads(p.read_text())
    except Exception: return default
def write_json(p:Path,x): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(x,indent=2,sort_keys=True)+"\n")
def parse_jsons(s:str):
    out=[]; dec=json.JSONDecoder(); i=0
    while i<len(s):
        while i<len(s) and s[i].isspace(): i+=1
        if i>=len(s): break
        try: o,e=dec.raw_decode(s,i); out.append(o); i=e
        except json.JSONDecodeError:
            j=s.find("{",i+1)
            if j<0: break
            i=j
    return out
def run(cmd,timeout=300):
    t=time.monotonic(); p=subprocess.run(cmd,cwd=ROOT,text=True,capture_output=True,timeout=timeout); js=parse_jsons(p.stdout or "")
    return {"cmd":cmd,"returncode":p.returncode,"stdout":p.stdout,"stderr":p.stderr,"elapsed_seconds":round(time.monotonic()-t,3),"json":js[-1] if js else None,"json_objects":js}
def require(res,step):
    if res["returncode"]!=0: raise RuntimeError(f"{step} failed: {((res.get('stderr') or res.get('stdout') or '').strip())[-1000:]}")
    return res.get("json") or {}
def slug(s:str)->str:
    import re
    return re.sub(r"[^a-z0-9]+","-",s.lower()).strip("-")[:60] or "sales-nav"
def approval_id(kind,payload): return kind+"_"+hashlib.sha1(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()[:12]
def ledger_path(args):
    if args.ledger: return Path(args.ledger)
    if args.state: return Path(str(args.state)+".pipeline.json")
    rid=args.run_id or f"sales-nav-{slug(args.query or 'run')}-{uuid.uuid4().hex[:8]}"
    return DEFAULT_BASE/rid/"pipeline.json"
def load(lp):
    l=read_json(lp,{}) or {}; l.setdefault("created_at",now()); l.setdefault("steps",{}); l.setdefault("approvals",{}); l.setdefault("artifacts",{}); return l
def save(lp,l): l["updated_at"]=now(); write_json(lp,l)
def done(l,step): return l.get("steps",{}).get(step,{}).get("status")=="completed"
def mark(lp,l,step,status,**kw):
    r=l.setdefault("steps",{}).setdefault(step,{"id":step}); r.update(status=status,**kw); r["updated_at"]=now(); save(lp,l)
def approved(l,aid): return bool(l.get("approvals",{}).get(aid,{}).get("confirmed"))
def block_tool_call(lp,l,tool,args_payload,save_to,next_cmd,msg):
    b={
        "primitive":"sales_nav_pipeline",
        "status":"blocked_tool_call",
        "message":msg,
        "tool_server":"powerset-search",
        "tool_name":tool,
        "tool_args":args_payload,
        "save_response_to":save_to,
        "continue_command":next_cmd,
        "ledger":str(lp),
    }
    l["current_block"]=b; save(lp,l); emit(b); return 30
def block_approval(lp,l,kind,payload,msg,next_cmd):
    aid=approval_id(kind,payload); b={"primitive":"sales_nav_pipeline","status":"blocked_approval","approval_type":kind,"approval_id":aid,"message":msg,"payload":payload,"continue_command":next_cmd,"ledger":str(lp)}
    l["current_block"]=b; mark(lp,l,kind,"blocked_approval",summary=b); emit(b); return 20

def ensure_init(args,lp,l):
    if args.state:
        l.setdefault("artifacts",{})["state"]=str(args.state); save(lp,l); return Path(args.state)
    if done(l,"init_local_artifacts"):
        return Path(l["artifacts"]["state"])
    if not args.query or not args.set_id:
        raise RuntimeError("Need --query and --set-id for init, or pass --state")
    conv=args.conversation_id or str(uuid.uuid4())
    out_dir=lp.parent
    cmd=[sys.executable,str(ROOT/"packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py"),"init","--query",args.query,"--set-id",args.set_id,"--conversation-id",conv,"--out-dir",str(out_dir)]
    res=require(run(cmd),"sales_nav_artifacts init")
    st=Path(res["state"]); l.setdefault("artifacts",{}).update({"state":str(st),"run_dir":str(st.parent),"conversation_id":conv}); mark(lp,l,"init_local_artifacts","completed",summary=res,command=" ".join(map(shlex.quote,cmd))); return st

def cmd_run(args):
    try:
        lp=ledger_path(args); l=load(lp); l["current_block"]=None; save(lp,l); st=ensure_init(args,lp,l); run_dir=st.parent; pages=run_dir/"pages"; pages.mkdir(parents=True,exist_ok=True)
        # Ingest a supplied tool response, if present.
        if args.response:
            step="ingest_enriched_page" if args.enriched else ("ingest_full_artifact" if args.prefer_content else "ingest_page")
            if not done(l,step) or args.force:
                cmd=[sys.executable,str(ROOT/"packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py"),"ingest-page","--state",str(st),"--response",str(args.response)]
                if args.prefer_content: cmd.append("--prefer-content")
                res=require(run(cmd),step); mark(lp,l,step,"completed",summary=res,command=" ".join(map(shlex.quote,cmd)))
        elif not done(l,"ingest_page"):
            search_args=read_json(Path(args.search_args_json),{}) if args.search_args_json else {}
            payload={"set_id":args.set_id,"conversation_id":l.get("artifacts",{}).get("conversation_id"),"persist_artifact":True,"count":args.count,**search_args}
            save_to=str(pages/"sales-nav-search.response.json")
            return block_tool_call(lp,l,"sales_nav_search",payload,save_to,f"python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py continue --ledger {shlex.quote(str(lp))} --response {shlex.quote(save_to)}", "Call the Sales Nav search tool, save the JSON response, then continue.")
        state_json=read_json(st,{}) or {}
        artifact_ids=state_json.get("artifact_ids") or []
        if artifact_ids and not done(l,"ingest_full_artifact"):
            save_to=str(pages/"artifact-full-000.json")
            payload={"artifact_id":artifact_ids[-1],"offset":0,"limit":args.count,"include_content":True}
            return block_tool_call(lp,l,"get_artifact",payload,save_to,f"python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py continue --ledger {shlex.quote(str(lp))} --response {shlex.quote(save_to)} --prefer-content", "Call get_artifact(include_content=true), save the full JSON response, then continue.")
        if args.require_enriched and not done(l,"ingest_enriched_page"):
            save_to=str(pages/"artifact-full-after-enrich.json")
            return block_tool_call(lp,l,"get_artifact",{"artifact_id":artifact_ids[-1] if artifact_ids else "<artifact_id>","offset":0,"limit":args.count,"include_content":True},save_to,f"python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py continue --ledger {shlex.quote(str(lp))} --response {shlex.quote(save_to)} --prefer-content --enriched", "After enriching visible leads with enrich_extended_profiles, call get_artifact(include_content=true), save the full JSON response, and continue.")
        if not done(l,"export") or args.force:
            cmd=[sys.executable,str(ROOT/"packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py"),"export","--state",str(st)]
            res=require(run(cmd),"export"); mark(lp,l,"export","completed",summary=res,command=" ".join(map(shlex.quote,cmd)))
        if args.criteria:
            payload={"criteria":args.criteria,"threshold":args.threshold,"state":str(st)}; aid=approval_id("llm",payload)
            if not approved(l,aid) and not args.confirm_llm:
                return block_approval(lp,l,"llm",payload,f"Score Sales Nav leads with LLM against criteria '{args.criteria}'?",f"python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py approve llm --ledger {shlex.quote(str(lp))} --approval-id {aid} --confirm && python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py continue --ledger {shlex.quote(str(lp))} --criteria {shlex.quote(args.criteria)}")
            if not done(l,"score_leads") or args.force:
                cmd=[sys.executable,str(ROOT/"packs/sales-nav/primitives/score_sales_nav_leads/score_sales_nav_leads.py"),"--state",str(st),"--criteria",args.criteria,"--threshold",str(args.threshold)]
                res=require(run(cmd,timeout=args.timeout),"score_leads"); mark(lp,l,"score_leads","completed",summary=res,command=" ".join(map(shlex.quote,cmd)))
        l["current_block"]=None; save(lp,l); emit({"primitive":"sales_nav_pipeline","status":"completed","ledger":str(lp),"state":str(st),"artifacts":l.get("artifacts",{})}); return 0
    except Exception as e:
        emit({"primitive":"sales_nav_pipeline","status":"failed","error":str(e)}); return 1

def cmd_status(args):
    lp=ledger_path(args); l=load(lp); emit({"primitive":"sales_nav_pipeline","status":"ok","ledger":str(lp),"current_block":l.get("current_block"),"artifacts":l.get("artifacts",{}),"step_counts":{s:sum(1 for r in l.get('steps',{}).values() if r.get('status')==s) for s in sorted({r.get('status') for r in l.get('steps',{}).values()})}}); return 0
def cmd_approve(args):
    if not args.confirm: emit({"status":"blocked","error":"pass --confirm"}); return 2
    lp=ledger_path(args); l=load(lp); cur=l.get("current_block") or {}; aid=args.approval_id or cur.get("approval_id")
    if not aid: emit({"status":"failed","error":"no approval_id"}); return 1
    l.setdefault("approvals",{})[aid]={"confirmed":True,"type":args.kind,"approved_at":now(),"payload":cur.get("payload",{})}; l["current_block"]=None; save(lp,l); emit({"primitive":"sales_nav_pipeline","status":"ok","approval_id":aid}); return 0

def add_common(p):
    p.add_argument("--ledger"); p.add_argument("--state"); p.add_argument("--query"); p.add_argument("--set-id"); p.add_argument("--conversation-id"); p.add_argument("--run-id"); p.add_argument("--search-args-json"); p.add_argument("--response",type=Path); p.add_argument("--prefer-content",action="store_true"); p.add_argument("--enriched",action="store_true"); p.add_argument("--require-enriched",action="store_true"); p.add_argument("--count",type=int,default=25); p.add_argument("--criteria"); p.add_argument("--threshold",type=float,default=0.7); p.add_argument("--confirm-llm",action="store_true"); p.add_argument("--force",action="store_true"); p.add_argument("--timeout",type=int,default=3600)
def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    r=sub.add_parser("run"); add_common(r); r.set_defaults(func=cmd_run)
    c=sub.add_parser("continue"); add_common(c); c.set_defaults(func=cmd_run)
    s=sub.add_parser("status"); add_common(s); s.set_defaults(func=cmd_status)
    a=sub.add_parser("approve"); a.add_argument("kind",choices=["llm"]); a.add_argument("--ledger"); a.add_argument("--state"); a.add_argument("--approval-id"); a.add_argument("--confirm",action="store_true"); a.set_defaults(func=cmd_approve)
    args=ap.parse_args(); raise SystemExit(args.func(args))
if __name__=="__main__": main()
