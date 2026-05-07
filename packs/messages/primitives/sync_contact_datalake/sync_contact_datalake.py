#!/usr/bin/env python3
"""Sync reviewed messages research rows to the contact datalake endpoint.

Reads `research_review.csv`, joins per-handle `01_research_parallel.json` files,
and POSTs usable contact/linkedin/profile payloads to
`/v2/contact-datalake/import`.

Stdlib-only. Does not upload unless `sync --confirm-sync` is passed.
"""
from __future__ import annotations

import argparse, csv, json, os, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_URL="https://search-api-7wk4uhe77q-uw.a.run.app"
DEFAULT_CSV=Path(".powerpacks/messages/research_review.csv")
DEFAULT_RESEARCH_DIR=Path(".powerpacks/messages/research")

def emit(x): print(json.dumps(x,indent=2,sort_keys=True))
def read_json(p:Path,default=None):
    if not p.exists(): return default
    return json.loads(p.read_text())
def repo_root(): return Path(__file__).resolve().parents[4]
def auth_token():
    cmd=[sys.executable,str(repo_root()/"packs/powerset/primitives/auth/auth.py"),"token","--bearer-only"]
    p=subprocess.run(cmd,cwd=repo_root(),text=True,capture_output=True)
    if p.returncode!=0 or not p.stdout.strip(): raise RuntimeError("could not get Powerset token; run $powerset login")
    return p.stdout.strip()
def decision(row):
    ex=(row.get("exclude") or "").strip().lower()
    if ex in {"yes","true","1"}: return "exclude"
    if ex in {"no","false","0"}: return "include"
    b=(row.get("bucket") or "").strip().lower()
    if b in {"confident","yes"}: return "include"
    if b in {"review","no"}: return "exclude"
    return "bucket_default"
def profile_for(handle, research_dir):
    p=research_dir/handle/"01_research_parallel.json"
    return read_json(p,{}) if p.exists() else {}
def linkedin_from_profile(profile):
    return (((profile or {}).get("social") or {}).get("linkedin_url") or "").strip()
def row_to_record(row, research_dir):
    handle=(row.get("handle") or "").strip()
    prof=profile_for(handle,research_dir) if handle else {}
    person=(prof.get("person") or {}) if isinstance(prof,dict) else {}
    social=(prof.get("social") or {}) if isinstance(prof,dict) else {}
    return {
        "source_key": handle or None,
        "handle": handle or None,
        "phone_e164": row.get("phone_e164") or social.get("primary_phone"),
        "full_name": row.get("full_name") or person.get("full_name"),
        "source_channel": row.get("message_source") or "messages_research",
        "message_count": int(float(row.get("total_messages") or 0)),
        "bucket": row.get("bucket") or "",
        "upload_decision": decision(row),
        "linkedin_url": linkedin_from_profile(prof),
        "research_profile": prof or None,
        "raw_record": row,
    }
def load_records(csv_path, research_dir, only_include=False):
    with csv_path.open(newline='',encoding='utf-8-sig') as f:
        rows=list(csv.DictReader(f))
    records=[]
    for row in rows:
        rec=row_to_record(row,research_dir)
        if only_include and rec["upload_decision"]=="exclude": continue
        records.append(rec)
    return records
def post_json(api_url, token, payload, timeout=120):
    data=json.dumps(payload).encode()
    req=urllib.request.Request(api_url.rstrip()+"/v2/contact-datalake/import",data=data,method="POST",headers={"Authorization":"Bearer "+token,"Content-Type":"application/json","Accept":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return r.status,json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e
def cmd_build(args):
    recs=load_records(Path(args.csv),Path(args.research_dir),args.only_include)
    linked=sum(1 for r in recs if r.get("linkedin_url"))
    payload={"records":recs,"source":"messages_research_review","dry_run":bool(args.dry_run)}
    if args.output:
        Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n")
    emit({"primitive":"sync_contact_datalake","command":"build","csv":args.csv,"research_dir":args.research_dir,"records":len(recs),"with_linkedin_url":linked,"output":args.output})
    return 0
def cmd_sync(args):
    if not args.confirm_sync:
        emit({"primitive":"sync_contact_datalake","command":"sync","status":"blocked","error":"pass --confirm-sync after explicit approval"}); return 2
    recs=load_records(Path(args.csv),Path(args.research_dir),args.only_include)
    status,body=post_json(args.api_url, args.token or auth_token(), {"records":recs,"source":"messages_research_review","dry_run":False}, timeout=args.timeout)
    emit({"primitive":"sync_contact_datalake","command":"sync","status":"ok","status_code":status,"response":body})
    return 0
def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd',required=True)
    for name in ['build','sync']:
        s=sub.add_parser(name); s.add_argument('--csv',default=str(DEFAULT_CSV)); s.add_argument('--research-dir',default=str(DEFAULT_RESEARCH_DIR)); s.add_argument('--only-include',action='store_true'); s.set_defaults(func=cmd_build if name=='build' else cmd_sync)
        if name=='build': s.add_argument('--output'); s.add_argument('--dry-run',action='store_true')
        else: s.add_argument('--api-url',default=os.getenv('POWERPACKS_API_URL') or os.getenv('POWERSET_API_URL') or DEFAULT_API_URL); s.add_argument('--token'); s.add_argument('--timeout',type=int,default=120); s.add_argument('--confirm-sync',action='store_true')
    a=p.parse_args(); raise SystemExit(a.func(a))
if __name__=='__main__': main()
