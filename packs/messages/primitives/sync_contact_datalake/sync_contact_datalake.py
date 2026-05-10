#!/usr/bin/env python3
"""Sync reviewed messages research rows to the contact datalake endpoint.

Reads `research_review.csv`, joins per-handle `01_research_parallel.json` files,
and POSTs usable contact/linkedin/profile payloads to
`/v2/contact-datalake/import`.

Stdlib-only. Does not upload unless `sync --confirm-sync` is passed.
"""
from __future__ import annotations

import argparse, csv, hashlib, json, os, re, subprocess, sys, urllib.error, urllib.request, uuid
from pathlib import Path
from typing import Any

DEFAULT_API_URL="https://search-api-7wk4uhe77q-uw.a.run.app"
DEFAULT_CSV=Path(".powerpacks/messages/research_review.csv")
DEFAULT_RESEARCH_DIR=Path(".powerpacks/messages/research")
DEFAULT_RETARGET_RESEARCH_DIR=Path(".powerpacks/messages/research_retarget")
UUID_NAMESPACE=uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

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
def approved(row):
    ex=(row.get("exclude") or "").strip().lower()
    if ex in {"yes","true","1"}: return False
    if ex in {"no","false","0"}: return True
    b=(row.get("bucket") or "").strip().lower()
    return b in {"confident","yes"}


def should_sync_record(rec):
    # Product invariant: only approved contacts are synced to the contact
    # datalake. Maybe/no rows can exist in review artifacts, but they must not
    # become staged contact records.
    return bool(rec.get("approved"))
def profile_for(handle, research_dir):
    p=research_dir/handle/"01_research_parallel.json"
    return read_json(p,{}) if p.exists() else {}
def profile_for_row(row, research_dir, retarget_research_dir):
    retarget_handle=(row.get("retarget_handle") or "").strip()
    if retarget_handle:
        prof=profile_for(retarget_handle, retarget_research_dir)
        if prof:
            return prof
    handle=(row.get("handle") or "").strip()
    return profile_for(handle,research_dir) if handle else {}
def normalize_phone(raw):
    s=str(raw or "").strip()
    if not s: return ""
    if s.startswith("+"):
        digits="".join(ch for ch in s[1:] if ch.isdigit())
        return "+"+digits if digits else ""
    digits="".join(ch for ch in s if ch.isdigit())
    return "+"+digits if digits else ""
def phone_source_key(phone, fallback):
    normalized=normalize_phone(phone)
    return "phone-"+hashlib.sha1(normalized.encode()).hexdigest()[:12] if normalized else (fallback or None)
def linkedin_from_profile(profile):
    return (((profile or {}).get("social") or {}).get("linkedin_url") or "").strip()
def public_identifier_from_url(linkedin_url):
    m=re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url or "", re.I)
    return m.group(1).lower().rstrip("/") if m else ""
def canonical_linkedin_url(linkedin_url):
    pub=public_identifier_from_url(linkedin_url)
    return f"https://www.linkedin.com/in/{pub}" if pub else ""
def normalize_twitter(raw):
    s=str(raw or "").strip().lstrip("@").strip().lower()
    return s if re.match(r"^[a-z0-9_]{1,15}$",s) else ""
def compute_public_identifier(social, person):
    pub=public_identifier_from_url(social.get("linkedin_url") or "")
    if pub: return pub
    tw=normalize_twitter(social.get("twitter_handle"))
    if tw: return f"synth-x-{tw}"
    email=(social.get("primary_email") or "").strip().lower()
    if email: return "synth-"+hashlib.md5(email.encode()).hexdigest()[:8]
    phone=(social.get("primary_phone") or "").strip()
    if phone: return "synth-phone-"+hashlib.md5(phone.encode()).hexdigest()[:8]
    name=(person.get("full_name") or "unknown").strip().lower()
    return "synth-"+hashlib.md5(name.encode()).hexdigest()[:8]
def generate_person_id(public_identifier):
    return str(uuid.uuid5(UUID_NAMESPACE, f"linkedin:{public_identifier.lower()}"))
def norm_text(v):
    if v is None: return None
    if isinstance(v,list):
        parts=[str(x).replace("\x00","").strip() for x in v if x is not None and str(x).strip()]
        return "\n".join(parts) or None
    s=str(v).replace("\x00","").strip()
    return s or None
