# TDnet Disclosure Alerts

This job queries TDnet once per day for the configured stock codes and sends a
plain-text SMTP digest containing matching disclosures and their official TDnet
links.

The tracked codes are configured in `main.py`:

- `441A`
- `1450` (Tanaken; TDnet displays it as `14500`)
- `6658`

TDnet searches by substring, so `main.py` applies an exact-code filter and only
allows the known trailing-zero display format. A service error, malformed
response, partial query failure, invalid link, or SMTP failure exits nonzero so
GitHub Actions does not report a false success.

## Local run

Copy `.env.example` to `.env` and fill in the SMTP values. Keep `.env` private;
it is ignored by Git. Then run:

```sh
python -m pip install -r requirements.txt
python main.py
```

By default the job checks yesterday in `Asia/Tokyo`. Set
`TDNET_REPORT_DATE=YYYYMMDD` for a one-off recovery run.

## Deployment

GitHub Actions is the deployment/runtime. The scheduled workflow runs at
15:05 UTC (00:05 JST) and receives SMTP settings from repository secrets. A
manual run can supply the optional `report_date` input to retry a missed day.
The separate CI workflow runs dependency auditing, linting, and tests on pushes
and pull requests without accessing SMTP secrets.

## Checks

```sh
python -m unittest -v
ruff check main.py test_main.py
python -m pip_audit --requirement requirements.txt
```
