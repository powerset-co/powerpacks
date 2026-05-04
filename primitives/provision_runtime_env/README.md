# provision_runtime_env

Provision a local Powerpacks `.env` from a Powerset-controlled source.

The primitive never prints secret values. It writes only allowlisted env keys
and reports redacted metadata.

```bash
python primitives/provision_runtime_env/provision_runtime_env.py plan --profile search-core
python primitives/provision_runtime_env/provision_runtime_env.py check --profile search-core --env-file .env
python primitives/provision_runtime_env/provision_runtime_env.py pull --profile search-core --env-file .env --confirm
```

Sources:

- `search-api`: preferred long-term path for per-user scoped keys and usage
  attribution.
- `gcp`: internal bootstrap fallback using `gcloud secrets versions access`.
- `auto`: try search-api first, then GCP.

Pulls require a cached Powerset Auth0 session from `powerset_auth login` and
reject non-`@powerset.co` accounts locally. Real authorization must still be
enforced by search-api or GCP IAM.
