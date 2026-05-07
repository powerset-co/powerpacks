# provision_runtime_env

Provision a local Powerpacks `.env` from GCP Secret Manager.

The primitive never prints secret values. It writes only allowlisted env keys
and reports redacted metadata.

```bash
python primitives/provision_runtime_env/provision_runtime_env.py plan --profile search-core
python primitives/provision_runtime_env/provision_runtime_env.py check --profile search-core --env-file .env
python primitives/provision_runtime_env/provision_runtime_env.py pull --profile search-core --env-file .env --confirm
```

`search-core` is the default setup profile and writes the standard Powerpacks
runtime secrets so search and messages workflows work after the normal
`$powerset login` / `$powerset env pull` flow.

Pulls require an active `@powerset.co` gcloud account. Authorization is enforced
by Google IAM on the Secret Manager resources. For user-scoped credentials,
store one secret resource per user/capability and grant access on that resource
or through a narrowly scoped IAM condition/group.
