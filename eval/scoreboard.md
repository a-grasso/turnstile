# Corpus scoreboard

- model: `module default`
- prompt digests: `secrets@7bf3e3aa7ea3`
- runs per fixture: 3

A score is a statement about the prompt digests above. Re-run after any
prompt edit and diff this file; that diff is the only evidence a change
was an improvement.

| fixture | want | pass | noise | sec | notes |
|---|---|---|---|---|---|
| `h01-test-db-compose-env` | pass | 3/3 | 0 | 13.2 |  |
| `h02-public-certificate` | pass | 3/3 | 0 | 13.8 |  |
| `h03-webhook-secret-placeholder` | pass | 2/3 | 0 | 30.5 |  |
| `h04-service-account-in-fixtures` | block | 3/3 | 0 | 40.3 |  |
| `h05-kubeconfig-base64` | block | 3/3 | 0 | 22.9 |  |
| `h06-password-assembled` | block | 3/3 | 0 | 23.3 |  |
| `n01-env-example-placeholders` | pass | 3/3 | 0 | 10.6 |  |
| `n02-read-from-environment` | pass | 3/3 | 0 | 8.7 |  |
| `n03-synthetic-test-fixtures` | pass | 3/3 | 0 | 22.2 |  |
| `n04-hashes-and-identifiers` | pass | 3/3 | 0 | 12.8 |  |
| `n05-public-identifiers` | pass | 3/3 | 0 | 20.4 |  |
| `n06-revoked-key-in-postmortem` | pass | 3/3 | 3 | 17.6 |  |
| `p01-aws-keys-settings` | block | 3/3 | 0 | 13.8 |  |
| `p02-env-file-committed` | block | 3/3 | 0 | 26.5 |  |
| `p03-openai-key-in-script` | block | 3/3 | 0 | 11.2 |  |
| `p04-github-pat-in-workflow` | block | 3/3 | 0 | 11.2 |  |
| `p05-private-key-added` | block | 3/3 | 0 | 12.1 |  |
| `p06-db-url-in-compose` | block | 3/3 | 0 | 16.0 |  |
| `p07-internal-infra-in-docs` | block | 3/3 | 0 | 21.2 |  |
