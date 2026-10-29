# Security

The alpha is a local research application. It binds to loopback by default, has no authentication or multi-user authorization, and has no broker integration. Do not expose it to the internet as a paid-model service.

## Reporting

The [public repository](https://github.com/opblackyo/quantagent-lab) has GitHub private vulnerability reporting enabled; authenticated and unauthenticated API status checks confirmed this on 2026-10-24. Use the repository Security tab's private reporting option. No test vulnerability report was submitted. Do not open a public issue containing credentials, account identifiers, model responses, databases or a working exploit against a private service. No personal contact address is published here.

## Credentials and artifacts

Keep keys in the backend environment or a private secret manager. Do not use frontend/public environment variables for secrets. `.env.example` has empty values and is documentation only; dotenv loading is not implemented. Model prompts receive numeric features and validated predecessor results, not credentials.

Artifacts, real data, runtime databases and local development configuration are private by default. Exports include raw responses and require review before sharing. Public examples must use synthetic Mock/Replay. Pattern scanners cannot prove the absence of every secret; images/video require visual review.

If a credential is committed, stop publication, revoke/rotate it, then approve a history-cleaning plan. Deleting the current file does not clean Git history. Do not rewrite history or force-push without explicit owner approval.

Reference: [GitHub guidance on removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
