# Deploy the A01 demo (CloudFront -> one EC2 instance, username + password, no MFA)

Account 604775478403, us-east-1, user `cva-output-agent`. Needs on your Mac: AWS CLI v2, python3, zip, curl.

1. `aws sso login --profile aivar-vision-admin` then `export AWS_PROFILE=aivar-vision-admin`
2. `aws sts get-caller-identity` must show account 604775478403 (the script also checks).
3. From the unzipped folder: `./deploy/deploy.sh`
   - creates the code bucket and the sign-in secret, builds the zip, deploys the stack, waits for `/health` (10 to 15 min).
   - prints the URL and the password once: store it.
4. Open the URL, sign in, open the **AWS Transform** tab, click the connection check (pre-flight).
   - All green: continue to 5.
   - AccessDenied or "not connected": `EXTRA_POLICY_ARN=arn:aws:iam::aws:policy/AdministratorAccess ./deploy/deploy.sh`, then check again.
5. Test: **New run** -> upload an RVTools `.xlsx` (A01 runs it through AWS Transform; Cloudamize files use the offline estimate) -> AWS Transform tab -> start, approve the listing it shows -> wait -> Overview, Review and release, Outputs (PPTX, PDF, XLSX).
6. Sign-in checks: a private window on the URL lands on the sign-in page; a wrong password is refused; the EC2 public address on port 8080 does not answer.

Later: `./deploy/deploy.sh update` (new code or `RESET_PASSWORD=1`), `status`, `logs`, `destroy`.
Cost: about $35/month for t3.medium + disk; CloudFront is pennies at demo volume.
Demo data only: this is Aivar's account with one shared login, not an Ingram-designated environment (SOW 13).