def to_harmonic_date(raw):
    if not raw: return None
    parts=str(raw).split("-")
    try:
        y=int(parts[0]); m=int(parts[1]) if len(parts)>1 else 1; d=int(parts[2]) if len(parts)>2 else 1
        return f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z"
    except (ValueError,TypeError): return None

def synthetic_profile_from_research(profile, row):
    """Build an Aleph-compatible 04_final_profile-style draft.

    Company URNs are intentionally blank here; the downstream synthetic pipeline
    can resolve companies/Harmonic and replace this draft before materializing.
    """
    if not isinstance(profile,dict) or not profile: return None
    person=profile.get("person") or {}; social=profile.get("social") or {}; loc=profile.get("location") or {}
    pub=compute_public_identifier(social, person); person_id=generate_person_id(pub)
    work=[]
    for pos in profile.get("positions") or []:
        if float(pos.get("confidence") or 0) < 0.7: continue
        end_raw=pos.get("end_date")
        if str(end_raw or "").lower().strip() in {"present","current","now"}: end_raw=None
        start=to_harmonic_date(pos.get("start_date")); end=to_harmonic_date(end_raw)
        work.append({
            "contact":{"emails":[],"phone_numbers":[],"exec_emails":[],"primary_email":None,"primary_email_person_id":None},
            "title":pos.get("title") or "",
            "department":"",
            "description":norm_text(pos.get("description")),
            "start_date":start,
            "end_date":end,
            "is_current_position":bool(pos.get("is_current")) or bool(start and not end),
            "location":None,
            "role_type":"",
            "company":"",
            "company_name":pos.get("company_name") or "",
        })
    edu=[]
    for e in profile.get("education") or []:
        if float(e.get("confidence") or 0) < 0.5 or not e.get("school_name"): continue
        edu.append({
            "school":{"name":e.get("school_name") or "","linkedin_url":None,"logo_url":None,"entity_urn":None},
            "degree":e.get("degree") or None,
            "field":e.get("field_of_study") or None,
            "grade":None,
            "start_date":f"{e['start_year']}-01-01T00:00:00Z" if e.get("start_year") else None,
            "end_date":f"{e['end_year']}-12-31T00:00:00Z" if e.get("end_year") else None,
        })
    current=next((w for w in work if w.get("is_current_position")), work[0] if work else {})
    return {
        "person_id":person_id,
        "public_identifier":pub,
        "enrichment_provider":"synthetic",
        "provider_entity_urn":f"synthetic:{pub}",
        "full_name":person.get("full_name") or "",
        "first_name":person.get("first_name") or "",
        "last_name":person.get("last_name") or "",
        "headline":(profile.get("headline") or {}).get("text") or "",
        "summary":(profile.get("summary") or {}).get("text"),
        "city":loc.get("city"),"state":loc.get("state"),"country":loc.get("country"),"location_raw":loc.get("raw"),
        "x_twitter_handle":normalize_twitter(social.get("twitter_handle")),
        "linkedin_url":canonical_linkedin_url(social.get("linkedin_url") or ""),
        "public_profile_url":canonical_linkedin_url(social.get("linkedin_url") or ""),
        "work_experiences":work,
        "education":edu,
        "current_title":current.get("title") or None,
        "current_company_urn":"",
        "synthetic_metadata":{
            "research_id":profile.get("research_id"),
            "confidence":(profile.get("metadata") or {}).get("estimated_completeness",0),
            "sources_count":(profile.get("metadata") or {}).get("total_sources_consulted",0),
            "gaps":(profile.get("metadata") or {}).get("gaps",[]),
            "research_date":(profile.get("metadata") or {}).get("research_date"),
            "primary_email":social.get("primary_email"),
            "primary_phone":social.get("primary_phone") or row.get("phone_e164"),
            "source_channel":"phone",
            "version":1,
            "draft":True,
        },
    }
