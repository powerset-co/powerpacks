# powerset_auth

Root wrapper around the shared Auth0 PKCE login primitive used by the messages
pack. Use this path for non-message Powerpacks workflows.

```bash
python primitives/powerset_auth/powerset_auth.py whoami
python primitives/powerset_auth/powerset_auth.py login
python primitives/powerset_auth/powerset_auth.py token --bearer-only
python primitives/powerset_auth/powerset_auth.py logout
```

Credentials are stored at `~/.powerpacks/credentials.json` with mode `0600`.
