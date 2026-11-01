# Stable, non-secret live inputs belong in version control so every operator
# plans against the same identity foundation and federation allowlist.
project_id      = "psd401-prr-identity"
organization_id = "482073499306"

aws_runtimes = {
  prod = {
    account_id = "338414773271"
    role_name  = "psd401-prr-prod-runtime"
  }
}