def parse_int_field(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def row_to_record(row, research_dir, retarget_research_dir):
    handle=(row.get("handle") or "").strip()
    prof=profile_for_row(row,research_dir,retarget_research_dir)
    person=(prof.get("person") or {}) if isinstance(prof,dict) else {}
    social=(prof.get("social") or {}) if isinstance(prof,dict) else {}
    synthetic=synthetic_profile_from_research(prof,row)
    linkedin=canonical_linkedin_url(row.get("retarget_linkedin_url") or linkedin_from_profile(prof))
    phone=row.get("phone_e164") or social.get("primary_phone")
    full_name=row.get("full_name") or row.get("name") or person.get("full_name")
    is_approved=approved(row)
    return {
        "source_key": phone_source_key(phone, handle),
        "handle": handle or None,
        "retarget_handle": (row.get("retarget_handle") or None),
        "phone_e164": normalize_phone(phone),
        "phone": normalize_phone(phone),
        "full_name": full_name,
        "name": full_name,
        "source_channel": row.get("message_source") or "messages_research",
        "message_count": parse_int_field(row.get("total_messages")),
        "imessage_message_count": parse_int_field(row.get("imessage_message_count")),
        "whatsapp_message_count": parse_int_field(row.get("whatsapp_message_count")),
        "last_message": row.get("last_message") or None,
        "imessage_last_message": row.get("imessage_last_message") or None,
        "whatsapp_last_message": row.get("whatsapp_last_message") or None,
        "bucket": row.get("bucket") or "",
        "approved": is_approved,
        "linkedin_url": linkedin,
        "public_identifier": (synthetic or {}).get("public_identifier"),
        "research_profile": prof or None,
        "synthetic_profile": synthetic,
        "processing_status": "staged",
        "raw_record": row,
    }
def load_records(csv_path, research_dir, retarget_research_dir=DEFAULT_RETARGET_RESEARCH_DIR):
    with csv_path.open(newline='',encoding='utf-8-sig') as f:
        rows=list(csv.DictReader(f))
    records=[]
    for row in rows:
        rec=row_to_record(row,research_dir,retarget_research_dir)
        if not should_sync_record(rec): continue
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
def summarize_sync_response(body):
    """Return user-facing sync summary without backend table/materialization details."""
    inserted=int(body.get("datalake_inserted") or 0) if isinstance(body,dict) else 0
    updated=int(body.get("datalake_updated") or 0) if isinstance(body,dict) else 0
    uploaded = inserted + updated
    return {
        "uploaded_contacts": uploaded,
        "message": f"Uploaded {uploaded} contacts",
        "errors": int(body.get("errors") or 0) if isinstance(body,dict) else 0,
    }
def cmd_build(args):
    recs=load_records(Path(args.csv),Path(args.research_dir),Path(args.retarget_research_dir))
    linked=sum(1 for r in recs if r.get("linkedin_url"))
    named=sum(1 for r in recs if r.get("full_name") or r.get("name"))
    phoned=sum(1 for r in recs if r.get("phone_e164") or r.get("phone"))
    synthetic=sum(1 for r in recs if r.get("synthetic_profile"))
    payload={"records":recs,"source":"messages_research_review","dry_run":bool(args.dry_run)}
    if args.output:
        Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n")
    emit({"primitive":"sync_contact_datalake","command":"build","csv":args.csv,"research_dir":args.research_dir,"records":len(recs),"with_name":named,"with_phone":phoned,"with_linkedin_url":linked,"with_synthetic_profile":synthetic,"output":args.output})
    return 0
def cmd_sync(args):
    if not args.confirm_sync:
        emit({"primitive":"sync_contact_datalake","command":"sync","status":"blocked","error":"pass --confirm-sync after explicit approval"}); return 2
    recs=load_records(Path(args.csv),Path(args.research_dir),Path(args.retarget_research_dir))
    status,body=post_json(args.api_url, args.token or auth_token(), {"records":recs,"source":"messages_research_review","dry_run":False}, timeout=args.timeout)
    emit({"primitive":"sync_contact_datalake","command":"sync","status":"ok","status_code":status,**summarize_sync_response(body)})
    return 0
def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd',required=True)
    for name in ['build','sync']:
        s=sub.add_parser(name); s.add_argument('--csv',default=str(DEFAULT_CSV)); s.add_argument('--research-dir',default=str(DEFAULT_RESEARCH_DIR)); s.add_argument('--retarget-research-dir',default=str(DEFAULT_RETARGET_RESEARCH_DIR)); s.set_defaults(func=cmd_build if name=='build' else cmd_sync)
        if name=='build': s.add_argument('--output'); s.add_argument('--dry-run',action='store_true')
        else: s.add_argument('--api-url',default=os.getenv('POWERPACKS_API_URL') or os.getenv('POWERSET_API_URL') or DEFAULT_API_URL); s.add_argument('--token'); s.add_argument('--timeout',type=int,default=120); s.add_argument('--confirm-sync',action='store_true')
    a=p.parse_args(); raise SystemExit(a.func(a))
if __name__=='__main__': main()
